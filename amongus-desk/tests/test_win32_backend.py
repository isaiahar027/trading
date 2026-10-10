"""Tests for the Win32 GUI backend.

Three layers:

* everywhere: the module imports on any OS, and constructing the backend off Windows fails clearly;
* everywhere: :class:`Win32Backend` logic driven through fakes of the OS layer and of pyautogui, and the
  ctypes-level wrappers (clipboard memory handling, DPI fallbacks, window helpers) driven through fake
  raw Win32 functions backed by real ctypes buffers;
* Windows only (``@pytest.mark.windows``, skipped elsewhere): a smoke test against the real desktop.
"""

from __future__ import annotations

import ctypes
import dataclasses
import subprocess
import sys
from collections.abc import Callable
from typing import Any, ClassVar

import pytest
from PIL import Image

from amongus_desk.config import UniTraderConfig
from amongus_desk.execution.unitrader import win32_backend as wb
from amongus_desk.execution.unitrader.backend import BackendError, OperatorAbortError, WindowInfo
from amongus_desk.execution.unitrader.win32_backend import Win32Backend, Win32UnavailableError

not_windows = pytest.mark.skipif(sys.platform == "win32", reason="checks the behaviour off Windows")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs a real Windows desktop session")


# ------------------------------------------------------------------------------------------- off Windows


