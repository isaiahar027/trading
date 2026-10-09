from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from amongus_desk.config import StrategyConfig
from amongus_desk.domain import Bar, Side, Vote
from amongus_desk.strategy import BarHistory, BreakoutScout, ChartsPlanner
from factories import T0, make_bar, make_instrument

SMALL: dict[str, Any] = {
    "donchian_lookback": 10,
    "atr_period": 5,
    "atr_slow_period": 20,
    "squeeze_period": 10,
    "squeeze_lookback": 20,
    "trend_ema_period": 20,
    "trend_slope_bars": 3,
    "signal_cooldown_bars": 4,
}


def scfg(**over: Any) -> StrategyConfig:
    return StrategyConfig.model_validate({**SMALL, **over})


def base_series(direction: int = 1, n_noisy: int = 25, n_quiet: int = 12) -> list[Bar]:
    """Noisy drift, then a tight squeeze drifting in ``direction``. Prices around 1.1000."""
    bars: list[Bar] = []
    price = 1.1000
    for i in range(n_noisy):
        o = price
        c = price + direction * 0.00010 + (0.0004 if i % 2 else -0.0004)
        hi, lo = max(o, c) + 0.0003, min(o, c) - 0.0003
        bars.append(make_bar(i, o, hi, lo, c))
        price = c
    for j in range(n_quiet):
        o = price
        c = price + direction * 0.00002
        bars.append(make_bar(n_noisy + j, o, max(o, c) + 0.0001, min(o, c) - 0.0001, c))
        price = c
    return bars


def breakout_bar(bars: list[Bar], direction: int = 1, *, size: float = 0.0006, body: float = 0.8) -> Bar:
    """A strong bar closing beyond the recent channel."""
    i = len(bars)
    window = bars[-10:]
    edge = max(b.high for b in window) if direction > 0 else min(b.low for b in window)
    o = edge - direction * 0.00005
    c = o + direction * size * body
    hi = max(o, c) + size * (1 - body) / 2
    lo = min(o, c) - size * (1 - body) / 2
    return make_bar(i, o, hi, lo, c)


def test_long_breakout_signal() -> None:
    bars = base_series(1)
    bars.append(breakout_bar(bars, 1))
    result = BreakoutScout(scfg()).evaluate(bars)
    assert result.signal is not None, result.reason
    sig = result.signal
    assert sig.side is Side.BUY and sig.symbol == "EURUSD"
    assert sig.close > sig.channel_high
    assert sig.detected_at == bars[-1].end and sig.bar_start == bars[-1].start
    assert 0 <= sig.strength <= 1 and sig.reasons
    assert sig.squeeze_percentile is not None and sig.squeeze_percentile <= 0.5


def test_short_breakout_signal() -> None:
    bars = base_series(-1)
    bars.append(breakout_bar(bars, -1))
    result = BreakoutScout(scfg()).evaluate(bars)
    assert result.signal is not None, result.reason
    assert result.signal.side is Side.SELL and result.signal.close < result.signal.channel_low


def test_no_look_ahead_and_atr_from_prior_bar() -> None:
    bars = base_series(1)
    bars.append(breakout_bar(bars, 1))
    with_future = [*bars, make_bar(len(bars), bars[-1].close, bars[-1].close + 0.01, bars[-1].close, bars[-1].close)]
    a = BreakoutScout(scfg()).evaluate(bars)
    b = BreakoutScout(scfg()).evaluate(with_future[: len(bars)])
    assert a == b
    # ATR is measured without the trigger bar: changing the trigger bar's range doesn't move it
    bigger = [*bars[:-1], breakout_bar(bars[:-1], 1, size=0.0012)]
    c = BreakoutScout(scfg()).evaluate(bigger)
    assert a.metrics["atr"] == c.metrics["atr"]


def test_warming_up_and_empty() -> None:
    scout = BreakoutScout(scfg())
    assert "warming up" in scout.evaluate(base_series(1)[:10]).reason
    assert scout.evaluate([]).reason == "no data"


