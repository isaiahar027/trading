"""Windows implementation of :class:`~amongus_desk.execution.unitrader.backend.GuiBackend`.

Windows only. Importing this module works on every platform (nothing Windows-specific is touched at
import time); constructing :class:`Win32Backend` anywhere else raises :class:`Win32UnavailableError`
(a :class:`RuntimeError`). Nothing here knows anything about the UniTrader terminal: it only lists
windows, reads pixels, moves the mouse and presses keys where the caller says.

Implementation notes (each one is there for a reason, keep them):

* **ctypes with explicit ``argtypes``/``restype`` on every Win32 function.** Without them ctypes
  passes and returns C ``int`` (32-bit), which silently truncates 64-bit handles. The functions are
  bound on this module's *own* ``ctypes.WinDLL`` instances (``use_last_error=True``), never on the
  shared ``ctypes.windll`` objects, so setting ``argtypes`` cannot change how pyautogui or any other
  library calls the same functions.
* **DPI awareness before pyautogui.** The process is made per-monitor DPI aware
  (``SetProcessDpiAwarenessContext`` → ``SetProcessDpiAwareness`` → ``SetProcessDPIAware``) *before*
  pyautogui is imported, because pyautogui calls ``SetProcessDPIAware()`` (system aware) on import and
  the first call wins. All coordinates are then physical pixels, like the screenshots.
* **pyautogui** with ``FAILSAFE = True``: slamming the mouse into a screen corner raises
  ``FailSafeException``, which becomes :class:`~.backend.OperatorAbortError`. Clicks always pass
  ``x=``/``y=`` as integers. Key names and typed characters are validated first, because pyautogui
  silently ignores keys it does not know.
* **Clipboard.** Opened with a retry loop (another process may hold it), always closed in
  ``finally``. Text is written as UTF-16-LE into ``GlobalAlloc(GMEM_MOVEABLE)`` memory owned by a
  temporary message-only window (``OpenClipboard(NULL)`` + ``EmptyClipboard`` makes the documented
  owner NULL, which can make ``SetClipboardData`` fail). The memory is freed only if
  ``SetClipboardData`` did not take ownership of it. Every write is read back and compared, so a
  failed write can never leave stale text behind that a lot readback would mistake for its own.
* **Foreground.** ``SetForegroundWindow`` is subject to the foreground lock. If the plain call does
  not work, a synthetic Alt key press makes this process the one that "received the last input event"
  (one of the documented conditions), ``AllowSetForegroundWindow`` is granted to the target process,
  and the call is repeated. The Alt key is always released in ``finally``.
* ``restore_window`` only calls ``ShowWindow(SW_RESTORE)`` for a *minimized* window: on a maximized
  window ``SW_RESTORE`` would un-maximize it, i.e. resize it.
* ``root_window_at`` uses ``GetAncestor(GA_ROOT)`` (not ``GA_ROOTOWNER``) so a popup owned by the
  terminal that covers a button is reported as a different window.
"""

from __future__ import annotations

import ctypes
import importlib
import logging
import ntpath
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast

from PIL import Image

from ...config import UniTraderConfig
from .backend import BackendError, OperatorAbortError, WindowInfo

log = logging.getLogger(__name__)

_T = TypeVar("_T")

# --- documented Win32 constants (winuser.h, winbase.h, winnt.h, shellscalingapi.h) ---------------------
SW_RESTORE = 9
GA_ROOT = 2
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DESKTOP_SWITCHDESKTOP = 0x0100
VK_MENU = 0x12  # the Alt key
KEYEVENTF_KEYUP = 0x0002
MAPVK_VK_TO_VSC = 0
HWND_MESSAGE = -3  # parent handle that makes CreateWindowEx create a message-only window
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
PROCESS_PER_MONITOR_DPI_AWARE = 2
USER_DEFAULT_SCREEN_DPI = 96
S_OK = 0
E_ACCESSDENIED = -2147024891  # HRESULT 0x80070005 as a signed 32-bit value
ERROR_ACCESS_DENIED = 5
ERROR_INSUFFICIENT_BUFFER = 122

_AWARENESS_NAMES = {0: "unaware", 1: "system", 2: "per-monitor"}  # DPI_AWARENESS enum values
_PER_MONITOR_OUTCOMES = ("per-monitor-v2", "per-monitor")
_MAX_CLIPBOARD_BYTES = 1 << 20  # never copy more than 1 MiB out of someone else's clipboard data
_MAX_IMAGE_PATH_CHARS = 32_768  # longest extended-length path QueryFullProcessImageNameW can return
_FOREGROUND_SETTLE_SECONDS = 0.25
_RESTORE_SETTLE_SECONDS = 1.0
_POLL_SECONDS = 0.02

_OFF_WINDOWS_MESSAGE = (
    "the win32 GUI backend only works on Windows, inside a logged-in, unlocked desktop session on the "
    "machine that runs UniTrader (this is {platform}). Use execution.unitrader.backend: fake, or "
    "`desk calibrate --simulate`, for the built-in simulator."
)


class Win32UnavailableError(BackendError, RuntimeError):
    """The Win32 backend can not run here (not Windows, or pyautogui is not installed)."""


