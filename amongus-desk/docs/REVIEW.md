# Code review: `AmongUs_TradingDesk_System` prototype

Reviewed files: `HANDOVER.txt`, `app.py`, `agent_brain.py`, `live_agents.py`, `terminal_map.py`,
`action_vault.py` (28 KB, 6 files). Everything under "Verified defects" was checked against source code:
the uploaded files themselves, the installed `pyautogui 0.9.54` source and the Anthropic Python SDK.
Opinions are labelled as opinions.

**Bottom line.** The prototype cannot place a trade as delivered: one file does not parse and the order
path raises on every call. That turns out to be lucky, because the design around the click has no
safety net. Nothing verifies which window or account receives the click, nothing confirms a fill, there
is no stop-loss, the LLM chooses the position size, and the dashboard is open to the internet without a
password. The ideas worth keeping (local scanning, LLM only on triggers, Pydantic at boundaries, a
phone dashboard with a panic button, demo first) are kept in the rebuild in this directory.

---

## 1. Verified defects (blocking)

| # | File | Defect | Evidence | Effect |
|---|------|--------|----------|--------|
| 1 | `app.py:8` | `logs_output = "` + newline + `".join(...)`: a raw newline inside a string literal | `ast.parse` fails: *unterminated string literal (detected at line 8)* | The dashboard never starts (`SyntaxError`). |
| 2 | `action_vault.py` | `pyautogui.doubleClick(panel.lot_input, panel.lot_input)` and `pyautogui.click(target_button, target_button)` pass a tuple as **both** `x` and `y` | pyautogui 0.9.54 `_normalizeXYArgs`: *"When passing a sequence for firstArg, secondArg must not be passed"* raises `PyAutoGUIException`. `click` and `doubleClick` both call it. | Every order attempt raises before the lot is typed. |
| 3 | `live_agents.py` | `json.loads(response.content.text)` | In the Anthropic SDK, `Message.content` is a **list** of content blocks; `.text` lives on each `TextBlock`. | `AttributeError` on every call, swallowed by `except Exception`, so the engine never trades and only prints a vague error. |
| 4 | `live_agents.py` | `model="claude-3-5-sonnet-20241022"` | Anthropic's model list shows Claude Sonnet 3.5 (`claude-3-5-sonnet-20241022`) **retired on Oct 28, 2025**. | Every request fails with "model not found". |
| 5 | `live_agents.py` | `Anthropic(api_key=os.environ.get(..., "MOCK_KEY_FOR_COMPILATION"))` | A missing key silently becomes an invalid key. | Misconfiguration shows up as runtime auth errors, not a clear startup failure. |
| 6 | `action_vault.py` | `snap_and_lock_terminal()` is never called before `execute_market_order()` | No caller anywhere in the bundle. | Clicks land on whatever window happens to be under the cursor. |
| 7 | `live_agents.py`, `action_vault.py` | Import-time side effects: `vault = UniTraderActionVault()` and `import pyautogui` run on import | `pyautogui` needs a display. | Importing the "brain" on a headless machine crashes it, and the logic can't be unit-tested. |
| 8 | `HANDOVER.txt` vs. code | The handover describes a 24/7 local scanning loop that detects volatility breakouts | No scanner, market-data source or loop exists in the bundle. `process_live_signal` takes a free-text `context_summary` from nowhere. | The "free local scanning" part of the cost strategy is not implemented. |

## 2. Safety and correctness gaps (would lose money once 1-3 are fixed)

**The LLM decides direction *and* size, unbounded.** `ClaudeTradeResponse.lot_size: float` has no
limit, and `action: str` is not even constrained to BUY/SELL/HOLD. A single malformed, prompt-injected
or overconfident reply can send any size to the terminal. Setting `temperature=0.0` makes output more
repeatable, not more correct. (Current Claude models no longer accept `temperature` at all.)

**No stop-loss, no exits, no position tracking.** Orders are fire-and-forget. Nothing records that a
position exists, closes it, or limits daily loss, trade count, exposure or loss streaks.

**Blind clicking.** Before the click, nothing proves:
- that the click lands on UniTrader (`getWindowsWithTitle('UniTrader')` is a substring match, `windows[0]` is arbitrary,
  and a browser tab titled "UniTrader review" would match);
