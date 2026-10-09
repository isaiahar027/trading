"""Low-level desktop automation contract used by the UniTrader venue and the calibration wizard.

Coordinates passed to/returned from a backend are always *screen* pixels (physical pixels; the
Windows backend makes the process per-monitor DPI aware). Converting between the terminal's
client-area coordinates (what the layout stores) and screen coordinates is the venue's job.

Key names follow pyautogui conventions: ``"ctrl"``, ``"a"``, ``"c"``, ``"tab"``, ``"enter"``, ``"esc"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from PIL import Image


class BackendError(Exception):
    """The desktop could not be automated (no input desktop, API failure, ...)."""


class OperatorAbortError(BackendError):
    """The operator triggered the fail-safe (e.g. slammed the mouse into a screen corner)."""


@dataclass(frozen=True, slots=True)
class WindowInfo:
    handle: int
    title: str
    pid: int
    process_name: str | None  # executable file name, e.g. "UniTrader.exe"; None if it can't be read
    client_left: int  # screen x of the client area's top-left corner
    client_top: int  # screen y of the client area's top-left corner
    client_width: int
    client_height: int
    minimized: bool
    visible: bool
    dpi: int  # effective DPI of the window (96 = 100% scaling)


class GuiBackend(Protocol):
    # --- windows -------------------------------------------------------------------------------
    def list_windows(self) -> list[WindowInfo]:
        """Visible top-level windows with a non-empty title."""
        ...

    def get_window(self, handle: int) -> WindowInfo | None: ...

    def restore_window(self, handle: int) -> None:
        """Un-minimize the window (no resize, no move)."""
        ...

    def activate_window(self, handle: int) -> bool:
        """Try to bring the window to the foreground. Returns whether it is now the foreground window."""
        ...

    def foreground_window(self) -> int | None: ...

    def root_window_at(self, x: int, y: int) -> int | None:
        """Handle of the top-level window that owns the screen point (detects popups covering a button)."""
        ...

    def input_desktop_available(self) -> bool:
        """False when the session is locked/disconnected (input and screenshots would silently fail)."""
        ...

    # --- screen --------------------------------------------------------------------------------
    def capture(self, left: int, top: int, width: int, height: int) -> Image.Image:
        """Screenshot of a screen rectangle as an RGB image of exactly ``width`` x ``height``."""
        ...

    # --- input ---------------------------------------------------------------------------------
    def move_mouse(self, x: int, y: int) -> None: ...

    def click(self, x: int, y: int) -> None:
        """Single left click at the screen point."""
        ...

    def hotkey(self, *keys: str) -> None:
        """Press keys together, e.g. ``hotkey("ctrl", "a")``."""
        ...

    def press(self, key: str) -> None: ...

    def type_text(self, text: str) -> None:
        """Type literal text into the focused control."""
        ...

    def cursor_position(self) -> tuple[int, int]: ...

    # --- clipboard -----------------------------------------------------------------------------
    def get_clipboard_text(self) -> str | None: ...

    def set_clipboard_text(self, text: str) -> None: ...

    # --- misc ----------------------------------------------------------------------------------
    def sleep(self, seconds: float) -> None: ...
