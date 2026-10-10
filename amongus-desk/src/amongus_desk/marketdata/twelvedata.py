"""Twelve Data REST feed: closed bars from ``GET {base_url}/time_series``, polled once per bar close.

Only the publicly documented parts of the API are used:

* request parameters ``symbol``, ``interval``, ``outputsize``, ``timezone=UTC``, ``order=asc`` and the
  ``Authorization: apikey <key>`` header (the key never goes into a URL, a log line or ``status()``);
* the response ``values`` list with string fields ``datetime`` (``"YYYY-MM-DD HH:MM:SS"``, UTC because
  of ``timezone=UTC``), ``open``, ``high``, ``low``, ``close`` and an optional ``volume`` (absent for
  forex), plus ``meta.interval`` when present;
* error objects ``{"code": int, "message": str, "status": "error"}``, which may arrive with HTTP 200.

Closed bars: a bar is used only once it closed at least ``poll_delay_seconds`` before the request was
*sent* (``end + poll_delay <= sent_at``). Judging by the time the response arrived instead would accept
a bar that was still forming when the provider built the answer, and a bar that closed a moment ago may
not be final yet; either would then be yielded once and never corrected. A bar held back this way is
picked up by a later poll.

Error policy: 401/403 (HTTP status or error code) raise :class:`FeedError` because nothing will work
until the operator fixes the key or plan. 429 blocks every request until the next UTC minute. Anything
else (5xx, network trouble, malformed JSON, an unknown symbol) is logged, shown in :meth:`status` and
retried at the next bar close.

Credit budget: every request costs 1 credit (one symbol per request). Before each request the feed
checks a rolling 61-second window (60 s plus a 1 s margin; this also keeps every calendar minute under
the limit) against ``per_minute_credit_limit`` and a per-UTC-day counter against
``daily_credit_limit``. A request that would exceed either limit is not sent: it waits for the window if
it can do so before the next poll is due, and is skipped otherwise. Credits are counted when a request
is sent, whatever the outcome, which can only over-count. The counters live in this process; a restart
starts the day's counter from zero.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from collections import deque
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx2

from ..clock import Clock
from ..config import FeedConfig, InstrumentConfig
from ..domain import Bar, Quote, ensure_utc
from .base import FeedError

log = logging.getLogger(__name__)

__all__ = ["INTERVALS", "TwelveDataFeed"]

#: Twelve Data ``interval`` values for the timeframes the strategy supports.
INTERVALS: dict[int, str] = {1: "1min", 5: "5min", 15: "15min", 30: "30min", 60: "1h"}

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_OUTPUTSIZE = 5000  # documented maximum for time_series
_MIN_POLL_OUTPUTSIZE = 3  # forming bar + the bar that just closed + one spare
_MINUTE_WINDOW_SECONDS = 61.0
_RATE_LIMIT_MARGIN = timedelta(seconds=1)
_MAX_ATTEMPTS_PER_CYCLE = 3  # per symbol, counting retries after a 429
_MESSAGE_LIMIT = 300


class _RateLimitedError(Exception):
    """HTTP 429 / error code 429."""


class _TransientError(Exception):
    """A failure worth retrying at the next bar close."""


class _CreditBudget:
    """Per-minute (rolling window) and per-UTC-day credit counters. The feed serialises access."""

    def __init__(self, per_minute: int, per_day: int) -> None:
        self.per_minute = per_minute
        self.per_day = per_day
        self._window: deque[float] = deque()  # monotonic send times
        self._day: date | None = None
        self._day_used = 0
        self._blocked_until: float | None = None  # monotonic
        self._blocked_until_wall: datetime | None = None

    def _roll(self, now: datetime, mono: float) -> None:
        if self._day != now.date():
            self._day = now.date()
            self._day_used = 0
        while self._window and mono - self._window[0] >= _MINUTE_WINDOW_SECONDS:
            self._window.popleft()
        if self._blocked_until is not None and mono >= self._blocked_until:
            self._blocked_until = None
            self._blocked_until_wall = None

    def wait_seconds(self, now: datetime, mono: float) -> float | None:
        """0 if a credit may be spent now, seconds to wait otherwise, ``None`` if today's budget is gone."""
        self._roll(now, mono)
        if self._day_used >= self.per_day:
            return None
        waits = [0.0]
        if self._blocked_until is not None:
            waits.append(self._blocked_until - mono)
        if len(self._window) >= self.per_minute:
            waits.append(self._window[0] + _MINUTE_WINDOW_SECONDS - mono)
        return max(waits)

    def consume(self, now: datetime, mono: float) -> None:
        self._roll(now, mono)
        self._window.append(mono)
        self._day_used += 1

    def block(self, until_mono: float, until_wall: datetime) -> None:
        if self._blocked_until is None or until_mono > self._blocked_until:
            self._blocked_until = until_mono
            self._blocked_until_wall = until_wall

    def snapshot(self, now: datetime, mono: float) -> dict[str, Any]:
        self._roll(now, mono)
        return {
            "minute_used": len(self._window),
            "minute_limit": self.per_minute,
            "minute_window_seconds": _MINUTE_WINDOW_SECONDS,
            "day": self._day.isoformat() if self._day else None,
            "day_used": self._day_used,
            "day_limit": self.per_day,
            "day_remaining": max(0, self.per_day - self._day_used),
            "rate_limited_until": self._blocked_until_wall.isoformat() if self._blocked_until_wall else None,
        }


