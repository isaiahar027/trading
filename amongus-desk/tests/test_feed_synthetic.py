from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import math
import os
import random
import statistics
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from amongus_desk.clock import ManualClock, SystemClock
from amongus_desk.config import FeedConfig, InstrumentConfig
from amongus_desk.domain import Bar
from amongus_desk.marketdata.base import MarketDataFeed
from amongus_desk.marketdata.synthetic import SyntheticFeed
from factories import T0, make_instrument

SRC = Path(__file__).resolve().parents[1] / "src"


class RecordingClock(ManualClock):
    """ManualClock that remembers every sleep request."""

    def __init__(self, start: datetime) -> None:
        super().__init__(start)
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await super().sleep(seconds)


def gbpusd() -> InstrumentConfig:
    return make_instrument(symbol="GBPUSD", feed_symbol="GBP/USD", currencies=["GBP", "USD"])


def feed_cfg(**synthetic: Any) -> FeedConfig:
    data: dict[str, Any] = {"seed": 7, "interval_seconds": 0.0, "start": T0}
    data.update(synthetic)
    return FeedConfig(synthetic=data)


def make_feed(
    *,
    instruments: Sequence[InstrumentConfig] | None = None,
    clock: ManualClock | None = None,
    max_bars: int | None = 50,
    tf: int = 5,
    **synthetic: Any,
) -> SyntheticFeed:
    return SyntheticFeed(
        feed_cfg(**synthetic),
        list(instruments) if instruments is not None else [make_instrument()],
        tf,
        clock or ManualClock(T0),
        max_bars=max_bars,
    )


async def collect(feed: SyntheticFeed) -> list[Bar]:
    return [bar async for bar in feed.stream()]


def by_symbol(bars: Sequence[Bar], symbol: str) -> list[Bar]:
    return [b for b in bars if b.symbol == symbol]


def as_tuples(bars: Sequence[Bar]) -> list[tuple[Any, ...]]:
    return [(b.symbol, b.start, b.open, b.high, b.low, b.close) for b in bars]


# ----------------------------------------------------------------------------------- protocol & determinism


def test_satisfies_market_data_feed_protocol() -> None:
    feed: MarketDataFeed = make_feed()
    assert feed.name == "synthetic"


@pytest.mark.anyio
async def test_same_seed_gives_identical_bars() -> None:
    insts = [make_instrument(), gbpusd()]
    a = make_feed(instruments=insts, max_bars=300)
    b = make_feed(instruments=insts, max_bars=300)
    assert as_tuples(await collect(a)) == as_tuples(await collect(b))
    wa = await a.warmup(["EURUSD", "GBPUSD"], 120)
    wb = await b.warmup(["EURUSD", "GBPUSD"], 120)
    assert {k: as_tuples(v) for k, v in wa.items()} == {k: as_tuples(v) for k, v in wb.items()}


@pytest.mark.anyio
async def test_different_seeds_differ() -> None:
    a = await collect(make_feed(seed=7, max_bars=100))
    b = await collect(make_feed(seed=8, max_bars=100))
    assert [x.close for x in a] != [x.close for x in b]


@pytest.mark.anyio
async def test_negative_seed_is_valid_and_distinct_from_positive() -> None:
    a = await collect(make_feed(seed=-7, max_bars=60))
    b = await collect(make_feed(seed=7, max_bars=60))
    assert len(a) == 60
    assert [x.close for x in a] != [x.close for x in b]


@pytest.mark.anyio
async def test_different_symbols_differ_even_with_the_same_base_price() -> None:
    feed = make_feed(instruments=[make_instrument(), gbpusd()], max_bars=200)
    bars = await collect(feed)
    eur = by_symbol(bars, "EURUSD")
    gbp = by_symbol(bars, "GBPUSD")
    assert eur[0].open == gbp[0].open  # same default base price
    assert [b.close for b in eur] != [b.close for b in gbp]


