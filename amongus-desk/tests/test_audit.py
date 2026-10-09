from __future__ import annotations

import json
import threading
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from amongus_desk.audit import GENESIS, AuditError, AuditLog
from amongus_desk.clock import ManualClock
from factories import T0


def _log(path: Path) -> AuditLog:
    return AuditLog(path, fsync=False, clock=ManualClock(T0))


def test_chain_verifies_and_normalizes_values(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = _log(path)
    first = log.append("intent.created", {"lots": Decimal("0.10"), "at": T0, "nan": float("nan"), "tags": ("a", 1)})
    second = log.append("intent.status", {"status": "CONFIRMED"})
    log.close()
    assert first.prev == GENESIS and second.prev == first.hash and second.seq == 2
    assert first.data == {"lots": "0.10", "at": str(T0), "nan": "nan", "tags": ["a", 1]}
    result = AuditLog.verify(path)
    assert result.ok and result.entries == 2 and result.first_bad_seq is None


def test_missing_journal_verifies_empty(tmp_path: Path) -> None:
    assert AuditLog.verify(tmp_path / "none.jsonl").ok


def _write_entries(path: Path, n: int) -> list[str]:
    log = _log(path)
    for i in range(n):
        log.append("e", {"i": i})
    log.close()
    return path.read_text(encoding="utf-8").splitlines()


def test_edit_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    lines = _write_entries(path, 4)
    entry = json.loads(lines[2])
    entry["data"]["i"] = 999
    lines[2] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = AuditLog.verify(path)
    assert not result.ok and result.first_bad_seq == 3 and "modified" in result.message


def test_deletion_and_reordering_are_detected(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    lines = _write_entries(path, 4)
    path.write_text("\n".join([lines[0], lines[2], lines[3]]) + "\n", encoding="utf-8")
    result = AuditLog.verify(path)
    assert not result.ok and result.first_bad_seq == 2
    path.write_text("\n".join([lines[1], lines[0], lines[2], lines[3]]) + "\n", encoding="utf-8")
    assert AuditLog.verify(path).first_bad_seq == 1


def test_rehashed_forgery_still_breaks_the_chain(tmp_path: Path) -> None:
    """Editing an entry and recomputing its own hash breaks the next entry's prev link."""
    import hashlib

    path = tmp_path / "a.jsonl"
    lines = _write_entries(path, 3)
    entry = json.loads(lines[1])
    entry["data"]["i"] = 42
    body = {k: entry[k] for k in ("seq", "ts", "type", "data", "prev")}
    entry["hash"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    lines[1] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = AuditLog.verify(path)
    assert not result.ok and result.first_bad_seq == 3 and "chain broken" in result.message


def test_reopen_resumes_chain(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _write_entries(path, 2)
    log = _log(path)
    third = log.append("e", {"i": 2})
    log.close()
    assert third.seq == 3
    assert AuditLog.verify(path).entries == 3


def test_partial_trailing_line_is_truncated_and_recorded(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _write_entries(path, 2)
    with path.open("ab") as fh:
        fh.write(b'{"seq": 3, "ts": "2026-')  # crash mid-write
    log = _log(path)
    nxt = log.append("e", {"after": True})
    log.close()
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [e["type"] for e in entries] == ["e", "e", "audit.recovered", "e"]
    assert entries[2]["data"]["truncated_bytes"] > 0
    assert nxt.seq == 4
    assert AuditLog.verify(path).ok


def test_unreadable_last_line_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    _write_entries(path, 1)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not json\n")
    with pytest.raises(AuditError, match="audit-verify"):
        _log(path)
    assert not AuditLog.verify(path).ok


def test_thread_safe_appends(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    clock = ManualClock(T0)
    log = AuditLog(path, fsync=False, clock=clock)

    def worker(n: int) -> None:
        for i in range(50):
            log.append("t", {"n": n, "i": i})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.close()
    result = AuditLog.verify(path)
    assert result.ok and result.entries == 400


def test_timestamps_come_from_the_clock(tmp_path: Path) -> None:
    clock = ManualClock(T0)
    log = AuditLog(tmp_path / "a.jsonl", fsync=False, clock=clock)
    clock.advance(timedelta(minutes=5).total_seconds())
    entry = log.append("e", {})
    log.close()
    assert entry.ts == (T0 + timedelta(minutes=5)).isoformat()
