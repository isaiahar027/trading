from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from amongus_desk.clock import ManualClock
from amongus_desk.config import FeedConfig, InstrumentConfig
from amongus_desk.domain import Bar
from amongus_desk.marketdata.base import FeedError, MarketDataFeed
from amongus_desk.marketdata.twelvedata import INTERVALS, TwelveDataFeed
from factories import T0, make_instrument

KEY = "td-secret-key-123"
FIXTURE = Path(__file__).parent / "fixtures" / "twelvedata_eurusd_5min.json"
Handler = Callable[[httpx2.Request], httpx2.Response]


def at(hh: int, mm: int, ss: int = 0, *, day: int = 5) -> datetime:
    return datetime(2026, 10, day, hh, mm, ss, tzinfo=UTC)  # 2026-10-05 is a Monday


class StopClock(ManualClock):
    """ManualClock that closes the feed once simulated time reaches ``stop_at`` (ends the stream)."""

    def __init__(self, start: datetime) -> None:
        super().__init__(start)
        self.stop_at: datetime | None = None
        self.feed: TwelveDataFeed | None = None
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[float], None] | None = None

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep(seconds)
        await super().sleep(seconds)
        if self.stop_at is not None and self.feed is not None and self.now() >= self.stop_at:
            await self.feed.aclose()


@dataclass(frozen=True)
class Seen:
    at: datetime
    mono: float
    request: httpx2.Request

    @property
    def params(self) -> dict[str, str]:
        return dict(self.request.url.params)


class FakeTwelveData:
    """Simulated ``/time_series``: bars on the timeframe grid up to and including the forming bar."""

    def __init__(self, clock: ManualClock, *, tf: int = 5) -> None:
        self.clock = clock
        self.tf = timedelta(minutes=tf)
        self.requests: list[Seen] = []
        self.script: list[Handler] = []  # canned responses used (in order) before the simulation
        self.interval_echo: str | None = None

    def value(self, feed_symbol: str, start: datetime) -> dict[str, str]:
        i = int((start - at(0, 0)).total_seconds() // 60)
        base = 1.30 if feed_symbol.startswith("GBP") else 1.08
        o = base + 0.0001 * ((i * 7) % 13)
        c = o + 0.00005 * ((i % 3) - 1)
        return {
            "datetime": start.strftime("%Y-%m-%d %H:%M:%S"),
            "open": f"{o:.5f}",
            "high": f"{max(o, c) + 0.0002:.5f}",
            "low": f"{min(o, c) - 0.0002:.5f}",
            "close": f"{c:.5f}",
        }

    def body(self, params: dict[str, str]) -> dict[str, Any]:
        now = self.clock.now()
        step = int(self.tf.total_seconds())
        epoch = int(now.timestamp())
        forming = datetime.fromtimestamp(epoch - epoch % step, tz=UTC)
        n = int(params["outputsize"])
        starts = [forming - k * self.tf for k in range(n)][::-1]
        return {
            "meta": {"symbol": params["symbol"], "interval": self.interval_echo or params["interval"]},
            "values": [self.value(params["symbol"], s) for s in starts],
            "status": "ok",
        }

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(Seen(self.clock.now(), self.clock.monotonic(), request))
        if self.script:
            return self.script.pop(0)(request)
        return httpx2.Response(200, json=self.body(dict(request.url.params)))

    def times(self) -> list[datetime]:
        return [s.at for s in self.requests]


def respond(status: int = 200, **kwargs: Any) -> Handler:
    return lambda _request: httpx2.Response(status, **kwargs)


def error_json(code: int, message: str, http_status: int = 200) -> Handler:
    return respond(http_status, json={"code": code, "message": message, "status": "error"})


def gbpusd() -> InstrumentConfig:
    return make_instrument(symbol="GBPUSD", feed_symbol="GBP/USD", currencies=["GBP", "USD"])


def make_feed(
    server: Callable[[httpx2.Request], httpx2.Response],
    clock: ManualClock,
    *,
    instruments: Sequence[InstrumentConfig] | None = None,
    tf: int = 5,
    delay: float = 5.0,
    **td: Any,
) -> TwelveDataFeed:
    cfg = FeedConfig(kind="twelvedata", poll_delay_seconds=delay, twelvedata=td)
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(server))
    feed = TwelveDataFeed(cfg, list(instruments or [make_instrument()]), tf, KEY, clock, client=client)
    if isinstance(clock, StopClock):
        clock.feed = feed
    return feed


