"""Tests for the interactive calibration wizard.

The wizard is driven end-to-end against a small scripted ``GuiBackend`` defined here (a random-noise
"screen" with made-up windows; it has nothing to do with the real UniTrader UI) and scripted prompt
answers. The backend fails the test on any click, key press, typing, clipboard use, window activation or
restore, because calibration must never do any of those.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml
from PIL import Image, ImageDraw

from amongus_desk.clock import ManualClock
from amongus_desk.config import DeskConfig
from amongus_desk.execution.unitrader import calibrate as calibrate_mod
from amongus_desk.execution.unitrader.backend import BackendError, OperatorAbortError, WindowInfo
from amongus_desk.execution.unitrader.calibrate import (
    TEMPLATES_DIR,
    CalibrationAborted,
    CalibrationError,
    CalibrationResult,
    TemplateCheck,
    run_calibration,
    template_refs,
)
from amongus_desk.execution.unitrader.layout import Layout, LayoutFile
from amongus_desk.execution.unitrader.vision import similarity
from factories import T0, eurusd, make_config

# ------------------------------------------------------------------------------------------- the fake desk

SCREEN = (1280, 800)
TERM, CONSOLE, POPUP, TWIN = 0x1001, 0x2002, 0x3003, 0x4004
ORIGIN = (100, 80)  # screen position of the terminal's client area
CLIENT = (800, 600)
TITLE = "Terminal - Demo 12345"  # made-up title of the made-up terminal window
PROCESS = "Terminal.exe"

PARK = (700, 20)
BADGE = ((10, 10), (59, 25))  # 50x16
POSITIONS = ((0, 500), (199, 529))  # 200x30
CLOSE_ALL = (400, 560)
PANELS: dict[str, dict[str, Any]] = {
    "EURUSD": {"label": ((50, 60), (119, 75)), "lot": (100, 100), "buy": (150, 140), "sell": (60, 140)},
    "GBPUSD": {"label": ((300, 60), (369, 75)), "lot": (350, 100), "buy": (400, 140), "sell": (310, 140)},
}
SLTP = {"EURUSD": ((100, 170), (100, 190)), "GBPUSD": ((350, 170), (350, 190))}
DIALOG_AREA = ((250, 250), (449, 329))  # where the made-up confirmation dialog shows up (client coords)
DIALOG_CONFIRM = (300, 310)
ERROR_AREA = ((500, 250), (599, 289))


def to_screen(x: int, y: int) -> tuple[int, int]:
    return ORIGIN[0] + x, ORIGIN[1] + y


def noise(width: int, height: int, seed: int) -> Image.Image:
    rng = np.random.default_rng(seed)
    return Image.fromarray(rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8))


def window(handle: int = TERM, **overrides: Any) -> WindowInfo:
    base: dict[str, Any] = {
        "handle": handle,
        "title": TITLE,
        "pid": 4242,
        "process_name": PROCESS,
        "client_left": ORIGIN[0],
        "client_top": ORIGIN[1],
        "client_width": CLIENT[0],
        "client_height": CLIENT[1],
        "minimized": False,
        "visible": True,
        "dpi": 96,
    }
    base.update(overrides)
    return WindowInfo(**base)


CONSOLE_WINDOW = window(
    CONSOLE,
    title="Console",
    pid=99,
    process_name="console.exe",
    client_left=950,
    client_top=0,
    client_width=300,
    client_height=200,
)


class ScriptedBackend:
    """A ``GuiBackend`` whose mouse positions come from a script and whose screen is a fixed image."""

    def __init__(self, hovers: Sequence[tuple[int, int]], windows: Sequence[WindowInfo] | None = None) -> None:
        self.screen = noise(*SCREEN, seed=7)
        self.windows: list[WindowInfo] = list(windows) if windows is not None else [CONSOLE_WINDOW, window()]
        self.stack: list[WindowInfo] = list(self.windows)  # Z order for root_window_at, topmost first
        self.hovers: deque[tuple[int, int]] = deque(hovers)
        self.cursor = (0, 0)
        self.moves: list[tuple[int, int]] = []
        self.sleeps: list[float] = []
        self.locked = False
        self.captures = 0
        self.capture_hook: Callable[[int, int, int, int, Image.Image], Image.Image] | None = None
        self.window_hook: Callable[[WindowInfo], WindowInfo | None] | None = None
        self.cursor_error: Exception | None = None

    # windows
    def list_windows(self) -> list[WindowInfo]:
        return list(self.windows)

    def get_window(self, handle: int) -> WindowInfo | None:
        for win in self.windows:
            if win.handle == handle:
                return self.window_hook(win) if self.window_hook else win
        return None

    def restore_window(self, handle: int) -> None:
        raise AssertionError("calibration must never restore (or otherwise change) the terminal window")

    def activate_window(self, handle: int) -> bool:
        raise AssertionError("calibration must never activate the terminal: the operator types into the console")

    def foreground_window(self) -> int | None:
        return CONSOLE

    def root_window_at(self, x: int, y: int) -> int | None:
        for win in self.stack:
            if (
                win.client_left <= x < win.client_left + win.client_width
                and win.client_top <= y < win.client_top + win.client_height
            ):
                return win.handle
        return None

    def input_desktop_available(self) -> bool:
        return not self.locked

    # screen
    def capture(self, left: int, top: int, width: int, height: int) -> Image.Image:
        self.captures += 1
        img = self.screen.crop((left, top, left + width, top + height))
        if self.capture_hook is not None:
            img = self.capture_hook(left, top, width, height, img)
        return img

    def paste(self, area: tuple[tuple[int, int], tuple[int, int]], image: Image.Image) -> None:
        self.screen.paste(image, to_screen(*area[0]))

    # input
    def move_mouse(self, x: int, y: int) -> None:
        self.moves.append((x, y))
        self.cursor = (x, y)

    def click(self, x: int, y: int) -> None:
        raise AssertionError("calibration must never click")

    def hotkey(self, *keys: str) -> None:
        raise AssertionError("calibration must never press keys")

    def press(self, key: str) -> None:
        raise AssertionError("calibration must never press keys")

    def type_text(self, text: str) -> None:
        raise AssertionError("calibration must never type")

    def cursor_position(self) -> tuple[int, int]:
        if self.cursor_error is not None:
            raise self.cursor_error
        if not self.hovers:
            raise AssertionError("the wizard asked for more mouse positions than the test scripted")
        self.cursor = self.hovers.popleft()
        return self.cursor

    # clipboard
    def get_clipboard_text(self) -> str | None:
        raise AssertionError("calibration must never touch the clipboard")

    def set_clipboard_text(self, text: str) -> None:
        raise AssertionError("calibration must never touch the clipboard")

    # misc
    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


# ------------------------------------------------------------------------------------------- scripting


@dataclass
class Script:
    """Prompt answers (each checked against an expected fragment of the question) plus mouse positions."""

    prompts: list[tuple[str, str, Callable[[], None] | None]] = field(default_factory=list)
    hovers: list[tuple[int, int]] = field(default_factory=list)

    def ask(self, expect: str, answer: str, action: Callable[[], None] | None = None) -> Script:
        self.prompts.append((expect, answer, action))
        return self

    def at(self, x: int, y: int) -> Script:
        self.hovers.append(to_screen(x, y))
        return self

    def at_screen(self, x: int, y: int) -> Script:
        self.hovers.append((x, y))
        return self

    def area(self, area: tuple[tuple[int, int], tuple[int, int]]) -> Script:
        return self.at(*area[0]).at(*area[1])


class ScriptedPrompt:
    def __init__(self, steps: Sequence[tuple[str, str, Callable[[], None] | None]]) -> None:
        self.steps = deque(steps)
        self.asked: list[str] = []

    def __call__(self, question: str) -> str:
        self.asked.append(question)
        if not self.steps:
            raise AssertionError(f"unexpected question: {question!r}")
        expect, answer, action = self.steps.popleft()
        assert expect.lower() in question.lower(), f"expected a question about {expect!r}, got {question!r}"
        if action is not None:
            action()
        return answer


def intro(s: Script) -> Script:
    return s.ask("logged in to UniTrader yourself", "y")


def pick_window(s: Script, number: str = "2", pattern: str = "") -> Script:
    return (
        s.ask("number of the UniTrader window", number).ask("belong to the program", "y").ask("Title pattern", pattern)
    )


def park_badge(s: Script, kind: str = "demo") -> Script:
    s.at(*PARK).area(BADGE).ask("Which account", kind)
    if kind == "live":
        s.ask("LIVE (real-money)", "y")
    return s


def no_one_click(s: Script) -> Script:
    return s.ask("one-click trading is switched on", "n")


def positions(s: Script) -> Script:
    return s.area(POSITIONS)


def panel(s: Script, symbol: str, *, sltp: str | None = "n") -> Script:
    p = PANELS[symbol]
    s.area(p["label"]).at(*p["lot"]).at(*p["buy"]).at(*p["sell"])
    if sltp is not None:
        s.ask("stop-loss and take-profit", sltp)
    if sltp in ("y", "required"):
        s.at(*SLTP[symbol][0]).at(*SLTP[symbol][1])
    return s


def close_all(s: Script, *, button: bool = True) -> Script:
    s.ask("closes ALL open positions", "y" if button else "n")
    if button:
        s.at(*CLOSE_ALL).ask("close-all button ask for confirmation", "n")
    return s


def tail(s: Script, *, save: str = "y") -> Script:
    return s.ask("after a BUY/SELL click", "n").ask("record an error message", "n").ask("Save this layout", save)


def happy(symbols: Sequence[str] = ("EURUSD",), *, kind: str = "demo") -> Script:
    s = Script()
    intro(s)
    pick_window(s)
    park_badge(s, kind)
    no_one_click(s)
    positions(s)
    for symbol in symbols:
        panel(s, symbol)
    close_all(s)
    return tail(s)


@dataclass
class Run:
    backend: ScriptedBackend
    prompt: ScriptedPrompt
    lines: list[str]
    out: Path

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def make_run(tmp_path: Path, script: Script, windows: Sequence[WindowInfo] | None = None) -> Run:
    return Run(
        ScriptedBackend(script.hovers, windows), ScriptedPrompt(script.prompts), [], tmp_path / "unitrader.layout.yaml"
    )


def calibrate(
    run: Run, cfg: DeskConfig | None = None, symbols: Sequence[str] | None = ("EURUSD",), **kwargs: Any
) -> CalibrationResult:
    kwargs.setdefault("countdown", 3)
    kwargs.setdefault("clock", ManualClock(T0))
    return run_calibration(
        cfg or make_config(), run.backend, run.out, symbols, prompt=run.prompt, say=run.lines.append, **kwargs
    )


def two_symbol_config(**overrides: Any) -> DeskConfig:
    return make_config(
        instruments=[eurusd(), eurusd(symbol="GBPUSD", feed_symbol="GBP/USD", currencies=["GBP", "USD"])], **overrides
    )


def assert_consumed(run: Run) -> None:
    assert not run.prompt.steps, f"unused prompt answers: {list(run.prompt.steps)}"
    assert not run.backend.hovers, f"unused mouse positions: {list(run.backend.hovers)}"


def assert_nothing_written(run: Run) -> None:
    assert not run.out.exists()
    templates = run.out.parent / TEMPLATES_DIR
    assert not templates.exists() or not any(templates.iterdir())


def region_box(area: tuple[tuple[int, int], tuple[int, int]]) -> tuple[int, int, int, int]:
    (x0, y0), (x1, y1) = area
    return x0, y0, x1 - x0 + 1, y1 - y0 + 1


# ------------------------------------------------------------------------------------------- happy paths


def test_end_to_end_writes_a_valid_layout_and_templates(tmp_path: Path) -> None:
    run = make_run(tmp_path, happy())
    result = calibrate(run)

    assert_consumed(run)
    assert result.ok and bool(result)
    assert result.path == run.out
    loaded = LayoutFile.load(run.out)  # parses and validates (incl. "inside the client area")
    lay = loaded.layout
    assert lay == result.layout_file.layout
    assert lay.window_title_regex == re.escape(TITLE)
    assert re.fullmatch(lay.window_title_regex, TITLE)
    assert lay.process_name == PROCESS
    assert (lay.client_width, lay.client_height, lay.dpi) == (*CLIENT, 96)
    assert lay.account_kind == "demo"
    assert (lay.park_point.x, lay.park_point.y) == PARK
    badge = lay.account_badge.region
    assert (badge.x, badge.y, badge.w, badge.h) == region_box(BADGE)
    pos = lay.positions_indicator
    assert (pos.x, pos.y, pos.w, pos.h) == region_box(POSITIONS)
    assert set(lay.panels) == {"EURUSD"}
    eur = lay.panels["EURUSD"]
    assert (eur.lot_input.x, eur.lot_input.y) == PANELS["EURUSD"]["lot"]
    assert (eur.buy_button.x, eur.buy_button.y) == PANELS["EURUSD"]["buy"]
    assert (eur.sell_button.x, eur.sell_button.y) == PANELS["EURUSD"]["sell"]
    assert eur.sl_input is None and eur.tp_input is None
    assert lay.close_all_button is not None and (lay.close_all_button.x, lay.close_all_button.y) == CLOSE_ALL
    assert lay.one_click_indicator is None and lay.order_dialog is None and lay.close_all_dialog is None
    assert lay.error_templates == []
    assert lay.calibrated_at == T0

    # every template exists, has exactly its region's size and holds the pixels of that screen area
    for _name, (ref, _required) in template_refs(lay).items():
        assert ref.image.startswith(f"{TEMPLATES_DIR}/")
        path = loaded.template_path(ref)
        assert path.is_file() and path.suffix == ".png"
        with Image.open(path) as img:
            assert img.size == (ref.region.w, ref.region.h)
        expected = run.backend.screen.crop(
            (
                ORIGIN[0] + ref.region.x,
                ORIGIN[1] + ref.region.y,
                ORIGIN[0] + ref.region.x + ref.region.w,
                ORIGIN[1] + ref.region.y + ref.region.h,
            )
        )
        assert np.array_equal(np.asarray(loaded.load_template(ref)), np.asarray(expected))
    assert not list((tmp_path / TEMPLATES_DIR).glob("*.tmp"))
    assert not list(tmp_path.glob("*.tmp"))

    # re-verification reported a similarity score for every template
    assert {c.name for c in result.checks} == {"account_badge", "panel:EURUSD"}
    assert all(c.required and c.passed and c.score == pytest.approx(1.0) for c in result.checks)
    assert all(c.threshold == pytest.approx(0.92) for c in result.checks)
    assert "similarity" in run.text and "Calibration saved and verified" in run.text


def test_tells_the_operator_to_log_in_themselves_and_counts_down(tmp_path: Path) -> None:
    run = make_run(tmp_path, happy())
    calibrate(run)
    text = run.text
    assert "log in to your account YOURSELF" in text
    assert "never asks for, reads or stores your password" in text
    assert "one-click trading panels for: EURUSD" in text
    assert "Hover the mouse over the account badge" not in text  # regions are marked corner by corner
    assert "Hover the mouse over the TOP-LEFT corner of the account badge" in text
    assert "Do not click. Capturing in 3..." in text
    assert "  2..." in run.lines and "  1..." in run.lines
    # 11 hovers x 3 one-second ticks, all slept through the backend (no real sleeping)
    assert run.backend.sleeps.count(1.0) == 11 * 3
    # the first question is the log-in confirmation, before any window is looked at
    assert "logged in to UniTrader yourself" in run.prompt.asked[0]


def test_parks_the_mouse_on_the_rest_spot_before_screenshots(tmp_path: Path) -> None:
    run = make_run(tmp_path, happy())
    calibrate(run)
    assert run.backend.moves, "the mouse should be parked before every template screenshot"
    assert set(run.backend.moves) == {to_screen(*PARK)}


def test_countdown_zero_does_not_wait(tmp_path: Path) -> None:
    run = make_run(tmp_path, happy())
    calibrate(run, countdown=0, settle_seconds=0.0)
    assert "Capturing now." in run.text
    assert 1.0 not in run.backend.sleeps


def test_subset_of_symbols(tmp_path: Path) -> None:
    run = make_run(tmp_path, happy(("GBPUSD",)))
    result = calibrate(run, two_symbol_config(), symbols=["gbpusd"])
    assert_consumed(run)
    assert set(result.layout_file.layout.panels) == {"GBPUSD"}
    assert "one-click trading panels for: GBPUSD" in run.text


def test_default_symbols_are_all_enabled_instruments(tmp_path: Path) -> None:
    run = make_run(tmp_path, happy(("EURUSD", "GBPUSD")))
    result = calibrate(run, two_symbol_config(), symbols=None)
    assert_consumed(run)
    assert list(result.layout_file.layout.panels) == ["EURUSD", "GBPUSD"]
    assert {c.name for c in result.checks} == {"account_badge", "panel:EURUSD", "panel:GBPUSD"}
    assert result.ok


def test_unknown_symbol_fails_before_any_question(tmp_path: Path) -> None:
    run = make_run(tmp_path, Script())
    with pytest.raises(CalibrationError, match="unknown symbol"):
        calibrate(run, symbols=["XAUUSD"])
    assert run.prompt.asked == []


def test_live_account_must_be_confirmed_and_warns_on_mode_mismatch(tmp_path: Path) -> None:
    run = make_run(tmp_path, happy(kind="live"))
    cfg = make_config(mode="demo", execution={"venue": "unitrader"})
    result = calibrate(run, cfg)
    assert_consumed(run)
    assert result.layout_file.layout.account_kind == "live"
    assert any("LIVE account" in w and "demo" in w for w in result.warnings)


def test_account_kind_is_asked_until_valid_and_live_can_be_withdrawn(tmp_path: Path) -> None:
    s = Script()
    intro(s)
    pick_window(s)
    s.at(*PARK).area(BADGE)
    s.ask("Which account", "probably demo").ask("Which account", "live").ask("LIVE (real-money)", "n")
    s.ask("Which account", "DEMO")
    no_one_click(s)
    positions(s)
    panel(s, "EURUSD")
    close_all(s)
    tail(s)
    run = make_run(tmp_path, s)
    result = calibrate(run)
    assert_consumed(run)
    assert result.layout_file.layout.account_kind == "demo"
    assert "Please type one of: demo, live" in run.text


def test_one_click_indicator_is_calibrated_and_verified(tmp_path: Path) -> None:
    s = Script()
    intro(s)
    pick_window(s)
    park_badge(s)
    s.ask("one-click trading is switched on", "y").area(((600, 10), (639, 25)))
    positions(s)
    panel(s, "EURUSD")
    close_all(s)
    tail(s)
    run = make_run(tmp_path, s)
    result = calibrate(run)
    assert_consumed(run)
    ref = result.layout_file.layout.one_click_indicator
    assert ref is not None and (ref.region.w, ref.region.h) == (40, 16)
    assert any(c.name == "one_click_indicator" and c.required and c.passed for c in result.checks)


def test_sl_tp_fields_optional_question(tmp_path: Path) -> None:
    s = Script()
    intro(s)
    pick_window(s)
    park_badge(s)
    no_one_click(s)
    positions(s)
    panel(s, "EURUSD", sltp="y")
    close_all(s)
    tail(s)
    run = make_run(tmp_path, s)
    eur = calibrate(run).layout_file.layout.panels["EURUSD"]
    assert_consumed(run)
    assert eur.sl_input is not None and (eur.sl_input.x, eur.sl_input.y) == SLTP["EURUSD"][0]
    assert eur.tp_input is not None and (eur.tp_input.x, eur.tp_input.y) == SLTP["EURUSD"][1]


def test_sl_tp_fields_required_with_panel_fields_protection(tmp_path: Path) -> None:
    s = Script()
    intro(s)
    pick_window(s)
    park_badge(s)
    no_one_click(s)
    positions(s)
    panel(s, "EURUSD", sltp=None)  # no question: the config needs them
    s.at(*SLTP["EURUSD"][0]).at(*SLTP["EURUSD"][1])
    close_all(s)
    tail(s)
    run = make_run(tmp_path, s)
    cfg = make_config(execution={"unitrader": {"protective_orders": "panel_fields"}})
    eur = calibrate(run, cfg).layout_file.layout.panels["EURUSD"]
    assert_consumed(run)
    assert eur.sl_input is not None and eur.tp_input is not None
    assert not any("stop-loss and take-profit" in q for q in run.prompt.asked)
    assert "protective_orders: panel_fields" in run.text


def _open_dialog(run_ref: list[Run], area: tuple[tuple[int, int], tuple[int, int]], seed: int) -> Callable[[], None]:
    def action() -> None:
        backend = run_ref[0].backend
        backend.saved_screen = backend.screen.copy()  # type: ignore[attr-defined]
        x, y, w, h = region_box(area)
        backend.paste(area, noise(w, h, seed))
        popup = window(
            POPUP, title="dialog", client_left=ORIGIN[0] + x, client_top=ORIGIN[1] + y, client_width=w, client_height=h
        )
        backend.stack.insert(0, popup)

    return action


def _close_dialog(run_ref: list[Run]) -> Callable[[], None]:
    def action() -> None:
        backend = run_ref[0].backend
        backend.screen = backend.saved_screen  # type: ignore[attr-defined]
        backend.stack = [w for w in backend.stack if w.handle != POPUP]

    return action


def test_optional_dialogs_and_error_templates(tmp_path: Path) -> None:
    run_ref: list[Run] = []
    s = Script()
    intro(s)
    pick_window(s)
    park_badge(s)
    no_one_click(s)
    positions(s)
    panel(s, "EURUSD")
    # close-all button + its confirmation dialog (the operator opens and closes it themselves)
    s.ask("closes ALL open positions", "y").at(*CLOSE_ALL)
    s.ask("close-all button ask for confirmation", "y")
    s.ask("yourself now", "", _open_dialog(run_ref, DIALOG_AREA, seed=11))
    s.area(DIALOG_AREA).at(*DIALOG_CONFIRM)  # covered by the popup: allowed for dialogs
    s.ask("WITHOUT confirming", "", _close_dialog(run_ref))
    # order dialog
    s.ask("after a BUY/SELL click", "y")
    s.ask("yourself now", "", _open_dialog(run_ref, DIALOG_AREA, seed=12))
    s.area(DIALOG_AREA).at(*DIALOG_CONFIRM)
    s.ask("WITHOUT confirming", "", _close_dialog(run_ref))
    # one error message, then no more
    s.ask("record an error message", "y", _open_dialog(run_ref, ERROR_AREA, seed=13))
    s.area(ERROR_AREA)
    s.ask("Close the error message", "", _close_dialog(run_ref))
    s.ask("another error message", "n")
    s.ask("Save this layout", "y")
    run = make_run(tmp_path, s)
    run_ref.append(run)
    result = calibrate(run)

    assert_consumed(run)
    lay = LayoutFile.load(run.out).layout
    assert lay.close_all_dialog is not None and lay.order_dialog is not None
    assert (lay.order_dialog.confirm_button.x, lay.order_dialog.confirm_button.y) == DIALOG_CONFIRM
    assert len(lay.error_templates) == 1
    by_name = {c.name: c for c in result.checks}
    assert set(by_name) == {"account_badge", "panel:EURUSD", "close_all_dialog", "order_dialog", "error_1"}
    # the dialogs were closed before re-verification: they must NOT match the normal screen now
    for name in ("close_all_dialog", "order_dialog", "error_1"):
        check = by_name[name]
        assert not check.required and not check.matched and check.passed
    assert result.ok
    assert "not on screen, as expected" in run.text
    assert run.text.count("is gone (similarity") == 3  # each one checked right after the operator closed it
    images = {ref.image for ref, _ in template_refs(lay).values()}
    assert len(images) == 5  # unique file per template


def test_no_close_all_button_warns_when_exit_method_needs_it(tmp_path: Path) -> None:
    s = Script()
    intro(s)
    pick_window(s)
    park_badge(s)
    no_one_click(s)
    positions(s)
    panel(s, "EURUSD")
    close_all(s, button=False)
    tail(s)
    run = make_run(tmp_path, s)
    result = calibrate(run)
    assert result.layout_file.layout.close_all_button is None
    assert any("close-all" in w and "by hand" in w for w in result.warnings)
    assert "WARNING" in run.text


def test_custom_title_pattern_and_duplicate_windows(tmp_path: Path) -> None:
    twin = window(TWIN, title=TITLE + " (2)", pid=4343)
    s = Script()
    intro(s)
    s.ask("number of the UniTrader window", "2").ask("belong to the program", "y")
    s.ask("Title pattern", "Terminal.*")  # matches both terminals: rejected, asked again
    s.ask("Title pattern", "(unclosed")  # not a regular expression
    s.ask("Title pattern", "Terminal")  # does not match the WHOLE title
    s.ask("Title pattern", r"Terminal - Demo \d+")  # unique
    park_badge(s)
    no_one_click(s)
    positions(s)
    panel(s, "EURUSD")
    close_all(s)
    tail(s)
    run = make_run(tmp_path, s, windows=[CONSOLE_WINDOW, window(), twin])
    result = calibrate(run)
    assert_consumed(run)
    assert result.layout_file.layout.window_title_regex == r"Terminal - Demo \d+"
    assert "matches more than one window" in run.text
    assert "not a valid regular expression" in run.text
    assert "does not match the whole title" in run.text


def test_process_filter_can_be_declined(tmp_path: Path) -> None:
    s = Script()
    intro(s)
    s.ask("number of the UniTrader window", "2").ask("belong to the program", "n").ask("Title pattern", "")
    park_badge(s)
    no_one_click(s)
    positions(s)
    panel(s, "EURUSD")
    close_all(s)
    tail(s)
    run = make_run(tmp_path, s)
    assert calibrate(run).layout_file.layout.process_name is None


# ------------------------------------------------------------------------------------------- abort paths


def test_operator_declines_login_confirmation(tmp_path: Path) -> None:
    run = make_run(tmp_path, Script().ask("logged in to UniTrader yourself", "n"))
    with pytest.raises(CalibrationAborted, match="Log in to UniTrader yourself"):
        calibrate(run)
    assert_nothing_written(run)


@pytest.mark.parametrize("answer", ["q", "QUIT"])
def test_quit_at_any_question(tmp_path: Path, answer: str) -> None:
    s = intro(Script()).ask("number of the UniTrader window", answer)
    run = make_run(tmp_path, s)
    with pytest.raises(CalibrationAborted, match="nothing was saved"):
        calibrate(run)
    assert_nothing_written(run)


def test_end_of_input_aborts(tmp_path: Path) -> None:
    run = make_run(tmp_path, Script())

    def eof(_question: str) -> str:
        raise EOFError

    with pytest.raises(CalibrationAborted, match="Input ended"):
        run_calibration(make_config(), run.backend, run.out, None, prompt=eof, say=run.lines.append)


def test_no_windows_at_all(tmp_path: Path) -> None:
    run = make_run(tmp_path, intro(Script()), windows=[])
    with pytest.raises(CalibrationError, match="log in to your account yourself"):
        calibrate(run)
    assert_nothing_written(run)


def test_terminal_not_listed(tmp_path: Path) -> None:
    run = make_run(tmp_path, intro(Script()).ask("number of the UniTrader window", "0"))
    with pytest.raises(CalibrationError, match="UniTrader window was not found"):
        calibrate(run)
    assert_nothing_written(run)


def test_invalid_window_numbers_are_asked_again_then_abort(tmp_path: Path) -> None:
    s = intro(Script())
    for answer in ("7", "-1", "two", "", "3"):
        s.ask("number of the UniTrader window", answer)
    run = make_run(tmp_path, s)
    with pytest.raises(CalibrationAborted, match="No valid answer"):
        calibrate(run)


def test_no_pattern_matches_exactly_one_window(tmp_path: Path) -> None:
    twin = window(TWIN, pid=4343)  # identical title and program: impossible to tell apart
    s = intro(Script()).ask("number of the UniTrader window", "2").ask("belong to the program", "y")
    for _ in range(3):
        s.ask("Title pattern", "")
    run = make_run(tmp_path, s, windows=[CONSOLE_WINDOW, window(), twin])
    with pytest.raises(CalibrationError, match="exactly the terminal window"):
        calibrate(run)
    assert "Close duplicate terminal windows" in run.text
    assert_nothing_written(run)


def test_locked_desktop(tmp_path: Path) -> None:
    run = make_run(tmp_path, intro(Script()))
    run.backend.locked = True
    with pytest.raises(CalibrationError, match="locked or disconnected"):
        calibrate(run)


def test_minimized_window_is_refused(tmp_path: Path) -> None:
    s = intro(Script()).ask("number of the UniTrader window", "2")
    run = make_run(tmp_path, s, windows=[CONSOLE_WINDOW, window(minimized=True)])
    with pytest.raises(CalibrationError, match="minimized"):
        calibrate(run)


def test_points_outside_the_client_area_abort_after_retries(tmp_path: Path) -> None:
    s = pick_window(intro(Script()))
    s.at_screen(5, 5).ask("try again", "").at(CLIENT[0], 10).ask("try again", "").at(-1, 10)
    run = make_run(tmp_path, s)
    with pytest.raises(CalibrationError, match="after 3 attempts") as excinfo:
        calibrate(run)
    assert "outside the terminal's client area" in str(excinfo.value)
    assert run.text.count("outside the terminal's client area") == 3
    assert_consumed(run)
    assert_nothing_written(run)


def test_point_outside_then_inside_recovers(tmp_path: Path) -> None:
    s = pick_window(intro(Script()))
    s.at(0, CLIENT[1]).ask("try again", "")  # one pixel below the client area
    park_badge(s)
    no_one_click(s)
    positions(s)
    panel(s, "EURUSD")
    close_all(s)
    tail(s)
    run = make_run(tmp_path, s)
    result = calibrate(run)
    assert_consumed(run)
    assert (result.layout_file.layout.park_point.x, result.layout_file.layout.park_point.y) == PARK


def test_point_covered_by_another_window_is_rejected(tmp_path: Path) -> None:
    s = pick_window(intro(Script()))
    s.at(*PARK).ask("try again", "q")
    run = make_run(tmp_path, s)
    # a window (e.g. this console) on top of the terminal, right where the rest spot is
    cover = window(
        CONSOLE,
        title="Console",
        client_left=to_screen(*PARK)[0] - 10,
        client_top=to_screen(*PARK)[1] - 10,
        client_width=40,
        client_height=40,
    )
    run.backend.stack.insert(0, cover)
    with pytest.raises(CalibrationAborted):
        calibrate(run)
    assert "does not belong to the terminal's main window" in run.text


def test_window_resized_during_calibration(tmp_path: Path) -> None:
    run = make_run(tmp_path, pick_window(intro(Script())).at(*PARK))
    run.backend.window_hook = lambda w: replace(w, client_width=w.client_width + 20)
    with pytest.raises(CalibrationError, match="resized"):
        calibrate(run)
    assert_nothing_written(run)


def test_window_disappears_during_calibration(tmp_path: Path) -> None:
    run = make_run(tmp_path, pick_window(intro(Script())).at(*PARK))
    run.backend.window_hook = lambda _w: None
    with pytest.raises(CalibrationError, match="disappeared"):
        calibrate(run)


def test_operator_declines_to_save(tmp_path: Path) -> None:
    s = Script()
    intro(s)
    pick_window(s)
    park_badge(s)
    no_one_click(s)
    positions(s)
    panel(s, "EURUSD")
    close_all(s)
    tail(s, save="n")
    run = make_run(tmp_path, s)
    with pytest.raises(CalibrationAborted, match="Not saved"):
        calibrate(run)
    assert_nothing_written(run)


def test_fail_safe_aborts(tmp_path: Path) -> None:
    run = make_run(tmp_path, pick_window(intro(Script())))
    run.backend.cursor_error = OperatorAbortError("fail-safe")
    with pytest.raises(CalibrationAborted, match="fail-safe triggered"):
        calibrate(run)
    assert_nothing_written(run)


def test_backend_error_becomes_calibration_error(tmp_path: Path) -> None:
    run = make_run(tmp_path, pick_window(intro(Script())))
    run.backend.cursor_error = BackendError("no input desktop")
    with pytest.raises(CalibrationError, match="desktop automation failed: no input desktop") as excinfo:
        calibrate(run)
    assert not isinstance(excinfo.value, CalibrationAborted)


def test_invalid_arguments(tmp_path: Path) -> None:
    run = make_run(tmp_path, Script())
    with pytest.raises(ValueError, match="countdown"):
        calibrate(run, countdown=-1)
    with pytest.raises(ValueError, match="max_attempts"):
        calibrate(run, max_attempts=0)
    with pytest.raises(ValueError, match="settle_seconds"):
        calibrate(run, settle_seconds=-0.1)


# ------------------------------------------------------------------------------------------- element checks


def _until_badge(s: Script) -> Script:
    return pick_window(intro(s)).at(*PARK)


def _after_badge(s: Script) -> Script:
    s.ask("Which account", "demo")
    no_one_click(s)
    positions(s)
    panel(s, "EURUSD")
    close_all(s)
    return tail(s)


def test_flat_template_is_rejected(tmp_path: Path) -> None:
    flat = ((10, 300), (59, 315))
    s = _until_badge(Script()).area(flat).ask("try again", "").area(BADGE)
    _after_badge(s)
    run = make_run(tmp_path, s)
    x, y, w, h = region_box(flat)
    ImageDraw.Draw(run.backend.screen).rectangle(
        (ORIGIN[0] + x, ORIGIN[1] + y, ORIGIN[0] + x + w - 1, ORIGIN[1] + y + h - 1), fill=(240, 240, 240)
    )
    result = calibrate(run)
    assert_consumed(run)
    assert "one flat colour" in run.text
    badge = result.layout_file.layout.account_badge.region
    assert (badge.x, badge.y) == BADGE[0]


def test_changing_template_area_is_rejected(tmp_path: Path) -> None:
    ticking = ((10, 300), (59, 315))
    s = _until_badge(Script()).area(ticking).ask("try again", "").area(BADGE)
    _after_badge(s)
    run = make_run(tmp_path, s)
    counter = iter(range(1000))
    x, y, _, _ = region_box(ticking)

    def live_prices(left: int, top: int, w: int, h: int, img: Image.Image) -> Image.Image:
        if (left, top) == to_screen(x, y):
            return noise(w, h, seed=100 + next(counter))  # different on every screenshot
        return img

    run.backend.capture_hook = live_prices
    calibrate(run)
    assert_consumed(run)
    assert "changed between two screenshots" in run.text


def test_too_small_or_inverted_region_is_rejected(tmp_path: Path) -> None:
    s = _until_badge(Script()).at(20, 20).at(22, 22).ask("try again", "").at(59, 25).at(10, 10)
    s.ask("try again", "").area(BADGE)
    _after_badge(s)
    run = make_run(tmp_path, s)
    calibrate(run)
    assert_consumed(run)
    assert run.text.count("at least 4 pixels") == 2


def test_rest_spot_inside_a_template_area_is_rejected(tmp_path: Path) -> None:
    s = pick_window(intro(Script())).at(30, 18)  # rest spot inside the badge area
    s.area(BADGE).ask("try again", "q")
    run = make_run(tmp_path, s)
    with pytest.raises(CalibrationAborted):
        calibrate(run)
    assert "rest spot is inside" in run.text


def test_same_spot_for_buy_and_sell_is_rejected(tmp_path: Path) -> None:
    p = PANELS["EURUSD"]
    s = Script()
    intro(s)
    pick_window(s)
    park_badge(s)
    no_one_click(s)
    positions(s)
    s.area(p["label"]).at(*p["lot"]).at(*p["buy"])
    s.at(p["buy"][0] + 1, p["buy"][1]).ask("try again", "").at(*p["sell"])  # SELL hovered on BUY first
    s.ask("stop-loss and take-profit", "n")
    close_all(s)
    tail(s)
    run = make_run(tmp_path, s)
    result = calibrate(run)
    assert_consumed(run)
    assert "same spot as the EURUSD BUY button" in run.text
    assert result.layout_file.layout.panels["EURUSD"].sell_button.x == p["sell"][0]


def test_identical_panel_labels_are_rejected(tmp_path: Path) -> None:
    s = Script()
    intro(s)
    pick_window(s)
    park_badge(s)
    no_one_click(s)
    positions(s)
    panel(s, "EURUSD")
    gbp = PANELS["GBPUSD"]
    s.area(((300, 200), (369, 215))).ask("try again", "")  # an area that looks exactly like EURUSD's label
    s.area(PANELS["EURUSD"]["label"]).ask("try again", "")  # EURUSD's own label again
    s.area(gbp["label"]).at(*gbp["lot"]).at(*gbp["buy"]).at(*gbp["sell"]).ask("stop-loss and take-profit", "n")
    close_all(s)
    tail(s)
    run = make_run(tmp_path, s)
    x, y, w, h = region_box(PANELS["EURUSD"]["label"])
    eur_label = run.backend.screen.crop((ORIGIN[0] + x, ORIGIN[1] + y, ORIGIN[0] + x + w, ORIGIN[1] + y + h))
    run.backend.paste(((300, 200), (369, 215)), eur_label)
    result = calibrate(run, two_symbol_config(), symbols=None)
    assert_consumed(run)
    assert "looks the same as the panel EURUSD label" in run.text
    assert "overlaps the panel EURUSD label area" in run.text
    assert set(result.layout_file.layout.panels) == {"EURUSD", "GBPUSD"}


def test_unstable_positions_indicator_is_rejected(tmp_path: Path) -> None:
    ticking = ((300, 500), (499, 529))
    s = Script()
    intro(s)
    pick_window(s)
    park_badge(s)
    no_one_click(s)
    s.area(ticking).ask("try again", "").area(POSITIONS)
    panel(s, "EURUSD")
    close_all(s)
    tail(s)
    run = make_run(tmp_path, s)
    counter = iter(range(1000))
    x, y, _, _ = region_box(ticking)

    def clock_in_area(left: int, top: int, w: int, h: int, img: Image.Image) -> Image.Image:
        if (left, top) == to_screen(x, y):
            out = img.copy()
            # a single pixel blinking black/white is enough for the area to be refused
            out.putpixel((0, 0), (255, 255, 255) if next(counter) % 2 else (0, 0, 0))
            return out
        return img

    run.backend.capture_hook = clock_in_area
    result = calibrate(run)
    assert_consumed(run)
    assert "changed by itself" in run.text
    pos = result.layout_file.layout.positions_indicator
    assert (pos.x, pos.y) == POSITIONS[0]


# A field/button inside the positions indicator would let the venue count its own typing/click as a fill
# (the baseline is captured before the lot is typed): it must be refused, with a margin.
@pytest.mark.parametrize("spot", [(50, 510), (203, 510)], ids=["inside", "right-next-to"])
def test_click_target_inside_the_positions_indicator_is_rejected(tmp_path: Path, spot: tuple[int, int]) -> None:
    p = PANELS["EURUSD"]
    s = Script()
    intro(s)
    pick_window(s)
    park_badge(s)
    no_one_click(s)
    positions(s)
    s.area(p["label"]).at(*spot).ask("try again", "")  # the lot field hovered in the positions indicator
    s.at(*p["lot"]).at(*p["buy"]).at(*p["sell"]).ask("stop-loss and take-profit", "n")
    close_all(s)
    tail(s)
    run = make_run(tmp_path, s)
    result = calibrate(run)
    assert_consumed(run)
    assert "inside (or right next to) the positions indicator" in run.text
    lot = result.layout_file.layout.panels["EURUSD"].lot_input
    assert (lot.x, lot.y) == p["lot"]


def test_dialog_confirm_button_inside_the_positions_indicator_is_rejected(tmp_path: Path) -> None:
    run_ref: list[Run] = []
    s = Script()
    intro(s)
    pick_window(s)
    park_badge(s)
    no_one_click(s)
    positions(s)
    panel(s, "EURUSD")
    close_all(s, button=False)
    s.ask("after a BUY/SELL click", "y")
    s.ask("yourself now", "", _open_dialog(run_ref, DIALOG_AREA, seed=12))
    s.area(DIALOG_AREA).at(100, 520).ask("try again", "").at(*DIALOG_CONFIRM)
    s.ask("WITHOUT confirming", "", _close_dialog(run_ref))
    s.ask("record an error message", "n").ask("Save this layout", "y")
    run = make_run(tmp_path, s)
    run_ref.append(run)
    result = calibrate(run)
    assert_consumed(run)
    assert "inside (or right next to) the positions indicator" in run.text
    dialog = result.layout_file.layout.order_dialog
    assert dialog is not None and (dialog.confirm_button.x, dialog.confirm_button.y) == DIALOG_CONFIRM


def test_click_targets_and_reference_areas_must_not_overlap(tmp_path: Path) -> None:
    eur, gbp = PANELS["EURUSD"], PANELS["GBPUSD"]
    s = Script()
    intro(s)
    pick_window(s)
    park_badge(s)
    no_one_click(s)
    positions(s)
    panel(s, "EURUSD")
    # GBPUSD label marked over the (already recorded) EURUSD BUY button: refused
    s.area(((140, 130), (209, 145))).ask("try again", "").area(gbp["label"])
    # GBPUSD lot field hovered inside the EURUSD label area: refused
    s.at(60, 65).ask("try again", "").at(*gbp["lot"]).at(*gbp["buy"]).at(*gbp["sell"])
    s.ask("stop-loss and take-profit", "n")
    close_all(s)
    tail(s)
    run = make_run(tmp_path, s)
    result = calibrate(run, two_symbol_config(), symbols=None)
    assert_consumed(run)
    assert f"the EURUSD BUY button ({eur['buy'][0]}, {eur['buy'][1]}) is inside this area" in run.text
    assert "that spot is inside the panel EURUSD label area" in run.text
    lay = result.layout_file.layout
    assert (lay.panels["GBPUSD"].lot_input.x, lay.panels["GBPUSD"].lot_input.y) == gbp["lot"]
    assert result.ok


def test_area_partly_covered_between_the_corners_is_rejected(tmp_path: Path) -> None:
    # A small window over the inside of the badge area that misses its centre and its four corners
    # (the old 5-point probe) but not the venue's 20 px grid.
    cover = window(
        POPUP, title="tooltip", client_left=ORIGIN[0] + 22, client_top=ORIGIN[1] + 8, client_width=9, client_height=7
    )
    holder: list[Run] = []

    def remove_cover() -> None:
        holder[0].backend.stack = [w for w in holder[0].backend.stack if w.handle != POPUP]

    s = _until_badge(Script()).area(BADGE).ask("try again", "", remove_cover).area(BADGE)
    _after_badge(s)
    run = make_run(tmp_path, s)
    holder.append(run)
    run.backend.stack.insert(0, cover)
    result = calibrate(run)
    assert_consumed(run)
    assert "covered by another window" in run.text
    assert result.ok


def _dialog_only_script(s: Script) -> Script:
    intro(s)
    pick_window(s)
    park_badge(s)
    no_one_click(s)
    positions(s)
    panel(s, "EURUSD")
    close_all(s, button=False)
    return s.ask("after a BUY/SELL click", "y")


def test_dialog_template_that_matches_the_closed_screen_is_refused(tmp_path: Path) -> None:
    # The operator marks an area of the terminal instead of the dialog (no dialog is drawn): the "dialog"
    # template would match the normal screen, and the venue would click its confirm point after every order.
    s = _dialog_only_script(Script())
    s.ask("yourself now", "")
    s.area(DIALOG_AREA).at(*DIALOG_CONFIRM)
    s.ask("WITHOUT confirming", "")
    s.ask("check again", "").ask("check again", "")
    run = make_run(tmp_path, s)
    with pytest.raises(CalibrationError, match="mistake the normal screen for it") as excinfo:
        calibrate(run)
    assert not isinstance(excinfo.value, CalibrationAborted)
    assert run.text.count("still looks the same while it should be closed") == 3
    assert_consumed(run)
    assert_nothing_written(run)


def test_dialog_still_open_is_checked_again_after_the_operator_closes_it(tmp_path: Path) -> None:
    run_ref: list[Run] = []
    s = _dialog_only_script(Script())
    s.ask("yourself now", "", _open_dialog(run_ref, DIALOG_AREA, seed=12))
    s.area(DIALOG_AREA).at(*DIALOG_CONFIRM)
    s.ask("WITHOUT confirming", "")  # says it is gone, but the dialog is still open
    s.ask("check again", "", _close_dialog(run_ref))
    s.ask("record an error message", "n").ask("Save this layout", "y")
    run = make_run(tmp_path, s)
    run_ref.append(run)
    result = calibrate(run)
    assert_consumed(run)
    assert run.text.count("still looks the same while it should be closed") == 1
    assert "is gone (similarity" in run.text
    assert result.ok and result.layout_file.layout.order_dialog is not None


def test_error_template_that_still_matches_at_reverification_fails_the_result(tmp_path: Path) -> None:
    run_ref: list[Run] = []
    s = Script()
    intro(s)
    pick_window(s)
    park_badge(s)
    no_one_click(s)
    positions(s)
    panel(s, "EURUSD")
    close_all(s)
    s.ask("after a BUY/SELL click", "n")
    s.ask("record an error message", "y", _open_dialog(run_ref, ERROR_AREA, seed=13))
    s.area(ERROR_AREA)
    s.ask("Close the error message", "", _close_dialog(run_ref))
    s.ask("another error message", "n").ask("Save this layout", "y")
    run = make_run(tmp_path, s)
    run_ref.append(run)
    ex, ey, ew, eh = region_box(ERROR_AREA)

    def error_shows_again_after_save(left: int, top: int, w: int, h: int, img: Image.Image) -> Image.Image:
        if run.out.exists() and (left, top, w, h) == (*to_screen(ex, ey), ew, eh):
            return noise(w, h, seed=13)
        return img

    run.backend.capture_hook = error_shows_again_after_save
    result = calibrate(run)
    assert not result.ok
    assert [c.name for c in result.failed] == ["error_1"]
    assert result.failed[0].matched and not result.failed[0].required
    assert "matches the screen although it should be closed" in run.text
    assert "re-verification FAILED for error_1" in run.text


def test_non_ascii_digits_are_asked_again_instead_of_crashing(tmp_path: Path) -> None:
    s = intro(Script()).ask("number of the UniTrader window", "²").ask("number of the UniTrader window", "q")
    run = make_run(tmp_path, s)
    with pytest.raises(CalibrationAborted, match="Quit"):
        calibrate(run)
    assert "Please type a number from 0 to 2" in run.text


# ------------------------------------------------------------------------------------------- saving


def test_recalibration_replaces_layout_and_removes_old_templates(tmp_path: Path) -> None:
    first = make_run(tmp_path, happy())
    calibrate(first)
    old_images = {
        LayoutFile.load(first.out).template_path(ref)
        for ref, _ in template_refs(LayoutFile.load(first.out).layout).values()
    }
    keep = tmp_path / TEMPLATES_DIR / "operator-notes.png"
    Image.new("RGB", (4, 4), "white").save(keep)

    second = make_run(tmp_path, happy())
    result = calibrate(second)
    new_lf = LayoutFile.load(second.out)
    new_images = {new_lf.template_path(ref) for ref, _ in template_refs(new_lf.layout).values()}
    assert result.ok
    assert new_images.isdisjoint(old_images)
    assert all(p.exists() for p in new_images)
    assert not any(p.exists() for p in old_images)
    assert keep.exists(), "files the old layout did not reference are left alone"


def test_failed_layout_write_keeps_previous_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = make_run(tmp_path, happy())
    calibrate(first)
    before = first.out.read_text(encoding="utf-8")
    files_before = sorted((tmp_path / TEMPLATES_DIR).iterdir())

    def broken_save(self: LayoutFile, path: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(LayoutFile, "save", broken_save)
    second = make_run(tmp_path, happy())
    with pytest.raises(CalibrationError, match="disk full"):
        calibrate(second)
    assert first.out.read_text(encoding="utf-8") == before
    assert sorted((tmp_path / TEMPLATES_DIR).iterdir()) == files_before  # new images cleaned up, old intact
    assert LayoutFile.load(first.out).layout.panels


def test_failed_template_write_leaves_nothing_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = calibrate_mod._write_png_atomically
    calls = {"n": 0}

    def flaky(path: Path, image: Image.Image) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("permission denied")
        real(path, image)

    monkeypatch.setattr(calibrate_mod, "_write_png_atomically", flaky)
    run = make_run(tmp_path, happy())
    with pytest.raises(CalibrationError, match="permission denied"):
        calibrate(run)
    assert_nothing_written(run)


def test_interrupted_layout_write_removes_new_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted_save(self: LayoutFile, path: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(LayoutFile, "save", interrupted_save)
    run = make_run(tmp_path, happy())
    with pytest.raises(KeyboardInterrupt):
        calibrate(run)
    assert_nothing_written(run)


def test_unreadable_previous_layout_is_replaced_without_deleting_anything(tmp_path: Path) -> None:
    out = tmp_path / "unitrader.layout.yaml"
    out.write_text("this: [is not a layout", encoding="utf-8")
    stray = tmp_path / TEMPLATES_DIR / "stray.png"
    stray.parent.mkdir()
    Image.new("RGB", (4, 4), "white").save(stray)
    run = make_run(tmp_path, happy())
    result = calibrate(run)
    assert result.ok
    assert stray.exists()
    assert "already exists and will be replaced" in run.text


def test_reverification_failure_is_reported(tmp_path: Path) -> None:
    run = make_run(tmp_path, happy())
    bx, by, _, _ = region_box(BADGE)

    def badge_changes_after_save(left: int, top: int, w: int, h: int, img: Image.Image) -> Image.Image:
        if run.out.exists() and (left, top) == to_screen(bx, by):
            return noise(w, h, seed=999)  # e.g. the operator logged out right after saving
        return img

    run.backend.capture_hook = badge_changes_after_save
    result = calibrate(run)
    assert not result.ok and not bool(result)
    assert [c.name for c in result.failed] == ["account_badge"]
    assert result.failed[0].score < 0.5
    assert run.out.exists()  # saved, but the operator is told it failed
    assert "re-verification FAILED for account_badge" in run.text


def test_template_names_are_unique_even_when_slugs_collide(tmp_path: Path) -> None:
    # "XX" and "XX_" are both valid symbols and slug to the same text; one label image must not
    # overwrite the other.
    cfg = make_config(instruments=[eurusd(symbol="XX", feed_symbol="X/X"), eurusd(symbol="XX_", feed_symbol="X_/X")])
    run = make_run(tmp_path, happy(("EURUSD", "GBPUSD")))  # the made-up desk's two panel positions
    result = calibrate(run, cfg, symbols=None)
    assert_consumed(run)
    lay = result.layout_file.layout
    assert lay.panels["XX"].label.image != lay.panels["XX_"].label.image
    images = [ref.image for ref, _ in template_refs(lay).values()]
    assert len(images) == len(set(images))
    assert result.ok


def test_written_yaml_is_plain_and_relative(tmp_path: Path) -> None:
    run = make_run(tmp_path, happy())
    calibrate(run)
    raw = yaml.safe_load(run.out.read_text(encoding="utf-8"))
    Layout.model_validate(raw)
    assert raw["account_badge"]["image"].startswith("templates/")
    assert not Path(raw["account_badge"]["image"]).is_absolute()
    assert "password" not in run.out.read_text(encoding="utf-8").lower()


# ------------------------------------------------------------------------------------------- helpers


def test_template_check_and_result_helpers(tmp_path: Path) -> None:
    run = make_run(tmp_path, happy())
    result = calibrate(run)
    failing = CalibrationResult(
        layout_file=result.layout_file,
        path=result.path,
        checks=(
            TemplateCheck("account_badge", 0.5, 0.92, True),
            TemplateCheck("order_dialog", 0.1, 0.92, False),
            TemplateCheck("panel:EURUSD", 0.95, 0.92, True),
        ),
    )
    assert [c.name for c in failing.failed] == ["account_badge"]
    assert not failing.ok and not failing
    assert TemplateCheck("x", 0.92, 0.92, True).passed
    # a dialog/error template passes only when it does NOT match (it was closed); matching is a failure
    assert TemplateCheck("order_dialog", 0.91, 0.92, False).passed
    still_there = TemplateCheck("order_dialog", 0.92, 0.92, False)
    assert still_there.matched and not still_there.passed
    dialog_matches = CalibrationResult(layout_file=result.layout_file, path=result.path, checks=(still_there,))
    assert [c.name for c in dialog_matches.failed] == ["order_dialog"] and not dialog_matches


def test_similarity_used_for_verification_matches_vision() -> None:
    a = noise(30, 20, seed=1)
    assert similarity(a, a.copy()) == pytest.approx(1.0)
