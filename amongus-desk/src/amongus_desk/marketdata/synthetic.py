"""Deterministic synthetic market data for demos, tests and backtests.

The series is a regime-switching random walk: ordinary noise, then a quiet stretch (a volatility squeeze
that drifts gently one way), then a short directional expansion, then ordinary noise again. That shape
makes volatility breakouts actually happen, so the whole crew can be exercised without a data vendor.

Nothing here models a real market, a real broker or the UniTrader terminal: every number is made up.
Because the expansions are built to follow through, backtests on synthetic bars flatter a breakout
strategy; they exercise the plumbing and say nothing about edge in real markets.

Determinism: each symbol gets its own generators, seeded from ``synthetic.seed`` and ``zlib.crc32`` of
the symbol (never ``hash()``, which is salted per process). Only ``random.Random.random()`` is used,
because CPython guarantees that sequence across versions for a given integer seed. Same seed + symbol +
start time gives the same bars on every run (and, since prices are rounded to the instrument's
decimals, in practice on every machine; the platform's ``exp``/``log`` could in principle differ in the
last bit).

History (:meth:`SyntheticFeed.warmup`) is generated *backwards* from the start time with a separate
generator, so it ends exactly at the start price, does not depend on how many bars were requested, and
never changes the bars that :meth:`SyntheticFeed.stream` produces afterwards.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import math
import random
import threading
import zlib
from collections import deque
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..clock import Clock
from ..config import FeedConfig, InstrumentConfig
from ..domain import Bar, Quote, ensure_utc

log = logging.getLogger(__name__)

__all__ = ["SyntheticFeed"]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Per-bar volatility as a fraction of price for a 5-minute bar; scaled by sqrt(timeframe / 5).
_BASE_SIGMA_FRACTION = 0.0004
# Bars should move at least this many typical spreads per bar, so synthetic trades are not all
# rejected for having stops that are too tight relative to the spread.
_MIN_SIGMA_SPREADS = 1.5
# Weak pull of the log price back towards the base price (keeps long runs bounded).
_MEAN_REVERSION = 0.0005
# A base price must be worth at least this many price ticks (so rounding can not reach zero).
_MIN_BASE_TICKS = 100
# Default base price when none is configured: this many pips.
_DEFAULT_BASE_PIPS = 10_000

_PURPOSE_FORWARD = 1
_PURPOSE_BACKWARD = 2


class _Regime(enum.Enum):
    NORMAL = "normal"
    QUIET = "quiet"
    EXPANSION = "expansion"


@dataclass(frozen=True, slots=True)
class _RegimeSpec:
    vol: float  # noise, in units of the symbol's base sigma
    drift: float  # directional drift per bar, in units of the base sigma
    wick: float  # wick size, in units of this regime's noise
    min_len: int
    max_len: int
    coil: float = 0.0  # pull per bar towards a slowly drifting anchor (range-bound squeeze); 0 = free walk
    follow_drift: float | None = None  # drift after the regime's first bar (breakout bar, then the run)


_SPECS: dict[_Regime, _RegimeSpec] = {
    _Regime.NORMAL: _RegimeSpec(vol=0.75, drift=0.0, wick=0.8, min_len=25, max_len=80),
    # The squeeze coils around an anchor that creeps (``drift``) towards the coming breakout.
    _Regime.QUIET: _RegimeSpec(vol=0.25, drift=0.04, wick=1.0, min_len=22, max_len=50, coil=0.3),
    # A moderate breakout bar out of the squeeze, then a stronger run in the same direction.
    _Regime.EXPANSION: _RegimeSpec(vol=0.5, drift=1.25, wick=0.3, min_len=3, max_len=8, follow_drift=2.0),
}
# Forward in time: normal -> quiet (squeeze) -> expansion (breakout) -> normal.
_NEXT_FORWARD: dict[_Regime, _Regime] = {
    _Regime.NORMAL: _Regime.QUIET,
    _Regime.QUIET: _Regime.EXPANSION,
    _Regime.EXPANSION: _Regime.NORMAL,
}
# Walking backwards in time the same cycle is visited in reverse.
_NEXT_BACKWARD: dict[_Regime, _Regime] = {
    _Regime.NORMAL: _Regime.EXPANSION,
    _Regime.EXPANSION: _Regime.QUIET,
    _Regime.QUIET: _Regime.NORMAL,
}


def _rng_seed(seed: int, symbol: str, purpose: int) -> int:
    """Stable integer seed for one symbol's generator (``zlib.crc32``, never ``hash``)."""
    crc = zlib.crc32(symbol.encode("utf-8")) & 0xFFFFFFFF
    return ((seed % (1 << 64)) << 40) | (crc << 8) | purpose


