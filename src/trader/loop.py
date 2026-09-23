"""24/7 live loop (paper or live execution).

Cadence:
  - at every bar close (+ a short settle delay): full Engine.step on fresh data
  - every `poll_s` seconds in between: mark-to-market, risk.update, kill-switch file,
    bracket checks against the mid, brain verdicts. No new entries between bars.
Crash policy: the loop never swallows an exception silently. Data errors skip the bar
(no decision without data); anything unexpected trips the kill switch and exits, and
the supervisor (systemd, see scripts/) restarts the process in a safe, disarmed state.
"""
from __future__ import annotations

import json
import signal
import time
import traceback
from pathlib import Path

from .engine import Engine, MarketData, StepReport
from .market import Candle, Portfolio, Position
from .state_engine import interval_ms

SETTLE_S = 3


class LiveRunner:
    def __init__(self, engine: Engine, data, symbols: list[str], poll_s: float = 15.0, live_broker=None):
        self.engine = engine
        self.data = data
        self.symbols = symbols
        self.poll_s = poll_s
        self.bar_ms = interval_ms(engine.cfg.run.interval)
        self.live_broker = live_broker
        self.stop = False
        self.heartbeat = Path(engine.cfg.run.state_dir) / "HEARTBEAT"
        self.portfolio_path = Path(engine.cfg.run.state_dir) / "portfolio.json"
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)

    def _stop(self, *_):
        self.stop = True

    def fetch(self, now_ms: int) -> dict[str, MarketData]:
        ctxs = self.data.contexts()
        out = {}
        for sym in self.symbols:
            candles = self.data.candles(sym, self.engine.cfg.run.interval, now_ms - 520 * self.bar_ms, now_ms)
            out[sym] = MarketData(candles, self.data.l2_book(sym), ctxs.get(sym))
        return out

    def reconcile(self, now_ms: int) -> None:
        """Live only: venue is the source of truth for equity and positions."""
        if self.live_broker is None:
            return
        pf = self.engine.portfolio
        venue = self.live_broker.positions()
        for sym in set(venue) | {s for s, p in pf.positions.items() if p.qty}:
            ours = pf.positions[sym].qty if sym in pf.positions else 0.0
            theirs = venue.get(sym, 0.0)
            if abs(ours - theirs) > 1e-9 + 1e-6 * abs(theirs):
                self.engine.journal.write("error", now_ms, where="reconcile", symbol=sym, ours=ours, venue=theirs)
                self.engine.risk.kill.trip(f"position mismatch on {sym}: ours={ours} venue={theirs}")
        pf.cash = self.live_broker.account_equity() - pf.unrealized

    def tick(self, now_ms: int) -> None:
        """Between bars: risk + brackets only."""
        eng = self.engine
        rep = StepReport()
        mids = {}
        for sym in self.symbols:
            book = self.data.l2_book(sym)
            mids[sym] = book.mid
            eng.portfolio.marks[sym] = book.mid
        self.reconcile(now_ms)
        eng.risk.update(eng.portfolio, now_ms)
        if eng.risk.kill.tripped:
            for sym in self.symbols:
                eng.flatten(sym, mids[sym], now_ms, "exit: kill switch", rep)
            return
        for sym, m in mids.items():
            eng.check_brackets(sym, Candle(now_ms, now_ms, m, m, m, m, 0.0), now_ms, rep)

    def accrue_funding(self, market: dict[str, MarketData], now_ms: int) -> None:
        """Paper only: charge/credit perp funding each bar (live funding is settled by the venue)."""
        pf = self.engine.portfolio
        for sym, md in market.items():
            p = pf.positions.get(sym)
            if p and p.qty and md.ctx:
                pay = p.qty * md.ctx.mark * md.ctx.funding * self.bar_ms / 3_600_000
                pf.cash -= pay
                pf.funding_paid += pay

    def run(self) -> None:
        eng = self.engine
        next_bar = (int(time.time() * 1000) // self.bar_ms + 1) * self.bar_ms
        eng.journal.write("start", int(time.time() * 1000), symbols=self.symbols, mode=eng.cfg.run.mode)
        while not self.stop:
            now_ms = int(time.time() * 1000)
            try:
                if now_ms >= next_bar + SETTLE_S * 1000:
                    self.reconcile(now_ms)
                    market = self.fetch(now_ms)
                    rep = eng.step(now_ms, market)
                    if self.live_broker is None:
                        self.accrue_funding(market, now_ms)
                    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] equity={eng.portfolio.equity:,.2f} "
                          f"actions={rep.actions} fills={len(rep.fills)} events={rep.events}", flush=True)
                    next_bar = (now_ms // self.bar_ms + 1) * self.bar_ms
                else:
                    self.tick(now_ms)
                self.heartbeat.write_text(str(now_ms))
                save_portfolio(eng.portfolio, self.portfolio_path)
            except (OSError, ValueError, KeyError) as e:  # data/network trouble: skip, never guess
                eng.journal.write("error", now_ms, where="loop", error=repr(e))
                print(f"data error, skipping: {e!r}", flush=True)
            except Exception as e:
                eng.journal.write("error", now_ms, where="loop-fatal", error=traceback.format_exc())
                eng.risk.kill.trip(f"unhandled exception: {e!r}")
                raise
            time.sleep(self.poll_s)
        eng.journal.write("stop", int(time.time() * 1000), equity=eng.portfolio.equity)


def save_portfolio(pf: Portfolio, path: Path) -> None:
    d = {"cash": pf.cash, "realized_pnl": pf.realized_pnl, "fees_paid": pf.fees_paid, "funding_paid": pf.funding_paid,
         "positions": {s: vars(p) for s, p in pf.positions.items() if p.qty}}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    tmp.replace(path)  # atomic


def load_portfolio(path: Path, starting_equity: float) -> Portfolio:
    if not path.exists():
        return Portfolio(cash=starting_equity)
    d = json.loads(path.read_text())
    pf = Portfolio(cash=d["cash"], realized_pnl=d["realized_pnl"], fees_paid=d["fees_paid"],
                   funding_paid=d["funding_paid"])
    pf.positions = {s: Position(**p) for s, p in d["positions"].items()}
    return pf