def test_inside_channel_is_no_breakout() -> None:
    bars = base_series(1)
    last = bars[-1]
    bars.append(make_bar(len(bars), last.close, last.close + 0.00005, last.close - 0.00005, last.close))
    assert BreakoutScout(scfg()).evaluate(bars).reason.startswith("no breakout")


@pytest.mark.parametrize(
    ("kwargs", "cfg_over", "reason"),
    [
        ({"size": 0.0004}, {"min_range_atr": 10.0}, "range too small"),
        ({"body": 0.3}, {}, "weak body"),
        ({"size": 0.0030, "body": 0.9}, {}, "over-extended"),
    ],
)
def test_filters(kwargs: dict[str, float], cfg_over: dict[str, Any], reason: str) -> None:
    bars = base_series(1)
    bars.append(breakout_bar(bars, 1, **kwargs))
    result = BreakoutScout(scfg(**cfg_over)).evaluate(bars)
    assert result.signal is None and reason in result.reason, result.reason


def test_poor_close_location() -> None:
    bars = base_series(1)
    window_high = max(b.high for b in bars[-10:])
    o = window_high - 0.00005
    c = o + 0.0006
    bars.append(make_bar(len(bars), o, c + 0.0006, o - 0.00001, c))  # big upper wick
    result = BreakoutScout(scfg(min_body_ratio=0.3)).evaluate(bars)
    assert result.signal is None and "close location" in result.reason, result.reason


def expanding_series() -> list[Bar]:
    """Quiet first, then swings that grow: bandwidth before the break is HIGH (no squeeze)."""
    bars = base_series(1, n_noisy=0, n_quiet=25)
    price = bars[-1].close
    for k in range(15):
        amp = 0.0002 + k * 0.00006
        o = price
        c = price + 0.00003 + (amp if k % 2 else -amp)
        bars.append(make_bar(len(bars), o, max(o, c) + 0.0001, min(o, c) - 0.0001, c))
        price = c
    return bars


def test_squeeze_required_and_disable() -> None:
    noisy = expanding_series()
    noisy.append(breakout_bar(noisy, 1, size=0.0012))
    strict = BreakoutScout(scfg(max_squeeze_percentile=0.2, max_extension_atr=5)).evaluate(noisy)
    assert strict.signal is None and "squeeze" in strict.reason, strict.reason
    off = BreakoutScout(scfg(max_squeeze_percentile=1.0, max_extension_atr=5)).evaluate(noisy)
    assert "squeeze" not in off.reason


def test_cooldown_per_symbol() -> None:
    scout = BreakoutScout(scfg(signal_cooldown_bars=4, max_squeeze_percentile=1.0))
    bars = base_series(1)
    bars.append(breakout_bar(bars, 1))
    assert scout.evaluate(bars).signal is not None
    # a second breakout two bars later is inside the cooldown
    again = [
        *bars,
        make_bar(len(bars), bars[-1].close, bars[-1].close + 0.0001, bars[-1].close - 0.0001, bars[-1].close),
    ]
    again.append(breakout_bar(again, 1, size=0.0006))
    later = scout.evaluate(again)
    assert later.signal is None and "cooldown" in later.reason, later.reason
    # other symbols are independent
    other = [Bar("GBPUSD", b.start, b.timeframe_minutes, b.open, b.high, b.low, b.close) for b in bars]
    assert scout.evaluate(other).signal is not None
    scout.reset("EURUSD")
    assert scout.evaluate(again).reason != "cooldown"


def test_evaluate_rejects_mixed_symbols() -> None:
    bars = base_series(1)
    bars[-1] = Bar("GBPUSD", bars[-1].start, 5, bars[-1].open, bars[-1].high, bars[-1].low, bars[-1].close)
    with pytest.raises(ValueError, match="single symbol"):
        BreakoutScout(scfg()).evaluate(bars)