def _floor_to_timeframe(moment: datetime, timeframe: timedelta) -> datetime:
    step = int(timeframe.total_seconds())
    seconds = int((moment - _EPOCH).total_seconds())
    return _EPOCH + timedelta(seconds=seconds - seconds % step)


class _PathGenerator:
    """One direction (forward or backward in time) of one symbol's regime-switching walk."""

    def __init__(self, rng: random.Random, *, price: float, base: float, sigma: float, forward: bool) -> None:
        self._rng = rng
        self._price = price
        self._log_base = math.log(base)
        self._sigma = sigma  # fraction of price per bar
        self._forward = forward
        self._regime = _Regime.NORMAL
        spec = _SPECS[self._regime]
        # Start part-way through an ordinary stretch so the first squeeze comes reasonably soon.
        self._remaining = 1 + int(self._uniform() * spec.max_len)
        self._length = self._remaining
        self._direction = 1 if self._uniform() < 0.5 else -1
        self._log_anchor = math.log(price)

    def _uniform(self) -> float:
        return self._rng.random()

    def _normal(self) -> float:
        # Box-Muller on random() only: random() is the one sequence CPython guarantees across versions.
        u1 = 1.0 - self._uniform()  # (0, 1]
        u2 = self._uniform()
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)

    def _enter(self, regime: _Regime) -> None:
        spec = _SPECS[regime]
        self._regime = regime
        self._remaining = spec.min_len + int(self._uniform() * (spec.max_len - spec.min_len + 1))
        self._length = self._remaining
        # The squeeze and the expansion that follows it share one direction. Pick it when the pair starts
        # (quiet when walking forward, expansion when walking backward), leaning back towards the base.
        starts_pair = (regime is _Regime.QUIET) if self._forward else (regime is _Regime.EXPANSION)
        if starts_pair:
            deviation = math.log(self._price) - self._log_base
            p_up = 0.5 - 0.3 * math.tanh(deviation / 0.01)
            self._direction = 1 if self._uniform() < p_up else -1
        self._log_anchor = math.log(self._price)

    def next_shape(self) -> tuple[float, float, float, float]:
        """Next bar as unrounded ``(open, high, low, close)``, continuing the walk in this direction."""
        if self._remaining <= 0:
            table = _NEXT_FORWARD if self._forward else _NEXT_BACKWARD
            self._enter(table[self._regime])
        spec = _SPECS[self._regime]
        noise = spec.vol * self._sigma
        # Position of this bar inside the regime in *forward* time (backward walks visit it in reverse).
        index = self._length - self._remaining if self._forward else self._remaining - 1
        strength = spec.drift if index == 0 or spec.follow_drift is None else spec.follow_drift
        drift = self._direction * strength * self._sigma
        log_price = math.log(self._price)
        pull = -_MEAN_REVERSION * (log_price - self._log_base)
        if spec.coil > 0:
            # Range-bound squeeze: the drift moves the anchor, the price is pulled towards the anchor.
            self._log_anchor += drift if self._forward else -drift
            pull += spec.coil * (self._log_anchor - log_price)
            drift = 0.0
        # Forward: the move starts at this price. Backward: it ends here, so the pulls are mirrored.
        move = drift + noise * self._normal() + (pull if self._forward else -pull)
        upper = spec.wick * noise * abs(self._normal())
        lower = spec.wick * noise * abs(self._normal())
        if self._forward:
            open_, close = self._price, self._price * math.exp(move)
            self._price = close
        else:
            open_, close = self._price * math.exp(-move), self._price
            self._price = open_
        high = max(open_, close) * math.exp(upper)
        low = min(open_, close) * math.exp(-lower)
        self._remaining -= 1
        return open_, high, low, close


