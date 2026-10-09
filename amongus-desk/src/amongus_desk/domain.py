"""Shared domain types.

Every module talks in these types. Conventions:

* All datetimes are timezone-aware UTC. Naive datetimes are rejected.
* Prices are ``float`` (analytics); lot sizes are ``Decimal`` so they can be quantized exactly to a
  broker's lot step and typed into the terminal without float artefacts (``0.30000000000000004``).
* Money amounts are estimates in the account currency unless a field says otherwise. The desk can not
  read the broker's ledger (UniTrader has no API), so P&L is always labelled "estimated".
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return value.astimezone(UTC)


class Side(enum.StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class Mode(enum.StrEnum):
    PAPER = "paper"  # simulated venue, no GUI automation at all
    DEMO = "demo"  # UniTrader GUI, account badge must show the calibrated DEMO account
    LIVE = "live"  # UniTrader GUI on a real-money account; needs an explicit acknowledgement


class CrewId(enum.StrEnum):
    CYAN_SCOUT = "CYAN_SCOUT"  # volatility breakout detector; calls the emergency meeting
    LIME_CHARTS = "LIME_CHARTS"  # trend filter + trade plan (entry/stop/target)
    YELLOW_NEWS = "YELLOW_NEWS"  # sessions, weekends, rollover, scheduled-news blackouts
    RED_SKEPTIC = "RED_SKEPTIC"  # hard risk limits and system health; sizes the position
    PINK_SENTIMENT = "PINK_SENTIMENT"  # Claude second opinion; can only veto
    BLACK_CHIEF = "BLACK_CHIEF"  # aggregates votes; any EJECT ends the round


CREW_DISPLAY: dict[CrewId, dict[str, str]] = {
    CrewId.CYAN_SCOUT: {"name": "Cyan Scout", "color": "#38fedc", "role": "Volatility breakout scanner"},
    CrewId.LIME_CHARTS: {"name": "Lime Charts", "color": "#50ef39", "role": "Trend filter and trade plan"},
    CrewId.YELLOW_NEWS: {"name": "Yellow News", "color": "#f5f557", "role": "Sessions and news blackouts"},
    CrewId.RED_SKEPTIC: {"name": "Red Skeptic", "color": "#c51111", "role": "Risk limits and sizing"},
    CrewId.PINK_SENTIMENT: {"name": "Pink Sentiment", "color": "#ed54ba", "role": "Claude veto-only review"},
    CrewId.BLACK_CHIEF: {"name": "Black Chief", "color": "#3f474e", "role": "Final decision"},
}


class Vote(enum.StrEnum):
    PROCEED = "PROCEED"
    EJECT = "EJECT"  # veto: the round ends with SKIP_ROUND
    HOLD = "HOLD"  # abstain / not evaluated


class FinalAction(enum.StrEnum):
    EXECUTE_BUY = "EXECUTE_BUY"
    EXECUTE_SELL = "EXECUTE_SELL"
    SKIP_ROUND = "SKIP_ROUND"


class IntentStatus(enum.StrEnum):
    PENDING = "PENDING"  # persisted; the venue has not been asked yet
    SUBMITTING = "SUBMITTING"  # write-ahead marker set immediately before the irreversible click
    CONFIRMED = "CONFIRMED"  # venue verified the order went through
    UNCONFIRMED = "UNCONFIRMED"  # click happened but the result could not be verified -> reconcile
    ABORTED = "ABORTED"  # stopped before any click; nothing was sent to the broker
    REJECTED = "REJECTED"  # venue saw an explicit rejection (error dialog / paper rejection)
    DRY_RUN = "DRY_RUN"  # every check passed; the click was skipped on purpose
    RECONCILED_FILLED = "RECONCILED_FILLED"  # operator confirmed it filled
    RECONCILED_NOT_FILLED = "RECONCILED_NOT_FILLED"  # operator confirmed nothing happened

    @property
    def is_final(self) -> bool:
        return self not in (IntentStatus.PENDING, IntentStatus.SUBMITTING)

    @property
    def needs_reconcile(self) -> bool:
        return self in (IntentStatus.SUBMITTING, IntentStatus.UNCONFIRMED)


class PositionStatus(enum.StrEnum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"  # a close order is in flight
    CLOSED = "CLOSED"
    RECONCILE = "RECONCILE"  # state unknown; operator must check the terminal


class ExitReason(enum.StrEnum):
    STOP = "STOP"
    TARGET = "TARGET"
    TIME = "TIME"
    MANUAL = "MANUAL"
    PANIC = "PANIC"
    DAILY_LOSS = "DAILY_LOSS"
    RECONCILED = "RECONCILED"


IntentPurpose = Literal["open", "close", "close_all"]
PriceSource = Literal["venue", "feed_estimate", "operator", "none"]


@dataclass(frozen=True, slots=True)
class Bar:
    """A *closed* OHLC bar. ``start`` is the bar's open time; the bar covers [start, end)."""

    symbol: str
    start: datetime
    timeframe_minutes: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None

    def __post_init__(self) -> None:
        ensure_utc(self.start)
        if self.timeframe_minutes <= 0:
            raise ValueError("timeframe_minutes must be positive")
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise ValueError(f"inconsistent OHLC for {self.symbol} at {self.start.isoformat()}")
        if self.low <= 0:
            raise ValueError("prices must be positive")

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.timeframe_minutes)

    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    time: datetime

    def __post_init__(self) -> None:
        ensure_utc(self.time)
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            raise ValueError(f"invalid quote for {self.symbol}: bid={self.bid} ask={self.ask}")

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