def test_bar_history() -> None:
    hist = BarHistory(maxlen=3, timeframe_minutes=5)
    b0, b1, b2, b3 = (make_bar(i, 1.1, 1.2, 1.0, 1.1) for i in range(4))
    assert hist.add(b0) and hist.add(b1)
    assert not hist.add(b1)  # duplicate
    assert not hist.add(b0)  # out of order
    assert not hist.add(make_bar(9, 1.1, 1.2, 1.0, 1.1, tf=15))  # wrong timeframe
    assert hist.add(b2) and hist.add(b3)
    assert hist.bars("EURUSD") == [b1, b2, b3]  # maxlen enforced
    assert hist.last("EURUSD") == b3 and hist.last("GBPUSD") is None
    assert len(hist) == 3 and hist.symbols() == ["EURUSD"]
    with pytest.raises(ValueError):
        BarHistory(1)


# ----------------------------------------------------------------------------------------- planner


def _signal(direction: int = 1):  # type: ignore[no-untyped-def]
    bars = base_series(direction)
    bars.append(breakout_bar(bars, direction))
    result = BreakoutScout(scfg()).evaluate(bars)
    assert result.signal is not None, result.reason
    return result.signal, bars


@pytest.mark.parametrize("direction", [1, -1])
def test_plan_levels(direction: int) -> None:
    signal, bars = _signal(direction)
    inst = make_instrument()
    res = ChartsPlanner(scfg(stop_atr_mult=1.5, target_r=2.0, min_stop_spread_multiple=1.0)).plan(
        signal, bars, inst, spread_price=0.00002
    )
    assert res.vote is Vote.PROCEED and res.plan is not None, res.reason
    plan = res.plan
    vol = max(signal.atr, signal.atr_slow)
    assert plan.entry_ref == round(signal.close, 5)
    assert plan.stop_distance == pytest.approx(1.5 * vol, abs=1e-5)
    assert (plan.entry_ref - plan.stop_price) * direction > 0
    assert (plan.target_price - plan.entry_ref) * direction == pytest.approx(2 * plan.stop_distance, abs=2e-5)
    for price in (plan.entry_ref, plan.stop_price, plan.target_price):
        assert round(price, 5) == price


def test_plan_rejects_stop_inside_spread() -> None:
    signal, bars = _signal(1)
    res = ChartsPlanner(scfg(min_stop_spread_multiple=4.0)).plan(signal, bars, make_instrument(), spread_price=0.01)
    assert res.vote is Vote.EJECT and res.plan is None and "spread" in res.reason


def test_trend_filter() -> None:
    signal, bars = _signal(1)
    # make the trend fall: rewrite history so the EMA sits far above the breakout close
    falling = [
        make_bar(i, 1.2 - i * 0.001, 1.2 - i * 0.001 + 0.0002, 1.2 - i * 0.001 - 0.0002, 1.2 - i * 0.001)
        for i in range(40)
    ]
    falling = [*falling, *bars[-6:]]
    planner = ChartsPlanner(scfg(min_stop_spread_multiple=0))
    res = planner.plan(signal, falling, make_instrument(), spread_price=0.0)
    assert res.vote is Vote.EJECT and "against the trend" in res.reason
    relaxed = ChartsPlanner(scfg(require_trend_alignment=False, min_stop_spread_multiple=0))
    assert relaxed.plan(signal, falling, make_instrument(), spread_price=0.0).vote is Vote.PROCEED
    aligned = planner.plan(signal, bars, make_instrument(), spread_price=0.0)
    assert aligned.vote is Vote.PROCEED, aligned.reason


def test_planner_symbol_mismatch() -> None:
    signal, bars = _signal(1)
    with pytest.raises(ValueError):
        ChartsPlanner(scfg()).plan(signal, bars, make_instrument(symbol="GBPUSD"), spread_price=0.0)


def test_scout_speed_on_realistic_history() -> None:
    import time

    cfg = StrategyConfig()  # production defaults: warmup 122 bars
    bars = [make_bar(i, 1.1, 1.1005, 1.0995, 1.1001 if i % 2 else 1.0999) for i in range(400)]
    scout = BreakoutScout(cfg)
    start = time.perf_counter()
    for _ in range(20):
        scout.evaluate(bars)
    assert (time.perf_counter() - start) / 20 < 0.05
    assert T0 + timedelta(minutes=5) == bars[1].start
