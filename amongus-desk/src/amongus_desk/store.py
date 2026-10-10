"""SQLite persistence for intents, positions, meetings and small key/value state.

One connection in WAL mode with ``synchronous=FULL`` (an order's write-ahead ``SUBMITTING`` marker must be
on disk before the click), guarded by a lock so the event loop and the GUI worker thread can both write.
Records are stored as their Pydantic JSON plus a few indexed columns.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .domain import (
    ExecutionReport,
    IntentStatus,
    MeetingRecord,
    OrderIntent,
    Position,
    PositionStatus,
    ensure_utc,
    utc_now,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_MIGRATIONS: dict[int, list[str]] = {
    1: [
        """CREATE TABLE intents (
               id TEXT PRIMARY KEY,
               purpose TEXT NOT NULL,
               symbol TEXT NOT NULL,
               status TEXT NOT NULL,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL,
               intent_json TEXT NOT NULL,
               report_json TEXT
           )""",
        "CREATE INDEX intents_status ON intents(status)",
        "CREATE INDEX intents_created ON intents(created_at)",
        """CREATE TABLE positions (
               id TEXT PRIMARY KEY,
               symbol TEXT NOT NULL,
               status TEXT NOT NULL,
               opened_at TEXT NOT NULL,
               closed_at TEXT,
               updated_at TEXT NOT NULL,
               position_json TEXT NOT NULL
           )""",
        "CREATE INDEX positions_status ON positions(status)",
        "CREATE INDEX positions_closed ON positions(closed_at)",
        """CREATE TABLE meetings (
               id TEXT PRIMARY KEY,
               symbol TEXT NOT NULL,
               called_at TEXT NOT NULL,
               decision TEXT NOT NULL,
               meeting_json TEXT NOT NULL
           )""",
        "CREATE INDEX meetings_called ON meetings(called_at)",
        """CREATE TABLE kv (
               key TEXT PRIMARY KEY,
               value_json TEXT NOT NULL,
               updated_at TEXT NOT NULL
           )""",
    ],
}

_S = IntentStatus
# Allowed status transitions. Final statuses are immutable; the operator's RECONCILED_* answers are only
# accepted for orders whose outcome is genuinely unknown.
_TRANSITIONS: dict[IntentStatus, frozenset[IntentStatus]] = {
    _S.PENDING: frozenset({_S.SUBMITTING, _S.ABORTED, _S.DRY_RUN, _S.CONFIRMED, _S.REJECTED, _S.UNCONFIRMED}),
    _S.SUBMITTING: frozenset(
        {_S.CONFIRMED, _S.UNCONFIRMED, _S.REJECTED, _S.RECONCILED_FILLED, _S.RECONCILED_NOT_FILLED}
    ),
    _S.UNCONFIRMED: frozenset({_S.RECONCILED_FILLED, _S.RECONCILED_NOT_FILLED}),
}

_ACTIVE_POSITION_STATUSES = (PositionStatus.OPEN, PositionStatus.CLOSING, PositionStatus.RECONCILE)


def _ts(value: datetime) -> str:
    """Fixed-width UTC timestamp so that string order equals time order in SQL."""
    return ensure_utc(value).astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class IntentRow:
    intent: OrderIntent
    status: IntentStatus
    report: ExecutionReport | None
    updated_at: datetime


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()

    # ------------------------------------------------------------------------------------ plumbing

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _migrate(self) -> None:
        current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"database {self.path} has schema version {current}, newer than this desk ({SCHEMA_VERSION})"
            )
        for version in range(current + 1, SCHEMA_VERSION + 1):
            with self._tx() as conn:
                for statement in _MIGRATIONS[version]:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {version}")
            log.info("database %s migrated to schema version %d", self.path, version)

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------------------------ intents

    def add_intent(self, intent: OrderIntent) -> None:
        now = _ts(utc_now())
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO intents (id, purpose, symbol, status, created_at, updated_at, intent_json, report_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    intent.id,
                    intent.purpose,
                    intent.symbol,
                    IntentStatus.PENDING.value,
                    _ts(intent.created_at),
                    now,
                    intent.model_dump_json(),
                ),
            )

    def set_intent_status(self, intent_id: str, status: IntentStatus, report: ExecutionReport | None = None) -> None:
        """Move an intent to ``status``; raises ``ValueError`` for unknown ids or illegal transitions."""
        with self._tx() as conn:
            self._set_intent_status(conn, intent_id, status, report)

    def resolve_intent(
        self, intent_id: str, status: IntentStatus, report: ExecutionReport | None, positions: Iterable[Position]
    ) -> None:
        """Set an intent's status and write the positions it affects in ONE transaction, so a crash can't
        leave a recorded fill without its position (or the reverse)."""
        with self._tx() as conn:
            self._set_intent_status(conn, intent_id, status, report)
            for position in positions:
                self._upsert_position(conn, position)

    @staticmethod
    def _set_intent_status(
        conn: sqlite3.Connection, intent_id: str, status: IntentStatus, report: ExecutionReport | None
    ) -> None:
        row = conn.execute("SELECT status FROM intents WHERE id = ?", (intent_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown intent {intent_id}")
        current = IntentStatus(row["status"])
        if status not in _TRANSITIONS.get(current, frozenset()):
            raise ValueError(f"illegal intent transition {current} -> {status} for {intent_id}")
        if report is not None and report.intent_id != intent_id:
            raise ValueError("report belongs to a different intent")
        if report is None:
            conn.execute(
                "UPDATE intents SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, _ts(utc_now()), intent_id),
            )
        else:
            conn.execute(
                "UPDATE intents SET status = ?, updated_at = ?, report_json = ? WHERE id = ?",
                (status.value, _ts(utc_now()), report.model_dump_json(), intent_id),
            )

    def get_intent(self, intent_id: str) -> IntentRow | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM intents WHERE id = ?", (intent_id,)).fetchone()
        return self._intent_row(row) if row is not None else None

    def intents_with_status(self, *statuses: IntentStatus) -> list[IntentRow]:
        if not statuses:
            return []
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM intents WHERE status IN ({marks}) ORDER BY created_at",  # noqa: S608 - placeholders only
                [s.value for s in statuses],
            ).fetchall()
        return [self._intent_row(r) for r in rows]

    def recent_intents(self, limit: int = 50) -> list[IntentRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM intents ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._intent_row(r) for r in rows]

    @staticmethod
    def _intent_row(row: sqlite3.Row) -> IntentRow:
        report_json = row["report_json"]
        return IntentRow(
            intent=OrderIntent.model_validate_json(row["intent_json"]),
            status=IntentStatus(row["status"]),
            report=ExecutionReport.model_validate_json(report_json) if report_json else None,
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    # ------------------------------------------------------------------------------------ positions

    def upsert_position(self, position: Position) -> None:
        with self._tx() as conn:
            self._upsert_position(conn, position)

    @staticmethod
    def _upsert_position(conn: sqlite3.Connection, position: Position) -> None:
        conn.execute(
            "INSERT INTO positions (id, symbol, status, opened_at, closed_at, updated_at, position_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET status = excluded.status, closed_at = excluded.closed_at,"
            " updated_at = excluded.updated_at, position_json = excluded.position_json",
            (
                position.id,
                position.symbol,
                position.status.value,
                _ts(position.opened_at),
                _ts(position.closed_at) if position.closed_at else None,
                _ts(utc_now()),
                position.model_dump_json(),
            ),
        )

    def position_intent_ids(self) -> set[str]:
        """Ids of every opening intent that has a position row (open or closed)."""
        with self._lock:
            rows = self._conn.execute("SELECT json_extract(position_json, '$.intent_id') AS iid FROM positions")
            return {r["iid"] for r in rows.fetchall() if r["iid"]}

    def get_position(self, position_id: str) -> Position | None:
        with self._lock:
            row = self._conn.execute("SELECT position_json FROM positions WHERE id = ?", (position_id,)).fetchone()
        return Position.model_validate_json(row["position_json"]) if row else None

    def delete_position(self, position_id: str) -> None:
        """Remove a position the operator confirmed never existed (reconciled "not filled")."""
        with self._tx() as conn:
            conn.execute("DELETE FROM positions WHERE id = ?", (position_id,))

    def active_positions(self) -> list[Position]:
        marks = ",".join("?" for _ in _ACTIVE_POSITION_STATUSES)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT position_json FROM positions WHERE status IN ({marks}) ORDER BY opened_at",  # noqa: S608
                [s.value for s in _ACTIVE_POSITION_STATUSES],
            ).fetchall()
        return [Position.model_validate_json(r["position_json"]) for r in rows]

    def closed_positions_since(self, since: datetime) -> list[Position]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT position_json FROM positions WHERE status = ? AND closed_at >= ? ORDER BY closed_at",
                (PositionStatus.CLOSED.value, _ts(since)),
            ).fetchall()
        return [Position.model_validate_json(r["position_json"]) for r in rows]

    def recent_positions(self, limit: int = 50) -> list[Position]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT position_json FROM positions ORDER BY opened_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Position.model_validate_json(r["position_json"]) for r in rows]

    # ------------------------------------------------------------------------------------ meetings

    def add_meeting(self, meeting: MeetingRecord) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO meetings (id, symbol, called_at, decision, meeting_json) VALUES (?, ?, ?, ?, ?)",
                (meeting.id, meeting.symbol, _ts(meeting.called_at), meeting.decision.value, meeting.model_dump_json()),
            )

    def recent_meetings(self, limit: int = 50) -> list[MeetingRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT meeting_json FROM meetings ORDER BY called_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [MeetingRecord.model_validate_json(r["meeting_json"]) for r in rows]

    # ------------------------------------------------------------------------------------ key/value

    def get_kv(self, key: str) -> Any | None:
        with self._lock:
            row = self._conn.execute("SELECT value_json FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else None

    def set_kv(self, key: str, value: Any) -> None:
        payload = json.dumps(value, allow_nan=False)
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO kv (key, value_json, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
                (key, payload, _ts(utc_now())),
            )
