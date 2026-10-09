"""Tamper-evident, append-only audit journal (JSON lines, SHA-256 hash chain).

Each line is ``{"seq", "ts", "type", "data", "prev", "hash"}`` where ``hash`` is the SHA-256 of the
canonical JSON of the other five fields and ``prev`` is the previous line's hash. Editing, deleting,
inserting or reordering lines breaks the chain, and :meth:`AuditLog.verify` reports the first bad entry.
This proves integrity against accidental or casual edits; someone able to rewrite the whole file can
of course rebuild a consistent chain, so keep off-box copies if that matters to you.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .clock import Clock, SystemClock

log = logging.getLogger(__name__)

GENESIS = "0" * 64
_FIELDS = ("seq", "ts", "type", "data", "prev")


class AuditError(Exception):
    """The journal is corrupt in a way that can't be repaired automatically."""


@dataclass(frozen=True, slots=True)
class AuditEntry:
    seq: int
    ts: str
    type: str
    data: dict[str, Any]
    prev: str
    hash: str


@dataclass(frozen=True, slots=True)
class AuditVerifyResult:
    ok: bool
    entries: int
    first_bad_seq: int | None
    message: str


def _sanitize(value: Any) -> Any:
    """JSON-normalize ``value`` (Decimal/datetime/Enum -> str, non-finite floats -> str)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {str(k): _sanitize(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_sanitize(v) for v in value]
    if value is None or isinstance(value, bool | int | str):
        return value
    return str(value)


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(body: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical({k: body[k] for k in _FIELDS}).encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: Path, fsync: bool = True, clock: Clock | None = None) -> None:
        self.path = path
        self._fsync = fsync
        self._clock = clock or SystemClock()
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        dropped = self._repair_tail()
        self._seq, self._prev = self._resume()
        self._fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        if dropped:
            self.append("audit.recovered", {"truncated_bytes": dropped})

    def _repair_tail(self) -> int:
        """Drop a trailing partial line left by a crash mid-write. Returns the number of bytes removed."""
        if not self.path.exists():
            return 0
        raw = self.path.read_bytes()
        if not raw or raw.endswith(b"\n"):
            return 0
        keep = raw.rfind(b"\n") + 1
        with self.path.open("r+b") as fh:
            fh.truncate(keep)
            fh.flush()
            os.fsync(fh.fileno())
        dropped = len(raw) - keep
        log.warning("audit journal %s: dropped a %d-byte partial last line", self.path, dropped)
        return dropped

    def _resume(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS
        last: bytes | None = None
        with self.path.open("rb") as fh:
            for line in fh:
                if line.strip():
                    last = line
        if last is None:
            return 0, GENESIS
        try:
            entry = json.loads(last)
            return int(entry["seq"]), str(entry["hash"])
        except (ValueError, KeyError, TypeError) as exc:
            raise AuditError(f"audit journal {self.path} has an unreadable last line; run `desk audit-verify`") from exc

    def append(self, type_: str, data: dict[str, Any]) -> AuditEntry:
        with self._lock:
            body: dict[str, Any] = {
                "seq": self._seq + 1,
                "ts": self._clock.now().isoformat(),
                "type": type_,
                "data": _sanitize(data),
                "prev": self._prev,
            }
            body["hash"] = _digest(body)
            line = (_canonical(body) + "\n").encode("utf-8")
            written = os.write(self._fd, line)
            if written != len(line):
                raise AuditError(f"short write to {self.path}")
            if self._fsync:
                os.fsync(self._fd)
            self._seq = body["seq"]
            self._prev = body["hash"]
            return AuditEntry(**body)

    def close(self) -> None:
        with self._lock, contextlib.suppress(OSError):
            os.close(self._fd)

    @staticmethod
    def verify(path: Path) -> AuditVerifyResult:
        if not path.exists():
            return AuditVerifyResult(True, 0, None, "no journal yet")
        prev = GENESIS
        expected = 1
        count = 0
        with path.open("rb") as fh:
            for lineno, raw in enumerate(fh, start=1):
                if not raw.strip():
                    continue
                try:
                    entry = json.loads(raw)
                    seq = int(entry["seq"])
                    body = {k: entry[k] for k in _FIELDS}
                    stored = str(entry["hash"])
                except (ValueError, KeyError, TypeError):
                    return AuditVerifyResult(False, count, expected, f"line {lineno}: not a valid journal entry")
                if seq != expected:
                    return AuditVerifyResult(
                        False, count, expected, f"line {lineno}: expected seq {expected}, got {seq}"
                    )
                if body["prev"] != prev:
                    return AuditVerifyResult(False, count, seq, f"line {lineno}: chain broken (prev hash mismatch)")
                if _digest(body) != stored:
                    return AuditVerifyResult(False, count, seq, f"line {lineno}: content was modified (hash mismatch)")
                prev = stored
                expected += 1
                count += 1
        return AuditVerifyResult(True, count, None, f"{count} entries verified")
