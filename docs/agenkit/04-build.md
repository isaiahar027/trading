# Phase 4 · Test-first build

**Gate:** `trader approve 4-build --by <operator>`

## Tests
```bash
pip install -e '.[dev]' && pytest -q      # 72 passed (2026-09-23)
```
Each test is mapped to its requirement in [`03-plan.md`](03-plan.md). The most important ones:
- `test_risk_layer_survives_an_overconfident_model`: a fake reflex that is always long at 0.95 confidence, run through a crash. The hard limits hold max drawdown under 17% (the 15% limit plus one bar of gap).
- `test_risk_and_policy_do_not_import_the_model`: an AST check that `risk.py` cannot see Jev or the brain.
- `test_kelly_fraction_hard_capped_even_if_config_bypassed`: quarter Kelly holds even when config validation is bypassed.
- `test_future_candles_are_excluded` / `test_book_from_future_raises`: causality.
- `test_request_wire_format`: the request body matches the TypeSafe `/v1/systemone` API reference.

## Backtest evidence (offline heuristic reflex, NOT Jev)
`trader backtest --days 365 --symbols BTC,ETH,SOL,HYPE,AERO,UNI`, run 2026-09-23 on real Hyperliquid 1h candles
and hourly funding:

| Metric | Value |
|---|---|
| History actually available | ~5,000 1h bars per symbol (~208 days). Hyperliquid's `candleSnapshot` only serves the most recent 5,000 candles, so "365 days" is capped |
| Return | **−2.10%** |
| Max drawdown | 5.45% |
| Sharpe | −0.73 |
| Trades | 34 (38% win rate) |
| Fees / funding paid | $61.53 / $14.53 on $10k |
| Kill switch | not triggered |

120-day BTC+ETH run: −1.88%, max DD 2.72%, 21 trades. The nightly review on that journal showed the heuristic's
direction Brier score at 0.91–0.95, **worse than the naive 0.667**.

**Reading:** the plumbing, costs, funding and risk limits behave as designed, and losses stay small and bounded.
The heuristic stand-in has no edge. That is expected; it exists to exercise the pipeline, not to make money.
**Whether Jev adds edge is an open, empirical question.** Answering it takes a paper soak with a real key
(`06-ship.md`). Nothing in this repo claims Jev is profitable.

To backtest Jev itself, run `trader backtest --reflex jev`. It costs one API call per symbol per bar
(about 5,000 × symbols calls). Note that backtest snapshots lack the `book` block.

## Live paper smoke (2026-09-23 20:34 UTC, Hyperliquid mainnet data, offline reflex)
BTC, ETH, HYPE and AERO: fetch took ~3s, snapshots were 123–142 estimated tokens, and all four decisions were `no_trade` with
their gate reasons journaled (for example `setup_quality 0.50 < 2.0; direction neutral`). A between-bar risk tick ran cleanly.
