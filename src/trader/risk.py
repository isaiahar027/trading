"""Hard, deterministic risk layer. The model can never override it.

This module deliberately imports nothing from `trader.jev` (a test enforces it): it
cannot see model outputs, only order intents, the portfolio and market data freshness.
It is consulted before EVERY order, including exits. It may shrink or reject an intent;
it never enlarges one. Reduce-only orders are always allowed through a tripped kill
switch so the book can be flattened.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import RiskConfig
from .market import OrderIntent, Portfolio


@dataclass(frozen=True)
class RiskVerdict:
    approved: bool
    qty: float
    reasons: tuple[str, ...]
    flatten_all: bool = False


class KillSwitch:
    """Two files under state_dir:
      ARMED -> operator has armed the system; nothing opens risk without it.
      KILL  -> tripped (by operator or automatically). Blocks all risk-increasing orders.
    File-based so an operator can trip it from any shell: `touch state/KILL`.
    """

    def __init__(self, state_dir: str | Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def armed(self) -> bool:
        return (self.dir / "ARMED").exists()

    @property
    def tripped(self) -> bool:
        return (self.dir / "KILL").exists()

    def arm(self, who: str = "operator") -> None:
        if self.tripped:
            raise RuntimeError("kill switch is tripped; investigate, then `trader reset-kill` before arming")
        (self.dir / "ARMED").write_text(json.dumps({"by": who, "ts": time.time()}))

    def disarm(self) -> None:
        (self.dir / "ARMED").unlink(missing_ok=True)

    def trip(self, reason: str) -> None:
        (self.dir / "KILL").write_text(json.dumps({"reason": reason, "ts": time.time()}))

    def reset(self) -> None:
        (self.dir / "KILL").unlink(missing_ok=True)

    def reason(self) -> str:
        p = self.dir / "KILL"
        return json.loads(p.read_text()).get("reason", "") if p.exists() else ""


@dataclass
class RiskState:
    peak_equity: float
    day: str
    day_start_equity: float
    order_times: list[float] = field(default_factory=list)


class RiskManager:
    def __init__(self, cfg: RiskConfig, kill: KillSwitch, equity: float, now_ms: int):
        self.cfg = cfg
        self.kill = kill
        self.state = RiskState(equity, _day(now_ms), equity)

    # ---- called every loop tick, before any decision
    def update(self, portfolio: Portfolio, now_ms: int) -> list[str]:
        eq = portfolio.equity
        s = self.state
        events = []
        if _day(now_ms) != s.day:
            s.day, s.day_start_equity = _day(now_ms), eq
            events.append("new UTC day: daily loss budget reset")
        s.peak_equity = max(s.peak_equity, eq)
        dd = self.drawdown(eq)
        if dd >= self.cfg.max_drawdown and not self.kill.tripped:
            self.kill.trip(f"max drawdown {dd:.2%} >= {self.cfg.max_drawdown:.0%}")
            events.append("KILL: max drawdown")
        return events

    def drawdown(self, equity: float) -> float:
        return 1 - equity / self.state.peak_equity if self.state.peak_equity > 0 else 0.0

    def daily_loss(self, equity: float) -> float:
        return max(0.0, 1 - equity / self.state.day_start_equity) if self.state.day_start_equity > 0 else 0.0

    # ---- called before every single order
    def check(self, intent: OrderIntent, portfolio: Portfolio, mid: float, data_ts_ms: int, now_ms: int) -> RiskVerdict:
        c = self.cfg
        eq = portfolio.equity
        pos_qty = portfolio.positions[intent.symbol].qty if intent.symbol in portfolio.positions else 0.0
        # Judge "reducing" from quantities only; the reduce_only flag is a claim, not a proof.
        reducing = pos_qty != 0 and abs(pos_qty + intent.qty) < abs(pos_qty) and (pos_qty + intent.qty) * pos_qty >= 0
        if intent.reduce_only and not reducing:
            return RiskVerdict(False, 0.0, ("reduce_only order would not reduce position",))

        if self.drawdown(eq) >= c.max_drawdown:
            if not self.kill.tripped:
                self.kill.trip(f"max drawdown {self.drawdown(eq):.2%}")
            return self._reduce_or_reject(reducing, intent, ("max drawdown breached",), flatten=True)
        if self.kill.tripped:
            return self._reduce_or_reject(reducing, intent, (f"kill switch tripped: {self.kill.reason()}",), flatten=True)
        if reducing:
            # Exits bypass sizing/rate limits but still need a sane price.
            if mid <= 0:
                return RiskVerdict(False, 0.0, ("no valid mid for exit",))
            return RiskVerdict(True, intent.qty, ("reduce-only",))

        reasons = []
        if not self.kill.armed:
            return RiskVerdict(False, 0.0, ("kill switch not armed: operator must `trader arm`",))
        if self.daily_loss(eq) >= c.max_daily_loss:
            return RiskVerdict(False, 0.0, (f"daily loss {self.daily_loss(eq):.2%} >= {c.max_daily_loss:.0%}",))
        age_s = (now_ms - data_ts_ms) / 1000
        if age_s > c.max_snapshot_age_s:
            return RiskVerdict(False, 0.0, (f"stale data: {age_s:.0f}s old",))
        if mid <= 0 or abs(intent.ref_px / mid - 1) > c.max_price_deviation:
            return RiskVerdict(False, 0.0, (f"price {intent.ref_px} deviates from mid {mid}",))
        now_s = now_ms / 1000
        self.state.order_times = [t for t in self.state.order_times if now_s - t < 3600]
        if len(self.state.order_times) >= c.max_orders_per_hour:
            return RiskVerdict(False, 0.0, ("order rate limit",))

        qty = intent.qty
        # per-instrument cap on the resulting position
        max_pos = c.max_position_frac * eq / mid
        new_pos = pos_qty + qty
        if abs(new_pos) > max_pos:
            capped = max_pos * (1 if new_pos > 0 else -1) - pos_qty
            reasons.append(f"clipped to max position {c.max_position_frac:.0%}")
            qty = capped if capped * qty > 0 else 0.0
        # gross leverage cap
        other_gross = portfolio.gross_notional - abs(portfolio.notional(intent.symbol))
        room = c.max_gross_leverage * eq - other_gross
        if abs(pos_qty + qty) * mid > room:
            allowed = max(0.0, room / mid) * (1 if qty > 0 else -1) - pos_qty
            reasons.append(f"clipped to gross leverage {c.max_gross_leverage}x")
            qty = allowed if allowed * qty > 0 else 0.0
        if abs(qty) * mid < c.min_order_notional:
            return RiskVerdict(False, 0.0, tuple(reasons) + (f"notional ${abs(qty) * mid:.2f} below minimum",))
        if abs(qty) > abs(intent.qty) + 1e-12:
            raise AssertionError("risk layer must never enlarge an order")
        self.state.order_times.append(now_s)
        return RiskVerdict(True, qty, tuple(reasons) or ("ok",))

    @staticmethod
    def _reduce_or_reject(reducing: bool, intent: OrderIntent, reasons: tuple, flatten: bool) -> RiskVerdict:
        if reducing:
            return RiskVerdict(True, intent.qty, reasons + ("reduce-only allowed",), flatten_all=flatten)
        return RiskVerdict(False, 0.0, reasons, flatten_all=flatten)


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")
