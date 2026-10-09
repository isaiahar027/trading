from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from amongus_desk.audit import AuditLog
from amongus_desk.clock import ManualClock
from amongus_desk.config import DeskConfig, Secrets
from amongus_desk.domain import (
    Bar,
    ExecutionReport,
    HealthCheck,
    IntentStatus,
    Mode,
    OrderIntent,
    Position,
    PositionStatus,
    Quote,
    Side,
    VenueHealth,
)
from amongus_desk.engine import DeskEngine, OperatorActionError
from amongus_desk.events import EventHub
from amongus_desk.execution.paper import PaperVenue
from amongus_desk.killswitch import KillSwitch
from amongus_desk.llm.base import DisabledReviewer
from amongus_desk.news import NewsCalendar
from amongus_desk.notify import NullNotifier
from amongus_desk.store import Store
from factories import SMALL_STRATEGY, T0, breakout_series, eurusd, make_bar


class ListFeed:
    name = "list"

    def __init__(self, warm: dict[str, list[Bar]], live: Sequence[Bar], on_yield: Callable[[Bar], None]) -> None:
        self._warm = warm
        self._live = list(live)
        self._on_yield = on_yield

    async def warmup(self, symbols: Sequence[str], bars: int) -> dict[str, list[Bar]]:
        return {s: self._warm[s][-bars:] for s in symbols if s in self._warm}

    async def stream(self) -> AsyncIterator[Bar]:
        for bar in self._live:
            self._on_yield(bar)
            yield bar

    def latest_quote(self, symbol: str) -> Quote | None:
        return None

    def status(self) -> dict[str, Any]:
        return {"name": "list"}

    async def aclose(self) -> None:
        return None


class ScriptedVenue:
    """Returns scripted statuses; ``click`` controls whether the write-ahead hook runs first."""

    name = "scripted"

    def __init__(
        self, clock: ManualClock, status: IntentStatus, *, click: bool = True, protective: bool = False
    ) -> None:
        self.clock = clock
        self.status = status
        self.click = click
        self.supports_protective_orders = protective
        self.raise_after_click = False
        self.calls: list[str] = []
        self.health_ok = True
        self.operator_abort = False

    def _report(self, intent: OrderIntent, price: float | None = None) -> ExecutionReport:
        now = self.clock.now()
        return ExecutionReport(
            intent_id=intent.id,
            status=self.status,
            venue=self.name,
            started_at=now,
            finished_at=now,
            clicked_at=now if self.click else None,
            fill_price=price if self.status is IntentStatus.CONFIRMED else None,
            fill_price_source="feed_estimate" if self.status is IntentStatus.CONFIRMED else "none",
            message=f"scripted {self.status.value}",
            operator_abort=self.operator_abort,
        )

    async def _go(
        self, kind: str, intent: OrderIntent, before: Callable[[], None], price: float | None
    ) -> ExecutionReport:
        self.calls.append(kind)
        if self.click:
            before()
            if self.raise_after_click:
                raise RuntimeError("GUI exploded after the click")
        return self._report(intent, price)

    async def preflight(self) -> VenueHealth:
        return await self.health()

    async def health(self) -> VenueHealth:
        check = HealthCheck(
            name="window",
            ok=self.health_ok,
            detail="ok" if self.health_ok else "UniTrader window not found: log in to UniTrader yourself",
        )
        return VenueHealth(venue=self.name, ok=self.health_ok, checked_at=self.clock.now(), checks=[check])

    async def submit(
        self, intent: OrderIntent, quote: Quote | None, on_before_click: Callable[[], None]
    ) -> ExecutionReport:
        return await self._go("submit", intent, on_before_click, intent.entry_ref)

    async def close_position(
        self, position: Position, intent: OrderIntent, quote: Quote | None, on_before_click: Callable[[], None]
    ) -> ExecutionReport:
        return await self._go("close", intent, on_before_click, intent.entry_ref)

    async def close_all(self, intent: OrderIntent, on_before_click: Callable[[], None]) -> ExecutionReport:
        return await self._go("close_all", intent, on_before_click, None)

    async def aclose(self) -> None:
        return None


