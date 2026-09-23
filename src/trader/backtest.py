"""Backtester: replays historical candles through the SAME Engine.step as live.

Differences from live, stated plainly:
  - no historical order book, so book-derived fields are absent from snapshots and fills
    are at the bar close + slippage + taker fee (no depth walk);
  - funding is charged from Hyperliquid's historical funding series when provided;
  - brackets are checked against each bar's high/low with the stop assumed hit first.
Default reflex is the offline heuristic (not Jev). Results from it say nothing about
Jev's edge; they validate plumbing, costs and risk behaviour.
"""
from __future__ import annotations

import bisect
import math
import tempfile
from dataclasses import dataclass

from .config import Config
from .engine import Engine, MarketData, StepReport
from .execution.paper import PaperBroker
from .jev.schema import JevSchema
from .journal import Journal
from .market import Candle, Portfolio
from .risk import KillSwitch, RiskManager
from .state_engine import MIN_BARS, interval_ms


@dataclass
class BacktestResult:
    equity_curve: list[tuple[int, float]]
    trades: list[dict]
    total_return: float
    max_drawdown: float
    sharpe: float
    n_trades: int
    win_rate: float
    fees: float
    funding: float
    killed: bool
    state_dir: str

    def summary(self) -> str:
        return (f"return {self.total_return:+.2%} | maxDD {self.max_drawdown:.2%} | sharpe {self.sharpe:.2f} | "
                f"trades {self.n_trades} | win {self.win_rate:.0%} | fees ${self.fees:,.2f} | "
                f"funding ${self.funding:,.2f} | killed={self.killed}")


def run_backtest(cfg: Config, schemas: dict[str, JevSchema], candles: dict[str, list[Candle]], reflex,
                 funding: dict[str, list[tuple[int, float]]] | None = None, state_dir: str | None = None) -> BacktestResult:
    state_dir = state_dir or tempfile.mkdtemp(prefix="bt-")
    funding = funding or {}
    bar = interval_ms(cfg.run.interval)
    times = sorted({c.t_open for cs in candles.values() for c in cs})
    idx = {s: {c.t_open: i for i, c in enumerate(cs)} for s, cs in candles.items()}
    fund_t = {s: [t for t, _ in f] for s, f in funding.items()}

    portfolio = Portfolio(cash=cfg.run.starting_equity)
    kill = KillSwitch(state_dir)
    kill.reset()
    kill.arm("backtest")
    start = times[0] + bar * MIN_BARS
    risk = RiskManager(cfg.risk, kill, portfolio.equity, start)
    journal = Journal(state_dir)
    engine = Engine(cfg, schemas, reflex, PaperBroker(cfg.costs), portfolio, risk, journal, desk=None)

    curve: list[tuple[int, float]] = []
    trades: list[dict] = []
    open_trades: dict[str, dict] = {}

    def record(rep: StepReport):
        for f in rep.fills:
            p = portfolio.positions.get(f.symbol)
            if f.symbol not in open_trades and p and p.qty:
                open_trades[f.symbol] = {"symbol": f.symbol, "entry_ts": f.ts, "entry_px": f.px, "qty": f.qty,
                                         "p_win": p.entry_prob, "fees": f.fee}
            elif f.symbol in open_trades and (not p or not p.qty):
                t = open_trades.pop(f.symbol)
                t.update(exit_ts=f.ts, exit_px=f.px, reason=f.reason, fees=t["fees"] + f.fee)
                t["pnl"] = t["qty"] * (f.px - t["entry_px"]) - t["fees"]
                t["win"] = t["pnl"] > 0
                trades.append(t)

    for t_open in times:
        now = t_open + bar  # the bar starting at t_open has just closed
        if now < start:
            continue
        market: dict[str, MarketData] = {}
        for sym, cs in candles.items():
            i = idx[sym].get(t_open)
            if i is None or i < MIN_BARS:
                continue
            market[sym] = MarketData(cs[max(0, i - 499):i + 1])
            # funding for the bar just elapsed (hourly rate, scaled to bar length)
            if sym in funding and portfolio.positions.get(sym) and portfolio.positions[sym].qty:
                j = bisect.bisect_right(fund_t[sym], now) - 1
                if j >= 0:
                    rate = funding[sym][j][1] * bar / 3_600_000
                    pay = portfolio.positions[sym].qty * cs[i].c * rate
                    portfolio.cash -= pay
                    portfolio.funding_paid += pay
            rep = StepReport()
            portfolio.marks[sym] = cs[i].c
            engine.check_brackets(sym, cs[i], now, rep)
            record(rep)
        if not market:
            continue
        rep = engine.step(now, market)
        record(rep)
        curve.append((now, portfolio.equity))
        if kill.tripped and not any(p.qty for p in portfolio.positions.values()):
            break

    eq = [e for _, e in curve] or [cfg.run.starting_equity]
    peak, mdd = eq[0], 0.0
    for e in eq:
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak)
    rets = [b / a - 1 for a, b in zip(eq[:-1], eq[1:])]
    bars_per_year = 365 * 86_400_000 / bar
    sd = (sum((r - sum(rets) / len(rets)) ** 2 for r in rets) / max(len(rets) - 1, 1)) ** 0.5 if rets else 0
    sharpe = (sum(rets) / len(rets)) / sd * math.sqrt(bars_per_year) if sd > 0 else 0.0
    wins = sum(t["win"] for t in trades)
    engine.pool.shutdown()
    return BacktestResult(curve, trades, eq[-1] / cfg.run.starting_equity - 1, mdd, sharpe, len(trades),
                          wins / len(trades) if trades else 0.0, portfolio.fees_paid, portfolio.funding_paid,
                          kill.tripped, state_dir)
