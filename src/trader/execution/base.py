"""Broker interface. Only execution code has side effects on the venue."""
from __future__ import annotations

from typing import Protocol

from ..market import Fill, OrderBook


class Broker(Protocol):
    def execute(self, symbol: str, qty: float, ref_px: float, ts: int, reason: str,
                book: OrderBook | None = None, decision_id: str = "") -> Fill | None: ...
