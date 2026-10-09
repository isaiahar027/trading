"""Contract for Pink Sentiment's second opinion.

The reviewer can only approve or veto. Its output schema has no direction, size, stop or target
fields, so it cannot change the trade — that is a structural guarantee, not a prompt instruction.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..domain import BreakoutSignal, TradePlan, Vote


class ReviewVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["APPROVE", "VETO"]
    confidence: float = Field(ge=0.0, le=1.0)
    concerns: list[str] = Field(default_factory=list, max_length=5)
    summary: str = Field(max_length=600)


class ReviewContext(BaseModel):
    """Numeric context only; nothing here comes from untrusted free text."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe_minutes: int
    session: str  # e.g. "London/NY overlap" or "in session"
    recent_bars: list[dict[str, Any]]  # [{"t": iso, "o":..,"h":..,"l":..,"c":..}], oldest first
    spread_price: float
    open_positions: int
    trades_today: int
    daily_pnl_pct: float


class ReviewOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vote: Vote  # PROCEED (approve), EJECT (veto or fail-closed), HOLD (disabled / abstained)
    verdict: ReviewVerdict | None = None
    error: str | None = None
    model: str | None = None
    request_id: str | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class TradeReviewer(Protocol):
    name: str

    async def review(self, signal: BreakoutSignal, plan: TradePlan, context: ReviewContext) -> ReviewOutcome:
        """Never raises for API/model problems; maps them to an outcome per the failure policy."""
        ...

    def usage_today(self) -> dict[str, Any]:
        """Calls/tokens used in the current UTC day (for the dashboard and the daily call cap)."""
        ...


class DisabledReviewer:
    name = "disabled"

    async def review(self, signal: BreakoutSignal, plan: TradePlan, context: ReviewContext) -> ReviewOutcome:
        return ReviewOutcome(vote=Vote.HOLD, error=None, model=None)

    def usage_today(self) -> dict[str, Any]:
        return {"enabled": False, "calls": 0}