def desk_cfg(tmp_path: Path, **over: Any) -> DeskConfig:
    raw: dict[str, Any] = {
        "data_dir": str(tmp_path / "data"),
        "instruments": [eurusd()],
        "strategy": {**SMALL_STRATEGY, "min_stop_spread_multiple": 2.0, "time_stop_bars": 50},
        "risk": {"starting_equity": 10_000, "max_lots_per_order": "0.50", "risk_per_trade_pct": 0.5},
    }
    raw.update(over)
    raw["secrets"] = Secrets()
    return DeskConfig.model_validate(raw)


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        *,
        cfg: DeskConfig | None = None,
        venue: Callable[[ManualClock], Any] | None = None,
        live: Sequence[Bar] = (),
    ) -> None:
        self.cfg = cfg or desk_cfg(tmp_path)
        self.clock = ManualClock(T0)
        self.bars = breakout_series(1)
        data = Path(self.cfg.data_dir)
        self.store = Store(data / "desk.db")
        self.audit = AuditLog(data / "audit.jsonl", fsync=False, clock=self.clock)
        self.kill = KillSwitch(data, clock=self.clock)
        self.notifier = NullNotifier()
        self.hub = EventHub(self.clock)
        inst = self.cfg.instrument("EURUSD")
        self.venue = (
            venue(self.clock)
            if venue
            else PaperVenue(
                {inst.symbol: inst}, self.cfg.execution.paper, self.clock, self.kill.engaged, lambda _s: None
            )
        )
        warm = {"EURUSD": self.bars[:-1]}
        stream = list(live) if live else [self.bars[-1]]
        self.feed = ListFeed(warm, stream, lambda b: self.clock.set(b.end + timedelta(seconds=2)))
        self.engine = DeskEngine(
            self.cfg,
            feed=self.feed,
            venue=self.venue,
            reviewer=DisabledReviewer(),
            store=self.store,
            audit=self.audit,
            notifier=self.notifier,
            hub=self.hub,
            clock=self.clock,
            killswitch=self.kill,
            calendar=NewsCalendar(self.cfg.news, tmp_path),
        )

    async def boot_and_run(self) -> None:
        self.clock.set(self.bars[-2].end + timedelta(seconds=1))
        await self.engine.start()
        await self.engine.run()

    def titles(self) -> list[str]:
        return [t for t, _m, _p in self.notifier.sent]

    def close(self) -> None:
        self.audit.close()
        self.store.close()


def target_bar(rig: Rig, index_offset: int = 0) -> Bar:
    last = rig.bars[-1]
    i = len(rig.bars) + index_offset
    return make_bar(i, last.close, last.close + 0.0030, last.close - 0.0001, last.close + 0.0028)


def stop_bar(rig: Rig, index_offset: int = 0) -> Bar:
    last = rig.bars[-1]
    i = len(rig.bars) + index_offset
    return make_bar(i, last.close, last.close + 0.0001, last.close - 0.0030, last.close - 0.0028)


