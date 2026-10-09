"""The kill switch: a flag file ``<data_dir>/KILL`` that halts all new trading while it exists.

* :meth:`KillSwitch.engaged` is ``True`` whenever an entry named ``KILL`` exists, whatever it contains.
  Operators may create it by hand (an empty file is fine, e.g. from the VPS desktop) and the CLI
  (``desk kill`` / ``desk resume``) may write it from another process.
* :meth:`KillSwitch.engage` writes JSON ``{"reason", "by", "at"}`` atomically: a temporary file in the
  same directory is fsynced and then renamed over ``KILL``, so readers never see a partial file.
* Fails closed: if the flag's state cannot be determined (permission errors, a ``KILL`` directory, ...)
  it counts as engaged, and if :meth:`engage` cannot persist the file at all this process stays halted
  in memory until :meth:`release`.

Thread-safe. :meth:`engaged` is lock-free and costs one ``lstat`` call, because the GUI worker thread
calls it right before clicking; writers are serialized by a lock.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .clock import Clock, SystemClock
from .domain import ensure_utc

log = logging.getLogger(__name__)

KILL_FILE_NAME = "KILL"
MAX_REASON_CHARS = 500
MAX_BY_CHARS = 100
#: A kill file is tiny; anything beyond this is garbage and is not read further.
MAX_READ_BYTES = 64 * 1024
NO_REASON = "kill file present (no reason given)"

_TMP_PREFIX = ".KILL."
_TMP_SUFFIX = ".tmp"
# Windows refuses to replace/remove a file another process has open for a moment (e.g. a reader).
_WINDOWS_RETRIES = 5
_WINDOWS_RETRY_DELAY_SECONDS = 0.02


class KillSwitchError(Exception):
    """The kill switch file could not be written or removed (the message is meant for the operator)."""


@dataclass(frozen=True)
class _KillInfo:
    engaged: bool
    reason: str | None
    by: str | None
    at: str | None
    source: str  # "file", "memory" (engage could not persist) or "none"
    error: str | None = None


class KillSwitch:
    """File-backed kill switch shared by the engine, the dashboard, the CLI and the GUI worker thread."""

    def __init__(self, data_dir: Path, *, clock: Clock | None = None) -> None:
        self._dir = Path(data_dir)
        self._path = self._dir / KILL_FILE_NAME
        self._clock: Clock = clock or SystemClock()
        self._lock = threading.Lock()
        # Set only when engage() could not persist the flag file: keeps this process halted regardless.
        self._latched = threading.Event()
        self._latched_info: tuple[str, str, str] | None = None  # (reason, by, at)
        self._last_error: str | None = None

    @property
    def path(self) -> Path:
        """The flag file (``data_dir / "KILL"``)."""
        return self._path

    # ---------------------------------------------------------------------------------------- reading
    def engaged(self) -> bool:
        """``True`` if the flag file exists (whatever it contains) or its state cannot be determined."""
        if self._latched.is_set():
            return True
        try:
            os.lstat(self._path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            self._note_error(f"cannot check {self._path}: {exc}; treating the kill switch as engaged")
            return True
        return True

    def reason(self) -> str | None:
        """Why the switch is engaged (tolerant of hand-written or garbage content); ``None`` if released."""
        info = self._read()
        return info.reason if info.engaged else None

    def state(self) -> dict[str, Any]:
        """JSON-serializable state for the dashboard / CLI."""
        info = self._read()
        return {
            "engaged": info.engaged,
            "reason": info.reason if info.engaged else None,
            "by": info.by,
            "at": info.at,
            "source": info.source,
            "path": str(self._path),
            "error": info.error or self._last_error,
        }

    def _read(self) -> _KillInfo:
        latched = self._latched_info if self._latched.is_set() else None
        try:
            entry = os.lstat(self._path)
        except FileNotFoundError:
            if latched is not None:
                reason, by, at = latched
                return _KillInfo(True, reason, by, at, "memory", "kill file could not be written")
            return _KillInfo(False, None, None, None, "none")
        except OSError as exc:
            return _KillInfo(
                True, f"kill switch state unreadable ({exc.strerror or exc})", None, None, "file", str(exc)
            )
        if not stat.S_ISREG(entry.st_mode):
            if latched is not None:
                return _KillInfo(True, latched[0], latched[1], latched[2], "file")
            return _KillInfo(True, f"{KILL_FILE_NAME} entry present (not a regular file)", None, None, "file")
        try:
            raw = _read_regular_file(self._path)
        except OSError as exc:
            return _KillInfo(True, f"kill file present but unreadable ({exc.strerror or exc})", None, None, "file")
        parsed_reason, parsed_by, parsed_at = _parse_kill_file(raw)
        if parsed_reason is None and latched is not None:
            # The fallback flag file is empty: the in-memory reason is the real one.
            return _KillInfo(True, latched[0], latched[1], latched[2], "file")
        return _KillInfo(True, parsed_reason or NO_REASON, parsed_by, parsed_at, "file")

    # ---------------------------------------------------------------------------------------- writing
    def engage(self, reason: str, by: str) -> None:
        """Engage: atomically write ``{"reason", "by", "at"}`` to the flag file (overwrites an older reason).

        If the JSON file cannot be written, an empty flag file is tried instead and this process stays
        halted in memory either way. :class:`KillSwitchError` is raised only when no flag file could be
        created at all (the halt then holds for this process only).
        """
        clean_reason = _clean(reason, MAX_REASON_CHARS) or "no reason given"
        clean_by = _clean(by, MAX_BY_CHARS) or "unknown"
        at = ensure_utc(self._clock.now()).isoformat()
        payload = json.dumps({"reason": clean_reason, "by": clean_by, "at": at}, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                self._atomic_write(payload.encode("utf-8"))
            except OSError as exc:
                message = f"could not write the kill switch file {self._path}: {exc}"
                self._latched_info = (clean_reason, clean_by, at)
                self._latched.set()
                self._last_error = message
                if self._create_empty_flag():
                    log.critical("%s; created an empty flag file instead (reason kept in memory)", message)
                    return
                log.critical("%s; trading is halted in this process only", message)
                raise KillSwitchError(f"{message}; trading is halted in this process only") from exc
            self._last_error = None
            log.warning("kill switch ENGAGED by %s: %s", clean_by, clean_reason)

    def release(self, by: str) -> None:
        """Release: remove the flag file (no-op if it is absent) and clear any in-memory halt."""
        clean_by = _clean(by, MAX_BY_CHARS) or "unknown"
        with self._lock:
            previous = self._read()
            try:
                _retry_on_windows(lambda: os.remove(self._path))
            except FileNotFoundError:
                pass
            except OSError as exc:
                message = f"could not remove the kill switch file {self._path}: {exc}; delete it by hand to resume"
                self._last_error = message
                log.error("%s", message)
                raise KillSwitchError(message) from exc
            self._latched.clear()
            self._latched_info = None
            self._last_error = None
            if previous.engaged:
                log.warning("kill switch RELEASED by %s (was: %s)", clean_by, previous.reason)
            else:
                log.info("kill switch release by %s: it was not engaged", clean_by)
            self._fsync_dir()

    # ---------------------------------------------------------------------------------------- helpers
    def _atomic_write(self, data: bytes) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=_TMP_PREFIX, suffix=_TMP_SUFFIX, dir=self._dir)
        tmp = Path(tmp_name)
        done = False
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            _retry_on_windows(lambda: os.replace(tmp, self._path))
            done = True
        finally:
            if not done:
                with contextlib.suppress(OSError):
                    tmp.unlink()
        self._fsync_dir()

    def _create_empty_flag(self) -> bool:
        """Last resort for :meth:`engage`: an empty ``KILL`` file still counts as engaged."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
            os.close(fd)
        except OSError as exc:
            log.error("could not create an empty kill switch file %s: %s", self._path, exc)
        try:
            os.lstat(self._path)
        except OSError:
            return False
        return True

    def _fsync_dir(self) -> None:
        """Persist the rename/unlink itself (POSIX only; best effort)."""
        if sys.platform == "win32":
            return
        try:
            fd = os.open(self._dir, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError as exc:
            log.debug("fsync of %s failed: %s", self._dir, exc)
        finally:
            os.close(fd)

    def _note_error(self, message: str) -> None:
        if message != self._last_error:
            log.error("%s", message)
        self._last_error = message


def _read_regular_file(path: Path) -> bytes:
    """Read at most :data:`MAX_READ_BYTES` without following symlinks or blocking on special files."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return b""
        chunks: list[bytes] = []
        remaining = MAX_READ_BYTES
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _parse_kill_file(raw: bytes) -> tuple[str | None, str | None, str | None]:
    """``(reason, by, at)`` from the file content; never raises. Plain text counts as the reason."""
    text = raw.decode("utf-8", errors="replace").strip().lstrip("﻿")
    if not text:
        return None, None, None
    try:
        data: Any = json.loads(text)
    except (ValueError, RecursionError):
        return _clean(text, MAX_REASON_CHARS) or None, None, None
    if isinstance(data, dict):
        reason = data.get("reason")
        by = data.get("by")
        at = data.get("at")
        return (
            (_clean(reason, MAX_REASON_CHARS) or None) if isinstance(reason, str) else None,
            (_clean(by, MAX_BY_CHARS) or None) if isinstance(by, str) else None,
            _parse_at(at),
        )
    if isinstance(data, str):
        return _clean(data, MAX_REASON_CHARS) or None, None, None
    return _clean(text, MAX_REASON_CHARS) or None, None, None


def _parse_at(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment.astimezone(UTC).isoformat()


def _clean(value: object, limit: int) -> str:
    """Single-line printable text (no control/format characters), at most ``limit`` characters."""
    text = str(value)
    printable = "".join(ch if ch.isprintable() else " " for ch in text)
    collapsed = " ".join(printable.split())
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1].rstrip() + "…"
    return collapsed


def _retry_on_windows(action: Callable[[], object]) -> None:
    """Run ``action``; on Windows retry briefly on ``PermissionError`` (file momentarily held open)."""
    attempts = _WINDOWS_RETRIES if sys.platform == "win32" else 1
    for attempt in range(attempts):
        try:
            action()
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(_WINDOWS_RETRY_DELAY_SECONDS)
