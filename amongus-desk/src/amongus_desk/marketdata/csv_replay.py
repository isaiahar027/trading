"""Replay closed bars from a CSV file (backtests, demos, reproducing a session).

Format: a header row, then one bar per row. Columns (any order, case-insensitive)::

    symbol,start,open,high,low,close[,volume]

* ``symbol`` is the symbol the desk trades (the UniTrader symbol from the config), e.g. ``EURUSD``.
* ``start`` is the bar's open time in ISO-8601 *with* a UTC offset or ``Z``
  (``2026-10-05T08:00:00Z``, ``2026-10-05 10:00:00+02:00``). Naive timestamps are rejected: guessing a
  timezone would silently shift every bar.
* prices are positive decimal numbers with ``low <= open, close <= high``; ``volume`` is optional.

Every problem is reported as a :class:`CsvFormatError` naming the file and line, so a broken export can
be fixed instead of half-loaded. Rows may come in any order; bars are sorted per symbol.
"""

from __future__ import annotations

import asyncio
import csv
import heapq
import itertools
import logging
import math
import threading
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..domain import Bar, Quote, ensure_utc
from .base import FeedError

log = logging.getLogger(__name__)

__all__ = ["REQUIRED_COLUMNS", "CsvFormatError", "CsvReplayFeed", "load_csv_bars"]

REQUIRED_COLUMNS: tuple[str, ...] = ("symbol", "start", "open", "high", "low", "close")
OPTIONAL_COLUMNS: tuple[str, ...] = ("volume",)
_PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")


class CsvFormatError(FeedError, ValueError):
    """The CSV file is malformed. ``line`` is the 1-based physical line number (``None`` = whole file)."""

    def __init__(self, path: Path, line: int | None, message: str) -> None:
        self.path = path
        self.line = line
        self.reason = message
        where = f"{path}, line {line}" if line is not None else str(path)
        super().__init__(f"{where}: {message}")


@dataclass(frozen=True, slots=True)
class _Row:
    line: int
    symbol: str
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None


def _parse_start(path: Path, line: int, text: str) -> datetime:
    value = text.strip()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise CsvFormatError(path, line, f"start {value!r} is not an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CsvFormatError(
            path,
            line,
            f"start {value!r} has no UTC offset; write it as e.g. 2026-10-05T08:00:00Z or "
            "2026-10-05T08:00:00+00:00 (naive timestamps are ambiguous and are rejected)",
        )
    return ensure_utc(parsed)


def _parse_number(path: Path, line: int, column: str, text: str, *, positive: bool) -> float:
    value = text.strip()
    if not value:
        raise CsvFormatError(path, line, f"{column} is empty")
    try:
        number = float(value)
    except ValueError:
        raise CsvFormatError(path, line, f"{column} {value!r} is not a number") from None
    if not math.isfinite(number):
        raise CsvFormatError(path, line, f"{column} {value!r} is not a finite number")
    if positive and number <= 0:
        raise CsvFormatError(path, line, f"{column} must be positive, got {value!r}")
    if not positive and number < 0:
        raise CsvFormatError(path, line, f"{column} must not be negative, got {value!r}")
    return number


def _header(path: Path, line: int, cells: list[str]) -> dict[str, int]:
    names = [c.strip().lower() for c in cells]
    columns: dict[str, int] = {}
    for index, name in enumerate(names):
        if name in columns:
            raise CsvFormatError(path, line, f"column {name!r} appears twice in the header")
        columns[name] = index
    expected = "symbol,start,open,high,low,close[,volume]"
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise CsvFormatError(path, line, f"missing column(s) {', '.join(missing)} (expected {expected})")
    unknown = [n for n in names if n not in REQUIRED_COLUMNS and n not in OPTIONAL_COLUMNS]
    if unknown:
        raise CsvFormatError(path, line, f"unexpected column(s) {', '.join(map(repr, unknown))} (expected {expected})")
    return columns