# ---------------------------------------------------------------------------------------- raw bindings


@dataclass(frozen=True, slots=True)
class _Win32Functions:
    """Raw Win32 entry points, each with ``argtypes``/``restype`` already set (see :func:`_bind_win32`).

    Optional entries are ``None`` when the running Windows version does not export them.
    """

    # user32: windows
    EnumWindows: Callable[..., Any]
    GetWindowTextLengthW: Callable[..., Any]
    GetWindowTextW: Callable[..., Any]
    IsWindow: Callable[..., Any]
    IsWindowVisible: Callable[..., Any]
    IsIconic: Callable[..., Any]
    GetClientRect: Callable[..., Any]
    ClientToScreen: Callable[..., Any]
    GetWindowThreadProcessId: Callable[..., Any]
    GetForegroundWindow: Callable[..., Any]
    SetForegroundWindow: Callable[..., Any]
    AllowSetForegroundWindow: Callable[..., Any]
    ShowWindow: Callable[..., Any]
    WindowFromPoint: Callable[..., Any]
    GetAncestor: Callable[..., Any]
    GetDpiForWindow: Callable[..., Any] | None  # Windows 10 1607+
    OpenInputDesktop: Callable[..., Any]
    CloseDesktop: Callable[..., Any]
    keybd_event: Callable[..., Any]
    MapVirtualKeyW: Callable[..., Any]
    CreateWindowExW: Callable[..., Any]
    DestroyWindow: Callable[..., Any]
    # user32: clipboard
    OpenClipboard: Callable[..., Any]
    CloseClipboard: Callable[..., Any]
    EmptyClipboard: Callable[..., Any]
    IsClipboardFormatAvailable: Callable[..., Any]
    GetClipboardData: Callable[..., Any]
    SetClipboardData: Callable[..., Any]
    # DPI awareness, newest first
    SetProcessDpiAwarenessContext: Callable[..., Any] | None  # user32, Windows 10 1703+
    SetProcessDpiAwareness: Callable[..., Any] | None  # shcore, Windows 8.1+
    SetProcessDPIAware: Callable[..., Any] | None  # user32, Vista+
    GetThreadDpiAwarenessContext: Callable[..., Any] | None  # user32, Windows 10 1607+
    GetAwarenessFromDpiAwarenessContext: Callable[..., Any] | None  # user32, Windows 10 1607+
    # kernel32
    OpenProcess: Callable[..., Any]
    QueryFullProcessImageNameW: Callable[..., Any]
    CloseHandle: Callable[..., Any]
    GlobalAlloc: Callable[..., Any]
    GlobalLock: Callable[..., Any]
    GlobalUnlock: Callable[..., Any]
    GlobalFree: Callable[..., Any]
    GlobalSize: Callable[..., Any]
    # helpers
    WNDENUMPROC: Callable[..., Any]  # ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM)
    get_last_error: Callable[[], int]
    format_error: Callable[[int], str]


