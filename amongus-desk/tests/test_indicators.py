from __future__ import annotations

import math

import pytest

from amongus_desk.indicators import (
    atr_wilder,
    bollinger_bandwidth,
    donchian,
    ema,
    percentile_rank,
    rolling_std,
    sma,
    true_ranges,
)
from factories import make_bar

BARS = [
    make_bar(0, 10.0, 11.0, 9.0, 10.5),
    make_bar(1, 10.5, 12.0, 10.0, 11.5),  # TR = max(2, 1.5, 0.5) = 2
    make_bar(2, 11.5, 11.8, 10.2, 10.4),  # TR = max(1.6, 0.3, 1.3) = 1.6
    make_bar(3, 10.4, 10.6, 8.0, 8.5),  # TR = max(2.6, 0.2, 2.4) = 2.6
    make_bar(4, 8.5, 12.0, 8.4, 11.9),  # TR = max(3.6, 3.5, 0.1) = 3.6
]


def test_true_ranges_use_previous_close() -> None:
    assert true_ranges(BARS) == pytest.approx([2.0, 2.0, 1.6, 2.6, 3.6])
    gap = [make_bar(0, 10, 10.5, 9.5, 10), make_bar(1, 12, 12.5, 11.8, 12.2)]
    assert true_ranges(gap)[1] == pytest.approx(2.5)  # 12.5 - previous close 10
    assert true_ranges([]) == []


def test_atr_wilder_seed_and_smoothing() -> None:
    atr = atr_wilder(BARS, 3)
    assert atr[:2] == [None, None]
    assert atr[2] == pytest.approx((2.0 + 2.0 + 1.6) / 3)
    assert atr[3] == pytest.approx((atr[2] * 2 + 2.6) / 3)  # type: ignore[operator]
    assert atr[4] == pytest.approx((atr[3] * 2 + 3.6) / 3)  # type: ignore[operator]
    assert atr_wilder(BARS[:2], 3) == [None, None]


def test_sma_ema_std() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert sma(values, 3) == [None, None, pytest.approx(2.0), pytest.approx(3.0), pytest.approx(4.0)]
    e = ema(values, 3)
    assert e[2] == pytest.approx(2.0)
    assert e[3] == pytest.approx(0.5 * 4 + 0.5 * 2.0)
    assert e[4] == pytest.approx(0.5 * 5 + 0.5 * 3.0)
    std = rolling_std(values, 3)
    assert std[2] == pytest.approx(math.sqrt(2 / 3))
    assert ema([1.0], 3) == [None]


def test_donchian_excludes_current_bar() -> None:
    highs, lows = donchian(BARS, 2)
    assert highs[:2] == [None, None] and lows[:2] == [None, None]
    assert highs[2] == 12.0 and lows[2] == 9.0  # bars 0-1
    assert highs[4] == 11.8 and lows[4] == 8.0  # bars 2-3, NOT bar 4's own 12.0 high


def test_bollinger_bandwidth() -> None:
    closes = [10.0, 10.0, 10.0, 11.0, 9.0]
    bw = bollinger_bandwidth(closes, 3, k=2.0)
    assert bw[2] == pytest.approx(0.0)
    mid = 10.0
    std = math.sqrt(((10 - mid) ** 2 + (11 - mid) ** 2 + (9 - mid) ** 2) / 3)
    assert bw[4] == pytest.approx(4 * std / mid)


def test_percentile_rank() -> None:
    assert percentile_rank([1, 2, 3, 4], 2.5) == pytest.approx(0.5)
    assert percentile_rank([1, 2, 3, 4], 4) == pytest.approx(1.0)
    assert percentile_rank([1, 2, 3, 4], 0) == pytest.approx(0.0)
    assert percentile_rank([], 1) == 1.0


@pytest.mark.parametrize("fn", [sma, ema, rolling_std])
def test_invalid_period(fn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        fn([1.0, 2.0], 0)
    with pytest.raises(ValueError):
        atr_wilder(BARS, 0)
    with pytest.raises(ValueError):
        donchian(BARS, 0)
