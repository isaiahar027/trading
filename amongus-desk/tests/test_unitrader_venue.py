from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from amongus_desk.clock import ManualClock
from amongus_desk.config import UniTraderConfig
from amongus_desk.domain import ExecutionReport, IntentStatus, Mode, OrderIntent, Position, Quote, Side
from amongus_desk.execution.unitrader.backend import BackendError
from amongus_desk.execution.unitrader.fake import FakeGuiBackend, FakeTerminal, Rect, calibrate_fake
from amongus_desk.execution.unitrader.layout import LayoutFile, Point
from amongus_desk.execution.unitrader.venue import UniTraderVenue
from factories import T0, make_instrument

QUOTE = Quote("EURUSD", 1.09990, 1.10010, T0)
PRE_CLICK_FAILURE_STAGES = {
    "halted", "input_desktop", "account_kind", "locate_window", "client_geometry", "focus", "account_badge",
    "one_click_indicator", "verify_panel_label", "stability", "lot_entry", "protective_fields", "pre_click_verify",
    "write_ahead", "deadline", "validate", "operator_abort", "watchdog_timeout", "closed", "error", "abandoned",
}  # fmt: skip


# ============================================================================================ helpers
class Halt:
    """Kill-switch stub; engaged from the ``from_call``-th call on (or when ``engaged`` is set)."""

    def __init__(self, from_call: int | None = None, engaged: bool = False) -> None:
        self.from_call = from_call
        self.engaged = engaged
        self.calls = 0
        self.threads: set[str] = set()

    def __call__(self) -> bool:
        self.calls += 1
        self.threads.add(threading.current_thread().name)
        return self.engaged or (self.from_call is not None and self.calls >= self.from_call)


class WriteAhead:
    """Records the engine's write-ahead hook: when, on which thread, and what had been clicked by then."""

    def __init__(
        self, terminal: FakeTerminal, *, raises: Exception | None = None, on_call: Callable[[], None] | None = None
    ) -> None:
        self.terminal = terminal
        self.raises = raises
        self.on_call = on_call
        self.calls = 0
        self.thread: str | None = None
        self.order_clicks_seen: list[tuple[str, str]] | None = None
        self.close_all_clicks_seen: int | None = None

    def __call__(self) -> None:
        self.calls += 1
        self.thread = threading.current_thread().name
        self.order_clicks_seen = list(self.terminal.order_clicks)
        self.close_all_clicks_seen = self.terminal.close_all_clicks
        if self.on_call is not None:
            self.on_call()
        if self.raises is not None:
            raise self.raises


class Probe:
    """Wraps a backend and records whether two calls ever overlapped or came from different threads."""

    def __init__(self, inner: FakeGuiBackend) -> None:
        self.inner = inner
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.threads: set[str] = set()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.threads.add(threading.current_thread().name)
            try:
                return attr(*args, **kwargs)
            finally:
                with self.lock:
                    self.active -= 1

        return wrapped


class ExplodingClick(FakeGuiBackend):
    """Raises on a click at ``target`` (the click may or may not have happened on a real desktop)."""

    target: tuple[int, int] | None = None

    def click(self, x: int, y: int) -> None:
        if (x, y) == self.target:
            self._log("click", x, y)
            raise BackendError("simulated input failure")
        super().click(x, y)


class SwallowedClick(FakeGuiBackend):
    """A click at ``target`` is logged but never reaches the terminal (e.g. a dialog button that ignores it)."""

    target: tuple[int, int] | None = None

    def click(self, x: int, y: int) -> None:
        if (x, y) == self.target:
            self._input("click", x, y)
            self.terminal.after_input("click")
            return
        super().click(x, y)


class BlackAfterOrderClick(FakeGuiBackend):
    """Screenshots turn black once an order was clicked while the input desktop still reports available
    (what a minimized remote-desktop session can look like)."""

    def capture(self, left: int, top: int, width: int, height: int) -> Image.Image:
        img = super().capture(left, top, width, height)
        return Image.new("RGB", (width, height), (0, 0, 0)) if self.terminal.order_clicks else img


