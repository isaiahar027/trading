"""Cyan Scout (volatility breakout detector) and Lime Charts (trend filter + trade plan).

The scout looks only at bars up to and including the one that just closed. Its yardsticks (ATR,
channel, squeeze) come from the bars *before* the trigger bar, so a huge trigger bar can't inflate the
volatility it is measured against.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import InstrumentConfig, StrategyConfig
from .domain import Bar, BreakoutSignal, Side, TradePlan, Vote
from .indicators import atr_wilder, bollinger_bandwidth, ema, percentile_rank

log = logging.getLogger(__name__)


class BarHistory:
    """Rolling per-symbol bar store that only accepts strictly newer, closed bars of one timeframe."""

    def __init__(self, maxlen: int, timeframe_minutes: int | None = None) -> None:
        if maxlen < 2:
            raise ValueError("maxlen must be >= 2")
        self._maxlen = maxlen
        self._timeframe = timeframe_minutes
        self._bars: dict[str, deque[Bar]] = {}

    def add(self, bar: Bar) -> bool:
        if self._timeframe is not None and bar.timeframe_minutes != self._timeframe:
            log.warning(
                "ignoring %s bar with timeframe %s (expected %s)", bar.symbol, bar.timeframe_minutes, self._timeframe
            )
            return False
        series = self._bars.setdefault(bar.symbol, deque(maxlen=self._maxlen))
        if series and bar.start <= series[-1].start:
            return False  # duplicate or out of order
        series.append(bar)
        return True

    def bars(self, symbol: str) -> list[Bar]:
        return list(self._bars.get(symbol, ()))

    def last(self, symbol: str) -> Bar | None:
        series = self._bars.get(symbol)
        return series[-1] if series else None

    def symbols(self) -> list[str]:
        return sorted(self._bars)

    def __len__(self) -> int:
        return sum(len(s) for s in self._bars.values())


@dataclass(frozen=True, slots=True)
class ScoutResult:
    signal: BreakoutSignal | None
    reason: str
    metrics: dict[str, float] = field(default_factory=dict)


class BreakoutScout:
    def __init__(self, cfg: StrategyConfig) -> None:
        self._cfg = cfg
        self._last_signal: dict[str, datetime] = {}

    def reset(self, symbol: str | None = None) -> None:
        if symbol is None:
            self._last_signal.clear()
        else:
            self._last_signal.pop(symbol, None)

    def evaluate(self, bars: Sequence[Bar]) -> ScoutResult:
        """Evaluate the last bar of ``bars`` (one symbol, oldest first) for a volatility breakout."""
        cfg = self._cfg
        if not bars:
            return ScoutResult(None, "no data")
        if len(bars) < cfg.warmup_bars:
            return ScoutResult(None, f"warming up ({len(bars)}/{cfg.warmup_bars} bars)")
        symbol = bars[-1].symbol
        if any(b.symbol != symbol for b in bars[-cfg.warmup_bars :]):
            raise ValueError("evaluate() expects bars of a single symbol")

        t = len(bars) - 1
        bar = bars[t]
        prior = bars[:t]
        atr = atr_wilder(prior, cfg.atr_period)[-1]
        atr_slow = atr_wilder(prior, cfg.atr_slow_period)[-1]
        if atr is None or atr_slow is None or atr <= 0:
            return ScoutResult(None, "volatility not measurable yet")

        window = prior[-cfg.donchian_lookback :]
        channel_high = max(b.high for b in window)
        channel_low = min(b.low for b in window)
        prev_close = prior[-1].close
        true_range = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        bar_range = bar.high - bar.low
        body = abs(bar.close - bar.open)
        metrics: dict[str, float] = {
            "close": bar.close,
            "channel_high": channel_high,
            "channel_low": channel_low,
            "atr": atr,
            "atr_slow": atr_slow,
            "range_atr": true_range / atr,
        }

        # "vol" is the normal volatility unit: after a squeeze the fast ATR is tiny, so distances that should
        # reflect ordinary market noise (extension, stop) use the larger of fast and slow ATR.
        vol = max(atr, atr_slow)
        metrics["vol"] = vol
        buffer = cfg.breakout_buffer_atr * atr
        if bar.close > channel_high + buffer:
            side = Side.BUY
            extension = (bar.close - channel_high) / vol
            close_location = (bar.close - bar.low) / bar_range if bar_range > 0 else 0.0
            with_body = bar.close > bar.open
        elif bar.close < channel_low - buffer:
            side = Side.SELL
            extension = (channel_low - bar.close) / vol
            close_location = (bar.high - bar.close) / bar_range if bar_range > 0 else 0.0
            with_body = bar.close < bar.open
        else:
            return ScoutResult(None, "no breakout: close inside the channel", metrics)

        body_ratio = body / bar_range if bar_range > 0 else 0.0
        metrics.update({"extension_atr": extension, "body_ratio": body_ratio, "close_location": close_location})

        if true_range < cfg.min_range_atr * atr:
            return ScoutResult(
                None, f"range too small: {true_range / atr:.2f}x ATR < {cfg.min_range_atr:.2f}x", metrics
            )
        if not with_body or body_ratio < cfg.min_body_ratio:
            return ScoutResult(
                None, f"weak body: {body_ratio:.0%} of the bar (needs {cfg.min_body_ratio:.0%})", metrics
            )
        if close_location < cfg.min_close_location:
            return ScoutResult(
                None, f"poor close location: {close_location:.0%} (needs {cfg.min_close_location:.0%})", metrics
            )
        if extension > cfg.max_extension_atr:
            return ScoutResult(
                None, f"over-extended: {extension:.2f} vol past the channel (max {cfg.max_extension_atr:.2f})", metrics
            )

        squeeze = self._squeeze_percentile(prior)
        if squeeze is not None:
            metrics["squeeze_percentile"] = squeeze
        if cfg.max_squeeze_percentile < 1.0 and (squeeze is None or squeeze > cfg.max_squeeze_percentile):
            shown = "n/a" if squeeze is None else f"p{squeeze * 100:.0f}"
            return ScoutResult(
                None,
                f"no squeeze before the break: bandwidth {shown} > p{cfg.max_squeeze_percentile * 100:.0f}",
                metrics,
            )

        last = self._last_signal.get(symbol)
        cooldown = timedelta(minutes=cfg.signal_cooldown_bars * bar.timeframe_minutes)
        if last is not None and bar.start - last < cooldown:
            return ScoutResult(None, f"cooldown: last signal at {last:%H:%M} UTC", metrics)

        range_atr = true_range / atr
        range_score = min(1.0, max(0.0, (range_atr - cfg.min_range_atr) / max(cfg.min_range_atr, 1e-9)))
        squeeze_score = 0.5 if squeeze is None else 1.0 - squeeze
        extension_score = 1.0 - min(1.0, extension / cfg.max_extension_atr)
        strength = round(
            min(1.0, max(0.0, 0.35 * range_score + 0.3 * squeeze_score + 0.2 * extension_score + 0.15 * body_ratio)), 3
        )
        edge = channel_high if side is Side.BUY else channel_low
        reasons = [
            f"close {bar.close} {'above' if side is Side.BUY else 'below'} the {cfg.donchian_lookback}-bar channel "
            f"{'high' if side is Side.BUY else 'low'} {edge}",
            f"range {range_atr:.2f}x ATR",
            f"body {body_ratio:.0%}, close location {close_location:.0%}",
            f"extension {extension:.2f} vol (vol = max of fast/slow ATR)",
        ]
        if squeeze is not None:
            reasons.append(f"pre-break bandwidth at p{squeeze * 100:.0f}")

        signal = BreakoutSignal(
            symbol=symbol,
            side=side,
            bar_start=bar.start,
            detected_at=bar.end,
            close=bar.close,
            channel_high=channel_high,
            channel_low=channel_low,
            atr=atr,
            atr_slow=atr_slow,
            range_atr=range_atr,
            extension_atr=extension,
            squeeze_percentile=squeeze,
            strength=strength,
            reasons=reasons,
        )
        self._last_signal[symbol] = bar.start
        metrics["strength"] = strength
        return ScoutResult(signal, f"{side.value} breakout", metrics)

    def _squeeze_percentile(self, prior: Sequence[Bar]) -> float | None:
        cfg = self._cfg
        needed = cfg.squeeze_period + cfg.squeeze_lookback
        closes = [b.close for b in prior[-needed:]]
        bandwidth = bollinger_bandwidth(closes, cfg.squeeze_period)
        current = bandwidth[-1] if bandwidth else None
        history = [v for v in bandwidth[-cfg.squeeze_lookback - 1 : -1] if v is not None]
        if current is None or not history:
            return None
        return percentile_rank(history, current)


@dataclass(frozen=True, slots=True)
class PlanResult:
    plan: TradePlan | None
    vote: Vote
    reason: str
    metrics: dict[str, float] = field(default_factory=dict)


class ChartsPlanner:
    def __init__(self, cfg: StrategyConfig) -> None:
        self._cfg = cfg

    def plan(
        self, signal: BreakoutSignal, bars: Sequence[Bar], instrument: InstrumentConfig, spread_price: float
    ) -> PlanResult:
        cfg = self._cfg
        if signal.symbol != instrument.symbol or (bars and bars[-1].symbol != signal.symbol):
            raise ValueError("signal, bars and instrument must be for the same symbol")
        closes = [b.close for b in bars]
        trend = ema(closes, cfg.trend_ema_period)
        metrics: dict[str, float] = {"spread_price": spread_price}
        if len(trend) > cfg.trend_slope_bars and trend[-1] is not None and trend[-1 - cfg.trend_slope_bars] is not None:
            now_ema = trend[-1]
            then_ema = trend[-1 - cfg.trend_slope_bars]
            assert now_ema is not None and then_ema is not None
            slope = now_ema - then_ema
            metrics.update({"ema": now_ema, "ema_slope": slope})
        else:
            now_ema = None
            slope = 0.0
        if cfg.require_trend_alignment:
            if now_ema is None:
                return PlanResult(
                    None, Vote.EJECT, f"not enough bars for the EMA{cfg.trend_ema_period} trend filter", metrics
                )
            if signal.side is Side.BUY and not (signal.close > now_ema and slope >= 0):
                why = "close below" if signal.close <= now_ema else "falling"
                return PlanResult(None, Vote.EJECT, f"against the trend: EMA{cfg.trend_ema_period} {why}", metrics)
            if signal.side is Side.SELL and not (signal.close < now_ema and slope <= 0):
                why = "close above" if signal.close >= now_ema else "rising"
                return PlanResult(None, Vote.EJECT, f"against the trend: EMA{cfg.trend_ema_period} {why}", metrics)

        digits = instrument.price_decimals
        entry = round(signal.close, digits)
        vol = max(signal.atr, signal.atr_slow)
        raw_distance = cfg.stop_atr_mult * vol
        sign = signal.side.sign
        stop = round(entry - sign * raw_distance, digits)
        stop_distance = abs(entry - stop)
        target = round(entry + sign * cfg.target_r * stop_distance, digits)
        metrics.update({"entry": entry, "stop": stop, "target": target, "stop_distance": stop_distance})
        if stop_distance <= 0 or stop <= 0:
            return PlanResult(None, Vote.EJECT, "stop distance rounds to zero", metrics)
        min_distance = cfg.min_stop_spread_multiple * spread_price
        if stop_distance < min_distance:
            pip = instrument.pip_size
            return PlanResult(
                None,
                Vote.EJECT,
                f"stop {stop_distance / pip:.1f} pips is under {cfg.min_stop_spread_multiple:g}x the spread "
                f"({spread_price / pip:.1f} pips)",
                metrics,
            )

        notes = [
            f"stop {cfg.stop_atr_mult:g} vol = {stop_distance / instrument.pip_size:.1f} pips",
            f"target {cfg.target_r:g}R",
        ]
        if now_ema is not None:
            notes.append(f"EMA{cfg.trend_ema_period} {now_ema:.{digits}f}, slope {slope:+.{digits}f}")
        plan = TradePlan(
            symbol=signal.symbol,
            side=signal.side,
            entry_ref=entry,
            stop_price=stop,
            target_price=target,
            stop_distance=stop_distance,
            atr=vol,
            target_r=cfg.target_r,
            time_stop_bars=cfg.time_stop_bars,
            spread_price=spread_price,
            notes=notes,
        )
        return PlanResult(plan, Vote.PROCEED, f"plan {signal.side.value} {entry} / SL {stop} / TP {target}", metrics)
