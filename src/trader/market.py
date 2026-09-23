"""Plain market/portfolio data types. All timestamps are epoch milliseconds, UTC."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Candle:
    t_open: int
    t_close: int  # last ms covered by the candle; the candle is only knowable after this
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass(frozen=True)
class Level:
    px: float
    sz: float


@dataclass(frozen=True)
class OrderBook:
    ts: int
    bids: tuple[Level, ...]  # best first
    asks: tuple[Level, ...]  # best first

    @property
    def mid(self) -> float:
        return (self.bids[0].px + self.asks[0].px) / 2


@dataclass(frozen=True)
class MarketCtx:
    """Perp context: funding is the hourly rate, OI in base units."""
    ts: int
    funding: float
    open_interest: float
    mark: float
    oracle: float
    premium: float
    day_volume_usd: float
    prev_day_px: float


@dataclass
class Position:
    symbol: str
    qty: float = 0.0          # signed, base units
    entry_px: float = 0.0
    stop_px: float = 0.0
    target_px: float = 0.0
    opened_ts: int = 0
    bars_held: int = 0
    entry_prob: float = 0.0   # calibrated p(win) at entry, for review
    decision_id: str = ""

    @property
    def side(self) -> int:
        return (self.qty > 0) - (self.qty < 0)


@dataclass(frozen=True)
class OrderIntent:
    """What policy wants. Risk may shrink or reject it; it can never grow it."""
    symbol: str
    qty: float                 # signed delta, base units
    ref_px: float              # price the policy sized against
    reduce_only: bool
    reason: str
    stop_px: float = 0.0
    target_px: float = 0.0
    entry_prob: float = 0.0
    decision_id: str = ""


@dataclass(frozen=True)
class Fill:
    symbol: str
    qty: float
    px: float
    fee: float
    ts: int
    reason: str
    decision_id: str = ""


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    marks: dict[str, float] = field(default_factory=dict)
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    funding_paid: float = 0.0

    def position(self, symbol: str) -> Position:
        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol)
        return self.positions[symbol]

    def notional(self, symbol: str) -> float:
        p = self.positions.get(symbol)
        return 0.0 if p is None else p.qty * self.marks.get(symbol, p.entry_px)

    @property
    def unrealized(self) -> float:
        return sum(p.qty * (self.marks.get(s, p.entry_px) - p.entry_px) for s, p in self.positions.items())

    @property
    def equity(self) -> float:
        # Perp accounting: cash holds collateral + realized; positions contribute unrealized only.
        return self.cash + self.unrealized

    @property
    def gross_notional(self) -> float:
        return sum(abs(self.notional(s)) for s in self.positions)

    def apply_fill(self, f: Fill) -> None:
        p = self.position(f.symbol)
        self.cash -= f.fee
        self.fees_paid += f.fee
        new_qty = p.qty + f.qty
        if p.qty == 0 or (p.qty > 0) == (f.qty > 0):
            # opening or adding: weighted entry
            total = abs(p.qty) + abs(f.qty)
            p.entry_px = (abs(p.qty) * p.entry_px + abs(f.qty) * f.px) / total
        else:
            closed = min(abs(f.qty), abs(p.qty))
            pnl = closed * (f.px - p.entry_px) * p.side
            self.cash += pnl
            self.realized_pnl += pnl
            if abs(f.qty) > abs(p.qty):  # flipped through zero
                p.entry_px = f.px
        p.qty = 0.0 if abs(new_qty) < 1e-12 else new_qty
        if p.qty == 0:
            self.positions[f.symbol] = Position(f.symbol)
