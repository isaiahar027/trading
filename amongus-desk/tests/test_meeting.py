from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from amongus_desk.config import DeskConfig
from amongus_desk.crew import MeetingContext
from amongus_desk.domain import BreakoutSignal, CrewId, FinalAction, Mode, RiskSnapshot, Side, TradePlan, Vote
from amongus_desk.llm.base import DisabledReviewer, ReviewContext, ReviewOutcome, ReviewVerdict
from amongus_desk.meeting import EmergencyMeeting, build_forced_signal
from amongus_desk.news import NewsCalendar
from amongus_desk.risk import RiskGate
from amongus_desk.strategy import BreakoutScout, ChartsPlanner
from factories import SMALL_STRATEGY, breakout_series, make_config


class FakeReviewer:
    name = "fake"

    def __init__(self, vote: Vote = Vote.PROCEED, error: str | None = None) -> None:
        self.vote = vote
        self.error = error
        self.calls: list[tuple[BreakoutSignal, TradePlan, ReviewContext]] = []

    async def review(self, signal: BreakoutSignal, plan: TradePlan, context: ReviewContext) -> ReviewOutcome:
        self.calls.append((signal, plan, context))
        if self.error:
            return ReviewOutcome(vote=self.vote, error=self.error)
        verdict = ReviewVerdict(
            verdict="APPROVE" if self.vote is Vote.PROCEED else "VETO",
            confidence=0.8,
            concerns=[] if self.vote is Vote.PROCEED else ["spike"],
            summary="looks fine" if self.vote is Vote.PROCEED else "stop run",
        )
        return ReviewOutcome(vote=self.vote, verdict=verdict, model="claude-opus-5-5")

    def usage_today(self) -> dict[str, Any]:
        return {"enabled": True, "calls": len(self.calls)}


def cfg(**over: Any) -> DeskConfig:
    raw: dict[str, Any] = {
        "strategy": {**SMALL_STRATEGY, "min_stop_spread_multiple": 2.0},
        "risk": {"starting_equity": 10_000, "max_lots_per_order": "0.50", "risk_per_trade_pct": 0.5},
        "llm": {"enabled": True},
    }
    raw.update(over)
    return make_config(**raw)


def snapshot(now: datetime, **over: Any) -> RiskSnapshot:
    data: dict[str, Any] = {
        "now": now,
        "equity": 10_000.0,
        "day_start_equity": 10_000.0,
        "realized_pnl_today": 0.0,
        "unrealized_pnl": 0.0,
        "trades_today": 0,
        "open_positions": [],
        "consecutive_losses": 0,
        "last_trade_at": None,
        "last_loss_at": None,
    }
    data.update(over)
    return RiskSnapshot.model_validate(data)


def setup(c: DeskConfig, reviewer: Any, tmp_path: Any, direction: int = 1, **snap: Any):  # type: ignore[no-untyped-def]
    bars = breakout_series(direction)
    result = BreakoutScout(c.strategy).evaluate(bars)
    assert result.signal is not None, result.reason
    now = bars[-1].end + timedelta(seconds=3)
    ctx = MeetingContext(
        now=now,
        mode=c.mode,
        instrument=c.instrument("EURUSD"),
        bars=bars,
        quote=None,
        snapshot=snapshot(now, **snap),
        last_bar_end=bars[-1].end,
    )
    meeting = EmergencyMeeting(
        c,
        planner=ChartsPlanner(c.strategy),
        calendar=NewsCalendar(c.news, tmp_path),
        gate=RiskGate(c.risk, c.strategy),
        reviewer=reviewer,
    )
    return meeting, result.signal, ctx, bars


@pytest.mark.anyio
@pytest.mark.parametrize("direction", [1, -1])
async def test_unanimous_meeting_executes(tmp_path, direction: int) -> None:  # type: ignore[no-untyped-def]
    reviewer = FakeReviewer(Vote.PROCEED)
    meeting, signal, ctx, _ = setup(cfg(), reviewer, tmp_path, direction)
    record = await meeting.convene(signal, ctx)
    expected = FinalAction.EXECUTE_BUY if direction > 0 else FinalAction.EXECUTE_SELL
    assert record.decision is expected, record.reasons
    assert record.lots is not None and record.lots > 0
    assert record.plan is not None and record.signal == signal
    votes = {r.crew_id: r.vote for r in record.reports}
    assert votes[CrewId.PINK_SENTIMENT] is Vote.PROCEED and len(reviewer.calls) == 1
    assert [r.crew_id for r in record.reports] == [
        CrewId.CYAN_SCOUT,
        CrewId.LIME_CHARTS,
        CrewId.YELLOW_NEWS,
        CrewId.RED_SKEPTIC,
        CrewId.PINK_SENTIMENT,
        CrewId.BLACK_CHIEF,
    ]
    assert record.transcript[0].endswith("SYSTEM: 🚨 EMERGENCY MEETING — EURUSD 🚨")
    assert any("Black Chief: [PROCEED] EXECUTE_" in line for line in record.transcript)
    # Claude only saw numeric context about this symbol
    _, _, review_ctx = reviewer.calls[0]
    assert review_ctx.symbol == "EURUSD" and len(review_ctx.recent_bars) == 30


