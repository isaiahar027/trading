# Phase 2 · Architecture

**Gate:** `trader approve 2-architecture --by <operator>`

```
                 ┌──────────────────────────── BRAIN (Opus 5.5, slow, off hot path) ───────────────────────────┐
                 │ research theses · nightly review · schema rewrite proposals · escalation re-reads           │
                 │ output constrained by JSON schema to: no_change | pause | flatten   (can never add risk)     │
                 └──────────────▲───────────────────────────────▲───────────────────────────────┬──────────────┘
                                │ escalation packet              │ journal + stats               │ pending schema
                                │ (conf<0.60 or crisis)          │                               ▼ (operator approves)
 Hyperliquid info ─► StateEngine ─► snapshot (<400 tok) ─► REFLEX: Jev, 1 call, 5 questions ─► JevDecision (judgments only)
  candles/book/ctx   (causal,           │                                                           │
                      numbers+buckets)  │ numeric features (code only)                              ▼
                                        └──────────────────────────────────────────────► POLICY (gates, quarter-Kelly, brackets)
                                                                                                    │ OrderIntent
                                                                                                    ▼
                                                     KillSwitch files ─► RISK (hard limits, before EVERY order; shrink/reject only)
                                                                                                    │ approved qty
                                                                                                    ▼
                                                                          BROKER: Paper | Hyperliquid (+ venue-side stop)
                                                                                                    │ Fill
                                                                                                    ▼
                                                                                   Portfolio  ·  Journal (append-only JSONL)
```

## Module map
| Path | Responsibility | May import |
|---|---|---|
| `src/trader/config.py` | Frozen config and validation. Refuses looser-than-spec limits | nothing internal |
| `src/trader/market.py` | Candle, OrderBook, MarketCtx, Position, OrderIntent, Fill, Portfolio accounting | nothing internal |
| `src/trader/state_engine.py` | Causal snapshot, <400 tokens, numeric → semantic buckets | market |
| `src/trader/jev/schema.py` | Five-question schema, calibration map, versioned store, pending proposals | nothing internal |
| `src/trader/jev/client.py` | `POST https://api.typesafe.ai/v1/systemone`, bounded retries, fail-closed `JevReflex` | jev.* |
| `src/trader/jev/decision.py` | Typed, validated `JevDecision`. No size or price fields | jev.schema |
| `src/trader/jev/offline.py` | Deterministic stand-in for backtests. Labeled "not Jev" everywhere | jev.* |
| `src/trader/policy.py` | Entry gates, exits, Kelly sizing, escalation trigger | config, market, jev.decision, jev.schema |
| `src/trader/risk.py` | Kill switch, drawdown, daily loss, caps, staleness, fat-finger, rate limit | config, market (**never jev or brain**, enforced by test) |
| `src/trader/brain.py` | Opus 5.5 escalation (structured output), EscalationDesk | config |
| `src/trader/engine.py` | The one decision step shared by live and backtest | all of the above |
| `src/trader/execution/paper.py` | Book-walking paper fills with fee and slippage | market |
| `src/trader/execution/hyperliquid_live.py` | Official SDK, IOC orders, reduce-only venue stop per position | market |
| `src/trader/loop.py` | 24/7 bar-aligned runner, between-bar risk ticks, reconciliation, persistence | engine |
| `src/trader/backtest.py` | Historical replay through `Engine.step`, funding charged | engine |
| `src/trader/review/*` | Labeling, Brier, reliability, monotone calibration, brain rewrite proposals | jev.schema, journal |
| `src/trader/research/*` | Regime and screener from primary sources with provenance | data |

## Key decisions
1. **Numbers become words before Jev sees them.** TypeSafe's own docs list arithmetic, numeric comparison and dates as Jev failure modes. The state engine computes every number, then maps it to a named bucket (`trend: strong_up`, `funding: longs_crowded`). Criteria refer to snapshot fields by name, in backticks, as their docs recommend.
2. **Pin `jev-1.13.0`, not `jev-latest`.** Thresholds (0.80, 0.60) and the calibration map are fitted to one model version. An alias moving under us would silently change behavior.
3. **Calibration lives in code.** Kelly uses `Calibration.apply(p)`: Jev's raw probability blended toward the empirically observed bracket win rate as evidence accumulates, with monotonicity enforced. Before evidence exists, the map shrinks p halfway toward 0.5, which is deliberately conservative.
4. **Kelly on a bracket.** Stop = 2·ATR, target = 3·ATR, so b = (3·ATR − costs)/(2·ATR + costs). f\* = p − (1−p)/b. We risk ¼·f\* of equity and size quantity = risk / stop distance. The cap is enforced twice: config validation and `min(cfg, KELLY_HARD_CAP)`.
5. **Risk shrinks, never grows.** `RiskManager.check` asserts the approved quantity never exceeds the intent. "Reducing" is computed from quantities; a `reduce_only` flag is a claim, not a proof.
6. **Kill switch is a file.** `touch state/KILL` from any shell stops new risk and flattens on the next tick (≤15s). `ARMED` must exist for any entry, so a fresh or crashed process starts disarmed.
7. **Venue-side stops in live mode.** Each entry places a reduce-only stop-market trigger on Hyperliquid. If placement fails, the kill switch trips. If the process dies, the position is still protected.
8. **The brain can only subtract.** Its output schema is `{no_change, pause, flatten}`. Its failure mode is `pause`. While an escalation is pending, the symbol takes no new entries.
9. **The nightly rewrite cannot change the contract.** Proposed schemas must keep every question id, type and option set (`same_shape`). Otherwise they are discarded. Wording changes always go to `schemas/pending/` for operator approval.

## Failure modes → behavior
| Failure | Behavior |
|---|---|
| Jev timeout, HTTP error or malformed answer | `JevDecision.abstain`: no entries, open positions exit |
| Market data error | skip the bar, journal the error. Never guess |
| Data from the future | `LookaheadError`, a bug, surfaced loudly |
| Unhandled exception in the loop | kill switch trips, process exits, supervisor restarts it disarmed |
| Position mismatch vs venue (live) | kill switch trips, flatten |
| Drawdown ≥ 15% from peak | kill switch trips, flatten all, operator must investigate and reset |
| Daily loss ≥ 3% | no new risk until the next UTC day; exits still allowed |
| Brain unavailable | escalations resolve to `pause` (4h) |