@dataclass
class _SymbolState:
    feed_symbol: str
    last_bar: Bar | None = None
    bars_emitted: int = 0
    requests: int = 0
    skipped_for_budget: int = 0
    malformed_values: int = 0
    last_ok_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None


def _floor(moment: datetime, step: timedelta) -> datetime:
    seconds = int((moment - _EPOCH).total_seconds())
    size = int(step.total_seconds())
    return _EPOCH + timedelta(seconds=seconds - seconds % size)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _price(item: dict[str, Any], key: str) -> float:
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{key} is missing or not a number")
    try:
        number = float(value.strip()) if isinstance(value, str) else float(value)
    except ValueError:
        raise ValueError(f"{key} {value!r} is not a number") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{key} {value!r} is not a positive finite number")
    return number


def _volume(item: dict[str, Any]) -> float | None:
    value = item.get("volume")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value.strip()) if isinstance(value, str) else float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


class TwelveDataFeed:
    """Polling ``MarketDataFeed`` over Twelve Data's ``time_series`` endpoint.

    Requests use each instrument's ``feed_symbol`` (e.g. ``EUR/USD``); yielded bars carry the broker
    ``symbol`` (e.g. ``EURUSD``). A bar is returned only once it closed ``feed.poll_delay_seconds``
    before the request was sent (so the still-forming bar never is), and a bar is yielded at most once.
    Polls happen once per bar close plus ``feed.poll_delay_seconds`` (see :meth:`next_poll_at`), only
    for instruments whose just-closed bar starts inside one of their sessions unless
    ``twelvedata.poll_outside_sessions`` is set. All waiting goes through ``clock``, so a
    ``ManualClock`` drives the whole schedule without real sleeps. There is no bid/ask, so
    :meth:`latest_quote` returns ``None``.
    """

    name = "twelvedata"

    def __init__(  # noqa: PLR0917 - positional signature fixed by docs/ARCHITECTURE.md
        self,
        cfg: FeedConfig,
        instruments: Sequence[InstrumentConfig],
        timeframe_minutes: int,
        api_key: str,
        clock: Clock,
        client: httpx2.AsyncClient | None = None,
    ) -> None:
        interval = INTERVALS.get(timeframe_minutes)
        if interval is None:
            supported = ", ".join(str(m) for m in INTERVALS)
            raise ValueError(f"Twelve Data feed supports timeframes of {supported} minutes, not {timeframe_minutes}")
        key = api_key.strip()
        if not key:
            raise ValueError("the Twelve Data API key is empty (set TWELVEDATA_API_KEY)")
        if not instruments:
            raise ValueError("the Twelve Data feed needs at least one instrument")
        symbols = [i.symbol for i in instruments]
        if len(set(symbols)) != len(symbols):
            raise ValueError("instrument symbols must be unique")
        self._td = cfg.twelvedata
        self._tf = timedelta(minutes=timeframe_minutes)
        self._interval = interval
        self._delay = timedelta(seconds=cfg.poll_delay_seconds)
        # Closed bars that can still be inside the poll delay when a request is sent (0 unless the delay
        # is at least one timeframe); every outputsize leaves room for them.
        self._unsettled = int(self._delay // self._tf)
        self._url = self._td.base_url.rstrip("/") + "/time_series"
        self._api_key = key
        self._clock = clock
        self._client = client if client is not None else httpx2.AsyncClient(timeout=self._td.request_timeout_seconds)
        self._owns_client = client is None
        self._client_closed = False
        self._by_symbol: dict[str, InstrumentConfig] = {i.symbol: i for i in instruments}
        self._order: list[str] = list(symbols)  # polling order; symbols that missed a cycle go first
        self._lock = threading.Lock()
        self._budget = _CreditBudget(self._td.per_minute_credit_limit, self._td.daily_credit_limit)
        self._symbols: dict[str, _SymbolState] = {
            i.symbol: _SymbolState(feed_symbol=i.feed_symbol) for i in instruments
        }
        self._handled: datetime | None = None  # latest bar boundary whose poll has run
        self._next_poll: datetime | None = None
        self._last_poll_at: datetime | None = None
        self._requests = 0
        self._errors = 0
        self._skipped_for_budget = 0
        self._last_error: str | None = None
        self._last_error_at: datetime | None = None
        self._fatal: str | None = None
        self._exhausted_logged: date | None = None
        self._streaming = False
        self._closed = False
        self._closed_event = asyncio.Event()

    # ------------------------------------------------------------------------------- public API

    @property
    def interval(self) -> str:
        """The Twelve Data ``interval`` parameter used for every request (e.g. ``"5min"``)."""
        return self._interval

    def next_poll_at(self, now: datetime | None = None) -> datetime:
        """When the stream polls next: the next bar close + ``poll_delay_seconds``, or ``now`` if overdue."""
        moment = ensure_utc(now) if now is not None else ensure_utc(self._clock.now())
        with self._lock:
            handled = self._handled
        if handled is None:
            handled = self._due_boundary(moment)
        return max(moment, handled + self._tf + self._delay)

    async def warmup(self, symbols: Sequence[str], bars: int) -> dict[str, list[Bar]]:
        """Up to ``bars`` most recent closed bars per symbol (one request each, within the credit budget).

        Sessions are ignored here (history is needed regardless). A symbol whose request fails or does
        not fit the budget gets ``[]``; bad credentials raise :class:`FeedError`.
        """
        if bars < 0:
            raise ValueError("bars must be >= 0")
        out: dict[str, list[Bar]] = {}
        for symbol in dict.fromkeys(symbols):  # a repeated symbol must not cost a second credit
            inst = self._by_symbol.get(symbol)
            if inst is None:
                log.warning("Twelve Data feed has no instrument %s; returning no warmup bars", symbol)
                out[symbol] = []
                continue
            if bars == 0:
                out[symbol] = []
                continue
            outputsize = min(_MAX_OUTPUTSIZE, bars + 2 + self._unsettled)
            result = await self._fetch_budgeted(inst, outputsize, deadline=None)
            if result is None:
                out[symbol] = []
                continue
            fetched, sent_at = result
            now = ensure_utc(self._clock.now())
            closed = self._settled(fetched, sent_at)[-bars:]
            with self._lock:
                state = self._symbols[symbol]
                if closed and (state.last_bar is None or closed[-1].start > state.last_bar.start):
                    state.last_bar = closed[-1]
                state.last_ok_at = now
            out[symbol] = closed
        return out

    async def stream(self) -> AsyncGenerator[Bar, None]:
        """Poll at every bar close and yield newly closed bars, per symbol in time order, until closed.

        Raises :class:`FeedError` when Twelve Data rejects the credentials (401/403).
        """
        if self._streaming:
            raise RuntimeError("stream() is already running for this feed")
        self._streaming = True
        try:
            with self._lock:
                if self._handled is None:
                    # Bars that closed before the stream started are warmup's job, not the stream's,
                    # unless a bar closed (and its poll fell due) after warmup asked for it: then poll
                    # right away instead of delivering that bar a whole timeframe late.
                    due = self._due_boundary(ensure_utc(self._clock.now()))
                    behind = any(s.last_bar is not None and s.last_bar.end < due for s in self._symbols.values())
                    self._handled = due - self._tf if behind else due
            while not self._is_closed():
                wake = self.next_poll_at()
                with self._lock:
                    self._next_poll = wake
                await self._sleep_until(wake)
                if self._is_closed():
                    return
                poll_time = max(ensure_utc(self._clock.now()), wake)
                due = self._due_boundary(poll_time)
                next_cycle = due + self._tf + self._delay
                with self._lock:
                    self._handled = due
                    self._last_poll_at = poll_time
                    self._next_poll = next_cycle
                    order = list(self._order)
                deadline = self._clock.monotonic() + max(0.0, (next_cycle - poll_time).total_seconds())
                just_closed = due - self._tf
                missed: list[str] = []
                for symbol in order:
                    inst = self._by_symbol[symbol]
                    if not (self._td.poll_outside_sessions or inst.in_session(just_closed)):
                        continue
                    if self._is_closed():
                        return
                    with self._lock:
                        last = self._symbols[symbol].last_bar
                    if last is not None and last.end >= due:
                        continue  # nothing newer can have settled yet; don't spend a credit
                    result = await self._fetch_budgeted(inst, self._poll_outputsize(symbol, due), deadline=deadline)
                    if result is None:
                        missed.append(symbol)
                        continue
                    fetched, sent_at = result
                    for bar in self._accept(symbol, fetched, sent_at):
                        yield bar
                if missed:
                    with self._lock:
                        self._order = missed + [s for s in self._order if s not in missed]
        finally:
            self._streaming = False

    def latest_quote(self, symbol: str) -> Quote | None:
        """Always ``None``: ``time_series`` has no bid/ask."""
        return None

    def status(self) -> dict[str, Any]:
        """JSON-safe health: credits used vs limits, schedule, errors and the last bar per symbol."""
        now = ensure_utc(self._clock.now())
        mono = self._clock.monotonic()
        with self._lock:
            symbols: dict[str, Any] = {}
            for symbol, state in self._symbols.items():
                inst = self._by_symbol[symbol]
                symbols[symbol] = {
                    "feed_symbol": state.feed_symbol,
                    "polling": self._td.poll_outside_sessions or inst.in_session(now),
                    "last_bar": state.last_bar.start.isoformat() if state.last_bar else None,
                    "last_close": state.last_bar.close if state.last_bar else None,
                    "bars_emitted": state.bars_emitted,
                    "requests": state.requests,
                    "skipped_for_budget": state.skipped_for_budget,
                    "malformed_values": state.malformed_values,
                    "last_ok_at": state.last_ok_at.isoformat() if state.last_ok_at else None,
                    "last_error": state.last_error,
                    "last_error_at": state.last_error_at.isoformat() if state.last_error_at else None,
                }
            return {
                "kind": "twelvedata",
                "interval": self._interval,
                "timeframe_minutes": int(self._tf.total_seconds() // 60),
                "poll_delay_seconds": self._delay.total_seconds(),
                "poll_outside_sessions": self._td.poll_outside_sessions,
                "credits": self._budget.snapshot(now, mono),
                "requests": self._requests,
                "errors": self._errors,
                "skipped_for_budget": self._skipped_for_budget,
                "last_poll_at": self._last_poll_at.isoformat() if self._last_poll_at else None,
                "next_poll_at": self._next_poll.isoformat() if self._next_poll and not self._closed else None,
                "last_error": self._last_error,
                "last_error_at": self._last_error_at.isoformat() if self._last_error_at else None,
                "fatal": self._fatal,
                "closed": self._closed,
                "symbols": symbols,
            }

    async def aclose(self) -> None:
        """Stop polling (a sleeping stream wakes up and ends) and close the HTTP client if we own it."""
        self._closed = True
        self._closed_event.set()
        if self._owns_client and not self._client_closed:
            self._client_closed = True
            await self._client.aclose()

    # ------------------------------------------------------------------------------- schedule

    def _due_boundary(self, moment: datetime) -> datetime:
        """Latest bar boundary whose poll time (boundary + delay) is not after ``moment``."""
        return _floor(moment - self._delay, self._tf)

    def _poll_outputsize(self, symbol: str, due: datetime) -> int:
        with self._lock:
            last = self._symbols[symbol].last_bar
        minimum = _MIN_POLL_OUTPUTSIZE + self._unsettled
        if last is None:
            return minimum
        missing = int((due - last.end) / self._tf)  # closed bars we have not seen yet, beyond the last one
        return max(minimum, min(_MAX_OUTPUTSIZE, missing + 2 + self._unsettled))

    def _settled(self, bars: list[Bar], sent_at: datetime) -> list[Bar]:
        """Bars that had closed at least ``poll_delay_seconds`` before the request was sent."""
        cutoff = sent_at - self._delay
        return [b for b in bars if b.end <= cutoff]

    def _accept(self, symbol: str, fetched: list[Bar], sent_at: datetime) -> list[Bar]:
        """Settled bars newer than the last one yielded (only the latest one if nothing was seen yet)."""
        now = ensure_utc(self._clock.now())
        closed = self._settled(fetched, sent_at)
        with self._lock:
            state = self._symbols[symbol]
            if state.last_bar is None:
                new = closed[-1:]
            else:
                last_start = state.last_bar.start
                new = [b for b in closed if b.start > last_start]
            if new:
                state.last_bar = new[-1]
                state.bars_emitted += len(new)
            state.last_ok_at = now
        return new

    # ------------------------------------------------------------------------------- requests

    async def _fetch_budgeted(
        self, inst: InstrumentConfig, outputsize: int, *, deadline: float | None
    ) -> tuple[list[Bar], datetime] | None:
        """One request within the credit budget: ``(bars, sent_at)``, or ``None`` when skipped or failed
        (already recorded). ``sent_at`` is when the request went out, which decides what has closed.

        ``deadline`` (monotonic) bounds how long we may wait for credits; ``None`` waits as needed.
        """
        attempts = 0
        while not self._is_closed():
            now = ensure_utc(self._clock.now())
            mono = self._clock.monotonic()
            with self._lock:
                wait = self._budget.wait_seconds(now, mono)
                if wait == 0:
                    self._budget.consume(now, mono)
                    self._requests += 1
                    self._symbols[inst.symbol].requests += 1
            if wait is None:
                self._note_daily_exhausted(inst.symbol, now)
                return None
            if wait > 0:
                if deadline is not None and mono + wait >= deadline:
                    self._note_budget_skip(inst.symbol)
                    return None
                await self._sleep(wait)
                continue
            attempts += 1
            try:
                # ``now`` was read just before the request with no await in between: it is the send time.
                return await self._request(inst, outputsize), now
            except _RateLimitedError as exc:
                until = self._block_until_next_minute()
                self._record_error(inst.symbol, f"rate limited (429): {exc}; pausing until {until:%H:%M:%S} UTC")
                if attempts >= _MAX_ATTEMPTS_PER_CYCLE:
                    return None
            except _TransientError as exc:
                self._record_error(inst.symbol, f"{exc}; retrying at the next bar close")
                return None
        return None

    async def _request(self, inst: InstrumentConfig, outputsize: int) -> list[Bar]:
        params = {
            "symbol": inst.feed_symbol,
            "interval": self._interval,
            "outputsize": str(outputsize),
            "timezone": "UTC",
            "order": "asc",
        }
        headers = {"Authorization": f"apikey {self._api_key}", "Accept": "application/json"}
        try:
            response = await self._client.get(
                self._url, params=params, headers=headers, timeout=self._td.request_timeout_seconds
            )
        except httpx2.TimeoutException:
            raise _TransientError(f"{inst.feed_symbol}: request timed out") from None
        except httpx2.HTTPError as exc:
            raise _TransientError(
                f"{inst.feed_symbol}: request failed ({type(exc).__name__}: {self._clean(str(exc))})"
            ) from None

        http_status = response.status_code
        payload: Any
        try:
            payload = response.json()
        except ValueError:
            payload = None
        code: int | None = None
        message = ""
        is_error = isinstance(payload, dict) and (
            payload.get("status") == "error" or ("code" in payload and "values" not in payload)
        )
        if isinstance(payload, dict) and is_error:
            code = _as_int(payload.get("code"))
            message = self._clean(str(payload.get("message", "")))
        if not message and http_status >= 400:
            message = response.reason_phrase or "error"

        if http_status in (401, 403) or code in (401, 403):
            text = (
                f"Twelve Data refused the request for {inst.feed_symbol} (HTTP {http_status}, code {code}): "
                f"{message}. Check TWELVEDATA_API_KEY and what your plan allows."
            )
            with self._lock:
                self._fatal = text
            self._record_error(inst.symbol, text, level=logging.ERROR)
            raise FeedError(text)
        if http_status == 429 or code == 429:
            raise _RateLimitedError(message or "too many requests")
        if is_error or http_status >= 400:
            raise _TransientError(f"{inst.feed_symbol}: Twelve Data error (HTTP {http_status}, code {code}): {message}")
        if not isinstance(payload, dict):
            raise _TransientError(f"{inst.feed_symbol}: malformed response (not a JSON object)")
        values = payload.get("values")
        if not isinstance(values, list):
            raise _TransientError(f"{inst.feed_symbol}: malformed response (no 'values' list)")
        meta = payload.get("meta")
        if isinstance(meta, dict) and "interval" in meta:
            echoed = str(meta["interval"]).strip().lower()
            if echoed != self._interval.lower():
                raise _TransientError(
                    f"{inst.feed_symbol}: response interval {self._clean(echoed)!r} does not match the requested "
                    f"{self._interval!r}; data ignored"
                )
        return self._parse_values(inst, values)

    def _parse_values(self, inst: InstrumentConfig, values: list[Any]) -> list[Bar]:
        bars: dict[datetime, Bar] = {}
        bad = 0
        first_problem = ""
        tf_minutes = int(self._tf.total_seconds() // 60)
        for item in values:
            try:
                if not isinstance(item, dict):
                    raise ValueError("value is not an object")
                raw = item.get("datetime")
                if not isinstance(raw, str):
                    raise ValueError("datetime is missing")
                start = datetime.strptime(raw.strip(), _DATETIME_FORMAT).replace(tzinfo=UTC)
                bar = Bar(
                    symbol=inst.symbol,
                    start=start,
                    timeframe_minutes=tf_minutes,
                    open=_price(item, "open"),
                    high=_price(item, "high"),
                    low=_price(item, "low"),
                    close=_price(item, "close"),
                    volume=_volume(item),
                )
            except ValueError as exc:
                bad += 1
                if not first_problem:
                    first_problem = self._clean(str(exc))
                continue
            bars[bar.start] = bar
        if bad:
            with self._lock:
                self._symbols[inst.symbol].malformed_values += bad
            log.warning(
                "%s: skipped %d malformed value(s) from Twelve Data (first: %s)", inst.feed_symbol, bad, first_problem
            )
        return [bars[start] for start in sorted(bars)]

    # ------------------------------------------------------------------------------- bookkeeping

    def _block_until_next_minute(self) -> datetime:
        now = ensure_utc(self._clock.now())
        until = now.replace(second=0, microsecond=0) + timedelta(minutes=1) + _RATE_LIMIT_MARGIN
        with self._lock:
            self._budget.block(self._clock.monotonic() + (until - now).total_seconds(), until)
        return until

    def _record_error(self, symbol: str, message: str, *, level: int = logging.WARNING) -> None:
        now = ensure_utc(self._clock.now())
        text = self._clean(message)
        with self._lock:
            self._errors += 1
            self._last_error = text
            self._last_error_at = now
            state = self._symbols[symbol]
            state.last_error = text
            state.last_error_at = now
        log.log(level, "Twelve Data %s: %s", symbol, text)

    def _note_budget_skip(self, symbol: str) -> None:
        with self._lock:
            self._skipped_for_budget += 1
            self._symbols[symbol].skipped_for_budget += 1
        log.warning("Twelve Data per-minute credit budget is full; %s skipped until the next bar close", symbol)

    def _note_daily_exhausted(self, symbol: str, now: datetime) -> None:
        with self._lock:
            self._skipped_for_budget += 1
            self._symbols[symbol].skipped_for_budget += 1
            first_today = self._exhausted_logged != now.date()
            self._exhausted_logged = now.date()
            limit = self._budget.per_day
        if first_today:
            message = f"daily credit budget ({limit}) used up; polling resumes after 00:00 UTC"
            self._record_error(symbol, message, level=logging.ERROR)

    def _clean(self, text: str) -> str:
        """Never let the API key reach logs or the dashboard; keep provider messages short."""
        return text.replace(self._api_key, "***")[:_MESSAGE_LIMIT]

    def _is_closed(self) -> bool:
        # A method (not the attribute) so type checkers don't assume it can't change across an await.
        return self._closed

    async def _sleep_until(self, moment: datetime) -> None:
        await self._sleep(max(0.0, (moment - ensure_utc(self._clock.now())).total_seconds()))

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
