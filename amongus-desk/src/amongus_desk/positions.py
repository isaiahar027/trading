"""Position book: open positions, exit rules, estimated P&L and the risk snapshot.

The desk can't read UniTrader's ledger, so every number here is an *estimate* built from the desk's
own records: fills reported by the venue (or the feed price at the click) and exits at stop/target
levels or bar closes. The operator re-syncs equity from the terminal with :meth:`set_equity_base`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn

from .config import DeskConfig
from .domain import (
    Bar,
    ExecutionReport,
    ExitReason,
    OrderIntent,
    Position,
    PositionStatus,
    PriceSource,
    RiskSnapshot,
    Side,
    ensure_utc,
)
from .money import estimate_pnl, trading_day_start

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExitDue:
    position: Position
    reason: ExitReason
    price: float  # estimated exit price


class PositionBook:
    def __init__(
        self,
        cfg: DeskConfig,
        active: Iterable[Position] = (),
        history: Iterable[Position] = (),
        *,
        equity_base: float | None = None,
        equity_base_at: datetime | None = None,
    ) -> None:
        self._cfg = cfg
        self._active: dict[str, Position] = {}
        for pos in active:
            if pos.status is PositionStatus.CLOSED:
                raise ValueError(f"position {pos.id} is closed; pass it as history")
            self._active[pos.id] = pos
        self._history: list[Position] = sorted(
            (p for p in history if p.status is PositionStatus.CLOSED and p.closed_at is not None),
            key=lambda p: p.closed_at or p.opened_at,
        )
        self._equity_base = float(equity_base) if equity_base is not None else cfg.risk.starting_equity
        self._equity_base_at = ensure_utc(equity_base_at) if equity_base_at is not None else None

    # ------------------------------------------------------------------------------------ queries

    def active(self) -> list[Position]:
        return sorted(self._active.values(), key=lambda p: p.opened_at)

    def get(self, position_id: str) -> Position | None:
        return self._active.get(position_id)

    def history(self) -> list[Position]:
        return list(self._history)

    @property
    def equity_base(self) -> tuple[float, datetime | None]:
        return self._equity_base, self._equity_base_at

    def realized_since_base(self) -> float:
        base_at = self._equity_base_at
        return sum(
            p.pnl or 0.0
            for p in self._history
            if base_at is None or (p.closed_at is not None and p.closed_at > base_at)
        )

    def unrealized(self) -> float:
        total = 0.0
        for pos in self._active.values():
            if pos.last_price is None:
                continue
            total += estimate_pnl(pos.side, pos.lots, pos.entry_price, pos.last_price, self._cfg.instrument(pos.symbol))
        return total

    def equity(self) -> float:
        """Estimated equity: synced base + realized since the sync + unrealized."""
        return self._equity_base + self.realized_since_base() + self.unrealized()

    # ------------------------------------------------------------------------------------ mutations

    def add_open(self, intent: OrderIntent, report: ExecutionReport, *, protective: bool, at: datetime) -> Position:
        if intent.purpose != "open" or intent.side is None or intent.lots is None:
            raise ValueError("add_open needs an opening intent with side and lots")
        if intent.stop_price is None or intent.target_price is None:
            raise ValueError("opening intents must carry stop and target")
        if report.intent_id != intent.id:
            raise ValueError("report does not belong to this intent")
        source: PriceSource
        if report.fill_price is not None:
            entry, source = report.fill_price, report.fill_price_source
        elif intent.entry_ref is not None:
            entry, source = intent.entry_ref, "feed_estimate"
        else:
            raise ValueError("no entry price: neither a fill price nor a reference price")
        pos = Position(
            intent_id=intent.id,
            symbol=intent.symbol,
            side=intent.side,
            lots=intent.lots,
            entry_price=entry,
            entry_price_source=source,
            stop_price=intent.stop_price,
            target_price=intent.target_price,
            opened_at=ensure_utc(at),
            protective_at_broker=protective,
            time_stop_bars=self._cfg.strategy.time_stop_bars,
            last_price=entry,
        )
        self._active[pos.id] = pos
        log.info("position opened %s %s %s lots @ %s", pos.symbol, pos.side.value, pos.lots, pos.entry_price)
        return pos

    def add_unmanaged(self, intent: OrderIntent, report: ExecutionReport, *, at: datetime, note: str) -> Position:
        """Record a broker position the desk must not manage itself (a test order, or a fill the desk only
        learned about from the operator or after a crash). It is kept in RECONCILE status so it counts
        toward exposure and blocks new entries until the operator says it is closed."""
        if intent.side is None or intent.lots is None:
            raise ValueError("add_unmanaged needs an intent with side and lots")
        if report.intent_id != intent.id:
            raise ValueError("report does not belong to this intent")
        source: PriceSource
        if report.fill_price is not None:
            entry, source = report.fill_price, report.fill_price_source
        elif intent.entry_ref is not None:
            entry, source = intent.entry_ref, "feed_estimate"
        else:
            raise ValueError("no entry price: neither a fill price nor a reference price")
        pos = Position(
            intent_id=intent.id,
            symbol=intent.symbol,
            side=intent.side,
            lots=intent.lots,
            entry_price=entry,
            entry_price_source=source,
            stop_price=intent.stop_price if intent.stop_price is not None else entry,
            target_price=intent.target_price if intent.target_price is not None else entry,
            opened_at=ensure_utc(at),
            status=PositionStatus.RECONCILE,
            time_stop_bars=self._cfg.strategy.time_stop_bars,
            last_price=entry,
            reconcile_note=note,
        )
        self._active[pos.id] = pos
        log.warning("unmanaged position recorded %s %s %s lots: %s", pos.symbol, pos.side.value, pos.lots, note)
        return pos

    def mark_bar(self, bar: Bar) -> list[ExitDue]:
        """Mark open positions of ``bar.symbol`` to the bar; return the exits it triggers.

        Positions are not closed here: the engine decides how (broker-side protective levels are simply
        recorded, software-managed exits are sent to the venue first).
        """
        due: list[ExitDue] = []
        for pos_id, pos in list(self._active.items()):
            if pos.symbol != bar.symbol or bar.end <= pos.opened_at:
                continue
            if pos.last_marked_bar_end is not None and bar.end <= pos.last_marked_bar_end:
                continue  # already applied (e.g. a bar replayed after a restart)
            if pos.status is not PositionStatus.OPEN:
                self._active[pos_id] = pos.model_copy(update={"last_price": bar.close, "last_marked_bar_end": bar.end})
                continue
            updated = pos.model_copy(
                update={"last_price": bar.close, "bars_held": pos.bars_held + 1, "last_marked_bar_end": bar.end}
            )
            self._active[pos_id] = updated
            exit_due = self._exit_for(updated, bar)
            if exit_due is not None:
                due.append(exit_due)
        return due

    def _exit_for(self, pos: Position, bar: Bar) -> ExitDue | None:
        long = pos.side is Side.BUY
        stop_hit = bar.low <= pos.stop_price if long else bar.high >= pos.stop_price
        target_hit = bar.high >= pos.target_price if long else bar.low <= pos.target_price
        if stop_hit:  # checked first: when both levels are inside one bar, assume the worse outcome
            if not pos.protective_at_broker:
                return ExitDue(pos, ExitReason.STOP, bar.close)
            gapped = bar.open <= pos.stop_price if long else bar.open >= pos.stop_price
            return ExitDue(pos, ExitReason.STOP, bar.open if gapped else pos.stop_price)
        if target_hit:
            if not pos.protective_at_broker:
                return ExitDue(pos, ExitReason.TARGET, bar.close)
            gapped = bar.open >= pos.target_price if long else bar.open <= pos.target_price
            return ExitDue(pos, ExitReason.TARGET, bar.open if gapped else pos.target_price)
        if pos.bars_held >= pos.time_stop_bars:
            return ExitDue(pos, ExitReason.TIME, bar.close)
        return None

    def set_status(
        self,
        position_id: str,
        status: PositionStatus,
        *,
        close_intent_id: str | None = None,
        note: str | None = None,
        suggested_exit: ExitDue | None = None,
    ) -> Position:
        """Change the status. Moving to RECONCILE records ``note`` (why the operator is asked) and the exit the
        feed suggested, if any; moving back to OPEN clears them."""
        if status is PositionStatus.CLOSED:
            raise ValueError("use close() to close a position")
        pos = self._require(position_id)
        update: dict[str, object] = {"status": status}
        if close_intent_id is not None:
            update["close_intent_id"] = close_intent_id
        if status is PositionStatus.RECONCILE:
            if note is not None:
                update["reconcile_note"] = note
            if suggested_exit is not None:
                update["suggested_exit_reason"] = suggested_exit.reason
                update["suggested_exit_price"] = suggested_exit.price
        elif status is PositionStatus.OPEN:
            update |= {"reconcile_note": None, "suggested_exit_reason": None, "suggested_exit_price": None}
        updated = pos.model_copy(update=update)
        self._active[position_id] = updated
        return updated

    def close(
        self, position_id: str, *, price: float, source: PriceSource, reason: ExitReason, at: datetime
    ) -> Position:
        pos = self._require(position_id)
        if price <= 0:
            raise ValueError("exit price must be positive")
        pnl = estimate_pnl(pos.side, pos.lots, pos.entry_price, price, self._cfg.instrument(pos.symbol))
        closed = pos.model_copy(
            update={
                "status": PositionStatus.CLOSED,
                "exit_price": price,
                "exit_price_source": source,
                "exit_reason": reason,
                "closed_at": ensure_utc(at),
                "last_price": price,
                "pnl": round(pnl, 2),
            }
        )
        del self._active[position_id]
        self._history.append(closed)
        self._history.sort(key=lambda p: p.closed_at or p.opened_at)
        log.info("position closed %s %s (%s) pnl est %.2f", closed.symbol, closed.side.value, reason.value, pnl)
        return closed

    def restore(self, position: Position) -> None:
        """Put back an earlier snapshot of an active position (undo a status change that did not happen)."""
        self._require(position.id)
        if position.status is PositionStatus.CLOSED:
            raise ValueError("cannot restore a closed position")
        self._active[position.id] = position

    def remove(self, position_id: str) -> Position:
        """Forget a position the operator confirmed never filled."""
        pos = self._require(position_id)
        del self._active[position_id]
        return pos

    def set_equity_base(self, equity: float, at: datetime) -> None:
        """Operator re-sync: ``equity`` is the terminal's balance at ``at`` (realized P&L up to now included)."""
        if equity <= 0:
            raise ValueError("equity must be positive")
        self._equity_base = float(equity)
        self._equity_base_at = ensure_utc(at)

    def _require(self, position_id: str) -> Position:
        pos = self._active.get(position_id)
        return pos if pos is not None else self._missing(position_id)

    @staticmethod
    def _missing(position_id: str) -> NoReturn:
        raise KeyError(f"no active position {position_id}")

    # ------------------------------------------------------------------------------------ risk view

    def snapshot(
        self,
        now: datetime,
        *,
        kill_switch: bool,
        kill_reason: str | None,
        reconcile_pending: list[str],
        degraded: list[str],
    ) -> RiskSnapshot:
        now = ensure_utc(now)
        day_start = trading_day_start(now, self._cfg.risk.day_rollover_utc)
        realized_today = sum(p.pnl or 0.0 for p in self._history if p.closed_at and p.closed_at >= day_start)
        realized_equity = self._equity_base + self.realized_since_base()
        everything = [*self._active.values(), *self._history]
        trades_today = sum(1 for p in everything if p.opened_at >= day_start)

        streak = 0
        for pos in reversed(self._history):
            if (pos.pnl or 0.0) < 0:
                streak += 1
            else:
                break
        losses = [p for p in self._history if (p.pnl or 0.0) < 0 and p.closed_at is not None]
        by_symbol: dict[str, datetime] = {}
        for pos in losses:
            assert pos.closed_at is not None
            by_symbol[pos.symbol] = max(pos.closed_at, by_symbol.get(pos.symbol, pos.closed_at))

        return RiskSnapshot(
            now=now,
            equity=realized_equity + self.unrealized(),
            day_start_equity=realized_equity - realized_today,
            realized_pnl_today=realized_today,
            unrealized_pnl=self.unrealized(),
            trades_today=trades_today,
            open_positions=self.active(),
            consecutive_losses=streak,
            last_trade_at=max((p.opened_at for p in everything), default=None),
            last_loss_at=max((p.closed_at for p in losses if p.closed_at is not None), default=None),
            last_loss_at_by_symbol=by_symbol,
            kill_switch=kill_switch,
            kill_reason=kill_reason,
            reconcile_pending=list(reconcile_pending),
            degraded=list(degraded),
        )