_SUBPROCESS_SCRIPT = """
import asyncio, json, sys
from datetime import UTC, datetime
from amongus_desk.clock import ManualClock
from amongus_desk.config import FeedConfig, InstrumentConfig
from amongus_desk.marketdata.synthetic import SyntheticFeed

T0 = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
inst = InstrumentConfig(symbol="EURUSD", feed_symbol="EUR/USD", pip_size=0.0001, contract_size=100000,
                        typical_spread_pips=1.0)

async def main():
    feed = SyntheticFeed(FeedConfig(synthetic={"seed": 7, "interval_seconds": 0, "start": T0}), [inst], 5,
                         ManualClock(T0), max_bars=40)
    warm = (await feed.warmup(["EURUSD"], 20))["EURUSD"]
    bars = [b async for b in feed.stream()]
    print(json.dumps([[b.open, b.high, b.low, b.close] for b in warm + bars]))

asyncio.run(main())
"""


def test_bars_do_not_depend_on_the_process_hash_seed() -> None:
    """Seeds come from zlib.crc32, so a different PYTHONHASHSEED must give identical bars."""
    outputs = []
    for hash_seed in ("1", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(SRC)}
        result = subprocess.run(
            [sys.executable, "-c", _SUBPROCESS_SCRIPT], capture_output=True, text=True, env=env, timeout=60, check=True
        )
        outputs.append(json.loads(result.stdout))
    assert outputs[0] == outputs[1]
    assert len(outputs[0]) == 60


# ----------------------------------------------------------------------------------- bar validity


@pytest.mark.anyio
async def test_ohlc_always_valid_rounded_and_continuous() -> None:
    insts = [make_instrument(), gbpusd(), make_instrument(symbol="USDJPY", pip_size=0.01, price_decimals=3)]
    feed = make_feed(instruments=insts, max_bars=2500)
    warm = await feed.warmup([i.symbol for i in insts], 500)
    streamed = await collect(feed)
    for inst in insts:
        series = warm[inst.symbol] + by_symbol(streamed, inst.symbol)
        assert len(series) == 3000
        tick = 10.0 ** (-inst.price_decimals)
        for prev, bar in itertools.pairwise(series):
            assert bar.start - prev.start == timedelta(minutes=5)
            assert bar.open == prev.close  # no gaps between consecutive bars
        for bar in series:
            assert bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
            assert bar.low > 0
            assert bar.high - bar.low >= tick / 2  # never a zero-range bar
            for value in (bar.open, bar.high, bar.low, bar.close):
                assert round(value, inst.price_decimals) == value
            assert bar.timeframe_minutes == 5
            assert bar.start.tzinfo is not None and bar.start.utcoffset() == timedelta(0)


@pytest.mark.anyio
async def test_prices_stay_bounded_near_the_base_over_long_runs() -> None:
    feed = make_feed(max_bars=6000, base_prices={"EURUSD": 1.1})
    closes = [b.close for b in await collect(feed)]
    assert closes[0] == pytest.approx(1.1, rel=0.01)
    assert all(0.9 < c < 1.3 for c in closes)


# ----------------------------------------------------------------------------------- regimes


def _rolling_mean(values: Sequence[float], window: int) -> list[float]:
    return [statistics.fmean(values[i - window : i]) for i in range(window, len(values) + 1)]


def _squeeze_breakouts(bars: Sequence[Bar]) -> list[int]:
    """Distinct episodes where a quiet 15-bar stretch is followed by 3 sharply larger, directional bars."""
    ranges = [b.range for b in bars]
    typical = statistics.median(ranges)
    starts: list[int] = []
    for i in range(15, len(ranges) - 3):
        quiet = statistics.fmean(ranges[i - 15 : i])
        burst = statistics.fmean(ranges[i : i + 3])
        travel = abs(bars[i + 2].close - bars[i].open)
        new_episode = not starts or i - starts[-1] > 10
        if quiet < 0.75 * typical and burst > 2.5 * quiet and travel > 5.0 * quiet and new_episode:
            starts.append(i)
    return starts


@pytest.mark.anyio
@pytest.mark.parametrize("seed", [1, 6, 7, 42])
async def test_quiet_stretches_are_followed_by_range_expansions(seed: int) -> None:
    bars = await collect(make_feed(seed=seed, max_bars=600))
    ranges = [b.range for b in bars]
    typical = statistics.median(ranges)
    # Volatility clusters: some 15-bar windows are clearly quiet, some 4-bar windows clearly expanded.
    assert min(_rolling_mean(ranges, 15)) < 0.75 * typical
    assert max(_rolling_mean(ranges, 4)) > 1.8 * typical
    # The squeeze -> expansion pattern itself, several times within a few hundred bars.
    episodes = _squeeze_breakouts(bars)
    assert len(episodes) >= 2, f"expected squeeze->expansion episodes, found {episodes}"


