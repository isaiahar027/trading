"""Command-line entry point: `trader <command>` (or `python -m trader <command>`)."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .approvals import GATES, Approvals
from .config import load_config
from .jev.schema import JevSchema, SchemaStore, compile_schema
from .risk import KillSwitch

FINALISTS = Path("research/finalists.json")
ANCHOR_THESIS = {
    "BTC": {"bias": "neutral", "thesis": "Regime anchor. Trade only clean trends; no fundamental view.",
            "invalidation": "n/a (no directional thesis)"},
    "ETH": {"bias": "neutral", "thesis": "Regime anchor. Trade only clean trends; no fundamental view.",
            "invalidation": "n/a (no directional thesis)"},
}


def _load_schemas(store: SchemaStore, symbols) -> dict[str, JevSchema]:
    out = {}
    for s in symbols:
        try:
            out[s] = store.active(s)
        except FileNotFoundError:
            print(f"warning: no active schema for {s}; run `trader compile`", file=sys.stderr)
    return out


def cmd_research(args, cfg):
    from .research.regime import market_regime
    from .research.screener import screen
    snap = {"regime": market_regime(), "screen": screen(top=args.top)}
    out = Path("research/snapshots") / (datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap, indent=2, default=str))
    r = snap["regime"]
    print(f"regime: {r['label']}  ({'; '.join(r['evidence'])})")
    for c in snap["screen"]["candidates"]:
        print(f"  {c['symbol']:8} score={c['score']:.2f} P/F={c['p_f']:<8} accrual={c['accrual']:<6} "
              f"fees30d_growth={c['fees_growth_30d']} circ={c['circ_ratio']}")
    print(f"wrote {out}. Write theses into {FINALISTS} (see docs/RESEARCH.md), then `trader compile`.")


def cmd_compile(args, cfg):
    store = SchemaStore()
    finalists = json.loads(FINALISTS.read_text())["finalists"] if FINALISTS.exists() else []
    theses = dict(ANCHOR_THESIS)
    for f in finalists:
        theses[f["symbol"]] = {"bias": f["bias"], "thesis": f["thesis_short"], "invalidation": f["invalidation_short"]}
    for sym, th in theses.items():
        existing = store.latest_version(sym)
        if existing and not args.force:
            print(f"{sym}: v{existing} exists (use --force to recompile as a new version)")
            continue
        schema = compile_schema(sym, th, version=existing + 1)
        p = store.save(schema, activate=True)
        print(f"{sym}: compiled {p} digest={schema.digest}")


def cmd_backtest(args, cfg):
    from .backtest import run_backtest
    from .data.hyperliquid import HyperliquidData, cache_candles, load_candles
    from .jev.offline import OfflineReflex
    hl = HyperliquidData()
    end = int(time.time() * 1000) // 3_600_000 * 3_600_000
    start = end - args.days * 86_400_000
    symbols = args.symbols.split(",") if args.symbols else list(cfg.universe.symbols)
    candles, funding = {}, {}
    for s in symbols:
        cache = Path("data/cache") / f"{s}-{cfg.run.interval}-{args.days}d-{end}.jsonl"
        if cache.exists():
            candles[s] = load_candles(cache)
        else:
            candles[s] = hl.candles(s, cfg.run.interval, start, end)
            cache_candles(cache, candles[s])
        fcache = cache.with_name(cache.stem + "-funding.json")
        if fcache.exists():
            funding[s] = [tuple(x) for x in json.loads(fcache.read_text())]
        else:
            funding[s] = hl.funding_history(s, start, end)
            fcache.write_text(json.dumps(funding[s]))
        print(f"{s}: {len(candles[s])} candles, {len(funding[s])} funding prints")
    schemas = {s: compile_schema(s, ANCHOR_THESIS.get(s, {"bias": "neutral"})) for s in symbols}
    if args.reflex == "jev":
        from .jev.client import JevClient, JevReflex
        reflex = JevReflex(JevClient(cfg.jev))
    else:
        reflex = OfflineReflex()
    res = run_backtest(cfg, schemas, candles, reflex, funding)
    print(res.summary())
    print(f"journal: {res.state_dir}/journal (run `trader review --state-dir {res.state_dir}` on it)")


def cmd_run(args, cfg):
    from .brain import Brain, EscalationDesk
    from .data.hyperliquid import HyperliquidData
    from .engine import Engine
    from .execution.paper import PaperBroker
    from .journal import Journal
    from .loop import LiveRunner, load_portfolio
    from .risk import RiskManager
    store = SchemaStore()
    schemas = _load_schemas(store, cfg.universe.symbols)
    if not schemas:
        sys.exit("no schemas; run `trader compile` first")
    if args.offline_reflex:
        from .jev.offline import OfflineReflex
        reflex = OfflineReflex()
        print("WARNING: offline heuristic reflex, not Jev")
    else:
        from .jev.client import JevClient, JevReflex
        reflex = JevReflex(JevClient(cfg.jev))
    live_broker = None
    if cfg.run.mode == "live":
        missing = Approvals(cfg.run.state_dir).missing()
        if missing:
            sys.exit(f"live mode refused: unapproved gates {missing}")
        from .execution.hyperliquid_live import HyperliquidBroker
        live_broker = broker = HyperliquidBroker(taker_fee_bps=cfg.costs.taker_fee_bps)
    else:
        broker = PaperBroker(cfg.costs)
    state_dir = Path(cfg.run.state_dir)
    portfolio = load_portfolio(state_dir / "portfolio.json", cfg.run.starting_equity)
    if live_broker:
        portfolio.cash = live_broker.account_equity()
    kill = KillSwitch(state_dir)
    now = int(time.time() * 1000)
    risk = RiskManager(cfg.risk, kill, portfolio.equity, now)
    desk = EscalationDesk(Brain(cfg.brain), cfg.policy.escalation_cooldown_s)
    engine = Engine(cfg, schemas, reflex, broker, portfolio, risk, Journal(state_dir), desk)
    print(f"mode={cfg.run.mode} symbols={list(schemas)} armed={kill.armed} tripped={kill.tripped} "
          f"brain={'on' if desk.brain.available else 'fail-closed'} equity={portfolio.equity:,.2f}")
    LiveRunner(engine, HyperliquidData(), list(schemas), live_broker=live_broker).run()


def cmd_review(args, cfg):
    from dataclasses import replace
    from .journal import Journal
    from .review.nightly import review
    if args.state_dir:
        cfg = replace(cfg, run=replace(cfg.run, state_dir=args.state_dir))
    rep = review(cfg, Journal(cfg.run.state_dir), SchemaStore(), args.day, use_brain=not args.no_brain,
                 auto_promote_calibration=args.auto_promote_calibration)
    for sym, s in rep["symbols"].items():
        print(f"{sym}: labeled={s['labeled']} brier={s.get('direction_brier')} (naive {s.get('naive_brier')}) "
              f"p_win_brier={s.get('p_win_brier')} setup={s['win_rate_by_setup']}")
        for g, v in s["gate_misses"].items():
            print(f"    gate {g:12} skipped winners={v['skipped_winners']:4} avoided losers={v['avoided_losers']:4}")
    for p in rep["proposals"]:
        print(f"proposal: {p}  (approve with `trader schema-approve {p}`)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="trader")
    ap.add_argument("--config", default="config/default.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("research", help="fetch regime + fundamental screen from primary sources")
    p.add_argument("--top", type=int, default=15)
    p = sub.add_parser("compile", help="compile finalists + anchors into Jev schemas")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("backtest")
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--symbols", default="")
    p.add_argument("--reflex", choices=["offline", "jev"], default="offline")
    p = sub.add_parser("run", help="24/7 loop (paper unless config says live)")
    p.add_argument("--offline-reflex", action="store_true", help="use the heuristic stand-in instead of Jev")
    p = sub.add_parser("review", help="nightly review: calibration, misses, schema proposals")
    p.add_argument("--day")
    p.add_argument("--state-dir")
    p.add_argument("--no-brain", action="store_true")
    p.add_argument("--auto-promote-calibration", action="store_true")
    for name in ("arm", "disarm", "kill", "reset-kill", "status"):
        p = sub.add_parser(name)
        if name == "kill":
            p.add_argument("reason", nargs="?", default="operator")
    p = sub.add_parser("approve", help=f"approve a phase gate: {', '.join(GATES)}")
    p.add_argument("gate", choices=GATES)
    p.add_argument("--by", required=True)
    p.add_argument("--note", default="")
    p = sub.add_parser("schema-approve")
    p.add_argument("pending")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    kill = KillSwitch(cfg.run.state_dir)

    handlers = {"research": cmd_research, "compile": cmd_compile, "backtest": cmd_backtest, "run": cmd_run,
                "review": cmd_review}
    if args.cmd in handlers:
        return handlers[args.cmd](args, cfg)
    if args.cmd == "arm":
        kill.arm()
    elif args.cmd == "disarm":
        kill.disarm()
    elif args.cmd == "kill":
        kill.trip(args.reason)
    elif args.cmd == "reset-kill":
        kill.reset()
    elif args.cmd == "approve":
        Approvals(cfg.run.state_dir).approve(args.gate, args.by, args.note)
    elif args.cmd == "schema-approve":
        s = SchemaStore().approve(args.pending)
        print(f"activated {s.symbol} v{s.version}")
        return
    print(json.dumps({"armed": kill.armed, "tripped": kill.tripped, "kill_reason": kill.reason(),
                      "gates_missing": Approvals(cfg.run.state_dir).missing(), "mode": cfg.run.mode}, indent=2))


if __name__ == "__main__":
    main()