class GatedHalt(Halt):
    """Kill-switch stub whose first check (step 1, on the GUI worker) blocks until ``gate`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()

    def __call__(self) -> bool:
        self.gate.wait(5)
        return super().__call__()


async def wait_for_event(event: threading.Event) -> None:
    """Wait (without blocking the event loop) until a worker-thread event is set; 5 s safety limit."""
    assert await asyncio.to_thread(event.wait, 5), "the GUI worker never reached the expected point"


@dataclass
class Rig:
    terminal: FakeTerminal
    backend: FakeGuiBackend
    layout: LayoutFile
    clock: ManualClock
    halt: Halt
    venue: UniTraderVenue
    evidence_dir: Path
    cfg: UniTraderConfig
    probe: Probe | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def write_ahead(self, **kwargs: Any) -> WriteAhead:
        return WriteAhead(self.terminal, **kwargs)

    def screen(self, point: Point) -> tuple[int, int]:
        left, top = self.terminal.client_origin
        return left + point.x, top + point.y

    def screen_rect(self, r: Rect) -> tuple[int, int]:
        left, top = self.terminal.client_origin
        cx, cy = r.center
        return left + cx, top + cy

    def typed(self) -> list[str]:
        return [str(a.args[0]) for a in self.backend.input_actions() if a.name == "type_text"]

    def no_trade_click(self) -> bool:
        return self.terminal.order_clicks == [] and self.terminal.close_all_clicks == 0


def intent(
    side: Side | None = Side.BUY,
    lots: str = "0.10",
    symbol: str = "EURUSD",
    mode: Mode = Mode.DEMO,
    **overrides: Any,
) -> OrderIntent:
    data: dict[str, Any] = {
        "purpose": "open",
        "meeting_id": "m1",
        "symbol": symbol,
        "side": side,
        "lots": Decimal(lots),
        "entry_ref": 1.10000,
        "stop_price": 1.09800 if side is Side.BUY else 1.10200,
        "target_price": 1.10400 if side is Side.BUY else 1.09600,
        "mode": mode,
        "created_at": T0,
    }
    data.update(overrides)
    return OrderIntent(**data)


def close_intent(position: Position | None = None, mode: Mode = Mode.DEMO, **overrides: Any) -> OrderIntent:
    data: dict[str, Any] = {
        "purpose": "close",
        "meeting_id": None,
        "position_id": position.id if position else None,
        "symbol": position.symbol if position else "EURUSD",
        "side": position.side.opposite if position else Side.SELL,
        "lots": position.lots if position else Decimal("0.10"),
        "entry_ref": None,
        "stop_price": None,
        "target_price": None,
        "mode": mode,
        "created_at": T0,
    }
    data.update(overrides)
    return OrderIntent(**data)


def close_all_intent(mode: Mode = Mode.DEMO) -> OrderIntent:
    return OrderIntent(
        purpose="close_all", meeting_id=None, symbol="*", side=None, lots=None, entry_ref=None, stop_price=None,
        target_price=None, mode=mode, created_at=T0,
    )  # fmt: skip


def position(side: Side = Side.BUY, lots: str = "0.10", symbol: str = "EURUSD", **overrides: Any) -> Position:
    data: dict[str, Any] = {
        "intent_id": "i0",
        "symbol": symbol,
        "side": side,
        "lots": Decimal(lots),
        "entry_price": 1.1,
        "entry_price_source": "feed_estimate",
        "stop_price": 1.098 if side is Side.BUY else 1.102,
        "target_price": 1.104 if side is Side.BUY else 1.096,
        "opened_at": T0,
        "time_stop_bars": 10,
        "last_price": 1.1005,
    }
    data.update(overrides)
    return Position(**data)


@pytest.fixture
def rigs(tmp_path: Path) -> Iterator[Callable[..., Rig]]:
    created: list[Rig] = []

    def factory(
        *,
        symbols: tuple[str, ...] = ("EURUSD",),
        mode: Mode = Mode.DEMO,
        dry_run: bool = False,
        cfg: dict[str, Any] | None = None,
        terminal: dict[str, Any] | None = None,
        calibrate: dict[str, Any] | None = None,
        halt: Halt | None = None,
        backend_cls: type[FakeGuiBackend] = FakeGuiBackend,
        probe: bool = False,
        hard_timeout: float | None = None,
        layout_hook: Callable[[LayoutFile], LayoutFile] | None = None,
    ) -> Rig:
        base = tmp_path / f"rig{len(created)}"
        term = FakeTerminal(symbols, **(terminal or {}))
        layout = calibrate_fake(term, base / "cal", calibrated_at=T0, **(calibrate or {}))
        if layout_hook is not None:
            layout = layout_hook(layout)
        clock = ManualClock(T0)
        be = backend_cls(term, clock=clock)
        settings: dict[str, Any] = {"backend": "fake", "fill_timeout_seconds": 2.0, "dialog_timeout_seconds": 1.0}
        settings.update(cfg or {})
        ut = UniTraderConfig(**settings)
        instruments = {s: make_instrument(symbol=s, feed_symbol=s) for s in symbols}
        the_probe = Probe(be) if probe else None
        h = halt or Halt()
        venue = UniTraderVenue(
            ut,
            layout,
            the_probe if the_probe is not None else be,
            mode,
            instruments,
            clock,
            h,
            base / "evidence",
            dry_run=dry_run,
            hard_timeout_seconds=hard_timeout,
        )
        rig = Rig(term, be, layout, clock, h, venue, base / "evidence", ut, the_probe)
        created.append(rig)
        return rig

    yield factory
    for rig in created:
        rig.venue._executor.shutdown(wait=True, cancel_futures=True)


def png_size(path: str) -> tuple[int, int]:
    """Size of a saved evidence PNG (sync helper: no blocking file I/O inside the async tests)."""
    with Image.open(path) as img:
        assert img.format == "PNG"
        return img.size


def step_names(report: ExecutionReport) -> list[str]:
    return [s["step"] for s in report.evidence["steps"]]


def assert_aborted_without_click(rig: Rig, report: ExecutionReport, wa: WriteAhead, stage: str) -> None:
    assert report.status is IntentStatus.ABORTED, report.message
    assert report.stage == stage, (report.stage, report.message)
    assert report.clicked_at is None
    assert rig.no_trade_click(), rig.terminal.order_clicks
    assert wa.calls == 0
    assert rig.terminal.orders == []
    assert "nothing was clicked" in report.message


# ============================================================================================ happy paths
@pytest.mark.anyio
@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
async def test_order_is_confirmed(rigs: Callable[..., Rig], side: Side) -> None:
    rig = rigs()
    wa = rig.write_ahead()
    loop_thread = threading.current_thread().name
    report = await rig.venue.submit(intent(side), QUOTE, wa)

    assert report.status is IntentStatus.CONFIRMED, report.message
    assert report.venue == "unitrader" and report.stage == "confirmed"
    assert not report.operator_abort
    # lot typed exactly (and nothing else typed)
    assert rig.typed() == ["0.10"]
    assert [(o.symbol, o.side, o.lot_text) for o in rig.terminal.orders] == [("EURUSD", side.value, "0.10")]
    # the right button, exactly once
    assert rig.terminal.order_clicks == [("EURUSD", side.value)]
    panel = rig.layout.layout.panels["EURUSD"]
    button = panel.buy_button if side is Side.BUY else panel.sell_button
    assert rig.backend.clicks().count(rig.screen(button)) == 1
    other = panel.sell_button if side is Side.BUY else panel.buy_button
    assert rig.screen(other) not in rig.backend.clicks()
    # write-ahead exactly once, on the event loop thread, before the click
    assert wa.calls == 1 and wa.order_clicks_seen == [] and wa.thread == loop_thread
    assert report.clicked_at is not None and report.started_at <= report.clicked_at <= report.finished_at
    # price estimate from the feed quote
    assert report.fill_price == (QUOTE.ask if side is Side.BUY else QUOTE.bid)
    assert report.fill_price_source == "feed_estimate"
    # mouse parked at the calibrated park point, as the last input
    park = rig.screen(rig.layout.layout.park_point)
    last = rig.backend.input_actions()[-1]
    assert (last.name, last.args) == ("move_mouse", park)
    assert rig.terminal.cursor == park
    # evidence
    files = report.evidence["files"]
    for name in ("before", "after"):
        assert Path(files[name]).parent == rig.evidence_dir / report.intent_id
        assert png_size(files[name]) == rig.terminal.client_size
    # all 14 steps recorded, in order
    names = step_names(report)
    expected = [
        "halted", "input_desktop", "account_kind", "window", "client_size", "dpi", "focus", "account_badge",
        "one_click_indicator", "panel:EURUSD", "stability", "lots_typed", "lots_readback", "pre_click_verify",
        "write_ahead", "click", "fill_check", "park_mouse",
    ]  # fmt: skip
    positions = [names.index(n) for n in expected]
    assert positions == sorted(positions), names
    assert report.evidence["fill_check"]["changed"] is True
    # the clipboard holds the operator's content again (here: nothing was there, so the sentinel is not restored)
    report.model_dump_json()  # JSON-safe


@pytest.mark.anyio
async def test_fill_price_falls_back_to_entry_ref_without_quote(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    report = await rig.venue.submit(intent(), None, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED
    assert report.fill_price == pytest.approx(1.1) and report.fill_price_source == "feed_estimate"


@pytest.mark.anyio
async def test_quote_for_another_symbol_is_ignored(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    report = await rig.venue.submit(intent(), Quote("GBPUSD", 1.3, 1.31, T0), rig.write_ahead())
    assert report.fill_price == pytest.approx(1.1)


@pytest.mark.anyio
async def test_comma_decimal_separator(rigs: Callable[..., Rig]) -> None:
    rig = rigs(cfg={"decimal_separator": ","})
    report = await rig.venue.submit(intent(lots="0.25"), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED, report.message
    assert rig.typed() == ["0,25"]
    assert rig.terminal.orders[0].lots == Decimal("0.25")


@pytest.mark.anyio
async def test_second_panel_is_used_for_its_symbol(rigs: Callable[..., Rig]) -> None:
    rig = rigs(symbols=("EURUSD", "GBPUSD"))
    report = await rig.venue.submit(intent(Side.SELL, symbol="GBPUSD"), None, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED, report.message
    assert rig.terminal.order_clicks == [("GBPUSD", "SELL")]
    assert rig.terminal.field_text("EURUSD") == "0.01"  # the other panel was not touched
    assert rig.terminal.field_text("GBPUSD") == "0.10"


@pytest.mark.anyio
async def test_minimized_window_is_restored_not_resized(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.minimized = True
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED, report.message
    assert any(a.name == "restore_window" for a in rig.backend.actions)
    assert "restore" in step_names(report)


@pytest.mark.anyio
async def test_operator_clipboard_is_restored(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.clipboard = "operator's notes"
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED
    assert rig.terminal.clipboard == "operator's notes"


@pytest.mark.anyio
async def test_focus_is_retried(rigs: Callable[..., Rig]) -> None:
    rig = rigs(cfg={"focus_retries": 3})
    rig.terminal.steal_focus()
    attempts: list[int] = []
    original = rig.backend.activate_window

    def flaky(handle: int) -> bool:
        attempts.append(handle)
        return original(handle) if len(attempts) >= 2 else False

    rig.backend.activate_window = flaky  # type: ignore[method-assign]
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED, report.message
    assert len(attempts) == 2


@pytest.mark.anyio
async def test_no_evidence_when_disabled(rigs: Callable[..., Rig]) -> None:
    rig = rigs(cfg={"save_evidence": False})
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED
    assert "files" not in report.evidence
    assert not rig.evidence_dir.exists()


@pytest.mark.anyio
async def test_readback_can_be_disabled_by_the_operator(rigs: Callable[..., Rig]) -> None:
    rig = rigs(cfg={"lot_readback": "none", "commit_key": "none"})
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED
    assert not any(a.name == "hotkey" and a.args == ("ctrl", "c") for a in rig.backend.actions)
    assert not any(a.name == "press" for a in rig.backend.actions)


# ============================================================================================ aborts before the click
@pytest.mark.anyio
async def test_halted_aborts_before_any_gui_action(rigs: Callable[..., Rig]) -> None:
    rig = rigs(halt=Halt(engaged=True))
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "halted")
    assert rig.backend.actions == []
    assert "kill switch" in report.message


@pytest.mark.anyio
async def test_locked_desktop(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.locked = True
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "input_desktop")
    assert "session locked/disconnected" in report.message
    assert rig.backend.input_actions() == []


@pytest.mark.anyio
async def test_window_missing_tells_the_operator_to_log_in(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.window_present = False
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "locate_window")
    assert "log in to your DEMO account yourself" in report.message
    assert rig.backend.input_actions() == []


@pytest.mark.anyio
async def test_duplicate_windows(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.duplicate_windows = 1
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "locate_window")
    assert "close duplicate windows" in report.message


@pytest.mark.anyio
async def test_process_mismatch(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.process_name = "impostor.exe"
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "locate_window")
    assert "impostor.exe" in report.message and "log in to your DEMO account yourself" in report.message


@pytest.mark.anyio
async def test_same_title_from_another_process_is_ignored(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    real = rig.terminal.windows()[0]
    rig.terminal.extra_windows = [type(real)(0x9999, real.title, 1, "browser.exe", 0, 0, 800, 600, False, True, 96)]
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED, report.message


@pytest.mark.anyio
async def test_title_must_match_fully(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.title = rig.terminal.title + " - review in a browser tab"
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "locate_window")


@pytest.mark.anyio
@pytest.mark.parametrize(("knob", "value"), [("client_size", (1000, 600)), ("dpi", 120)])
async def test_client_size_or_dpi_mismatch(rigs: Callable[..., Rig], knob: str, value: Any) -> None:
    rig = rigs()
    setattr(rig.terminal, knob, value)
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "client_geometry")
    assert "calibrated" in report.message
    assert rig.backend.input_actions() == []  # never resized, never clicked


@pytest.mark.anyio
async def test_focus_cannot_be_obtained(rigs: Callable[..., Rig]) -> None:
    rig = rigs(cfg={"focus_retries": 3})
    rig.terminal.steal_focus()
    rig.terminal.refuse_focus = True
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "focus")
    assert sum(1 for a in rig.backend.actions if a.name == "activate_window") == 3
    assert rig.backend.input_actions() == []


@pytest.mark.anyio
async def test_badge_mismatch(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.badge = "LIVE"
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "account_badge")
    assert "wrong account / not logged in" in report.message
    assert "Log in to your DEMO account in UniTrader yourself" in report.message


@pytest.mark.anyio
async def test_badge_colour_change_is_caught_even_when_the_shape_matches(rigs: Callable[..., Rig]) -> None:
    def recolour(lf: LayoutFile) -> LayoutFile:
        ref = lf.layout.account_badge
        path = lf.template_path(ref)
        with Image.open(path) as img:
            shifted = img.convert("RGB").point(lambda v: min(255, v + 60))
        shifted.save(path)
        return lf

    rig = rigs(layout_hook=recolour)
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "account_badge")
    assert "colours differ" in report.message


@pytest.mark.anyio
async def test_live_mode_with_a_demo_layout(rigs: Callable[..., Rig]) -> None:
    rig = rigs(mode=Mode.LIVE)  # badge on screen matches the DEMO calibration
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(mode=Mode.LIVE), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "account_kind")
    assert "calibrated on a DEMO account" in report.message
    assert rig.backend.input_actions() == []


@pytest.mark.anyio
async def test_demo_mode_with_a_live_layout(rigs: Callable[..., Rig]) -> None:
    rig = rigs(terminal={"badge": "LIVE"})
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "account_kind")
    assert "Log in to your DEMO account in UniTrader yourself" in report.message


@pytest.mark.anyio
async def test_live_mode_with_a_live_layout_trades(rigs: Callable[..., Rig]) -> None:
    rig = rigs(mode=Mode.LIVE, terminal={"badge": "LIVE"})
    report = await rig.venue.submit(intent(mode=Mode.LIVE), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED, report.message


@pytest.mark.anyio
async def test_one_click_indicator_mismatch(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.one_click_enabled = False
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "one_click_indicator")


@pytest.mark.anyio
async def test_swapped_panels(rigs: Callable[..., Rig]) -> None:
    rig = rigs(symbols=("EURUSD", "GBPUSD"))
    rig.terminal.swap_panels = True
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "verify_panel_label")
    assert "EURUSD panel label does not match" in report.message
    assert rig.backend.input_actions() == []


@pytest.mark.anyio
async def test_popup_covering_the_panel_label(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    label = rig.terminal.geometry().panels["EURUSD"].label
    rig.terminal.popup = Rect(label.x, label.y, 80, 20)
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "verify_panel_label")


@pytest.mark.anyio
async def test_popup_covering_the_button(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    buy = rig.terminal.geometry().panels["EURUSD"].buy_button
    rig.terminal.popup = Rect(buy.x + 20, buy.y + 5, 60, 30)
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "pre_click_verify")
    assert "on top of the BUY button" in report.message
    assert rig.typed() == ["0.10"]  # typed, verified, but the click was refused
    assert rig.terminal.cursor == rig.screen(rig.layout.layout.park_point)  # still parked


@pytest.mark.anyio
async def test_unstable_positions_indicator(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.indicator_flicker = True
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "stability")
    assert rig.backend.input_actions() == []


@pytest.mark.anyio
async def test_stability_check_can_be_disabled(rigs: Callable[..., Rig]) -> None:
    rig = rigs(cfg={"stability_check": False})
    rig.terminal.indicator_flicker = True
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED, report.message  # the operator's choice is honoured


@pytest.mark.anyio
async def test_covered_positions_indicator(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    ind = rig.terminal.geometry().indicator
    rig.terminal.popup = Rect(ind.x + ind.w - 40, ind.y + ind.h - 30, 60, 50)  # covers a corner only
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "stability")


@pytest.mark.anyio
async def test_lot_readback_mismatch(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.lot_field_transform = lambda text: "0.1" + text[-1:] + "5"  # the field "rounds" what we typed
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "lot_entry")
    assert "read-back mismatch" in report.message


@pytest.mark.anyio
async def test_lot_field_ignoring_select_all(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.lot_field_ignores_select_all = True
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "lot_entry")
    assert rig.terminal.field_text("EURUSD") == "0.010.10"  # garbage stayed in the field, never sent


class ForeignClipboardWriter(FakeGuiBackend):
    """Another program (password manager, clipboard history) replaces the clipboard right before we read it."""

    def get_clipboard_text(self) -> str | None:
        if any(a.name == "hotkey" and a.args == ("ctrl", "c") for a in self.actions):
            self.terminal.clipboard = "hunter2-Secret!"
        return super().get_clipboard_text()


@pytest.mark.anyio
async def test_foreign_clipboard_text_is_never_echoed(rigs: Callable[..., Rig]) -> None:
    rig = rigs(backend_cls=ForeignClipboardWriter)
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "lot_entry")
    assert "not shown" in report.message
    assert "hunter2" not in report.model_dump_json()  # not in the message, the steps or any evidence


@pytest.mark.anyio
async def test_clipboard_sentinel_unchanged(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.clipboard_copy_broken = True
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "lot_entry")
    assert "sentinel" in report.message


@pytest.mark.anyio
async def test_halted_right_before_the_click(rigs: Callable[..., Rig]) -> None:
    rig = rigs(halt=Halt(from_call=2))  # 1st check (step 1) passes, 2nd check (step 10) is engaged
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "halted")
    assert rig.typed() == ["0.10"]
    assert rig.halt.threads == {"unitrader-gui_0"}  # the kill switch is read on the GUI thread


@pytest.mark.anyio
async def test_halted_during_the_write_ahead(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    wa = rig.write_ahead(on_call=lambda: setattr(rig.halt, "engaged", True))
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert report.status is IntentStatus.ABORTED and report.stage == "halted"
    assert wa.calls == 1
    assert rig.no_trade_click()


@pytest.mark.anyio
async def test_kill_switch_errors_count_as_engaged(rigs: Callable[..., Rig]) -> None:
    def broken() -> bool:
        raise OSError("cannot stat the kill file")

    rig = rigs()
    rig.venue._is_halted = broken
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "halted")


@pytest.mark.anyio
async def test_focus_stolen_after_typing(rigs: Callable[..., Rig]) -> None:
    rig = rigs()

    def hook(term: FakeTerminal, name: str) -> None:
        if name == "press":  # the commit key after the lot entry
            term.steal_focus()

    rig.terminal.on_action = hook
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "pre_click_verify")
    assert "lost the foreground" in report.message


@pytest.mark.anyio
async def test_window_moved_after_the_checks(rigs: Callable[..., Rig]) -> None:
    rig = rigs()

    def hook(term: FakeTerminal, name: str) -> None:
        if name == "press":
            term.client_origin = (term.client_origin[0] + 15, term.client_origin[1])

    rig.terminal.on_action = hook
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "pre_click_verify")
    assert "moved" in report.message


@pytest.mark.anyio
async def test_panel_swapped_after_typing(rigs: Callable[..., Rig]) -> None:
    rig = rigs(symbols=("EURUSD", "GBPUSD"))

    def hook(term: FakeTerminal, name: str) -> None:
        if name == "press":
            term.swap_panels = True

    rig.terminal.on_action = hook
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "pre_click_verify")


@pytest.mark.anyio
@pytest.mark.parametrize("stability_check", [True, False])
async def test_indicator_change_between_baseline_and_click_aborts(
    rigs: Callable[..., Rig], stability_check: bool
) -> None:
    # Something else (another position, a stop at the broker) changes the indicator while the lot is typed:
    # a later "change" would prove nothing about our order, so the click must not happen.
    rig = rigs(cfg={"stability_check": stability_check})
    rig.terminal.fill_enabled = False  # our order would never fill: a click here could only be a false CONFIRMED

    def hook(term: FakeTerminal, name: str) -> None:
        if name == "press" and not term.positions:
            term.open_position("EURUSD", "SELL", Decimal("0.05"))

    rig.terminal.on_action = hook
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "pre_click_verify")
    assert "between its baseline and the click" in report.message


@pytest.mark.anyio
async def test_stale_confirmation_dialog_is_never_confirmed(rigs: Callable[..., Rig]) -> None:
    # A confirmation left open by an earlier order would swallow our click, and confirming it would execute
    # THAT order (here 0.01 lots) while we report ours (0.10) as CONFIRMED.
    rig = rigs(terminal={"order_confirm_dialog": True}, cfg={"lot_readback": "none"})
    rig.terminal.show_dialog("order_confirm", ("EURUSD", "BUY"))
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "pre_click_verify")
    assert "order confirmation dialog is already showing" in report.message
    assert rig.terminal.dialog == "order_confirm" and rig.terminal.dialog_confirms == 0
    buy = rig.screen(rig.layout.layout.panels["EURUSD"].buy_button)
    assert buy not in rig.backend.clicks()


@pytest.mark.anyio
async def test_stale_error_message_aborts_before_the_click(rigs: Callable[..., Rig]) -> None:
    # Otherwise the old error would be read as a rejection of the new order (a false REJECTED).
    rig = rigs(cfg={"lot_readback": "none"})
    rig.terminal.show_dialog("error")
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "pre_click_verify")
    assert "error message" in report.message
    assert not any(a.name == "press" and a.args == ("esc",) for a in rig.backend.actions)  # never dismissed


@pytest.mark.anyio
async def test_stale_dialog_blocks_close_all_too(rigs: Callable[..., Rig]) -> None:
    rig = rigs(terminal={"close_all_confirm_dialog": True})
    rig.terminal.open_position("EURUSD", "BUY", Decimal("0.10"))
    rig.terminal.show_dialog("close_all_confirm")
    wa = rig.write_ahead()
    report = await rig.venue.close_all(close_all_intent(), wa)
    assert_aborted_without_click(rig, report, wa, "pre_click_verify")
    assert len(rig.terminal.positions) == 1


@pytest.mark.anyio
async def test_slow_write_ahead_never_clicks_past_the_budget(rigs: Callable[..., Rig]) -> None:
    rig = rigs(cfg={"max_order_seconds": 10.0, "fill_timeout_seconds": 2.0})
    wa = rig.write_ahead(on_call=lambda: rig.clock.advance(9.0))  # e.g. a slow disk under the store
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert report.status is IntentStatus.ABORTED and report.stage == "deadline", report.message
    assert "write-ahead took too long" in report.message
    assert wa.calls == 1 and report.clicked_at is None and rig.no_trade_click()


@pytest.mark.anyio
async def test_write_ahead_raising_aborts_without_click(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    wa = rig.write_ahead(raises=RuntimeError("database is locked"))
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert report.status is IntentStatus.ABORTED and report.stage == "write_ahead"
    assert "database is locked" in report.message
    assert wa.calls == 1
    assert rig.no_trade_click()
    assert report.clicked_at is None


@pytest.mark.anyio
async def test_operator_failsafe_before_the_click(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.failsafe_after_actions = 2  # fires during the lot entry
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert report.status is IntentStatus.ABORTED and report.stage == "operator_abort"
    assert report.operator_abort is True
    assert rig.no_trade_click() and wa.calls == 0


@pytest.mark.anyio
async def test_unexpected_error_before_the_click_aborts(rigs: Callable[..., Rig]) -> None:
    rig = rigs()

    def boom(*_: int) -> None:
        raise RuntimeError("screen grab failed")

    rig.backend.capture_hook = boom
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert report.status is IntentStatus.ABORTED and report.stage == "error"
    assert rig.no_trade_click() and wa.calls == 0


@pytest.mark.anyio
async def test_missing_template_file_aborts(rigs: Callable[..., Rig]) -> None:
    def drop(lf: LayoutFile) -> LayoutFile:
        lf.template_path(lf.layout.panels["EURUSD"].label).unlink()
        return lf

    rig = rigs(layout_hook=drop)
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "verify_panel_label")
    assert "re-run desk calibrate" in report.message


@pytest.mark.anyio
async def test_blank_template_never_matches(rigs: Callable[..., Rig]) -> None:
    def blank(lf: LayoutFile) -> LayoutFile:
        ref = lf.layout.account_badge
        Image.new("RGB", (ref.region.w, ref.region.h), (0, 0, 0)).save(lf.template_path(ref))
        return lf

    rig = rigs(layout_hook=blank)
    rig.terminal.locked = False
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "account_badge")
    assert "blank" in report.message


# ============================================================================================ validation
@pytest.mark.anyio
@pytest.mark.parametrize(
    ("make", "fragment"),
    [
        (lambda: intent(purpose="close"), "expected a 'open' intent"),
        (lambda: intent(mode=Mode.LIVE), "intent was created for mode 'live'"),
        (lambda: intent(symbol="USDJPY"), "unknown instrument"),
        (lambda: intent(lots="0.015"), "lot step"),
        (lambda: intent(lots="0.60"), "outside"),
        (lambda: intent(lots="0"), "invalid lot size"),
        (lambda: intent(side=None), "needs a side"),
    ],
)
async def test_invalid_intents_abort_without_touching_the_gui(
    rigs: Callable[..., Rig], make: Callable[[], OrderIntent], fragment: str
) -> None:
    rig = rigs()
    wa = rig.write_ahead()
    report = await rig.venue.submit(make(), QUOTE, wa)
    assert report.status is IntentStatus.ABORTED and report.stage == "validate"
    assert fragment in report.message
    assert rig.backend.actions == [] and wa.calls == 0


@pytest.mark.anyio
async def test_instrument_without_panel(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.venue._instruments["GBPUSD"] = make_instrument(symbol="GBPUSD", feed_symbol="GBP/USD")
    report = await rig.venue.submit(intent(symbol="GBPUSD"), None, rig.write_ahead())
    assert report.status is IntentStatus.ABORTED and "no calibrated panel" in report.message
    assert rig.backend.actions == []


@pytest.mark.anyio
async def test_disabled_instrument(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.venue._instruments["EURUSD"] = make_instrument(enabled=False)
    report = await rig.venue.submit(intent(), None, rig.write_ahead())
    assert report.status is IntentStatus.ABORTED and "disabled" in report.message


@pytest.mark.anyio
async def test_paper_mode_never_uses_the_terminal(rigs: Callable[..., Rig]) -> None:
    rig = rigs(mode=Mode.PAPER)
    report = await rig.venue.submit(intent(mode=Mode.PAPER), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.ABORTED and "does not use the UniTrader terminal" in report.message
    health = await rig.venue.health()
    assert not health.ok
    assert rig.backend.actions == []


# ============================================================================================ dry run
@pytest.mark.anyio
async def test_dry_run_types_nothing_and_clicks_nothing(rigs: Callable[..., Rig]) -> None:
    rig = rigs(dry_run=True)
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert report.status is IntentStatus.DRY_RUN and report.stage == "dry_run"
    assert rig.backend.input_actions() == []
    assert rig.terminal.field_text("EURUSD") == "0.01"
    assert wa.calls == 0 and rig.no_trade_click()
    assert report.clicked_at is None
    assert "panel:EURUSD" in step_names(report) and "stability" in step_names(report)
    assert rig.venue.dry_run


@pytest.mark.anyio
async def test_dry_run_still_reports_failed_checks(rigs: Callable[..., Rig]) -> None:
    rig = rigs(dry_run=True)
    rig.terminal.badge = "LIVE"
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.ABORTED and report.stage == "account_badge"


# ============================================================================================ after the click
@pytest.mark.anyio
async def test_no_indicator_change_is_unconfirmed(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.fill_enabled = False
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert report.status is IntentStatus.UNCONFIRMED and report.stage == "fill"
    assert "reconcile" in report.message
    assert report.clicked_at is not None and wa.calls == 1
    assert rig.terminal.order_clicks == [("EURUSD", "BUY")]  # clicked once, never retried
    assert report.fill_price is None
    assert rig.terminal.cursor == rig.screen(rig.layout.layout.park_point)
    assert png_size(report.evidence["files"]["after"]) == rig.terminal.client_size
    # bounded by fill_timeout_seconds (virtual time)
    assert report.clicked_at is not None
    assert (report.finished_at - report.clicked_at).total_seconds() <= rig.cfg.fill_timeout_seconds + 0.5


@pytest.mark.anyio
async def test_delayed_fill_within_the_timeout_is_confirmed(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.fill_delay_seconds = 1.0
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED, report.message


@pytest.mark.anyio
async def test_fill_after_the_timeout_is_unconfirmed(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.fill_delay_seconds = 5.0
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.UNCONFIRMED


@pytest.mark.anyio
async def test_error_dialog_is_rejected(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.order_error = True
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert report.status is IntentStatus.REJECTED and report.stage == "rejected"
    assert any(a.name == "press" and a.args == ("esc",) for a in rig.backend.actions)
    assert rig.terminal.dialog is None
    assert rig.terminal.order_clicks == [("EURUSD", "BUY")]
    assert report.clicked_at is not None and wa.calls == 1


@pytest.mark.anyio
async def test_error_dialog_plus_indicator_change_is_unconfirmed(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.order_error = True

    def hook(term: FakeTerminal, name: str) -> None:
        if name == "click" and term.order_clicks and not term.positions:
            term.open_position("EURUSD", "BUY", Decimal("0.10"))  # contradictory evidence

    rig.terminal.on_action = hook
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.UNCONFIRMED


@pytest.mark.anyio
async def test_confirmation_dialog_is_handled(rigs: Callable[..., Rig]) -> None:
    rig = rigs(terminal={"order_confirm_dialog": True})
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED, report.message
    assert rig.terminal.dialog_confirms == 1
    assert rig.screen(rig.layout.layout.order_dialog.confirm_button) in rig.backend.clicks()  # type: ignore[union-attr]
    assert report.evidence["fill_check"]["dialog"] == "confirmed"


@pytest.mark.anyio
async def test_confirmation_dialog_that_was_not_calibrated_is_unconfirmed(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.order_confirm_dialog = True  # appears, but the layout knows nothing about it
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.UNCONFIRMED
    assert rig.terminal.dialog == "order_confirm"  # left alone: never clicked blindly


@pytest.mark.anyio
async def test_calibrated_dialog_that_does_not_appear(rigs: Callable[..., Rig]) -> None:
    rig = rigs(terminal={"order_confirm_dialog": True})
    rig.terminal.order_confirm_dialog = False
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED, report.message
    assert rig.terminal.dialog_confirms == 0


@pytest.mark.anyio
async def test_operator_failsafe_after_the_click(rigs: Callable[..., Rig]) -> None:
    rig = rigs(terminal={"order_confirm_dialog": True})
    rig.terminal.failsafe_after_order_click = True  # the dialog confirm click raises
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.UNCONFIRMED and report.operator_abort is True
    assert report.clicked_at is not None


@pytest.mark.anyio
async def test_operator_failsafe_while_parking_after_a_fill(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.failsafe_after_order_click = True  # only the park move raises; the fill is seen
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.UNCONFIRMED and report.operator_abort is True
    assert len(rig.terminal.positions) == 1


@pytest.mark.anyio
async def test_click_that_raises_is_unconfirmed(rigs: Callable[..., Rig]) -> None:
    rig = rigs(backend_cls=ExplodingClick)
    assert isinstance(rig.backend, ExplodingClick)
    rig.backend.target = rig.screen(rig.layout.layout.panels["EURUSD"].buy_button)
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert report.status is IntentStatus.UNCONFIRMED and report.clicked_at is not None
    assert wa.calls == 1


@pytest.mark.anyio
async def test_error_after_the_click_is_unconfirmed(rigs: Callable[..., Rig]) -> None:
    rig = rigs()

    def boom(*_: int) -> None:
        if rig.terminal.order_clicks:
            raise RuntimeError("capture failed")

    rig.backend.capture_hook = boom
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.UNCONFIRMED and report.stage == "error"


@pytest.mark.anyio
async def test_desktop_locking_after_the_click_never_confirms(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.fill_enabled = False

    def hook(term: FakeTerminal, name: str) -> None:
        if name == "click" and term.order_clicks:
            term.locked = True  # a black screen must not look like "the indicator changed"

    rig.terminal.on_action = hook
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.UNCONFIRMED


@pytest.mark.anyio
async def test_popup_over_the_indicator_after_the_click_never_confirms(rigs: Callable[..., Rig]) -> None:
    rig = rigs()

    def hook(term: FakeTerminal, name: str) -> None:
        if name == "click" and term.order_clicks and term.popup is None:
            ind = term.geometry().indicator
            term.popup = Rect(ind.x + 10, ind.y + 10, 200, 60)

    rig.terminal.on_action = hook
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.UNCONFIRMED


@pytest.mark.anyio
async def test_black_capture_after_the_click_never_confirms(rigs: Callable[..., Rig]) -> None:
    # A black screenshot differs from the baseline in every pixel; it must never read as "the order filled".
    rig = rigs(backend_cls=BlackAfterOrderClick)
    rig.terminal.fill_enabled = False
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.UNCONFIRMED and report.stage == "fill", report.message
    assert rig.terminal.orders == []
    check = report.evidence["fill_check"]
    assert check["changed"] is False and check["samples"] == 0 and check["skipped"] > 0
    assert "could not be checked" in report.message


@pytest.mark.anyio
async def test_confirmation_dialog_still_showing_never_confirms(rigs: Callable[..., Rig]) -> None:
    # The simulated dialog overlaps the positions indicator in a 960x400 window (~1.4% of its pixels). If the
    # confirm click does not dismiss it, the dialog itself "changes" the indicator beyond a 1% threshold; that
    # must not count as a fill.
    rig = rigs(
        terminal={"order_confirm_dialog": True, "client_size": (960, 400)},
        cfg={"change_threshold": 0.01},
        backend_cls=SwallowedClick,
    )
    dialog = rig.layout.layout.order_dialog
    assert dialog is not None and isinstance(rig.backend, SwallowedClick)
    geo = rig.terminal.geometry()
    assert geo.dialog.overlaps(geo.indicator)  # the scenario needs the overlap
    rig.backend.target = rig.screen(dialog.confirm_button)
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.UNCONFIRMED, report.message
    assert rig.terminal.orders == [] and rig.terminal.dialog == "order_confirm"
    assert rig.backend.clicks().count(rig.screen(dialog.confirm_button)) == 1  # tried once, never again


@pytest.mark.anyio
async def test_confirmation_dialog_over_the_indicator_still_confirms_a_real_fill(rigs: Callable[..., Rig]) -> None:
    rig = rigs(terminal={"order_confirm_dialog": True, "client_size": (960, 400)}, cfg={"change_threshold": 0.01})
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED, report.message
    assert len(rig.terminal.orders) == 1 and rig.terminal.dialog_confirms == 1


@pytest.mark.anyio
async def test_after_click_reports_are_never_aborted(rigs: Callable[..., Rig]) -> None:
    scenarios: list[Callable[[FakeTerminal], None]] = [
        lambda t: setattr(t, "fill_enabled", False),
        lambda t: setattr(t, "order_error", True),
        lambda t: setattr(t, "failsafe_after_order_click", True),
        lambda t: setattr(t, "order_confirm_dialog", True),
    ]
    for setup in scenarios:
        rig = rigs()
        setup(rig.terminal)
        report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
        assert rig.terminal.order_clicks
        assert report.status in (IntentStatus.CONFIRMED, IntentStatus.REJECTED, IntentStatus.UNCONFIRMED)


@pytest.mark.anyio
async def test_internal_crash_after_the_click_is_still_unconfirmed(
    rigs: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = rigs()

    def broken(*_: Any) -> ExecutionReport:
        raise RuntimeError("bug while building the report")

    monkeypatch.setattr(rig.venue, "_final_report", broken)
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert rig.terminal.order_clicks == [("EURUSD", "BUY")]
    assert report.status is IntentStatus.UNCONFIRMED and report.stage == "error"


@pytest.mark.anyio
async def test_internal_crash_before_the_click_is_aborted(
    rigs: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = rigs(dry_run=True)

    def broken(*_: Any) -> ExecutionReport:
        raise RuntimeError("bug while building the report")

    monkeypatch.setattr(rig.venue, "_final_report", broken)
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert rig.no_trade_click()
    assert report.status is IntentStatus.ABORTED and report.stage == "error"


@pytest.mark.anyio
async def test_aborted_flow_keeps_a_snapshot_for_the_operator(rigs: Callable[..., Rig]) -> None:
    rig = rigs(symbols=("EURUSD", "GBPUSD"))
    rig.terminal.swap_panels = True
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.ABORTED
    assert set(report.evidence["files"]) == {"snapshot"}
    assert png_size(report.evidence["files"]["snapshot"]) == rig.terminal.client_size


@pytest.mark.anyio
async def test_evidence_folder_name_is_sanitised(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    report = await rig.venue.submit(intent(id="../../etc/passwd"), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED
    folder = Path(report.evidence["files"]["after"]).parent
    assert folder.parent == rig.evidence_dir and ".." not in folder.name and "/" not in folder.name


# ============================================================================================ time bounds
@pytest.mark.anyio
async def test_max_order_seconds_before_the_click(rigs: Callable[..., Rig]) -> None:
    rig = rigs(cfg={"max_order_seconds": 5.0})
    rig.backend.capture_hook = lambda *_: rig.clock.advance(2.0)  # every screenshot "takes" 2 s
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "deadline")


@pytest.mark.anyio
async def test_no_click_without_enough_time_left_to_confirm(rigs: Callable[..., Rig]) -> None:
    rig = rigs(cfg={"max_order_seconds": 10.0, "fill_timeout_seconds": 8.0})
    rig.backend.capture_hook = lambda *_: rig.clock.advance(0.5)
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "deadline")
    assert "left" in report.message


@pytest.mark.anyio
async def test_max_order_seconds_bounds_the_whole_flow(rigs: Callable[..., Rig]) -> None:
    rig = rigs(cfg={"max_order_seconds": 6.0, "fill_timeout_seconds": 4.0})
    rig.terminal.fill_enabled = False
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.UNCONFIRMED
    assert (report.finished_at - report.started_at).total_seconds() <= 6.0 + 0.5


@pytest.mark.anyio
async def test_queue_time_counts_against_max_order_seconds(rigs: Callable[..., Rig]) -> None:
    halt = GatedHalt()  # holds the worker until BOTH submits have taken their start time (no race on the clock)
    rig = rigs(cfg={"max_order_seconds": 3.0, "fill_timeout_seconds": 1.0}, halt=halt)
    rig.terminal.fill_enabled = False
    tasks = [
        asyncio.create_task(rig.venue.submit(intent(), QUOTE, rig.write_ahead())),
        asyncio.create_task(rig.venue.submit(intent(Side.SELL), QUOTE, rig.write_ahead())),
    ]
    await asyncio.sleep(0)  # both coroutines run up to their first await: deadlines = T0 + 3 s
    assert rig.clock.monotonic() == 0.0
    halt.gate.set()
    first, second = await asyncio.gather(*tasks)
    assert first.status is IntentStatus.UNCONFIRMED
    assert second.status is IntentStatus.ABORTED and second.stage == "deadline"
    assert rig.terminal.order_clicks == [("EURUSD", "BUY")]


@pytest.mark.anyio
async def test_stuck_worker_before_the_click_is_aborted_and_never_clicks_later(rigs: Callable[..., Rig]) -> None:
    rig = rigs(hard_timeout=0.05)
    gate = threading.Event()
    rig.backend.capture_hook = lambda *_: gate.wait(5)
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert report.status is IntentStatus.ABORTED and report.stage == "watchdog_timeout"
    gate.set()
    rig.venue._hard_timeout = 30.0
    await rig.venue.health()  # queued behind the stuck job: returns once the worker is done with it
    assert rig.no_trade_click() and wa.calls == 0


@pytest.mark.anyio
async def test_stuck_worker_after_the_click_is_unconfirmed(rigs: Callable[..., Rig]) -> None:
    # The real-time watchdog must fire AFTER the click (~25 ms after submit on an idle machine). On a loaded
    # machine the click can be later; then the watchdog legitimately aborts first, so retry with more room.
    for hard_timeout in (0.1, 0.4, 1.6):
        rig = rigs(hard_timeout=hard_timeout)
        gate = threading.Event()
        rig.backend.capture_hook = lambda *_, r=rig, g=gate: g.wait(5) if r.terminal.order_clicks else None
        report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
        gate.set()
        rig.venue._hard_timeout = 30.0
        await rig.venue.health()  # queued behind the stuck job: returns once the worker is done with it
        if report.status is IntentStatus.ABORTED:  # the watchdog won the race: then it must never click
            assert report.stage == "watchdog_timeout" and rig.no_trade_click()
            continue
        assert report.status is IntentStatus.UNCONFIRMED and report.stage == "watchdog_timeout"
        assert rig.terminal.order_clicks == [("EURUSD", "BUY")]  # exactly one click, no retry
        break
    else:
        pytest.fail("the order never reached the click before the watchdog")


@pytest.mark.anyio
async def test_no_input_after_the_watchdog_gave_up(rigs: Callable[..., Rig]) -> None:
    # Once UNCONFIRMED was reported the operator may already be reconciling from the terminal: a late confirm
    # click by the resumed worker would open a position nobody tracks.
    for hard_timeout in (0.1, 0.4, 1.6):
        rig = rigs(hard_timeout=hard_timeout, terminal={"order_confirm_dialog": True})
        gate = threading.Event()
        rig.backend.capture_hook = lambda *_, r=rig, g=gate: g.wait(5) if r.terminal.order_clicks else None
        report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
        if report.status is IntentStatus.ABORTED:  # the watchdog fired before the click (slow machine): retry
            gate.set()
            continue
        assert report.status is IntentStatus.UNCONFIRMED and report.stage == "watchdog_timeout"
        assert rig.terminal.dialog == "order_confirm"
        inputs_at_report = len(rig.backend.input_actions())
        gate.set()
        rig.venue._hard_timeout = 30.0
        await rig.venue.health()  # queued behind the stuck job: returns once the worker is done with it
        assert rig.terminal.dialog_confirms == 0 and rig.terminal.dialog == "order_confirm"
        assert rig.terminal.orders == []
        later = [a for a in rig.backend.input_actions()[inputs_at_report:] if a.name != "move_mouse"]
        assert later == []  # only the harmless mouse park, no click / key press
        break
    else:
        pytest.fail("the order never reached the click before the watchdog")


@pytest.mark.anyio
async def test_cancelled_submit_never_clicks_later(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    gate = threading.Event()
    rig.backend.capture_hook = lambda *_: gate.wait(5)
    wa = rig.write_ahead()
    task = asyncio.create_task(rig.venue.submit(intent(), QUOTE, wa))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    await rig.venue.health()
    assert rig.no_trade_click() and wa.calls == 0


@pytest.mark.anyio
async def test_shutdown_cancels_queued_orders_without_clicking(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    gate = threading.Event()
    busy = threading.Event()

    def block(*_: int) -> None:
        busy.set()
        gate.wait(5)

    rig.backend.capture_hook = block
    first = asyncio.create_task(rig.venue.submit(intent(), QUOTE, rig.write_ahead()))
    await wait_for_event(busy)  # the first order is running on the worker (not merely queued)
    queued_wa = rig.write_ahead()
    second = asyncio.create_task(rig.venue.submit(intent(Side.SELL), QUOTE, queued_wa))
    await asyncio.sleep(0)
    await rig.venue.aclose()
    report = await second
    assert report.status is IntentStatus.ABORTED and report.stage == "closed"
    assert queued_wa.calls == 0
    gate.set()
    first_report = await first  # the running order finishes normally
    assert first_report.status is IntentStatus.CONFIRMED
    assert rig.terminal.order_clicks == [("EURUSD", "BUY")]


# ============================================================================================ serialization
@pytest.mark.anyio
async def test_concurrent_submits_never_interleave(rigs: Callable[..., Rig]) -> None:
    rig = rigs(symbols=("EURUSD", "GBPUSD"), probe=True)
    first, second = await asyncio.gather(
        rig.venue.submit(intent(lots="0.10"), QUOTE, rig.write_ahead()),
        rig.venue.submit(intent(Side.SELL, lots="0.20", symbol="GBPUSD"), None, rig.write_ahead()),
    )
    assert first.status is IntentStatus.CONFIRMED, first.message
    assert second.status is IntentStatus.CONFIRMED, second.message
    assert rig.probe is not None
    assert rig.probe.max_active == 1
    assert rig.probe.threads == {"unitrader-gui_0"}
    names = [(a.name, a.args) for a in rig.backend.input_actions()]
    first_click = names.index(("click", rig.screen(rig.layout.layout.panels["EURUSD"].buy_button)))
    second_typing = names.index(("type_text", ("0.20",)))
    assert first_click < second_typing  # the whole first order ran before the second one started
    assert rig.terminal.order_clicks == [("EURUSD", "BUY"), ("GBPUSD", "SELL")]


@pytest.mark.anyio
async def test_health_never_interleaves_with_an_order(rigs: Callable[..., Rig]) -> None:
    rig = rigs(probe=True)
    report, health = await asyncio.gather(rig.venue.submit(intent(), QUOTE, rig.write_ahead()), rig.venue.health())
    assert report.status is IntentStatus.CONFIRMED and health.ok
    assert rig.probe is not None and rig.probe.max_active == 1 and rig.probe.threads == {"unitrader-gui_0"}


@pytest.mark.anyio
async def test_gui_work_does_not_block_the_event_loop(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    gate = threading.Event()
    rig.backend.capture_hook = lambda *_: gate.wait(5)
    task = asyncio.create_task(rig.venue.submit(intent(), QUOTE, rig.write_ahead()))
    started = time.perf_counter()
    await asyncio.sleep(0.01)  # the loop keeps running while the worker is blocked
    assert time.perf_counter() - started < 1.0
    assert not task.done()
    gate.set()
    report = await task
    assert report.status is IntentStatus.CONFIRMED


# ============================================================================================ protective orders
@pytest.mark.anyio
async def test_protective_fields_are_typed_and_read_back(rigs: Callable[..., Rig]) -> None:
    rig = rigs(terminal={"protective_fields": True}, cfg={"protective_orders": "panel_fields"})
    assert rig.venue.supports_protective_orders
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED, report.message
    assert rig.typed() == ["0.10", "1.09800", "1.10400"]
    order = rig.terminal.orders[0]
    assert (order.sl_text, order.tp_text) == ("1.09800", "1.10400")
    assert {"stop_loss_readback", "take_profit_readback"} <= set(step_names(report))


@pytest.mark.anyio
async def test_protective_fields_in_pips(rigs: Callable[..., Rig]) -> None:
    rig = rigs(
        terminal={"protective_fields": True},
        cfg={"protective_orders": "panel_fields", "protective_units": "pips"},
    )
    report = await rig.venue.submit(intent(Side.SELL), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED, report.message
    assert rig.typed() == ["0.10", "20.0", "40.0"]


@pytest.mark.anyio
async def test_protective_field_readback_mismatch(rigs: Callable[..., Rig]) -> None:
    rig = rigs(terminal={"protective_fields": True}, cfg={"protective_orders": "panel_fields"})

    def hook(term: FakeTerminal, name: str) -> None:
        if name == "type_text" and term.focused_field == ("EURUSD", "sl"):
            term.set_field_text("EURUSD", "sl", "1.0")

    rig.terminal.on_action = hook
    wa = rig.write_ahead()
    report = await rig.venue.submit(intent(), QUOTE, wa)
    assert_aborted_without_click(rig, report, wa, "protective_fields")


@pytest.mark.anyio
async def test_protective_fields_not_calibrated(rigs: Callable[..., Rig]) -> None:
    rig = rigs(cfg={"protective_orders": "panel_fields"})
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.ABORTED and report.stage == "validate"
    assert "SL/TP inputs" in report.message
    assert rig.backend.actions == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "overrides",
    [
        {"stop_price": None},
        {"stop_price": 1.105},  # stop above target for a BUY
        {"entry_ref": 1.2},  # entry outside stop..target
    ],
)
async def test_inconsistent_protective_prices(rigs: Callable[..., Rig], overrides: dict[str, Any]) -> None:
    rig = rigs(terminal={"protective_fields": True}, cfg={"protective_orders": "panel_fields"})
    report = await rig.venue.submit(intent(**overrides), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.ABORTED and report.stage == "validate"


# ============================================================================================ closing
@pytest.mark.anyio
async def test_close_all_clicks_the_button_even_when_halted(rigs: Callable[..., Rig]) -> None:
    rig = rigs(halt=Halt(engaged=True))
    rig.terminal.open_position("EURUSD", "BUY", Decimal("0.10"))
    wa = rig.write_ahead()
    report = await rig.venue.close_all(close_all_intent(), wa)
    assert report.status is IntentStatus.CONFIRMED, report.message
    assert rig.terminal.close_all_clicks == 1 and rig.terminal.positions == []
    assert wa.calls == 1 and wa.close_all_clicks_seen == 0
    assert rig.terminal.order_clicks == []
    assert report.fill_price is None
    assert rig.halt.calls == 0  # the kill switch is not even consulted for closes


@pytest.mark.anyio
async def test_close_all_with_a_confirmation_dialog(rigs: Callable[..., Rig]) -> None:
    rig = rigs(terminal={"close_all_confirm_dialog": True})
    rig.terminal.open_position("EURUSD", "SELL", Decimal("0.10"))
    report = await rig.venue.close_all(close_all_intent(), rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED, report.message
    assert rig.terminal.dialog_confirms == 1 and rig.terminal.positions == []


@pytest.mark.anyio
async def test_close_all_without_calibrated_button(rigs: Callable[..., Rig]) -> None:
    rig = rigs(terminal={"has_close_all": False})
    wa = rig.write_ahead()
    report = await rig.venue.close_all(close_all_intent(), wa)
    assert report.status is IntentStatus.ABORTED and "close the position(s) manually" in report.message
    assert rig.backend.actions == [] and wa.calls == 0


@pytest.mark.anyio
async def test_close_all_with_nothing_open_is_unconfirmed(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    report = await rig.venue.close_all(close_all_intent(), rig.write_ahead())
    assert report.status is IntentStatus.UNCONFIRMED
    assert rig.terminal.close_all_clicks == 1


@pytest.mark.anyio
async def test_close_all_dry_run(rigs: Callable[..., Rig]) -> None:
    rig = rigs(dry_run=True)
    rig.terminal.open_position("EURUSD", "BUY", Decimal("0.10"))
    report = await rig.venue.close_all(close_all_intent(), rig.write_ahead())
    assert report.status is IntentStatus.DRY_RUN
    assert rig.backend.input_actions() == [] and len(rig.terminal.positions) == 1


@pytest.mark.anyio
async def test_close_all_still_verifies_the_account(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.badge = "LIVE"
    wa = rig.write_ahead()
    report = await rig.venue.close_all(close_all_intent(), wa)
    assert_aborted_without_click(rig, report, wa, "account_badge")


@pytest.mark.anyio
async def test_close_all_wrong_purpose(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    report = await rig.venue.close_all(intent(), rig.write_ahead())
    assert report.status is IntentStatus.ABORTED and report.stage == "validate"


@pytest.mark.anyio
async def test_close_position_via_close_all(rigs: Callable[..., Rig]) -> None:
    rig = rigs(halt=Halt(engaged=True))
    pos = position(Side.BUY)
    rig.terminal.open_position("EURUSD", "BUY", pos.lots)
    report = await rig.venue.close_position(pos, close_intent(pos), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.CONFIRMED, report.message
    assert rig.terminal.close_all_clicks == 1 and rig.terminal.positions == []
    assert report.fill_price == QUOTE.bid and report.fill_price_source == "feed_estimate"  # a BUY closes at the bid


@pytest.mark.anyio
async def test_close_position_via_close_all_needs_the_button(rigs: Callable[..., Rig]) -> None:
    rig = rigs(terminal={"has_close_all": False})
    pos = position()
    report = await rig.venue.close_position(pos, close_intent(pos), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.ABORTED and "manually" in report.message


@pytest.mark.anyio
async def test_close_position_via_opposite_order(rigs: Callable[..., Rig]) -> None:
    rig = rigs(cfg={"exit_method": "opposite_order", "netting_account_confirmed": True}, halt=Halt(engaged=True))
    pos = position(Side.SELL, lots="0.20")
    rig.terminal.open_position("EURUSD", "SELL", pos.lots)
    wa = rig.write_ahead()
    report = await rig.venue.close_position(pos, close_intent(pos), None, wa)
    assert report.status is IntentStatus.CONFIRMED, report.message
    assert rig.terminal.order_clicks == [("EURUSD", "BUY")]
    assert rig.typed() == ["0.20"]
    assert rig.terminal.positions == []
    assert report.fill_price == pytest.approx(pos.last_price)
    assert wa.calls == 1 and "halted" not in step_names(report)


@pytest.mark.anyio
async def test_opposite_order_needs_netting_confirmation(rigs: Callable[..., Rig]) -> None:
    rig = rigs(cfg={"exit_method": "opposite_order"})
    pos = position()
    report = await rig.venue.close_position(pos, close_intent(pos), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.ABORTED and "netting" in report.message
    assert rig.backend.actions == []


@pytest.mark.anyio
async def test_opposite_order_refuses_with_panel_protective_fields(rigs: Callable[..., Rig]) -> None:
    rig = rigs(
        terminal={"protective_fields": True},
        cfg={"exit_method": "opposite_order", "netting_account_confirmed": True, "protective_orders": "panel_fields"},
    )
    pos = position()
    report = await rig.venue.close_position(pos, close_intent(pos), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.ABORTED and "SL/TP" in report.message
    assert rig.backend.actions == []


@pytest.mark.anyio
async def test_close_position_intent_must_match_the_position(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    pos = position()
    report = await rig.venue.close_position(pos, close_intent(pos, position_id="other"), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.ABORTED and report.stage == "validate"
    report = await rig.venue.close_position(pos, intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.ABORTED and report.stage == "validate"


@pytest.mark.anyio
async def test_close_position_write_ahead_failure(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    pos = position()
    rig.terminal.open_position("EURUSD", "BUY", pos.lots)
    wa = rig.write_ahead(raises=RuntimeError("disk full"))
    report = await rig.venue.close_position(pos, close_intent(pos), QUOTE, wa)
    assert report.status is IntentStatus.ABORTED and report.stage == "write_ahead"
    assert rig.terminal.close_all_clicks == 0 and len(rig.terminal.positions) == 1


# ============================================================================================ preflight / health
HEALTH_NAMES = [
    "account_kind", "input_desktop", "window", "client_size", "dpi", "focus", "account_badge",
    "one_click_indicator", "panel:EURUSD", "panel:GBPUSD",
]  # fmt: skip


@pytest.mark.anyio
async def test_health_all_green(rigs: Callable[..., Rig]) -> None:
    rig = rigs(symbols=("EURUSD", "GBPUSD"))
    health = await rig.venue.health()
    assert health.ok and health.venue == "unitrader"
    assert [c.name for c in health.checks] == HEALTH_NAMES
    assert rig.backend.input_actions() == []  # never types or clicks


@pytest.mark.anyio
async def test_preflight_all_green_with_static_checks(rigs: Callable[..., Rig]) -> None:
    rig = rigs(symbols=("EURUSD", "GBPUSD"), terminal={"order_confirm_dialog": True}, dry_run=True)
    health = await rig.venue.preflight()
    assert health.ok, [c for c in health.checks if not c.ok]
    names = [c.name for c in health.checks]
    for expected in (
        "layout", "template:account_badge", "template:panel:EURUSD", "template:order_dialog",
        "template:error_templates[0]", "instrument:EURUSD", "instrument:GBPUSD", "exit_method", "timing", "dry_run",
    ):  # fmt: skip
        assert expected in names
    assert names[-len(HEALTH_NAMES) :] == HEALTH_NAMES
    assert len(names) == len(set(names))  # one check per item
    assert rig.backend.input_actions() == []


@pytest.mark.anyio
async def test_health_window_missing_tells_the_operator_to_log_in(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.window_present = False
    health = await rig.venue.health()
    assert not health.ok
    [failure] = health.failures
    assert failure.name == "window"
    assert failure.detail == (
        "UniTrader window not found: open UniTrader and log in to your DEMO account yourself, then retry."
    )
    assert failure.data["needs_login"] is True
    assert rig.backend.input_actions() == []


@pytest.mark.anyio
async def test_health_in_live_mode_names_the_live_account(rigs: Callable[..., Rig]) -> None:
    rig = rigs(mode=Mode.LIVE, terminal={"badge": "LIVE"})
    rig.terminal.window_present = False
    health = await rig.venue.health()
    assert "log in to your LIVE account yourself" in health.failures[0].detail


@pytest.mark.anyio
async def test_health_badge_mismatch(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.badge = "LIVE"
    health = await rig.venue.health()
    [failure] = health.failures
    assert failure.name == "account_badge"
    assert "Log in to your DEMO account in UniTrader yourself" in failure.detail
    assert failure.data["needs_login"] is True and failure.data["score"] < failure.data["threshold"]
    assert [c.name for c in health.checks] == HEALTH_NAMES[:-1]  # panels are still reported


@pytest.mark.anyio
async def test_health_reports_each_panel(rigs: Callable[..., Rig]) -> None:
    rig = rigs(symbols=("EURUSD", "GBPUSD"))
    rig.terminal.swap_panels = True
    health = await rig.venue.health()
    assert {c.name for c in health.failures} == {"panel:EURUSD", "panel:GBPUSD"}
    assert all("re-run desk calibrate" in c.detail for c in health.failures)


def refuse_focus(term: FakeTerminal) -> None:
    term.steal_focus()
    term.refuse_focus = True


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("setup", "name", "fragment"),
    [
        (lambda t: setattr(t, "locked", True), "input_desktop", "session locked/disconnected"),
        (lambda t: setattr(t, "duplicate_windows", 1), "window", "close duplicate windows"),
        (lambda t: setattr(t, "client_size", (1000, 600)), "client_size", "never resizes"),
        (lambda t: setattr(t, "dpi", 144), "dpi", "display scaling"),
        (refuse_focus, "focus", "foreground"),
        (lambda t: setattr(t, "one_click_enabled", False), "one_click_indicator", "one-click"),
    ],
)
async def test_health_failures_carry_remediation(
    rigs: Callable[..., Rig], setup: Callable[[FakeTerminal], Any], name: str, fragment: str
) -> None:
    rig = rigs()
    setup(rig.terminal)
    health = await rig.venue.health()
    assert not health.ok
    failure = next(c for c in health.failures if c.name == name)
    assert fragment in failure.detail
    assert rig.backend.input_actions() == []


@pytest.mark.anyio
async def test_health_account_kind_mismatch(rigs: Callable[..., Rig]) -> None:
    rig = rigs(mode=Mode.LIVE)
    health = await rig.venue.health()
    failure = next(c for c in health.failures if c.name == "account_kind")
    assert "LIVE account" in failure.detail and "re-run desk calibrate" in failure.detail


@pytest.mark.anyio
async def test_preflight_static_failures(rigs: Callable[..., Rig]) -> None:
    def blank_label(lf: LayoutFile) -> LayoutFile:
        ref = lf.layout.panels["EURUSD"].label
        Image.new("RGB", (ref.region.w, ref.region.h), (255, 255, 255)).save(lf.template_path(ref))
        return lf

    rig = rigs(terminal={"has_close_all": False}, layout_hook=blank_label, cfg={"max_order_seconds": 2.0})
    rig.venue._instruments["GBPUSD"] = make_instrument(symbol="GBPUSD", feed_symbol="GBP/USD")
    health = await rig.venue.preflight()
    failed = {c.name: c.detail for c in health.failures}
    assert "blank" in failed["template:panel:EURUSD"]
    assert "no calibrated panel" in failed["instrument:GBPUSD"]
    assert "close-all button" in failed["exit_method"]
    assert "max_order_seconds" in failed["timing"]
    assert "panel:EURUSD" in failed  # the live check fails too


@pytest.mark.anyio
async def test_preflight_missing_template(rigs: Callable[..., Rig]) -> None:
    def drop(lf: LayoutFile) -> LayoutFile:
        lf.template_path(lf.layout.account_badge).unlink()
        return lf

    rig = rigs(layout_hook=drop)
    health = await rig.venue.preflight()
    failed = {c.name: c for c in health.failures}
    assert "could not be loaded" in failed["template:account_badge"].detail
    assert "needs_login" not in failed["account_badge"].data  # a calibration problem, not a login problem


@pytest.mark.anyio
async def test_preflight_opposite_order_checks(rigs: Callable[..., Rig]) -> None:
    rig = rigs(cfg={"exit_method": "opposite_order", "netting_account_confirmed": True})
    health = await rig.venue.preflight()
    exit_check = next(c for c in health.checks if c.name == "exit_method")
    assert exit_check.ok
    rig2 = rigs(
        terminal={"protective_fields": True},
        cfg={"exit_method": "opposite_order", "netting_account_confirmed": True, "protective_orders": "panel_fields"},
    )
    health2 = await rig2.venue.preflight()
    assert not next(c for c in health2.checks if c.name == "exit_method").ok
    rig3 = rigs(cfg={"protective_orders": "panel_fields"})  # panels without SL/TP inputs
    health3 = await rig3.venue.preflight()
    assert not next(c for c in health3.checks if c.name == "instrument:EURUSD").ok


@pytest.mark.anyio
async def test_health_survives_backend_errors(rigs: Callable[..., Rig]) -> None:
    rig = rigs()

    def boom(*_: int) -> None:
        raise RuntimeError("grab failed")

    rig.backend.capture_hook = boom
    health = await rig.venue.health()
    assert not health.ok and health.checks[-1].name == "gui"


@pytest.mark.anyio
async def test_health_operator_failsafe_is_reported(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    original = rig.backend.activate_window

    def failsafe(handle: int) -> bool:
        from amongus_desk.execution.unitrader.backend import OperatorAbortError

        raise OperatorAbortError("corner")

    rig.backend.activate_window = failsafe  # type: ignore[method-assign]
    health = await rig.venue.health()
    assert not health.ok and "fail-safe" in health.checks[-1].detail
    rig.backend.activate_window = original  # type: ignore[method-assign]


@pytest.mark.anyio
async def test_closed_venue(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    await rig.venue.aclose()
    await rig.venue.aclose()  # idempotent
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.status is IntentStatus.ABORTED and report.stage == "closed"
    report = await rig.venue.close_all(close_all_intent(), rig.write_ahead())
    assert report.status is IntentStatus.ABORTED
    health = await rig.venue.health()
    assert not health.ok
    assert rig.backend.actions == []


@pytest.mark.anyio
async def test_reports_are_json_safe_and_pre_click_stages_are_known(rigs: Callable[..., Rig]) -> None:
    rig = rigs()
    rig.terminal.badge = "LIVE"
    report = await rig.venue.submit(intent(), QUOTE, rig.write_ahead())
    assert report.stage in PRE_CLICK_FAILURE_STAGES
    ExecutionReport.model_validate_json(report.model_dump_json())