def _bind_win32() -> _Win32Functions:
    """Load user32/kernel32/shcore and bind every function the backend uses (Windows only)."""
    if sys.platform == "win32":
        from ctypes import wintypes  # noqa: PLC0415 - Windows-only, imported lazily on purpose

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shcore: ctypes.WinDLL | None
        try:
            shcore = ctypes.WinDLL("shcore", use_last_error=True)
        except OSError:  # shcore.dll exists from Windows 8.1 on
            shcore = None

        def bind(dll: ctypes.WinDLL, name: str, restype: Any, *argtypes: Any) -> Callable[..., Any]:
            func = getattr(dll, name)
            func.restype = restype
            func.argtypes = list(argtypes)
            return cast(Callable[..., Any], func)

        def bind_optional(
            dll: ctypes.WinDLL | None, name: str, restype: Any, *argtypes: Any
        ) -> Callable[..., Any] | None:
            if dll is None:
                return None
            try:
                return bind(dll, name, restype, *argtypes)
            except AttributeError:
                log.debug("%s is not available on this Windows version", name)
                return None

        hwnd = wintypes.HWND
        handle = wintypes.HANDLE
        hglobal = wintypes.HGLOBAL
        dword = wintypes.DWORD
        uint = wintypes.UINT
        boolean = wintypes.BOOL
        lpdword = ctypes.POINTER(wintypes.DWORD)
        wndenumproc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        return _Win32Functions(
            EnumWindows=bind(user32, "EnumWindows", boolean, wndenumproc, wintypes.LPARAM),
            GetWindowTextLengthW=bind(user32, "GetWindowTextLengthW", ctypes.c_int, hwnd),
            GetWindowTextW=bind(user32, "GetWindowTextW", ctypes.c_int, hwnd, wintypes.LPWSTR, ctypes.c_int),
            IsWindow=bind(user32, "IsWindow", boolean, hwnd),
            IsWindowVisible=bind(user32, "IsWindowVisible", boolean, hwnd),
            IsIconic=bind(user32, "IsIconic", boolean, hwnd),
            GetClientRect=bind(user32, "GetClientRect", boolean, hwnd, ctypes.POINTER(wintypes.RECT)),
            ClientToScreen=bind(user32, "ClientToScreen", boolean, hwnd, ctypes.POINTER(wintypes.POINT)),
            GetWindowThreadProcessId=bind(user32, "GetWindowThreadProcessId", dword, hwnd, lpdword),
            GetForegroundWindow=bind(user32, "GetForegroundWindow", hwnd),
            SetForegroundWindow=bind(user32, "SetForegroundWindow", boolean, hwnd),
            AllowSetForegroundWindow=bind(user32, "AllowSetForegroundWindow", boolean, dword),
            ShowWindow=bind(user32, "ShowWindow", boolean, hwnd, ctypes.c_int),
            WindowFromPoint=bind(user32, "WindowFromPoint", hwnd, wintypes.POINT),
            GetAncestor=bind(user32, "GetAncestor", hwnd, hwnd, uint),
            GetDpiForWindow=bind_optional(user32, "GetDpiForWindow", uint, hwnd),
            OpenInputDesktop=bind(user32, "OpenInputDesktop", wintypes.HDESK, dword, boolean, dword),
            CloseDesktop=bind(user32, "CloseDesktop", boolean, wintypes.HDESK),
            keybd_event=bind(user32, "keybd_event", None, wintypes.BYTE, wintypes.BYTE, dword, ctypes.c_size_t),
            MapVirtualKeyW=bind(user32, "MapVirtualKeyW", uint, uint, uint),
            CreateWindowExW=bind(
                user32,
                "CreateWindowExW",
                hwnd,
                dword,  # dwExStyle
                wintypes.LPCWSTR,  # lpClassName
                wintypes.LPCWSTR,  # lpWindowName
                dword,  # dwStyle
                ctypes.c_int,  # X
                ctypes.c_int,  # Y
                ctypes.c_int,  # nWidth
                ctypes.c_int,  # nHeight
                hwnd,  # hWndParent
                wintypes.HMENU,  # hMenu
                wintypes.HINSTANCE,  # hInstance
                wintypes.LPVOID,  # lpParam
            ),
            DestroyWindow=bind(user32, "DestroyWindow", boolean, hwnd),
            OpenClipboard=bind(user32, "OpenClipboard", boolean, hwnd),
            CloseClipboard=bind(user32, "CloseClipboard", boolean),
            EmptyClipboard=bind(user32, "EmptyClipboard", boolean),
            IsClipboardFormatAvailable=bind(user32, "IsClipboardFormatAvailable", boolean, uint),
            GetClipboardData=bind(user32, "GetClipboardData", handle, uint),
            SetClipboardData=bind(user32, "SetClipboardData", handle, uint, handle),
            SetProcessDpiAwarenessContext=bind_optional(user32, "SetProcessDpiAwarenessContext", boolean, handle),
            SetProcessDpiAwareness=bind_optional(shcore, "SetProcessDpiAwareness", ctypes.c_long, ctypes.c_int),
            SetProcessDPIAware=bind_optional(user32, "SetProcessDPIAware", boolean),
            GetThreadDpiAwarenessContext=bind_optional(user32, "GetThreadDpiAwarenessContext", handle),
            GetAwarenessFromDpiAwarenessContext=bind_optional(
                user32, "GetAwarenessFromDpiAwarenessContext", ctypes.c_int, handle
            ),
            OpenProcess=bind(kernel32, "OpenProcess", handle, dword, boolean, dword),
            QueryFullProcessImageNameW=bind(
                kernel32, "QueryFullProcessImageNameW", boolean, handle, dword, wintypes.LPWSTR, lpdword
            ),
            CloseHandle=bind(kernel32, "CloseHandle", boolean, handle),
            GlobalAlloc=bind(kernel32, "GlobalAlloc", hglobal, uint, ctypes.c_size_t),
            GlobalLock=bind(kernel32, "GlobalLock", wintypes.LPVOID, hglobal),
            GlobalUnlock=bind(kernel32, "GlobalUnlock", boolean, hglobal),
            GlobalFree=bind(kernel32, "GlobalFree", hglobal, hglobal),
            GlobalSize=bind(kernel32, "GlobalSize", ctypes.c_size_t, hglobal),
            WNDENUMPROC=wndenumproc,
            get_last_error=ctypes.get_last_error,
            format_error=ctypes.FormatError,
        )
    raise Win32UnavailableError(_OFF_WINDOWS_MESSAGE.format(platform=sys.platform))


def _decode_clipboard_text(raw: bytes) -> str:
    """Decode CF_UNICODETEXT bytes (UTF-16-LE), stopping at the first NUL; never reads past ``raw``."""
    if len(raw) % 2:
        raw = raw[:-1]
    text = raw.decode("utf-16-le", errors="replace")
    end = text.find("\x00")
    return text if end < 0 else text[:end]