def test_breakout_detector_does_not_fire_on_a_constant_volatility_walk() -> None:
    """Control for the test above: the same detector finds nothing in a regime-free random walk."""
    rng = random.Random(1)
    price = 1.0
    bars: list[Bar] = []
    for i in range(600):
        close = price + rng.gauss(0, 0.0003)
        high = max(price, close) + abs(rng.gauss(0, 0.0002))
        low = min(price, close) - abs(rng.gauss(0, 0.0002))
        bars.append(Bar("CTRL", T0 + timedelta(minutes=5 * i), 5, price, high, low, close))
        price = close
    assert _squeeze_breakouts(bars) == []


@pytest.mark.anyio
async def test_expansions_go_both_ways() -> None:
    moves = []
    for seed in range(1, 6):
        bars = await collect(make_feed(seed=seed, max_bars=800))
        ranges = [b.range for b in bars]
        typical = statistics.median(ranges)
        for i in range(len(bars) - 3):
            if statistics.fmean(ranges[i : i + 3]) > 2.0 * typical:
                moves.append(math.copysign(1, bars[i + 2].close - bars[i].open))
    assert 1.0 in moves and -1.0 in moves


@pytest.mark.anyio
async def test_volatility_is_at_least_a_few_spreads_for_wide_spread_instruments() -> None:
    wide = make_instrument(symbol="WIDE", typical_spread_pips=20.0, max_spread_pips=40.0)
    bars = await collect(make_feed(instruments=[wide], max_bars=400))
    assert statistics.median(b.range for b in bars) > wide.typical_spread_price


# ----------------------------------------------------------------------------------- warmup & time grid


@pytest.mark.anyio
async def test_warmup_ends_at_the_start_and_joins_the_stream() -> None:
    feed = make_feed(instruments=[make_instrument(), gbpusd()], max_bars=5)
    warm = await feed.warmup(["EURUSD", "GBPUSD"], 30)
    bars = await collect(feed)
    for symbol in ("EURUSD", "GBPUSD"):
        history = warm[symbol]
        assert len(history) == 30
        assert history[-1].end == T0 == feed.start
        assert history[0].start == T0 - timedelta(minutes=5 * 30)
        first = by_symbol(bars, symbol)[0]
        assert first.start == T0
        assert first.open == history[-1].close


@pytest.mark.anyio
async def test_warmup_history_does_not_depend_on_the_requested_length() -> None:
    feed = make_feed()
    short = (await feed.warmup(["EURUSD"], 10))["EURUSD"]
    long = (await feed.warmup(["EURUSD"], 200))["EURUSD"]
    assert as_tuples(long[-10:]) == as_tuples(short)
    other = make_feed()
    direct = (await other.warmup(["EURUSD"], 200))["EURUSD"]
    assert as_tuples(direct) == as_tuples(long)


@pytest.mark.anyio
async def test_warmup_does_not_change_the_streamed_bars() -> None:
    with_warmup = make_feed(max_bars=100)
    await with_warmup.warmup(["EURUSD"], 400)
    without = make_feed(max_bars=100)
    assert as_tuples(await collect(with_warmup)) == as_tuples(await collect(without))


@pytest.mark.anyio
async def test_warmup_edge_cases() -> None:
    feed = make_feed()
    assert await feed.warmup(["EURUSD"], 0) == {"EURUSD": []}
    assert await feed.warmup(["NOPE"], 10) == {"NOPE": []}
    with pytest.raises(ValueError, match="bars"):
        await feed.warmup(["EURUSD"], -1)


