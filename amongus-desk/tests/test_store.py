from __future__ import annotations

import sqlite3
import threading
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from amongus_desk.domain import (
    CrewId,
    CrewReport,
    ExecutionReport,
    FinalAction,
    IntentStatus,
    MeetingRecord,
    Mode,
    OrderIntent,
    Position,
    PositionStatus,
    Side,
    Vote,
)
from amongus_desk.store import SCHEMA_VERSION, Store
from factories import T0


def intent(**over: object) -> OrderIntent:
    data: dict[str, object] = {
        "meeting_id": "m1",
        "symbol": "EURUSD",
        "side": Side.BUY,
        "lots": Decimal("0.10"),
        "entry_ref": 1.08512,
        "stop_price": 1.08362,
        "target_price": 1.08812,
        "mode": Mode.DEMO,
        "created_at": T0,
    }
    data.update(over)
    return OrderIntent.model_validate(data)


def report(intent_id: str, status: IntentStatus = IntentStatus.CONFIRMED) -> ExecutionReport:
    return ExecutionReport(
        intent_id=intent_id,
        status=status,
        venue="unitrader",
        started_at=T0,
        finished_at=T0 + timedelta(seconds=2),
        clicked_at=T0 + timedelta(seconds=1),
        fill_price=1.08514,
        fill_price_source="feed_estimate",
        message="ok",
        evidence={"steps": ["a", "b"]},
    )


def position(pid: str = "p1", **over: object) -> Position:
    data: dict[str, object] = {
        "id": pid,
        "intent_id": "i1",
        "symbol": "EURUSD",
        "side": Side.SELL,
        "lots": Decimal("0.07"),
        "entry_price": 1.1,
        "entry_price_source": "venue",
        "stop_price": 1.101,
        "target_price": 1.098,
        "opened_at": T0,
        "time_stop_bars": 24,
    }
    data.update(over)
    return Position.model_validate(data)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "desk.db")
    yield s
    s.close()


def test_schema_and_wal(store: Store, tmp_path: Path) -> None:
    assert store.schema_version == SCHEMA_VERSION
    with sqlite3.connect(tmp_path / "desk.db") as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="newer than this desk"):
        Store(path)


def test_intent_round_trip_preserves_decimal_and_utc(store: Store) -> None:
    it = intent()
    store.add_intent(it)
    row = store.get_intent(it.id)
    assert row is not None
    assert row.intent == it
    assert row.intent.lots == Decimal("0.10") and str(row.intent.lots) == "0.10"
    assert row.status is IntentStatus.PENDING and row.report is None
    assert row.updated_at.tzinfo is not None
    assert store.get_intent("nope") is None


def test_status_transitions(store: Store) -> None:
    it = intent()
    store.add_intent(it)
    store.set_intent_status(it.id, IntentStatus.SUBMITTING)
    rep = report(it.id, IntentStatus.UNCONFIRMED)
    store.set_intent_status(it.id, IntentStatus.UNCONFIRMED, rep)
    row = store.get_intent(it.id)
    assert row is not None and row.status is IntentStatus.UNCONFIRMED and row.report == rep
    with pytest.raises(ValueError, match="illegal"):
        store.set_intent_status(it.id, IntentStatus.CONFIRMED)
    store.set_intent_status(it.id, IntentStatus.RECONCILED_FILLED)
    with pytest.raises(ValueError, match="illegal"):
        store.set_intent_status(it.id, IntentStatus.RECONCILED_NOT_FILLED)


@pytest.mark.parametrize(
    "final", [IntentStatus.ABORTED, IntentStatus.CONFIRMED, IntentStatus.REJECTED, IntentStatus.DRY_RUN]
)
def test_final_statuses_are_immutable(store: Store, final: IntentStatus) -> None:
    it = intent()
    store.add_intent(it)
    store.set_intent_status(it.id, final)
    for nxt in IntentStatus:
        with pytest.raises(ValueError):
            store.set_intent_status(it.id, nxt)