async def run_until(feed: TwelveDataFeed, clock: StopClock, stop_at: datetime) -> list[Bar]:
    clock.stop_at = stop_at
    return [bar async for bar in feed.stream()]


def starts(bars: Sequence[Bar], symbol: str | None = None) -> list[datetime]:
    return [b.start for b in bars if symbol is None or b.symbol == symbol]


def setup(start: datetime, **kwargs: Any) -> tuple[StopClock, FakeTwelveData, TwelveDataFeed]:
    clock = StopClock(start)
    server = FakeTwelveData(clock, tf=kwargs.get("tf", 5))
    feed = make_feed(server, clock, **kwargs)
    return clock, server, feed


# ----------------------------------------------------------------------------------- requests


def test_satisfies_the_protocol_and_has_no_quotes() -> None:
    _, _, feed = setup(at(8, 2, 30))
    typed: MarketDataFeed = feed
    assert typed.name == "twelvedata"
    assert feed.latest_quote("EURUSD") is None


@pytest.mark.anyio
async def test_request_parameters_and_auth_header() -> None:
    _, server, feed = setup(at(8, 2, 30))
    await feed.warmup(["EURUSD"], 10)
    assert len(server.requests) == 1
    request = server.requests[0].request
    assert request.method == "GET"
    assert request.url.scheme == "https" and request.url.host == "api.twelvedata.com"
    assert request.url.path == "/time_series"
    assert server.requests[0].params == {
        "symbol": "EUR/USD",
        "interval": "5min",
        "outputsize": "12",
        "timezone": "UTC",
        "order": "asc",
    }
    assert request.headers["Authorization"] == f"apikey {KEY}"
    assert KEY not in str(request.url)


@pytest.mark.anyio
async def test_custom_base_url_with_trailing_slash() -> None:
    _, server, feed = setup(at(8, 2, 30), base_url="https://td.example.test/v2/")
    await feed.warmup(["EURUSD"], 1)
    assert str(server.requests[0].request.url).startswith("https://td.example.test/v2/time_series?")


@pytest.mark.parametrize(("tf", "interval"), sorted(INTERVALS.items()))
def test_interval_mapping(tf: int, interval: str) -> None:
    _, _, feed = setup(at(8, 2, 30), tf=tf)
    assert feed.interval == interval


@pytest.mark.anyio
async def test_hourly_bars_request_1h_and_carry_the_timeframe() -> None:
    _, server, feed = setup(at(10, 30), tf=60)
    warm = await feed.warmup(["EURUSD"], 3)
    assert server.requests[0].params["interval"] == "1h"
    assert starts(warm["EURUSD"]) == [at(7, 0), at(8, 0), at(9, 0)]
    assert all(b.timeframe_minutes == 60 for b in warm["EURUSD"])


def test_invalid_construction() -> None:
    clock = ManualClock(T0)
    cfg = FeedConfig(kind="twelvedata")
    with pytest.raises(ValueError, match="timeframes"):
        TwelveDataFeed(cfg, [make_instrument()], 7, KEY, clock)
    with pytest.raises(ValueError, match="API key is empty"):
        TwelveDataFeed(cfg, [make_instrument()], 5, "  ", clock)
    with pytest.raises(ValueError, match="at least one"):
        TwelveDataFeed(cfg, [], 5, KEY, clock)
    with pytest.raises(ValueError, match="unique"):
        TwelveDataFeed(cfg, [make_instrument(), make_instrument()], 5, KEY, clock)


# ----------------------------------------------------------------------------------- parsing & warmup


