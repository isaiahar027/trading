"""Yellow News: trading sessions, weekend closure, daily rollover and scheduled-news blackouts.

The calendar only answers "may a *new* trade be opened right now?". It knows nothing about the broker:
sessions, rollover and the weekend window come from the operator's config, and scheduled news comes
from ``news.events`` plus an optional operator-maintained YAML file (``news.events_file``).

The events file is re-read whenever its modification time (or size) changes. A file that cannot be
read or validated never replaces the last good list; the problem is reported through :meth:`status`.
If the configured file has *never* loaded successfully the calendar cannot know about upcoming news,
so :meth:`check` fails closed and blocks with an explanatory reason until the file is fixed.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .config import InstrumentConfig, NewsConfig, NewsEvent
from .domain import ensure_utc

log = logging.getLogger(__name__)

#: Refuse events files larger than this (a calendar is a few kilobytes; anything bigger is a mistake).
MAX_EVENTS_FILE_BYTES = 1_000_000
#: How many upcoming events :meth:`NewsCalendar.status` lists.
STATUS_UPCOMING_LIMIT = 10

_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_FRIDAY = 4
_SATURDAY = 5
_SUNDAY = 6


@dataclass(frozen=True)
class CalendarCheck:
    """Result of :meth:`NewsCalendar.check`.

    ``ok`` is ``True`` only when ``reasons`` is empty. ``next_event`` is the earliest event relevant to the
    instrument whose blackout window has not ended yet (the one blocking now, or the next one coming).
    """

    ok: bool
    reasons: list[str]
    next_event: NewsEvent | None


class EventsFileError(Exception):
    """The events file could not be read or validated (the message is meant for the operator)."""


@dataclass(frozen=True)
class _FileStamp:
    # mtime is the contract's reload trigger; size and inode also catch same-tick edits and atomic replaces.
    mtime_ns: int
    size: int
    inode: int


class NewsCalendar:
    """Session, weekend, rollover and news-blackout checks for one desk configuration.

    Thread-safe: the reload state of the events file is guarded by a lock, so the engine and the
    dashboard may call :meth:`check` / :meth:`status` concurrently.
    """

    def __init__(self, cfg: NewsConfig, base_dir: Path) -> None:
        self._cfg = cfg
        self._config_events: tuple[NewsEvent, ...] = tuple(sorted(cfg.events, key=lambda e: e.at))
        self._file: Path | None = None
        if cfg.events_file is not None:
            self._file = cfg.events_file if cfg.events_file.is_absolute() else base_dir / cfg.events_file
        self._lock = threading.Lock()
        self._file_events: tuple[NewsEvent, ...] = ()
        self._stamp: _FileStamp | None = None  # stamp of the last load *attempt* (good or bad)
        self._loaded_ok = False  # at least one successful load since start-up
        self._last_good_stamp: _FileStamp | None = None
        self._error: str | None = None
        self._loads = 0
        self._failures = 0

    # ------------------------------------------------------------------------------------------ events
    def events(self) -> list[NewsEvent]:
        """Config events plus events-file events, sorted by time.

        The events file is re-read when its modification time or size changes. A broken or missing file
        keeps the last good list; the error is reported by :meth:`status`.
        """
        with self._lock:
            self._refresh_locked()
            file_events = self._file_events
        return sorted((*self._config_events, *file_events), key=lambda e: e.at)

    def _refresh_locked(self) -> None:
        if self._file is None:
            return
        path = self._file
        try:
            st = path.stat()
        except FileNotFoundError:
            self._record_failure(None, f"news events file not found: {path}")
            return
        except OSError as exc:
            self._record_failure(None, f"news events file {path} cannot be accessed: {exc}")
            return
        stamp = _FileStamp(mtime_ns=st.st_mtime_ns, size=st.st_size, inode=st.st_ino)
        if stamp == self._stamp:
            return  # unchanged since the last attempt (good or bad): nothing to do
        try:
            events = _load_events_file(path, st.st_size)
        except EventsFileError as exc:
            self._record_failure(stamp, str(exc))
            return
        self._stamp = stamp
        self._last_good_stamp = stamp
        self._file_events = tuple(sorted(events, key=lambda e: e.at))
        self._loaded_ok = True
        self._loads += 1
        if self._error is not None:
            log.info("news events file %s loads again (%d events)", path, len(events))
        else:
            log.info("loaded %d news events from %s", len(events), path)
        self._error = None

    def _record_failure(self, stamp: _FileStamp | None, message: str) -> None:
        # Log once per distinct problem; a missing file is re-checked on every call (stamp None).
        if message != self._error:
            if self._loaded_ok:
                log.error("%s; keeping the last good list of %d events", message, len(self._file_events))
            else:
                log.error("%s; new trades stay blocked until it loads", message)
            self._failures += 1
        self._error = message
        self._stamp = stamp

    # ------------------------------------------------------------------------------------------- check
    def check(self, instrument: InstrumentConfig, now: datetime) -> CalendarCheck:
        """Decide whether a new trade on ``instrument`` may open at ``now`` (UTC).

        Blocks (each adds a readable reason): outside ``instrument.sessions``; the weekend (Friday from
        ``weekend_close_utc``, all of Saturday, Sunday before ``weekend_open_utc``) when
        ``block_weekends``; the ``rollover_block`` window; any event relevant to the instrument within
        ``[at - blackout_minutes_before, at + blackout_minutes_after]``; and a configured events file
        that has never loaded successfully.
        """
        now = ensure_utc(now)
        cfg = self._cfg
        reasons: list[str] = []

        if not instrument.in_session(now):
            reasons.append(f"outside the configured trading sessions for {instrument.symbol} ({_fmt_moment(now)})")

        if cfg.block_weekends and _in_weekend(now, cfg):
            reasons.append(
                f"weekend: no new trades from Fri {cfg.weekend_close_utc:%H:%M} UTC "
                f"until Sun {cfg.weekend_open_utc:%H:%M} UTC ({_fmt_moment(now)})"
            )

        if cfg.rollover_block is not None and cfg.rollover_block.contains(now):
            window = cfg.rollover_block
            reasons.append(f"daily rollover window {window.start:%H:%M}-{window.end:%H:%M} UTC ({_fmt_moment(now)})")

        all_events = self.events()
        with self._lock:
            file_never_loaded = self._file is not None and not self._loaded_ok
            file_error = self._error
        if file_never_loaded:
            reasons.append(
                f"news calendar unavailable: {file_error or 'events file not loaded'}; fix the file to resume trading"
            )

        before = timedelta(minutes=cfg.blackout_minutes_before)
        after = timedelta(minutes=cfg.blackout_minutes_after)
        next_event: NewsEvent | None = None
        for event in all_events:  # sorted by time
            if not _relevant(event, instrument):
                continue
            if event.at + after < now:
                continue  # blackout already over
            if next_event is None:
                next_event = event
            if event.at - before <= now:
                reasons.append(
                    f"news blackout: {event.title} ({_fmt_currencies(event, instrument)}) at "
                    f"{_fmt_moment(event.at)}, no new trades from {cfg.blackout_minutes_before} min before "
                    f"to {cfg.blackout_minutes_after} min after"
                )
            else:
                break  # later events start even later

        return CalendarCheck(ok=not reasons, reasons=reasons, next_event=next_event)

    # ------------------------------------------------------------------------------------------ status
    def status(self, now: datetime | None = None) -> dict[str, Any]:
        """JSON-serializable state for the dashboard: event counts, events-file health and upcoming events."""
        all_events = self.events()
        moment = ensure_utc(now) if now is not None else datetime.now(UTC)
        with self._lock:
            file_info: dict[str, Any] | None = None
            if self._file is not None:
                good = self._last_good_stamp
                file_info = {
                    "path": str(self._file),
                    "ok": self._error is None and self._loaded_ok,
                    "loaded": self._loaded_ok,
                    "events": len(self._file_events),
                    "error": self._error,
                    "loads": self._loads,
                    "failures": self._failures,
                    "last_good_mtime": (
                        datetime.fromtimestamp(good.mtime_ns / 1e9, tz=UTC).isoformat() if good is not None else None
                    ),
                }
            error = self._error
        horizon = timedelta(minutes=self._cfg.blackout_minutes_after)
        upcoming = [e for e in all_events if e.at + horizon >= moment][:STATUS_UPCOMING_LIMIT]
        return {
            "ok": error is None,
            "error": error,
            "block_weekends": self._cfg.block_weekends,
            "weekend_close_utc": self._cfg.weekend_close_utc.strftime("%H:%M"),
            "weekend_open_utc": self._cfg.weekend_open_utc.strftime("%H:%M"),
            "rollover_block": (
                {
                    "start": self._cfg.rollover_block.start.strftime("%H:%M"),
                    "end": self._cfg.rollover_block.end.strftime("%H:%M"),
                    "days": list(self._cfg.rollover_block.days),
                }
                if self._cfg.rollover_block is not None
                else None
            ),
            "blackout_minutes_before": self._cfg.blackout_minutes_before,
            "blackout_minutes_after": self._cfg.blackout_minutes_after,
            "config_events": len(self._config_events),
            "events_total": len(all_events),
            "events_file": file_info,
            "upcoming": [
                {"at": e.at.isoformat(), "currencies": list(e.currencies), "title": e.title} for e in upcoming
            ],
        }


# ---------------------------------------------------------------------------------------------- helpers
def _load_events_file(path: Path, size: int) -> list[NewsEvent]:
    """Read and validate an events file: a YAML list of ``{at, currencies, title}`` mappings."""
    if size > MAX_EVENTS_FILE_BYTES:
        raise EventsFileError(f"news events file {path} is too large ({size} bytes > {MAX_EVENTS_FILE_BYTES})")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise EventsFileError(f"news events file not found: {path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise EventsFileError(f"news events file {path} cannot be read: {exc}") from exc
    if not text.strip():
        # An empty file is far more likely a half-written save than an intended "no events".
        raise EventsFileError(f"news events file {path} is empty; write [] for an empty calendar")
    try:
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise EventsFileError(f"news events file {path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, list):
        raise EventsFileError(f"news events file {path} must contain a top-level list of events")
    events: list[NewsEvent] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise EventsFileError(f"news events file {path}: entry {index} must be a mapping")
        try:
            events.append(NewsEvent.model_validate(item))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or '(entry)'}: {err['msg']}" for err in exc.errors()
            )
            raise EventsFileError(f"news events file {path}: entry {index} is invalid: {problems}") from exc
    return events


def _in_weekend(now: datetime, cfg: NewsConfig) -> bool:
    t = now.time().replace(tzinfo=None)
    day = now.weekday()
    if day == _FRIDAY:
        return t >= cfg.weekend_close_utc
    if day == _SATURDAY:
        return True
    if day == _SUNDAY:
        return t < cfg.weekend_open_utc
    return False


def _relevant(event: NewsEvent, instrument: InstrumentConfig) -> bool:
    """Whether ``event`` can move ``instrument``.

    Normally the currency lists must intersect. Fail closed when either list is empty: an event without
    currencies is treated as affecting everything, and an instrument without configured currencies is
    treated as exposed to every event (the desk cannot tell what moves it).
    """
    if not event.currencies or not instrument.currencies:
        return True
    return not set(event.currencies).isdisjoint(instrument.currencies)


def _fmt_currencies(event: NewsEvent, instrument: InstrumentConfig) -> str:
    if not event.currencies:
        return "all currencies"
    if not instrument.currencies:
        return ", ".join(event.currencies) + f"; {instrument.symbol} has no currencies configured"
    shared = [c for c in event.currencies if c in instrument.currencies]
    return ", ".join(shared)


def _fmt_moment(moment: datetime) -> str:
    return f"{_WEEKDAY_NAMES[moment.weekday()]} {moment:%Y-%m-%d %H:%M} UTC"
