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
    return Bar(
        symbol=symbol, start=start + timedelta(minutes=tf * i), timeframe_minutes=tf, open=o, high=h, low=l, close=c
    )


def lots(value: str) -> Decimal:
    return Decimal(value)


# ----------------------------------------------------------------------------------- breakout scenarios

SMALL_STRATEGY: dict[str, Any] = {
    "donchian_lookback": 10,
    "atr_period": 5,
    "atr_slow_period": 20,
    "squeeze_period": 10,
    "squeeze_lookback": 20,
    "trend_ema_period": 20,
    "trend_slope_bars": 3,
    "signal_cooldown_bars": 4,
}


def base_series(direction: int = 1, n_noisy: int = 25, n_quiet: int = 12, *, symbol: str = "EURUSD") -> list[Bar]:
    """Noisy drift, then a tight squeeze drifting in ``direction``. Prices around 1.1000."""
    bars: list[Bar] = []
    price = 1.1000
    for i in range(n_noisy):
        o = price
        c = price + direction * 0.00010 + (0.0004 if i % 2 else -0.0004)
        bars.append(make_bar(i, o, max(o, c) + 0.0003, min(o, c) - 0.0003, c, symbol=symbol))
        price = c
    for j in range(n_quiet):
        o = price
        c = price + direction * 0.00002
        bars.append(make_bar(n_noisy + j, o, max(o, c) + 0.0001, min(o, c) - 0.0001, c, symbol=symbol))
        price = c
    return bars


def breakout_bar(bars: list[Bar], direction: int = 1, *, size: float = 0.0006, body: float = 0.8) -> Bar:
    """A strong bar closing beyond the recent 10-bar channel."""
    window = bars[-10:]
    edge = max(b.high for b in window) if direction > 0 else min(b.low for b in window)
    o = edge - direction * 0.00005
    c = o + direction * size * body
    hi = max(o, c) + size * (1 - body) / 2
    lo = min(o, c) - size * (1 - body) / 2
    return make_bar(len(bars), o, hi, lo, c, symbol=bars[-1].symbol)


def breakout_series(direction: int = 1, *, symbol: str = "EURUSD") -> list[Bar]:
    bars = base_series(direction, symbol=symbol)
    bars.append(breakout_bar(bars, direction))
    return bars
