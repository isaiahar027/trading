"""Market data feed contract."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol

from ..domain import Bar, Quote


class FeedError(Exception):
    """A feed problem the engine should surface (bad credentials, quota exhausted, malformed data)."""


class MarketDataFeed(Protocol):
    name: str

    async def warmup(self, symbols: Sequence[str], bars: int) -> dict[str, list[Bar]]:
        """Return up to ``bars`` most recent *closed* bars per symbol, oldest first.

        Called once before :meth:`stream`. Warmup bars must never trigger signals.
        """
        ...

    def stream(self) -> AsyncIterator[Bar]:
        """Yield newly *closed* bars as they become available, in time order per symbol.

        A bar is yielded at most once. Bars that are still forming are never yielded. The iterator
        ends only when the feed is closed (or, for finite feeds like CSV/backtests, when data runs out).
        """
        ...

    def latest_quote(self, symbol: str) -> Quote | None:
        """Most recent bid/ask if the feed provides one; ``None`` otherwise (callers then assume
        the instrument's typical spread around the last close)."""
        ...

    def status(self) -> dict[str, Any]:
        """JSON-serializable health/status for the dashboard (last bar per symbol, credits used, errors)."""
        ...

    async def aclose(self) -> None: ...
