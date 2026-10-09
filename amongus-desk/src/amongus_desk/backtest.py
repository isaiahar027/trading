"""Backtester: the live crew (scout, planner, calendar, sizing, risk gate, position rules) on historical bars.

* A signal on a bar's close fills at the **next bar's open**, plus or minus half the typical spread and
  the paper slippage, never at the signal bar's close (no look-ahead).
* Exits follow :class:`~amongus_desk.positions.PositionBook`. ``execution.unitrader.protective_orders``
  decides stop/target fills at the level (broker-side) versus at the bar close (software-managed), as in
  live trading. Paper venues count as protective. Exits pay half the spread too.
* Claude is never called; Pink abstains. Instrument ``specs_verified`` is ignored (backtests are paper).
* Bars from several symbols are interleaved in time order. Positions still open at the end are marked to
  the last close and reported as such.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .clock import ManualClock
from .config import DeskConfig
from .crew import MeetingContext
from .domain import (
    Bar,
    BreakoutSignal,
    ExecutionReport,
    ExitReason,
    FinalAction,
    IntentStatus,
    MeetingRecord,
    Mode,
    OrderIntent,
    Side,
    TradePlan,
    Vote,
)
from .llm.base import DisabledReviewer
from .meeting import EmergencyMeeting
from .money import estimate_pnl
from .news import NewsCalendar
from .positions import PositionBook
from .risk import RiskGate
from .strategy import BarHistory, BreakoutScout, ChartsPlanner

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BacktestTrade:
    symbol: str
    side: Side
    lots: Decimal
    entry_time: datetime
    entry_price: float
    stop_price: float
    target_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str
    pnl: float
    risk_amount: float  # loss had the stop been hit at its level
    closed_at_end: bool = False

    @property
    def r_multiple(self) -> float:
        return self.pnl / self.risk_amount if self.risk_amount > 0 else 0.0


@dataclass
class BacktestReport:
    starting_equity: float
    trades: list[BacktestTrade] = field(default_factory=list)
    signals: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    bars: int = 0
    start: datetime | None = None
    end: datetime | None = None

    @property
    def count(self) -> int:
        return len(self.trades)

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def ending_equity(self) -> float:
        return self.starting_equity + self.total_pnl

    @property
    def win_rate(self) -> float:
        return sum(1 for t in self.trades if t.pnl > 0) / self.count if self.trades else 0.0

    @property
    def avg_r(self) -> float:
        return sum(t.r_multiple for t in self.trades) / self.count if self.trades else 0.0

    @property
    def expectancy_r(self) -> float:
        """Average R per trade: win rate * average win R - loss rate * average loss R."""
        if not self.trades:
            return 0.0
        wins = [t.r_multiple for t in self.trades if t.pnl > 0]
        losses = [-t.r_multiple for t in self.trades if t.pnl <= 0]
        p = len(wins) / self.count
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        return p * avg_win - (1 - p) * avg_loss

    @property
    def profit_factor(self) -> float | None:
        gross_win = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = -sum(t.pnl for t in self.trades if t.pnl < 0)
        if gross_loss == 0:
            return None if gross_win == 0 else float("inf")
        return gross_win / gross_loss

    @property
    def max_drawdown(self) -> tuple[float, float]:
        """(amount, percent of the peak) on the closed-trade equity curve."""
        peak = equity = self.starting_equity
        worst = worst_pct = 0.0
        for trade in sorted(self.trades, key=lambda t: t.exit_time):
            equity += trade.pnl
            peak = max(peak, equity)
            drawdown = peak - equity
            if drawdown > worst:
                worst, worst_pct = drawdown, drawdown / peak * 100 if peak > 0 else 0.0
        return worst, worst_pct

    @property
    def total_return_pct(self) -> float:
        return self.total_pnl / self.starting_equity * 100 if self.starting_equity else 0.0

    def to_dict(self) -> dict[str, object]:
        dd, dd_pct = self.max_drawdown
        pf = self.profit_factor
        return {
            "bars": self.bars,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "signals": self.signals,
            "trades": self.count,
            "win_rate": round(self.win_rate, 4),
            "avg_r": round(self.avg_r, 4),
            "expectancy_r": round(self.expectancy_r, 4),
            "profit_factor": None if pf is None else (round(pf, 4) if pf != float("inf") else "inf"),
            "total_pnl": round(self.total_pnl, 2),
            "total_return_pct": round(self.total_return_pct, 4),
            "max_drawdown": round(dd, 2),
            "max_drawdown_pct": round(dd_pct, 4),
            "skipped": dict(self.skipped.most_common()),
            "trade_list": [
                {
                    "symbol": t.symbol,
                    "side": t.side.value,
                    "lots": str(t.lots),
                    "entry_time": t.entry_time.isoformat(),
                    "entry_price": t.entry_price,
                    "exit_time": t.exit_time.isoformat(),
                    "exit_price": t.exit_price,
                    "exit_reason": t.exit_reason,
                    "pnl": round(t.pnl, 2),
                    "r": round(t.r_multiple, 3),
                    "closed_at_end": t.closed_at_end,
                }
                for t in self.trades
            ],
        }


@dataclass
class _Pending:
    plan: TradePlan
    lots: Decimal
    meeting_id: str


def run_backtest(cfg: DeskConfig, bars: Mapping[str, Sequence[Bar]]) -> BacktestReport:
    """Replay ``bars`` (per symbol, any order) through the deterministic crew."""
    symbols = {i.symbol for i in cfg.enabled_instruments}
    timeline = sorted(
        (b for s, series in bars.items() if s in symbols for b in series), key=lambda b: (b.end, b.symbol)
    )
    report = BacktestReport(starting_equity=cfg.risk.starting_equity)
    if not timeline:
        return report
    report.start, report.end, report.bars = timeline[0].start, timeline[-1].end, len(timeline)

    clock = ManualClock(timeline[0].start)
    history = BarHistory(
        maxlen=max(500, 3 * cfg.strategy.warmup_bars), timeframe_minutes=cfg.strategy.timeframe_minutes
    )
    scout = BreakoutScout(cfg.strategy)
    gate = RiskGate(cfg.risk, cfg.strategy)
    calendar = NewsCalendar(cfg.news, cfg.root_dir)
    meeting = EmergencyMeeting(
        cfg, planner=ChartsPlanner(cfg.strategy), calendar=calendar, gate=gate, reviewer=DisabledReviewer()
    )
    book = PositionBook(cfg)
    protective = cfg.execution.venue == "paper" or cfg.execution.unitrader.protective_orders == "panel_fields"
    slip = cfg.execution.paper.slippage_pips
    pending: dict[str, _Pending] = {}
    risk_at_entry: dict[str, float] = {}
    last_close: dict[str, Bar] = {}

    def half_spread(symbol: str) -> float:
        inst = cfg.instrument(symbol)
        return inst.typical_spread_price / 2 + slip * inst.pip_size

    def record_close(pos_id: str, price: float, reason: ExitReason, at: datetime, *, at_end: bool = False) -> None:
        pos = book.get(pos_id)
        assert pos is not None
        fill = price - half_spread(pos.symbol) if pos.side is Side.BUY else price + half_spread(pos.symbol)
        closed = book.close(pos_id, price=fill, source="venue", reason=reason, at=at)
        report.trades.append(
            BacktestTrade(
                symbol=closed.symbol,
                side=closed.side,
                lots=closed.lots,
                entry_time=closed.opened_at,
                entry_price=closed.entry_price,
                stop_price=closed.stop_price,
                target_price=closed.target_price,
                exit_time=at,
                exit_price=fill,
                exit_reason=reason.value if not at_end else "END",
                pnl=closed.pnl or 0.0,
                risk_amount=risk_at_entry.pop(pos_id, 0.0),
                closed_at_end=at_end,
            )
        )

    for bar in timeline:
        clock.set(bar.start)
        inst = cfg.instrument(bar.symbol)

        # 1) fill yesterday's decision at this bar's open
        order = pending.pop(bar.symbol, None)
        if order is not None:
            plan = order.plan
            side = plan.side
            entry = bar.open + half_spread(bar.symbol) if side is Side.BUY else bar.open - half_spread(bar.symbol)
            gapped = entry <= plan.stop_price if side is Side.BUY else entry >= plan.stop_price
            if gapped:
                report.skipped["fill: opened through the stop"] += 1
            else:
                intent = OrderIntent(
                    meeting_id=order.meeting_id,
                    symbol=bar.symbol,
                    side=side,
                    lots=order.lots,
                    entry_ref=plan.entry_ref,
                    stop_price=plan.stop_price,
                    target_price=plan.target_price,
                    mode=Mode.PAPER,
                    created_at=bar.start,
                )
                fill = ExecutionReport(
                    intent_id=intent.id,
                    status=IntentStatus.CONFIRMED,
                    venue="backtest",
                    started_at=bar.start,
                    finished_at=bar.start,
                    clicked_at=bar.start,
                    fill_price=round(entry, inst.price_decimals),
                    fill_price_source="venue",
                    message="backtest fill at next open",
                )
                pos = book.add_open(intent, fill, protective=protective, at=bar.start)
                risk_at_entry[pos.id] = abs(estimate_pnl(side, order.lots, pos.entry_price, plan.stop_price, inst))

        # 2) exits on this bar
        for due in book.mark_bar(bar):
            record_close(due.position.id, due.price, due.reason, bar.end)

        # 3) scout on the closed bar
        clock.set(bar.end)
        last_close[bar.symbol] = bar
        if not history.add(bar):
            continue
        result = scout.evaluate(history.bars(bar.symbol))
        if result.signal is None:
            continue
        report.signals += 1
        snapshot = book.snapshot(bar.end, kill_switch=False, kill_reason=None, reconcile_pending=[], degraded=[])
        ctx = MeetingContext(
            now=bar.end,
            mode=Mode.PAPER,
            instrument=inst,
            bars=history.bars(bar.symbol),
            quote=None,
            snapshot=snapshot,
            last_bar_end=bar.end,
        )
        record = _run(meeting, result.signal, ctx)
        if record.decision is FinalAction.SKIP_ROUND or record.plan is None or record.lots is None:
            report.skipped[_skip_key(record)] += 1
            continue
        if len(book.active()) + len(pending) >= cfg.risk.max_open_positions or bar.symbol in pending:
            report.skipped["Red Skeptic: max_open_positions (pending entry)"] += 1
            continue
        pending[bar.symbol] = _Pending(plan=record.plan, lots=record.lots, meeting_id=record.id)

    for pos in book.active():
        last = last_close.get(pos.symbol)
        if last is not None:
            record_close(pos.id, last.close, ExitReason.MANUAL, last.end, at_end=True)
    return report


def _run(meeting: EmergencyMeeting, signal: BreakoutSignal, ctx: MeetingContext) -> MeetingRecord:
    """Run the (async) meeting synchronously: with Claude disabled nothing in it awaits real I/O, so the
    coroutine finishes on its first step. Works inside or outside a running event loop."""
    coro = meeting.convene(signal, ctx)
    try:
        coro.send(None)
    except StopIteration as done:
        record: MeetingRecord = done.value
        return record
    coro.close()
    raise RuntimeError("the backtest meeting tried to await I/O; the LLM must be disabled in backtests")


def _skip_key(record: MeetingRecord) -> str:
    for rep in record.reports:
        if rep.vote is Vote.EJECT:
            name = rep.crew_id.value.replace("_", " ").title()
            reason = rep.summary.split(";")[0].split(":")[0].strip()
            return f"{name}: {reason}"[:80]
    return "skipped"


def format_report(report: BacktestReport) -> str:
    d = report.to_dict()
    lines = [
        "Backtest",
        "========",
        f"period          {d['start']} -> {d['end']}  ({d['bars']} bars)",
        f"signals         {d['signals']}",
        f"trades          {d['trades']}",
        f"win rate        {report.win_rate * 100:.1f}%",
        f"avg R           {report.avg_r:+.3f}",
        f"expectancy      {report.expectancy_r:+.3f} R per trade",
        f"profit factor   {d['profit_factor']}",
        f"total P&L       {report.total_pnl:+.2f} ({report.total_return_pct:+.2f}%)",
        f"max drawdown    {report.max_drawdown[0]:.2f} ({report.max_drawdown[1]:.2f}%)",
    ]
    if report.skipped:
        lines.append("skipped rounds:")
        lines.extend(f"  {count:5d}  {reason}" for reason, count in report.skipped.most_common(10))
    if report.trades and report.count < 30:
        lines.append(f"note: only {report.count} trades; too few to judge the edge")
    return "\n".join(lines)