- that the right **account** is showing (demo vs. live);
- that the panel under `(75, 260)` really is EURUSD (reordered or covered panels would trade the wrong instrument);
- that nothing covers the button (Windows update prompt, broker announcement, "connection lost" dialog);
- that the lot field contains the intended value.

After the click, nothing checks that the order executed, was rejected, or opened a confirmation dialog.

**Lot entry is fragile.**
- `pyautogui.write(str(lot_size))` writes Python's float repr: `0.1 + 0.2` becomes `0.30000000000000004`.
- Double-clicking a numeric field selects one "word". In many edit controls `.` is a word boundary, so
  part of the old value survives. This depends on the control; the rebuild never relies on it and reads
  the value back instead.

**Window handling assumptions.**
- `resizeTo(1920, 1080)` can re-flow the terminal's layout, which invalidates every coordinate. It also
  fails on a VPS with a smaller desktop.
- The coordinates treat the window origin as the client origin, ignoring the title bar and borders.
- Display scaling (125%/150%) shifts physical vs. logical pixels.
- On a Windows VPS, **disconnecting or minimizing the RDP session leaves no interactive desktop**.
  Synthetic input and screenshots then fail or return black. The prototype would keep "clicking"
  without knowing.

**`pyautogui.FAILSAFE`** (fling the mouse into a corner) is a good local brake, but on a VPS nobody is
holding the mouse. When it fires mid-order, the exception escapes half-way through the sequence. You
need a remote kill switch that persists.

**Concurrency and idempotency.** Nothing serializes GUI access, so two callers (a second thread, a
second copy of the bot) would interleave mouse and keyboard actions. Nothing persists an order before
it is sent. A crash right after the click therefore can't tell "sent" from "not sent", and an automatic
retry could double the position.

**Dashboard security.** `demo.launch(server_name="0.0.0.0", server_port=3000)` with no authentication
puts the desk on the public internet. Anyone who finds the VPS IP and port can reach it. Once a panic or
trade button is wired, that becomes a remote trading trigger.

**The "multi-agent" layer is theatre.** `SimulationEngine` maps the radio button the user picked
(BULLISH/BEARISH) straight to EXECUTE_BUY/SELL. "Red Skeptic" always says "Clean workspace checks out.
Proceeding". `crewmate_statuses` is never filled in. The panic button described in the handover is not
on the dashboard.

**Smaller issues.**
- Naive local timestamps (no timezone).
- `trend: str` and `ticker` are not validated.
- Broad `except Exception` everywhere.
- `print` instead of logs.
- No audit trail.
- `terminal_map.py` coordinates and buttons (`EMERGENCY_CLOSE_ALL_TRADES`, `FORCE_BOT_SHUTDOWN`) are not
  backed by any calibration step. **I could not verify that UniTrader has these controls or that they sit
  at those pixels. Treat them as placeholders.**

## 3. Where I agree and disagree with the strategy

| Strategy in the handover | Opinion |
|---|---|
| Local, deterministic scanning; call the LLM only on a real trigger | **Agree.** Kept: the scanner, planner, calendar and risk checks run locally for free. Claude is called only when every deterministic check has already said yes, with a daily call cap. |
| Pydantic models at the boundaries | **Agree**, but put them where it matters. The LLM boundary uses a strict JSON schema (structured outputs) plus Pydantic validation. Sizes are `Decimal`. |
| Claude as "Black Chief" choosing BUY/SELL and lot size | **Disagree.** A one-paragraph text summary gives the model no information edge, and sizing must be deterministic. In the rebuild Claude (Pink) can only **veto**. Its output schema has no direction, size or stop fields, so it *cannot* change the trade. |
| Pixel-coordinate GUI automation | **Agree it's the only path given no API**, but only with verification. Every action is gated on screen evidence matched against images *you* calibrate: window identity, client size/DPI, account badge, panel label, nothing covering the button, lot read back via the clipboard, write-ahead record, fill detection, then reconcile when unsure. |
| Snap the window to (0,0) and resize to 1920x1080 | **Disagree.** Never resize. Use client-area-relative coordinates and refuse to act if the client size or DPI differs from calibration. |
| Gradio dashboard on 0.0.0.0 | **Disagree** on exposure. The rebuild serves a FastAPI dashboard on `127.0.0.1` by default, with a bearer token, Host allow-list, CSP and auth lockout. Reach it from your phone through an SSH tunnel or Tailscale. |
| Among Us theme | **Agree, it's fun.** Each crewmate is now a real component with a real vote. |
| One-week test on a demo account | **Strongly agree.** Demo mode is enforced on screen: the desk refuses to click unless the calibrated DEMO badge is visible. |

