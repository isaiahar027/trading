"""SIMULATION ONLY — a made-up trading window for tests and ``backend: fake`` demos.

:class:`FakeTerminal` draws an invented "terminal" with PIL: one panel per symbol (symbol label, lot
field, SELL/BUY buttons, optional SL/TP fields), a positions list, an account badge, a one-click
indicator, an optional close-all button and optional dialogs. :class:`FakeGuiBackend` implements
:class:`~amongus_desk.execution.unitrader.backend.GuiBackend` on top of it, so the UniTrader venue and
the calibration wizard can be exercised end to end without a real desktop.

**This module says nothing about the real UniTrader terminal.** Its look, its widgets, its texts, its
dialogs and its behaviour (how a lot field reacts to ``ctrl+a``, when a fill shows up, ...) are
invented for testing and must never be read as a description of UniTrader or UnitedPips. Everything
the desk knows about the real terminal comes from the operator's own calibration.

Coordinates: the terminal works in *client* coordinates (top-left of its client area = 0,0); the
backend translates to and from *screen* coordinates like a real desktop backend would.

Time is virtual: :meth:`FakeGuiBackend.sleep` advances :attr:`FakeTerminal.time` (and an optional
:class:`~amongus_desk.clock.ManualClock`) instead of sleeping, so tests stay fast and deterministic.
"""

from __future__ import annotations

import logging
import re
import threading
import time as _time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageDraw, ImageFont

from ...clock import ManualClock
from ...money import parse_lots
from .backend import BackendError, OperatorAbortError, WindowInfo
from .layout import DialogLayout, Layout, LayoutFile, PanelLayout, Point, Region, TemplateRef

log = logging.getLogger(__name__)

# Window handles of the simulated desktop.
HANDLE_TERMINAL = 0x1001
HANDLE_DUPLICATE_BASE = 0x1100
HANDLE_POPUP = 0x2001
HANDLE_OTHER = 0x3001  # an unrelated window that can take the foreground

FAKE_TITLE = "Fake Terminal (simulation)"
FAKE_PROCESS = "fake-terminal.exe"
FAKE_LAYOUT_FILENAME = "fake-terminal.layout.yaml"
FAKE_TEMPLATE_DIR = "fake-templates"

INPUT_ACTIONS = frozenset({"move_mouse", "click", "hotkey", "press", "type_text"})

FieldKind = Literal["lot", "sl", "tp"]
DialogKind = Literal["order_confirm", "close_all_confirm", "error"]
Font = ImageFont.FreeTypeFont | ImageFont.ImageFont

# Invented palette (it only has to be self-consistent).
_DESKTOP = (24, 36, 52)
_BG = (236, 238, 242)
_HEADER = (52, 58, 70)
_PANEL = (250, 250, 252)
_PANEL_BORDER = (150, 156, 168)
_FIELD = (255, 255, 255)
_FIELD_FOCUS = (40, 120, 220)
_TEXT = (20, 22, 28)
_BUY = (40, 110, 200)
_SELL = (200, 70, 60)

_HEADER_H = 56
_PANEL_W = 290
_PANEL_H = 180
_GAP = 16
_PANEL_TOP = 72


@dataclass(frozen=True, slots=True)
class Rect:
    """A rectangle in client coordinates."""

    x: int
    y: int
    w: int
    h: int

    def contains(self, px: int, py: int) -> bool:
        """True when the client point lies inside the rectangle."""
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h

    def overlaps(self, other: Rect) -> bool:
        """True when the two rectangles share at least one pixel."""
        return (
            self.x < other.x + other.w
            and other.x < self.x + self.w
            and self.y < other.y + other.h
            and other.y < self.y + self.h
        )

    @property
    def center(self) -> tuple[int, int]:
        """Centre point (client coordinates)."""
        return self.x + self.w // 2, self.y + self.h // 2

    @property
    def box(self) -> tuple[int, int, int, int]:
        """PIL box (left, top, right, bottom), right/bottom exclusive."""
        return self.x, self.y, self.x + self.w, self.y + self.h

    def region(self) -> Region:
        """The rectangle as a layout :class:`Region`."""
        return Region(x=self.x, y=self.y, w=self.w, h=self.h)

    def point(self) -> Point:
        """The rectangle's centre as a layout :class:`Point`."""
        cx, cy = self.center
        return Point(x=cx, y=cy)


@dataclass(frozen=True, slots=True)
class FakePanelGeometry:
    """Where one simulated panel and its widgets are drawn (client coordinates)."""

    symbol: str
    frame: Rect
    label: Rect
    lot_field: Rect
    sell_button: Rect
    buy_button: Rect
    sl_field: Rect | None
    tp_field: Rect | None


@dataclass(frozen=True, slots=True)
class FakeOrder:
    """An order the simulated terminal executed (a fill)."""

    symbol: str
    side: str  # "BUY" / "SELL"
    lot_text: str
    lots: Decimal
    sl_text: str | None
    tp_text: str | None
    at: float  # virtual seconds


@dataclass(slots=True)
class FakePosition:
    """An open position in the simulated terminal."""

    symbol: str
    side: str
    lots: Decimal


@dataclass(frozen=True, slots=True)
class FakeAction:
    """One call made through :class:`FakeGuiBackend` (for assertions in tests)."""

    name: str
    args: tuple[Any, ...]
    thread: str


@dataclass(slots=True)
class _Dialog:
    kind: DialogKind
    order: tuple[str, str] | None = None  # (symbol, side) for an order confirmation


@dataclass(slots=True)
class _Pending:
    due: float
    action: Callable[[], None]


