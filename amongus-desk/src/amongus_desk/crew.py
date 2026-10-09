"""The six crewmates. Each turns one component's verdict into a :class:`CrewReport` (vote + summary + numbers).

Votes: PROCEED (fine by me), EJECT (veto: the round ends with SKIP_ROUND), HOLD (abstain / not evaluated).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import DeskConfig, InstrumentConfig
from .domain import (
    Bar,
    BreakoutSignal,
    CrewId,
    CrewReport,
    FinalAction,
    Mode,
    Quote,
    RiskSnapshot,
    Side,
    SizingResult,
    TradePlan,
    Vote,
)
from .llm.base import ReviewContext, TradeReviewer
from .news import NewsCalendar
from .risk import RiskGate, size_position
from .strategy import ChartsPlanner, ScoutResult

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MeetingContext:
    now: datetime
    mode: Mode
    instrument: InstrumentConfig
    bars: Sequence[Bar]  # this symbol's history, oldest first; the last bar just closed
    quote: Quote | None
    snapshot: RiskSnapshot
    last_bar_end: datetime | None

    @property
    def spread_price(self) -> float:
        return self.quote.spread if self.quote is not None else self.instrument.typical_spread_price


def _json_safe(metrics: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in metrics.items():
        if isinstance(value, float):
            out[key] = round(value, 8)
        elif isinstance(value, int | str | bool) or value is None:
            out[key] = value
        else:
            out[key] = str(value)
    return out


class CyanScout:
    """Reports the breakout scanner's finding."""

    def report(self, result: ScoutResult) -> CrewReport:
        if result.signal is None:
            return CrewReport(
                crew_id=CrewId.CYAN_SCOUT, vote=Vote.EJECT, summary=result.reason, details=_json_safe(result.metrics)
            )
        sig = result.signal
        summary = f"{sig.side.value} breakout on {sig.symbol}: " + "; ".join(sig.reasons[:3])
        details = _json_safe({**result.metrics, "strength": sig.strength})
        return CrewReport(crew_id=CrewId.CYAN_SCOUT, vote=Vote.PROCEED, summary=summary, details=details)


class LimeCharts:
    """Trend filter and trade plan."""

    def __init__(self, planner: ChartsPlanner) -> None:
        self._planner = planner

    def assess(self, signal: BreakoutSignal, ctx: MeetingContext) -> tuple[CrewReport, TradePlan | None]:
        result = self._planner.plan(signal, ctx.bars, ctx.instrument, ctx.spread_price)
        summary = result.reason
        if result.plan is not None and result.plan.notes:
            summary = f"{result.reason} ({'; '.join(result.plan.notes[:2])})"
        report = CrewReport(
            crew_id=CrewId.LIME_CHARTS, vote=result.vote, summary=summary, details=_json_safe(result.metrics)
        )
        return report, result.plan


class YellowNews:
    """Sessions, weekend, rollover and scheduled-news blackouts."""

    def __init__(self, calendar: NewsCalendar) -> None:
        self._calendar = calendar

    def assess(self, ctx: MeetingContext) -> CrewReport:
        check = self._calendar.check(ctx.instrument, ctx.now)
        details: dict[str, Any] = {"reasons": check.reasons}
        nxt = check.next_event
        if nxt is not None:
            details["next_event"] = {"at": nxt.at.isoformat(), "title": nxt.title, "currencies": nxt.currencies}
        if not check.ok:
            return CrewReport(
                crew_id=CrewId.YELLOW_NEWS, vote=Vote.EJECT, summary="; ".join(check.reasons), details=details
            )
        summary = "all clear: in session, no rollover, no news blackout"
        if nxt is not None:
            summary += f" (next: {nxt.title} at {nxt.at:%a %H:%M} UTC)"
        return CrewReport(crew_id=CrewId.YELLOW_NEWS, vote=Vote.PROCEED, summary=summary, details=details)


class RedSkeptic:
    """Sizes the trade and runs every hard risk limit."""

    def __init__(self, cfg: DeskConfig, gate: RiskGate) -> None:
        self._cfg = cfg
        self._gate = gate

    def assess(self, plan: TradePlan, ctx: MeetingContext) -> tuple[CrewReport, SizingResult]:
        sizing = size_position(plan, ctx.instrument, self._cfg.risk, ctx.snapshot.equity)
        gate = self._gate.check(
            plan,
            ctx.instrument,
            sizing,
            ctx.snapshot,
            last_bar_end=ctx.last_bar_end,
            spread_price=ctx.spread_price,
            mode=ctx.mode,
        )
        details = {**gate.details, "sizing": sizing.reason}
        if not gate.ok:
            return (
                CrewReport(
                    crew_id=CrewId.RED_SKEPTIC, vote=Vote.EJECT, summary="; ".join(gate.violations), details=details
                ),
                sizing,
            )
        ccy = self._cfg.risk.account_currency
        assert sizing.lots is not None
        at_risk = float(sizing.lots) * sizing.per_lot_risk
        summary = (
            f"{sizing.lots} lots, about {at_risk:.2f} {ccy} at the stop "
            f"({self._cfg.risk.risk_per_trade_pct:g}% budget of {sizing.equity:.2f} est. equity)"
        )
        if sizing.capped_by:
            summary += f", capped by {sizing.capped_by}"
        return CrewReport(crew_id=CrewId.RED_SKEPTIC, vote=Vote.PROCEED, summary=summary, details=details), sizing


