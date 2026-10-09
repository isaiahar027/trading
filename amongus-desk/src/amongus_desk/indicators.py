"""Technical indicators as pure functions. Every output list is aligned with its input (``None`` until
there is enough data); nothing ever looks at data after the index it is computed for."""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Sequence

from .domain import Bar


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError("period must be >= 1")


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    """True range per bar; the first bar has no previous close, so it uses high - low."""
    out: list[float] = []
    prev_close: float | None = None
    for bar in bars:
        if prev_close is None:
            out.append(bar.high - bar.low)
        else:
            out.append(max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close)))
        prev_close = bar.close
    return out


def atr_wilder(bars: Sequence[Bar], period: int) -> list[float | None]:
    """Average true range with Wilder smoothing; seeded with the SMA of the first ``period`` true ranges."""
    _check_period(period)
    tr = true_ranges(bars)
    out: list[float | None] = [None] * len(tr)
    if len(tr) < period:
        return out
    value = sum(tr[:period]) / period
    out[period - 1] = value
    for i in range(period, len(tr)):
        value = (value * (period - 1) + tr[i]) / period
        out[i] = value
    return out


def sma(values: Sequence[float], period: int) -> list[float | None]:
    _check_period(period)
    out: list[float | None] = [None] * len(values)
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """Exponential moving average (alpha = 2 / (period + 1)), seeded with the SMA of the first ``period``."""
    _check_period(period)
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    value = sum(values[:period]) / period
    out[period - 1] = value
    for i in range(period, len(values)):
        value = alpha * values[i] + (1 - alpha) * value
        out[i] = value
    return out


def rolling_std(values: Sequence[float], period: int) -> list[float | None]:
    """Population standard deviation over a trailing window."""
    _check_period(period)
    out: list[float | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        out[i] = math.sqrt(max(0.0, sum((v - mean) ** 2 for v in window) / period))
    return out


def donchian(bars: Sequence[Bar], lookback: int) -> tuple[list[float | None], list[float | None]]:
    """Highest high / lowest low of the *previous* ``lookback`` bars at each index (current bar excluded)."""
    _check_period(lookback)
    highs: list[float | None] = [None] * len(bars)
    lows: list[float | None] = [None] * len(bars)
    for i in range(lookback, len(bars)):
        window = bars[i - lookback : i]
        highs[i] = max(b.high for b in window)
        lows[i] = min(b.low for b in window)
    return highs, lows


def bollinger_bandwidth(closes: Sequence[float], period: int, k: float = 2.0) -> list[float | None]:
    """(upper - lower) / middle of Bollinger bands = 2k * std / sma."""
    mids = sma(closes, period)
    stds = rolling_std(closes, period)
    out: list[float | None] = []
    for mid, std in zip(mids, stds, strict=True):
        out.append(None if mid is None or std is None or mid == 0 else (2 * k * std) / mid)
    return out


def percentile_rank(window: Sequence[float], value: float) -> float:
    """Share of ``window`` values that are <= ``value`` (0..1). An empty window gives 1.0 (no evidence)."""
    if not window:
        return 1.0
    ordered = sorted(window)
    return bisect_right(ordered, value) / len(ordered)
