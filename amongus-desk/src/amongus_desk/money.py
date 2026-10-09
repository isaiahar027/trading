"""Lot arithmetic, P&L estimates and trading-day boundaries shared by risk, positions and venues."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from .config import InstrumentConfig
from .domain import Side, ensure_utc


def quantize_lots(value: Decimal, step: Decimal) -> Decimal:
    """Round ``value`` DOWN to a multiple of ``step`` (never up: rounding up would exceed the risk budget)."""
    if step <= 0:
        raise ValueError("lot step must be positive")
    if value <= 0:
        return Decimal(0).quantize(step)
    steps = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return (steps * step).quantize(step)


def format_lots(lots: Decimal, step: Decimal, decimal_separator: str = ".") -> str:
    """Render lots with exactly as many decimals as the lot step (``0.1`` with step ``0.01`` -> ``"0.10"``)."""
    if decimal_separator not in (".", ","):
        raise ValueError("decimal separator must be '.' or ','")
    if lots <= 0:
        raise ValueError("lots must be positive")
    if (lots % step) != 0:
        raise ValueError(f"{lots} is not a multiple of the lot step {step}")
    text = format(lots.quantize(step), "f")
    return text.replace(".", decimal_separator) if decimal_separator != "." else text


def parse_lots(text: str) -> Decimal | None:
    """Parse lots read back from the terminal (``"0,10"``, ``" 0.10 "``); ``None`` if it is not a number."""
    cleaned = text.strip().replace(" ", "").replace(" ", "")
    if not cleaned:
        return None
    if cleaned.count(",") == 1 and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def estimate_pnl(side: Side, lots: Decimal, entry: float, exit_price: float, instrument: InstrumentConfig) -> float:
    """Estimated P&L in the account currency (no commission/swap: the desk cannot read the broker ledger)."""
    move = (exit_price - entry) * side.sign
    return move * float(lots) * instrument.contract_size * instrument.quote_to_account_rate


def trading_day_start(now: datetime, rollover: time) -> datetime:
    """Start of the trading day containing ``now``, for a daily boundary at ``rollover`` (UTC)."""
    now = ensure_utc(now)
    boundary = now.replace(hour=rollover.hour, minute=rollover.minute, second=0, microsecond=0)
    return boundary if now >= boundary else boundary - timedelta(days=1)
