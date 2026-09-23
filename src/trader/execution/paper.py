"""Paper broker: fills against the live book (or a reference price) with fees + slippage."""
from __future__ import annotations

from ..config import CostConfig
from ..market import Fill, OrderBook


class PaperBroker:
    def __init__(self, costs: CostConfig):
        self.costs = costs

    def fill_price(self, qty: float, ref_px: float, book: OrderBook | None) -> float:
        side = 1 if qty > 0 else -1
        if book is not None and book.bids and book.asks:
            # walk the book for the size; anything beyond visible depth pays the last level
            levels = book.asks if side > 0 else book.bids
            remaining, cost = abs(qty), 0.0
            for lvl in levels:
                take = min(remaining, lvl.sz)
                cost += take * lvl.px
                remaining -= take
                if remaining <= 0:
                    break
            if remaining > 0:
                cost += remaining * levels[-1].px
            touch = cost / abs(qty)
        else:
            touch = ref_px
        return touch * (1 + side * self.costs.slippage_bps / 1e4)

    def execute(self, symbol, qty, ref_px, ts, reason, book=None, decision_id=""):
        if qty == 0:
            return None
        px = self.fill_price(qty, ref_px, book)
        fee = abs(qty) * px * self.costs.taker_fee_bps / 1e4
        return Fill(symbol, qty, px, fee, ts, reason, decision_id)