@pytest.mark.anyio
async def test_one_bar_per_symbol_per_step_in_instrument_order() -> None:
    insts = [gbpusd(), make_instrument()]
    bars = await collect(make_feed(instruments=insts, max_bars=4))
    assert [b.symbol for b in bars] == ["GBPUSD", "EURUSD"] * 4
    assert [b.start for b in bars] == [T0 + timedelta(minutes=5 * (i // 2)) for i in range(8)]


def test_default_start_is_now_rounded_down_to_the_timeframe() -> None:
    clock = ManualClock(datetime(2026, 10, 5, 8, 7, 33, 123, tzinfo=UTC))
    feed = SyntheticFeed(FeedConfig(), [make_instrument()], 5, clock)
    assert feed.start == datetime(2026, 10, 5, 8, 5, tzinfo=UTC)
    hourly = SyntheticFeed(FeedConfig(), [make_instrument()], 60, clock)
    assert hourly.start == datetime(2026, 10, 5, 8, 0, tzinfo=UTC)


def test_explicit_start_is_used_as_given() -> None:
    start = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    feed = make_feed(start=start)
    assert feed.start == start


@pytest.mark.anyio
async def test_base_price_from_config_is_the_price_at_the_start() -> None:
    feed = make_feed(base_prices={"EURUSD": 1.0850}, max_bars=1)
    warm = await feed.warmup(["EURUSD"], 3)
    bars = await collect(feed)
    assert warm["EURUSD"][-1].close == pytest.approx(1.0850)
    assert bars[0].open == pytest.approx(1.0850)


@pytest.mark.anyio
async def test_hourly_timeframe_scales_volatility() -> None:
    five = await collect(make_feed(max_bars=500, tf=5))
    hour = await collect(make_feed(max_bars=500, tf=60))
    assert all(b.timeframe_minutes == 60 for b in hour)
    assert statistics.median(b.range for b in hour) > 2 * statistics.median(b.range for b in five)


# ----------------------------------------------------------------------------------- pacing & termination


@pytest.mark.anyio
async def test_sleeps_interval_between_steps_through_the_clock() -> None:
    clock = RecordingClock(T0)
    feed = make_feed(clock=clock, interval_seconds=2.5, max_bars=4, instruments=[make_instrument(), gbpusd()])
    bars = await collect(feed)
    assert len(bars) == 8
    assert clock.sleeps == [2.5, 2.5, 2.5]  # between steps only; none after the last step
    assert clock.now() == T0 + timedelta(seconds=7.5)


@pytest.mark.anyio
async def test_zero_interval_still_yields_to_the_event_loop() -> None:
    clock = RecordingClock(T0)
    feed = make_feed(clock=clock, interval_seconds=0.0, max_bars=3)
    await collect(feed)
    assert clock.sleeps == [0, 0]
    assert clock.now() == T0


@pytest.mark.anyio
async def test_max_bars_ends_the_stream() -> None:
    feed = make_feed(max_bars=5, instruments=[make_instrument(), gbpusd()])
    bars = await collect(feed)
    assert len(by_symbol(bars, "EURUSD")) == 5 and len(by_symbol(bars, "GBPUSD")) == 5
    assert feed.status()["exhausted"] is True
    assert await collect(feed) == []  # nothing more, and nothing repeated


@pytest.mark.anyio
async def test_max_bars_zero_streams_nothing() -> None:
    assert await collect(make_feed(max_bars=0)) == []


@pytest.mark.anyio
async def test_a_second_stream_call_resumes_without_repeating_bars() -> None:
    feed = make_feed(max_bars=None)
    first: list[Bar] = []
    gen = feed.stream()
    async with contextlib.aclosing(gen):
        async for bar in gen:
            first.append(bar)
            if len(first) == 3:
                break
    second: list[Bar] = []
    gen = feed.stream()
    async with contextlib.aclosing(gen):
        async for bar in gen:
            second.append(bar)
            if len(second) == 3:
                break
    assert [b.start for b in first + second] == [T0 + timedelta(minutes=5 * i) for i in range(6)]


@pytest.mark.anyio
async def test_concurrent_streams_are_refused() -> None:
    feed = make_feed(max_bars=None)
    gen = feed.stream()
    async with contextlib.aclosing(gen):
        await gen.__anext__()
        other = feed.stream()
        with pytest.raises(RuntimeError, match="already running"):
            await other.__anext__()


@pytest.mark.anyio
async def test_aclose_ends_an_unbounded_stream() -> None:
    feed = make_feed(max_bars=None)
    seen = 0
    async for _bar in feed.stream():
        seen += 1
        if seen == 7:
            await feed.aclose()
    assert seen == 7
    assert feed.status()["closed"] is True


@pytest.mark.anyio
async def test_aclose_interrupts_a_long_sleep() -> None:
    feed = SyntheticFeed(feed_cfg(interval_seconds=3600.0), [make_instrument()], 5, SystemClock(), max_bars=None)
    got: list[Bar] = []

    async def consume() -> None:
        async for bar in feed.stream():
            got.append(bar)

    task = asyncio.create_task(consume())
    for _ in range(100):
        if got:
            break
        await asyncio.sleep(0)
    assert len(got) == 1  # the first step is immediate, then the feed sleeps for an hour
    await feed.aclose()
    await asyncio.wait_for(task, timeout=1.0)
    assert len(got) == 1


@pytest.mark.anyio
async def test_clock_errors_during_sleep_propagate() -> None:
    class BrokenClock(ManualClock):
        async def sleep(self, seconds: float) -> None:
            raise RuntimeError("clock broke")

    feed = make_feed(clock=BrokenClock(T0), interval_seconds=1.0, max_bars=None)
    with pytest.raises(RuntimeError, match="clock broke"):
        await collect(feed)


# ----------------------------------------------------------------------------------- quotes & status


@pytest.mark.anyio
async def test_latest_quote_is_close_plus_minus_half_the_typical_spread() -> None:
    inst = make_instrument(typical_spread_pips=1.4)
    feed = make_feed(instruments=[inst], max_bars=3)
    assert feed.latest_quote("EURUSD") is None
    await feed.warmup(["EURUSD"], 5)
    warm_quote = feed.latest_quote("EURUSD")
    assert warm_quote is not None and warm_quote.time == T0
    bars = await collect(feed)
    quote = feed.latest_quote("EURUSD")
    assert quote is not None
    last = bars[-1]
    assert quote.mid == pytest.approx(last.close)
    assert quote.spread == pytest.approx(inst.typical_spread_price)
    assert quote.time == last.end
    assert feed.latest_quote("NOPE") is None


@pytest.mark.anyio
async def test_zero_spread_instrument_gets_a_locked_quote() -> None:
    inst = make_instrument(typical_spread_pips=0.0)
    feed = make_feed(instruments=[inst], max_bars=1)
    await collect(feed)
    quote = feed.latest_quote("EURUSD")
    assert quote is not None and quote.bid == quote.ask


@pytest.mark.anyio
async def test_warmup_does_not_move_the_quote_back_after_streaming() -> None:
    feed = make_feed(max_bars=3)
    bars = await collect(feed)
    await feed.warmup(["EURUSD"], 10)
    quote = feed.latest_quote("EURUSD")
    assert quote is not None and quote.time == bars[-1].end


@pytest.mark.anyio
async def test_status_is_json_safe_and_informative() -> None:
    feed = make_feed(instruments=[make_instrument(), gbpusd()], max_bars=2)
    bars = await collect(feed)
    status = feed.status()
    json.dumps(status)
    assert status["kind"] == "synthetic"
    assert status["steps"] == 2 and status["bars_emitted"] == 4
    assert status["seed"] == 7 and status["timeframe_minutes"] == 5
    assert status["start"] == T0.isoformat()
    assert status["symbols"]["EURUSD"]["last_bar"] == by_symbol(bars, "EURUSD")[-1].start.isoformat()
    assert status["symbols"]["GBPUSD"]["last_close"] == by_symbol(bars, "GBPUSD")[-1].close


# ----------------------------------------------------------------------------------- argument validation


def test_invalid_arguments_are_rejected() -> None:
    clock = ManualClock(T0)
    with pytest.raises(ValueError, match="timeframe"):
        SyntheticFeed(FeedConfig(), [make_instrument()], 0, clock)
    with pytest.raises(ValueError, match="max_bars"):
        SyntheticFeed(FeedConfig(), [make_instrument()], 5, clock, max_bars=-1)
    with pytest.raises(ValueError, match="at least one"):
        SyntheticFeed(FeedConfig(), [], 5, clock)
    with pytest.raises(ValueError, match="unique"):
        SyntheticFeed(FeedConfig(), [make_instrument(), make_instrument()], 5, clock)
    with pytest.raises(ValueError, match="too small"):
        SyntheticFeed(feed_cfg(base_prices={"EURUSD": 0.0001}), [make_instrument()], 5, clock)
