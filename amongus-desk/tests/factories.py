"""Shared test builders. Import from tests: ``from factories import make_config, make_bar``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from amongus_desk.config import DeskConfig, InstrumentConfig
from amongus_desk.domain import Bar

T0 = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)  # a Monday, inside the London session


def eurusd(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": "EURUSD",
        "feed_symbol": "EUR/USD",
        "pip_size": 0.0001,
        "contract_size": 100000,
        "lot_step": "0.01",
        "min_lot": "0.01",
        "max_lot": "0.50",
        "typical_spread_pips": 1.0,
        "max_spread_pips": 3.0,
        "price_decimals": 5,
        "currencies": ["EUR", "USD"],
        "sessions": [{"days": [0, 1, 2, 3, 4], "start": "07:00", "end": "20:00"}],
        "specs_verified": True,
    }
    base.update(overrides)
    return base


def make_config(**overrides: Any) -> DeskConfig:
    raw: dict[str, Any] = {
        "instruments": [eurusd()],
        "risk": {"starting_equity": 10000, "max_lots_per_order": "0.50"},
    }
    for key, value in overrides.items():
        raw[key] = value
    return DeskConfig.model_validate(raw)


def make_instrument(**overrides: Any) -> InstrumentConfig:
    return InstrumentConfig.model_validate(eurusd(**overrides))


def make_bar(
    i: int,
    o: float,
    h: float,
    l: float,  # noqa: E741
    c: float,
    *,
    symbol: str = "EURUSD",
    tf: int = 5,
    start: datetime = T0,
) -> Bar:
    return Bar(symbol=symbol, start=start + timedelta(minutes=tf * i), timeframe_minutes=tf, open=o, high=h, low=l, close=c)


def lots(value: str) -> Decimal:
    return Decimal(value)