class _Win32Api:
    """Typed, error-checked wrappers around the raw Win32 functions (the only code that touches ctypes)."""

    def __init__(
        self,
        fns: _Win32Functions,
        *,
        clipboard_attempts: int = 10,
        clipboard_retry_seconds: float = 0.05,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if clipboard_attempts < 1:
            raise ValueError("clipboard_attempts must be at least 1")
        if clipboard_retry_seconds < 0:
            raise ValueError("clipboard_retry_seconds must not be negative")
        self._f = fns
        self._clipboard_attempts = clipboard_attempts
        self._clipboard_retry = clipboard_retry_seconds
        self._sleep = sleep
        self._clipboard_lock = threading.RLock()

    def _error(self, code: int | None = None) -> str:
        code = self._f.get_last_error() if code is None else code
        try:
            text = self._f.format_error(code).strip()
        except (OSError, ValueError):
            text = ""
        return f"Win32 error {code}" + (f" ({text})" if text else "")

    # --- DPI ------------------------------------------------------------------------------------------

    def make_dpi_aware(self) -> str:
        """Make the process per-monitor DPI aware (best available API). Returns what was achieved."""
        f = self._f
        if f.SetProcessDpiAwarenessContext is not None:
            if f.SetProcessDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
                return "per-monitor-v2"
            code = f.get_last_error()
            if code == ERROR_ACCESS_DENIED:
                return "already-set"
            log.debug("SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2) failed: %s", self._error(code))
        if f.SetProcessDpiAwareness is not None:
            hresult = int(f.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE))
            if hresult == S_OK:
                return "per-monitor"
            if hresult == E_ACCESSDENIED:
                return "already-set"
            log.debug("SetProcessDpiAwareness(PER_MONITOR) failed: HRESULT %#010x", hresult & 0xFFFFFFFF)
        if f.SetProcessDPIAware is not None and f.SetProcessDPIAware():
            return "system"
        return "unaware"

    def dpi_awareness_level(self) -> int | None:
        """DPI_AWARENESS of the calling thread (0 unaware, 1 system, 2 per-monitor); ``None`` if unknown."""
        f = self._f
        if f.GetThreadDpiAwarenessContext is None or f.GetAwarenessFromDpiAwarenessContext is None:
            return None
        context = f.GetThreadDpiAwarenessContext()
        if not context:
            return None
        level = int(f.GetAwarenessFromDpiAwarenessContext(context))
        return level if level in _AWARENESS_NAMES else None

    # --- windows --------------------------------------------------------------------------------------

    def top_level_windows(self) -> list[int]:
        """Handles of all top-level windows, in the order EnumWindows reports them."""
        handles: list[int] = []

        def collect(hwnd: int | None, _lparam: int) -> bool:
            if hwnd:
                handles.append(int(hwnd))
            return True

        callback = self._f.WNDENUMPROC(collect)  # keep a reference for the duration of the call
        if not self._f.EnumWindows(callback, 0):
            # ``collect`` never stops the enumeration, so FALSE is a real failure. A partial list could
            # hide a duplicate terminal window, so fail instead of returning it.
            raise BackendError(f"EnumWindows failed: {self._error()}")
        return handles

    def is_window(self, hwnd: int) -> bool:
        return bool(self._f.IsWindow(hwnd))

    def is_visible(self, hwnd: int) -> bool:
        return bool(self._f.IsWindowVisible(hwnd))

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self._f.IsIconic(hwnd))

    def window_text(self, hwnd: int) -> str:
        """Title bar text ("" if none). GetWindowTextW never blocks on a hung window of another process."""
        length = int(self._f.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return ""
        size = length + 2
        for _ in range(4):
            buf = ctypes.create_unicode_buffer(size)
            copied = int(self._f.GetWindowTextW(hwnd, buf, size))
            if copied <= 0:
                return ""
            if copied < size - 1:  # the whole title fitted
                return buf.value[:copied]
            size *= 2  # the title grew between the two calls; read it again
        return buf.value

    def client_rect_on_screen(self, hwnd: int) -> tuple[int, int, int, int] | None:
        """``(left, top, width, height)`` of the client area in screen pixels, ``None`` if it can't be read."""
        from ctypes import wintypes  # noqa: PLC0415 - kept off the import path of non-Windows hosts

        rect = wintypes.RECT()
        if not self._f.GetClientRect(hwnd, ctypes.byref(rect)):
            log.debug("GetClientRect(%#x) failed: %s", hwnd, self._error())
            return None
        origin = wintypes.POINT(0, 0)
        if not self._f.ClientToScreen(hwnd, ctypes.byref(origin)):
            log.debug("ClientToScreen(%#x) failed: %s", hwnd, self._error())
            return None
        return int(origin.x), int(origin.y), int(rect.right - rect.left), int(rect.bottom - rect.top)

    def window_pid(self, hwnd: int) -> int:
        from ctypes import wintypes  # noqa: PLC0415 - kept off the import path of non-Windows hosts

        pid = wintypes.DWORD(0)
        self._f.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def process_image_name(self, pid: int) -> str | None:
        """Executable file name of a process (e.g. ``"Terminal.exe"``), ``None`` if it can't be read."""
        from ctypes import wintypes  # noqa: PLC0415 - kept off the import path of non-Windows hosts

        if pid <= 0:
            return None
        process = self._f.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            log.debug("OpenProcess(%d) failed: %s", pid, self._error())
            return None
        try:
            size = 1024
            while size <= _MAX_IMAGE_PATH_CHARS:
                buf = ctypes.create_unicode_buffer(size)
                length = wintypes.DWORD(size)
                if self._f.QueryFullProcessImageNameW(process, 0, buf, ctypes.byref(length)):
                    name = ntpath.basename(buf.value[: int(length.value)])
                    return name or None
                code = self._f.get_last_error()
                if code != ERROR_INSUFFICIENT_BUFFER:
                    log.debug("QueryFullProcessImageNameW(%d) failed: %s", pid, self._error(code))
                    return None
                size *= 2
            return None
        finally:
            if not self._f.CloseHandle(process):
                log.debug("CloseHandle(process %d) failed: %s", pid, self._error())

    def dpi_for_window(self, hwnd: int) -> int:
        """Effective DPI of a window; 96 when GetDpiForWindow is unavailable or reports nothing."""
        fn = self._f.GetDpiForWindow
        if fn is None:
            return USER_DEFAULT_SCREEN_DPI
        dpi = int(fn(hwnd))
        return dpi if dpi > 0 else USER_DEFAULT_SCREEN_DPI

    def foreground_window(self) -> int | None:
        hwnd = self._f.GetForegroundWindow()
        return int(hwnd) if hwnd else None

    def set_foreground_window(self, hwnd: int) -> bool:
        return bool(self._f.SetForegroundWindow(hwnd))

    def allow_set_foreground_window(self, pid: int) -> bool:
        return bool(self._f.AllowSetForegroundWindow(pid))

    def alt_key(self, *, up: bool) -> None:
        """Synthesize an Alt key down or up event (with its scan code)."""
        scan = int(self._f.MapVirtualKeyW(VK_MENU, MAPVK_VK_TO_VSC)) & 0xFF
        self._f.keybd_event(VK_MENU, scan, KEYEVENTF_KEYUP if up else 0, 0)

    def show_restore(self, hwnd: int) -> None:
        # The return value says whether the window was visible before, not whether the call worked.
        self._f.ShowWindow(hwnd, SW_RESTORE)

    def root_window_at(self, x: int, y: int) -> int | None:
        from ctypes import wintypes  # noqa: PLC0415 - kept off the import path of non-Windows hosts

        hwnd = self._f.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        if not hwnd:
            return None
        root = self._f.GetAncestor(hwnd, GA_ROOT)
        return int(root) if root else None

    def input_desktop_available(self) -> bool:
        """True when this session owns the input desktop (False when locked or disconnected)."""
        desktop = self._f.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
        if not desktop:
            log.debug("OpenInputDesktop failed (session locked or disconnected?): %s", self._error())
            return False
        if not self._f.CloseDesktop(desktop):
            log.debug("CloseDesktop failed: %s", self._error())
        return True

    # --- clipboard ------------------------------------------------------------------------------------

    @contextmanager
    def _open_clipboard(self, owner: int | None) -> Iterator[None]:
        code = 0
        for attempt in range(1, self._clipboard_attempts + 1):
            if self._f.OpenClipboard(owner):
                break
            code = self._f.get_last_error()
            if attempt < self._clipboard_attempts:
                self._sleep(self._clipboard_retry)
        else:
            raise BackendError(
                f"could not open the clipboard after {self._clipboard_attempts} attempts "
                f"(another program is holding it): {self._error(code)}"
            )
        try:
            yield
        finally:
            if not self._f.CloseClipboard():
                log.warning("CloseClipboard failed: %s", self._error())

    @contextmanager
    def _clipboard_owner(self) -> Iterator[int | None]:
        """A temporary message-only window to own the clipboard while it is written."""
        hwnd = self._f.CreateWindowExW(
            0, "STATIC", None, 0, 0, 0, 0, 0, ctypes.c_void_p(HWND_MESSAGE), None, None, None
        )
        if not hwnd:
            log.debug("could not create a clipboard owner window (%s); writing without an owner", self._error())
            yield None
            return
        try:
            yield int(hwnd)
        finally:
            if not self._f.DestroyWindow(hwnd):
                log.debug("DestroyWindow(clipboard owner) failed: %s", self._error())

    def get_clipboard_text(self) -> str | None:
        """Unicode text on the clipboard, ``None`` if the clipboard holds no text."""
        with self._clipboard_lock, self._open_clipboard(None):
            if not self._f.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return None
            handle = self._f.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                raise BackendError(f"GetClipboardData(CF_UNICODETEXT) failed: {self._error()}")
            pointer = self._f.GlobalLock(handle)
            if not pointer:
                raise BackendError(f"GlobalLock on the clipboard data failed: {self._error()}")
            try:
                size = min(int(self._f.GlobalSize(handle)), _MAX_CLIPBOARD_BYTES)
                raw = ctypes.string_at(pointer, size) if size > 0 else b""
            finally:
                self._f.GlobalUnlock(handle)  # returns 0 once unlocked; the clipboard keeps owning it
        return _decode_clipboard_text(raw)

    def set_clipboard_text(self, text: str) -> None:
        """Replace the clipboard with ``text`` and verify that it took. Raises :class:`BackendError`."""
        if "\x00" in text:
            raise ValueError("clipboard text must not contain NUL characters")
        data = text.encode("utf-16-le") + b"\x00\x00"
        with self._clipboard_lock:
            memory = self._f.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not memory:
                raise BackendError(f"GlobalAlloc({len(data)}) failed: {self._error()}")
            owned_by_clipboard = False
            try:
                pointer = self._f.GlobalLock(memory)
                if not pointer:
                    raise BackendError(f"GlobalLock failed: {self._error()}")
                try:
                    ctypes.memmove(pointer, data, len(data))
                finally:
                    self._f.GlobalUnlock(memory)
                with self._clipboard_owner() as owner, self._open_clipboard(owner):
                    if not self._f.EmptyClipboard():
                        raise BackendError(f"EmptyClipboard failed: {self._error()}")
                    if not self._f.SetClipboardData(CF_UNICODETEXT, memory):
                        raise BackendError(f"SetClipboardData failed: {self._error()}")
                    owned_by_clipboard = True  # the system owns the memory now: never free it
            finally:
                if not owned_by_clipboard and self._f.GlobalFree(memory):
                    log.warning("GlobalFree failed after a failed clipboard write: %s", self._error())
            readback = self.get_clipboard_text()
        if readback != text:
            raise BackendError("the clipboard did not keep the text that was just written")


# ------------------------------------------------------------------------------------- backend


class _WindowsApi(Protocol):
    """What :class:`Win32Backend` needs from the OS layer (implemented by :class:`_Win32Api`)."""

    def top_level_windows(self) -> list[int]: ...
    def is_window(self, hwnd: int) -> bool: ...
    def is_visible(self, hwnd: int) -> bool: ...
    def is_iconic(self, hwnd: int) -> bool: ...
    def window_text(self, hwnd: int) -> str: ...
    def client_rect_on_screen(self, hwnd: int) -> tuple[int, int, int, int] | None: ...
    def window_pid(self, hwnd: int) -> int: ...
    def process_image_name(self, pid: int) -> str | None: ...
    def dpi_for_window(self, hwnd: int) -> int: ...
    def foreground_window(self) -> int | None: ...
    def set_foreground_window(self, hwnd: int) -> bool: ...
    def allow_set_foreground_window(self, pid: int) -> bool: ...
    def alt_key(self, *, up: bool) -> None: ...
    def show_restore(self, hwnd: int) -> None: ...
    def root_window_at(self, x: int, y: int) -> int | None: ...
    def input_desktop_available(self) -> bool: ...
    def get_clipboard_text(self) -> str | None: ...
    def set_clipboard_text(self, text: str) -> None: ...


class _Automation(Protocol):
    """The subset of the ``pyautogui`` module the backend uses."""

    FAILSAFE: bool
    PAUSE: float

    @property
    def FailSafeException(self) -> type[Exception]: ...
    @property
    def PyAutoGUIException(self) -> type[Exception]: ...

    def click(self, *, x: int, y: int) -> object: ...
    def moveTo(self, *, x: int, y: int) -> object: ...
    def hotkey(self, *keys: str) -> object: ...
    def press(self, key: str) -> object: ...
    def write(self, message: str, *, interval: float) -> object: ...
    def position(self) -> tuple[int, int]: ...
    def isValidKey(self, key: str) -> bool: ...
    def failSafeCheck(self) -> object: ...


_Grab = Callable[..., Image.Image]


def _import_pyautogui() -> _Automation:
    try:
        module = importlib.import_module("pyautogui")
    except ImportError as exc:
        raise Win32UnavailableError(
            "pyautogui is not installed; on the Windows machine run: pip install -e .[windows]"
        ) from exc
    return cast(_Automation, module)


def _import_grab() -> _Grab:
    from PIL import ImageGrab  # noqa: PLC0415 - only needed (and only meaningful) on Windows

    return ImageGrab.grab


def _describe_awareness(outcome: str, level: int | None) -> tuple[str, bool]:
    """Combine what the DPI calls reported with the awareness the OS reports: ``(name, per_monitor)``."""
    if level is None:
        return outcome, outcome in _PER_MONITOR_OUTCOMES
    if level == 2:
        return (outcome if outcome in _PER_MONITOR_OUTCOMES else "per-monitor"), True
    return _AWARENESS_NAMES[level], False


class Win32Backend:
    """:class:`~.backend.GuiBackend` for a real, logged-in and unlocked Windows desktop session.

    All coordinates are physical screen pixels (the process is made per-monitor DPI aware first).
    Construct it once per process, from the thread that will use it or any other: the GUI calls are
    serialized by an internal lock, but the venue is expected to drive it from a single worker thread.
    """

    def __init__(
        self,
        *,
        pause_seconds: float = 0.08,
        include_layered_windows: bool = True,
        clipboard_attempts: int = 10,
        clipboard_retry_seconds: float = 0.05,
    ) -> None:
        """Bind the Win32 API, set DPI awareness, then import and configure pyautogui.

        Raises :class:`Win32UnavailableError` off Windows or when pyautogui is missing.
        """
        _check_pause(pause_seconds)
        fns = _bind_win32()  # raises Win32UnavailableError when not on Windows
        api = _Win32Api(fns, clipboard_attempts=clipboard_attempts, clipboard_retry_seconds=clipboard_retry_seconds)
        preloaded = "pyautogui" in sys.modules
        awareness, per_monitor = _describe_awareness(api.make_dpi_aware(), api.dpi_awareness_level())
        if per_monitor:
            log.info("process DPI awareness: %s", awareness)
        else:
            log.warning(
                "process DPI awareness is %s, not per-monitor%s: on scaled displays Windows may virtualize "
                "coordinates, and the desk will then refuse to act (DPI/template checks fail). Create "
                "Win32Backend before anything imports pyautogui.",
                awareness,
                " (pyautogui was imported before the backend was created)" if preloaded else "",
            )
        self._setup(
            api,
            _import_pyautogui(),
            _import_grab(),
            pause_seconds=pause_seconds,
            include_layered_windows=include_layered_windows,
            dpi_awareness=awareness,
        )

    @classmethod
    def from_config(cls, cfg: UniTraderConfig) -> Win32Backend:
        """Backend configured from ``execution.unitrader`` (pyautogui pause = ``action_delay_seconds``)."""
        return cls(pause_seconds=cfg.action_delay_seconds)

    @classmethod
    def _from_parts(
        cls,
        api: _WindowsApi,
        automation: _Automation,
        grab: _Grab,
        *,
        pause_seconds: float = 0.08,
        include_layered_windows: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> Win32Backend:
        """Assemble a backend from already-built parts (no platform check). Test seam; not for production."""
        _check_pause(pause_seconds)
        self = cls.__new__(cls)
        self._setup(
            api,
            automation,
            grab,
            pause_seconds=pause_seconds,
            include_layered_windows=include_layered_windows,
            dpi_awareness="injected",
            sleep=sleep,
            monotonic=monotonic,
        )
        return self

    def _setup(
        self,
        api: _WindowsApi,
        automation: _Automation,
        grab: _Grab,
        *,
        pause_seconds: float,
        include_layered_windows: bool,
        dpi_awareness: str,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api = api
        self._gui = automation
        self._grab = grab
        self._layered = include_layered_windows
        self._sleep = sleep
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self.dpi_awareness = dpi_awareness
        automation.FAILSAFE = True
        automation.PAUSE = float(pause_seconds)

    # --- helpers --------------------------------------------------------------------------------------

    def _run(self, action: str, fn: Callable[[], _T]) -> _T:
        try:
            return fn()
        except self._gui.FailSafeException as exc:
            raise OperatorAbortError(
                f"pyautogui fail-safe triggered during {action} (mouse moved into a screen corner): operator abort"
            ) from exc
        except self._gui.PyAutoGUIException as exc:
            raise BackendError(f"{action} failed: {exc}") from exc

    def _check_key(self, key: str) -> str:
        if not key:
            raise BackendError(f"invalid key name {key!r}")
        name = key if len(key) == 1 else key.lower()
        if not self._gui.isValidKey(name):
            raise BackendError(f"unknown key {key!r} (pyautogui would silently ignore it)")
        return name

    def _wait_foreground(self, handle: int) -> bool:
        deadline = self._monotonic() + _FOREGROUND_SETTLE_SECONDS
        while True:
            if self._api.foreground_window() == handle:
                return True
            if self._monotonic() >= deadline:
                return False
            self._sleep(_POLL_SECONDS)

    def _describe(self, hwnd: int, title: str) -> WindowInfo | None:
        rect = self._api.client_rect_on_screen(hwnd)
        if rect is None:
            return None  # the window went away while it was being read
        left, top, width, height = rect
        pid = self._api.window_pid(hwnd)
        return WindowInfo(
            handle=hwnd,
            title=title,
            pid=pid,
            process_name=self._api.process_image_name(pid),
            client_left=left,
            client_top=top,
            client_width=max(0, width),
            client_height=max(0, height),
            minimized=self._api.is_iconic(hwnd),
            visible=self._api.is_visible(hwnd),
            dpi=self._api.dpi_for_window(hwnd),
        )

    # --- windows --------------------------------------------------------------------------------------

    def list_windows(self) -> list[WindowInfo]:
        """Visible top-level windows with a non-empty title (minimized windows included)."""
        out: list[WindowInfo] = []
        for hwnd in self._api.top_level_windows():
            if not self._api.is_visible(hwnd):
                continue
            title = self._api.window_text(hwnd)
            if not title.strip():
                continue
            info = self._describe(hwnd, title)
            if info is not None:
                out.append(info)
        return out

    def get_window(self, handle: int) -> WindowInfo | None:
        """Fresh information about one window, ``None`` if it no longer exists."""
        if not self._api.is_window(handle):
            return None
        return self._describe(handle, self._api.window_text(handle))

    def restore_window(self, handle: int) -> None:
        """Un-minimize the window. A window that is not minimized is left exactly as it is."""
        with self._lock:
            if not self._api.is_window(handle):
                raise BackendError(f"window {handle:#x} no longer exists")
            if not self._api.is_iconic(handle):
                return
            self._api.show_restore(handle)
            deadline = self._monotonic() + _RESTORE_SETTLE_SECONDS
            while self._api.is_iconic(handle):
                if self._monotonic() >= deadline:
                    log.warning("window %#x is still minimized after SW_RESTORE", handle)
                    return
                self._sleep(_POLL_SECONDS)

    def activate_window(self, handle: int) -> bool:
        """Bring the window to the foreground; True only if it is the foreground window afterwards."""
        with self._lock:
            self._run("activate_window", self._gui.failSafeCheck)  # honour the fail-safe before any key event
            if not self._api.is_window(handle):
                return False
            if self._api.foreground_window() == handle:
                return True
            if self._api.set_foreground_window(handle) and self._wait_foreground(handle):
                return True
            # Foreground lock: only the process that received the last input event may take the
            # foreground. A synthetic Alt press makes this process that one.
            pid = self._api.window_pid(handle)
            self._api.alt_key(up=False)
            try:
                if pid:
                    self._api.allow_set_foreground_window(pid)
                self._api.set_foreground_window(handle)
            finally:
                self._api.alt_key(up=True)  # never leave Alt held down
            ok = self._wait_foreground(handle)
            if not ok:
                log.warning("could not bring window %#x to the foreground", handle)
            return ok

    def foreground_window(self) -> int | None:
        """Handle of the current foreground window, ``None`` if there is none."""
        return self._api.foreground_window()

    def root_window_at(self, x: int, y: int) -> int | None:
        """Top-level window (GA_ROOT) under a screen point; ``None`` if there is none."""
        return self._api.root_window_at(int(x), int(y))

    def input_desktop_available(self) -> bool:
        """False when the session is locked or disconnected (OpenInputDesktop fails)."""
        return self._api.input_desktop_available()

    # --- screen ---------------------------------------------------------------------------------------

    def capture(self, left: int, top: int, width: int, height: int) -> Image.Image:
        """RGB screenshot of a screen rectangle (virtual-screen coordinates, all monitors)."""
        if width <= 0 or height <= 0:
            raise ValueError(f"capture size must be positive, got {width}x{height}")
        bbox = (int(left), int(top), int(left) + int(width), int(top) + int(height))
        try:
            img = self._grab(bbox=bbox, include_layered_windows=self._layered, all_screens=True)
        except OSError as exc:
            raise BackendError(f"screen capture failed (session locked or disconnected?): {exc}") from exc
        rgb = img if img.mode == "RGB" else img.convert("RGB")
        if rgb.size != (width, height):
            raise BackendError(f"screen capture returned {rgb.size[0]}x{rgb.size[1]}, expected {width}x{height}")
        return rgb

    # --- input ----------------------------------------------------------------------------------------

    def move_mouse(self, x: int, y: int) -> None:
        """Move the mouse to the screen point (no click)."""
        with self._lock:
            self._run("move_mouse", lambda: self._gui.moveTo(x=int(x), y=int(y)))

    def click(self, x: int, y: int) -> None:
        """Single primary-button click at the screen point."""
        with self._lock:
            self._run("click", lambda: self._gui.click(x=int(x), y=int(y)))

    def hotkey(self, *keys: str) -> None:
        """Press keys together (pyautogui key names), e.g. ``hotkey("ctrl", "a")``."""
        if not keys:
            raise BackendError("hotkey needs at least one key")
        names = [self._check_key(k) for k in keys]
        with self._lock:
            self._run("hotkey", lambda: self._gui.hotkey(*names))

    def press(self, key: str) -> None:
        """Press and release one key (pyautogui key name)."""
        name = self._check_key(key)
        with self._lock:
            self._run("press", lambda: self._gui.press(name))

    def type_text(self, text: str) -> None:
        """Type literal text; refuses characters the keyboard layout can not type (instead of skipping them)."""
        bad = sorted({c for c in text if not self._gui.isValidKey(c)})
        if bad:
            raise BackendError(f"cannot type {''.join(bad)!r} with the current keyboard layout")
        with self._lock:
            self._run("type_text", lambda: self._gui.write(text, interval=0.0))

    def cursor_position(self) -> tuple[int, int]:
        """Current mouse position in screen pixels."""
        pos = self._run("cursor_position", self._gui.position)
        return int(pos[0]), int(pos[1])

    # --- clipboard ------------------------------------------------------------------------------------

    def get_clipboard_text(self) -> str | None:
        """Unicode text on the clipboard, ``None`` if it holds no text. Raises :class:`BackendError`."""
        with self._lock:
            return self._api.get_clipboard_text()

    def set_clipboard_text(self, text: str) -> None:
        """Replace the clipboard text and verify it took. Raises :class:`BackendError` on any failure."""
        with self._lock:
            self._api.set_clipboard_text(text)

    # --- misc -----------------------------------------------------------------------------------------

    def sleep(self, seconds: float) -> None:
        """Block the calling thread (negative values count as zero)."""
        self._sleep(max(0.0, seconds))


def _check_pause(pause_seconds: float) -> None:
    if not 0.0 <= pause_seconds <= 5.0:
        raise ValueError("pause_seconds must be between 0 and 5")
