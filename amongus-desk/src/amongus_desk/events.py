"""In-process event hub: the engine publishes, the dashboard (SSE) and tests subscribe."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from .clock import Clock, SystemClock
from .domain import DeskEvent

log = logging.getLogger(__name__)


class EventHub:
    def __init__(self, clock: Clock | None = None, history: int = 200, queue_size: int = 256) -> None:
        self._clock = clock or SystemClock()
        self._recent: deque[DeskEvent] = deque(maxlen=history)
        self._subscribers: set[asyncio.Queue[DeskEvent]] = set()
        self._queue_size = queue_size

    def publish(self, type_: str, data: dict[str, Any] | None = None) -> DeskEvent:
        event = DeskEvent(type=type_, at=self._clock.now(), data=data or {})
        self._recent.append(event)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A stalled browser must never block trading; it re-syncs from /api/state on reconnect.
                log.warning("dropping event %s for a slow subscriber", type_)
        return event

    def recent(self, limit: int = 50) -> list[DeskEvent]:
        return list(self._recent)[-limit:]

    async def subscribe(self) -> AsyncIterator[DeskEvent]:
        queue: asyncio.Queue[DeskEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