@pytest.mark.anyio
async def test_documented_fixture_is_parsed_and_the_forming_bar_dropped() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    clock = StopClock(at(8, 2, 30))
    feed = make_feed(respond(json=payload), clock)
    bars = (await feed.warmup(["EURUSD"], 50))["EURUSD"]
    assert len(bars) == 11  # 08:00 is still forming at 08:02:30
    assert starts(bars) == [at(7, 5) + timedelta(minutes=5 * i) for i in range(11)]
    first = bars[0]
    assert (first.symbol, first.open, first.high, first.low, first.close) == ("EURUSD", 1.085, 1.0853, 1.0849, 1.0852)
    assert all(b.volume is None and b.timeframe_minutes == 5 for b in bars)
    assert all(b.start.tzinfo is UTC for b in bars)


@pytest.mark.anyio
async def test_a_bar_that_ends_exactly_now_is_closed() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    feed = make_feed(respond(json=payload), StopClock(at(8, 5)))
    bars = (await feed.warmup(["EURUSD"], 50))["EURUSD"]
    assert bars[-1].start == at(8, 0)


@pytest.mark.anyio
async def test_warmup_returns_the_last_n_closed_bars_and_caps_outputsize() -> None:
    _, server, feed = setup(at(8, 2, 30))
    bars = (await feed.warmup(["EURUSD"], 4))["EURUSD"]
    assert starts(bars) == [at(7, 40), at(7, 45), at(7, 50), at(7, 55)]
    await feed.warmup(["EURUSD"], 100_000)
    assert server.requests[-1].params["outputsize"] == "5000"


@pytest.mark.anyio
async def test_warmup_edge_cases(caplog: pytest.LogCaptureFixture) -> None:
    _, server, feed = setup(at(8, 2, 30))
    with caplog.at_level(logging.WARNING):
        assert await feed.warmup(["USDJPY"], 10) == {"USDJPY": []}
    assert "USDJPY" in caplog.text
    assert await feed.warmup(["EURUSD"], 0) == {"EURUSD": []}
    assert server.requests == []
    with pytest.raises(ValueError, match="bars"):
        await feed.warmup(["EURUSD"], -1)


@pytest.mark.anyio
async def test_warmup_ignores_sessions() -> None:
    _, server, feed = setup(at(12, 0, day=10))  # Saturday
    bars = (await feed.warmup(["EURUSD"], 3))["EURUSD"]
    assert len(bars) == 3 and len(server.requests) == 1


