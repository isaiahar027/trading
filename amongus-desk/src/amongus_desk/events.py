"""In-process event hub: the engine publishes, the dashboard (SSE) and tests subscribe."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from types import TracebackType
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

    def subscribe(self) -> Subscription:
        """Start receiving events. Use as ``async with hub.subscribe() as sub: async for event in sub: ...``
        so the subscription is released deterministically when the consumer goes away."""
        queue: asyncio.Queue[DeskEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return Subscription(self, queue)

    def _unsubscribe(self, queue: asyncio.Queue[DeskEvent]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


class Subscription:
    """An event stream for one consumer; iterate it, and close it (or use ``async with``) when done."""

    def __init__(self, hub: EventHub, queue: asyncio.Queue[DeskEvent]) -> None:
        self._hub = hub
        self._queue = queue
        self._closed = False

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> DeskEvent:
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    async def next_within(self, seconds: float) -> DeskEvent | None:
        """Next event, or ``None`` after ``seconds`` (lets SSE handlers send keep-alives)."""
        try:
            return await asyncio.wait_for(self._queue.get(), seconds)
        except TimeoutError:
            return None

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._hub._unsubscribe(self._queue)

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()
