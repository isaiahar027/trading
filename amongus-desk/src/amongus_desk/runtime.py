"""Process plumbing: single-instance lock and logging configuration.

Two desks clicking the same terminal would interleave mouse and keyboard input, so the desk refuses to
start while another instance holds the lock on ``data_dir/desk.lock``.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

_DESK_HANDLER_ATTR = "_amongus_desk_handler"


class AlreadyRunningError(RuntimeError):
    """Another desk process holds the instance lock."""


class SingleInstanceLock:
    """Exclusive, non-blocking OS file lock (``msvcrt`` on Windows, ``fcntl`` elsewhere).

    The OS releases the lock automatically if the process dies, so a crash never leaves a stale lock.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self._lock(fd)
        except OSError as exc:
            os.close(fd)
            raise AlreadyRunningError(
                f"another desk instance is already running (lock held on {self.path}); stop it first"
            ) from exc
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            self._unlock(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _lock(fd: int) -> None:
        if sys.platform == "win32":
            import msvcrt  # noqa: PLC0415 - platform-specific

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # noqa: PLC0415 - platform-specific

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(fd: int) -> None:
        if sys.platform == "win32":
            import msvcrt  # noqa: PLC0415

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # noqa: PLC0415

            fcntl.flock(fd, fcntl.LOCK_UN)

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.release()


class JsonFormatter(logging.Formatter):
    """One JSON object per line: easy to grep, ship or load into a spreadsheet."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(data_dir: Path, level: str = "INFO", *, console: bool = True) -> Path:
    """Configure root logging: rotating JSON-lines file under ``data_dir/logs`` plus optional console.

    Idempotent: calling it again replaces the handlers it installed before instead of duplicating them.
    Returns the log file path.
    """
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "desk.log"

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _DESK_HANDLER_ATTR, False):
            root.removeHandler(handler)
            handler.close()

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    setattr(file_handler, _DESK_HANDLER_ATTR, True)
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        setattr(stream, _DESK_HANDLER_ATTR, True)
        root.addHandler(stream)

    root.setLevel(level.upper())
    for noisy in ("httpx2", "httpcore", "anthropic", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file