@pytest.mark.anyio
async def test_volume_numeric_values_and_descending_order_are_handled() -> None:
    values = [
        {"datetime": "2026-10-05 07:55:00", "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15, "volume": "42"},
        {"datetime": "2026-10-05 07:50:00", "open": "1.1", "high": "1.2", "low": "1.0", "close": "1.1"},
    ]
    feed = make_feed(respond(json={"values": values, "status": "ok"}), StopClock(at(8, 2)))
    bars = (await feed.warmup(["EURUSD"], 5))["EURUSD"]
    assert starts(bars) == [at(7, 50), at(7, 55)]
    assert bars[1].volume == 42.0 and bars[0].volume is None


@pytest.mark.anyio
async def test_malformed_values_are_skipped_and_counted(caplog: pytest.LogCaptureFixture) -> None:
    good = {"datetime": "2026-10-05 07:55:00", "open": "1.1", "high": "1.2", "low": "1.0", "close": "1.1"}
    values: list[Any] = [
        "not an object",
        {"open": "1.1", "high": "1.2", "low": "1.0", "close": "1.1"},
        {**good, "datetime": "05/10/2026 07:40"},
        {**good, "datetime": "2026-10-05 07:45:00", "high": "abc"},
        {**good, "datetime": "2026-10-05 07:45:00", "high": "1.05"},  # high below close
        {**good, "datetime": "2026-10-05 07:45:00", "low": "-1"},
        {**good, "datetime": "2026-10-05 07:45:00", "close": None},
        {**good, "datetime": "2026-10-05 07:45:00", "close": True},
        {**good, "datetime": "2026-10-05 07:45:00", "open": "nan"},
        good,
    ]
    feed = make_feed(respond(json={"values": values, "status": "ok"}), StopClock(at(8, 2)))
    with caplog.at_level(logging.WARNING):
        bars = (await feed.warmup(["EURUSD"], 5))["EURUSD"]
    assert starts(bars) == [at(7, 55)]
    assert feed.status()["symbols"]["EURUSD"]["malformed_values"] == 9
    assert "skipped 9 malformed value(s)" in caplog.text


@pytest.mark.parametrize(
    ("handler", "fragment"),
    [
        (respond(200, text="<html>gateway</html>"), "not a JSON object"),
        (respond(200, json=[1, 2, 3]), "not a JSON object"),
        (respond(200, json={"status": "ok"}), "no 'values' list"),
        (respond(500, text="oops"), "HTTP 500"),
        (respond(502, json={"code": 502, "message": "bad gateway", "status": "error"}), "bad gateway"),
        (error_json(400, "**symbol** not found"), "symbol** not found"),
        (error_json(404, "not found", http_status=404), "HTTP 404"),
        (respond(200, json={"status": "error", "message": "maintenance window"}), "maintenance window"),
    ],
)
@pytest.mark.anyio
async def test_transient_errors_in_warmup_give_no_bars(handler: Handler, fragment: str) -> None:
    feed = make_feed(handler, StopClock(at(8, 2, 30)))
    assert await feed.warmup(["EURUSD"], 5) == {"EURUSD": []}
    status = feed.status()
    assert fragment in (status["last_error"] or "")
    assert status["symbols"]["EURUSD"]["last_error"] == status["last_error"]
    assert status["errors"] == 1 and status["fatal"] is None


@pytest.mark.anyio
async def test_meta_interval_mismatch_is_rejected() -> None:
    clock = StopClock(at(8, 2, 30))
    server = FakeTwelveData(clock)
    server.interval_echo = "1min"
    feed = make_feed(server, clock)
    assert await feed.warmup(["EURUSD"], 5) == {"EURUSD": []}
    assert "does not match the requested '5min'" in (feed.status()["last_error"] or "")


# ----------------------------------------------------------------------------------- auth errors


@pytest.mark.parametrize(
    "handler",
    [
        error_json(401, "**apikey** parameter is incorrect", http_status=401),
        error_json(403, "this endpoint is not available on your plan", http_status=403),
        error_json(401, "**apikey** parameter is incorrect"),  # HTTP 200 with an error body
        error_json(403, "plan does not allow this symbol"),
        respond(401, text="unauthorized"),
    ],
)
@pytest.mark.anyio
async def test_401_and_403_raise_feed_error_from_warmup(handler: Handler) -> None:
    feed = make_feed(handler, StopClock(at(8, 2, 30)))
    with pytest.raises(FeedError, match="TWELVEDATA_API_KEY"):
        await feed.warmup(["EURUSD"], 5)
    assert feed.status()["fatal"] is not None


@pytest.mark.anyio
async def test_401_raises_feed_error_from_the_stream() -> None:
    clock = StopClock(at(8, 2, 30))
    feed = make_feed(error_json(401, "**apikey** parameter is incorrect", http_status=401), clock)
    clock.stop_at = at(9, 0)
    with pytest.raises(FeedError):
        async for _ in feed.stream():
            pass
    assert "refused" in (feed.status()["fatal"] or "")


# ----------------------------------------------------------------------------------- schedule & streaming


def test_next_poll_at_is_the_next_bar_close_plus_the_delay() -> None:
    _, _, feed = setup(at(8, 2, 30))
    assert feed.next_poll_at(at(8, 2, 30)) == at(8, 5, 5)
    assert feed.next_poll_at(at(8, 5, 3)) == at(8, 5, 5)  # 08:00 bar closed, its poll is not due yet
    assert feed.next_poll_at(at(8, 5, 6)) == at(8, 10, 5)
    assert feed.next_poll_at() == at(8, 5, 5)  # defaults to clock.now()
    _, _, no_delay = setup(at(8, 2, 30), delay=0.0)
    assert no_delay.next_poll_at(at(8, 5)) == at(8, 10)
    _, _, hourly = setup(at(8, 2, 30), tf=60)
    assert hourly.next_poll_at(at(8, 2, 30)) == at(9, 0, 5)


@pytest.mark.anyio
async def test_stream_polls_once_per_bar_close_plus_delay() -> None:
    clock, server, feed = setup(at(8, 2, 30))
    bars = await run_until(feed, clock, at(8, 16))
    assert server.times() == [at(8, 5, 5), at(8, 10, 5), at(8, 15, 5)]
    # Without warmup the first poll yields only the latest closed bar; then one new bar per close.
    assert starts(bars) == [at(8, 0), at(8, 5), at(8, 10)]
    assert all(s.params["outputsize"] == "3" for s in server.requests)


@pytest.mark.anyio
async def test_dedup_across_warmup_and_overlapping_polls() -> None:
    clock, server, feed = setup(at(8, 2, 30))
    warm = (await feed.warmup(["EURUSD"], 20))["EURUSD"]
    assert warm[-1].start == at(7, 55)
    bars = await run_until(feed, clock, at(8, 21))
    assert starts(bars) == [at(8, 0), at(8, 5), at(8, 10), at(8, 15)]
    assert len(server.requests) == 5  # warmup + four polls, each window overlapping the previous one


@pytest.mark.anyio
async def test_stream_started_mid_bar_after_warmup_does_not_repoll_immediately() -> None:
    clock, server, feed = setup(at(8, 5, 30))
    await feed.warmup(["EURUSD"], 5)
    bars = await run_until(feed, clock, at(8, 11))
    assert server.times()[1:] == [at(8, 10, 5)]
    assert starts(bars) == [at(8, 5)]


@pytest.mark.anyio
async def test_catch_up_after_failed_polls_yields_missing_bars_in_order() -> None:
    clock, server, feed = setup(at(8, 2, 30))
    await feed.warmup(["EURUSD"], 5)
    server.script = [respond(500, text="down"), respond(503, text="down")]
    bars = await run_until(feed, clock, at(8, 16))
    assert server.times()[1:] == [at(8, 5, 5), at(8, 10, 5), at(8, 15, 5)]
    assert server.requests[-1].params["outputsize"] == "5"
    assert starts(bars) == [at(8, 0), at(8, 5), at(8, 10)]


@pytest.mark.anyio
async def test_symbol_mapping_feed_symbol_to_broker_symbol() -> None:
    clock, server, feed = setup(at(8, 2, 30), instruments=[make_instrument(), gbpusd()])
    bars = await run_until(feed, clock, at(8, 11))
    assert [s.params["symbol"] for s in server.requests] == ["EUR/USD", "GBP/USD"] * 2
    assert {b.symbol for b in bars} == {"EURUSD", "GBPUSD"}
    assert starts(bars, "EURUSD") == [at(8, 0), at(8, 5)] == starts(bars, "GBPUSD")
    gbp = [b for b in bars if b.symbol == "GBPUSD"]
    assert gbp[0].open > 1.29  # the GBP/USD series, not EUR/USD relabelled


@pytest.mark.anyio
async def test_error_json_with_http_200_is_logged_and_retried_next_cycle(caplog: pytest.LogCaptureFixture) -> None:
    clock, server, feed = setup(at(8, 2, 30))
    server.script = [error_json(400, "**symbol** EUR/USD is temporarily unavailable")]
    with caplog.at_level(logging.WARNING):
        bars = await run_until(feed, clock, at(8, 11))
    assert server.times() == [at(8, 5, 5), at(8, 10, 5)]  # no immediate retry
    assert starts(bars) == [at(8, 5)]
    assert "temporarily unavailable" in caplog.text
    status = feed.status()
    assert "temporarily unavailable" in (status["last_error"] or "")
    assert status["symbols"]["EURUSD"]["last_ok_at"] == at(8, 10, 5).isoformat()


@pytest.mark.anyio
async def test_network_errors_and_timeouts_are_retried_next_cycle() -> None:
    clock, server, feed = setup(at(8, 2, 30))

    def connect_error(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    server.script = [connect_error, timeout]
    bars = await run_until(feed, clock, at(8, 16))
    assert server.times() == [at(8, 5, 5), at(8, 10, 5), at(8, 15, 5)]
    assert starts(bars) == [at(8, 10)]
    assert feed.status()["errors"] == 2


# ----------------------------------------------------------------------------------- rate limits & budgets


@pytest.mark.parametrize(
    "limited",
    [
        respond(429, json={"code": 429, "message": "run out of API credits for the current minute", "status": "error"}),
        error_json(429, "run out of API credits for the current minute"),
    ],
)
@pytest.mark.anyio
async def test_429_waits_for_the_next_minute(limited: Handler) -> None:
    clock, server, feed = setup(at(8, 2, 30))
    server.script = [limited]
    blocked: list[Any] = []
    clock.on_sleep = lambda seconds: blocked.append((seconds, feed.status()["credits"]["rate_limited_until"]))
    bars = await run_until(feed, clock, at(8, 7))
    assert server.times() == [at(8, 5, 5), at(8, 6, 1)]  # nothing in between
    assert starts(bars) == [at(8, 0)]
    assert (56.0, at(8, 6, 1).isoformat()) in blocked  # reported while waiting
    assert "rate limited" in (feed.status()["last_error"] or "")


@pytest.mark.anyio
async def test_429_in_warmup_retries_after_the_minute() -> None:
    _clock, server, feed = setup(at(8, 2, 30))
    server.script = [error_json(429, "too many requests", http_status=429)]
    bars = (await feed.warmup(["EURUSD"], 3))["EURUSD"]
    assert server.times() == [at(8, 2, 30), at(8, 3, 1)]
    assert len(bars) == 3


@pytest.mark.anyio
async def test_persistent_429_gives_up_for_the_cycle() -> None:
    clock, server, feed = setup(at(8, 2, 30))
    server.script = [error_json(429, "slow down")] * 3
    bars = await run_until(feed, clock, at(8, 11))
    assert server.times() == [at(8, 5, 5), at(8, 6, 1), at(8, 7, 1), at(8, 10, 5)]
    assert starts(bars) == [at(8, 5)]


def _max_in_window(times: Sequence[float], window: float) -> int:
    return max((sum(1 for u in times if t - window < u <= t) for t in times), default=0)


def _instruments(n: int) -> list[InstrumentConfig]:
    return [make_instrument(symbol=f"SYM{i}", feed_symbol=f"S{i}/USD") for i in range(n)]


@pytest.mark.anyio
async def test_per_minute_budget_is_never_exceeded_and_everyone_gets_polled() -> None:
    clock, server, feed = setup(at(8, 2, 30), instruments=_instruments(10), per_minute_credit_limit=3)
    await feed.warmup([f"SYM{i}" for i in range(10)], 5)
    bars = await run_until(feed, clock, at(8, 31))
    monos = [s.mono for s in server.requests]
    assert _max_in_window(monos, 60.0) <= 3
    per_minute: dict[datetime, int] = {}
    for t in server.times():
        key = t.replace(second=0, microsecond=0)
        per_minute[key] = per_minute.get(key, 0) + 1
    assert max(per_minute.values()) <= 3
    for i in range(10):
        assert starts(bars, f"SYM{i}")[-1] >= at(8, 20)  # nobody starves
    assert feed.status()["credits"]["minute_limit"] == 3


@pytest.mark.anyio
async def test_budget_too_small_for_all_symbols_skips_and_rotates() -> None:
    clock, server, feed = setup(at(8, 2, 30), instruments=_instruments(12), per_minute_credit_limit=2)
    bars = await run_until(feed, clock, at(8, 41))
    assert _max_in_window([s.mono for s in server.requests], 60.0) <= 2
    status = feed.status()
    assert status["skipped_for_budget"] > 0
    polled = {s.params["symbol"] for s in server.requests}
    assert polled == {f"S{i}/USD" for i in range(12)}  # rotation: skipped symbols go first next cycle
    assert {b.symbol for b in bars} == {f"SYM{i}" for i in range(12)}


@pytest.mark.anyio
async def test_daily_budget_is_never_exceeded_and_resets_at_utc_midnight() -> None:
    clock, server, feed = setup(at(23, 21), daily_credit_limit=5, poll_outside_sessions=True)
    bars = await run_until(feed, clock, at(0, 6, day=6))
    by_day: dict[Any, int] = {}
    for t in server.times():
        by_day[t.date()] = by_day.get(t.date(), 0) + 1
    assert by_day == {at(0, 0).date(): 5, at(0, 0, day=6).date(): 2}
    assert server.times()[:5] == [at(23, 25, 5), at(23, 30, 5), at(23, 35, 5), at(23, 40, 5), at(23, 45, 5)]
    assert server.times()[5:] == [at(0, 0, 5, day=6), at(0, 5, 5, day=6)]
    # 23:50 and 23:55 were skipped; the first poll of the new day catches up on them.
    assert starts(bars)[-4:] == [at(23, 45), at(23, 50), at(23, 55), at(0, 0, day=6)]
    status = feed.status()
    assert status["skipped_for_budget"] == 2
    assert status["credits"]["day_used"] == 2 and status["credits"]["day"] == "2026-10-06"
    assert "daily credit budget (5) used up" in (status["last_error"] or "")


@pytest.mark.anyio
async def test_daily_exhaustion_is_reported_once_per_day(caplog: pytest.LogCaptureFixture) -> None:
    clock, server, feed = setup(at(9, 0), daily_credit_limit=1)
    with caplog.at_level(logging.ERROR):
        await run_until(feed, clock, at(9, 30))
    assert len(server.requests) == 1
    assert caplog.text.count("daily credit budget (1) used up") == 1
    assert "daily credit budget" in (feed.status()["last_error"] or "")


@pytest.mark.anyio
async def test_warmup_waits_for_minute_credits() -> None:
    _clock, server, feed = setup(at(8, 2, 30), instruments=_instruments(5), per_minute_credit_limit=2)
    warm = await feed.warmup([f"SYM{i}" for i in range(5)], 3)
    assert all(len(v) == 3 for v in warm.values())
    assert _max_in_window([s.mono for s in server.requests], 60.0) <= 2
    assert server.times() == [at(8, 2, 30), at(8, 2, 30), at(8, 3, 31), at(8, 3, 31), at(8, 4, 32)]


# ----------------------------------------------------------------------------------- sessions


@pytest.mark.anyio
async def test_polls_only_while_the_just_closed_bar_is_in_session() -> None:
    clock, server, feed = setup(at(19, 47))  # session Mon-Fri 07:00-20:00
    bars = await run_until(feed, clock, at(21, 0))
    assert server.times() == [at(19, 50, 5), at(19, 55, 5), at(20, 0, 5)]
    assert starts(bars) == [at(19, 45), at(19, 50), at(19, 55)]


@pytest.mark.anyio
async def test_no_polls_on_the_weekend() -> None:
    clock, server, feed = setup(at(10, 0, day=10))  # Saturday
    await run_until(feed, clock, at(14, 0, day=10))
    assert server.requests == []
    assert feed.status()["symbols"]["EURUSD"]["polling"] is False


@pytest.mark.anyio
async def test_poll_outside_sessions_keeps_polling() -> None:
    clock, server, feed = setup(at(19, 47), poll_outside_sessions=True)
    await run_until(feed, clock, at(20, 16))
    assert server.times() == [at(19, 50, 5), at(19, 55, 5), at(20, 0, 5), at(20, 5, 5), at(20, 10, 5), at(20, 15, 5)]
    assert feed.status()["symbols"]["EURUSD"]["polling"] is True


@pytest.mark.anyio
async def test_session_opening_catches_up_from_the_last_bar() -> None:
    clock, server, feed = setup(at(6, 40))
    await feed.warmup(["EURUSD"], 3)  # last closed bar 06:35
    bars = await run_until(feed, clock, at(7, 6))
    assert server.times()[1:] == [at(7, 5, 5)]  # 07:00 is the first in-session bar
    assert server.requests[-1].params["outputsize"] == "7"
    assert starts(bars)[0] == at(6, 40) and starts(bars)[-1] == at(7, 0)


# ----------------------------------------------------------------------------------- status & secrets


@pytest.mark.anyio
async def test_status_reports_credits_schedule_and_symbols() -> None:
    _clock, _server, feed = setup(at(8, 2, 30), instruments=[make_instrument(), gbpusd()])
    status = feed.status()
    assert status["next_poll_at"] is None and status["last_poll_at"] is None
    await feed.warmup(["EURUSD", "GBPUSD"], 3)
    gen = feed.stream()
    async with contextlib.aclosing(gen):
        await gen.__anext__()
        status = feed.status()
    json.dumps(status)
    assert status["kind"] == "twelvedata" and status["interval"] == "5min" and status["timeframe_minutes"] == 5
    assert status["poll_delay_seconds"] == 5.0 and status["poll_outside_sessions"] is False
    budget = status["credits"]
    assert budget["minute_used"] == 1 and budget["minute_limit"] == 8  # warmup calls left the 61 s window
    assert budget["day_used"] == 3 and budget["day_limit"] == 800 and budget["day_remaining"] == 797
    assert budget["day"] == "2026-10-05" and budget["rate_limited_until"] is None
    assert status["requests"] == 3 and status["errors"] == 0 and status["fatal"] is None
    assert status["last_poll_at"] == at(8, 5, 5).isoformat()
    assert status["next_poll_at"] == at(8, 10, 5).isoformat()
    eur = status["symbols"]["EURUSD"]
    assert eur["feed_symbol"] == "EUR/USD" and eur["polling"] is True
    assert eur["last_bar"] == at(8, 0).isoformat() and eur["bars_emitted"] == 1 and eur["requests"] == 2
    assert status["symbols"]["GBPUSD"]["last_bar"] == at(7, 55).isoformat()  # not polled yet this cycle


@pytest.mark.anyio
async def test_the_api_key_never_reaches_logs_or_status(caplog: pytest.LogCaptureFixture) -> None:
    _clock, server, feed = setup(at(8, 2, 30))
    server.script = [error_json(400, f"invalid request for apikey={KEY}")]
    with caplog.at_level(logging.DEBUG):
        await feed.warmup(["EURUSD"], 3)
        await feed.warmup(["EURUSD"], 3)
    assert KEY not in caplog.text
    dumped = json.dumps(feed.status())
    assert KEY not in dumped
    assert "apikey=***" in dumped


# ----------------------------------------------------------------------------------- lifecycle


@pytest.mark.anyio
async def test_concurrent_streams_are_refused() -> None:
    _clock, _server, feed = setup(at(8, 2, 30))
    gen = feed.stream()
    async with contextlib.aclosing(gen):
        await gen.__anext__()
        with pytest.raises(RuntimeError, match="already running"):
            await feed.stream().__anext__()


@pytest.mark.anyio
async def test_aclose_wakes_a_sleeping_stream() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(500)

    class BlockingClock(ManualClock):
        async def sleep(self, seconds: float) -> None:
            await asyncio.Event().wait()  # never returns by itself

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    feed = TwelveDataFeed(FeedConfig(kind="twelvedata"), [make_instrument()], 5, KEY, BlockingClock(T0), client=client)

    async def consume() -> list[Bar]:
        return [bar async for bar in feed.stream()]

    task = asyncio.create_task(consume())
    for _ in range(10):
        await asyncio.sleep(0)
    assert not task.done()  # sleeping until the next bar close
    await feed.aclose()
    assert await asyncio.wait_for(task, timeout=1.0) == []
    assert requests == []
    assert feed.status()["closed"] is True and feed.status()["next_poll_at"] is None
    assert not client.is_closed  # an injected client belongs to the caller
    await client.aclose()


@pytest.mark.anyio
async def test_aclose_closes_an_owned_client_once() -> None:
    feed = TwelveDataFeed(FeedConfig(kind="twelvedata"), [make_instrument()], 5, KEY, ManualClock(T0))
    await feed.aclose()
    await feed.aclose()
    assert feed._client.is_closed