def _read_rows(path: Path) -> list[_Row]:
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise CsvFormatError(path, None, f"cannot open the file: {exc.strerror or exc}") from exc
    rows: list[_Row] = []
    with handle:
        reader = csv.reader(handle)
        columns: dict[str, int] | None = None
        try:
            for cells in reader:
                line = reader.line_num
                if not cells or all(not c.strip() for c in cells):
                    continue  # blank line
                if columns is None:
                    columns = _header(path, line, cells)  # the first non-blank line, wherever it is
                    continue
                if len(cells) != len(columns):
                    raise CsvFormatError(path, line, f"expected {len(columns)} fields, found {len(cells)}")
                symbol = cells[columns["symbol"]].strip()
                if not symbol:
                    raise CsvFormatError(path, line, "symbol is empty")
                start = _parse_start(path, line, cells[columns["start"]])
                o, h, lo, c = (_parse_number(path, line, n, cells[columns[n]], positive=True) for n in _PRICE_COLUMNS)
                volume: float | None = None
                if "volume" in columns and cells[columns["volume"]].strip():
                    volume = _parse_number(path, line, "volume", cells[columns["volume"]], positive=False)
                rows.append(_Row(line, symbol, start, o, h, lo, c, volume))
        except csv.Error as exc:
            raise CsvFormatError(path, reader.line_num, f"unreadable CSV: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise CsvFormatError(path, None, f"the file is not UTF-8 text: {exc.reason}") from exc
    if columns is None:
        raise CsvFormatError(path, None, "the file is empty (expected a header row and bars)")
    if not rows:
        raise CsvFormatError(path, None, "the file has a header but no bars")
    return rows


def _infer_timeframe(path: Path, grouped: dict[str, list[_Row]]) -> int:
    gaps = [
        (later.start - earlier.start).total_seconds()
        for series in grouped.values()
        for earlier, later in itertools.pairwise(series)
    ]
    if not gaps:
        raise CsvFormatError(
            path, None, "cannot infer the timeframe: every symbol has a single bar (pass timeframe_minutes)"
        )
    smallest = min(gaps)
    if smallest <= 0 or smallest % 60 != 0:
        raise CsvFormatError(path, None, f"cannot infer the timeframe: smallest bar spacing is {smallest:g} s")
    return int(smallest // 60)


def load_csv_bars(path: Path | str, *, timeframe_minutes: int | None = None) -> dict[str, list[Bar]]:
    """Load and validate every bar in ``path``: ``{symbol: [bars oldest first]}``, symbols sorted.

    ``timeframe_minutes`` labels the bars; when omitted it is inferred as the smallest spacing between
    two consecutive bars of a symbol. Every gap between consecutive bars must be a whole multiple of the
    timeframe (missing bars are fine; overlapping or off-grid bars are not). Duplicate bars are errors.
    Raises :class:`CsvFormatError` (a :class:`~amongus_desk.marketdata.base.FeedError`) on any problem.
    """
    path = Path(path)
    if timeframe_minutes is not None and timeframe_minutes <= 0:
        raise ValueError("timeframe_minutes must be positive")
    grouped: dict[str, list[_Row]] = {}
    for row in _read_rows(path):
        grouped.setdefault(row.symbol, []).append(row)
    for series in grouped.values():
        series.sort(key=lambda r: (r.start, r.line))
        for earlier, later in itertools.pairwise(series):
            if later.start == earlier.start:
                raise CsvFormatError(
                    path,
                    later.line,
                    f"duplicate {later.symbol} bar at {later.start.isoformat()} (also on line {earlier.line})",
                )
    tf = timeframe_minutes if timeframe_minutes is not None else _infer_timeframe(path, grouped)
    step = tf * 60
    out: dict[str, list[Bar]] = {}
    for symbol in sorted(grouped):
        series = grouped[symbol]
        bars: list[Bar] = []
        for index, row in enumerate(series):
            if index:
                gap = (row.start - series[index - 1].start).total_seconds()
                if gap % step != 0:
                    raise CsvFormatError(
                        path,
                        row.line,
                        f"{symbol} bar at {row.start.isoformat()} starts {gap / 60:g} min after the previous bar "
                        f"(line {series[index - 1].line}); expected a multiple of the {tf}-minute timeframe",
                    )
            try:
                bars.append(
                    Bar(
                        symbol=symbol,
                        start=row.start,
                        timeframe_minutes=tf,
                        open=row.open,
                        high=row.high,
                        low=row.low,
                        close=row.close,
                        volume=row.volume,
                    )
                )
            except ValueError as exc:
                raise CsvFormatError(
                    path,
                    row.line,
                    f"{exc} (open={row.open:g} high={row.high:g} low={row.low:g} close={row.close:g})",
                ) from None
        out[symbol] = bars
    return out


class CsvReplayFeed:
    """Finite feed over a CSV file (implements ``MarketDataFeed``).

    The file is loaded and validated in the constructor, so a broken file fails before the desk starts.
    :meth:`warmup` consumes the first ``n`` bars of each requested symbol; :meth:`stream` then yields every
    remaining bar exactly once in global time order (ties broken by symbol) and ends when the data runs
    out. There is no bid/ask in a CSV, so :meth:`latest_quote` returns ``None``.
    """

    name = "csv"

    def __init__(self, path: Path | str, timeframe_minutes: int, symbols: Sequence[str] | None = None) -> None:
        if timeframe_minutes <= 0:
            raise ValueError("timeframe_minutes must be positive")
        self._path = Path(path)
        self._tf = timeframe_minutes
        data = load_csv_bars(self._path, timeframe_minutes=timeframe_minutes)
        self._missing: list[str] = []
        if symbols is not None:
            wanted = list(dict.fromkeys(symbols))
            self._missing = [s for s in wanted if s not in data]
            if self._missing:
                log.warning("%s has no bars for %s", self._path, ", ".join(self._missing))
            data = {s: data[s] for s in wanted if s in data}
        self._bars = data
        self._lock = threading.Lock()
        self._cursor: dict[str, int] = dict.fromkeys(data, 0)
        self._last: dict[str, Bar] = {}
        self._emitted = 0
        self._warmed = 0
        self._stream_started = False
        self._streaming = False
        self._closed = False

    @property
    def symbols(self) -> list[str]:
        """Symbols this feed replays."""
        return list(self._bars)

    async def warmup(self, symbols: Sequence[str], bars: int) -> dict[str, list[Bar]]:
        """Consume and return the first ``bars`` not-yet-used bars of each symbol (oldest first)."""
        if bars < 0:
            raise ValueError("bars must be >= 0")
        if self._stream_started:
            raise RuntimeError("warmup() must be called before stream()")
        out: dict[str, list[Bar]] = {}
        with self._lock:
            # A repeated symbol must not consume a second slice: those bars would be returned under the
            # same key as the first slice (overwriting it), so neither warmup nor the stream would see them.
            for symbol in dict.fromkeys(symbols):
                series = self._bars.get(symbol)
                if series is None:
                    out[symbol] = []
                    continue
                cursor = self._cursor[symbol]
                taken = series[cursor : cursor + bars]
                self._cursor[symbol] = cursor + len(taken)
                self._warmed += len(taken)
                if taken:
                    self._last[symbol] = taken[-1]
                out[symbol] = taken
        unknown = [s for s in symbols if s not in self._bars]
        if unknown:
            log.warning("CSV replay has no bars for %s; returning no warmup bars", ", ".join(unknown))
        await asyncio.sleep(0)
        return out

    async def stream(self) -> AsyncGenerator[Bar, None]:
        """Yield every remaining bar once, in global time order, then end."""
        if self._streaming:
            raise RuntimeError("stream() is already running for this feed")
        self._streaming = True
        self._stream_started = True
        try:
            with self._lock:
                remaining = [series[self._cursor[symbol] :] for symbol, series in self._bars.items()]
            for bar in heapq.merge(*remaining, key=lambda b: (b.start, b.symbol)):
                if self._closed:
                    return
                with self._lock:
                    self._cursor[bar.symbol] += 1
                    self._last[bar.symbol] = bar
                    self._emitted += 1
                yield bar
                await asyncio.sleep(0)  # let the watchdog / dashboard run between bars
        finally:
            self._streaming = False

    def latest_quote(self, symbol: str) -> Quote | None:
        """Always ``None``: a CSV of bars has no bid/ask."""
        return None

    def status(self) -> dict[str, Any]:
        """JSON-safe progress: bars per symbol, consumed, remaining and the last bar seen."""
        with self._lock:
            total = sum(len(s) for s in self._bars.values())
            consumed = sum(self._cursor.values())
            return {
                "kind": "csv",
                "path": str(self._path),
                "timeframe_minutes": self._tf,
                "symbols": {
                    symbol: {
                        "bars": len(series),
                        "remaining": len(series) - self._cursor[symbol],
                        "last_bar": self._last[symbol].start.isoformat() if symbol in self._last else None,
                        "last_close": self._last[symbol].close if symbol in self._last else None,
                    }
                    for symbol, series in self._bars.items()
                },
                "missing_symbols": list(self._missing),
                "bars_total": total,
                "bars_warmup": self._warmed,
                "bars_emitted": self._emitted,
                "bars_remaining": total - consumed,
                "exhausted": consumed >= total,
                "closed": self._closed,
            }

    async def aclose(self) -> None:
        """Stop the replay; the stream ends before its next bar."""
        self._closed = True