@pytest.mark.anyio
async def test_pink_veto_skips(tmp_path) -> None:  # type: ignore[no-untyped-def]
    meeting, signal, ctx, _ = setup(cfg(), FakeReviewer(Vote.EJECT), tmp_path)
    record = await meeting.convene(signal, ctx)
    assert record.decision is FinalAction.SKIP_ROUND and record.lots is None
    assert any("Pink Sentiment ejected" in r for r in record.reasons)


@pytest.mark.anyio
async def test_pink_not_called_when_crew_already_ejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    reviewer = FakeReviewer(Vote.PROCEED)
    meeting, signal, ctx, _ = setup(cfg(), reviewer, tmp_path, kill_switch=True, kill_reason="test")
    record = await meeting.convene(signal, ctx)
    assert record.decision is FinalAction.SKIP_ROUND
    assert reviewer.calls == []
    pink = next(r for r in record.reports if r.crew_id is CrewId.PINK_SENTIMENT)
    assert pink.vote is Vote.HOLD and "skipped" in pink.summary
    assert any("Red Skeptic ejected: kill switch" in r for r in record.reasons)


@pytest.mark.anyio
async def test_llm_disabled_holds_but_trade_proceeds(tmp_path) -> None:  # type: ignore[no-untyped-def]
    meeting, signal, ctx, _ = setup(cfg(llm={"enabled": False}), DisabledReviewer(), tmp_path)
    record = await meeting.convene(signal, ctx)
    pink = next(r for r in record.reports if r.crew_id is CrewId.PINK_SENTIMENT)
    assert pink.vote is Vote.HOLD and "off" in pink.summary
    assert record.decision is FinalAction.EXECUTE_BUY


@pytest.mark.anyio
async def test_review_failure_vetoes_with_reason(tmp_path) -> None:  # type: ignore[no-untyped-def]
    meeting, signal, ctx, _ = setup(cfg(), FakeReviewer(Vote.EJECT, error="cannot reach the Claude API"), tmp_path)
    record = await meeting.convene(signal, ctx)
    pink = next(r for r in record.reports if r.crew_id is CrewId.PINK_SENTIMENT)
    assert pink.vote is Vote.EJECT and "unavailable, vetoing" in pink.summary
    assert record.decision is FinalAction.SKIP_ROUND


@pytest.mark.anyio
async def test_lime_eject_skips_red(tmp_path) -> None:  # type: ignore[no-untyped-def]
    c = cfg(strategy={**SMALL_STRATEGY, "min_stop_spread_multiple": 100.0})
    meeting, signal, ctx, _ = setup(c, FakeReviewer(), tmp_path)
    record = await meeting.convene(signal, ctx)
    red = next(r for r in record.reports if r.crew_id is CrewId.RED_SKEPTIC)
    assert red.vote is Vote.HOLD and record.plan is None and record.decision is FinalAction.SKIP_ROUND


@pytest.mark.anyio
async def test_yellow_blocks_outside_session(tmp_path) -> None:  # type: ignore[no-untyped-def]
    meeting, signal, ctx, _ = setup(cfg(), FakeReviewer(), tmp_path)
    night = datetime(2026, 10, 6, 3, 0, tzinfo=UTC)
    ctx = MeetingContext(
        now=night,
        mode=ctx.mode,
        instrument=ctx.instrument,
        bars=ctx.bars,
        quote=None,
        snapshot=snapshot(night),
        last_bar_end=night,
    )
    record = await meeting.convene(signal, ctx)
    yellow = next(r for r in record.reports if r.crew_id is CrewId.YELLOW_NEWS)
    assert yellow.vote is Vote.EJECT and "outside" in yellow.summary


@pytest.mark.anyio
async def test_no_signal_round(tmp_path) -> None:  # type: ignore[no-untyped-def]
    meeting, _, ctx, bars = setup(cfg(), FakeReviewer(), tmp_path)
    result = BreakoutScout(cfg().strategy).evaluate(bars[:-1])
    record = await meeting.no_signal_round("EURUSD", ctx, result)
    assert record.decision is FinalAction.SKIP_ROUND and record.trigger == "manual"
    assert record.reports[0].vote is Vote.EJECT and len(record.reports) == 6


def test_forced_signal_uses_real_volatility() -> None:
    bars = breakout_series(1)
    sig = build_forced_signal(bars, Side.SELL, cfg().strategy)
    assert sig.side is Side.SELL and sig.atr > 0 and sig.atr_slow > 0
    assert "forced" in sig.reasons[0]
    with pytest.raises(ValueError):
        build_forced_signal(bars[:5], Side.BUY, cfg().strategy)


def test_mode_field_flows_into_context() -> None:
    assert cfg().mode is Mode.PAPER
