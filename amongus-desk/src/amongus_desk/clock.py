"""Time source abstraction so engine, risk and watchdog logic are testable without real sleeps."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .domain import ensure_utc


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds for measuring durations (immune to wall-clock jumps)."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class ManualClock:
    """Deterministic clock for tests and backtests. ``sleep`` advances time instantly."""

    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def set(self, moment: datetime) -> None:
        moment = ensure_utc(moment)
        delta = (moment - self._now).total_seconds()
        if delta > 0:
            self._mono += delta
        self._now = moment

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(max(0.0, seconds))
        await asyncio.sleep(0)
