# Phase 6 · Ship (runbook)

**Gate:** `trader approve 6-ship --by <operator>` (only after every box below is ticked)

## Install
```bash
git clone <repo> ~/trading && cd ~/trading
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,brain]'          # add ,live only on the machine that will route real orders
cp .env.example .env                   # TYPESAFE_API_KEY (console.typesafe.ai), ANTHROPIC_API_KEY
pytest -q
```

## Daily operation
| Command | Effect |
|---|---|
| `trader research` | regime + fundamental screen → `research/snapshots/<date>.json` |
| `trader compile [--force]` | `research/finalists.json` → `schemas/<SYM>/v###.json` (active) |
| `trader arm` / `trader disarm` | allow / forbid new risk (entries need `state/ARMED`) |
| `trader kill "reason"` or `touch state/KILL` | stop new risk; flatten within one tick (~15s) |
| `trader reset-kill` | clear a tripped switch (after investigating, then `arm` again) |
| `trader run` | the 24/7 loop (paper unless `run.mode = "live"`) |
| `trader review [--day YYYY-MM-DD]` | nightly review + pending schema proposals |
| `trader schema-approve schemas/pending/<file>` | activate a proposed schema version |
| `trader status` | armed / tripped / missing gates |

## Supervision (systemd, user units)
```bash
cp scripts/trader.service scripts/trader-nightly.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now trader.service trader-nightly.timer
( crontab -l; echo "* * * * * $HOME/trading/scripts/watchdog.sh" ) | crontab -
```
- The loop writes `state/HEARTBEAT` every tick. The watchdog trips the kill switch if it is older than 5 minutes.
- An unhandled exception trips the kill switch before exiting. systemd restarts the process, which then only flattens until an operator runs `reset-kill` and `arm`.
- `scripts/nightly.sh` runs at 23:30 UTC: research refresh, review, optional calibration auto-promote (`AUTO_PROMOTE_CALIBRATION=1`), then a service restart so approved schemas take effect before the next session. Crypto trades 24/7, so the "next open" is the 00:00 UTC risk-day rollover.

## Go-live checklist (all required)
- [ ] ≥ 14 days of paper trading with the **real Jev reflex** (not `--offline-reflex`)
- [ ] Nightly reviews show direction Brier **below the naive 0.667** on those days, and the reliability table's
      ≥0.80 bins are populated (≥ 20 samples) with an observed win rate above the break-even for b≈1.4 (≈ 42%)
- [ ] `p_win_brier` on actual paper trades is below 0.25
- [ ] Paper PnL is positive **after** fees and funding; if not, do not ship
- [ ] Testnet: `HyperliquidBroker(testnet=True)` round trip including `protect`/`unprotect`, verified in the UI
- [ ] API wallet (agent key) with no withdrawal rights; main key never on the box
- [ ] First two weeks live: `max_position_frac = 0.05`, `max_gross_leverage = 0.5`, account funded with money you can lose
- [ ] Kill drill: `touch state/KILL` while in a position → flat within one tick, verified on the venue

## Rollback
`trader kill "rollback"` → confirm flat on the venue → `systemctl --user stop trader` →
`echo <prev> > schemas/<SYM>/ACTIVE` → restart → `trader reset-kill && trader arm`.