class PinkSentiment:
    """Claude's veto-only second opinion."""

    def __init__(self, cfg: DeskConfig, reviewer: TradeReviewer) -> None:
        self._cfg = cfg
        self._reviewer = reviewer

    async def assess(self, signal: BreakoutSignal, plan: TradePlan, ctx: MeetingContext) -> CrewReport:
        if not self._cfg.llm.enabled:
            return CrewReport(
                crew_id=CrewId.PINK_SENTIMENT, vote=Vote.HOLD, summary="Claude review is off (llm.enabled: false)"
            )
        outcome = await self._reviewer.review(signal, plan, self._context(ctx))
        details: dict[str, Any] = {
            "model": outcome.model,
            "request_id": outcome.request_id,
            "latency_ms": outcome.latency_ms,
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "error": outcome.error,
        }
        if outcome.verdict is not None:
            v = outcome.verdict
            details.update(verdict=v.verdict, confidence=v.confidence, concerns=v.concerns)
            summary = f"{v.verdict} ({v.confidence:.2f}): {v.summary}"
            if v.concerns:
                summary += " Concerns: " + "; ".join(v.concerns)
        elif outcome.error:
            action = "vetoing" if outcome.vote is Vote.EJECT else "abstaining"
            summary = f"review unavailable, {action} per failure_policy: {outcome.error}"
        else:
            summary = "no opinion"
        return CrewReport(crew_id=CrewId.PINK_SENTIMENT, vote=outcome.vote, summary=summary, details=details)

    def _context(self, ctx: MeetingContext) -> ReviewContext:
        digits = ctx.instrument.price_decimals
        recent = [
            {
                "t": b.start.isoformat(),
                "o": round(b.open, digits),
                "h": round(b.high, digits),
                "l": round(b.low, digits),
                "c": round(b.close, digits),
            }
            for b in list(ctx.bars)[-self._cfg.llm.recent_bars :]
        ]
        snap = ctx.snapshot
        day_pnl = snap.realized_pnl_today + snap.unrealized_pnl
        pct = (day_pnl / snap.day_start_equity * 100) if snap.day_start_equity > 0 else 0.0
        return ReviewContext(
            symbol=ctx.instrument.symbol,
            timeframe_minutes=self._cfg.strategy.timeframe_minutes,
            session=f"in session, {ctx.now:%a %H:%M} UTC",
            recent_bars=recent,
            spread_price=ctx.spread_price,
            open_positions=len(snap.open_positions),
            trades_today=snap.trades_today,
            daily_pnl_pct=round(pct, 4),
        )


class BlackChief:
    """Any EJECT ends the round; otherwise the deterministic crew's plan executes at Red's size."""

    def decide(
        self, signal: BreakoutSignal | None, reports: Sequence[CrewReport], sizing: SizingResult | None
    ) -> tuple[FinalAction, list[str]]:
        names = {
            CrewId.CYAN_SCOUT: "Cyan Scout",
            CrewId.LIME_CHARTS: "Lime Charts",
            CrewId.YELLOW_NEWS: "Yellow News",
            CrewId.RED_SKEPTIC: "Red Skeptic",
            CrewId.PINK_SENTIMENT: "Pink Sentiment",
        }
        ejections = [
            f"{names.get(r.crew_id, r.crew_id.value)} ejected: {r.summary}" for r in reports if r.vote is Vote.EJECT
        ]
        if ejections:
            return FinalAction.SKIP_ROUND, ejections
        votes = {r.crew_id: r.vote for r in reports}
        required = (CrewId.CYAN_SCOUT, CrewId.LIME_CHARTS, CrewId.YELLOW_NEWS, CrewId.RED_SKEPTIC)
        missing = [names[c] for c in required if votes.get(c) is not Vote.PROCEED]
        if missing:
            return FinalAction.SKIP_ROUND, [f"no PROCEED from {', '.join(missing)}"]
        if signal is None or sizing is None or sizing.lots is None:
            return FinalAction.SKIP_ROUND, ["no tradable size"]
        action = FinalAction.EXECUTE_BUY if signal.side is Side.BUY else FinalAction.EXECUTE_SELL
        return action, [f"unanimous: {action.value} {sizing.lots} lots"]
