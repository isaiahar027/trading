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

## Current research (2026-09-23), detail in [`docs/RESEARCH.md`](docs/RESEARCH.md)
**Regime:** rule-based `risk_on`: BTC and ETH are above their 200d EMA and stablecoin supply is +1.1% over 30d.
The macro backdrop is hostile: a Fed hike on Sep 16, the next FOMC on Oct 27–28, and a rally driven by a short squeeze.
Limits stay at the defaults.

| Finalist | Bias | Core reason | Invalidation |
|---|---|---|---|
| PUMP | long | 50% of revenue to buy-and-burn; buybacks ≈ 2–3× monthly unlocks | buybacks ≪ $1M/day for 2+ weeks; adverse RICO ruling |
| ENA | short | ~14% of circulating unlocks Oct 5 (derived); no live accrual | little unlocked supply reaches exchanges; USDe → $6B+ |
| HYPE | neutral | best accrual, but at an all-time high on record OI | fees −30% with OI < $12B |
| UNI | neutral | real catalysts (CME Oct 19), +49% in a week | back below ~$6.7 after the CME launch |
| AERO | neutral | cheap on fees, but tokenized-stock-driven spike; 11% emissions | fees revert; merger slips |
| AAVE | neutral | buybacks live (DefiLlama shows 0); exploit hangover | new exploit; buyback pause |

Watchlist, not traded: JUP (unclear accrual) and ASTER (untrustworthy fee data). "Neutral" means the thesis is not
a tie-breaker; the reflex can still trade the symbol both ways under the same gates.

## Evidence so far
- `pytest`: 72 passed. Tests cover causality, gates, Kelly cap, every risk limit, fail-closed paths, and an
  always-long 0.95-confident reflex through a crash (max DD stays under 17%, against the 15% limit plus one bar of gap).
- Backtest with the **offline heuristic, not Jev**, on BTC, ETH, SOL, HYPE, AERO and UNI: ~208 days of 1h data
  (Hyperliquid serves only the last 5,000 candles). Return −2.10%, max DD 5.45%, 34 trades, fees and funding included.
  The heuristic has no edge, and the nightly review says so (direction Brier 0.91 vs naive 0.667).
- Live paper smoke test on mainnet data: fetch → snapshot (123–142 tokens) → decide → journal, end to end.
- Six bugs were found and fixed during the build, including a live-loop causality bug and a risk-layer `reduce_only`
  bypass. See [`docs/agenkit/05-review.md`](docs/agenkit/05-review.md).

## Final check
| Question | Honest answer |
|---|---|
| Is the edge organic or incentive-driven? | Mixed. PUMP and HYPE fees are organic but cyclical (memecoin and leverage). AERO's fee spike is event-driven, and ASTER's fees are suspect, so it was excluded. |
| Is the catalyst priced in? | HYPE and UNI: largely. AERO: partly. ENA's unlock has been public for a month. PUMP: not obviously, but past buybacks didn't lift the price either. |
| Does value accrue to the token? | PUMP, HYPE and AERO: yes, verified in secondary sources. AAVE: yes, understated by DefiLlama. UNI: partially. ENA: no. JUP: doubtful. |
| Will it survive costs and slippage? | Unknown for Jev. The offline stand-in does **not**: it lost 2.1% after $76 of fees and funding on $10k. Kelly is computed net of round-trip costs, so marginal setups size to zero. |
| Is any hard limit delegated to a model? | **No.** Jev returns judgments only (no size fields, tested). The brain can only say `no_change`, `pause` or `flatten`. `risk.py` cannot import either model (AST test). Config refuses looser limits. |

## WHAT COULD I BE WRONG ABOUT?
1. **Jev may have no edge here at all.** Nothing in this repo shows that it does. The one reflex I could actually test
   (the heuristic) was worse than a coin flip on direction. Treat Jev as a hypothesis until 14+ days of paper
   trading beat the naive Brier score *and* make money after costs.
2. **"Every block, 81ms" was the wrong target, and I didn't build it.** The spec asked for a snapshot on every block;
   I built per-candle (1h) decisions. At block cadence, a model call per decision loses to co-located market makers,
   and fees would dominate. If the edge exists, it is more likely in multi-hour judgment than microstructure. I could
   be wrong about that, but the shorter the horizon, the more a cost model I haven't validated decides the P&L.
3. **Calibration drift.** Jev's probabilities are calibrated on *its* training distribution, not on crypto bracket
   outcomes. The gates (>0.80 confidence) may almost never fire, or may fire on exactly the wrong regimes. The
   calibration map starts by shrinking p halfway to 0.5 for this reason. That means it may trade too little for weeks.
4. **Snapshot bucket edges are my choices** (trend z-scores, funding bands, vol percentiles), not fitted values.
   Jev judges the words I give it. If the buckets are badly placed, the model reasons correctly about the wrong inputs.
5. **The research leans on secondary sources.** Several numbers (ENA's 1.41B unlock, AERO emissions, JUP accrual) come
   from news and AI-summary sites and are labeled as such. ENA's short thesis rests on a derived number. An OTC
   buyout I can't see could mean far less supply hits the market.
6. **Backtests understate reality.** They have no historical order book and only ~208 days of history (one regime,
   mostly up). Stops fill at the stop price plus 3bps. A real liquidation cascade gaps through that, which is why
   the crash test allows 17%.
7. **Correlation.** Six alts plus BTC and ETH are one trade in a crash. The 1× gross cap bounds it, but per-position
   limits don't diversify anything when correlations go to 1.
8. **Operational risk dominates early.** The live broker has never touched a real account. The first real losses
   are more likely to come from API or key mistakes, a precision rejection, or a stop order that didn't rest than from
   a bad thesis. Run testnet first and do the kill drill (`06-ship.md`).
9. **The nightly loop can overfit.** A daily calibration refit on a small sample can chase noise. That's why it needs
   ≥20 samples per bin, blends toward the prior, enforces monotonicity, and sends wording changes to a human.

**Priority, restated: a system that survives beats one that looks profitable.** Every ambiguous path in this
code resolves toward less exposure.
