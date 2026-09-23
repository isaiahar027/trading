# Phase 1 · Spec

**Gate:** `trader approve 1-spec --by <operator>`

## Mission
A 24/7 autonomous crypto trading agent that researches the market, finds asymmetric setups, and executes
them automatically. The system owns research, code, risk and the live loop. The operator only approves gates.

## The split (never blurred)
| Layer | Who | Cadence | Owns |
|---|---|---|---|
| **BRAIN** | Claude Opus 5.5 (`claude-opus-5-5`) | minutes to hours, off the hot path | research theses, strategy, code, nightly review, escalation re-reads |
| **REFLEX** | Jev (TypeSafe System One, `jev-1.13.0` pinned) | every candle, one call per symbol | typed judgments: regime, direction, toxic flow, setup quality, risk state |
| **CODE** | this repo | every tick | every threshold, size, limit and side effect |

Jev only judges. It never sees a size, a price target, or a limit, and nothing it returns can carry one.

## Functional requirements
| ID | Requirement | Where it lives |
|---|---|---|
| R1 | Market regime from BTC/ETH trend, dominance, stablecoin liquidity, funding, OI, sentiment, macro | `src/trader/research/regime.py` |
| R2 | Screen for 5 to 10 asymmetric setups: supply, revenue, fees, TVL, value accrual | `src/trader/research/screener.py` |
| R3 | Per finalist: confirmed vs speculative catalysts, an aggressive bear case, invalidation | `research/finalists.json`, `docs/RESEARCH.md` |
| R4 | Compile each finalist into a typed Jev schema: `regime` choice{trending, mean_reverting, high_vol, crisis}, `direction` choice{long, short, neutral}, `toxic_flow` noul, `setup_quality` score 0 to 3, `risk_state` choice{safe, near_limit, reduce}. All five in ONE call | `src/trader/jev/schema.py` |
| R5 | Fire only when setup_quality ≥ 2 AND direction confidence > 0.80 AND risk_state == safe | `src/trader/policy.py::entry_gates` |
| R6 | Size with fractional Kelly on Jev's calibrated probability, capped at quarter Kelly | `src/trader/policy.py::decide`, `config.KELLY_HARD_CAP` |
| R7 | Deterministic state engine: one compact numeric snapshot under 400 tokens per bar, strictly causal timestamps; mid, spread, imbalance, realized vol, inventory, drawdown computed in code | `src/trader/state_engine.py` |
| R8 | The snapshot is the only thing Jev sees | `engine.py` passes `snap.jev_state` only |
| R9 | Hard risk layer: max drawdown 15%, max position, max daily loss, armed kill switch, checked before EVERY order | `src/trader/risk.py` |
| R10 | Escalate to Opus 5.5 when Jev confidence < 0.60 or regime flips to crisis | `policy.should_escalate`, `brain.EscalationDesk` |
| R11 | Nightly: review every fill and every miss, Brier calibration, rewrite the schema, ship before the next session | `src/trader/review/nightly.py`, `scripts/` |
| R12 | Cite live primary sources with dates; never invent metrics; label estimates | `research/sources.py`, `docs/RESEARCH.md` |

## Non-functional requirements
- **Fail closed.** No data means no decision. No reflex means abstain, which blocks entries and exits held positions. No brain means pause.
- **Survival over profit.** Any limit breach resolves toward less exposure, never more.
- **One code path.** The backtester and the live loop call the same `Engine.step`.
- **Everything journaled.** If it isn't in `state/journal/*.jsonl`, it didn't happen.
- **Paper by default.** Live trading needs `run.mode = "live"`, all six gates approved, and `TRADER_LIVE_CONFIRM=I_ACCEPT_REAL_LOSSES`.

## Non-goals
- High-frequency market making. The reflex fires per candle (1h by default), not per block. See "WHAT COULD I BE WRONG ABOUT?" in the README.
- Spot DEX execution, leverage above 1x gross, or any venue other than Hyperliquid perps.
- Letting any model change a risk limit. Ever.

## Acceptance criteria
1. `pytest` is green, including the tests that prove the risk layer survives an always-long, always-confident reflex in a crash.
2. `trader backtest` runs a year of real Hyperliquid data through the live code path, costs and funding included.
3. `trader run --offline-reflex` completes live fetch, snapshot, decide and journal cycles against real market data in paper mode.
4. `trader review` produces Brier, reliability and gate-miss accounting, and writes a pending schema version.