**Not verified, and I won't guess:** UnitedPips' regulatory status, whether its terms allow automated or
GUI-driven trading, UniTrader's real contract specifications (contract size, lot step, minimum lot for
NAS100/US30), whether its one-click panel shows SL/TP fields or confirmation dialogs, and whether the
account is netting or hedging. Please check these with the broker. The config has explicit switches for
each one (`specs_verified`, `protective_orders`, `order_dialog`, `netting_account_confirmed`), and they
default to the conservative choice.

**The biggest single improvement**, if your jurisdiction allows it: a broker with a real API (MT5,
cTrader, FIX or REST). GUI automation is the most fragile link. The rebuild keeps execution behind an
`ExecutionVenue` interface so an API venue can replace the GUI venue without touching the strategy.

## 4. What the rebuild does instead

| Prototype | Rebuild (`amongus-desk/`) |
|---|---|
| `agent_brain.SimulationEngine` (user picks direction) | `strategy.BreakoutScout` (Cyan: Donchian breakout + range expansion + squeeze, no look-ahead), `strategy.ChartsPlanner` (Lime: trend filter, ATR stop, R-multiple target), `news.NewsCalendar` (Yellow: sessions, weekend, rollover, news blackouts), `risk.RiskGate` + `size_position` (Red: limits, health, Decimal sizing that never rounds up), `llm.reviewer.ClaudeReviewer` (Pink: veto-only), Chief (any EJECT skips the round) |
| `live_agents.LiveExecutionEngine` | `engine.DeskEngine`: feed → crew meeting → write-ahead intent → venue → position book → exits; startup reconciliation; watchdog |
| `terminal_map.MASTER_MAP` (hand-typed coordinates) | `desk calibrate`: an interactive wizard that captures *your* window's coordinates and reference images while *you* are logged in |
| `action_vault` | `execution.unitrader.venue.UniTraderVenue`: one GUI thread, 14 verified steps, evidence screenshots, never auto-retries, `UNCONFIRMED` + reconcile when in doubt; `PaperVenue` for paper mode |
| `app.py` Gradio | FastAPI + SSE dashboard: crew board, meeting chat, positions, estimated P&L, kill/resume, hold-to-confirm panic, reconcile prompts |
| none | SQLite state, hash-chained audit log, single-instance lock, persistent kill switch file, phone alerts (ntfy/Telegram), backtester, Windows deployment scripts |

**Login.** The desk never asks for, stores or types your UniTrader password, and never automates the
login screen. If the terminal window is missing or the badge doesn't match, `desk run` / `desk preflight`
stop and ask you to log in yourself, then re-check. Unattended, the desk halts new trades and sends a
phone alert.

## 5. Suggested plan for the one-week demo test

1. **Day 0, set-up (no trading).** Log in to the UnitedPips **demo** account yourself. Copy each
   instrument's contract size, lot step and min lot from UniTrader's symbol specification into
   `config/desk.yaml`, then set `specs_verified: true`. Run `desk calibrate`, then `desk preflight` until
   every check is green. Run `desk backtest` on a few months of 5-minute data to sanity-check that the
   strategy has a positive expectancy *before* spending demo time on it.
2. **Day 1, shadow (`execution.dry_run: true`).** Real feed, real meetings. Every pre-click check runs
   against the live terminal, but nothing is typed or clicked. Target: zero false aborts, zero wrong-panel
   detections.
3. **Days 2-6, demo trading at minimum size** (`max_lots_per_order` = the instrument's min lot,
   `max_open_positions: 1`). Each evening, compare the terminal's history with the desk's journal
   (`desk status`, dashboard) and record the real fill prices.
4. **Measure.**
   - Verification success rate (CONFIRMED vs UNCONFIRMED).
   - Aborts by stage.
   - Signal count.
   - Pink veto rate and API cost.
   - Slippage: feed price vs. terminal fill.
   - Realized R vs. backtest R.
5. **Go/no-go for live.**
   - No unexplained UNCONFIRMED orders.
   - Zero wrong-instrument or wrong-size orders.
   - Daily reconcile matches.
   - Demo expectancy is not materially worse than the backtest.
   - Broker terms confirm that automated trading is allowed.
