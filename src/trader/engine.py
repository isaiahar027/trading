"""One decision step, shared verbatim by the live loop and the backtester.

Order of operations each bar (never reordered):
  1. risk.update       - drawdown/daily-loss bookkeeping; may trip the kill switch
  2. flatten if killed - reduce-only orders, still risk-checked
  3. brain verdicts    - apply any finished escalations (pause / flatten only)
  4. snapshots         - deterministic, causal, < 400 tokens each
  5. reflex            - one Jev call per symbol, all symbols concurrently
  6. policy            - gates + sizing in code
  7. risk.check        - before EVERY order
  8. broker            - the only side effect
  9. journal           - everything above, append-only
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import policy as pol
from .brain import EscalationDesk
from .config import Config
from .jev.decision import JevDecision
from .jev.schema import JevSchema
from .journal import Journal
from .risk import RiskManager
from .state_engine import InsufficientData, Snapshot, StateEngine
from .market import Candle, Fill, MarketCtx, OrderBook, OrderIntent, Portfolio


@dataclass
class MarketData:
    candles: list[Candle]
    book: OrderBook | None = None
    ctx: MarketCtx | None = None


@dataclass
class StepReport:
    fills: list[Fill] = field(default_factory=list)
    actions: dict[str, str] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)


class Engine:
    def __init__(self, cfg: Config, schemas: dict[str, JevSchema], reflex, broker, portfolio: Portfolio,
                 risk: RiskManager, journal: Journal, desk: EscalationDesk | None = None, max_workers: int = 8):
        self.cfg = cfg
        self.schemas = schemas
        self.reflex = reflex
        self.broker = broker
        self.portfolio = portfolio
        self.risk = risk
        self.journal = journal
        self.desk = desk
        self.states = StateEngine(cfg.run.interval)
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="reflex")
        self.stop_oids: dict[str, int | None] = {}

    # ------------------------------------------------------------------ helpers
    def _submit(self, intent: OrderIntent, mid: float, data_ts: int, now_ms: int,
                book: OrderBook | None, report: StepReport) -> Fill | None:
        verdict = self.risk.check(intent, self.portfolio, mid, data_ts, now_ms)
        self.journal.write("risk", now_ms, symbol=intent.symbol, intent=intent, verdict=verdict)
        if not verdict.approved or verdict.qty == 0:
            return None
        fill = self.broker.execute(intent.symbol, verdict.qty, intent.ref_px, now_ms, intent.reason, book,
                                   intent.decision_id)
        if fill is None:
            return None
        was_flat = self.portfolio.position(intent.symbol).qty == 0
        self.portfolio.apply_fill(fill)
        p = self.portfolio.position(intent.symbol)
        if was_flat and p.qty != 0:
            p.stop_px, p.target_px, p.entry_prob = intent.stop_px, intent.target_px, intent.entry_prob
            p.opened_ts, p.bars_held, p.decision_id = now_ms, 0, intent.decision_id
            if hasattr(self.broker, "protect"):  # live: resting venue-side stop in case we die
                try:
                    self.stop_oids[intent.symbol] = self.broker.protect(intent.symbol, p.qty, p.stop_px)
                except Exception as e:  # an unprotected live position is not acceptable
                    self.journal.write("error", now_ms, where="protect", error=str(e))
                    self.risk.kill.trip(f"could not place protective stop for {intent.symbol}: {e}")
        elif p.qty == 0 and intent.symbol in self.stop_oids and hasattr(self.broker, "unprotect"):
            oid = self.stop_oids.pop(intent.symbol)
            if oid is not None:
                try:
                    self.broker.unprotect(intent.symbol, oid)
                except Exception as e:
                    self.journal.write("error", now_ms, where="unprotect", error=str(e))
        self.journal.write("fill", now_ms, fill=fill, equity=self.portfolio.equity)
        report.fills.append(fill)
        return fill

    def flatten(self, symbol: str, px: float, now_ms: int, reason: str, report: StepReport,
                book: OrderBook | None = None, data_ts: int | None = None) -> None:
        p = self.portfolio.positions.get(symbol)
        if p and p.qty:
            self._submit(OrderIntent(symbol, -p.qty, px, True, reason, decision_id=p.decision_id),
                         px, data_ts or now_ms, now_ms, book, report)

    def check_brackets(self, symbol: str, bar: Candle, now_ms: int, report: StepReport) -> None:
        """Intrabar stop/target using the bar's range. Stop assumed hit first (conservative)."""
        p = self.portfolio.positions.get(symbol)
        if not p or not p.qty:
            return
        if p.side > 0:
            if bar.l <= p.stop_px:
                self.flatten(symbol, min(bar.o, p.stop_px), now_ms, "exit: stop hit", report, data_ts=now_ms)
            elif bar.h >= p.target_px:
                self.flatten(symbol, max(bar.o, p.target_px), now_ms, "exit: target hit", report, data_ts=now_ms)
        else:
            if bar.h >= p.stop_px:
                self.flatten(symbol, max(bar.o, p.stop_px), now_ms, "exit: stop hit", report, data_ts=now_ms)
            elif bar.l <= p.target_px:
                self.flatten(symbol, min(bar.o, p.target_px), now_ms, "exit: target hit", report, data_ts=now_ms)

    # ------------------------------------------------------------------ the step
    def step(self, now_ms: int, market: dict[str, MarketData]) -> StepReport:
        report = StepReport()
        for sym, md in market.items():
            px = md.book.mid if md.book else (md.candles[-1].c if md.candles else 0)
            if px:
                self.portfolio.marks[sym] = px

        report.events += self.risk.update(self.portfolio, now_ms)
        if self.risk.kill.tripped:
            for sym, md in market.items():
                self.flatten(sym, self.portfolio.marks.get(sym, 0), now_ms, "exit: kill switch", report, md.book)
            self.journal.write("kill", now_ms, reason=self.risk.kill.reason(), equity=self.portfolio.equity)
            return report

        if self.desk:
            for sym, v in self.desk.collect().items():
                self.journal.write("brain_verdict", now_ms, symbol=sym, verdict=v)
                if v.action == "flatten" and sym in market:
                    self.flatten(sym, self.portfolio.marks.get(sym, 0), now_ms, "exit: brain flatten", report,
                                 market[sym].book)

        # snapshots: BTC first so alts can carry the anchor
        snaps: dict[str, Snapshot] = {}
        order = sorted(market, key=lambda s: s != "BTC")
        anchor = None
        for sym in order:
            if sym not in self.schemas:
                continue
            md = market[sym]
            try:
                snap = self.states.build(sym, now_ms, md.candles, self.portfolio, self.risk.state.peak_equity,
                                         self.risk.state.day_start_equity, md.book, md.ctx,
                                         anchor if sym != "BTC" else None)
            except InsufficientData as e:
                report.events.append(str(e))
                continue
            snaps[sym] = snap
            if sym == "BTC":
                anchor = StateEngine.anchor_summary(snap)

        futures = {s: self.pool.submit(self.reflex.decide, snap.jev_state, self.schemas[s]) for s, snap in snaps.items()}
        decisions: dict[str, JevDecision] = {s: f.result() for s, f in futures.items()}

        for sym, snap in snaps.items():
            d = decisions[sym]
            did = f"{sym}-{now_ms}"
            res = pol.decide(snap, d, self.portfolio, self.cfg.policy, self.cfg.costs,
                             self.schemas[sym].calibration, did)
            blocked = None
            if self.desk and res.escalate:
                if self.desk.maybe_escalate(sym, {"snapshot": snap.jev_state, "decision": d.to_dict(),
                                                  "thesis": self.schemas[sym].thesis,
                                                  "position": self.portfolio.positions.get(sym)}):
                    report.events.append(f"{sym}: escalated to brain")
            if res.intent and not res.intent.reduce_only and self.desk and self.desk.blocks_entry(sym):
                blocked = "entry blocked: escalation pending or symbol paused"
            self.journal.write("decision", now_ms, id=did, symbol=sym, schema_version=self.schemas[sym].version,
                               snapshot=snap.jev_state, numeric=snap.numeric, data_ts=snap.data_ts,
                               jev=d.to_dict(), action=res.action, reasons=res.reasons, p_win=res.p_win,
                               kelly=res.kelly, escalate=res.escalate, blocked=blocked)
            report.actions[sym] = blocked and "blocked" or res.action
            if res.intent and not blocked:
                md = market[sym]
                self._submit(res.intent, snap.numeric["px"], snap.data_ts, now_ms, md.book, report)

        for p in self.portfolio.positions.values():
            if p.qty:
                p.bars_held += 1
        self.journal.write("equity", now_ms, equity=self.portfolio.equity, cash=self.portfolio.cash,
                           gross=self.portfolio.gross_notional, peak=self.risk.state.peak_equity)
        return report
