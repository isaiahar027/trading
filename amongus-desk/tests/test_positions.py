from __future__ import annotations

from datetime import time, timedelta
from decimal import Decimal

import pytest

from amongus_desk.domain import (
    ExecutionReport,
    ExitReason,
    IntentStatus,
    Mode,
    OrderIntent,
    PositionStatus,
    Side,
)
from amongus_desk.positions import PositionBook
from factories import T0, make_bar, make_config


def cfg(**risk: object):  # type: ignore[no-untyped-def]
    return make_config(
        risk={"starting_equity": 10000, "max_lots_per_order": "0.50", **risk},
        strategy={"time_stop_bars": 3},
    )


def _intent(side: Side = Side.BUY, *, entry: float = 1.1000, stop: float | None = None, target: float | None = None):  # type: ignore[no-untyped-def]
    if stop is None:
        stop = entry - 0.0015 if side is Side.BUY else entry + 0.0015
    if target is None:
        target = entry + 0.0030 if side is Side.BUY else entry - 0.0030
    return OrderIntent(
        meeting_id="m",
        symbol="EURUSD",
        side=side,
        lots=Decimal("0.10"),
        entry_ref=entry,
        stop_price=stop,
        target_price=target,
        mode=Mode.PAPER,
        created_at=T0,
    )


def _report(intent: OrderIntent, price: float | None) -> ExecutionReport:
    return ExecutionReport(
        intent_id=intent.id,
        status=IntentStatus.CONFIRMED,
        venue="paper",
        started_at=T0,
        finished_at=T0,
        fill_price=price,
        fill_price_source="venue" if price is not None else "none",
        message="",
    )


def _open(book: PositionBook, side: Side = Side.BUY, *, protective: bool = True, at=T0, entry: float = 1.1000):  # type: ignore[no-untyped-def]
    it = _intent(side, entry=entry)
    return book.add_open(it, _report(it, entry), protective=protective, at=at)


# bars: index 0 starts at T0 (08:00); a position opened at T0 is affected from bar 0 on (bar.end > opened_at)


def test_add_open_uses_fill_price_or_reference() -> None:
    book = PositionBook(cfg())
    it = _intent()
    pos = book.add_open(it, _report(it, 1.10012), protective=True, at=T0)
    assert pos.entry_price == 1.10012 and pos.entry_price_source == "venue"
    it2 = _intent()
    pos2 = book.add_open(it2, _report(it2, None), protective=False, at=T0)
    assert pos2.entry_price == 1.1000 and pos2.entry_price_source == "feed_estimate"
    assert pos2.time_stop_bars == 3 and pos2.last_price == 1.1000
    with pytest.raises(ValueError, match="does not belong"):
        book.add_open(it, _report(it2, 1.1), protective=True, at=T0)


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_stop_exit_protective_at_level(side: Side) -> None:
    book = PositionBook(cfg())
    pos = _open(book, side)
    bar = (
        make_bar(0, 1.0999, 1.1001, 1.0980, 1.0990) if side is Side.BUY else make_bar(0, 1.1001, 1.1020, 1.0999, 1.1010)
    )
    (due,) = book.mark_bar(bar)
    assert due.reason is ExitReason.STOP and due.price == pytest.approx(pos.stop_price)


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_target_exit_protective_at_level(side: Side) -> None:
    book = PositionBook(cfg())
    pos = _open(book, side)
    bar = (
        make_bar(0, 1.1001, 1.1035, 1.0999, 1.1030) if side is Side.BUY else make_bar(0, 1.0999, 1.1001, 1.0965, 1.0970)
    )
    (due,) = book.mark_bar(bar)
    assert due.reason is ExitReason.TARGET and due.price == pytest.approx(pos.target_price)


def test_both_levels_in_one_bar_assume_stop() -> None:
    book = PositionBook(cfg())
    _open(book)
    (due,) = book.mark_bar(make_bar(0, 1.1000, 1.1040, 1.0980, 1.1010))
    assert due.reason is ExitReason.STOP


def test_gap_through_uses_bar_open_when_protective() -> None:
    book = PositionBook(cfg())
    _open(book)
    (due,) = book.mark_bar(make_bar(0, 1.0970, 1.0975, 1.0960, 1.0965))
    assert due.reason is ExitReason.STOP and due.price == pytest.approx(1.0970)
    book2 = PositionBook(cfg())
    _open(book2)
    (due2,) = book2.mark_bar(make_bar(0, 1.1040, 1.1050, 1.1035, 1.1045))
    assert due2.reason is ExitReason.TARGET and due2.price == pytest.approx(1.1040)


def test_software_managed_exits_at_bar_close() -> None:
    book = PositionBook(cfg())
    _open(book, protective=False)
    (due,) = book.mark_bar(make_bar(0, 1.0999, 1.1001, 1.0980, 1.0990))
    assert due.reason is ExitReason.STOP and due.price == pytest.approx(1.0990)


def test_time_stop_after_n_bars_and_bars_held() -> None:
    book = PositionBook(cfg())
    pos = _open(book)
    quiet = [make_bar(i, 1.1000, 1.1005, 1.0995, 1.1001) for i in range(3)]
    assert book.mark_bar(quiet[0]) == [] and book.mark_bar(quiet[1]) == []
    (due,) = book.mark_bar(quiet[2])
    assert due.reason is ExitReason.TIME and due.price == pytest.approx(1.1001)
    assert book.get(pos.id).bars_held == 3  # type: ignore[union-attr]


