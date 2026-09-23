"""Deterministic state engine.

Turns raw market data into one compact snapshot per instrument per bar. The snapshot's
`jev_state` is the ONLY thing Jev ever sees. Every number is computed here, in code:
Jev is documented to be weak at arithmetic and numeric comparison (see docs.typesafe.ai
"Jev 1.13 jaggedness"), so each numeric feature is also mapped to a named bucket and
Jev is asked to judge the buckets, not do math.

Causality: an input is admissible only if it was knowable at `decision_ts`. A candle is
knowable after its close; a book/ctx after its own timestamp. Violations raise
LookaheadError instead of being silently dropped, because a leak is a bug, not noise.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from .market import Candle, MarketCtx, OrderBook, Portfolio

TOKEN_BUDGET = 400
MIN_BARS = 60
LOOKBACK = 500  # bars of history the engine considers (bounds cost; older bars add nothing)
HOURS_PER_YEAR = 24 * 365


class LookaheadError(RuntimeError):
    pass


class InsufficientData(RuntimeError):
    pass


def interval_ms(interval: str) -> int:
    unit = interval[-1]
    n = int(interval[:-1])
    return n * {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[unit]


def estimate_tokens(obj) -> int:
    """Conservative token estimate for compact JSON (~3 chars/token for numeric-heavy JSON)."""
    return math.ceil(len(json.dumps(obj, separators=(",", ":"))) / 3)


# ---------------------------------------------------------------- math helpers (pure)

def ema(values: list[float], span: int) -> float:
    k = 2 / (span + 1)
    out = values[0]
    for v in values[1:]:
        out = v * k + out * (1 - k)
    return out


def atr(candles: list[Candle], n: int = 14) -> float:
    trs = []
    for prev, cur in zip(candles[-n - 1:-1], candles[-n:]):
        trs.append(max(cur.h - cur.l, abs(cur.h - prev.c), abs(cur.l - prev.c)))
    return sum(trs) / len(trs)


def log_returns(closes: list[float]) -> list[float]:
    return [math.log(b / a) for a, b in zip(closes[:-1], closes[1:])]


def stdev(xs: list[float]) -> float:
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / max(len(xs) - 1, 1))


def realized_vol(closes: list[float], bars_per_year: float) -> float:
    return stdev(log_returns(closes)) * math.sqrt(bars_per_year)


def pct_rank(value: float, sample: list[float]) -> float:
    if not sample:
        return 0.5
    return sum(1 for s in sample if s <= value) / len(sample)


# ---------------------------------------------------------------- buckets (semantic labels)

def bucket(x: float, edges: list[float], labels: list[str]) -> str:
    for edge, label in zip(edges, labels):
        if x < edge:
            return label
    return labels[-1]


def trend_label(z: float) -> str:
    return bucket(z, [-1.5, -0.5, 0.5, 1.5], ["strong_down", "down", "flat", "up", "strong_up"])


def vol_label(pctile: float) -> str:
    return bucket(pctile, [0.2, 0.8, 0.95], ["low", "normal", "high", "extreme"])


def funding_label(apr: float) -> str:
    # apr as a fraction. Neutral band ~ +-10%/yr; beyond 50% the trade is crowded.
    return bucket(apr, [-0.5, -0.1, 0.1, 0.5],
                  ["shorts_crowded", "shorts_paying", "neutral", "longs_paying", "longs_crowded"])


def imbalance_label(imb: float) -> str:
    return bucket(imb, [-0.4, -0.15, 0.15, 0.4], ["heavy_asks", "asks", "balanced", "bids", "heavy_bids"])


# ---------------------------------------------------------------- snapshot

@dataclass(frozen=True)
class Snapshot:
    symbol: str
    decision_ts: int
    data_ts: int              # newest input timestamp actually used
    jev_state: dict           # the compact payload Jev sees
    numeric: dict             # full-precision features for code (policy/risk/review)

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.jev_state)


class StateEngine:
    def __init__(self, interval: str = "1h", token_budget: int = TOKEN_BUDGET):
        self.interval = interval
        self.bar_ms = interval_ms(interval)
        self.bars_per_year = HOURS_PER_YEAR * 3_600_000 / self.bar_ms
        self.token_budget = token_budget

    def build(
        self,
        symbol: str,
        decision_ts: int,
        candles: list[Candle],
        portfolio: Portfolio,
        peak_equity: float,
        day_start_equity: float,
        book: OrderBook | None = None,
        ctx: MarketCtx | None = None,
        anchor: dict | None = None,
    ) -> Snapshot:
        closed = [c for c in candles if c.t_close < decision_ts]
        if closed and any(b.t_open <= a.t_open for a, b in zip(closed, closed[1:])):
            raise LookaheadError(f"{symbol}: candles not strictly time-ordered")
        if book is not None and book.ts > decision_ts:
            raise LookaheadError(f"{symbol}: book ts {book.ts} > decision {decision_ts}")
        if ctx is not None and ctx.ts > decision_ts:
            raise LookaheadError(f"{symbol}: ctx ts {ctx.ts} > decision {decision_ts}")
        if len(closed) < MIN_BARS:
            raise InsufficientData(f"{symbol}: {len(closed)} closed bars < {MIN_BARS}")

        closed = closed[-LOOKBACK:]
        closes = [c.c for c in closed]
        last = closed[-1]
        a = atr(closed)
        px = book.mid if book is not None else last.c
        ema_fast, ema_slow = ema(closes[-60:], 20), ema(closes[-60:], 50)
        trend_z = (ema_fast - ema_slow) / a if a > 0 else 0.0
        rv = realized_vol(closes[-25:], self.bars_per_year)
        rv_hist = [realized_vol(closes[i - 25:i], self.bars_per_year) for i in range(25, len(closes), 6)]
        rv_pct = pct_rank(rv, rv_hist)
        hi24, lo24 = max(c.h for c in closed[-24:]), min(c.l for c in closed[-24:])
        range_pos = (last.c - lo24) / (hi24 - lo24) if hi24 > lo24 else 0.5
        vols = [c.v for c in closed[-49:-1]]
        vol_sd = stdev(vols) if len(vols) > 1 else 0.0
        vol_z = (last.v - sum(vols) / len(vols)) / vol_sd if vol_sd > 0 else 0.0

        def ret(n: int) -> float:
            return closes[-1] / closes[-1 - n] - 1 if len(closes) > n else 0.0

        pos = portfolio.positions.get(symbol)
        equity = portfolio.equity
        inv = (pos.qty * px / equity) if pos and equity > 0 else 0.0
        upnl_r = 0.0
        if pos and pos.qty and pos.stop_px:
            risk_per_unit = abs(pos.entry_px - pos.stop_px)
            upnl_r = (px - pos.entry_px) * pos.side / risk_per_unit if risk_per_unit else 0.0
        dd = 1 - equity / peak_equity if peak_equity > 0 else 0.0
        day_pnl = equity / day_start_equity - 1 if day_start_equity > 0 else 0.0

        numeric = {
            "px": px, "close": last.c, "atr": a, "atr_pct": a / px, "trend_z": trend_z,
            "ret_1": ret(1), "ret_6": ret(6), "ret_24": ret(24), "rv": rv, "rv_pctile": rv_pct,
            "range_pos": range_pos, "vol_z": vol_z, "inventory": inv, "upnl_r": upnl_r,
            "drawdown": dd, "day_pnl": day_pnl, "equity": equity,
            "spread_bps": None, "imbalance": None, "funding_apr": None, "premium_bps": None,
        }
        data_ts = last.t_close
        state: dict = {
            "sym": symbol,
            "t": datetime.fromtimestamp(decision_ts / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
            "trend": trend_label(trend_z),
            "ret%": {"1b": _r(ret(1) * 100), "6b": _r(ret(6) * 100), "24b": _r(ret(24) * 100)},
            "vol": vol_label(rv_pct),
            "rv%": round(rv * 100),
            "atr%": _r(a / px * 100),
            "range24": bucket(range_pos, [0.15, 0.4, 0.6, 0.85], ["at_low", "lower", "middle", "upper", "at_high"]),
            "volume": bucket(vol_z, [-1, 1, 2.5], ["light", "normal", "heavy", "climactic"]),
        }
        if book is not None and book.bids and book.asks:
            spread_bps = (book.asks[0].px - book.bids[0].px) / book.mid * 1e4
            bd = sum(l.sz for l in book.bids[:10])
            ad = sum(l.sz for l in book.asks[:10])
            imb = (bd - ad) / (bd + ad) if bd + ad > 0 else 0.0
            numeric.update(spread_bps=spread_bps, imbalance=imb)
            state["book"] = {"spread_bps": _r(spread_bps), "imbalance": imbalance_label(imb)}
            data_ts = max(data_ts, book.ts)
        if ctx is not None:
            funding_apr = ctx.funding * HOURS_PER_YEAR  # Hyperliquid funding is hourly
            premium_bps = ctx.premium * 1e4
            numeric.update(funding_apr=funding_apr, premium_bps=premium_bps)
            state["perp"] = {"funding": funding_label(funding_apr), "funding_apr%": round(funding_apr * 100),
                             "premium_bps": _r(premium_bps)}
            data_ts = max(data_ts, ctx.ts)
        if anchor:
            state["btc"] = anchor
        state["pos"] = {
            "side": "flat" if not pos or not pos.qty else ("long" if pos.qty > 0 else "short"),
            "inv%": _r(inv * 100), "upnl_R": _r(upnl_r), "bars": pos.bars_held if pos else 0,
        }
        state["acct"] = {"dd%": _r(dd * 100), "day%": _r(day_pnl * 100)}

        snap = Snapshot(symbol, decision_ts, data_ts, state, numeric)
        if snap.tokens > self.token_budget:
            raise ValueError(f"{symbol}: snapshot {snap.tokens} tokens > budget {self.token_budget}")
        return snap

    @staticmethod
    def anchor_summary(snap: Snapshot) -> dict:
        """Tiny BTC context attached to every alt snapshot (regime anchor)."""
        return {"trend": snap.jev_state["trend"], "vol": snap.jev_state["vol"], "24b%": snap.jev_state["ret%"]["24b"]}


def _r(x: float) -> float:
    return round(x, 2)