class _SymbolSeries:
    """Bars for one symbol: forward from the start time for the stream, backward for the history."""

    def __init__(self, inst: InstrumentConfig, *, seed: int, base: float, timeframe_minutes: int) -> None:
        self.inst = inst
        self.base = base
        self._tf = timeframe_minutes
        self._decimals = inst.price_decimals
        self._tick = 10.0 ** (-inst.price_decimals)
        if base < _MIN_BASE_TICKS * self._tick:
            raise ValueError(
                f"{inst.symbol}: synthetic base price {base} is too small for {inst.price_decimals} price decimals"
            )
        sigma = _BASE_SIGMA_FRACTION * math.sqrt(timeframe_minutes / 5.0)
        sigma = max(sigma, _MIN_SIGMA_SPREADS * inst.typical_spread_price / base)
        self.sigma = sigma
        fwd_rng = random.Random(_rng_seed(seed, inst.symbol, _PURPOSE_FORWARD))  # noqa: S311 - simulation, not crypto
        back_rng = random.Random(_rng_seed(seed, inst.symbol, _PURPOSE_BACKWARD))  # noqa: S311
        self._forward = _PathGenerator(fwd_rng, price=base, base=base, sigma=sigma, forward=True)
        self._backward = _PathGenerator(back_rng, price=base, base=base, sigma=sigma, forward=False)
        self._history: list[Bar] = []  # newest first: index 0 ends at the start time

    def _make_bar(self, start: datetime, shape: tuple[float, float, float, float]) -> Bar:
        d = self._decimals
        open_, high, low, close = (round(v, d) for v in shape)
        high = max(high, open_, close)
        low = min(low, open_, close)
        if high - low < self._tick / 2:
            high = round(high + self._tick, d)  # never emit a zero-range bar
        return Bar(
            symbol=self.inst.symbol,
            start=start,
            timeframe_minutes=self._tf,
            open=open_,
            high=high,
            low=low,
            close=close,
        )

    def next_bar(self, start: datetime) -> Bar:
        return self._make_bar(start, self._forward.next_shape())

    def history(self, count: int, start: datetime) -> list[Bar]:
        """The ``count`` bars before ``start``, oldest first (always the same bars for the same seed)."""
        tf = timedelta(minutes=self._tf)
        while len(self._history) < count:
            k = len(self._history) + 1
            self._history.append(self._make_bar(start - k * tf, self._backward.next_shape()))
        return list(reversed(self._history[:count]))