@dataclass(slots=True)
class _Geometry:
    badge: Rect
    one_click: Rect
    indicator: Rect
    close_all: Rect
    dialog: Rect
    dialog_header: Rect
    dialog_ok: Rect
    dialog_cancel: Rect
    park: tuple[int, int]
    panels: dict[str, FakePanelGeometry] = field(default_factory=dict)


class FakeTerminal:
    """A simulated desktop holding one invented trading window. NOT the real UniTrader UI.

    Scenario knobs (plain attributes; change them at any time, also from :attr:`on_action`):

    * window: ``window_present``, ``duplicate_windows``, ``extra_windows``, ``minimized``, ``title``,
      ``process_name``, ``client_origin``, ``client_size``, ``dpi``;
    * desktop: ``locked`` (screenshots go black, input is silently dropped), ``popup`` (a foreign window
      covering a client rectangle), ``refuse_focus``, ``steal_focus_after_actions``;
    * looks: ``badge_text`` (alias ``badge``; "DEMO"/"LIVE"/anything), ``one_click_enabled``, ``swap_panels``,
      ``indicator_flicker``;
    * fields: ``lot_field_ignores_select_all``, ``clipboard_copy_broken``, ``lot_field_transform`` (applied while
      typing into the lot field), ``commit_transform`` (applied to any field when it commits: tab/enter, or the
      focus leaving it for another field or a button), ``empty_copy_keeps_clipboard`` (ctrl+c in an empty field
      copies nothing);
    * orders: ``fill_enabled``, ``fill_delay_seconds``, ``order_confirm_dialog``,
      ``close_all_confirm_dialog``, ``order_error``, ``netting``;
    * operator fail-safe: ``failsafe_after_actions`` (input actions allowed before the fail-safe
      fires), ``failsafe_after_order_click`` (fires on the first input after a BUY/SELL click).
    """

    def __init__(
        self,
        symbols: Sequence[str] = ("EURUSD",),
        *,
        title: str = FAKE_TITLE,
        process_name: str | None = FAKE_PROCESS,
        pid: int = 4242,
        client_origin: tuple[int, int] = (120, 90),
        client_size: tuple[int, int] = (960, 600),
        dpi: int = 96,
        badge: str = "DEMO",
        one_click_enabled: bool = True,
        has_close_all: bool = True,
        protective_fields: bool = False,
        order_confirm_dialog: bool = False,
        close_all_confirm_dialog: bool = False,
        netting: bool = True,
        initial_lot_text: str = "0.01",
    ) -> None:
        if not symbols:
            raise ValueError("FakeTerminal needs at least one symbol")
        if len(set(symbols)) != len(symbols):
            raise ValueError("FakeTerminal symbols must be unique")
        self._lock = threading.RLock()
        self.symbols: tuple[str, ...] = tuple(symbols)
        # window / desktop knobs
        self.title = title
        self.process_name = process_name
        self.pid = pid
        self.client_origin = client_origin
        self.client_size = client_size
        self.dpi = dpi
        self.window_present = True
        self.duplicate_windows = 0
        self.extra_windows: list[WindowInfo] = []
        self.minimized = False
        self.locked = False
        self.popup: Rect | None = None
        self.refuse_focus = False
        self.steal_focus_after_actions: int | None = None
        # looks
        self.badge_text = badge
        self.one_click_enabled = one_click_enabled
        self.has_close_all = has_close_all
        self.protective_fields = protective_fields
        self.swap_panels = False
        self.indicator_flicker = False
        # fields
        self.lot_field_ignores_select_all = False
        self.clipboard_copy_broken = False
        self.lot_field_transform: Callable[[str], str] | None = None
        self.commit_transform: Callable[[FieldKind, str], str] | None = None
        self.empty_copy_keeps_clipboard = False
        # orders
        self.fill_enabled = True
        self.fill_delay_seconds = 0.0
        self.order_confirm_dialog = order_confirm_dialog
        self.close_all_confirm_dialog = close_all_confirm_dialog
        self.order_error = False
        self.netting = netting
        # fail-safe
        self.failsafe_after_actions: int | None = None
        self.failsafe_after_order_click = False
        # hook: called after every input action with (terminal, action name)
        self.on_action: Callable[[FakeTerminal, str], None] | None = None

        # simulated state
        self.time = 0.0
        self.foreground: int | None = HANDLE_TERMINAL
        self.clipboard: str | None = None
        self.cursor: tuple[int, int] = (0, 0)
        self.input_count = 0
        self.positions: list[FakePosition] = []
        self.orders: list[FakeOrder] = []
        self.order_clicks: list[tuple[str, str]] = []
        self.close_all_clicks = 0
        self.dialog_confirms = 0
        self._fields: dict[tuple[str, FieldKind], str] = {}
        for sym in self.symbols:
            self._fields[(sym, "lot")] = initial_lot_text
            self._fields[(sym, "sl")] = ""
            self._fields[(sym, "tp")] = ""
        self._focused: tuple[str, FieldKind] | None = None
        self._selected_all = False
        self._dialog: _Dialog | None = None
        self._pending: list[_Pending] = []
        self._focus_stolen = False
        self._failsafe_armed = False
        self._flicker_phase = 0
        self._fonts: dict[int, Font] = {}
        self._render_cache: tuple[tuple[Any, ...], Image.Image] | None = None
        self.geometry()  # validates that everything fits

    @property
    def badge(self) -> str:
        """Alias of :attr:`badge_text` (the text drawn in the simulated account badge)."""
        return self.badge_text

    @badge.setter
    def badge(self, value: str) -> None:
        self.badge_text = value

    # ------------------------------------------------------------------ geometry
    def geometry(self, *, calibration: bool = True) -> _Geometry:
        """Where every widget sits (client coordinates). ``calibration=False`` applies ``swap_panels``."""
        w, h = self.client_size
        cols = max(1, (w - _GAP) // (_PANEL_W + _GAP))
        order = list(self.symbols)
        if not calibration and self.swap_panels:
            order.reverse()
        panels: dict[str, FakePanelGeometry] = {}
        for i, sym in enumerate(order):
            px = _GAP + (i % cols) * (_PANEL_W + _GAP)
            py = _PANEL_TOP + (i // cols) * (_PANEL_H + _GAP)
            panels[sym] = FakePanelGeometry(
                symbol=sym,
                frame=Rect(px, py, _PANEL_W, _PANEL_H),
                label=Rect(px + 12, py + 8, 150, 32),
                lot_field=Rect(px + 70, py + 48, 110, 30),
                sell_button=Rect(px + 12, py + 88, 126, 44),
                buy_button=Rect(px + 152, py + 88, 126, 44),
                sl_field=Rect(px + 40, py + 142, 98, 28) if self.protective_fields else None,
                tp_field=Rect(px + 180, py + 142, 98, 28) if self.protective_fields else None,
            )
        indicator = Rect(_GAP, h - 132, 560, 112)
        lowest_panel = max(p.frame.y + p.frame.h for p in panels.values())
        if lowest_panel > indicator.y - 4 or _GAP + min(cols, len(order)) * (_PANEL_W + _GAP) > w:
            raise ValueError(f"client area {w}x{h} is too small for {len(order)} simulated panels")
        badge = Rect(w - 140, 12, 120, 32)
        one_click = Rect(w - 290, 12, 136, 32)
        close_all = Rect(w - 196, h - 70, 176, 44)
        for name, r in (("badge", badge), ("one-click indicator", one_click), ("positions indicator", indicator)):
            if r.x < 0 or r.y < 0 or r.x + r.w > w or r.y + r.h > h:
                raise ValueError(f"client area {w}x{h} is too small for the simulated {name}")
        if indicator.overlaps(close_all):
            raise ValueError(f"client area {w}x{h} is too small: simulated widgets would overlap")
        dialog = Rect(w // 2 - 180, h // 2 - 90, 360, 170)
        return _Geometry(
            badge=badge,
            one_click=one_click,
            indicator=indicator,
            close_all=close_all,
            dialog=dialog,
            dialog_header=Rect(dialog.x, dialog.y, dialog.w, 40),
            dialog_ok=Rect(dialog.x + 210, dialog.y + 116, 120, 36),
            dialog_cancel=Rect(dialog.x + 30, dialog.y + 116, 120, 36),
            park=(w - 320, 28),
            panels=panels,
        )

    # ------------------------------------------------------------------ test helpers
    def field_text(self, symbol: str, kind: FieldKind = "lot") -> str:
        """Current text of a panel field."""
        with self._lock:
            return self._fields[(symbol, kind)]

    def set_field_text(self, symbol: str, kind: FieldKind, text: str) -> None:
        """Overwrite a panel field (test setup)."""
        with self._lock:
            self._fields[(symbol, kind)] = text
            self._invalidate()

    def open_position(self, symbol: str, side: str, lots: Decimal) -> None:
        """Seed an existing position (e.g. before testing a close)."""
        with self._lock:
            self.positions.append(FakePosition(symbol, side, lots))
            self._invalidate()

    def show_dialog(self, kind: DialogKind, order: tuple[str, str] | None = None) -> None:
        """Open a simulated dialog (test setup), e.g. one left over from an earlier order.

        ``order`` is the ``(symbol, side)`` an ``order_confirm`` dialog executes when it is confirmed.
        """
        with self._lock:
            if kind == "order_confirm" and order is None:
                raise ValueError("an order confirmation dialog needs order=(symbol, side)")
            self._dialog = _Dialog(kind, order)
            self._invalidate()

    @property
    def dialog(self) -> DialogKind | None:
        """The dialog currently shown, if any."""
        with self._lock:
            return self._dialog.kind if self._dialog else None

    @property
    def focused_field(self) -> tuple[str, FieldKind] | None:
        """The field that has keyboard focus, if any."""
        with self._lock:
            return self._focused

    def steal_focus(self) -> None:
        """Another window takes the foreground (keyboard input stops reaching the terminal)."""
        with self._lock:
            self.foreground = HANDLE_OTHER

    def advance(self, seconds: float) -> None:
        """Advance virtual time and run whatever became due (fills, close-alls)."""
        with self._lock:
            self.time += max(0.0, seconds)
            self._process_due()

    # ------------------------------------------------------------------ windows
    def client_rect_screen(self) -> Rect:
        """The client area in screen coordinates."""
        left, top = self.client_origin
        w, h = self.client_size
        return Rect(left, top, w, h)

    def windows(self) -> list[WindowInfo]:
        """Top-level windows of the simulated desktop (terminal, duplicates, popup, an unrelated window)."""
        with self._lock:
            left, top = self.client_origin
            w, h = self.client_size
            out: list[WindowInfo] = []
            if self.window_present:
                out.append(
                    WindowInfo(
                        HANDLE_TERMINAL,
                        self.title,
                        self.pid,
                        self.process_name,
                        left,
                        top,
                        w,
                        h,
                        self.minimized,
                        True,
                        self.dpi,
                    )
                )
            for i in range(self.duplicate_windows):
                off = 30 * (i + 1)
                out.append(
                    WindowInfo(
                        HANDLE_DUPLICATE_BASE + i,
                        self.title,
                        self.pid + 1 + i,
                        self.process_name,
                        left + off,
                        top + off,
                        w,
                        h,
                        False,
                        True,
                        self.dpi,
                    )
                )
            if self.popup is not None:
                p = self.popup
                out.append(
                    WindowInfo(
                        HANDLE_POPUP,
                        "Popup (simulation)",
                        777,
                        "popup.exe",
                        left + p.x,
                        top + p.y,
                        p.w,
                        p.h,
                        False,
                        True,
                        96,
                    )
                )
            out.append(
                WindowInfo(HANDLE_OTHER, "Notes (simulation)", 999, "notes.exe", 0, 0, 400, 300, False, True, 96)
            )
            out.extend(self.extra_windows)
            return out

    def _terminal_on_screen(self) -> bool:
        return self.window_present and not self.minimized

    def root_window_at(self, x: int, y: int) -> int | None:
        """Handle of the window on top at a screen point (popup over terminal over desktop)."""
        with self._lock:
            left, top = self.client_origin
            cx, cy = x - left, y - top
            if self.popup is not None and self._terminal_on_screen() and self.popup.contains(cx, cy):
                return HANDLE_POPUP
            if self._terminal_on_screen() and Rect(0, 0, *self.client_size).contains(cx, cy):
                return HANDLE_TERMINAL
            return None

    def restore(self, handle: int) -> None:
        """Un-minimize the terminal window."""
        with self._lock:
            if handle == HANDLE_TERMINAL:
                self.minimized = False
                self._invalidate()

    def activate(self, handle: int) -> bool:
        """Try to make ``handle`` the foreground window; returns whether it now is."""
        with self._lock:
            if self.locked:
                return False
            if handle == HANDLE_TERMINAL:
                if not self.window_present or self.refuse_focus:
                    return False
                self.minimized = False
                self.foreground = HANDLE_TERMINAL
            elif any(win.handle == handle for win in self.windows()):
                self.foreground = handle
            return self.foreground == handle

    # ------------------------------------------------------------------ input
    def before_input(self) -> None:
        """Raise :class:`OperatorAbortError` when the simulated fail-safe fires."""
        with self._lock:
            limit = self.failsafe_after_actions
            if self._failsafe_armed or (limit is not None and self.input_count >= limit):
                raise OperatorAbortError("fail-safe triggered (simulation)")

    def after_input(self, name: str) -> None:
        """Book-keeping after an input action: focus stealing and the :attr:`on_action` hook."""
        hook: Callable[[FakeTerminal, str], None] | None
        with self._lock:
            self.input_count += 1
            limit = self.steal_focus_after_actions
            if limit is not None and not self._focus_stolen and self.input_count >= limit:
                self._focus_stolen = True
                self.foreground = HANDLE_OTHER
            hook = self.on_action
        if hook is not None:
            hook(self, name)

    def click_screen(self, x: int, y: int) -> None:
        """A left click at a screen point, hit-tested against popup, dialog and widgets."""
        with self._lock:
            self.cursor = (x, y)
            if self.locked:
                return  # a locked session silently drops input
            target = self.root_window_at(x, y)
            if target == HANDLE_POPUP:
                self.foreground = HANDLE_POPUP
                return
            if target != HANDLE_TERMINAL:
                self.foreground = None
                return
            self.foreground = HANDLE_TERMINAL
            left, top = self.client_origin
            self._click_client(x - left, y - top)
            self._invalidate()

    def _click_client(self, cx: int, cy: int) -> None:
        geo = self.geometry(calibration=False)
        if self._dialog is not None:
            if geo.dialog_ok.contains(cx, cy):
                self._confirm_dialog()
            elif geo.dialog_cancel.contains(cx, cy):
                self._dialog = None
            return  # modal: everything else is ignored
        for panel in geo.panels.values():
            if panel.lot_field.contains(cx, cy):
                self._focus(panel.symbol, "lot")
                return
            if panel.sl_field is not None and panel.sl_field.contains(cx, cy):
                self._focus(panel.symbol, "sl")
                return
            if panel.tp_field is not None and panel.tp_field.contains(cx, cy):
                self._focus(panel.symbol, "tp")
                return
            if panel.buy_button.contains(cx, cy):
                self._order_click(panel.symbol, "BUY")
                return
            if panel.sell_button.contains(cx, cy):
                self._order_click(panel.symbol, "SELL")
                return
        if self.has_close_all and geo.close_all.contains(cx, cy):
            self._close_all_click()
            return
        self._commit_focused()

    def _focus(self, symbol: str, kind: FieldKind) -> None:
        if self._focused != (symbol, kind):
            self._commit_focused()  # the previously focused field loses the focus
        self._focused = (symbol, kind)
        self._selected_all = False  # a click puts the caret at the end, nothing selected

    def _commit_focused(self) -> None:
        """The focused field commits its text (tab/enter or the focus moving away) and loses the focus."""
        target = self._focused
        if target is not None and self.commit_transform is not None:
            self._fields[target] = self.commit_transform(target[1], self._fields[target])
        self._focused = None
        self._selected_all = False

    def _keyboard_target(self) -> tuple[str, FieldKind] | None:
        if self.locked or self.foreground != HANDLE_TERMINAL or self._dialog is not None:
            return None
        return self._focused

    def type_text(self, text: str) -> None:
        """Type into the focused field (replaces the selection, else appends)."""
        with self._lock:
            target = self._keyboard_target()
            if target is None:
                return
            current = "" if self._selected_all else self._fields[target]
            new = current + text
            if target[1] == "lot" and self.lot_field_transform is not None:
                new = self.lot_field_transform(new)
            self._fields[target] = new
            self._selected_all = False
            self._invalidate()

    def hotkey(self, keys: Sequence[str]) -> str | None:
        """Apply a key chord; returns text that should go to the clipboard (ctrl+c), if any."""
        with self._lock:
            norm = tuple(k.lower() for k in keys)
            target = self._keyboard_target()
            if target is None:
                return None
            if norm == ("ctrl", "a"):
                if not (target[1] == "lot" and self.lot_field_ignores_select_all):
                    self._selected_all = True
                return None
            if norm == ("ctrl", "c"):
                text = self._fields[target]
                if not self._selected_all or self.clipboard_copy_broken:
                    return None
                if not text and self.empty_copy_keeps_clipboard:
                    return None  # an empty selection copies nothing
                return text
            return None

    def press(self, key: str) -> None:
        """Press one key: tab/enter commit, esc dismisses dialogs, backspace/delete edit."""
        with self._lock:
            key = key.lower()
            if self.locked:
                return
            if key in ("esc", "escape"):
                if self._dialog is not None and self.foreground == HANDLE_TERMINAL:
                    self._dialog = None
                    self._invalidate()
                return
            target = self._keyboard_target()
            if target is None:
                return
            if key in ("tab", "enter", "return"):
                self._commit_focused()
            elif key in ("backspace", "delete"):
                if self._selected_all:
                    self._fields[target] = ""
                else:
                    self._fields[target] = self._fields[target][:-1]
                self._selected_all = False
            self._invalidate()

    # ------------------------------------------------------------------ orders
    def _order_click(self, symbol: str, side: str) -> None:
        self.order_clicks.append((symbol, side))
        if self.failsafe_after_order_click:
            self._failsafe_armed = True
        self._commit_focused()  # the button takes the focus: the field commits before the order reads it
        if not self.one_click_enabled:
            return
        if self.order_confirm_dialog:
            self._dialog = _Dialog("order_confirm", (symbol, side))
            return
        self._execute_order(symbol, side)

    def _execute_order(self, symbol: str, side: str) -> None:
        lot_text = self._fields[(symbol, "lot")]
        lots = parse_lots(lot_text)
        if self.order_error or lots is None or lots <= 0:
            self._dialog = _Dialog("error")
            return
        sl = self._fields[(symbol, "sl")] or None
        tp = self._fields[(symbol, "tp")] or None
        order = FakeOrder(symbol, side, lot_text, lots, sl, tp, self.time)
        if self.fill_enabled:
            self._schedule(lambda: self._fill(order))

    def _fill(self, order: FakeOrder) -> None:
        self.orders.append(order)
        if not self.netting:
            self.positions.append(FakePosition(order.symbol, order.side, order.lots))
            return
        for pos in self.positions:
            if pos.symbol != order.symbol:
                continue
            if pos.side == order.side:
                pos.lots += order.lots
            elif pos.lots > order.lots:
                pos.lots -= order.lots
            elif pos.lots == order.lots:
                self.positions.remove(pos)
            else:
                pos.lots = order.lots - pos.lots
                pos.side = order.side
            return
        self.positions.append(FakePosition(order.symbol, order.side, order.lots))

    def _close_all_click(self) -> None:
        self.close_all_clicks += 1
        self._commit_focused()
        if self.close_all_confirm_dialog:
            self._dialog = _Dialog("close_all_confirm")
            return
        self._execute_close_all()

    def _execute_close_all(self) -> None:
        if self.order_error:
            self._dialog = _Dialog("error")
            return
        if self.fill_enabled:
            self._schedule(self.positions.clear)

    def _confirm_dialog(self) -> None:
        dialog = self._dialog
        self._dialog = None
        if dialog is None:
            return
        self.dialog_confirms += 1
        if dialog.kind == "order_confirm" and dialog.order is not None:
            self._execute_order(*dialog.order)
        elif dialog.kind == "close_all_confirm":
            self._execute_close_all()

    def _schedule(self, action: Callable[[], None]) -> None:
        self._pending.append(_Pending(self.time + max(0.0, self.fill_delay_seconds), action))
        self._process_due()

    def _process_due(self) -> None:
        due = [p for p in self._pending if p.due <= self.time]
        if not due:
            return
        self._pending = [p for p in self._pending if p.due > self.time]
        for item in due:
            item.action()
        self._invalidate()

    # ------------------------------------------------------------------ rendering
    def _invalidate(self) -> None:
        self._render_cache = None

    def _font(self, size: int) -> Font:
        font = self._fonts.get(size)
        if font is None:
            font = ImageFont.load_default(size=size)
            self._fonts[size] = font
        return font

    def render_client(self) -> Image.Image:
        """The client area as the simulated user would see it right now."""
        with self._lock:
            self._process_due()
            if self.indicator_flicker:
                self._flicker_phase += 1
            dialog = self._dialog.kind if self._dialog else None
            return self._render(calibration=False, dialog=dialog)

    def render_for_calibration(self, dialog: DialogKind | None = None) -> Image.Image:
        """A clean rendering (no swap, no flicker, no popup) used by :func:`calibrate_fake`."""
        with self._lock:
            return self._render(calibration=True, dialog=dialog)

    def _render(self, *, calibration: bool, dialog: DialogKind | None) -> Image.Image:
        flicker = (not calibration) and self.indicator_flicker and self._flicker_phase % 2 == 1
        key: tuple[Any, ...] = (
            calibration,
            dialog,
            flicker,
            self.client_size,
            self.badge_text,
            self.one_click_enabled,
            self.has_close_all,
            self.protective_fields,
            self.swap_panels and not calibration,
            tuple(sorted(self._fields.items())),
            self._focused,
            tuple((p.symbol, p.side, p.lots) for p in self.positions),
            self._dialog.order if self._dialog else None,
        )
        if self._render_cache is not None and self._render_cache[0] == key:
            return self._render_cache[1].copy()
        geo = self.geometry(calibration=calibration)
        w, h = self.client_size
        img = Image.new("RGB", (w, h), _BG)
        draw = ImageDraw.Draw(img)
        # header (made-up chrome that says what it is)
        draw.rectangle((0, 0, w - 1, _HEADER_H - 1), fill=_HEADER)
        draw.text((16, 18), "SIMULATED TERMINAL - test double", fill=(230, 232, 236), font=self._font(16))
        self._draw_badge(draw, geo.badge)
        self._draw_one_click(draw, geo.one_click)
        for panel in geo.panels.values():
            self._draw_panel(draw, panel)
        self._draw_indicator(draw, geo.indicator, flicker)
        if self.has_close_all:
            self._button(draw, geo.close_all, "CLOSE ALL", (90, 90, 100), (255, 255, 255))
        if dialog is not None:
            self._draw_dialog(draw, geo, dialog)
        self._render_cache = (key, img)
        return img.copy()

    def _button(
        self, draw: ImageDraw.ImageDraw, r: Rect, text: str, fill: tuple[int, int, int], ink: tuple[int, int, int]
    ) -> None:
        draw.rectangle((r.x, r.y, r.x + r.w - 1, r.y + r.h - 1), fill=fill, outline=(30, 30, 30))
        draw.text(r.center, text, fill=ink, font=self._font(16), anchor="mm")

    def _draw_badge(self, draw: ImageDraw.ImageDraw, r: Rect) -> None:
        text = self.badge_text
        if text.upper() == "DEMO":
            fill, ink = (240, 170, 40), (20, 20, 20)
        elif text.upper() == "LIVE":
            fill, ink = (190, 30, 30), (255, 255, 255)
        else:
            fill, ink = (120, 120, 120), (255, 255, 255)
        draw.rectangle((r.x, r.y, r.x + r.w - 1, r.y + r.h - 1), fill=fill)
        draw.text(r.center, text, fill=ink, font=self._font(18), anchor="mm")

    def _draw_one_click(self, draw: ImageDraw.ImageDraw, r: Rect) -> None:
        if self.one_click_enabled:
            fill, text = (40, 160, 80), "1-CLICK ON"
        else:
            fill, text = (110, 110, 110), "1-CLICK OFF"
        draw.rectangle((r.x, r.y, r.x + r.w - 1, r.y + r.h - 1), fill=fill)
        draw.text(r.center, text, fill=(255, 255, 255), font=self._font(15), anchor="mm")

    def _draw_field(self, draw: ImageDraw.ImageDraw, r: Rect, text: str, focused: bool) -> None:
        outline = _FIELD_FOCUS if focused else _PANEL_BORDER
        draw.rectangle((r.x, r.y, r.x + r.w - 1, r.y + r.h - 1), fill=_FIELD, outline=outline, width=2)
        draw.text((r.x + 6, r.y + r.h // 2), text, fill=_TEXT, font=self._font(15), anchor="lm")

    def _draw_panel(self, draw: ImageDraw.ImageDraw, p: FakePanelGeometry) -> None:
        f = p.frame
        draw.rectangle((f.x, f.y, f.x + f.w - 1, f.y + f.h - 1), fill=_PANEL, outline=_PANEL_BORDER, width=2)
        draw.text((p.label.x + 4, p.label.y + p.label.h // 2), p.symbol, fill=_TEXT, font=self._font(22), anchor="lm")
        draw.text((f.x + 14, p.lot_field.y + p.lot_field.h // 2), "Lots", fill=_TEXT, font=self._font(14), anchor="lm")
        self._draw_field(draw, p.lot_field, self._fields[(p.symbol, "lot")], self._focused == (p.symbol, "lot"))
        self._button(draw, p.sell_button, "SELL", _SELL, (255, 255, 255))
        self._button(draw, p.buy_button, "BUY", _BUY, (255, 255, 255))
        if p.sl_field is not None and p.tp_field is not None:
            draw.text((f.x + 14, p.sl_field.y + 14), "SL", fill=_TEXT, font=self._font(13), anchor="lm")
            draw.text((f.x + 152, p.tp_field.y + 14), "TP", fill=_TEXT, font=self._font(13), anchor="lm")
            self._draw_field(draw, p.sl_field, self._fields[(p.symbol, "sl")], self._focused == (p.symbol, "sl"))
            self._draw_field(draw, p.tp_field, self._fields[(p.symbol, "tp")], self._focused == (p.symbol, "tp"))

    def _draw_indicator(self, draw: ImageDraw.ImageDraw, r: Rect, flicker: bool) -> None:
        draw.rectangle((r.x, r.y, r.x + r.w - 1, r.y + r.h - 1), fill=(255, 255, 255), outline=_PANEL_BORDER)
        draw.text((r.x + 8, r.y + 6), f"Open positions: {len(self.positions)}", fill=_TEXT, font=self._font(14))
        for i, pos in enumerate(self.positions[:3]):
            ry = r.y + 28 + i * 27
            shade = (205, 225, 250) if pos.side == "BUY" else (250, 215, 210)
            draw.rectangle((r.x + 4, ry, r.x + r.w - 5, ry + 24), fill=shade)
            draw.text(
                (r.x + 10, ry + 12),
                f"{pos.symbol}  {pos.side}  {pos.lots}",
                fill=_TEXT,
                font=self._font(14),
                anchor="lm",
            )
        if flicker:
            draw.rectangle((r.x + r.w - 36, r.y + 4, r.x + r.w - 8, r.y + 24), fill=(255, 0, 255))

    def _draw_dialog(self, draw: ImageDraw.ImageDraw, geo: _Geometry, kind: DialogKind) -> None:
        d = geo.dialog
        if kind == "order_confirm":
            head, title = (40, 90, 170), "Confirm order (simulation)"
            order = self._dialog.order if self._dialog and self._dialog.order else ("?", "?")
            body = f"{order[1]} {self._fields.get((order[0], 'lot'), '?')} {order[0]}?"
        elif kind == "close_all_confirm":
            head, title, body = (120, 60, 150), "Close everything? (simulation)", "All positions will be closed."
        else:
            head, title, body = (170, 30, 30), "ERROR (simulation)", "The simulated order was refused."
        draw.rectangle((d.x, d.y, d.x + d.w - 1, d.y + d.h - 1), fill=(248, 248, 248), outline=(20, 20, 20), width=2)
        hh = geo.dialog_header
        draw.rectangle((hh.x, hh.y, hh.x + hh.w - 1, hh.y + hh.h - 1), fill=head)
        draw.text((hh.x + 12, hh.y + hh.h // 2), title, fill=(255, 255, 255), font=self._font(17), anchor="lm")
        draw.text((d.x + 16, d.y + 72), body, fill=_TEXT, font=self._font(15), anchor="lm")
        self._button(draw, geo.dialog_ok, "OK" if kind != "error" else "Close", (60, 130, 70), (255, 255, 255))
        if kind != "error":
            self._button(draw, geo.dialog_cancel, "Cancel", (150, 150, 150), (255, 255, 255))

    def render_screen_region(
        self, left: int, top: int, width: int, height: int, desktop: tuple[int, int, int] = _DESKTOP
    ) -> Image.Image:
        """Compose the simulated screen inside a rectangle (screen coordinates)."""
        with self._lock:
            if self.locked:
                return Image.new("RGB", (width, height), (0, 0, 0))
            out = Image.new("RGB", (width, height), desktop)
            if self._terminal_on_screen():
                cl, ct = self.client_origin
                out.paste(self.render_client(), (cl - left, ct - top))
                if self.popup is not None:
                    p = self.popup
                    pop = Image.new("RGB", (p.w, p.h), (90, 90, 96))
                    pdraw = ImageDraw.Draw(pop)
                    pdraw.rectangle((0, 0, p.w - 1, p.h - 1), outline=(250, 250, 0), width=3)
                    pdraw.text((8, 8), "Popup (simulation)", fill=(255, 255, 255), font=self._font(14))
                    out.paste(pop, (cl + p.x - left, ct + p.y - top))
            return out


class FakeGuiBackend:
    """:class:`GuiBackend` over a :class:`FakeTerminal` (simulation only; see the module docstring).

    Every call is appended to :attr:`actions`. ``sleep`` is virtual: it advances the terminal's time and,
    when given, ``clock`` (a :class:`ManualClock`); with ``real_sleep=True`` it also really sleeps
    (useful for ``backend: fake`` demos on the system clock).
    """

    def __init__(
        self,
        terminal: FakeTerminal,
        screen_size: tuple[int, int] = (1920, 1080),
        *,
        clock: ManualClock | None = None,
        real_sleep: bool = False,
    ) -> None:
        self.terminal = terminal
        self.screen_size = screen_size
        self._clock = clock
        self._real_sleep = real_sleep
        self._lock = threading.Lock()
        self.actions: list[FakeAction] = []
        self.capture_hook: Callable[[int, int, int, int], object] | None = None  # test hook, runs before a capture

    def _log(self, name: str, *args: Any) -> None:
        with self._lock:
            self.actions.append(FakeAction(name, args, threading.current_thread().name))

    def input_actions(self) -> list[FakeAction]:
        """Only the mouse/keyboard actions (what could have changed something on screen)."""
        with self._lock:
            return [a for a in self.actions if a.name in INPUT_ACTIONS]

    def clicks(self) -> list[tuple[int, int]]:
        """Screen points of every click made through this backend."""
        with self._lock:
            return [(int(a.args[0]), int(a.args[1])) for a in self.actions if a.name == "click"]

    # --- windows -------------------------------------------------------------------------------
    def list_windows(self) -> list[WindowInfo]:
        """Visible top-level windows with a title."""
        self._log("list_windows")
        return [w for w in self.terminal.windows() if w.visible and w.title]

    def get_window(self, handle: int) -> WindowInfo | None:
        """Current info for one window, or None if it is gone."""
        self._log("get_window", handle)
        for win in self.terminal.windows():
            if win.handle == handle:
                return win
        return None

    def restore_window(self, handle: int) -> None:
        """Un-minimize a window (never resizes)."""
        self._log("restore_window", handle)
        self.terminal.restore(handle)

    def activate_window(self, handle: int) -> bool:
        """Bring a window to the foreground; returns whether it is now the foreground."""
        self._log("activate_window", handle)
        return self.terminal.activate(handle)

    def foreground_window(self) -> int | None:
        """Handle of the foreground window."""
        self._log("foreground_window")
        return self.terminal.foreground

    def root_window_at(self, x: int, y: int) -> int | None:
        """Top-level window owning a screen point."""
        self._log("root_window_at", x, y)
        return self.terminal.root_window_at(x, y)

    def input_desktop_available(self) -> bool:
        """False while the simulated session is locked."""
        self._log("input_desktop_available")
        return not self.terminal.locked

    # --- screen --------------------------------------------------------------------------------
    def capture(self, left: int, top: int, width: int, height: int) -> Image.Image:
        """Screenshot of a screen rectangle, exactly ``width`` x ``height``."""
        if width <= 0 or height <= 0:
            raise BackendError(f"invalid capture size {width}x{height}")
        self._log("capture", left, top, width, height)
        hook = self.capture_hook
        if hook is not None:
            hook(left, top, width, height)
        return self.terminal.render_screen_region(left, top, width, height)

    # --- input ---------------------------------------------------------------------------------
    def _input(self, name: str, *args: Any) -> None:
        self.terminal.before_input()
        self._log(name, *args)

    def move_mouse(self, x: int, y: int) -> None:
        """Move the pointer (an input action: the fail-safe may fire)."""
        self._input("move_mouse", x, y)
        self.terminal.cursor = (x, y)
        self.terminal.after_input("move_mouse")

    def click(self, x: int, y: int) -> None:
        """Left click at a screen point."""
        self._input("click", x, y)
        self.terminal.click_screen(x, y)
        self.terminal.after_input("click")

    def hotkey(self, *keys: str) -> None:
        """Key chord, e.g. ``hotkey("ctrl", "a")``; ctrl+c fills the clipboard."""
        self._input("hotkey", *keys)
        copied = self.terminal.hotkey(keys)
        if copied is not None:
            self.terminal.clipboard = copied
        self.terminal.after_input("hotkey")

    def press(self, key: str) -> None:
        """Press one key."""
        self._input("press", key)
        self.terminal.press(key)
        self.terminal.after_input("press")

    def type_text(self, text: str) -> None:
        """Type literal text into the focused field."""
        self._input("type_text", text)
        self.terminal.type_text(text)
        self.terminal.after_input("type_text")

    def cursor_position(self) -> tuple[int, int]:
        """Last pointer position."""
        self._log("cursor_position")
        return self.terminal.cursor

    # --- clipboard -----------------------------------------------------------------------------
    def get_clipboard_text(self) -> str | None:
        """Simulated clipboard text."""
        self._log("get_clipboard_text")
        return self.terminal.clipboard

    def set_clipboard_text(self, text: str) -> None:
        """Replace the simulated clipboard text."""
        self._log("set_clipboard_text", text)
        self.terminal.clipboard = text

    # --- misc ----------------------------------------------------------------------------------
    def sleep(self, seconds: float) -> None:
        """Virtual sleep: advances the terminal's time (and the optional clock)."""
        seconds = max(0.0, float(seconds))
        self._log("sleep", seconds)
        if self._real_sleep and seconds > 0:
            _time.sleep(seconds)
        if self._clock is not None:
            self._clock.advance(seconds)
        self.terminal.advance(seconds)


def calibrate_fake(
    terminal: FakeTerminal,
    out_dir: Path,
    *,
    calibrated_at: datetime | None = None,
    one_click_indicator: bool = True,
    error_template: bool = True,
) -> LayoutFile:
    """Build a correct layout + templates for ``terminal`` programmatically (simulation only).

    Templates go to ``out_dir/fake-templates/`` and the layout to ``out_dir/fake-terminal.layout.yaml``
    (names chosen so a real calibration in the same folder is never overwritten). Dialogs are
    calibrated when the terminal currently has them enabled; the account kind follows ``badge_text``.
    """
    kind_by_badge = {"DEMO": "demo", "LIVE": "live"}
    account_kind = kind_by_badge.get(terminal.badge_text.upper())
    if account_kind is None:
        raise ValueError(f"calibrate_fake needs badge_text DEMO or LIVE, not {terminal.badge_text!r}")
    base = out_dir.resolve()
    tdir = base / FAKE_TEMPLATE_DIR
    tdir.mkdir(parents=True, exist_ok=True)
    geo = terminal.geometry(calibration=True)
    clean = terminal.render_for_calibration()

    def save(name: str, img: Image.Image, rect: Rect) -> TemplateRef:
        rel = f"{FAKE_TEMPLATE_DIR}/{name}.png"
        img.crop(rect.box).save(base / rel, format="PNG")
        return TemplateRef(region=rect.region(), image=rel)

    panels: dict[str, PanelLayout] = {}
    for sym, p in geo.panels.items():
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", sym)
        panels[sym] = PanelLayout(
            lot_input=p.lot_field.point(),
            buy_button=p.buy_button.point(),
            sell_button=p.sell_button.point(),
            label=save(f"panel_{safe}", clean, p.label),
            sl_input=p.sl_field.point() if p.sl_field else None,
            tp_input=p.tp_field.point() if p.tp_field else None,
        )

    def dialog(kind: DialogKind, name: str) -> DialogLayout:
        img = terminal.render_for_calibration(dialog=kind)
        return DialogLayout(template=save(name, img, geo.dialog_header), confirm_button=geo.dialog_ok.point())

    errors: list[TemplateRef] = []
    if error_template:
        errors.append(save("error_dialog", terminal.render_for_calibration(dialog="error"), geo.dialog_header))

    w, h = terminal.client_size
    layout = Layout(
        window_title_regex=re.escape(terminal.title),
        process_name=terminal.process_name,
        client_width=w,
        client_height=h,
        dpi=terminal.dpi,
        account_kind="demo" if account_kind == "demo" else "live",
        account_badge=save("account_badge", clean, geo.badge),
        one_click_indicator=save("one_click", clean, geo.one_click) if one_click_indicator else None,
        positions_indicator=geo.indicator.region(),
        panels=panels,
        close_all_button=geo.close_all.point() if terminal.has_close_all else None,
        close_all_dialog=dialog("close_all_confirm", "close_all_dialog") if terminal.close_all_confirm_dialog else None,
        order_dialog=dialog("order_confirm", "order_dialog") if terminal.order_confirm_dialog else None,
        error_templates=errors,
        park_point=Point(x=geo.park[0], y=geo.park[1]),
        calibrated_at=calibrated_at or datetime.now(UTC),
        notes="SIMULATED layout generated by calibrate_fake() for FakeTerminal; "
        "it says nothing about the real UniTrader terminal.",
    )
    layout_file = LayoutFile(layout, base)
    layout_file.save(base / FAKE_LAYOUT_FILENAME)
    log.info("simulated layout written to %s", base / FAKE_LAYOUT_FILENAME)
    return layout_file
