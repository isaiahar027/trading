# 🛸 Among Us Trading Desk

A multi-agent FX / index-CFD trading desk for the **UniTrader** desktop terminal (UnitedPips), which has
no API. Six "crewmates" debate every breakout in an emergency meeting. A guarded GUI-automation venue
clicks the terminal only after verifying, on screen, that it is clicking the right thing on the right
account.

> **Status.** Built for a one-week test on a **demo** account. Paper mode is the default. Trading
> involves risk of loss; nothing here is financial advice, and the strategy is unproven until you
> backtest and demo-test it. Check that your broker's terms allow automated trading.

This project is self-contained and shares no code with the odds hub at the repository root.

* Review of the original prototype and design rationale: [`docs/REVIEW.md`](docs/REVIEW.md)
* Module contracts: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
* Deploying on the Windows VPS: [`docs/WINDOWS_VPS.md`](docs/WINDOWS_VPS.md)

## You log in. The desk never does.

The desk never asks for, stores or types your UniTrader password, and never automates the login
screen. Every coordinate and reference image of the terminal comes from **your** screen through
`desk calibrate`, captured while you are logged in. Nothing about UniTrader's layout is hard-coded,
because we don't know it.

If the terminal window disappears or the account badge doesn't match the one you calibrated (logged
out, wrong account, LIVE instead of DEMO), the desk stops opening trades:
* in the console, `desk run` / `desk preflight` asks you to log in yourself, then re-checks when you
  press Enter;
* when running unattended, it sends a phone alert: "UniTrader needs you: log in to your DEMO account".

## The crew

| Crewmate | Job | Can |
|---|---|---|
| 🟦 **Cyan Scout** | Volatility breakout: Donchian channel break + range expansion + prior squeeze, no look-ahead | call the meeting |
| 🟩 **Lime Charts** | Trend filter (EMA), ATR stop, R-multiple target; rejects stops inside the spread | plan / eject |
| 🟨 **Yellow News** | Sessions, weekend, daily rollover, your scheduled news blackouts | eject |
| 🟥 **Red Skeptic** | Kill switch, reconcile/health, open positions, trades/day, daily loss, loss streak, cooldowns, spread, stale data, verified specs; sizes the trade (`Decimal`, never rounds up) | eject |
| 🩷 **Pink Sentiment** | Claude second opinion on the finished plan, only if everyone above said PROCEED | **veto only** |
| ⬛ **Black Chief** | Any EJECT ends the round; otherwise executes | decide |

Claude (Pink) cannot change direction, size, stop or target. Its structured output has no fields for
them. It is called only for trades that would otherwise execute, with a daily call cap, so the free local
loop does the scanning.

## Execution: verify, click once, never guess

`UniTraderVenue` runs all GUI work on one thread and checks, in order:
1. session unlocked;
2. exactly one UniTrader window;
3. client size and DPI equal to the calibration;
4. window in the foreground;
5. **DEMO/LIVE badge matches the mode**;
6. the panel's symbol label matches;
7. positions indicator stable;
8. lot typed and **read back through the clipboard**;
9. foreground, label and "nothing covers the button" re-checked;
10. write-ahead record;
11. **one click**;
12. confirmation/error dialogs;
13. fill detected from the positions indicator.

The result is reported honestly:
* anything wrong before the click → `ABORTED` (nothing was sent);
* anything uncertain after it → `UNCONFIRMED`, trading halts and you reconcile it on the dashboard.

Orders are never retried automatically. Before/after screenshots of every order go to `data/evidence/`.

## Quick start (paper mode, any OS)

```bash
cd amongus-desk
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
desk init-config                                    # writes config/desk.yaml (mode: paper, synthetic feed)
desk run -c config/desk.yaml                        # prints the dashboard URL + token
```

Open `http://127.0.0.1:8765`, paste the token, and watch the crew meet on synthetic data. Or backtest:

```bash
desk backtest -c config/desk.yaml --synthetic 5000
desk backtest -c config/desk.yaml --csv my_eurusd_5m.csv --symbol EURUSD
```

## Demo test on the VPS

Follow [`docs/WINDOWS_VPS.md`](docs/WINDOWS_VPS.md):
1. install;
2. log in to UniTrader yourself;
3. copy the instrument specs from UniTrader;
4. `desk calibrate`;
5. `desk preflight`;
6. a shadow day with `dry_run: true`;
7. then demo trading at minimum size.

## Configuration

`config/desk.yaml` (start from [`config/desk.example.yaml`](config/desk.example.yaml)) holds the mode,
instruments, strategy, risk, news, LLM, feed, execution, dashboard and alerts. Secrets come only from the
environment ([`.env.example`](.env.example)): `ANTHROPIC_API_KEY`, `TWELVEDATA_API_KEY`,
`DESK_DASHBOARD_TOKEN`, `DESK_NTFY_TOPIC`, `DESK_TELEGRAM_BOT_TOKEN` / `DESK_TELEGRAM_CHAT_ID`,
`DESK_LIVE_ACK`.

Unsafe combinations are rejected at start-up, for example:
* live mode without `DESK_LIVE_ACK`;
* `close_all` exits with more than one open position allowed;
* opposite-order exits without confirming a netting account;
* a public dashboard without a token;
* risk above 2% per trade.

## Commands

| Command | What it does |
|---|---|
| `desk run` | Engine + dashboard (login gate first in demo/live) |
| `desk preflight [--wait-for-login]` | Checks config, layout, window, badge and panels. Never clicks |
| `desk calibrate [--simulate]` | Interactive layout wizard (hover, don't click) |
| `desk test-order --symbol S --side BUY [--lots L]` | Round trip: one order after a typed confirmation, then close on Enter |
| `desk backtest --csv F \| --synthetic N` | Same strategy and risk code on historical bars |
| `desk kill [--reason]` / `desk resume` | Persistent kill switch (also: create `data/KILL`) |
| `desk status` | Positions, orders, reconcile items, kill state |
| `desk audit-verify` | Verifies the hash-chained audit journal |

## Development

```bash
pip install -e ".[dev]"
ruff check src tests && ruff format --check src tests
mypy
pytest
```

Tests never touch the network or a real desktop. GUI behaviour is exercised against `FakeTerminal`, a
simulated window that has nothing to do with UniTrader's real look.