def test_bars_before_opening_and_other_symbols_are_ignored() -> None:
    book = PositionBook(cfg())
    pos = _open(book, at=T0 + timedelta(minutes=10))
    assert book.mark_bar(make_bar(0, 1.1, 1.1, 1.08, 1.09)) == []  # ended before the position existed
    assert book.mark_bar(make_bar(2, 1.1, 1.1, 1.08, 1.09, symbol="GBPUSD")) == []
    assert book.get(pos.id).bars_held == 0  # type: ignore[union-attr]


def test_closing_and_reconcile_positions_are_not_re_exited() -> None:
    book = PositionBook(cfg())
    pos = _open(book)
    book.set_status(pos.id, PositionStatus.CLOSING, close_intent_id="c1")
    assert book.mark_bar(make_bar(0, 1.0999, 1.1001, 1.0980, 1.0990)) == []
    assert book.get(pos.id).last_price == pytest.approx(1.0990)  # type: ignore[union-attr]
    assert book.get(pos.id).close_intent_id == "c1"  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="close"):
        book.set_status(pos.id, PositionStatus.CLOSED)


def test_close_computes_pnl_and_moves_to_history() -> None:
    book = PositionBook(cfg())
    pos = _open(book)
    closed = book.close(pos.id, price=1.1030, source="venue", reason=ExitReason.TARGET, at=T0 + timedelta(hours=1))
    assert closed.pnl == pytest.approx(30.0) and closed.status is PositionStatus.CLOSED
    assert book.active() == [] and book.history() == [closed]
    with pytest.raises(KeyError):
        book.close(pos.id, price=1.1, source="venue", reason=ExitReason.MANUAL, at=T0)
    short = _open(book, Side.SELL)
    assert book.close(short.id, price=1.1010, source="venue", reason=ExitReason.STOP, at=T0).pnl == pytest.approx(-10.0)


def test_equity_unrealized_and_sync() -> None:
    book = PositionBook(cfg())
    pos = _open(book)
    book.mark_bar(make_bar(0, 1.1000, 1.1012, 1.0998, 1.1010))
    assert book.unrealized() == pytest.approx(10.0)
    assert book.equity() == pytest.approx(10010.0)
    book.close(pos.id, price=1.1010, source="venue", reason=ExitReason.MANUAL, at=T0 + timedelta(minutes=5))
    assert book.equity() == pytest.approx(10010.0)
    book.set_equity_base(10_500.0, T0 + timedelta(minutes=6))
    assert book.equity() == pytest.approx(10_500.0)  # realized before the sync is already in the base
    with pytest.raises(ValueError):
        book.set_equity_base(0, T0)


def test_remove_reconciled_not_filled() -> None:
    book = PositionBook(cfg())
    pos = _open(book)
    assert book.remove(pos.id).id == pos.id
    assert book.active() == []
    with pytest.raises(KeyError):
        book.remove(pos.id)


def test_snapshot_daily_figures_and_streaks() -> None:
    c = cfg(day_rollover_utc="00:00")
    book = PositionBook(c)
    yesterday = T0 - timedelta(days=1)
    # yesterday: one loss; today: win then two losses (streak 2), plus one open position
    for at, exit_price in (
        (yesterday, 1.0990),
        (T0, 1.1020),
        (T0 + timedelta(hours=1), 1.0990),
        (T0 + timedelta(hours=2), 1.0985),
    ):
        pos = _open(book, at=at)
        book.close(pos.id, price=exit_price, source="venue", reason=ExitReason.MANUAL, at=at + timedelta(minutes=30))
    open_pos = _open(book, at=T0 + timedelta(hours=3))
    now = T0 + timedelta(hours=4)
    snap = book.snapshot(now, kill_switch=False, kill_reason=None, reconcile_pending=["x"], degraded=["feed stale"])
    assert snap.trades_today == 4
    assert snap.realized_pnl_today == pytest.approx(20 - 10 - 15)
    assert snap.day_start_equity == pytest.approx(10000 - 10)
    assert snap.consecutive_losses == 2
    assert snap.last_trade_at == open_pos.opened_at
    assert snap.last_loss_at == T0 + timedelta(hours=2, minutes=30)
    assert snap.last_loss_at_by_symbol == {"EURUSD": T0 + timedelta(hours=2, minutes=30)}
    assert [p.id for p in snap.open_positions] == [open_pos.id]
    assert snap.reconcile_pending == ["x"] and snap.degraded == ["feed stale"]
    assert snap.equity == pytest.approx(10000 - 10 + 20 - 10 - 15)


def test_win_resets_streak_and_rollover_hour_moves_the_day() -> None:
    book = PositionBook(cfg(day_rollover_utc="22:00"))
    a = _open(book, at=T0)
    book.close(a.id, price=1.0990, source="venue", reason=ExitReason.STOP, at=T0 + timedelta(minutes=10))
    b = _open(book, at=T0 + timedelta(minutes=20))
    book.close(b.id, price=1.1010, source="venue", reason=ExitReason.TARGET, at=T0 + timedelta(minutes=30))
    snap = book.snapshot(
        T0 + timedelta(hours=15), kill_switch=False, kill_reason=None, reconcile_pending=[], degraded=[]
    )
    assert snap.consecutive_losses == 0
    assert snap.trades_today == 0  # 23:00 is after the 22:00 rollover: a new trading day
    assert time(22, 0) == book._cfg.risk.day_rollover_utc


def test_history_must_be_closed() -> None:
    book = PositionBook(cfg())
    pos = _open(book)
    with pytest.raises(ValueError, match="history"):
        PositionBook(cfg(), active=[pos.model_copy(update={"status": PositionStatus.CLOSED})])
    restored = PositionBook(cfg(), active=[pos], history=[pos])  # open entries in history are ignored
    assert restored.history() == [] and restored.get(pos.id) == pos