class Record(BaseModel):
    """Base for persisted / API-visible records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def _aware_datetimes(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            return ensure_utc(value)
        return value


class BreakoutSignal(Record):
    symbol: str
    side: Side
    bar_start: datetime  # start of the breakout bar
    detected_at: datetime  # end of the breakout bar (when it closed)
    close: float
    channel_high: float
    channel_low: float
    atr: float  # fast ATR at the bar *before* the breakout bar (no look-ahead into the trigger bar)
    atr_slow: float
    range_atr: float  # breakout bar true range / atr
    extension_atr: float  # distance of close beyond the channel edge, in "vol" units = max(atr, atr_slow)
    squeeze_percentile: float | None  # Bollinger bandwidth percentile before the breakout (0..1)
    strength: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)


class TradePlan(Record):
    symbol: str
    side: Side
    entry_ref: float  # reference price (feed close); the real fill happens at the broker's bid/ask
    stop_price: float
    target_price: float
    stop_distance: float  # absolute price distance entry_ref -> stop_price (> 0)
    atr: float  # the volatility unit the stop was built from: max(fast ATR, slow ATR)
    target_r: float
    time_stop_bars: int
    spread_price: float  # spread assumed for sizing/cost, in price units
    notes: list[str] = Field(default_factory=list)


class CrewReport(Record):
    crew_id: CrewId
    vote: Vote
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)


class SizingResult(Record):
    lots: Decimal | None  # None means "do not trade" (below min lot, invalid inputs, ...)
    equity: float
    risk_amount: float  # account-currency amount risked if the stop is hit
    per_lot_risk: float  # account-currency loss per 1.0 lot at the stop, including spread
    capped_by: str | None = None  # which cap reduced the size, if any
    reason: str


class MeetingRecord(Record):
    id: str = Field(default_factory=new_id)
    symbol: str
    called_at: datetime
    trigger: Literal["breakout", "manual"]
    signal: BreakoutSignal | None
    plan: TradePlan | None
    reports: list[CrewReport]
    decision: FinalAction
    lots: Decimal | None
    reasons: list[str] = Field(default_factory=list)
    transcript: list[str] = Field(default_factory=list)


class OrderIntent(Record):
    id: str = Field(default_factory=new_id)
    purpose: IntentPurpose = "open"
    meeting_id: str | None
    position_id: str | None = None  # set for purpose="close"
    symbol: str  # "*" for close_all
    side: Side | None  # None for close_all
    lots: Decimal | None  # None for close_all
    entry_ref: float | None
    stop_price: float | None
    target_price: float | None
    mode: Mode
    created_at: datetime


class ExecutionReport(Record):
    intent_id: str
    status: IntentStatus
    venue: str
    started_at: datetime
    finished_at: datetime
    clicked_at: datetime | None = None
    fill_price: float | None = None
    fill_price_source: PriceSource = "none"
    message: str
    stage: str | None = None  # step at which the flow stopped (e.g. "verify_panel_label")
    operator_abort: bool = False  # the operator triggered the GUI fail-safe
    evidence: dict[str, Any] = Field(default_factory=dict)


class Position(Record):
    id: str = Field(default_factory=new_id)
    intent_id: str
    symbol: str
    side: Side
    lots: Decimal
    entry_price: float
    entry_price_source: PriceSource
    stop_price: float
    target_price: float
    opened_at: datetime
    status: PositionStatus = PositionStatus.OPEN
    protective_at_broker: bool = False  # SL/TP were typed into the terminal's order panel
    time_stop_bars: int
    bars_held: int = 0
    last_price: float | None = None
    exit_price: float | None = None
    exit_price_source: PriceSource = "none"
    closed_at: datetime | None = None
    exit_reason: ExitReason | None = None
    pnl: float | None = None  # realized, estimated, account currency
    close_intent_id: str | None = None


class RiskSnapshot(Record):
    """Everything the risk gate needs, captured at one instant."""

    now: datetime
    equity: float  # estimated current equity (starting equity + realized + unrealized estimates)
    day_start_equity: float
    realized_pnl_today: float
    unrealized_pnl: float
    trades_today: int
    open_positions: list[Position]
    consecutive_losses: int
    last_trade_at: datetime | None
    last_loss_at: datetime | None
    last_loss_at_by_symbol: dict[str, datetime] = Field(default_factory=dict)
    kill_switch: bool = False
    kill_reason: str | None = None
    reconcile_pending: list[str] = Field(default_factory=list)  # intent / position ids
    degraded: list[str] = Field(default_factory=list)  # human-readable health problems


class HealthCheck(Record):
    name: str
    ok: bool
    detail: str
    data: dict[str, Any] = Field(default_factory=dict)


class VenueHealth(Record):
    venue: str
    ok: bool
    checked_at: datetime
    checks: list[HealthCheck]

    @property
    def failures(self) -> list[HealthCheck]:
        return [c for c in self.checks if not c.ok]


class DeskEvent(Record):
    type: str
    at: datetime
    data: dict[str, Any] = Field(default_factory=dict)
