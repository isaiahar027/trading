# Phase 3 · Plan (six phases, exact file paths)

**Gate:** `trader approve 3-plan --by <operator>`

This is the build plan the AgenKit `/agenkit` workflow drives (spec → architecture → plan → test-first
build → review → ship), with an approval gate at each phase. Gates are recorded in
`state/approvals.json` by `trader approve <gate>`. Live mode refuses to start unless all six are approved
(`src/trader/approvals.py`, enforced in `src/trader/cli.py::cmd_run`).

> **About AgenKit itself.** AgenKit (agenkit.xyz) is a paid, license-keyed kit (`npx agenkit activate <key>`,
> `npx agenkit install engineering-kit`, then `/agenkit` in Claude Code). No license was available in this
> build environment, so the kit's agents were not installed. This repo instead ships the same six-phase
> structure as documents plus enforced gates. To drive future changes through the real kit, install it
> and point `/agenkit` at `docs/agenkit/01-spec.md`.

| # | Phase | Deliverables (paths) | Exit criteria | Gate |
|---|---|---|---|---|
| 1 | **Spec** | `docs/agenkit/01-spec.md` | requirements R1 to R12 traceable to files | `1-spec` |
| 2 | **Architecture** | `docs/agenkit/02-architecture.md` | module map, trust boundaries, failure table | `2-architecture` |
| 3 | **Plan** | `docs/agenkit/03-plan.md` (this file) | every requirement has a file and a test | `3-plan` |
| 4 | **Test-first build** | see the table below | `pytest` green; backtest and paper smoke run | `4-build` |
| 5 | **Review** | `docs/agenkit/05-review.md` | adversarial review done, findings fixed or accepted | `5-review` |
| 6 | **Ship** | `docs/agenkit/06-ship.md`, `scripts/*` | paper soak ≥ 14 days, calibration evidence, go-live checklist | `6-ship` |

## Phase 4 build order (tests written with or before each unit)
| Step | Source file | Test file | What the test proves |
|---|---|---|---|
| 4.1 | `src/trader/config.py`, `config/default.toml` | `tests/test_risk.py::test_config_refuses_looser_limits` | config cannot loosen DD>15%, Kelly>¼, conf<0.80, setup<2 |
| 4.2 | `src/trader/market.py` | `tests/test_accounting.py` | PnL, fees, flips, book-walk fills |
| 4.3 | `src/trader/state_engine.py` | `tests/test_state_engine.py` | <400 tokens; future data excluded; forming candle excluded; future book/ctx raises |
| 4.4 | `src/trader/jev/schema.py`, `decision.py`, `client.py` | `tests/test_jev.py` | wire format matches docs.typesafe.ai; retries 429/529; fails closed; malformed rejected |
| 4.5 | `src/trader/jev/offline.py` | `tests/test_jev.py::test_offline_reflex_types` | same typed output as Jev |
| 4.6 | `src/trader/policy.py` | `tests/test_policy.py` | every gate blocks; Kelly math; ¼ cap survives a config bypass; exits; the decision object has no size field |
| 4.7 | `src/trader/risk.py` | `tests/test_risk.py` | unarmed blocks; tripped allows exits only; DD trips kill; daily loss; caps clip, never enlarge; stale data; fat finger; rate limit; module never imports the model |
| 4.8 | `src/trader/brain.py` | `tests/test_system.py::test_brain_*`, `test_escalation_*` | fail-closed pause; pending escalation blocks entries |
| 4.9 | `src/trader/engine.py`, `execution/*` | `tests/test_system.py` | kill file flattens; protective stop placed and cancelled |
| 4.10 | `src/trader/backtest.py` | `tests/test_system.py::test_risk_layer_survives_an_overconfident_model` | always-long, 0.95-confident reflex in a crash: max DD < 17% (15% limit plus one bar of gap) |
| 4.11 | `src/trader/review/*` | `tests/test_review.py` | Brier; monotone calibration; bracket labels; rewrites must keep shape |
| 4.12 | `src/trader/research/*` | live run: `trader research` → `research/snapshots/<date>.json` | every value has a URL and fetch time |
| 4.13 | `src/trader/loop.py`, `cli.py` | live paper smoke (see `05-review.md`) | fetch → snapshot → decide → journal on real data |

## Phase 6 ship sequence
1. `trader research`, then write `research/finalists.json`, then `trader compile`.
2. Paper soak with the real Jev reflex: `trader arm && trader run` (paper), for at least 14 days.
3. Nightly: `scripts/nightly.sh` runs `trader review` and writes `state/reviews/<day>.json` and `schemas/pending/*`.
4. Operator reads the reviews and approves schema versions (`trader schema-approve ...`).
5. Go-live checklist in `06-ship.md`, then `trader approve 6-ship`, set `run.mode = "live"`, and start with reduced `max_position_frac`.