@pytest.mark.anyio
async def test_breakout_to_paper_fill_to_target_exit(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.feed._live = [rig.bars[-1], target_bar(rig)]
    await rig.boot_and_run()
    meetings = rig.store.recent_meetings()
    assert len(meetings) == 1 and meetings[0].decision.value == "EXECUTE_BUY", meetings[0].reasons
    (row,) = rig.store.recent_intents()
    assert row.status is IntentStatus.CONFIRMED and row.intent.side is Side.BUY
    closed = rig.store.closed_positions_since(T0)
    assert len(closed) == 1 and closed[0].exit_reason is not None and closed[0].exit_reason.value == "TARGET"
    assert closed[0].pnl is not None and closed[0].pnl > 0
    assert rig.engine.book.active() == []
    assert any(t.startswith("Opened BUY") for t in rig.titles())
    assert any(t.startswith("Closed BUY") for t in rig.titles())
    assert AuditLog.verify(Path(rig.cfg.data_dir) / "audit.jsonl").ok
    state = rig.engine.state()
    json.dumps(state)  # JSON-safe
    assert state["equity"]["estimate"] > 10_000 and state["limits"]["trades_today"] == 1
    assert state["crew"][0]["id"] == "CYAN_SCOUT"
    rig.close()


@pytest.mark.anyio
async def test_kill_switch_blocks_new_trades(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.kill.engage("testing", by="test")
    await rig.boot_and_run()
    (meeting,) = rig.store.recent_meetings()
    assert meeting.decision.value == "SKIP_ROUND"
    assert any("kill switch" in r for r in meeting.reasons)
    assert rig.store.recent_intents() == []
    rig.close()


@pytest.mark.anyio
async def test_unconfirmed_open_halts_and_reconciles(tmp_path: Path) -> None:
    rig = Rig(tmp_path, venue=lambda c: ScriptedVenue(c, IntentStatus.UNCONFIRMED))
    await rig.boot_and_run()
    (row,) = rig.store.recent_intents()
    assert row.status is IntentStatus.UNCONFIRMED
    state = rig.engine.state()
    assert len(state["reconcile"]) == 1 and state["reconcile"][0]["kind"] == "intent"
    assert any(p == "urgent" for _t, _m, p in rig.notifier.sent)
    with pytest.raises(OperatorActionError, match="reconcil"):
        await rig.engine.resume(by="op")
    await rig.engine.reconcile(row.intent.id, "filled", fill_price=1.10123, by="op")
    assert rig.store.get_intent(row.intent.id).status is IntentStatus.RECONCILED_FILLED  # type: ignore[union-attr]
    (pos,) = rig.engine.book.active()
    assert pos.entry_price == 1.10123 and pos.entry_price_source == "operator"
    assert rig.engine.state()["reconcile"] == []
    with pytest.raises(OperatorActionError):
        await rig.engine.reconcile(row.intent.id, "filled", fill_price=None, by="op")
    rig.close()


@pytest.mark.anyio
async def test_reconcile_not_filled(tmp_path: Path) -> None:
    rig = Rig(tmp_path, venue=lambda c: ScriptedVenue(c, IntentStatus.UNCONFIRMED))
    await rig.boot_and_run()
    (row,) = rig.store.recent_intents()
    await rig.engine.reconcile(row.intent.id, "not_filled", fill_price=None, by="op")
    assert rig.engine.book.active() == []
    assert rig.store.get_intent(row.intent.id).status is IntentStatus.RECONCILED_NOT_FILLED  # type: ignore[union-attr]
    rig.close()


@pytest.mark.anyio
async def test_aborted_after_write_ahead_is_forced_unconfirmed(tmp_path: Path) -> None:
    rig = Rig(tmp_path, venue=lambda c: ScriptedVenue(c, IntentStatus.ABORTED, click=True))
    await rig.boot_and_run()
    (row,) = rig.store.recent_intents()
    assert row.status is IntentStatus.UNCONFIRMED
    assert row.report is not None and "after the write-ahead marker" in row.report.message
    rig.close()


@pytest.mark.anyio
@pytest.mark.parametrize(("click", "expected"), [(False, IntentStatus.ABORTED), (True, IntentStatus.UNCONFIRMED)])
async def test_venue_exception(tmp_path: Path, click: bool, expected: IntentStatus) -> None:
    async def explode(*_a: Any, **_k: Any) -> ExecutionReport:
        raise RuntimeError("boom")

    def make(c: ManualClock) -> ScriptedVenue:
        venue = ScriptedVenue(c, IntentStatus.CONFIRMED, click=click)
        venue.raise_after_click = True
        if not click:
            venue.submit = explode  # type: ignore[method-assign]
        return venue

    rig = Rig(tmp_path, venue=make)
    await rig.boot_and_run()
    (row,) = rig.store.recent_intents()
    assert row.status is expected
    assert row.report is not None and row.report.stage == "venue_exception"
    rig.close()


@pytest.mark.anyio
async def test_operator_failsafe_engages_kill_switch(tmp_path: Path) -> None:
    def make(c: ManualClock) -> ScriptedVenue:
        venue = ScriptedVenue(c, IntentStatus.ABORTED, click=False)
        venue.operator_abort = True
        return venue

    rig = Rig(tmp_path, venue=make)
    await rig.boot_and_run()
    assert rig.kill.engaged() and "fail-safe" in (rig.kill.reason() or "")
    rig.close()


@pytest.mark.anyio
async def test_software_exit_closes_via_venue_and_retries(tmp_path: Path) -> None:
    rig = Rig(tmp_path, venue=lambda c: ScriptedVenue(c, IntentStatus.CONFIRMED, protective=False))
    venue = rig.venue
    rig.feed._live = [rig.bars[-1], stop_bar(rig), stop_bar(rig, 1), stop_bar(rig, 2)]

    # first close aborts (e.g. window missing), the next bar's retry succeeds
    original = venue.close_position
    attempts = {"n": 0}

    async def flaky_close(*args: Any, **kwargs: Any) -> ExecutionReport:
        attempts["n"] += 1
        venue.status = IntentStatus.ABORTED if attempts["n"] == 1 else IntentStatus.CONFIRMED
        venue.click = attempts["n"] != 1
        return await original(*args, **kwargs)

    venue.close_position = flaky_close  # type: ignore[method-assign]
    await rig.boot_and_run()
    assert attempts["n"] == 2
    closed = rig.store.closed_positions_since(T0)
    assert len(closed) == 1 and closed[0].exit_reason is not None and closed[0].exit_reason.value == "STOP"
    assert any("Could not close" in t for t in rig.titles())
    rig.close()


@pytest.mark.anyio
async def test_unconfirmed_close_marks_reconcile(tmp_path: Path) -> None:
    rig = Rig(tmp_path, venue=lambda c: ScriptedVenue(c, IntentStatus.CONFIRMED, protective=False))
    venue = rig.venue
    rig.feed._live = [rig.bars[-1], stop_bar(rig)]
    original = venue.close_position

    async def unconfirmed_close(*args: Any, **kwargs: Any) -> ExecutionReport:
        venue.status = IntentStatus.UNCONFIRMED
        return await original(*args, **kwargs)

    venue.close_position = unconfirmed_close  # type: ignore[method-assign]
    await rig.boot_and_run()
    (pos,) = rig.engine.book.active()
    assert pos.status is PositionStatus.RECONCILE
    items = rig.engine.state()["reconcile"]
    assert len(items) == 1 and items[0]["purpose"] == "close"
    await rig.engine.reconcile(items[0]["id"], "filled", fill_price=1.0990, by="op")
    assert rig.engine.book.active() == []
    rig.close()


@pytest.mark.anyio
async def test_panic_closes_everything(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.boot_and_run()
    assert len(rig.engine.book.active()) == 1
    report = await rig.engine.panic(by="dashboard")
    assert report is not None and report.status is IntentStatus.CONFIRMED
    assert rig.engine.book.active() == [] and rig.kill.engaged()
    assert rig.store.closed_positions_since(T0)[0].exit_reason is not None
    rig.close()


@pytest.mark.anyio
async def test_panic_without_close_all_reopens_and_alerts(tmp_path: Path) -> None:
    rig = Rig(tmp_path, venue=lambda c: ScriptedVenue(c, IntentStatus.CONFIRMED, protective=True))
    venue = rig.venue
    await rig.boot_and_run()
    venue.status, venue.click = IntentStatus.ABORTED, False
    await rig.engine.panic(by="dashboard")
    (pos,) = rig.engine.book.active()
    assert pos.status is PositionStatus.OPEN
    assert any("PANIC could not close" in t for t in rig.titles())
    rig.close()


@pytest.mark.anyio
async def test_restart_reconciliation(tmp_path: Path) -> None:
    cfg = desk_cfg(tmp_path)
    data = Path(cfg.data_dir)
    store = Store(data / "desk.db")
    common: dict[str, Any] = {
        "meeting_id": "m",
        "symbol": "EURUSD",
        "side": Side.BUY,
        "lots": Decimal("0.10"),
        "entry_ref": 1.1,
        "stop_price": 1.099,
        "target_price": 1.102,
        "mode": Mode.PAPER,
        "created_at": T0,
    }
    pending = OrderIntent(**common)
    submitting = OrderIntent(**common)
    store.add_intent(pending)
    store.add_intent(submitting)
    store.set_intent_status(submitting.id, IntentStatus.SUBMITTING)
    store.close()

    rig = Rig(tmp_path, cfg=cfg)
    rig.feed._live = []
    await rig.boot_and_run()
    assert rig.store.get_intent(pending.id).status is IntentStatus.ABORTED  # type: ignore[union-attr]
    items = rig.engine.state()["reconcile"]
    assert [i["id"] for i in items] == [submitting.id]
    assert "restarted" in items[0]["message"]
    rig.close()


@pytest.mark.anyio
async def test_equity_sync_persists(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.feed._live = []
    await rig.boot_and_run()
    await rig.engine.set_equity(12_345.0, by="op")
    with pytest.raises(OperatorActionError):
        await rig.engine.set_equity(0, by="op")
    rig.close()
    again = Rig(tmp_path)
    again.feed._live = []
    await again.boot_and_run()
    assert again.engine.state()["equity"]["estimate"] == pytest.approx(12_345.0)
    again.close()


@pytest.mark.anyio
async def test_manual_meetings(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.feed._live = []
    await rig.boot_and_run()
    meeting = await rig.engine.manual_meeting("EURUSD", by="op")
    assert meeting.trigger == "manual"
    forced = await rig.engine.manual_meeting("EURUSD", by="op", force_side=Side.SELL)
    assert forced.signal is not None and forced.signal.side is Side.SELL
    with pytest.raises(OperatorActionError):
        await rig.engine.manual_meeting("XAUUSD", by="op")
    rig.close()

    demo_cfg = desk_cfg(
        tmp_path / "demo", mode="demo", execution={"venue": "unitrader", "unitrader": {"backend": "fake"}}
    )
    demo = Rig(tmp_path / "demo", cfg=demo_cfg, venue=lambda c: ScriptedVenue(c, IntentStatus.CONFIRMED))
    demo.feed._live = []
    await demo.boot_and_run()
    with pytest.raises(OperatorActionError, match="paper mode"):
        await demo.engine.manual_meeting("EURUSD", by="op", force_side=Side.BUY)
    demo.close()


@pytest.mark.anyio
async def test_watchdog_venue_and_feed_health(tmp_path: Path) -> None:
    demo_cfg = desk_cfg(tmp_path, mode="demo", execution={"venue": "unitrader", "unitrader": {"backend": "fake"}})
    rig = Rig(tmp_path, cfg=demo_cfg, venue=lambda c: ScriptedVenue(c, IntentStatus.CONFIRMED))
    venue = rig.venue
    rig.feed._live = []
    await rig.boot_and_run()
    venue.health_ok = False
    await rig.engine.check_health()
    state = rig.engine.state()
    assert any("execution venue not ready" in d for d in state["degraded"])
    assert any(t == "UniTrader needs you" for t in rig.titles())
    login_msg = next(m for t, m, _p in rig.notifier.sent if t == "UniTrader needs you")
    assert "log in to your DEMO account yourself" in login_msg
    venue.health_ok = True
    rig.clock.advance(3600)  # an hour without bars during the session -> stale
    await rig.engine.check_health()
    state = rig.engine.state()
    assert not any("venue" in d for d in state["degraded"])
    assert any("stale" in d for d in state["degraded"])
    rig.close()


@pytest.mark.anyio
async def test_disabled_and_duplicate_bars_ignored(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.feed._live = []
    await rig.boot_and_run()
    before = len(rig.engine.history)
    await rig.engine.on_bar(rig.bars[-2])  # already in history
    await rig.engine.on_bar(make_bar(99, 1.3, 1.31, 1.29, 1.3, symbol="GBPUSD"))  # not configured
    assert len(rig.engine.history) == before
    rig.close()
