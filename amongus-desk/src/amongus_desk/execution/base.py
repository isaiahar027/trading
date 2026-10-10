"""Execution venue contract.

Invariants every venue must uphold (the engine and the risk logic rely on them):

1. ``submit``/``close_*`` never raise for trading outcomes; they always return an
   :class:`~amongus_desk.domain.ExecutionReport`. Only programming errors may raise.
2. ``ABORTED`` means *nothing* reached the broker. A venue may only report ``ABORTED`` if it can
   prove no irreversible action (click / API call) happened.
3. ``on_before_click`` is called exactly once, synchronously, immediately before the first
   irreversible action. The engine uses it to persist a write-ahead ``SUBMITTING`` marker so a crash
   after the click is detected on restart. If it raises, the venue must abort without clicking. Venues may
   call it from the event-loop thread or from a worker thread (UniTraderVenue runs it on the event loop while
   its GUI thread waits), so it must be thread-safe and must not await.
4. After the irreversible action the venue reports ``CONFIRMED``, ``REJECTED`` or ``UNCONFIRMED`` —
   never ``ABORTED``. When in doubt: ``UNCONFIRMED``.
5. ``is_halted`` (constructor argument) is checked at the start and again immediately before the click
   for **opening** orders only. Closing orders (``close_position``/``close_all``) must still run while the
   kill switch is engaged; that is how PANIC flattens the book.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..domain import ExecutionReport, OrderIntent, Position, Quote, VenueHealth

BeforeClick = Callable[[], None]
HaltCheck = Callable[[], bool]


class ExecutionVenue(Protocol):
    name: str
    supports_protective_orders: bool  # True when SL/TP are placed at the broker together with the entry

    async def preflight(self) -> VenueHealth:
        """Thorough start-up check (window, layout, account badge, templates). Never clicks."""
        ...

    async def health(self) -> VenueHealth:
        """Cheap periodic check used by the watchdog. Never clicks."""
        ...

    async def submit(self, intent: OrderIntent, quote: Quote | None, on_before_click: BeforeClick) -> ExecutionReport:
        """Open a position for ``intent`` (purpose ``open``) at market."""
        ...

    async def close_position(
        self, position: Position, intent: OrderIntent, quote: Quote | None, on_before_click: BeforeClick
    ) -> ExecutionReport:
        """Close one position (purpose ``close``)."""
        ...

    async def close_all(self, intent: OrderIntent, on_before_click: BeforeClick) -> ExecutionReport:
        """Close everything (purpose ``close_all``); used by the panic button."""
        ...

    async def aclose(self) -> None: ...
