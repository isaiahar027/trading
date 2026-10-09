"""The emergency meeting: runs the crew in order and records an Among Us-style transcript.

Order: Cyan → Lime → Yellow → Red → (Pink only if all four said PROCEED) → Chief. Claude is never paid for
a trade that is already dead. The transcript contains only computed facts.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Literal

from .config import DeskConfig, StrategyConfig
from .crew import BlackChief, CyanScout, LimeCharts, MeetingContext, PinkSentiment, RedSkeptic, YellowNews
from .domain import (
    CREW_DISPLAY,
    Bar,
    BreakoutSignal,
    CrewId,
    CrewReport,
    FinalAction,
    MeetingRecord,
    Side,
    SizingResult,
    TradePlan,
    Vote,
)
from .indicators import atr_wilder
from .llm.base import TradeReviewer
from .news import NewsCalendar
from .risk import RiskGate
from .strategy import ChartsPlanner, ScoutResult

log = logging.getLogger(__name__)

_ORDER = (
    CrewId.CYAN_SCOUT,
    CrewId.LIME_CHARTS,
    CrewId.YELLOW_NEWS,
    CrewId.RED_SKEPTIC,
    CrewId.PINK_SENTIMENT,
    CrewId.BLACK_CHIEF,
)


class EmergencyMeeting:
    def __init__(
        self,
        cfg: DeskConfig,
        *,
        planner: ChartsPlanner,
        calendar: NewsCalendar,
        gate: RiskGate,
        reviewer: TradeReviewer,
    ) -> None:
        self._cfg = cfg
        self.cyan = CyanScout()
        self.lime = LimeCharts(planner)
        self.yellow = YellowNews(calendar)
        self.red = RedSkeptic(cfg, gate)
        self.pink = PinkSentiment(cfg, reviewer)
        self.chief = BlackChief()

    async def convene(
        self,
        signal: BreakoutSignal,
        ctx: MeetingContext,
        *,
        trigger: Literal["breakout", "manual"] = "breakout",
        scout_report: CrewReport | None = None,
    ) -> MeetingRecord:
        cyan = scout_report or CrewReport(
            crew_id=CrewId.CYAN_SCOUT,
            vote=Vote.PROCEED,
            summary=f"{signal.side.value} breakout on {signal.symbol}: " + "; ".join(signal.reasons[:3]),
            details={"strength": signal.strength},
        )
        reports: list[CrewReport] = [cyan]
        plan: TradePlan | None = None
        sizing: SizingResult | None = None

        lime, plan = self.lime.assess(signal, ctx)
        reports.append(lime)
        reports.append(self.yellow.assess(ctx))
        if plan is not None:
            red, sizing = self.red.assess(plan, ctx)
            reports.append(red)
        else:
            reports.append(_skipped(CrewId.RED_SKEPTIC, "no plan to size"))

        if all(r.vote is Vote.PROCEED for r in reports) and plan is not None:
            reports.append(await self.pink.assess(signal, plan, ctx))
        else:
            reports.append(_skipped(CrewId.PINK_SENTIMENT, "the crew already ejected this trade"))

        decision, reasons = self.chief.decide(signal, reports, sizing)
        lots = sizing.lots if decision is not FinalAction.SKIP_ROUND and sizing is not None else None
        chief_summary = (
            f"{decision.value} {lots} lots {signal.symbol}"
            if lots is not None
            else f"SKIP_ROUND ({reasons[0]})"
            if reasons
            else "SKIP_ROUND"
        )
        reports.append(
            CrewReport(
                crew_id=CrewId.BLACK_CHIEF,
                vote=Vote.PROCEED if lots is not None else Vote.HOLD,
                summary=chief_summary,
                details={"reasons": reasons},
            )
        )
        record = MeetingRecord(
            symbol=signal.symbol,
            called_at=ctx.now,
            trigger=trigger,
            signal=signal,
            plan=plan,
            reports=reports,
            decision=decision,
            lots=lots,
            reasons=reasons,
            transcript=_transcript(ctx, signal.symbol, reports),
        )
        log.info("meeting %s %s -> %s %s", record.id, signal.symbol, decision.value, lots or "")
        return record

    async def no_signal_round(self, symbol: str, ctx: MeetingContext, scout_result: ScoutResult) -> MeetingRecord:
        """A manual meeting when Cyan has no breakout: nothing else runs and the round is skipped."""
        reports = [self.cyan.report(scout_result)]
        reports.extend(
            _skipped(c, "no breakout to discuss")
            for c in (CrewId.LIME_CHARTS, CrewId.YELLOW_NEWS, CrewId.RED_SKEPTIC, CrewId.PINK_SENTIMENT)
        )
        reason = f"Cyan Scout ejected: {scout_result.reason}"
        reports.append(
            CrewReport(crew_id=CrewId.BLACK_CHIEF, vote=Vote.HOLD, summary=f"SKIP_ROUND ({reason})", details={})
        )
        return MeetingRecord(
            symbol=symbol,
            called_at=ctx.now,
            trigger="manual",
            signal=None,
            plan=None,
            reports=reports,
            decision=FinalAction.SKIP_ROUND,
            lots=None,
            reasons=[reason],
            transcript=_transcript(ctx, symbol, reports),
        )


def build_forced_signal(bars: Sequence[Bar], side: Side, cfg: StrategyConfig) -> BreakoutSignal:
    """Paper-mode demo helper: a 'signal' on the last bar in the operator's chosen direction.

    Volatility figures are real (computed like the scout's); only the breakout itself is imposed.
    """
    if len(bars) < cfg.atr_slow_period + 2:
        raise ValueError(f"need at least {cfg.atr_slow_period + 2} bars of history")
    last = bars[-1]
    prior = bars[:-1]
    atr = atr_wilder(prior, cfg.atr_period)[-1]
    atr_slow = atr_wilder(prior, cfg.atr_slow_period)[-1]
    if atr is None or atr_slow is None or atr <= 0:
        raise ValueError("volatility not measurable yet")
    window = prior[-cfg.donchian_lookback :]
    return BreakoutSignal(
        symbol=last.symbol,
        side=side,
        bar_start=last.start,
        detected_at=last.end,
        close=last.close,
        channel_high=max(b.high for b in window),
        channel_low=min(b.low for b in window),
        atr=atr,
        atr_slow=atr_slow,
        range_atr=(last.high - last.low) / atr,
        extension_atr=0.0,
        squeeze_percentile=None,
        strength=0.0,
        reasons=[f"forced {side.value} by the operator (paper-mode demo, not a real breakout)"],
    )


def _skipped(crew_id: CrewId, why: str) -> CrewReport:
    return CrewReport(crew_id=crew_id, vote=Vote.HOLD, summary=f"skipped: {why}")


def _transcript(ctx: MeetingContext, symbol: str, reports: Sequence[CrewReport]) -> list[str]:
    stamp = f"[{ctx.now:%H:%M:%S}]"
    lines = [f"{stamp} SYSTEM: 🚨 EMERGENCY MEETING — {symbol} 🚨"]
    by_id = {r.crew_id: r for r in reports}
    for crew_id in _ORDER:
        report = by_id.get(crew_id)
        if report is None:
            continue
        name = CREW_DISPLAY[crew_id]["name"]
        lines.append(f"{stamp} {name}: [{report.vote.value}] {report.summary}")
    return lines
