# trader: a 24/7 autonomous crypto trading agent

A BRAIN/REFLEX trading system for Hyperliquid perps.

- **Brain:** Claude Opus 5.5, used for research, review and escalations. It runs slowly and off the hot path.
- **Reflex:** Jev, TypeSafe's System One model. It makes one typed judgment per symbol per candle, in about 81 ms.
- **Code:** owns every threshold, size, limit and side effect. Jev only judges.

> **Status:** built, tested (`pytest`: 72 passed), backtested on a year of real data, and smoke-run live in paper
> mode. **It has not been run with a real Jev key or real money.** Paper is the default, and live mode is gated
> three times. See the go-live checklist in [`docs/agenkit/06-ship.md`](docs/agenkit/06-ship.md).

## How it decides (every candle)
```
Hyperliquid data ─► StateEngine ─► snapshot (<400 tokens, causal, numbers → named buckets)
                                     │
                                     ├─► Jev: ONE call, five questions evaluated in parallel
                                     │     regime        choice   trending | mean_reverting | high_vol | crisis
                                     │     direction     choice   long | short | neutral
                                     │     toxic_flow    noul     P(flow is toxic)
                                     │     setup_quality score    0..3
                                     │     risk_state    choice   safe | near_limit | reduce
                                     ▼
Policy (code):  fire only if setup_quality ≥ 2 AND direction confidence > 0.80 AND risk_state == safe
                (and regime ≠ crisis, toxic_flow < 0.5)
                size = ¼ × Kelly(calibrated p, b = net payoff of a 2·ATR stop / 3·ATR target bracket)
Risk (code):    before EVERY order: armed kill switch, 15% max drawdown, 3% daily loss, 20% per-position,
                1× gross, stale data, fat-finger, rate limit. Shrinks or rejects; never enlarges.
Escalation:     Jev confidence < 0.60 or regime == crisis → Opus 5.5 re-reads the case. It can only
                answer no_change | pause | flatten. The symbol takes no entries until it answers.
Nightly:        review every fill and every miss, Brier + reliability, refit the calibration map,
                propose schema rewrites → operator approves → live before the 00:00 UTC rollover.
```

## Quick start
```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,brain]'
pytest -q                                           # 72 tests
trader research                                     # live regime + fundamental screen (primary sources)
trader compile                                      # research/finalists.json → schemas/<SYM>/v###.json
trader backtest --days 365 --symbols BTC,ETH,SOL    # offline reflex, costs + funding included
export TYPESAFE_API_KEY=...                         # from console.typesafe.ai
trader arm && trader run                            # paper trading, 24/7
trader review                                       # nightly review (scripts/nightly.sh schedules it)
```

## Repository map
| Path | What |
|---|---|
| `docs/agenkit/01-spec.md` … `06-ship.md` | the six phases (spec, architecture, plan, build, review, ship) with approval gates |
| `docs/RESEARCH.md` | market regime, finalists, theses, catalysts, bear cases, invalidation, sources |
| `research/finalists.json` | the machine-readable finalists compiled into Jev schemas |
| `research/snapshots/<date>.json` | raw regime + screen output, with a source URL and fetch time on every value |
| `schemas/<SYM>/v###.json` | compiled Jev schemas (questions + calibration map); `ACTIVE` points to the live version |
| `config/default.toml` | every limit and threshold; validation refuses looser-than-spec values |
| `src/trader/state_engine.py` | deterministic causal snapshot |
| `src/trader/jev/` | schema compiler, HTTP client for `POST api.typesafe.ai/v1/systemone`, typed decision, offline stand-in |
| `src/trader/policy.py` | gates + fractional Kelly + exits |
| `src/trader/risk.py` | the hard risk layer and kill switch |
| `src/trader/brain.py` | Opus 5.5 escalation desk (risk-reducing verdicts only) |
| `src/trader/engine.py` | the single decision step shared by live and backtest |
| `src/trader/loop.py` | 24/7 bar-aligned runner with between-bar risk ticks |
| `src/trader/review/` | nightly review, Brier, calibration refit, schema rewrite proposals |
| `src/trader/research/` | regime + screener from DefiLlama, Hyperliquid, CoinPaprika, alternative.me |
| `scripts/` | systemd units, nightly job, heartbeat watchdog |

RESULTS_PLACEHOLDER
