# Phase 5 · Review

**Gate:** `trader approve 5-review --by <operator>`

An adversarial pass over the build. It covers what was checked, what broke, and what was fixed, plus what is
accepted as a known limitation.

## Defects found during the build (all fixed, each with a regression test or live verification)
| # | Defect | Severity | How it was found | Fix |
|---|---|---|---|---|
| D1 | The live loop stamped the decision time *before* fetching data, so the order book looked like it came from the future | high (a causality bug in the live path) | the `LookaheadError` guard fired during the first live paper smoke run | books are stamped with receipt time (`data/hyperliquid.py::l2_book`); the decision time is taken after the fetch (`loop.py::run`) |
| D2 | The risk layer trusted the `reduce_only` flag, so a mislabeled order could *increase* exposure through a tripped kill switch | high | `tests/test_risk.py::test_reduce_only_that_increases_is_rejected` | "reducing" is now computed from quantities only (`risk.py::check`) |
| D3 | The escalation cooldown compared against 0, and `now or time.time()` treated `0` as missing | low | `tests/test_system.py::test_escalation_blocks_entries_until_resolved` | `-inf` default and an explicit `None` check (`brain.py`) |
| D4 | The funding bucket called Hyperliquid's ~11% APR baseline "longs_paying", which biased every snapshot | medium (a systematic feature bias) | reading a live snapshot | neutral band is now −5% to +20% APR (`state_engine.py::funding_label`) |
| D5 | The screener matched tokens by DefiLlama child-protocol symbol, so chain tokens picked up bridge fees (e.g. BNB via a launchpad) | medium (wrong research inputs) | inspecting the first live screen | dedupe per symbol, take the category from the largest fee stream, exclude chain/bridge/launchpad categories (`research/screener.py`) |
| D6 | One-year funding pagination hit the info endpoint's rate limit | low | the first 365-day backtest | paced pagination, longer backoff, disk cache |

## Checklist
- [x] No path from any model output to a size, price, leverage or limit (`JevDecision` has no such field, which is tested; the brain schema only allows `no_change`, `pause` or `flatten`)
- [x] `risk.py` imports neither the reflex nor the brain (AST test)
- [x] Every order, including exits, goes through `RiskManager.check`
- [x] The approved quantity can never exceed the requested one (runtime assertion)
- [x] Quarter Kelly is enforced even if config validation were bypassed (tested)
- [x] Backtest and live share `Engine.step`
- [x] Forming candles, future books and future contexts are excluded or rejected (tested)
- [x] Jev failure → abstain → no entries, exit held positions (tested)
- [x] Live orders are IOC with bounded slippage, a venue-side stop per position, and reconciliation against venue positions every tick
- [x] Secrets only via env (`.env.example`); `.env` and `state/` are git-ignored

## Evidence
- `pytest`: see `04-build.md` for the current count
- A live paper smoke test on 2026-09-23 against Hyperliquid mainnet data (BTC, ETH, HYPE, AERO): fetch in about 3s; snapshots of 123 to 142 estimated tokens; all four decisions journaled with their gate reasons
- One-year backtest numbers are in `04-build.md`

## Accepted limitations (not fixed; stated plainly)
1. **No historical order book.** Backtests have no `book` block and fill at the bar close plus slippage. Live snapshots include the book, so live and backtest inputs differ.
2. **The offline reflex is not Jev.** Its backtest results say nothing about Jev's edge. The nightly review showed it is *worse than naive* on direction (Brier 0.91 vs 0.67 over 120 days). It validates plumbing, costs and risk behavior, nothing more.
3. **Intrabar stops in backtests are assumed to hit before targets.** This is conservative for the strategy.
4. **The live broker was not exercised against a real account** in this environment (no keys). Its SDK calls were checked against `hyperliquid-python-sdk` 0.24.0 source. Run on testnet first (`HyperliquidBroker(testnet=True)`).
5. **Macro, unlock calendars, holder concentration and active users** have no free primary API wired in. They are researched manually with dated sources in `docs/RESEARCH.md`.
