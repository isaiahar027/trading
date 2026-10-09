from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from amongus_desk.runtime import AlreadyRunningError, SingleInstanceLock, configure_logging


def test_second_lock_in_same_process_fails(tmp_path: Path) -> None:
    path = tmp_path / "desk.lock"
    with SingleInstanceLock(path), pytest.raises(AlreadyRunningError, match="already running"):
        SingleInstanceLock(path).acquire()
    # released: can be taken again
    lock = SingleInstanceLock(path)
    lock.acquire()
    lock.acquire()  # idempotent
    lock.release()
    lock.release()


def test_lock_held_by_another_process(tmp_path: Path) -> None:
    path = tmp_path / "desk.lock"
    with SingleInstanceLock(path):
        code = (
            "import sys; from pathlib import Path; from amongus_desk.runtime import SingleInstanceLock, "
            "AlreadyRunningError\n"
            "try:\n    SingleInstanceLock(Path(sys.argv[1])).acquire()\nexcept AlreadyRunningError:\n    sys.exit(3)\n"
            "sys.exit(0)\n"
        )
        result = subprocess.run([sys.executable, "-c", code, str(path)], check=False, timeout=30)
        assert result.returncode == 3
    result = subprocess.run([sys.executable, "-c", code, str(path)], check=False, timeout=30)
    assert result.returncode == 0


def test_lock_file_records_pid(tmp_path: Path) -> None:
    import os

    path = tmp_path / "desk.lock"
    with SingleInstanceLock(path):
        assert path.read_text().strip() == str(os.getpid())


def test_configure_logging_is_idempotent_and_writes_json(tmp_path: Path) -> None:
    root = logging.getLogger()
    before = len(root.handlers)
    log_file = configure_logging(tmp_path, "DEBUG", console=True)
    configure_logging(tmp_path, "INFO", console=True)
    assert len(root.handlers) == before + 2  # file + console, not duplicated
    try:
        logging.getLogger("amongus_desk.test").warning("hello %s", "world")
        try:
            raise ValueError("boom")
        except ValueError:
            logging.getLogger("amongus_desk.test").exception("failed")
        for handler in root.handlers:
            handler.flush()
        lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        assert {"ts", "level", "logger", "msg"} <= set(lines[0])
        assert lines[0]["msg"] == "hello world" and lines[0]["level"] == "WARNING"
        assert "ValueError: boom" in lines[1]["exc"]
        assert log_file == tmp_path / "logs" / "desk.log"
    finally:
        configure_logging(tmp_path, "WARNING", console=False)
        for handler in list(root.handlers):
            if getattr(handler, "_amongus_desk_handler", False):
                root.removeHandler(handler)
                handler.close()