def test_reconcile_answers_only_for_unknown_outcomes(store: Store) -> None:
    it = intent()
    store.add_intent(it)
    with pytest.raises(ValueError):
        store.set_intent_status(it.id, IntentStatus.RECONCILED_FILLED)  # PENDING never reached the venue
    with pytest.raises(ValueError, match="unknown intent"):
        store.set_intent_status("missing", IntentStatus.ABORTED)


def test_report_must_match_intent(store: Store) -> None:
    it = intent()
    store.add_intent(it)
    with pytest.raises(ValueError, match="different intent"):
        store.set_intent_status(it.id, IntentStatus.CONFIRMED, report("other"))


def test_intents_with_status_and_recent(store: Store) -> None:
    ids = []
    for i in range(5):
        it = intent(created_at=T0 + timedelta(minutes=i))
        store.add_intent(it)
        ids.append(it.id)
    store.set_intent_status(ids[1], IntentStatus.SUBMITTING)
    store.set_intent_status(ids[3], IntentStatus.SUBMITTING)
    store.set_intent_status(ids[3], IntentStatus.UNCONFIRMED)
    pending = store.intents_with_status(IntentStatus.SUBMITTING, IntentStatus.UNCONFIRMED)
    assert [r.intent.id for r in pending] == [ids[1], ids[3]]
    assert store.intents_with_status() == []
    assert [r.intent.id for r in store.recent_intents(2)] == [ids[4], ids[3]]


def test_positions(store: Store) -> None:
    p_open = position("a", opened_at=T0)
    p_rec = position("b", opened_at=T0 + timedelta(minutes=1), status=PositionStatus.RECONCILE)
    p_closed = position(
        "c",
        opened_at=T0,
        status=PositionStatus.CLOSED,
        closed_at=T0 + timedelta(hours=1),
        exit_price=1.099,
        pnl=7.0,
    )
    for p in (p_open, p_rec, p_closed):
        store.upsert_position(p)
    assert [p.id for p in store.active_positions()] == ["a", "b"]
    assert [p.id for p in store.closed_positions_since(T0)] == ["c"]
    assert store.closed_positions_since(T0 + timedelta(hours=2)) == []
    updated = p_open.model_copy(update={"status": PositionStatus.CLOSING, "close_intent_id": "x"})
    store.upsert_position(updated)
    assert store.get_position("a") == updated
    store.delete_position("b")
    assert store.get_position("b") is None
    assert {p.id for p in store.recent_positions()} == {"a", "c"}


def test_meetings_and_kv(store: Store) -> None:
    m = MeetingRecord(
        symbol="EURUSD",
        called_at=T0,
        trigger="manual",
        signal=None,
        plan=None,
        reports=[CrewReport(crew_id=CrewId.CYAN_SCOUT, vote=Vote.EJECT, summary="no breakout")],
        decision=FinalAction.SKIP_ROUND,
        lots=None,
        transcript=["SYSTEM: meeting"],
    )
    store.add_meeting(m)
    assert store.recent_meetings() == [m]
    assert store.get_kv("equity") is None
    store.set_kv("equity", {"value": 10_000.5, "at": "2026-10-05T08:00:00+00:00"})
    store.set_kv("equity", {"value": 9_999.0})
    assert store.get_kv("equity") == {"value": 9_999.0}
    with pytest.raises(ValueError):
        store.set_kv("bad", float("nan"))


def test_data_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "desk.db"
    s1 = Store(path)
    it = intent()
    s1.add_intent(it)
    s1.set_intent_status(it.id, IntentStatus.SUBMITTING)
    s1.close()
    s2 = Store(path)
    row = s2.get_intent(it.id)
    assert row is not None and row.status is IntentStatus.SUBMITTING
    s2.close()


def test_concurrent_writers(store: Store) -> None:
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(25):
                it = intent(created_at=T0 + timedelta(seconds=n * 100 + i))
                store.add_intent(it)
                store.set_intent_status(it.id, IntentStatus.ABORTED)
                store.set_kv(f"k{n}", i)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(store.intents_with_status(IntentStatus.ABORTED)) == 150
    assert all(store.get_kv(f"k{n}") == 24 for n in range(6))
