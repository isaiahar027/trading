from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from amongus_desk.clock import ManualClock
from amongus_desk.killswitch import NO_REASON, KillSwitch
from factories import T0


def ks(tmp_path: Path) -> KillSwitch:
    return KillSwitch(tmp_path, clock=ManualClock(T0))


def test_engage_and_release(tmp_path: Path) -> None:
    k = ks(tmp_path)
    assert not k.engaged() and k.reason() is None
    k.engage("daily loss limit", by="engine")
    assert k.engaged() and k.reason() == "daily loss limit"
    data = json.loads((tmp_path / "KILL").read_text(encoding="utf-8"))
    assert data == {"reason": "daily loss limit", "by": "engine", "at": T0.isoformat()}
    state = k.state()
    assert state["engaged"] and state["by"] == "engine" and state["source"] == "file"
    k.release(by="operator")
    assert not k.engaged() and not (tmp_path / "KILL").exists()
    k.release(by="operator")  # idempotent


def test_hand_created_empty_file_counts(tmp_path: Path) -> None:
    (tmp_path / "KILL").write_bytes(b"")
    k = ks(tmp_path)
    assert k.engaged() and k.reason() == NO_REASON


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"stop now please\n", "stop now please"),
        (b'"quoted"', "quoted"),
        (b"\xff\xfe garbage \x00\x01", None),
        (b'{"reason": 5, "by": []}', NO_REASON),
        (b"[1, 2]", "[1, 2]"),
    ],
)
def test_tolerant_reason_parsing(tmp_path: Path, content: bytes, reason: str | None) -> None:
    (tmp_path / "KILL").write_bytes(content)
    k = ks(tmp_path)
    assert k.engaged()
    got = k.reason()
    assert got is not None
    if reason is not None:
        assert got == reason


def test_reason_is_sanitized_and_truncated(tmp_path: Path) -> None:
    k = ks(tmp_path)
    k.engage("line1\nline2\x00" + "x" * 1000, by="op\r\n")
    reason = k.reason()
    assert reason is not None and "\n" not in reason and len(reason) <= 500
    assert k.state()["by"] == "op"


def test_no_partial_file_ever_visible(tmp_path: Path) -> None:
    k = ks(tmp_path)
    stop = threading.Event()
    seen_bad: list[bytes] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                raw = (tmp_path / "KILL").read_bytes()
            except FileNotFoundError:
                continue
            if raw:
                try:
                    json.loads(raw)
                except ValueError:
                    seen_bad.append(raw)

    thread = threading.Thread(target=reader)
    thread.start()
    for i in range(200):
        k.engage(f"reason {i} " + "y" * 200, by="t")
    stop.set()
    thread.join()
    assert seen_bad == []
    assert not list(tmp_path.glob(".KILL.*.tmp"))


def test_concurrent_engage_and_check(tmp_path: Path) -> None:
    k = ks(tmp_path)
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for _ in range(50):
                k.engage(f"w{n}", by="t")
                assert k.engaged()
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and k.engaged()


def test_directory_named_kill_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "KILL").mkdir()
    k = ks(tmp_path)
    assert k.engaged()
    assert k.reason() is not None


def test_unwritable_directory_latches_in_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    k = ks(tmp_path)

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr("amongus_desk.killswitch.tempfile.mkstemp", boom)
    real_open = os.open

    def no_create(path: object, flags: int, *args: object) -> int:
        if str(path).endswith("KILL"):
            raise OSError("read-only file system")
        return real_open(path, flags, *args)  # type: ignore[arg-type]

    monkeypatch.setattr("amongus_desk.killswitch.os.open", no_create)
    with pytest.raises(Exception, match="halted in this process only"):
        k.engage("disk full", by="engine")
    assert k.engaged() and k.reason() == "disk full"
    monkeypatch.undo()
    k.release(by="op")
    assert not k.engaged()