def test_module_import_touches_nothing_windows_specific() -> None:
    code = (
        "import sys\n"
        "import amongus_desk.execution.unitrader.win32_backend as m\n"
        "assert hasattr(m, 'Win32Backend') and hasattr(m, 'Win32UnavailableError')\n"
        "print('pyautogui' in sys.modules, 'PIL.ImageGrab' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True)
    assert out.stdout.split() == ["False", "False"]


def test_unavailable_error_is_a_runtime_and_backend_error() -> None:
    assert issubclass(Win32UnavailableError, RuntimeError)
    assert issubclass(Win32UnavailableError, BackendError)


@not_windows
def test_constructing_off_windows_raises_a_clear_runtime_error() -> None:
    with pytest.raises(RuntimeError, match="only works on Windows") as excinfo:
        Win32Backend()
    assert isinstance(excinfo.value, Win32UnavailableError)
    message = str(excinfo.value)
    assert sys.platform in message
    assert "backend: fake" in message and "--simulate" in message


@not_windows
def test_from_config_off_windows_raises() -> None:
    with pytest.raises(Win32UnavailableError):
        Win32Backend.from_config(UniTraderConfig())


@not_windows
def test_binding_off_windows_raises() -> None:
    with pytest.raises(Win32UnavailableError):
        wb._bind_win32()


def test_invalid_pause_is_rejected_before_anything_else() -> None:
    with pytest.raises(ValueError, match="pause_seconds"):
        Win32Backend(pause_seconds=-0.1)
    with pytest.raises(ValueError, match="pause_seconds"):
        Win32Backend(pause_seconds=10)


# ------------------------------------------------------------------------------------------- fakes


class FakeGui:
    """Stands in for the pyautogui module."""

    class PyAutoGUIException(Exception):
        pass

    class FailSafeException(PyAutoGUIException):
        pass

    def __init__(self) -> None:
        self.FAILSAFE = False
        self.PAUSE = 0.5
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.raise_on: dict[str, Exception] = {}
        self.pos: tuple[int, int] = (12, 34)
        self.valid = set("abcdefghijklmnopqrstuvwxyz0123456789.,- ") | {"ctrl", "enter", "esc", "tab", "shift"}

    def _do(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        exc = self.raise_on.get(name)
        if exc is not None:
            raise exc

    def click(self, *args: Any, **kwargs: Any) -> None:
        self._do("click", *args, **kwargs)

    def moveTo(self, *args: Any, **kwargs: Any) -> None:
        self._do("moveTo", *args, **kwargs)

    def hotkey(self, *keys: str) -> None:
        self._do("hotkey", *keys)

    def press(self, key: str) -> None:
        self._do("press", key)

    def write(self, message: str, interval: float = 0.0) -> None:
        self._do("write", message, interval=interval)

    def position(self) -> tuple[int, int]:
        self._do("position")
        return self.pos

    def isValidKey(self, key: str) -> bool:
        return key in self.valid

    def failSafeCheck(self) -> None:
        self._do("failSafeCheck")

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


@dataclasses.dataclass
class FakeWin:
    title: str
    rect: tuple[int, int, int, int] | None = (100, 50, 800, 600)
    pid: int = 4242
    process: str | None = "Terminal.exe"
    visible: bool = True
    iconic: bool = False
    dpi: int = 96


class FakeApi:
    """Stands in for the OS layer (``_Win32Api``)."""

    def __init__(self, windows: dict[int, FakeWin] | None = None) -> None:
        self.windows = windows or {}
        self.foreground: int | None = None
        self.events: list[tuple[Any, ...]] = []
        self.grant_foreground: Callable[[int, bool], bool] = lambda hwnd, alt_down: True
        self.alt_down = False
        self.restore_takes_polls = 0
        self.clipboard: str | None = None

    def top_level_windows(self) -> list[int]:
        return list(self.windows)

    def is_window(self, hwnd: int) -> bool:
        return hwnd in self.windows

    def is_visible(self, hwnd: int) -> bool:
        return self.windows[hwnd].visible

    def is_iconic(self, hwnd: int) -> bool:
        win = self.windows[hwnd]
        if win.iconic and self.events and self.events[-1][0] == "show_restore":
            if self.restore_takes_polls <= 0:
                win.iconic = False
            self.restore_takes_polls -= 1
        return win.iconic

    def window_text(self, hwnd: int) -> str:
        return self.windows[hwnd].title

    def client_rect_on_screen(self, hwnd: int) -> tuple[int, int, int, int] | None:
        return self.windows[hwnd].rect

    def window_pid(self, hwnd: int) -> int:
        return self.windows[hwnd].pid

    def process_image_name(self, pid: int) -> str | None:
        for win in self.windows.values():
            if win.pid == pid:
                return win.process
        return None

    def dpi_for_window(self, hwnd: int) -> int:
        return self.windows[hwnd].dpi

    def foreground_window(self) -> int | None:
        return self.foreground

    def set_foreground_window(self, hwnd: int) -> bool:
        self.events.append(("set_foreground", hwnd, self.alt_down))
        if self.grant_foreground(hwnd, self.alt_down):
            self.foreground = hwnd
            return True
        return False

    def allow_set_foreground_window(self, pid: int) -> bool:
        self.events.append(("allow", pid))
        return True

    def alt_key(self, *, up: bool) -> None:
        self.events.append(("alt_up" if up else "alt_down",))
        self.alt_down = not up

    def show_restore(self, hwnd: int) -> None:
        self.events.append(("show_restore", hwnd))

    def root_window_at(self, x: int, y: int) -> int | None:
        self.events.append(("root_window_at", x, y))
        return 7

    def input_desktop_available(self) -> bool:
        return True

    def get_clipboard_text(self) -> str | None:
        return self.clipboard

    def set_clipboard_text(self, text: str) -> None:
        self.clipboard = text


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeGrab:
    def __init__(self, mode: str = "RGB", size: tuple[int, int] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.mode = mode
        self.size = size
        self.error: Exception | None = None

    def __call__(self, **kwargs: Any) -> Image.Image:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        left, top, right, bottom = kwargs["bbox"]
        return Image.new(self.mode, self.size or (right - left, bottom - top), "red")


def make_backend(
    api: FakeApi | None = None, gui: FakeGui | None = None, grab: FakeGrab | None = None, pause: float = 0.05
) -> tuple[Win32Backend, FakeApi, FakeGui, FakeGrab, FakeTime]:
    api = api or FakeApi({1: FakeWin("Terminal")})
    gui = gui or FakeGui()
    grab = grab or FakeGrab()
    clock = FakeTime()
    backend = Win32Backend._from_parts(
        api, gui, grab, pause_seconds=pause, sleep=clock.sleep, monotonic=clock.monotonic
    )
    return backend, api, gui, grab, clock


# ------------------------------------------------------------------------------------------- backend logic


def test_setup_turns_the_fail_safe_on_and_sets_the_pause() -> None:
    _backend, _api, gui, _grab, _clock = make_backend(pause=0.12)
    assert gui.FAILSAFE is True
    assert pytest.approx(0.12) == gui.PAUSE


def test_list_windows_filters_and_describes() -> None:
    api = FakeApi(
        {
            10: FakeWin("Terminal - Demo", rect=(100, 50, 800, 600), pid=1, process="Terminal.exe", dpi=144),
            11: FakeWin("hidden", visible=False),
            12: FakeWin(""),
            13: FakeWin("   "),
            14: FakeWin("Minimized one", iconic=True, rect=(-32000, -32000, 0, 0), pid=2, process=None),
            15: FakeWin("vanished", rect=None),
        }
    )
    backend, *_ = make_backend(api)
    windows = backend.list_windows()
    assert [w.handle for w in windows] == [10, 14]
    first = windows[0]
    assert first == WindowInfo(
        handle=10,
        title="Terminal - Demo",
        pid=1,
        process_name="Terminal.exe",
        client_left=100,
        client_top=50,
        client_width=800,
        client_height=600,
        minimized=False,
        visible=True,
        dpi=144,
    )
    assert windows[1].minimized and windows[1].process_name is None


def test_get_window() -> None:
    backend, *_ = make_backend(FakeApi({5: FakeWin("Terminal")}))
    info = backend.get_window(5)
    assert info is not None and info.handle == 5 and info.title == "Terminal"
    assert backend.get_window(6) is None


def test_restore_only_touches_minimized_windows() -> None:
    api = FakeApi({1: FakeWin("Terminal", iconic=False)})
    backend, *_ = make_backend(api)
    backend.restore_window(1)
    assert api.events == []  # SW_RESTORE on a maximized window would resize it: never sent

    api.windows[1].iconic = True
    api.restore_takes_polls = 2
    backend.restore_window(1)
    assert api.events == [("show_restore", 1)]
    assert api.windows[1].iconic is False


def test_restore_gives_up_after_the_settle_time() -> None:
    api = FakeApi({1: FakeWin("Terminal", iconic=True)})
    api.restore_takes_polls = 10_000
    backend, _api, _gui, _grab, clock = make_backend(api)
    backend.restore_window(1)  # logs a warning, does not raise
    assert api.windows[1].iconic
    assert clock.now >= wb._RESTORE_SETTLE_SECONDS


def test_restore_unknown_window_raises() -> None:
    backend, *_ = make_backend(FakeApi({}))
    with pytest.raises(BackendError, match="no longer exists"):
        backend.restore_window(99)


def test_activate_when_already_foreground() -> None:
    api = FakeApi({1: FakeWin("Terminal")})
    api.foreground = 1
    backend, _api, gui, *_ = make_backend(api)
    assert backend.activate_window(1) is True
    assert api.events == []
    assert gui.names() == ["failSafeCheck"]


def test_activate_plain_set_foreground() -> None:
    api = FakeApi({1: FakeWin("Terminal")})
    backend, *_ = make_backend(api)
    assert backend.activate_window(1) is True
    assert api.events == [("set_foreground", 1, False)]


def test_activate_uses_the_alt_key_workaround_under_the_foreground_lock() -> None:
    api = FakeApi({1: FakeWin("Terminal", pid=4242)})
    api.grant_foreground = lambda hwnd, alt_down: alt_down  # only works right after an input event
    backend, *_ = make_backend(api)
    assert backend.activate_window(1) is True
    assert api.events == [
        ("set_foreground", 1, False),
        ("alt_down",),
        ("allow", 4242),
        ("set_foreground", 1, True),
        ("alt_up",),
    ]
    assert api.alt_down is False


def test_activate_always_releases_alt() -> None:
    api = FakeApi({1: FakeWin("Terminal", pid=4242)})

    def grant(hwnd: int, alt_down: bool) -> bool:
        if alt_down:
            raise OSError("boom")  # something goes wrong while Alt is held down
        return False

    api.grant_foreground = grant
    backend, *_ = make_backend(api)
    with pytest.raises(OSError, match="boom"):
        backend.activate_window(1)
    assert api.events == [
        ("set_foreground", 1, False),
        ("alt_down",),
        ("allow", 4242),
        ("set_foreground", 1, True),
        ("alt_up",),
    ]
    assert api.alt_down is False


def test_activate_reports_failure() -> None:
    api = FakeApi({1: FakeWin("Terminal")})
    api.grant_foreground = lambda hwnd, alt_down: False
    backend, _api, _gui, _grab, clock = make_backend(api)
    assert backend.activate_window(1) is False
    assert api.alt_down is False
    assert clock.now >= wb._FOREGROUND_SETTLE_SECONDS


def test_activate_missing_window() -> None:
    backend, api, *_ = make_backend(FakeApi({}))
    assert backend.activate_window(3) is False
    assert api.events == []


def test_activate_honours_the_fail_safe_before_any_key_event() -> None:
    api = FakeApi({1: FakeWin("Terminal")})
    gui = FakeGui()
    gui.raise_on["failSafeCheck"] = FakeGui.FailSafeException("corner")
    backend, *_ = make_backend(api, gui)
    with pytest.raises(OperatorAbortError):
        backend.activate_window(1)
    assert api.events == []


def test_click_passes_integer_keywords() -> None:
    backend, _api, gui, *_ = make_backend()
    backend.click(10, 20)
    assert gui.calls == [("click", (), {"x": 10, "y": 20})]
    assert all(type(v) is int for v in gui.calls[0][2].values())


def test_move_mouse_passes_integer_keywords() -> None:
    backend, _api, gui, *_ = make_backend()
    backend.move_mouse(5, 6)
    assert gui.calls == [("moveTo", (), {"x": 5, "y": 6})]


@pytest.mark.parametrize("method", ["click", "move_mouse"])
def test_fail_safe_becomes_operator_abort(method: str) -> None:
    gui = FakeGui()
    gui.raise_on["click"] = FakeGui.FailSafeException("corner")
    gui.raise_on["moveTo"] = FakeGui.FailSafeException("corner")
    backend, *_ = make_backend(gui=gui)
    with pytest.raises(OperatorAbortError, match="fail-safe"):
        getattr(backend, method)(1, 2)


def test_other_pyautogui_errors_become_backend_errors() -> None:
    gui = FakeGui()
    gui.raise_on["press"] = FakeGui.PyAutoGUIException("nope")
    backend, *_ = make_backend(gui=gui)
    with pytest.raises(BackendError, match="press failed") as excinfo:
        backend.press("tab")
    assert not isinstance(excinfo.value, OperatorAbortError)


def test_hotkey_press_and_type_go_through_pyautogui() -> None:
    backend, _api, gui, *_ = make_backend()
    backend.hotkey("ctrl", "a")
    backend.press("Enter")
    backend.type_text("0.10")
    assert gui.calls == [
        ("hotkey", ("ctrl", "a"), {}),
        ("press", ("enter",), {}),
        ("write", ("0.10",), {"interval": 0.0}),
    ]


def test_unknown_keys_and_untypeable_text_are_refused() -> None:
    backend, _api, gui, *_ = make_backend()
    with pytest.raises(BackendError, match="unknown key"):
        backend.hotkey("ctrl", "ctlr")
    with pytest.raises(BackendError, match="unknown key"):
        backend.press("escape-ish")
    with pytest.raises(BackendError, match="at least one key"):
        backend.hotkey()
    with pytest.raises(BackendError, match="cannot type"):
        backend.type_text("0.10€")
    assert gui.calls == []  # nothing half-typed


def test_cursor_position_is_integers() -> None:
    gui = FakeGui()
    gui.pos = (3, 4)
    backend, *_ = make_backend(gui=gui)
    assert backend.cursor_position() == (3, 4)


def test_capture_uses_screen_bbox_all_screens_and_layered_windows() -> None:
    backend, _api, _gui, grab, _clock = make_backend()
    img = backend.capture(-1920, 10, 30, 20)
    assert grab.calls == [{"bbox": (-1920, 10, -1890, 30), "include_layered_windows": True, "all_screens": True}]
    assert img.mode == "RGB" and img.size == (30, 20)


def test_capture_converts_to_rgb_and_checks_size() -> None:
    backend, *_ = make_backend(grab=FakeGrab(mode="RGBA"))
    assert backend.capture(0, 0, 5, 5).mode == "RGB"
    wrong, *_ = make_backend(grab=FakeGrab(size=(4, 4)))
    with pytest.raises(BackendError, match="expected 5x5"):
        wrong.capture(0, 0, 5, 5)
    with pytest.raises(ValueError, match="positive"):
        wrong.capture(0, 0, 0, 5)


def test_capture_failure_becomes_backend_error() -> None:
    grab = FakeGrab()
    grab.error = OSError("screen grab failed")
    backend, *_ = make_backend(grab=grab)
    with pytest.raises(BackendError, match="locked or disconnected"):
        backend.capture(0, 0, 5, 5)


def test_passthroughs() -> None:
    backend, api, _gui, _grab, clock = make_backend()
    api.foreground = 1
    assert backend.foreground_window() == 1
    assert backend.root_window_at(5, 6) == 7
    assert api.events[-1] == ("root_window_at", 5, 6)
    assert backend.input_desktop_available() is True
    backend.set_clipboard_text("sentinel")
    assert backend.get_clipboard_text() == "sentinel"
    backend.sleep(-1)
    backend.sleep(0.25)
    assert clock.slept == [0.0, 0.25]


# ------------------------------------------------------------------------------------------- ctypes layer


def record(log: list[Any], item: Any) -> int:
    """Append ``item`` to ``log`` and return 1 (Win32 TRUE), for one-line fake functions."""
    log.append(item)
    return 1


def _unexpected(name: str) -> Callable[..., Any]:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"unexpected Win32 call {name}{args}")

    return fail


def make_functions(**overrides: Any) -> wb._Win32Functions:
    values: dict[str, Any] = {f.name: _unexpected(f.name) for f in dataclasses.fields(wb._Win32Functions)}
    values.update(GetDpiForWindow=None, SetProcessDpiAwarenessContext=None, SetProcessDpiAwareness=None)
    values.update(SetProcessDPIAware=None, GetThreadDpiAwarenessContext=None, GetAwarenessFromDpiAwarenessContext=None)
    values.update(get_last_error=lambda: 0, format_error=lambda code: f"message {code}")
    values.update(overrides)
    return wb._Win32Functions(**values)


class FakeClipboard:
    """Fake user32/kernel32 clipboard + global memory functions backed by real ctypes buffers."""

    def __init__(self) -> None:
        self.memory: dict[int, ctypes.Array[ctypes.c_char]] = {}
        self.locks: dict[int, int] = {}
        self.freed: list[int] = []
        self.data: dict[int, int] = {}
        self.is_open = False
        self.owner: int | None = None
        self.opens: list[int | None] = []
        self.closes = 0
        self.busy = 0
        self.fail: set[str] = set()
        self.next_handle = 0x100
        self.windows_created: list[int] = []
        self.windows_destroyed: list[int] = []
        self.last_error = 0

    def _handle(self) -> int:
        self.next_handle += 8
        return self.next_handle

    # kernel32
    def GlobalAlloc(self, flags: int, size: int) -> int | None:
        assert flags == wb.GMEM_MOVEABLE
        if "GlobalAlloc" in self.fail:
            return None
        h = self._handle()
        self.memory[h] = ctypes.create_string_buffer(size)
        self.locks[h] = 0
        return h

    def GlobalLock(self, h: int) -> int | None:
        if "GlobalLock" in self.fail:
            return None
        self.locks[h] += 1
        return ctypes.addressof(self.memory[h])

    def GlobalUnlock(self, h: int) -> int:
        self.locks[h] -= 1
        return self.locks[h]

    def GlobalFree(self, h: int) -> int | None:
        self.freed.append(h)
        del self.memory[h]
        return None

    def GlobalSize(self, h: int) -> int:
        return len(self.memory[h])

    # user32
    def OpenClipboard(self, owner: int | None) -> int:
        self.opens.append(owner)
        if self.busy > 0:
            self.busy -= 1
            self.last_error = wb.ERROR_ACCESS_DENIED
            return 0
        assert not self.is_open, "clipboard opened twice"
        self.is_open = True
        self.owner = owner
        return 1

    def CloseClipboard(self) -> int:
        assert self.is_open, "CloseClipboard without OpenClipboard"
        self.is_open = False
        self.closes += 1
        return 1

    def EmptyClipboard(self) -> int:
        assert self.is_open
        if "EmptyClipboard" in self.fail:
            return 0
        self.data.clear()
        return 1

    def SetClipboardData(self, fmt: int, h: int) -> int | None:
        assert self.is_open
        if "SetClipboardData" in self.fail:
            return None
        self.data[fmt] = h
        return h

    def IsClipboardFormatAvailable(self, fmt: int) -> int:
        return int(fmt in self.data)

    def GetClipboardData(self, fmt: int) -> int | None:
        assert self.is_open
        if "GetClipboardData" in self.fail:
            return None
        return self.data.get(fmt)

    def CreateWindowExW(self, *args: Any) -> int | None:
        assert args[1] == "STATIC"
        assert isinstance(args[8], ctypes.c_void_p) and args[8].value == ctypes.c_void_p(wb.HWND_MESSAGE).value
        if "CreateWindowExW" in self.fail:
            return None
        h = self._handle()
        self.windows_created.append(h)
        return h

    def DestroyWindow(self, h: int) -> int:
        self.windows_destroyed.append(h)
        return 1

    def put_text(self, raw: bytes) -> None:
        h = self._handle()
        self.memory[h] = ctypes.create_string_buffer(raw, len(raw))
        self.locks[h] = 0
        self.data[wb.CF_UNICODETEXT] = h

    def functions(self) -> wb._Win32Functions:
        names = [
            "GlobalAlloc",
            "GlobalLock",
            "GlobalUnlock",
            "GlobalFree",
            "GlobalSize",
            "OpenClipboard",
            "CloseClipboard",
            "EmptyClipboard",
            "SetClipboardData",
            "IsClipboardFormatAvailable",
            "GetClipboardData",
            "CreateWindowExW",
            "DestroyWindow",
        ]
        return make_functions(**{n: getattr(self, n) for n in names}, get_last_error=lambda: self.last_error)

    def text_bytes(self) -> bytes:
        h = self.data[wb.CF_UNICODETEXT]
        return bytes(self.memory[h].raw)


def clip_api(clip: FakeClipboard, attempts: int = 4) -> tuple[wb._Win32Api, list[float]]:
    slept: list[float] = []
    api = wb._Win32Api(clip.functions(), clipboard_attempts=attempts, clipboard_retry_seconds=0.01, sleep=slept.append)
    return api, slept


@pytest.mark.parametrize("text", ["0.10", "", "Ünïcødé ✓ 𝄞", "line\r\nbreak"])
def test_clipboard_round_trip(text: str) -> None:
    clip = FakeClipboard()
    api, slept = clip_api(clip)
    api.set_clipboard_text(text)
    assert clip.text_bytes() == text.encode("utf-16-le") + b"\x00\x00"
    assert api.get_clipboard_text() == text
    assert not clip.is_open and clip.closes == 3  # write + verification read + read
    assert all(count == 0 for count in clip.locks.values())
    assert clip.freed == []  # the clipboard owns the memory after SetClipboardData
    assert clip.windows_created == clip.windows_destroyed and len(clip.windows_created) == 1
    assert clip.opens[0] == clip.windows_created[0]  # written with a real owner window
    assert clip.opens[1:] == [None, None]
    assert slept == []


def test_clipboard_write_without_owner_window_still_works() -> None:
    clip = FakeClipboard()
    clip.fail.add("CreateWindowExW")
    api, _ = clip_api(clip)
    api.set_clipboard_text("x")
    assert clip.opens[0] is None
    assert api.get_clipboard_text() == "x"


def test_clipboard_open_is_retried_while_another_program_holds_it() -> None:
    clip = FakeClipboard()
    clip.busy = 3
    api, slept = clip_api(clip, attempts=4)
    api.set_clipboard_text("ok")
    assert slept == [0.01, 0.01, 0.01]
    assert api.get_clipboard_text() == "ok"


def test_clipboard_busy_for_good_frees_the_memory() -> None:
    clip = FakeClipboard()
    clip.busy = 100
    api, slept = clip_api(clip, attempts=4)
    with pytest.raises(BackendError, match="after 4 attempts"):
        api.set_clipboard_text("x")
    assert len(slept) == 3
    assert len(clip.freed) == 1 and not clip.memory
    assert clip.windows_created == clip.windows_destroyed


@pytest.mark.parametrize("failing", ["SetClipboardData", "EmptyClipboard"])
def test_failed_clipboard_write_frees_memory_and_closes(failing: str) -> None:
    clip = FakeClipboard()
    clip.fail.add(failing)
    api, _ = clip_api(clip)
    with pytest.raises(BackendError, match=failing):
        api.set_clipboard_text("x")
    assert len(clip.freed) == 1 and not clip.memory
    assert not clip.is_open and clip.closes == 1


def test_failed_alloc_or_lock() -> None:
    clip = FakeClipboard()
    clip.fail.add("GlobalAlloc")
    api, _ = clip_api(clip)
    with pytest.raises(BackendError, match="GlobalAlloc"):
        api.set_clipboard_text("x")
    assert clip.opens == []

    clip = FakeClipboard()
    clip.fail.add("GlobalLock")
    api, _ = clip_api(clip)
    with pytest.raises(BackendError, match="GlobalLock"):
        api.set_clipboard_text("x")
    assert len(clip.freed) == 1 and clip.opens == []


def test_clipboard_write_is_verified() -> None:
    clip = FakeClipboard()
    api, _ = clip_api(clip)
    real_set = clip.SetClipboardData

    def set_then_lose(fmt: int, h: int) -> int | None:
        result = real_set(fmt, h)
        clip.put_text("stale".encode("utf-16-le") + b"\x00\x00")  # someone else replaced it immediately
        return result

    funcs = dataclasses.replace(clip.functions(), SetClipboardData=set_then_lose)
    api = wb._Win32Api(funcs, sleep=lambda s: None)
    with pytest.raises(BackendError, match="did not keep"):
        api.set_clipboard_text("sentinel")


def test_clipboard_text_with_nul_is_refused() -> None:
    api, _ = clip_api(FakeClipboard())
    with pytest.raises(ValueError, match="NUL"):
        api.set_clipboard_text("a\x00b")


def test_clipboard_read_edge_cases() -> None:
    clip = FakeClipboard()
    api, _ = clip_api(clip)
    assert api.get_clipboard_text() is None  # no text on the clipboard
    assert not clip.is_open

    clip.put_text("abc".encode("utf-16-le"))  # no terminator: bounded by GlobalSize
    assert api.get_clipboard_text() == "abc"
    clip.put_text("ab".encode("utf-16-le") + b"\x00\x00junk")
    assert api.get_clipboard_text() == "ab"
    clip.put_text(b"a\x00b")  # odd length
    assert api.get_clipboard_text() == "a"
    assert all(count == 0 for count in clip.locks.values())

    clip.fail.add("GlobalLock")
    with pytest.raises(BackendError, match="GlobalLock"):
        api.get_clipboard_text()
    assert not clip.is_open  # closed in finally
    clip.fail = {"GetClipboardData"}
    with pytest.raises(BackendError, match="GetClipboardData"):
        api.get_clipboard_text()
    assert not clip.is_open


def test_decode_clipboard_text() -> None:
    assert wb._decode_clipboard_text(b"") == ""
    assert wb._decode_clipboard_text("x\x00y".encode("utf-16-le")) == "x"


def test_api_rejects_bad_retry_settings() -> None:
    with pytest.raises(ValueError, match="attempts"):
        wb._Win32Api(make_functions(), clipboard_attempts=0)
    with pytest.raises(ValueError, match="retry"):
        wb._Win32Api(make_functions(), clipboard_retry_seconds=-1)


# --- DPI awareness fallbacks


def test_dpi_per_monitor_v2() -> None:
    seen: list[Any] = []

    def set_context(ctx: Any) -> int:
        seen.append(ctx)
        return 1

    api = wb._Win32Api(make_functions(SetProcessDpiAwarenessContext=set_context))
    assert api.make_dpi_aware() == "per-monitor-v2"
    assert isinstance(seen[0], ctypes.c_void_p)
    assert seen[0].value == ctypes.c_void_p(wb.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2).value


def test_dpi_already_set() -> None:
    api = wb._Win32Api(
        make_functions(
            SetProcessDpiAwarenessContext=lambda ctx: 0,
            get_last_error=lambda: wb.ERROR_ACCESS_DENIED,
            SetProcessDpiAwareness=_unexpected("SetProcessDpiAwareness"),
        )
    )
    assert api.make_dpi_aware() == "already-set"


def test_dpi_falls_back_to_shcore_then_system() -> None:
    calls: list[int] = []

    def shcore(value: int) -> int:
        calls.append(value)
        return wb.S_OK

    api = wb._Win32Api(make_functions(SetProcessDpiAwareness=shcore))
    assert api.make_dpi_aware() == "per-monitor"
    assert calls == [wb.PROCESS_PER_MONITOR_DPI_AWARE]

    api = wb._Win32Api(
        make_functions(
            SetProcessDpiAwarenessContext=lambda ctx: 0,
            get_last_error=lambda: 87,
            SetProcessDpiAwareness=lambda value: wb.E_ACCESSDENIED,
        )
    )
    assert api.make_dpi_aware() == "already-set"

    api = wb._Win32Api(make_functions(SetProcessDpiAwareness=lambda value: -1, SetProcessDPIAware=lambda: 1))
    assert api.make_dpi_aware() == "system"

    api = wb._Win32Api(make_functions(SetProcessDPIAware=lambda: 0))
    assert api.make_dpi_aware() == "unaware"


def test_dpi_awareness_level_reported_by_the_os() -> None:
    assert wb._Win32Api(make_functions()).dpi_awareness_level() is None  # API not available
    contexts: list[Any] = []

    def awareness(ctx: Any) -> int:
        contexts.append(ctx)
        return 2

    api = wb._Win32Api(
        make_functions(GetThreadDpiAwarenessContext=lambda: 0x22, GetAwarenessFromDpiAwarenessContext=awareness)
    )
    assert api.dpi_awareness_level() == 2
    assert contexts == [0x22]
    no_context = wb._Win32Api(
        make_functions(GetThreadDpiAwarenessContext=lambda: None, GetAwarenessFromDpiAwarenessContext=awareness)
    )
    assert no_context.dpi_awareness_level() is None
    invalid = wb._Win32Api(
        make_functions(GetThreadDpiAwarenessContext=lambda: 1, GetAwarenessFromDpiAwarenessContext=lambda c: -1)
    )
    assert invalid.dpi_awareness_level() is None


@pytest.mark.parametrize(
    ("outcome", "level", "expected"),
    [
        ("per-monitor-v2", None, ("per-monitor-v2", True)),
        ("per-monitor", None, ("per-monitor", True)),
        ("already-set", None, ("already-set", False)),
        ("system", None, ("system", False)),
        ("per-monitor-v2", 2, ("per-monitor-v2", True)),
        ("already-set", 2, ("per-monitor", True)),  # an earlier call already made it per-monitor
        ("already-set", 1, ("system", False)),  # e.g. pyautogui was imported first
        ("unaware", 0, ("unaware", False)),
    ],
)
def test_describe_awareness(outcome: str, level: int | None, expected: tuple[str, bool]) -> None:
    assert wb._describe_awareness(outcome, level) == expected


# --- window helpers


def test_top_level_windows() -> None:
    def enum(callback: Callable[[int | None, int], bool], lparam: int) -> int:
        assert lparam == 0
        for hwnd in (0x10, None, 0x30):
            assert callback(hwnd, lparam) is True
        return 1

    api = wb._Win32Api(make_functions(EnumWindows=enum, WNDENUMPROC=lambda fn: fn))
    assert api.top_level_windows() == [0x10, 0x30]

    failing = wb._Win32Api(make_functions(EnumWindows=lambda cb, lp: 0, WNDENUMPROC=lambda fn: fn))
    with pytest.raises(BackendError, match="EnumWindows failed"):
        failing.top_level_windows()


def test_window_text() -> None:
    titles = {1: "Terminal", 2: ""}

    def get_text(hwnd: int, buf: Any, size: int) -> int:
        text = titles[hwnd][: size - 1]
        buf.value = text
        return len(text)

    api = wb._Win32Api(make_functions(GetWindowTextLengthW=lambda hwnd: len(titles[hwnd]), GetWindowTextW=get_text))
    assert api.window_text(1) == "Terminal"
    assert api.window_text(2) == ""


def test_window_text_rereads_a_growing_title() -> None:
    title = "Terminal - a much longer title now"

    def get_text(hwnd: int, buf: Any, size: int) -> int:
        text = title[: size - 1]
        buf.value = text
        return len(text)

    api = wb._Win32Api(make_functions(GetWindowTextLengthW=lambda hwnd: 8, GetWindowTextW=get_text))
    assert api.window_text(1) == title


def test_client_rect_and_pid() -> None:
    def client_rect(hwnd: int, ref: Any) -> int:
        rect = ref._obj
        rect.left, rect.top, rect.right, rect.bottom = 0, 0, 800, 600
        return 1

    def client_to_screen(hwnd: int, ref: Any) -> int:
        point = ref._obj
        point.x += 100
        point.y += 50
        return 1

    def thread_pid(hwnd: int, ref: Any) -> int:
        ref._obj.value = 4242
        return 77

    api = wb._Win32Api(
        make_functions(GetClientRect=client_rect, ClientToScreen=client_to_screen, GetWindowThreadProcessId=thread_pid)
    )
    assert api.client_rect_on_screen(1) == (100, 50, 800, 600)
    assert api.window_pid(1) == 4242
    broken = wb._Win32Api(make_functions(GetClientRect=lambda hwnd, ref: 0))
    assert broken.client_rect_on_screen(1) is None


def test_process_image_name() -> None:
    closed: list[int] = []
    path = r"C:\Program Files\Some Broker\Terminal.exe"
    sizes: list[int] = []

    def query(handle: int, flags: int, buf: Any, ref: Any) -> int:
        size = ref._obj.value
        sizes.append(size)
        if size < 2048:
            return 0  # pretend the path needs a bigger buffer
        buf.value = path
        ref._obj.value = len(path)
        return 1

    funcs = make_functions(
        OpenProcess=lambda access, inherit, pid: 0x55 if pid == 1 else None,
        QueryFullProcessImageNameW=query,
        CloseHandle=lambda h: record(closed, h),
        get_last_error=lambda: wb.ERROR_INSUFFICIENT_BUFFER,
    )
    api = wb._Win32Api(funcs)
    assert api.process_image_name(1) == "Terminal.exe"
    assert sizes == [1024, 2048]
    assert closed == [0x55]
    assert api.process_image_name(2) is None  # OpenProcess failed: nothing to close
    assert api.process_image_name(0) is None
    assert closed == [0x55]

    denied = wb._Win32Api(dataclasses.replace(funcs, QueryFullProcessImageNameW=lambda *a: 0, get_last_error=lambda: 5))
    assert denied.process_image_name(1) is None
    assert closed == [0x55, 0x55]  # closed in finally


def test_process_image_name_uses_limited_access() -> None:
    seen: list[tuple[int, bool, int]] = []

    def open_process(access: int, inherit: bool, pid: int) -> None:
        seen.append((access, inherit, pid))

    api = wb._Win32Api(make_functions(OpenProcess=open_process))
    assert api.process_image_name(9) is None
    assert seen == [(wb.PROCESS_QUERY_LIMITED_INFORMATION, False, 9)]


def test_dpi_for_window_fallback() -> None:
    assert wb._Win32Api(make_functions()).dpi_for_window(1) == 96
    assert wb._Win32Api(make_functions(GetDpiForWindow=lambda hwnd: 0)).dpi_for_window(1) == 96
    assert wb._Win32Api(make_functions(GetDpiForWindow=lambda hwnd: 144)).dpi_for_window(1) == 144


def test_root_window_at_uses_ga_root() -> None:
    seen: list[Any] = []

    def from_point(point: Any) -> int | None:
        seen.append((point.x, point.y))
        return 0x99 if point.x >= 0 else None

    def ancestor(hwnd: int, flag: int) -> int:
        seen.append((hwnd, flag))
        return 0x11

    api = wb._Win32Api(make_functions(WindowFromPoint=from_point, GetAncestor=ancestor))
    assert api.root_window_at(5, 6) == 0x11
    assert seen == [(5, 6), (0x99, wb.GA_ROOT)]
    assert api.root_window_at(-5, 6) is None


def test_input_desktop() -> None:
    closed: list[int] = []
    opened: list[tuple[int, bool, int]] = []

    def open_desktop(flags: int, inherit: bool, access: int) -> int:
        opened.append((flags, inherit, access))
        return 0x66

    api = wb._Win32Api(make_functions(OpenInputDesktop=open_desktop, CloseDesktop=lambda h: record(closed, h)))
    assert api.input_desktop_available() is True
    assert opened == [(0, False, wb.DESKTOP_SWITCHDESKTOP)] and closed == [0x66]
    locked = wb._Win32Api(make_functions(OpenInputDesktop=lambda *a: None))
    assert locked.input_desktop_available() is False


def test_alt_key_and_restore_and_foreground() -> None:
    events: list[tuple[Any, ...]] = []
    funcs = make_functions(
        MapVirtualKeyW=lambda vk, kind: 0x38 if (vk, kind) == (wb.VK_MENU, wb.MAPVK_VK_TO_VSC) else 0,
        keybd_event=lambda vk, scan, flags, extra: events.append(("key", vk, scan, flags, extra)),
        ShowWindow=lambda hwnd, cmd: record(events, ("show", hwnd, cmd)),
        SetForegroundWindow=lambda hwnd: 1,
        AllowSetForegroundWindow=lambda pid: record(events, ("allow", pid)),
        GetForegroundWindow=lambda: None,
        IsWindow=lambda hwnd: 1,
        IsWindowVisible=lambda hwnd: 0,
        IsIconic=lambda hwnd: 1,
    )
    api = wb._Win32Api(funcs)
    api.alt_key(up=False)
    api.alt_key(up=True)
    api.show_restore(3)
    assert api.allow_set_foreground_window(42) is True
    assert events == [
        ("key", wb.VK_MENU, 0x38, 0, 0),
        ("key", wb.VK_MENU, 0x38, wb.KEYEVENTF_KEYUP, 0),
        ("show", 3, wb.SW_RESTORE),
        ("allow", 42),
    ]
    assert api.set_foreground_window(3) is True
    assert api.foreground_window() is None
    assert api.is_window(3) and not api.is_visible(3) and api.is_iconic(3)


# --- the binding table itself (runs everywhere: fake DLLs, real ctypes types)

_NOT_WIN32_FUNCTIONS = ("WNDENUMPROC", "get_last_error", "format_error")
# Documented parameter counts of every bound Win32 function.
_ARITY = {
    "EnumWindows": 2,
    "GetWindowTextLengthW": 1,
    "GetWindowTextW": 3,
    "IsWindow": 1,
    "IsWindowVisible": 1,
    "IsIconic": 1,
    "GetClientRect": 2,
    "ClientToScreen": 2,
    "GetWindowThreadProcessId": 2,
    "GetForegroundWindow": 0,
    "SetForegroundWindow": 1,
    "AllowSetForegroundWindow": 1,
    "ShowWindow": 2,
    "WindowFromPoint": 1,
    "GetAncestor": 2,
    "GetDpiForWindow": 1,
    "OpenInputDesktop": 3,
    "CloseDesktop": 1,
    "keybd_event": 4,
    "MapVirtualKeyW": 2,
    "CreateWindowExW": 12,
    "DestroyWindow": 1,
    "OpenClipboard": 1,
    "CloseClipboard": 0,
    "EmptyClipboard": 0,
    "IsClipboardFormatAvailable": 1,
    "GetClipboardData": 1,
    "SetClipboardData": 2,
    "SetProcessDpiAwarenessContext": 1,
    "SetProcessDpiAwareness": 1,
    "SetProcessDPIAware": 0,
    "GetThreadDpiAwarenessContext": 0,
    "GetAwarenessFromDpiAwarenessContext": 1,
    "OpenProcess": 3,
    "QueryFullProcessImageNameW": 4,
    "CloseHandle": 1,
    "GlobalAlloc": 2,
    "GlobalLock": 1,
    "GlobalUnlock": 1,
    "GlobalFree": 1,
    "GlobalSize": 1,
}
# Functions returning a handle/pointer/size: a 32-bit C int restype would truncate it on 64-bit Windows.
_POINTER_SIZED_RESULT = {
    "GetForegroundWindow",
    "WindowFromPoint",
    "GetAncestor",
    "OpenInputDesktop",
    "CreateWindowExW",
    "GetClipboardData",
    "SetClipboardData",
    "GetThreadDpiAwarenessContext",
    "OpenProcess",
    "GlobalAlloc",
    "GlobalLock",
    "GlobalFree",
    "GlobalSize",
}
# Functions whose first parameter is a handle (HWND, HANDLE, HDESK, HGLOBAL, DPI_AWARENESS_CONTEXT).
_HANDLE_FIRST_ARG = {
    "GetWindowTextLengthW",
    "GetWindowTextW",
    "IsWindow",
    "IsWindowVisible",
    "IsIconic",
    "GetClientRect",
    "ClientToScreen",
    "GetWindowThreadProcessId",
    "SetForegroundWindow",
    "ShowWindow",
    "GetAncestor",
    "GetDpiForWindow",
    "CloseDesktop",
    "DestroyWindow",
    "OpenClipboard",
    "SetProcessDpiAwarenessContext",
    "GetAwarenessFromDpiAwarenessContext",
    "QueryFullProcessImageNameW",
    "CloseHandle",
    "GlobalLock",
    "GlobalUnlock",
    "GlobalFree",
    "GlobalSize",
}


def _pointer_sized(ctype: Any) -> bool:
    return ctypes.sizeof(ctype) == ctypes.sizeof(ctypes.c_void_p)


def test_binding_table_matches_the_documented_signatures() -> None:
    assert set(_ARITY) == {f.name for f in dataclasses.fields(wb._Win32Functions)} - set(_NOT_WIN32_FUNCTIONS)
    assert _POINTER_SIZED_RESULT.issubset(_ARITY) and _HANDLE_FIRST_ARG.issubset(_ARITY)


class _FakeFunction:
    """Records the ``restype``/``argtypes`` that ``_bind_win32`` assigns (nothing is set by default)."""

    def __init__(self, dll: str, name: str) -> None:
        self.dll = dll
        self.name = name


class _FakeDLL:
    missing_dlls: ClassVar[set[str]] = set()
    missing_functions: ClassVar[set[str]] = set()
    loaded: ClassVar[list[_FakeDLL]] = []

    def __init__(self, name: str, use_last_error: bool = False) -> None:
        if name in self.missing_dlls:
            raise OSError(f"{name}.dll not found")
        self.name = name
        self.use_last_error = use_last_error
        self.functions: dict[str, _FakeFunction] = {}
        self.loaded.append(self)

    def __getattr__(self, attr: str) -> _FakeFunction:
        if attr.startswith("_") or attr in self.missing_functions:
            raise AttributeError(attr)
        return self.functions.setdefault(attr, _FakeFunction(self.name, attr))


class _NoSharedWindll:
    def __getattr__(self, attr: str) -> Any:
        raise AssertionError(f"ctypes.windll.{attr} touched: bindings must use private WinDLL instances")


@pytest.fixture
def fake_windows(monkeypatch: pytest.MonkeyPatch) -> type[_FakeDLL]:
    fake = type("FakeDLL", (_FakeDLL,), {"missing_dlls": set(), "missing_functions": set(), "loaded": []})
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", fake, raising=False)
    monkeypatch.setattr(ctypes, "windll", _NoSharedWindll(), raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)
    monkeypatch.setattr(ctypes, "FormatError", lambda code=0: "", raising=False)
    return fake


def test_every_win32_function_gets_explicit_argtypes_and_restype(fake_windows: type[_FakeDLL]) -> None:
    from ctypes import wintypes  # importable on every OS; only used for type comparisons here

    fns = wb._bind_win32()
    assert {d.name for d in fake_windows.loaded} == {"user32", "kernel32", "shcore"}
    assert all(d.use_last_error for d in fake_windows.loaded)  # ctypes.get_last_error() needs it
    for name, arity in _ARITY.items():
        fn = getattr(fns, name)
        assert isinstance(fn, _FakeFunction), name
        assert fn.name == name
        assert "restype" in vars(fn) and "argtypes" in vars(fn), f"{name}: restype/argtypes not set explicitly"
        argtypes = vars(fn)["argtypes"]
        restype = vars(fn)["restype"]
        assert isinstance(argtypes, list) and len(argtypes) == arity, name
        if name in _POINTER_SIZED_RESULT:
            assert restype is not None and _pointer_sized(restype), f"{name} would truncate its result"
        if name in _HANDLE_FIRST_ARG:
            assert _pointer_sized(argtypes[0]), f"{name} would truncate its handle argument"
    # POINT is passed by value, not by pointer
    assert vars(fns.WindowFromPoint)["argtypes"] == [wintypes.POINT]
    assert vars(fns.keybd_event)["restype"] is None
    # handles in the middle of a signature are pointer-sized too
    create = vars(fns.CreateWindowExW)["argtypes"]
    assert all(_pointer_sized(t) for t in create[8:])  # hWndParent, hMenu, hInstance, lpParam
    assert _pointer_sized(vars(fns.SetClipboardData)["argtypes"][1])
    dlls = {name: getattr(fns, name).dll for name in _ARITY}
    assert dlls["SetProcessDpiAwareness"] == "shcore"
    assert {dlls[n] for n in ("OpenProcess", "GlobalAlloc", "CloseHandle", "QueryFullProcessImageNameW")} == {
        "kernel32"
    }
    assert dlls["EnumWindows"] == dlls["OpenClipboard"] == "user32"


def test_optional_functions_are_none_when_windows_lacks_them(fake_windows: type[_FakeDLL]) -> None:
    fake_windows.missing_dlls.add("shcore")
    fake_windows.missing_functions.update({"GetDpiForWindow", "SetProcessDpiAwarenessContext"})
    fns = wb._bind_win32()
    assert fns.SetProcessDpiAwareness is None
    assert fns.GetDpiForWindow is None and fns.SetProcessDpiAwarenessContext is None
    assert fns.SetProcessDPIAware is not None and vars(fns.SetProcessDPIAware)["argtypes"] == []
    api = wb._Win32Api(fns)
    assert api.dpi_for_window(1) == wb.USER_DEFAULT_SCREEN_DPI


def test_a_missing_required_function_fails_loudly(fake_windows: type[_FakeDLL]) -> None:
    fake_windows.missing_functions.add("EnumWindows")
    with pytest.raises(AttributeError, match="EnumWindows"):
        wb._bind_win32()


# ------------------------------------------------------------------------------------------- real Windows


@pytest.mark.windows
@windows_only
def test_real_desktop_smoke() -> None:
    pytest.importorskip("pyautogui")
    backend = Win32Backend(pause_seconds=0.0)
    assert backend.dpi_awareness in {"per-monitor-v2", "per-monitor", "system", "already-set", "unaware"}
    windows = backend.list_windows()
    assert all(isinstance(w, WindowInfo) and w.title.strip() and w.visible for w in windows)
    assert isinstance(backend.input_desktop_available(), bool)
    if not backend.input_desktop_available():
        pytest.skip("the desktop is locked")
    x, y = backend.cursor_position()
    assert isinstance(x, int) and isinstance(y, int)
    img = backend.capture(x, y, 8, 6)
    assert img.mode == "RGB" and img.size == (8, 6)
    root = backend.root_window_at(x, y)
    assert root is None or isinstance(root, int)
    fg = backend.foreground_window()
    if fg is not None:
        info = backend.get_window(fg)
        assert info is None or info.handle == fg


@pytest.mark.windows
@windows_only
def test_real_clipboard_round_trip() -> None:
    pytest.importorskip("pyautogui")
    backend = Win32Backend(pause_seconds=0.0)
    original = backend.get_clipboard_text()
    try:
        backend.set_clipboard_text("amongus-desk sentinel ✓")
        assert backend.get_clipboard_text() == "amongus-desk sentinel ✓"
    finally:
        if original is not None:
            backend.set_clipboard_text(original.replace("\x00", ""))


@pytest.mark.windows
@windows_only
def test_real_bindings_have_argtypes_and_restype() -> None:
    fns = wb._bind_win32()
    for name, arity in _ARITY.items():
        fn = getattr(fns, name)
        if fn is None:  # optional, not exported by this Windows version
            continue
        assert fn.argtypes is not None and len(fn.argtypes) == arity, name
        if name in _POINTER_SIZED_RESULT:  # hasattr(fn, "restype") is always true; check the size instead
            assert fn.restype is not None and _pointer_sized(fn.restype), name
        if name in _HANDLE_FIRST_ARG:
            assert _pointer_sized(fn.argtypes[0]), name
