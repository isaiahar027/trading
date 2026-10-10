from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from amongus_desk.domain import Bar
from amongus_desk.marketdata.base import FeedError, MarketDataFeed
from amongus_desk.marketdata.csv_replay import CsvFormatError, CsvReplayFeed, load_csv_bars

FIXTURE = Path(__file__).parent / "fixtures" / "sample_bars.csv"
HEADER = "symbol,start,open,high,low,close,volume\n"
T0 = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)


def write_csv(tmp_path: Path, body: str, *, header: str = HEADER, name: str = "bars.csv") -> Path:
    path = tmp_path / name
    path.write_text(header + body, encoding="utf-8")
    return path


def row(symbol: str, minute: int, o: float = 1.1, h: float = 1.1005, lo: float = 1.0995, c: float = 1.1002) -> str:
    start = (T0 + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")
    return f"{symbol},{start},{o},{h},{lo},{c},\n"


async def drain(feed: CsvReplayFeed) -> list[Bar]:
    return [bar async for bar in feed.stream()]


def expect_error(path: Path, line: int | None, *fragments: str) -> CsvFormatError:
    with pytest.raises(CsvFormatError) as info:
        load_csv_bars(path)
    err = info.value
    assert err.line == line, str(err)
    for fragment in fragments:
        assert fragment in str(err), str(err)
    if line is not None:
        assert f"line {line}" in str(err)
    return err


# ----------------------------------------------------------------------------------- parsing the fixture


def test_fixture_loads_every_bar_per_symbol_sorted() -> None:
    data = load_csv_bars(FIXTURE)
    assert list(data) == ["EURUSD", "GBPUSD"]
    for symbol, bars in data.items():
        assert len(bars) == 10
        assert [b.start for b in bars] == [T0 + timedelta(minutes=5 * i) for i in range(10)]
        assert all(b.symbol == symbol and b.timeframe_minutes == 5 for b in bars)


def test_offsets_z_utc_and_non_utc_are_normalised_to_utc() -> None:
    eur = load_csv_bars(FIXTURE)["EURUSD"]
    # line 2 uses "Z", line 4 "+00:00", line 6 "10:10:00+02:00", line 14 a space separator.
    assert eur[0].start == T0
    assert eur[1].start == T0 + timedelta(minutes=5)
    assert eur[2].start == T0 + timedelta(minutes=10)
    assert eur[6].start == T0 + timedelta(minutes=30)
    assert all(b.start.tzinfo is UTC for b in eur)


def test_prices_and_optional_volume() -> None:
    data = load_csv_bars(FIXTURE)
    first = data["EURUSD"][0]
    assert (first.open, first.high, first.low, first.close, first.volume) == (1.1, 1.1004, 1.0998, 1.1002, 120.0)
    assert data["EURUSD"][2].volume is None  # empty cell
    assert all(b.volume is None for b in data["GBPUSD"])


def test_volume_column_is_optional(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "EURUSD,2026-10-05T08:00:00Z,1.1,1.2,1.0,1.1\nEURUSD,2026-10-05T08:05:00Z,1.1,1.2,1.0,1.1\n",
        header="symbol,start,open,high,low,close\n",
    )
    bars = load_csv_bars(path)["EURUSD"]
    assert len(bars) == 2 and bars[0].volume is None


def test_header_is_case_insensitive_any_order_and_bom_tolerant(tmp_path: Path) -> None:
    path = tmp_path / "bom.csv"
    path.write_text(
        "﻿Close, Symbol ,START,Open,High,Low\n1.1002,EURUSD,2026-10-05T08:00:00Z,1.1,1.1005,1.0995\n"
        "1.1003,EURUSD,2026-10-05T08:05:00Z,1.1002,1.1006,1.0999\n",
        encoding="utf-8",
    )
    bars = load_csv_bars(path, timeframe_minutes=5)["EURUSD"]
    assert bars[1].close == 1.1003 and bars[1].open == 1.1002


def test_blank_lines_are_skipped_but_line_numbers_stay_physical(tmp_path: Path) -> None:
    path = write_csv(tmp_path, row("EURUSD", 0) + "\n\n" + row("EURUSD", 5) + "EURUSD,2026-10-05T08:10:00Z,x,1,1,1,\n")
    expect_error(path, 6, "open 'x' is not a number")


def test_rows_in_any_order_are_sorted(tmp_path: Path) -> None:
    path = write_csv(tmp_path, row("EURUSD", 10) + row("EURUSD", 0) + row("GBPUSD", 5) + row("EURUSD", 5))
    data = load_csv_bars(path)
    assert [b.start for b in data["EURUSD"]] == [T0, T0 + timedelta(minutes=5), T0 + timedelta(minutes=10)]
    assert [b.start for b in data["GBPUSD"]] == [T0 + timedelta(minutes=5)]


# ----------------------------------------------------------------------------------- timestamps


def test_naive_timestamp_is_rejected_with_a_clear_error(tmp_path: Path) -> None:
    path = write_csv(tmp_path, row("EURUSD", 0) + "EURUSD,2026-10-05T08:05:00,1.1,1.1005,1.0995,1.1002,\n")
    err = expect_error(path, 3, "no UTC offset", "2026-10-05T08:05:00", "Z")
    assert isinstance(err, FeedError) and isinstance(err, ValueError)


def test_date_only_timestamp_is_naive_and_rejected(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "EURUSD,2026-10-05,1.1,1.1005,1.0995,1.1002,\n")
    expect_error(path, 2, "no UTC offset")


def test_garbage_timestamp_is_rejected(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "EURUSD,yesterday,1.1,1.1005,1.0995,1.1002,\n")
    expect_error(path, 2, "not an ISO-8601 timestamp")


# ----------------------------------------------------------------------------------- bad rows


@pytest.mark.parametrize(
    ("bad_row", "fragment"),
    [
        ("EURUSD,2026-10-05T08:05:00Z,abc,1.1005,1.0995,1.1002,", "open 'abc' is not a number"),
        ("EURUSD,2026-10-05T08:05:00Z,1.1,,1.0995,1.1002,", "high is empty"),
        ("EURUSD,2026-10-05T08:05:00Z,1.1,nan,1.0995,1.1002,", "not a finite number"),
        ("EURUSD,2026-10-05T08:05:00Z,1.1,inf,1.0995,1.1002,", "not a finite number"),
        ("EURUSD,2026-10-05T08:05:00Z,1.1,1.1005,-1.0,1.1002,", "low must be positive"),
        ("EURUSD,2026-10-05T08:05:00Z,0,1.1005,1.0995,1.1002,", "open must be positive"),
        ("EURUSD,2026-10-05T08:05:00Z,1.1,1.1001,1.0995,1.1002,", "inconsistent OHLC"),
        ("EURUSD,2026-10-05T08:05:00Z,1.1,1.1005,1.1001,1.1002,", "inconsistent OHLC"),
        ("EURUSD,2026-10-05T08:05:00Z,1.1,1.1005,1.0995,1.1002,-5", "volume must not be negative"),
        ("EURUSD,2026-10-05T08:05:00Z,1.1,1.1005,1.0995,1.1002,lots", "volume 'lots' is not a number"),
        (",2026-10-05T08:05:00Z,1.1,1.1005,1.0995,1.1002,", "symbol is empty"),
        ("EURUSD,2026-10-05T08:05:00Z,1.1,1.1005,1.0995", "expected 7 fields, found 5"),
        ("EURUSD,2026-10-05T08:05:00Z,1.1,1.1005,1.0995,1.1002,1,extra", "expected 7 fields, found 8"),
    ],
)
def test_bad_rows_name_the_line(tmp_path: Path, bad_row: str, fragment: str) -> None:
    path = write_csv(tmp_path, row("EURUSD", 0) + bad_row + "\n" + row("EURUSD", 10))
    err = expect_error(path, 3, fragment)
    assert str(path) in str(err)


def test_duplicate_bars_are_rejected_with_both_lines(tmp_path: Path) -> None:
    path = write_csv(tmp_path, row("EURUSD", 0) + row("GBPUSD", 0) + row("EURUSD", 5) + row("EURUSD", 0))
    expect_error(path, 5, "duplicate EURUSD bar", "also on line 2")


def test_missing_and_unknown_columns_are_rejected(tmp_path: Path) -> None:
    missing = write_csv(tmp_path, "EURUSD,2026-10-05T08:00:00Z,1.1,1.2,1.0\n", header="symbol,start,open,high,low\n")
    expect_error(missing, 1, "missing column(s) close")
    unknown = write_csv(
        tmp_path, "EURUSD,2026-10-05T08:00:00Z,1.1,1.2,1.0,1.1,3\n", header="symbol,time,open,high,low,close,spread\n"
    )
    expect_error(unknown, 1, "missing column(s) start")
    extra = write_csv(
        tmp_path, "EURUSD,2026-10-05T08:00:00Z,1.1,1.2,1.0,1.1,3\n", header="symbol,start,open,high,low,close,spread\n"
    )
    expect_error(extra, 1, "unexpected column(s) 'spread'")
    twice = write_csv(tmp_path, "", header="symbol,start,open,high,low,close,close\n")
    expect_error(twice, 1, "'close' appears twice")


def test_empty_file_and_header_only_are_rejected(tmp_path: Path) -> None:
    expect_error(write_csv(tmp_path, "", header=""), None, "empty")
    expect_error(write_csv(tmp_path, "", header=HEADER), None, "no bars")
    expect_error(write_csv(tmp_path, "\n\n", header="\n"), None, "empty")


def test_missing_file_is_a_csv_format_error(tmp_path: Path) -> None:
    expect_error(tmp_path / "nope.csv", None, "cannot open")


def test_non_utf8_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "latin1.csv"
    path.write_bytes(HEADER.encode() + "EURUSD,2026-10-05T08:00:00Z,1.1,1.2,1.0,1.1,\xe9\n".encode("latin-1"))
    expect_error(path, None, "not UTF-8")


# ----------------------------------------------------------------------------------- timeframe


def test_timeframe_is_inferred_from_the_smallest_spacing(tmp_path: Path) -> None:
    path = write_csv(tmp_path, row("EURUSD", 0) + row("EURUSD", 15) + row("EURUSD", 45) + row("GBPUSD", 0))
    data = load_csv_bars(path)
    assert {b.timeframe_minutes for bars in data.values() for b in bars} == {15}


def test_gaps_must_be_multiples_of_the_timeframe(tmp_path: Path) -> None:
    path = write_csv(tmp_path, row("EURUSD", 0) + row("EURUSD", 5) + row("EURUSD", 12))
    expect_error(path, 4, "7 min after the previous bar (line 3)", "multiple of the 5-minute timeframe")


def test_explicit_timeframe_rejects_finer_data(tmp_path: Path) -> None:
    path = write_csv(tmp_path, row("EURUSD", 0) + row("EURUSD", 1) + row("EURUSD", 2))
    with pytest.raises(CsvFormatError, match="multiple of the 5-minute timeframe"):
        load_csv_bars(path, timeframe_minutes=5)
    assert len(load_csv_bars(path, timeframe_minutes=1)["EURUSD"]) == 3


def test_explicit_timeframe_allows_missing_bars(tmp_path: Path) -> None:
    path = write_csv(tmp_path, row("EURUSD", 0) + row("EURUSD", 5) + row("EURUSD", 25))
    assert len(load_csv_bars(path, timeframe_minutes=5)["EURUSD"]) == 3


def test_timeframe_cannot_be_inferred_from_single_bars(tmp_path: Path) -> None:
    path = write_csv(tmp_path, row("EURUSD", 0) + row("GBPUSD", 5))
    expect_error(path, None, "cannot infer the timeframe")
    assert len(load_csv_bars(path, timeframe_minutes=5)) == 2


def test_sub_minute_spacing_cannot_be_a_timeframe(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "EURUSD,2026-10-05T08:00:00Z,1.1,1.2,1.0,1.1,\nEURUSD,2026-10-05T08:00:30Z,1.1,1.2,1.0,1.1,\n",
    )
    expect_error(path, None, "smallest bar spacing is 30 s")


def test_invalid_timeframe_argument() -> None:
    with pytest.raises(ValueError, match="positive"):
        load_csv_bars(FIXTURE, timeframe_minutes=0)
    with pytest.raises(ValueError, match="positive"):
        CsvReplayFeed(FIXTURE, 0)


# ----------------------------------------------------------------------------------- the replay feed


def test_feed_satisfies_the_protocol() -> None:
    feed: MarketDataFeed = CsvReplayFeed(FIXTURE, 5)
    assert feed.name == "csv"
    assert feed.latest_quote("EURUSD") is None


def test_broken_file_fails_at_construction(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "EURUSD,2026-10-05T08:00:00,1.1,1.2,1.0,1.1,\n")
    with pytest.raises(CsvFormatError, match="no UTC offset"):
        CsvReplayFeed(path, 5)


@pytest.mark.anyio
async def test_warmup_consumes_the_first_bars_and_stream_yields_the_rest() -> None:
    feed = CsvReplayFeed(FIXTURE, 5)
    warm = await feed.warmup(["EURUSD", "GBPUSD"], 3)
    assert [b.start for b in warm["EURUSD"]] == [T0 + timedelta(minutes=5 * i) for i in range(3)]
    assert [b.start for b in warm["GBPUSD"]] == [T0 + timedelta(minutes=5 * i) for i in range(3)]
    streamed = await drain(feed)
    assert len(streamed) == 14
    warm_keys = {(b.symbol, b.start) for bars in warm.values() for b in bars}
    stream_keys = [(b.symbol, b.start) for b in streamed]
    assert len(set(stream_keys)) == len(stream_keys)  # every bar at most once
    assert warm_keys.isdisjoint(stream_keys)
    assert len(warm_keys) + len(stream_keys) == 20  # and nothing lost
    assert feed.status()["exhausted"] is True


@pytest.mark.anyio
async def test_stream_interleaves_symbols_in_global_time_order() -> None:
    feed = CsvReplayFeed(FIXTURE, 5)
    streamed = await drain(feed)
    assert [(b.start, b.symbol) for b in streamed] == sorted((b.start, b.symbol) for b in streamed)
    assert [b.symbol for b in streamed[:4]] == ["EURUSD", "GBPUSD", "EURUSD", "GBPUSD"]


@pytest.mark.anyio
async def test_symbols_with_different_histories_merge_in_time_order(tmp_path: Path) -> None:
    body = "".join(row("EURUSD", 5 * i) for i in range(6)) + "".join(row("GBPUSD", 15 + 5 * i) for i in range(3))
    feed = CsvReplayFeed(write_csv(tmp_path, body), 5)
    await feed.warmup(["EURUSD", "GBPUSD"], 2)
    streamed = await drain(feed)
    assert [(b.symbol, int((b.start - T0).total_seconds() // 60)) for b in streamed] == [
        ("EURUSD", 10),
        ("EURUSD", 15),
        ("EURUSD", 20),
        ("EURUSD", 25),
        ("GBPUSD", 25),
    ]


@pytest.mark.anyio
async def test_warmup_larger_than_the_data_takes_everything() -> None:
    feed = CsvReplayFeed(FIXTURE, 5)
    warm = await feed.warmup(["EURUSD"], 1000)
    assert len(warm["EURUSD"]) == 10
    streamed = await drain(feed)
    assert {b.symbol for b in streamed} == {"GBPUSD"} and len(streamed) == 10


@pytest.mark.anyio
async def test_warmup_edge_cases() -> None:
    feed = CsvReplayFeed(FIXTURE, 5)
    assert await feed.warmup(["EURUSD"], 0) == {"EURUSD": []}
    assert await feed.warmup(["USDJPY"], 5) == {"USDJPY": []}
    with pytest.raises(ValueError, match="bars"):
        await feed.warmup(["EURUSD"], -1)


@pytest.mark.anyio
async def test_warmup_after_streaming_started_is_refused() -> None:
    feed = CsvReplayFeed(FIXTURE, 5)
    gen = feed.stream()
    async with contextlib.aclosing(gen):
        await gen.__anext__()
    with pytest.raises(RuntimeError, match="before stream"):
        await feed.warmup(["EURUSD"], 1)


@pytest.mark.anyio
async def test_symbol_filter_and_missing_symbols(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        feed = CsvReplayFeed(FIXTURE, 5, symbols=["GBPUSD", "XAUUSD"])
    assert "XAUUSD" in caplog.text
    assert feed.symbols == ["GBPUSD"]
    warm = await feed.warmup(["GBPUSD", "XAUUSD", "EURUSD"], 3)
    assert len(warm["GBPUSD"]) == 3
    assert warm["XAUUSD"] == [] and warm["EURUSD"] == []  # EURUSD is in the file but was not selected
    streamed = await drain(feed)
    assert {b.symbol for b in streamed} == {"GBPUSD"} and len(streamed) == 7
    assert feed.status()["missing_symbols"] == ["XAUUSD"]


@pytest.mark.anyio
async def test_bars_carry_the_feed_timeframe() -> None:
    feed = CsvReplayFeed(FIXTURE, 5)
    assert all(b.timeframe_minutes == 5 for b in await drain(feed))


@pytest.mark.anyio
async def test_aclose_stops_the_replay_early() -> None:
    feed = CsvReplayFeed(FIXTURE, 5)
    seen: list[Bar] = []
    async for bar in feed.stream():
        seen.append(bar)
        if len(seen) == 4:
            await feed.aclose()
    assert len(seen) == 4
    assert feed.status()["closed"] is True


@pytest.mark.anyio
async def test_an_abandoned_stream_resumes_without_repeating_bars() -> None:
    feed = CsvReplayFeed(FIXTURE, 5)
    first: list[Bar] = []
    gen = feed.stream()
    async with contextlib.aclosing(gen):
        async for bar in gen:
            first.append(bar)
            if len(first) == 5:
                break
    rest = await drain(feed)
    assert len(first) + len(rest) == 20
    keys = [(b.symbol, b.start) for b in first + rest]
    assert len(set(keys)) == 20


@pytest.mark.anyio
async def test_status_tracks_progress_and_is_json_safe() -> None:
    feed = CsvReplayFeed(FIXTURE, 5)
    await feed.warmup(["EURUSD", "GBPUSD"], 2)
    gen = feed.stream()
    async with contextlib.aclosing(gen):
        for _ in range(3):
            await gen.__anext__()
    status = feed.status()
    json.dumps(status)
    assert status["kind"] == "csv" and status["timeframe_minutes"] == 5
    assert status["bars_total"] == 20 and status["bars_warmup"] == 4 and status["bars_emitted"] == 3
    assert status["bars_remaining"] == 13 and status["exhausted"] is False
    assert status["symbols"]["EURUSD"]["bars"] == 10
    assert status["symbols"]["EURUSD"]["last_bar"] == (T0 + timedelta(minutes=15)).isoformat()