class SyntheticFeed:
    """Made-up, reproducible bars for every configured instrument (implements ``MarketDataFeed``).

    ``stream`` emits one bar per symbol per step (in instrument order), sleeping
    ``synthetic.interval_seconds`` on ``clock`` between steps. Bar times are simulated: step ``k``
    covers ``[start + k*timeframe, start + (k+1)*timeframe)`` regardless of how fast wall time passes.
    ``max_bars`` (bars per symbol) ends the stream; ``None`` streams until :meth:`aclose`.
    Quotes are the last close +/- half the instrument's typical spread.
    """

    name = "synthetic"

    def __init__(
        self,
        cfg: FeedConfig,
        instruments: Sequence[InstrumentConfig],
        timeframe_minutes: int,
        clock: Clock,
        max_bars: int | None = None,
    ) -> None:
        if timeframe_minutes <= 0:
            raise ValueError("timeframe_minutes must be positive")
        if max_bars is not None and max_bars < 0:
            raise ValueError("max_bars must be >= 0 or None")
        if not instruments:
            raise ValueError("the synthetic feed needs at least one instrument")
        symbols = [i.symbol for i in instruments]
        if len(set(symbols)) != len(symbols):
            raise ValueError("instrument symbols must be unique")
        self._cfg = cfg.synthetic
        self._clock = clock
        self._tf = timedelta(minutes=timeframe_minutes)
        self._max_bars = max_bars
        if self._cfg.start is not None:
            self._start = ensure_utc(self._cfg.start)
        else:
            self._start = _floor_to_timeframe(ensure_utc(clock.now()), self._tf)
        unknown = sorted(set(self._cfg.base_prices) - set(symbols))
        if unknown:
            log.warning("synthetic.base_prices has no instrument for %s (ignored)", ", ".join(unknown))
        self._series: dict[str, _SymbolSeries] = {}
        for inst in instruments:
            base = self._cfg.base_prices.get(inst.symbol, _DEFAULT_BASE_PIPS * inst.pip_size)
            self._series[inst.symbol] = _SymbolSeries(
                inst, seed=self._cfg.seed, base=base, timeframe_minutes=timeframe_minutes
            )
        self._lock = threading.Lock()
        self._step = 0
        self._emitted = 0
        self._pending: deque[Bar] = deque()  # bars of the current step not yet handed out
        self._last: dict[str, Bar] = {}
        self._streaming = False
        self._closed = False
        self._closed_event = asyncio.Event()

    @property
    def start(self) -> datetime:
        """Start time of the first streamed bar (warmup history ends here)."""
        return self._start

    async def warmup(self, symbols: Sequence[str], bars: int) -> dict[str, list[Bar]]:
        """``bars`` bars per symbol ending exactly at :attr:`start`, oldest first."""
        if bars < 0:
            raise ValueError("bars must be >= 0")
        out: dict[str, list[Bar]] = {}
        for symbol in symbols:
            series = self._series.get(symbol)
            if series is None:
                log.warning("synthetic feed has no instrument %s; returning no warmup bars", symbol)
                out[symbol] = []
                continue
            history = series.history(bars, self._start)
            out[symbol] = history
            if history:
                with self._lock:
                    current = self._last.get(symbol)
                    if current is None or current.start < history[-1].start:
                        self._last[symbol] = history[-1]
        await asyncio.sleep(0)
        return out

    async def stream(self) -> AsyncGenerator[Bar, None]:
        """Yield the next step's bars (one per symbol), sleeping ``interval_seconds`` between steps."""
        if self._streaming:
            raise RuntimeError("stream() is already running for this feed")
        self._streaming = True
        try:
            first = True
            while not self._is_closed():
                if not self._pending:
                    if self._max_bars is not None and self._step >= self._max_bars:
                        return
                    if not first:
                        await self._sleep(self._cfg.interval_seconds)
                        if self._is_closed():
                            return
                    start = self._start + self._step * self._tf
                    step_bars = [series.next_bar(start) for series in self._series.values()]
                    with self._lock:
                        self._pending.extend(step_bars)
                        self._step += 1
                first = False
                with self._lock:
                    bar = self._pending.popleft()
                    self._last[bar.symbol] = bar
                    self._emitted += 1
                yield bar
        finally:
            self._streaming = False

    def latest_quote(self, symbol: str) -> Quote | None:
        """Last close +/- half the typical spread, stamped at that bar's close; ``None`` before any bar."""
        with self._lock:
            last = self._last.get(symbol)
        series = self._series.get(symbol)
        if last is None or series is None:
            return None
        half = series.inst.typical_spread_price / 2
        digits = series.inst.price_decimals + 2
        bid = round(last.close - half, digits)
        ask = round(last.close + half, digits)
        if bid <= 0:
            log.warning("synthetic quote for %s would have a non-positive bid; no quote", symbol)
            return None
        return Quote(symbol=symbol, bid=bid, ask=ask, time=last.end)

    def status(self) -> dict[str, Any]:
        """JSON-safe snapshot: steps, bars emitted and the last bar per symbol."""
        with self._lock:
            symbols = {
                symbol: {
                    "last_bar": self._last[symbol].start.isoformat() if symbol in self._last else None,
                    "last_close": self._last[symbol].close if symbol in self._last else None,
                    "base_price": series.base,
                }
                for symbol, series in self._series.items()
            }
            return {
                "kind": "synthetic",
                "seed": self._cfg.seed,
                "timeframe_minutes": int(self._tf.total_seconds() // 60),
                "start": self._start.isoformat(),
                "interval_seconds": self._cfg.interval_seconds,
                "steps": self._step,
                "bars_emitted": self._emitted,
                "max_bars": self._max_bars,
                "exhausted": self._max_bars is not None and self._step >= self._max_bars and not self._pending,
                "closed": self._closed,
                "symbols": symbols,
            }

    async def aclose(self) -> None:
        """Stop the stream (it ends at its next step, or immediately if it is sleeping)."""
        self._closed = True
        self._closed_event.set()

    def _is_closed(self) -> bool:
        # A method (not the attribute) so type checkers don't assume it can't change across an await.
        return self._closed

    async def _sleep(self, seconds: float) -> None:
        """Sleep on the injected clock, returning early when the feed is closed."""
        if seconds <= 0:
            await self._clock.sleep(0)
            return
        sleeper = asyncio.ensure_future(self._clock.sleep(seconds))
        closer = asyncio.ensure_future(self._closed_event.wait())
        try:
            await asyncio.wait({sleeper, closer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, closer):
                task.cancel()
            # gather() collects the helpers' own CancelledErrors but still raises one aimed at *this*
            # task while they wind down (awaiting each under suppress(CancelledError) would swallow it).
            await asyncio.gather(sleeper, closer, return_exceptions=True)
        if not sleeper.cancelled():
            sleeper.result()  # surface clock errors instead of swallowing them
