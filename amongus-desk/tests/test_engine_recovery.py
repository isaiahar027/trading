"""Engine behaviour around restarts, unknown outcomes and operator answers (adversarial-review fixes)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from amongus_desk.config import ConfigError, DeskConfig
from amongus_desk.domain import (
    BreakoutSignal,
    ExecutionReport,
    ExitReason,
    IntentStatus,
    Mode,
    OrderIntent,
    Position,
    PositionStatus,
    Side,
    TradePlan,
    Vote,
)
from amongus_desk.engine import OperatorActionError
from amongus_desk.llm.base import ReviewContext, ReviewOutcome
from amongus_desk.store import Store
from factories import T0, eurusd, make_bar
from rigs import Rig, ScriptedVenue, desk_cfg, stop_bar, target_bar


def demo_cfg(tmp_path: Path, **over: Any) -> DeskConfig:
    over.setdefault("instruments", [eurusd(specs_verified=True)])
    return desk_cfg(tmp_path, mode="demo", execution={"venue": "unitrader", "unitrader": {"backend": "fake"}}, **over)


def scripted(protective: bool) -> Any:
    return lambda c: ScriptedVenue(c, IntentStatus.CONFIRMED, protective=protective)


def flat_bar(rig: Rig, offset: int) -> Any:
    last = rig.bars[-1]
    return make_bar(len(rig.bars) + offset, last.close, last.close + 0.00005, last.close - 0.00005, last.close)


async def open_one(tmp_path: Path, cfg: DeskConfig, protective: bool) -> Position:
    """Run the desk once so it opens a single BUY, then stop it."""
    rig = Rig(tmp_path, cfg=cfg, venue=scripted(protective))
    await rig.boot_and_run()
    (pos,) = rig.engine.book.active()
    assert pos.status is PositionStatus.OPEN
    rig.close()
    return pos


def report_for(intent: OrderIntent, status: IntentStatus, fill: float | None = None) -> ExecutionReport:
    return ExecutionReport(
        intent_id=intent.id,
        status=status,
        venue="scripted",
        started_at=T0,
        finished_at=T0,
        clicked_at=T0,
        fill_price=fill,
        fill_price_source="feed_estimate" if fill is not None else "none",
        message="test",
    )


def open_intent(**over: Any) -> OrderIntent:
    fields: dict[str, Any] = {
        "meeting_id": None,
        "symbol": "EURUSD",
        "side": Side.BUY,
        "lots": Decimal("0.10"),
        "entry_ref": 1.1,
        "stop_price": 1.099,
        "target_price": 1.102,
        "mode": Mode.PAPER,
        "created_at": T0,
    }
    fields.update(over)
    return OrderIntent(**fields)


# ------------------------------------------------------------------- broker-side SL/TP is never inferred


@pytest.mark.anyio
async def test_feed_touching_broker_target_asks_the_operator(tmp_path: Path) -> None:
    rig = Rig(tmp_path, cfg=demo_cfg(tmp_path), venue=scripted(True))
    rig.feed._live = [rig.bars[-1], target_bar(rig)]
    await rig.boot_and_run()
    (pos,) = rig.engine.book.active()
    assert pos.status is PositionStatus.RECONCILE and pos.suggested_exit_reason is ExitReason.TARGET
    assert rig.store.closed_positions_since(T0) == []
    assert rig.venue.calls == ["submit"]  # nothing clicked for the exit
    (item,) = rig.engine.state()["reconcile"]
    assert item["kind"] == "position" and "target" in item["message"]
    assert "still open" in item["question"]
    assert {o["outcome"]: o["label"] for o in item["options"]} == {
        "filled": "No, it is closed",
        "not_filled": "Yes, it is still open",
    }
    assert any(t.startswith("Check EURUSD") for t in rig.titles())
    with pytest.raises(OperatorActionError):
        await rig.engine.resume(by="op")
    await rig.engine.reconcile(pos.id, "filled", fill_price=None, by="op")
    (closed,) = rig.store.closed_positions_since(T0)
    assert closed.exit_reason is ExitReason.TARGET and closed.exit_price == pos.target_price
    rig.close()


@pytest.mark.anyio
async def test_operator_says_still_open_returns_the_position(tmp_path: Path) -> None:
    rig = Rig(tmp_path, cfg=demo_cfg(tmp_path), venue=scripted(True))
    rig.feed._live = [rig.bars[-1], target_bar(rig)]
    await rig.boot_and_run()
    (pos,) = rig.engine.book.active()
    await rig.engine.reconcile(pos.id, "not_filled", fill_price=None, by="op")
    (again,) = rig.engine.book.active()
    assert again.status is PositionStatus.OPEN and again.reconcile_note is None
    assert rig.engine.state()["reconcile"] == []
    rig.close()


@pytest.mark.anyio
async def test_paper_mode_still_books_simulated_broker_levels(tmp_path: Path) -> None:
    rig = Rig(tmp_path, venue=scripted(True))
    rig.feed._live = [rig.bars[-1], target_bar(rig)]
    await rig.boot_and_run()
    (closed,) = rig.store.closed_positions_since(T0)
    assert closed.exit_reason is ExitReason.TARGET
    rig.close()


# ------------------------------------------------------------------- restart catch-up


@pytest.mark.anyio
async def test_restart_applies_missed_bars_to_broker_protected_position(tmp_path: Path) -> None:
    cfg = demo_cfg(tmp_path)
    await open_one(tmp_path, cfg, protective=True)
    rig = Rig(tmp_path, cfg=cfg, venue=scripted(True))
    rig.feed._warm = {"EURUSD": [*rig.bars, stop_bar(rig)]}  # the stop was crossed while the desk was down
    rig.feed._live = []
    await rig.boot_and_run()
    (pos,) = rig.engine.book.active()
    assert pos.status is PositionStatus.RECONCILE and pos.suggested_exit_reason is ExitReason.STOP
    assert "while the desk was down" in (pos.reconcile_note or "")
    assert rig.venue.calls == []
    rig.close()


@pytest.mark.anyio
async def test_restart_sends_a_missed_software_exit_on_the_first_live_bar(tmp_path: Path) -> None:
    cfg = desk_cfg(tmp_path)
    await open_one(tmp_path, cfg, protective=False)
    rig = Rig(tmp_path, cfg=cfg, venue=scripted(False))
    rig.feed._warm = {"EURUSD": [*rig.bars, stop_bar(rig)]}
    rig.feed._live = [flat_bar(rig, 1)]  # back above the stop: the exit still goes out
    await rig.boot_and_run()
    assert rig.venue.calls == ["close"]
    (closed,) = rig.store.closed_positions_since(T0)
    assert closed.exit_reason is ExitReason.STOP
    rig.close()


@pytest.mark.anyio
async def test_replayed_bars_are_not_counted_twice(tmp_path: Path) -> None:
    cfg = desk_cfg(tmp_path)
    rig = Rig(tmp_path, cfg=cfg, venue=scripted(False))
    rig.feed._live = [rig.bars[-1], flat_bar(rig, 0), flat_bar(rig, 1)]
    await rig.boot_and_run()
    (pos,) = rig.engine.book.active()
    assert pos.bars_held == 2
    rig.close()
    rig2 = Rig(tmp_path, cfg=cfg, venue=scripted(False))
    rig2.feed._warm = {"EURUSD": [*rig2.bars, flat_bar(rig2, 0), flat_bar(rig2, 1), flat_bar(rig2, 2)]}
    rig2.feed._live = []
    await rig2.boot_and_run()
    (pos2,) = rig2.engine.book.active()
    assert pos2.bars_held == 3  # only the one bar it had not seen
    rig2.close()


@pytest.mark.anyio
async def test_disabled_instrument_keeps_managing_its_open_position(tmp_path: Path) -> None:
    await open_one(tmp_path, desk_cfg(tmp_path), protective=False)
    gbp = eurusd(symbol="GBPUSD", feed_symbol="GBP/USD")
    cfg = desk_cfg(tmp_path, instruments=[eurusd(enabled=False), gbp])
    rig = Rig(tmp_path, cfg=cfg, venue=scripted(False))
    assert [i.symbol for i in rig.engine.managed_instruments()] == ["GBPUSD"]  # book not loaded yet
    rig.feed._live = [stop_bar(rig)]
    await rig.boot_and_run()
    assert rig.venue.calls == ["close"]
    assert rig.engine.book.active() == []
    rig.close()


@pytest.mark.anyio
async def test_position_on_an_instrument_removed_from_the_config_refuses_to_start(tmp_path: Path) -> None:
    await open_one(tmp_path, desk_cfg(tmp_path), protective=False)
    cfg = desk_cfg(tmp_path, instruments=[eurusd(symbol="GBPUSD", feed_symbol="GBP/USD")])
    # Rig assumes EURUSD is configured, so build this engine by hand.
    from amongus_desk.audit import AuditLog
    from amongus_desk.clock import ManualClock
    from amongus_desk.engine import DeskEngine
    from amongus_desk.events import EventHub
    from amongus_desk.killswitch import KillSwitch
    from amongus_desk.llm.base import DisabledReviewer
    from amongus_desk.news import NewsCalendar
    from amongus_desk.notify import NullNotifier
    from rigs import ListFeed

    clock = ManualClock(T0)
    data = Path(cfg.data_dir)
    store = Store(data / "desk.db")
    audit = AuditLog(data / "audit.jsonl", fsync=False, clock=clock)
    engine = DeskEngine(
        cfg,
        feed=ListFeed({}, [], lambda _b: None),
        venue=ScriptedVenue(clock, IntentStatus.CONFIRMED),
        reviewer=DisabledReviewer(),
        store=store,
        audit=audit,
        notifier=NullNotifier(),
        hub=EventHub(clock),
        clock=clock,
        killswitch=KillSwitch(data, clock=clock),
        calendar=NewsCalendar(cfg.news, tmp_path),
    )
    with pytest.raises(ConfigError, match="EURUSD"):
        await engine.start()
    audit.close()
    store.close()


# ------------------------------------------------------------------- startup repair after a crash


@pytest.mark.anyio
async def test_startup_closes_position_whose_close_was_confirmed(tmp_path: Path) -> None:
    cfg = desk_cfg(tmp_path)
    store = Store(Path(cfg.data_dir) / "desk.db")
    opener = open_intent()
    store.add_intent(opener)
    store.set_intent_status(opener.id, IntentStatus.CONFIRMED, report_for(opener, IntentStatus.CONFIRMED, 1.1))
    close = OrderIntent(
        purpose="close",
        meeting_id=None,
        symbol="EURUSD",
        side=Side.SELL,
        lots=Decimal("0.10"),
        entry_ref=1.101,
        stop_price=None,
        target_price=None,
        mode=Mode.PAPER,
        created_at=T0,
    )
    store.add_intent(close)
    store.set_intent_status(close.id, IntentStatus.CONFIRMED, report_for(close, IntentStatus.CONFIRMED, 1.101))
    pos = Position(
        intent_id=opener.id,
        symbol="EURUSD",
        side=Side.BUY,
        lots=Decimal("0.10"),
        entry_price=1.1,
        entry_price_source="feed_estimate",
        stop_price=1.099,
        target_price=1.102,
        opened_at=T0,
        status=PositionStatus.CLOSING,
        time_stop_bars=50,
        last_price=1.101,
        close_intent_id=close.id,
    )
    store.upsert_position(pos)
    store.close()

    rig = Rig(tmp_path, cfg=cfg)
    rig.feed._live = []
    await rig.boot_and_run()
    assert rig.engine.book.active() == []
    (closed,) = rig.store.closed_positions_since(T0)
    assert closed.id == pos.id and closed.exit_reason is ExitReason.RECONCILED
    rig.close()


@pytest.mark.anyio
async def test_startup_recovers_a_filled_order_without_a_position(tmp_path: Path) -> None:
    cfg = desk_cfg(tmp_path)
    store = Store(Path(cfg.data_dir) / "desk.db")
    opener = open_intent()
    store.add_intent(opener)
    store.set_intent_status(opener.id, IntentStatus.CONFIRMED, report_for(opener, IntentStatus.CONFIRMED, 1.1))
    store.close()

    rig = Rig(tmp_path, cfg=cfg)
    rig.feed._live = []
    await rig.boot_and_run()
    (pos,) = rig.engine.book.active()
    assert pos.intent_id == opener.id and pos.status is PositionStatus.RECONCILE
    assert [i["id"] for i in rig.engine.state()["reconcile"]] == [pos.id]
    assert any("recovered" in t for t in rig.titles())
    rig.close()


@pytest.mark.anyio
async def test_startup_puts_a_rejected_close_to_the_operator(tmp_path: Path) -> None:
    cfg = desk_cfg(tmp_path)
    store = Store(Path(cfg.data_dir) / "desk.db")
    opener = open_intent()
    store.add_intent(opener)
    store.set_intent_status(opener.id, IntentStatus.CONFIRMED, report_for(opener, IntentStatus.CONFIRMED, 1.1))
    close = open_intent(purpose="close", side=Side.SELL, stop_price=None, target_price=None)
    store.add_intent(close)
    store.set_intent_status(close.id, IntentStatus.SUBMITTING)
    store.set_intent_status(close.id, IntentStatus.REJECTED, report_for(close, IntentStatus.REJECTED))
    store.upsert_position(
        Position(
            intent_id=opener.id,
            symbol="EURUSD",
            side=Side.BUY,
            lots=Decimal("0.10"),
            entry_price=1.1,
            entry_price_source="feed_estimate",
            stop_price=1.099,
            target_price=1.102,
            opened_at=T0,
            status=PositionStatus.CLOSING,
            time_stop_bars=50,
            close_intent_id=close.id,
        )
    )
    store.close()
    rig = Rig(tmp_path, cfg=cfg)
    rig.feed._live = []
    await rig.boot_and_run()
    (pos,) = rig.engine.book.active()
    assert pos.status is PositionStatus.RECONCILE
    rig.close()


# ------------------------------------------------------------------- clicks that came back wrong


@pytest.mark.anyio
async def test_rejected_close_is_not_clicked_again(tmp_path: Path) -> None:
    rig = Rig(tmp_path, venue=scripted(False))
    venue = rig.venue
    rig.feed._live = [rig.bars[-1], stop_bar(rig), stop_bar(rig, 1), stop_bar(rig, 2)]
    original = venue.close_position

    async def rejected_close(*args: Any, **kwargs: Any) -> ExecutionReport:
        venue.status = IntentStatus.REJECTED
        return await original(*args, **kwargs)

    venue.close_position = rejected_close  # type: ignore[method-assign]
    await rig.boot_and_run()
    assert venue.calls.count("close") == 1
    (pos,) = rig.engine.book.active()
    assert pos.status is PositionStatus.RECONCILE and "terminal showed an error" in (pos.reconcile_note or "")
    rig.close()


@pytest.mark.anyio
async def test_failed_panic_keeps_the_link_to_an_unresolved_close(tmp_path: Path) -> None:
    cfg = desk_cfg(tmp_path)
    pos = await open_one(tmp_path, cfg, protective=False)
    store = Store(Path(cfg.data_dir) / "desk.db")
    close = open_intent(purpose="close", side=Side.SELL, stop_price=None, target_price=None, position_id=pos.id)
    store.add_intent(close)
    store.set_intent_status(close.id, IntentStatus.SUBMITTING)  # the desk died mid-close
    store.upsert_position(pos.model_copy(update={"status": PositionStatus.CLOSING, "close_intent_id": close.id}))
    store.close()

    rig = Rig(tmp_path, cfg=cfg, venue=scripted(False))
    rig.feed._live = []
    await rig.boot_and_run()
    rig.venue.status, rig.venue.click = IntentStatus.ABORTED, False
    await rig.engine.panic(by="op")
    (after,) = rig.engine.book.active()
    assert after.status is PositionStatus.CLOSING and after.close_intent_id == close.id
    assert rig.store.get_position(pos.id).close_intent_id == close.id  # type: ignore[union-attr]
    await rig.engine.reconcile(close.id, "filled", fill_price=1.1, by="op")
    assert rig.engine.book.active() == []
    rig.close()


# ------------------------------------------------------------------- operator answers


@pytest.mark.anyio
async def test_reconciling_a_test_order_open_records_an_unmanaged_position(tmp_path: Path) -> None:
    cfg = desk_cfg(tmp_path)
    store = Store(Path(cfg.data_dir) / "desk.db")
    test_order = open_intent(entry_ref=None, stop_price=None, target_price=None)
    store.add_intent(test_order)
    store.set_intent_status(test_order.id, IntentStatus.SUBMITTING)
    store.close()
    rig = Rig(tmp_path, cfg=cfg)
    rig.feed._live = []
    await rig.boot_and_run()
    (item,) = rig.engine.state()["reconcile"]
    assert item["question"].startswith("Did this BUY 0.10 EURUSD order open a position")
    with pytest.raises(OperatorActionError, match="fill price"):
        await rig.engine.reconcile(test_order.id, "filled", fill_price=None, by="op")
    assert rig.store.get_intent(test_order.id).status is IntentStatus.SUBMITTING  # type: ignore[union-attr]
    await rig.engine.reconcile(test_order.id, "filled", fill_price=1.1005, by="op")
    (pos,) = rig.engine.book.active()
    assert pos.status is PositionStatus.RECONCILE and pos.entry_price == 1.1005
    assert rig.store.get_position(pos.id) is not None
    with pytest.raises(OperatorActionError):
        await rig.engine.resume(by="op")
    rig.close()


@pytest.mark.anyio
async def test_reconcile_refuses_a_healthy_position_and_bad_prices(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.boot_and_run()
    (pos,) = rig.engine.book.active()
    with pytest.raises(OperatorActionError, match="not waiting"):
        await rig.engine.reconcile(pos.id, "filled", fill_price=None, by="op")
    for bad in (float("inf"), float("nan"), -1.0, 0.0):
        with pytest.raises(OperatorActionError):
            await rig.engine.reconcile(pos.id, "filled", fill_price=bad, by="op")
        with pytest.raises(OperatorActionError):
            await rig.engine.set_equity(bad, by="op")
    assert rig.engine.book.active() == [pos]
    rig.close()


# ------------------------------------------------------------------- stale signals and slow reviews


class SlowReviewer:
    """Approves, but only after ``release`` is set (or after moving the clock)."""

    name = "slow"

    def __init__(self, advance: timedelta | None = None, clock: Any = None) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.advance = advance
        self.clock = clock

    async def review(self, signal: BreakoutSignal, plan: TradePlan, context: ReviewContext) -> ReviewOutcome:
        self.started.set()
        if self.advance is not None:
            self.clock.set(self.clock.now() + self.advance)
        else:
            await self.release.wait()
        return ReviewOutcome(vote=Vote.PROCEED)

    def usage_today(self) -> dict[str, Any]:
        return {"enabled": True, "calls": 0}


def llm_cfg(tmp_path: Path) -> DeskConfig:
    return desk_cfg(tmp_path, llm={"enabled": True})


@pytest.mark.anyio
async def test_kill_abandons_a_meeting_waiting_for_claude(tmp_path: Path) -> None:
    reviewer = SlowReviewer()
    rig = Rig(tmp_path, cfg=llm_cfg(tmp_path), reviewer=reviewer)
    rig.clock.set(rig.bars[-2].end + timedelta(seconds=1))
    await rig.engine.start()
    rig.clock.set(rig.bars[-1].end + timedelta(seconds=2))
    task = asyncio.create_task(rig.engine.on_bar(rig.bars[-1]))
    await asyncio.wait_for(reviewer.started.wait(), 5)
    await asyncio.wait_for(rig.engine.panic(by="op"), 5)  # does not wait for the review
    await asyncio.wait_for(task, 5)
    assert [r.intent.purpose for r in rig.store.recent_intents()] == ["close_all"]
    assert rig.store.recent_meetings() == []
    rig.close()


@pytest.mark.anyio
async def test_plan_that_went_stale_during_the_review_is_not_sent(tmp_path: Path) -> None:
    reviewer = SlowReviewer(advance=timedelta(minutes=6))
    rig = Rig(tmp_path, cfg=llm_cfg(tmp_path), reviewer=reviewer)
    reviewer.clock = rig.clock
    await rig.boot_and_run()
    (meeting,) = rig.store.recent_meetings()
    assert meeting.decision.value == "EXECUTE_BUY"
    assert rig.store.recent_intents() == []
    assert any("went stale" in t for t in rig.titles())
    rig.close()


@pytest.mark.anyio
async def test_old_bar_from_a_feed_catch_up_is_not_traded(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.clock.set(rig.bars[-2].end + timedelta(seconds=1))
    await rig.engine.start()
    rig.clock.set(rig.bars[-1].end + timedelta(minutes=5, seconds=5))  # the next bar has closed too
    await rig.engine.on_bar(rig.bars[-1])
    assert rig.store.recent_meetings() == []
    assert "not trading on it" in rig.engine.state()["crew"][0]["summary"]
    rig.close()


# ------------------------------------------------------------------- daily loss and equity


@pytest.mark.anyio
async def test_daily_loss_flatten_is_retried_until_something_is_clicked(tmp_path: Path) -> None:
    cfg = desk_cfg(
        tmp_path,
        risk={
            "starting_equity": 10_000,
            "max_lots_per_order": "0.50",
            "risk_per_trade_pct": 0.5,
            "max_daily_loss_pct": 0.1,
            "flatten_on_daily_loss": True,
        },
    )
    rig = Rig(tmp_path, cfg=cfg, venue=scripted(False))
    venue = rig.venue
    original = venue.close_all
    attempts = {"n": 0}

    async def flaky_close_all(*args: Any, **kwargs: Any) -> ExecutionReport:
        attempts["n"] += 1
        venue.status, venue.click = (
            (IntentStatus.ABORTED, False) if attempts["n"] == 1 else (IntentStatus.CONFIRMED, True)
        )
        return await original(*args, **kwargs)

    venue.close_all = flaky_close_all  # type: ignore[method-assign]
    last = rig.bars[-1]
    # Stay above the stop but lose enough to breach 0.1% of equity.
    sag = [
        make_bar(len(rig.bars) + k, last.close, last.close, last.close - 0.0006, last.close - 0.0005) for k in (0, 1)
    ]
    rig.feed._live = [last, *sag]
    await rig.boot_and_run()
    assert attempts["n"] == 2
    assert rig.engine.book.active() == []
    assert sum(t == "Daily loss limit reached" for t in rig.titles()) == 1
    rig.close()


@pytest.mark.anyio
async def test_equity_without_a_sync_includes_old_realized_pnl(tmp_path: Path) -> None:
    cfg = desk_cfg(tmp_path)
    store = Store(Path(cfg.data_dir) / "desk.db")
    old = T0 - timedelta(days=40)
    store.upsert_position(
        Position(
            intent_id="x",
            symbol="EURUSD",
            side=Side.BUY,
            lots=Decimal("1"),
            entry_price=1.1,
            entry_price_source="feed_estimate",
            stop_price=1.09,
            target_price=1.12,
            opened_at=old,
            status=PositionStatus.CLOSED,
            time_stop_bars=50,
            exit_price=1.08,
            closed_at=old + timedelta(hours=1),
            exit_reason=ExitReason.STOP,
            pnl=-2000.0,
        )
    )
    store.close()
    rig = Rig(tmp_path, cfg=cfg)
    rig.feed._live = []
    await rig.boot_and_run()
    assert rig.engine.state()["equity"]["estimate"] == 8000.0
    rig.close()
