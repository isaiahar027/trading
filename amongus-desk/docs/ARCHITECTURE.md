# Architecture and module contracts

This is the build contract for `amongus-desk`. Shared types live in `src/amongus_desk/domain.py`,
configuration in `config.py`, and the cross-module protocols in:

| Protocol | File |
|---|---|
| `Clock` (`SystemClock`, `ManualClock`) | `clock.py` |
| `EventHub` (pub/sub for the dashboard) | `events.py` |
| `MarketDataFeed`, `FeedError` | `marketdata/base.py` |
| `ExecutionVenue` + venue invariants | `execution/base.py` |
| `GuiBackend`, `WindowInfo`, `OperatorAbortError` | `execution/unitrader/backend.py` |
| `Layout`, `LayoutFile` (calibrated screen layout) | `execution/unitrader/layout.py` |
| `TradeReviewer`, `ReviewVerdict`, `ReviewContext`, `ReviewOutcome` | `llm/base.py` |
| `Notifier`, `NullNotifier`, `AlertNotifier` (ntfy JSON + Telegram, dedup), `build_notifier` | `notify.py` |
| `quantize_lots`, `format_lots`, `parse_lots`, `estimate_pnl`, `trading_day_start` | `money.py` |

Those files are the spine. Change them only when a contract is genuinely wrong, and keep every caller
consistent when you do.

## Ground rules (non-negotiable)

1. **No invented facts about UniTrader or UnitedPips.** We have no documentation for the UniTrader
   terminal. It has no API, no CLI and no scripting. Never hard-code coordinates, labels, button names,
   dialog texts, colors, lot-field behavior or contract specs as if they were known. Everything about
   the real UI comes from the operator's own screen through `desk calibrate` (`layout.py`). Anything
   instrument-specific (contract size, lot step, min lot) comes from the operator copying UniTrader's
   symbol specification into the config. `InstrumentConfig.specs_verified` must be `true` before an
   instrument trades in demo or live mode.
2. **The operator logs in, never the desk.** The desk never asks for, stores, reads or types broker
   credentials, and never automates the login screen. When the terminal window is missing, or the
   account badge does not match the calibrated badge, the desk stops trading. It then tells the
   operator to log in themselves: interactively in the CLI (wait for Enter, then re-check) and by phone
   alert when it runs unattended.
3. **Fail closed.** Uncertainty means no trade (before the click) or `UNCONFIRMED` + reconcile (after
   the click). Never retry an order click automatically. Never infer that something filled because it
   "probably did".
4. **The LLM can only veto.** Direction, size, stop and target are deterministic. Claude's structured
   output has no fields for them.
5. **Demo first.** `mode: paper` is the default. `demo` needs the calibrated DEMO badge on screen
   before every click. `live` needs `DESK_LIVE_ACK` plus a layout calibrated on the live account. Demo
   and live on the real terminal need real market data (`feed.kind: twelvedata`); only the built-in
   simulated terminal (`backend: fake`) may run on synthetic prices.
6. **No network in tests. No real GUI in tests.** Use `FakeGuiBackend`, `httpx2.MockTransport` and fake
   Anthropic clients. Tests are deterministic (seeded, `ManualClock`).

## Data flow

```
feed (synthetic | csv | twelvedata)
  └─> engine.on_bar(bar)
        ├─ PositionBook.mark_bar(bar) ─> exits due ─> venue.close_position / mark closed
        └─ BarHistory.add(bar) ─> BreakoutScout.evaluate()  (CYAN)
               └─ signal? ─> EmergencyMeeting.convene(signal)
                     ├─ ChartsPlanner.plan()            LIME   trend filter + plan
                     ├─ NewsCalendar.check()             YELLOW sessions / weekend / rollover / news
                     ├─ size_position() + RiskGate.check() RED  limits, health, sizing
                     ├─ TradeReviewer.review()           PINK   only if every deterministic vote is PROCEED
                     └─ Chief                            BLACK  any EJECT -> SKIP_ROUND
               └─ EXECUTE_* ─> Store.add_intent(PENDING)
                               venue.submit(intent, quote, on_before_click=mark SUBMITTING)
                               CONFIRMED -> PositionBook.add_open ; UNCONFIRMED -> reconcile required
watchdog (every health_interval_seconds): venue.health(), feed staleness, desktop locked -> degraded
dashboard (FastAPI + SSE) <─ EventHub ; kill / resume / panic / reconcile / equity sync
```

## Module contracts

All modules: `from __future__ import annotations`, full type hints (mypy strict), `logging.getLogger(__name__)`,
no `print` outside `cli.py` / `calibrate.py`. Public functions have short docstrings. Tests live in
`tests/test_<module>.py` and use `pytest`. Async tests use `@pytest.mark.anyio` (the `anyio_backend`
fixture in `tests/conftest.py` pins asyncio).

### `indicators.py` — pure functions

```python
def true_ranges(bars: Sequence[Bar]) -> list[float]                     # TR[0] = high - low
def atr_wilder(bars: Sequence[Bar], period: int) -> list[float | None]  # Wilder smoothing, seed = SMA(TR[0:period])
def sma(values: Sequence[float], period: int) -> list[float | None]
def ema(values: Sequence[float], period: int) -> list[float | None]     # seeded with SMA of first `period`
def rolling_std(values: Sequence[float], period: int) -> list[float | None]  # population std
def donchian(bars: Sequence[Bar], lookback: int) -> tuple[list[float | None], list[float | None]]
    # (highest high, lowest low) of the *previous* `lookback` bars at each index (current bar excluded)
def bollinger_bandwidth(closes: Sequence[float], period: int, k: float = 2.0) -> list[float | None]
def percentile_rank(window: Sequence[float], value: float) -> float     # share of window <= value, 0..1
```
Outputs are aligned with inputs (`None` until enough data). Invalid periods raise `ValueError`.

### `strategy.py` — Cyan Scout and Lime Charts

```python
class BarHistory:
    def __init__(self, maxlen: int) -> None
    def add(self, bar: Bar) -> bool           # False (ignored) for duplicates / out-of-order / wrong timeframe
    def bars(self, symbol: str) -> list[Bar]  # oldest first
    def last(self, symbol: str) -> Bar | None

@dataclass(frozen=True) class ScoutResult: signal: BreakoutSignal | None; reason: str; metrics: dict[str, float]
class BreakoutScout:
    def __init__(self, cfg: StrategyConfig) -> None
    def evaluate(self, bars: Sequence[Bar]) -> ScoutResult   # one symbol, evaluates the LAST (just closed) bar
    def reset(self, symbol: str | None = None) -> None

@dataclass(frozen=True) class PlanResult: plan: TradePlan | None; vote: Vote; reason: str; metrics: dict[str, float]
class ChartsPlanner:
    def __init__(self, cfg: StrategyConfig) -> None
    def plan(self, signal: BreakoutSignal, bars: Sequence[Bar], instrument: InstrumentConfig,
             spread_price: float) -> PlanResult
```
Scout rules for the last bar `t` (mirror for shorts). Use only data up to `t`, and use ATR from `t-1` so
the trigger bar does not inflate its own yardstick:
- at least `cfg.warmup_bars` bars, else `signal=None, reason="warming up"`;
- `close_t > donchian_high_t + breakout_buffer_atr * ATR_{t-1}`;
- `TR_t >= min_range_atr * ATR_{t-1}` (range expansion);
- body ratio `|close-open| / range >= min_body_ratio`; close location `(close-low)/range >= min_close_location`;
- extension `(close_t - donchian_high_t) / vol <= max_extension_atr` (don't chase), where
  `vol = max(ATR_{t-1}, ATR_slow_{t-1})` (after a squeeze the fast ATR is tiny; "vol" is normal noise);
- squeeze: Bollinger bandwidth percentile at `t-1` over the previous `squeeze_lookback` values
  `<= max_squeeze_percentile` (`1.0` disables);
- cooldown: no second signal on the same symbol within `signal_cooldown_bars` bars of the last signal;
- `strength` in [0,1] combines extension, range expansion and squeeze tightness; `reasons` explain it.

Charts rules: with `require_trend_alignment`, a long needs `close > EMA(trend_ema_period)` and an EMA slope
over `trend_slope_bars` of `>= 0` (mirror for shorts); otherwise vote EJECT. Plan: `entry_ref = signal.close`,
`stop_distance = stop_atr_mult * max(signal.atr, signal.atr_slow)`, stop/target from `target_r`. EJECT if
`stop_distance < min_stop_spread_multiple * spread_price`. Round prices to `instrument.price_decimals`.

### `news.py` — Yellow News

```python
@dataclass(frozen=True) class CalendarCheck: ok: bool; reasons: list[str]; next_event: NewsEvent | None
class NewsCalendar:
    def __init__(self, cfg: NewsConfig, base_dir: Path) -> None
    def events(self) -> list[NewsEvent]        # config events + events_file (re-read when mtime changes;
                                               # a broken file keeps the last good list and is reported)
    def check(self, instrument: InstrumentConfig, now: datetime) -> CalendarCheck
    def status(self) -> dict[str, Any]
```
Blocks: outside `instrument.sessions`; weekend (Fri >= `weekend_close_utc`, all Saturday, Sun < `weekend_open_utc`);
`rollover_block`; any event whose currencies intersect `instrument.currencies` within
`[at - blackout_minutes_before, at + blackout_minutes_after]`.

### `risk.py` — Red Skeptic's math

```python
# quantize_lots / format_lots / parse_lots / estimate_pnl / trading_day_start live in money.py (spine)
def size_position(plan: TradePlan, instrument: InstrumentConfig, risk: RiskConfig, equity: float) -> SizingResult
@dataclass(frozen=True) class GateResult: ok: bool; violations: list[str]; details: dict[str, Any]
class RiskGate:
    def __init__(self, risk: RiskConfig, strategy: StrategyConfig) -> None
    def check(self, plan: TradePlan, instrument: InstrumentConfig, sizing: SizingResult, snapshot: RiskSnapshot,
              *, last_bar_end: datetime | None, spread_price: float, mode: Mode) -> GateResult
    def daily_loss_breached(self, snapshot: RiskSnapshot) -> bool
```
Sizing: `risk_amount = equity * risk_per_trade_pct / 100`;
`per_lot_risk = (stop_distance + spread_price) * contract_size * quote_to_account_rate`; lots = risk/per-lot,
rounded down to `lot_step`, capped at `min(instrument.max_lot, risk.max_lots_per_order)` and by
`max_notional_per_order`. Below `min_lot` gives `lots=None` with a reason. The size is **never rounded up**
to the minimum. Gate violations (each a readable string): kill switch, reconcile pending, degraded health,
`max_open_positions`, `max_positions_per_symbol`, `max_trades_per_day`, daily loss (realized + unrealized
<= -`max_daily_loss_pct`% of `day_start_equity`), loss streak inside `loss_streak_cooldown_minutes`,
`min_minutes_between_trades`, `symbol_cooldown_minutes_after_loss`, spread > `max_spread_pips`, stale data
(`now - last_bar_end > stale_after_bars * timeframe`), no size, instrument disabled, and (demo/live only)
`specs_verified` false.

### `killswitch.py`

```python
class KillSwitch:
    def __init__(self, data_dir: Path) -> None   # flag file: data_dir / "KILL"
    def engaged(self) -> bool                    # True if the file exists, whatever it contains (operators may
                                                 # create it by hand, e.g. from the VPS desktop); any case and
                                                 # any extension count too (KILL.txt from Windows Explorer)
    def reason(self) -> str | None
    def engage(self, reason: str, by: str) -> None    # atomic write of JSON {reason, by, at}
    def release(self, by: str) -> None           # removes KILL and every variant
    def state(self) -> dict[str, Any]
```
Thread-safe (the GUI worker thread calls `engaged()` right before clicking). Cheap: one `stat` call.

### `store.py` — SQLite (WAL), thread-safe

```python
@dataclass(frozen=True) class IntentRow: intent: OrderIntent; status: IntentStatus; report: ExecutionReport | None; updated_at: datetime
class Store:
    def __init__(self, path: Path) -> None       # creates schema; PRAGMA user_version migrations
    def close(self) -> None
    def add_intent(self, intent: OrderIntent) -> None                       # status PENDING
    def set_intent_status(self, intent_id: str, status: IntentStatus, report: ExecutionReport | None = None) -> None
    def get_intent(self, intent_id: str) -> IntentRow | None
    def intents_with_status(self, *statuses: IntentStatus) -> list[IntentRow]
    def recent_intents(self, limit: int = 50) -> list[IntentRow]
    def upsert_position(self, position: Position) -> None
    def get_position(self, position_id: str) -> Position | None
    def active_positions(self) -> list[Position]                            # OPEN, CLOSING, RECONCILE
    def closed_positions_since(self, since: datetime) -> list[Position]
    def recent_positions(self, limit: int = 50) -> list[Position]
    def add_meeting(self, meeting: MeetingRecord) -> None
    def recent_meetings(self, limit: int = 50) -> list[MeetingRecord]
    def get_kv(self, key: str) -> Any | None
    def set_kv(self, key: str, value: Any) -> None                          # JSON-serializable
```
Records are stored as `model_dump_json()` plus indexed columns. Status transitions out of a final status
raise `ValueError`, except the RECONCILED_* statuses, which are only allowed from SUBMITTING/UNCONFIRMED.

### `audit.py` — tamper-evident journal

```python
class AuditLog:
    def __init__(self, path: Path, fsync: bool = True) -> None   # resumes the chain; truncates a trailing partial line
    def append(self, type_: str, data: dict[str, Any]) -> AuditEntry
    @staticmethod
    def verify(path: Path) -> AuditVerifyResult                  # ok, entries, first_bad_seq, message
```
JSONL lines `{seq, ts, type, data, prev, hash}`, `hash = sha256(canonical_json({seq, ts, type, data, prev}))`,
canonical = `json.dumps(sort_keys=True, separators=(",", ":"), default=str)`. Thread-safe.

### `runtime.py` — process plumbing

```python
class SingleInstanceLock:   # context manager; msvcrt on Windows, fcntl elsewhere; raises AlreadyRunningError
def configure_logging(data_dir: Path, level: str = "INFO") -> None   # rotating JSON-lines file + console
```

### `marketdata/` — feeds implementing `MarketDataFeed`

* `synthetic.py`: `SyntheticFeed(cfg: FeedConfig, instruments: Sequence[InstrumentConfig], timeframe_minutes: int,
  clock: Clock, max_bars: int | None = None)`. Deterministic per seed + symbol (`zlib.crc32`, not `hash`).
  Regime-switching random walk with quiet stretches followed by expansions, so breakouts actually happen.
  `warmup` returns history ending at the start time. `stream` emits one bar per symbol per step, sleeping
  `synthetic.interval_seconds` via the clock. Quotes are close +/- half the typical spread.
* `csv_replay.py`: `load_csv_bars(path) -> dict[str, list[Bar]]` and `CsvReplayFeed(path, timeframe_minutes,
  symbols=None)`. Columns `symbol,start,open,high,low,close[,volume]`, ISO-8601 starts with an offset/`Z`
  (naive timestamps are rejected with a clear error). `warmup(n)` consumes the first n bars per symbol;
  `stream` yields the rest in global time order.
* `twelvedata.py`: `TwelveDataFeed(cfg: FeedConfig, instruments, timeframe_minutes, api_key: str, clock,
  client: httpx2.AsyncClient | None = None)`. `GET {base_url}/time_series` with `symbol=<feed_symbol>`,
  `interval=<5min|...>`, `outputsize`, `timezone=UTC`, `order=asc`, header `Authorization: apikey <key>`.
  The values are strings and `datetime` is `"YYYY-MM-DD HH:MM:SS"` (UTC because of `timezone=UTC`).
  Errors come back as JSON `{"code": int, "message": str, "status": "error"}`, possibly with HTTP 200.
  Drop the still-forming bar (`start + timeframe > now`). Poll once per bar close + `poll_delay_seconds`,
  only for symbols in session (unless `poll_outside_sessions`). Respect credit budgets (1 credit per
  symbol per request; per-minute and per-UTC-day counters; never exceed them; report in `status()`).
  401/403 raise `FeedError`; 429 waits for the next minute; other errors are logged and retried next cycle.
  `latest_quote` returns `None` (no bid/ask).

### `execution/paper.py`

`PaperVenue(instruments: Mapping[str, InstrumentConfig], cfg: PaperConfig, clock: Clock, is_halted: HaltCheck,
quote_source: Callable[[str], Quote | None])`. Fills at ask/bid (+/- slippage), falling back to
`entry_ref`/last price +/- half the typical spread. Calls `on_before_click` before "sending". Reports
`CONFIRMED` with `fill_price_source="venue"`. Halted gives `ABORTED` for opening orders only. `supports_protective_orders = True`.

### `positions.py`

```python
@dataclass(frozen=True) class ExitDue: position: Position; reason: ExitReason; price: float
class PositionBook:
    def __init__(self, cfg: DeskConfig, active: Iterable[Position] = (), history: Iterable[Position] = (), *,
                 equity_base: float | None = None, equity_base_at: datetime | None = None) -> None
    def active(self) -> list[Position]
    def get(self, position_id: str) -> Position | None
    def history(self) -> list[Position]                     # closed positions, oldest close first
    def add_open(self, intent: OrderIntent, report: ExecutionReport, *, protective: bool, at: datetime) -> Position
    def add_unmanaged(self, intent: OrderIntent, report: ExecutionReport, *, at: datetime, note: str) -> Position
        # a broker position the desk tracks but never manages (test order, recovered fill): status RECONCILE
    def mark_bar(self, bar: Bar) -> list[ExitDue]           # updates last_price/bars_held; never closes by itself;
                                                            # idempotent per bar (Position.last_marked_bar_end)
    def set_status(self, position_id: str, status: PositionStatus, *, close_intent_id: str | None = None,
                   note: str | None = None, suggested_exit: ExitDue | None = None) -> Position
    def restore(self, position: Position) -> None           # undo a status change that did not happen
    def close(self, position_id: str, *, price: float, source: PriceSource, reason: ExitReason, at: datetime) -> Position
    def remove(self, position_id: str) -> Position          # reconciled "never filled"
    def set_equity_base(self, equity: float, at: datetime) -> None   # operator re-sync from the terminal balance
    def equity(self) -> float ; def unrealized(self) -> float ; def realized_since_base(self) -> float
    def snapshot(self, now: datetime, *, kill_switch: bool, kill_reason: str | None,
                 reconcile_pending: list[str], degraded: list[str]) -> RiskSnapshot
```
Exit rules per closed bar (long; mirror for short): `low <= stop` gives STOP. Otherwise `high >= target` gives
TARGET. Both in the same bar gives STOP (conservative). Otherwise `bars_held >= time_stop_bars` gives TIME.
Exit price: with `protective_at_broker`, the stop/target level (or the bar open if it gapped through).
Software-managed positions use the bar close, because the desk only acts at bar close. P&L comes from
`money.estimate_pnl`. Equity = starting equity + all realized estimates + operator adjustments. Unrealized
uses `last_price`. `trades_today` counts positions opened since `trading_day_start`. A bar affects a
position when `bar.end > position.opened_at` (the bar during which it opened counts). The engine loads
history since `min(equity_base_at, now - 30 days)` so streaks and today's P&L are complete, and ALL closed
history when there is no operator equity sync (equity = starting equity + every realized estimate).

### `execution/unitrader/vision.py`

```python
def similarity(a: Image.Image, b: Image.Image) -> float        # zero-mean normalized cross-correlation, -1..1
def is_blank(img: Image.Image, tolerance: float = 2.0) -> bool # nearly uniform (locked desktop = black)
def changed_fraction(a: Image.Image, b: Image.Image, pixel_tolerance: int = 24) -> float
```
Grayscale float math with numpy. Size mismatch raises `ValueError`. Two flat images count as 1.0 when
their means agree within tolerance, else 0.0.

### `execution/unitrader/fake.py` — test double (NOT the real UniTrader UI)

`FakeTerminal` renders a made-up trading window with PIL: per-symbol panels with a symbol label, a lot
field, BUY/SELL buttons, a positions counter, an account badge ("DEMO"/"LIVE"), an optional close-all
button and optional confirmation dialog. `FakeGuiBackend(terminal, screen_size=(1920, 1080))` implements
`GuiBackend`. Clicks hit-test the fake widgets; focused-field typing, ctrl+a/ctrl+c and the clipboard all
behave. Scenario knobs: focus stealing after N actions, a popup covering a panel, swapped panels, badge
text, fill delay / no fill, a confirmation dialog on click, an error dialog, locked desktop, missing
window, duplicate windows, a lot field that ignores ctrl+a, fail-safe trigger. Also
`calibrate_fake(terminal, out_dir) -> LayoutFile`, which builds a correct layout and templates
programmatically. The module docstring must say it is a simulation for tests and `backend: fake` demos,
and that it says nothing about the real terminal.

### `execution/unitrader/venue.py`

`UniTraderVenue(cfg: UniTraderConfig, layout: LayoutFile, backend: GuiBackend, mode: Mode, instruments,
clock: Clock, is_halted: HaltCheck, evidence_dir: Path, dry_run: bool = False)`. All GUI work runs on ONE
dedicated worker thread (`ThreadPoolExecutor(max_workers=1)`) behind a `threading.Lock`. Order flow
(`submit`), each step recorded in `report.evidence["steps"]`:

1. halted? → ABORTED (opening orders only — close_position/close_all ignore the kill switch so PANIC can
   flatten). `input_desktop_available()` false → ABORTED ("session locked/disconnected").
2. Locate the window: exactly one window whose title matches `window_title_regex` (and `process_name` if
   set). None or several → ABORTED with "log in to UniTrader / close duplicate windows" guidance.
3. Restore if minimized. Client size and DPI must equal the layout → else ABORTED (never resize the window).
4. Activate. `foreground_window()` must equal the handle (`focus_retries` attempts) → else ABORTED.
5. Account badge must match the template (threshold), AND `layout.account_kind` must equal the mode
   (`demo`↔DEMO, `live`↔LIVE) → else ABORTED "wrong account / not logged in". Optional one-click indicator.
6. Panel label template for the symbol must match → else ABORTED (protects against reordered/covered panels).
7. Positions-indicator baseline captured twice ~0.3 s apart; if `stability_check` and they differ → ABORTED.
8. Input guard, before the first click into the panel: same window geometry, still the foreground, no
   calibrated dialog or error template showing (a stale confirmation's button may sit right over a field),
   `root_window_at(point)` is our window for every field point, and the panel label still matches. The same
   guard runs again right before every click into a field and every key press (commit key, backspace).
   `dry_run` → run the read-only step 10 too (close-all flows as well), then DRY_RUN (nothing typed or
   clicked, so only the read-backs are not exercised).
9. Lot entry: click field, ctrl+a, type `format_lots`, then readback through the clipboard. Put a sentinel
   on the clipboard first; after click/ctrl+a/ctrl+c the field text must parse to the same Decimal. A
   sentinel left behind means the readback did not work: treat it as a mismatch. Then commit_key. With
   `protective_orders: panel_fields` also fill the SL/TP fields (same readback). With `protective_orders:
   none` every *calibrated* SL/TP input is emptied instead, because an order carries whatever those fields
   hold: type a probe and read it back (an empty field copies nothing, exactly like a broken copy, so this
   proves the field takes keys and copies), backspace, and require that it now copies nothing. Inputs that are
   not calibrated cannot be emptied; preflight warns the operator to keep them empty.
   After every field was committed, read each one back once more (fresh sentinel) and ABORT on any difference:
   the terminal may round, clamp or re-parse a value when it commits the edit, and the order goes out with the
   committed value. Then commit_key again. `lot_readback: clipboard` needs `commit_key: tab|enter`, and
   `commit_key: enter` needs the readback (Enter goes to whatever has the focus): config validation, preflight
   (`field_entry`) and the venue refuse the other combinations.
   The desk never reads or restores the operator's clipboard (it may hold the password they pasted into the
   login, and writing it back would drop a password manager's clipboard-history exclusion): the sentinel
   overwrites it and the flow ends with an empty clipboard. Only numeric read-backs are ever logged.
10. Re-verify foreground + panel label, no calibrated dialog showing, positions indicator unchanged since the
    baseline, and check that `root_window_at(button)` is our window (nothing on top). Check `is_halted()` again.
11. `on_before_click()` (write-ahead), click BUY/SELL, then record `clicked_at`. From here on, never ABORTED.
12. Optional order dialog: if its template appears within `dialog_timeout_seconds`, click confirm.
    Error templates appearing → REJECTED (press esc).
13. Poll the positions indicator until `changed_fraction >= change_threshold` (CONFIRMED) or
    `fill_timeout_seconds` passes (UNCONFIRMED). Fill price = feed quote/entry_ref,
    `fill_price_source="feed_estimate"`.
14. Park the mouse at `park_point`. Empty the clipboard if the readback used it. Save before/after evidence
    PNGs under `evidence_dir/<intent_id>/` when `save_evidence`.

`OperatorAbortError` before the click → ABORTED with `operator_abort=True`. After the click it gives
UNCONFIRMED with `operator_abort=True`. Any other exception before the click → ABORTED. After the click →
UNCONFIRMED. A whole flow is bounded by `max_order_seconds`. `close_position` uses `exit_method`
(`close_all` button + optional dialog + indicator change, or an `opposite_order` panel order).
`close_all` clicks the close-all button (no button calibrated → ABORTED, "close manually").
An `opposite_order` close empties the calibrated SL/TP inputs like an opening order with
`protective_orders: none`.
`preflight()` and `health()` run steps 1-6 for every calibrated panel without typing or clicking. They
report one `HealthCheck` per item, with remediation text, e.g. "UniTrader window not found: open UniTrader
and log in to your DEMO account yourself, then retry." Warnings that need no fix to trade (e.g. SL/TP inputs
not calibrated with `protective_orders: none`) are `ok` checks with `data={"warning": true}`.

### `execution/unitrader/win32_backend.py` and `calibrate.py`

* `Win32Backend` implements `GuiBackend` on Windows only (imports are lazy and guarded by
  `sys.platform == "win32"`). It uses ctypes with **explicit `argtypes`/`restype` on every Win32
  function** (64-bit handle safety): `EnumWindows`, `GetWindowTextW`, `IsWindowVisible`, `IsIconic`,
  `GetClientRect`, `ClientToScreen`, `GetWindowThreadProcessId`, `OpenProcess` +
  `QueryFullProcessImageNameW`, `GetForegroundWindow`, `SetForegroundWindow` (with the Alt-key
  foreground-lock workaround), `ShowWindow(SW_RESTORE)`, `WindowFromPoint` + `GetAncestor(GA_ROOT)`,
  `GetDpiForWindow`, `OpenInputDesktop`/`CloseDesktop`, and the clipboard (`OpenClipboard`,
  `GetClipboardData(CF_UNICODETEXT)`, `GlobalLock/Unlock/Alloc`, `SetClipboardData`, `EmptyClipboard`,
  `CloseClipboard`, with retries while another process holds it). It sets per-monitor DPI awareness before
  importing pyautogui. Mouse/keyboard go through pyautogui (`FAILSAFE=True`, `PAUSE` from config);
  `pyautogui.FailSafeException` becomes `OperatorAbortError`. Screenshots use `PIL.ImageGrab.grab(bbox=...,
  all_screens=True)`.
* `calibrate.py`: an interactive wizard (`run_calibration(cfg, backend, out_path, symbols, prompt=input,
  say=print, countdown=3)`). It first tells the operator to **log in to UniTrader themselves** and open
  the one-click panels, then asks for confirmation. It lets them pick the window (title + process),
  records client size and DPI, and for every element says "hover over X; capturing in 3..2..1" (it never
  requires clicking the terminal). It asks which account is showing (demo/live), captures the template
  images, writes the layout + `templates/*.png` atomically, then re-verifies. `prompt`/`say` are injectable
  so tests drive it with `FakeGuiBackend`.

### `llm/reviewer.py` — Pink Sentiment (Claude)

`ClaudeReviewer(cfg: LlmConfig, clock: Clock, client: anthropic.AsyncAnthropic | None = None)` implements
`TradeReviewer`. Call `client.beta.messages.create(model=cfg.model, max_tokens=cfg.max_tokens,
system=<stable prompt>, messages=[{"role": "user", "content": <JSON context>}],
output_config={"effort": cfg.effort, "format": {"type": "json_schema", "schema": <ReviewVerdict schema,
additionalProperties false, no numeric/length constraints>}}, betas=["server-side-fallback-2026-07-01"],
fallbacks="default")` (drop `betas`/`fallbacks` when `server_fallback` is false). There is no
`temperature`: current models reject sampling parameters. Client:
`AsyncAnthropic(timeout=cfg.timeout_seconds, max_retries=cfg.max_retries)`, and the whole call (SDK retries
included) runs under `asyncio.timeout(cfg.timeout_seconds)`, so one review never takes longer than that.
Cancellation (the engine abandoning a meeting on kill/panic) propagates. Handle `stop_reason ==
"refusal"` and `"max_tokens"`. Parse the first text block as JSON and validate with `ReviewVerdict`
(`concerns` truncated to 5, `summary` to 600 chars, client side). Typed SDK errors are caught most-specific
first (`AuthenticationError`, `PermissionDeniedError`, `NotFoundError`, `BadRequestError`, `RateLimitError`,
`APIStatusError`, `APITimeoutError`, `APIConnectionError`), then any other `Exception` (e.g. the SDK's
`TypeError` when no credentials are set). Any failure → `failure_policy` (`veto` → EJECT, `abstain` →
HOLD) with `error` set. The daily call cap (`max_calls_per_day`, UTC day) gives the policy outcome
without calling; its counters are kept in the store's kv table (`llm_usage`) so a restart does not reset
them. Log `_request_id` and usage. The system prompt states that the data is untrusted
numeric JSON, that the reviewer can only APPROVE or VETO, and when to veto.

### Wave 2/3 (built on top of the above)

#### `crew.py` — the six crewmates

```python
@dataclass(frozen=True)
class MeetingContext:
    now: datetime
    mode: Mode
    instrument: InstrumentConfig
    bars: Sequence[Bar]            # this symbol's history, oldest first, last = just-closed bar
    quote: Quote | None
    snapshot: RiskSnapshot
    last_bar_end: datetime | None

    @property
    def spread_price(self) -> float  # quote.spread if a quote exists, else instrument.typical_spread_price

class CyanScout:      def report(self, result: ScoutResult) -> CrewReport
class LimeCharts:     def __init__(self, planner: ChartsPlanner); def assess(self, signal, ctx) -> tuple[CrewReport, TradePlan | None]
class YellowNews:     def __init__(self, calendar: NewsCalendar); def assess(self, ctx) -> CrewReport
class RedSkeptic:     def __init__(self, cfg: DeskConfig, gate: RiskGate); def assess(self, plan, ctx) -> tuple[CrewReport, SizingResult]
class PinkSentiment:  def __init__(self, cfg: DeskConfig, reviewer: TradeReviewer); async def assess(self, signal, plan, ctx) -> CrewReport
class BlackChief:     def decide(self, signal, reports: Sequence[CrewReport], sizing: SizingResult | None) -> tuple[FinalAction, list[str]]
```
Chief rule: EXECUTE only if Cyan, Lime, Yellow and Red all vote PROCEED, Pink does not vote EJECT (HOLD =
LLM disabled is fine), and the sizing has lots. Any EJECT gives SKIP_ROUND with the EJECT summaries as
reasons. `CrewReport.details` holds the numbers behind each vote (JSON-safe).

#### `meeting.py`

```python
class EmergencyMeeting:
    def __init__(self, cfg: DeskConfig, *, planner: ChartsPlanner, calendar: NewsCalendar, gate: RiskGate,
                 reviewer: TradeReviewer) -> None
    async def convene(self, signal: BreakoutSignal, ctx: MeetingContext, *, trigger: Literal["breakout", "manual"] = "breakout",
                      scout_report: CrewReport | None = None) -> MeetingRecord
    async def no_signal_round(self, symbol: str, ctx: MeetingContext, scout_result: ScoutResult) -> MeetingRecord
        # manual meeting without a breakout: Cyan EJECT with the scout's reason, others HOLD, decision SKIP_ROUND
```
Order of evaluation: Cyan → Lime → Yellow → Red → (Pink only if all four said PROCEED) → Chief. Crewmates
that don't run report HOLD with the summary "skipped: <earlier EJECT>", so Claude is never paid for a
trade that is already dead. The transcript is chat-style and timestamped (UTC `HH:MM:SS`):
`SYSTEM: 🚨 EMERGENCY MEETING — EURUSD 🚨`, `Cyan Scout: ...`, ..., `Black Chief: EXECUTE_BUY 0.12 lots` or
`... SKIP_ROUND (Red Skeptic ejected: ...)`. Only computed facts go in it, no invented flavour text about
the market.

#### `engine.py`

```python
class OperatorActionError(Exception): ...    # message is shown to the operator verbatim

class DeskEngine:
    def __init__(self, cfg: DeskConfig, *, feed: MarketDataFeed, venue: ExecutionVenue, reviewer: TradeReviewer,
                 store: Store, audit: AuditLog, notifier: Notifier, hub: EventHub, clock: Clock,
                 killswitch: KillSwitch, calendar: NewsCalendar) -> None
    async def start(self) -> None   # load positions/intents from the store, startup reconciliation, warmup, venue preflight
    async def run(self) -> None     # consume feed.stream() + watchdog until stop() or the feed ends
    async def stop(self) -> None
    async def on_bar(self, bar: Bar) -> None
    def state(self) -> dict[str, Any]                     # JSON-safe snapshot, shape below
    async def kill(self, reason: str, *, by: str) -> None
    async def resume(self, *, by: str) -> None            # OperatorActionError while reconcile items are pending
    async def panic(self, *, by: str) -> ExecutionReport | None
    async def reconcile(self, item_id: str, outcome: Literal["filled", "not_filled"], *, fill_price: float | None, by: str) -> None
    async def set_equity(self, equity: float, *, by: str) -> None        # operator syncs with the terminal's balance
    async def manual_meeting(self, symbol: str, *, by: str, force_side: Side | None = None) -> MeetingRecord
        # force_side is accepted ONLY in paper mode (OperatorActionError otherwise)
```
* **Startup reconciliation.** Active positions are reloaded (a position on an instrument missing from the
  config refuses the start with a ConfigError). PENDING intents become ABORTED (the venue was never
  called). SUBMITTING and UNCONFIRMED intents stay as reconcile items: new trading halts until the
  operator answers. A CLOSING position whose close is CONFIRMED/RECONCILED_FILLED is closed (estimated
  price, reason RECONCILED); one whose close was REJECTED goes to RECONCILE; otherwise it is OPEN again. A
  CONFIRMED/RECONCILED_FILLED open intent with no position row (crash between the two writes) becomes a
  RECONCILE position plus an urgent alert. Then the warmup bars that closed after each position's
  `last_marked_bar_end` are applied (catch-up): a broker-side SL/TP crossed meanwhile goes to the
  operator, a software exit that became due is sent on the first live bar.
* **Instruments.** The feed, warmup and staleness watchdog cover the enabled instruments plus any
  instrument with an active position; `enabled: false` only stops new entries.
* **on_bar.** Ignore bars for unknown symbols, and for disabled ones without a position. Update the
  position book, which may return exits: a software-managed exit uses close_position; a broker-protected
  STOP/TARGET reached on the feed is **not** booked (the feed is not the broker's price): the position goes
  to RECONCILE with the suggested exit and an urgent "is it still open?" alert (paper mode books it, since
  its simulated broker fills on that feed). Only an ABORTED/DRY_RUN close is sent again on the next bar; a
  REJECTED close (it was clicked) goes to RECONCILE. Add the bar to history. A bar older than one
  timeframe (a newer one has closed: feed catch-up) is not traded. Run the scout. On a signal, convene the
  meeting as a task that `kill()`/`panic()` cancel; if the decision comes after the next bar has closed,
  the plan is stale and not sent. On EXECUTE_*, build an OrderIntent, `store.add_intent` (PENDING), then
  `venue.submit(intent, quote, on_before_click=<persist SUBMITTING + audit>)`. Then persist the report.
  CONFIRMED becomes `PositionBook.add_open` (protective = `venue.supports_protective_orders`).
  UNCONFIRMED becomes a reconcile item, a phone alert and a halt. REJECTED/ABORTED are logged and
  alerted. `report.operator_abort` engages the kill switch.
* **Reconcile.** Only SUBMITTING/UNCONFIRMED intents and RECONCILE positions can be answered. The intent
  status and the positions it affects are written in one transaction (`Store.resolve_intent`). An open
  answered "filled" without stop/target (a test order) becomes an unmanaged RECONCILE position; without
  any known price the answer is refused until the operator types the fill price. "not filled" for a close
  only reopens positions still linked to that close. A failed PANIC restores every position exactly as it
  was (keeping links to unresolved closes).
* **Daily loss.** When `RiskGate.daily_loss_breached` is true, notify once per trading day
  (`risk.day_rollover_utc`). With `flatten_on_daily_loss`, also run the panic close, again on later bars
  until something was clicked (CONFIRMED/UNCONFIRMED).
* **Watchdog** (every `execution.unitrader.health_interval_seconds`, or 30 s for paper). It runs
  `venue.health()` and checks feed staleness (no bar for `feed.max_staleness_bars` × timeframe during an
  enabled instrument's session). Failures go into `degraded` (which blocks new trades through the risk
  gate) plus a deduplicated alert. A failure that means "window/badge missing" produces the alert
  "UniTrader needs you: log in to your <DEMO|LIVE> account yourself". The desk never tries to log in.
* **Everything** of consequence is written to the audit log (meetings, intents and every status change,
  operator actions, kill/resume, health transitions) and published on the EventHub. Event types:
  `meeting`, `intent`, `position`, `kill`, `resume`, `health`, `bar`, `alert`, `equity`.

`state()` shape (all JSON-safe; datetimes ISO-8601 UTC; Decimals as strings):
```json
{
  "mode": "paper|demo|live", "venue": "paper|unitrader", "dry_run": false,
  "started_at": "…", "now": "…",
  "kill_switch": {"engaged": false, "reason": null, "by": null, "at": null},
  "reconcile": [{"id": "…", "kind": "intent|position", "symbol": "EURUSD", "side": "BUY", "lots": "0.10",
                 "status": "UNCONFIRMED", "created_at": "…", "message": "…",
                 "question": "Did this BUY 0.10 EURUSD order open a position in UniTrader?",
                 "options": [{"outcome": "filled", "label": "Yes, a position opened", "needs_price": true},
                             {"outcome": "not_filled", "label": "No, nothing opened", "needs_price": false}]}],
  "degraded": ["…"],
  "health": {"venue": {"venue": "…", "ok": true, "checked_at": "…", "checks": [{"name": "…", "ok": true, "detail": "…"}]} ,
             "feed": {"…": "…"}},
  "equity": {"estimate": 10000.0, "day_start": 10000.0, "realized_today": 0.0, "unrealized": 0.0, "currency": "USD"},
  "limits": {"trades_today": 0, "max_trades_per_day": 4, "open_positions": 0, "max_open_positions": 1,
             "daily_loss_pct": 0.0, "max_daily_loss_pct": 1.5},
  "positions": [Position + {"unrealized_pnl": float | null}, …],
  "crew": [{"id": "CYAN_SCOUT", "name": "Cyan Scout", "color": "#38fedc", "role": "…", "vote": "HOLD", "summary": "…", "at": null}],
  "meetings": [MeetingRecord, … latest first, max 20],
  "intents": [{"intent": OrderIntent, "status": "CONFIRMED", "report": ExecutionReport | null}, … max 20],
  "instruments": [{"symbol": "EURUSD", "enabled": true, "specs_verified": true, "in_session": true,
                   "last_bar": {"start": "…", "close": 1.0852} | null}],
  "llm": {"enabled": false, "calls": 0, "…": "…"}
}
```

#### `dashboard/server.py` + `dashboard/static/`

`create_app(engine: DeskEngine, cfg: DeskConfig, token: str) -> FastAPI`.
* `GET /` and `/static/*` are public (no data in them). `GET /api/health` is public and returns
  `{"ok": true}` only.
* Everything else under `/api` needs `Authorization: Bearer <token>`, compared with `hmac.compare_digest`.
  The right token is **always** accepted. Only a wrong Bearer token counts as a failure (a request without
  one guessed nothing). After `max_auth_failures` failures inside `auth_failure_window_seconds` from one
  client IP, further wrong tokens get 429 with `Retry-After`. Every client behind the SSH tunnel is
  127.0.0.1, so a lockout that also refused the right token would let any web page or local process keep the
  operator away from kill/panic. Brute force is stopped by the token's entropy, not by the lockout.
* The token file `data_dir/dashboard_token` (`wiring.ensure_dashboard_token`) is readable by the desk's user
  only, also when it already exists: mode 0600 on POSIX; on Windows, where that mode does nothing, `icacls`
  removes the inherited ACL entries and grants only the current user. A failure there is logged as a warning.
* The Host header must be in `dashboard.allowed_hosts` (port stripped), else 400 (DNS-rebinding guard).
* Security headers on every response: `Content-Security-Policy: default-src 'self'; img-src 'self' data:;
  style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none';
  form-action 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `X-Frame-Options: DENY`, `Cache-Control: no-store` on `/api`.
* `GET /api/state` returns `engine.state()`.
* `GET /api/events` is SSE (`text/event-stream`) of hub events (`event: <type>`, `data: <json>`) plus a
  keep-alive comment every 15 s. Use `async with hub.subscribe() as sub:` and `await sub.next_within(15)`
  (returns `None` on timeout, which is when to send the keep-alive). Leaving the `async with` releases the
  subscription when the client disconnects. Clients read it with `fetch()` streaming so the token stays in a header,
  never in a URL.
* `POST /api/kill {"reason": str}`, `POST /api/resume {"confirm": "RESUME"}`,
  `POST /api/panic {"confirm": "PANIC"}`, `POST /api/reconcile {"id": str, "outcome": "filled|not_filled",
  "fill_price": float|null}`, `POST /api/equity {"equity": float}`, `POST /api/meeting {"symbol": str,
  "force_side": "BUY|SELL|null"}` (only when `allow_manual_meetings`). `OperatorActionError` becomes 409
  `{"error": msg}`. Validation errors are 422. All bodies use Pydantic models with `extra="forbid"` and
  `allow_inf_nan=False`, parsed by a JSON parser that refuses `NaN`, `Infinity` and numbers that overflow to
  infinity (`1e400`), so a non-finite number is a 422 and never reaches the engine. `fill_price` must be
  `> 0` and `< 1e9`.
* The UI (vanilla JS, no CDN, no inline scripts or styles so the CSP holds) is mobile-first and
  Among Us-themed (space background, SVG crewmates in the role colours). It has a token prompt (stored in
  `localStorage`), a status bar (mode badge PAPER/DEMO/LIVE, kill switch, health, equity estimate, trades
  vs limit), the crew board (vote and summary per crewmate), the emergency-meeting chat transcript with an
  "EMERGENCY MEETING" banner animation on new meetings, the positions table, recent orders, reconcile cards
  (each shows the item's `question` and one button per entry of its `options`, with the price field only
  when an option has `needs_price`; without them, a position card asks "still open?", never "filled?"),
  kill/resume, a **hold-to-confirm** PANIC button (2 s press), manual meeting
  (symbol picker; force side only in paper mode), and estimated values labelled "est." everywhere. Mark
  the DEMO/LIVE mode badge loudly (LIVE in red).
* Every form has exactly one submit button, its action: Enter in a field submits through the first submit
  button, so the confirm dialog's Cancel is a plain button and Enter in the kill Reason field engages the
  kill switch. Cancelling the kill dialog says so in a toast.
* On 401 or 429 the page drops the token (memory and `localStorage`), stops polling and the event stream,
  and asks for the token again ("token rejected"), so a stale tab can't keep sending a wrong token.

#### `cli.py`

`desk run | preflight | calibrate | test-order | backtest | kill | resume | status | audit-verify | init-config`
(argparse; `-c/--config` defaults to `config/desk.yaml`).
* `run` builds every component from the config. It takes the single-instance lock, configures logging and
  ensures the dashboard token (`DESK_DASHBOARD_TOKEN`, or one generated and saved to
  `data_dir/dashboard_token` with 0600 permissions; printed once). For demo/live it runs the **login
  gate**: run the preflight; if it fails, print the failing checks and the instruction "Open UniTrader and
  log in to your DEMO account yourself (this tool never handles your password)". It re-checks every 30 s
  on its own and sends one alert on the first failure, with or without a TTY (a restarted desk in a
  console must not wait silently); with a TTY, Enter re-checks at once and q quits.
  Then it starts the engine and uvicorn in one event loop and shuts down cleanly on Ctrl+C.
* `preflight` runs the config + layout + venue preflight (with the same interactive login gate when
  `--wait-for-login` is passed) and exits non-zero on failure.
* `calibrate` runs the wizard (Windows backend, or `--simulate` for the fake terminal).
* `test-order --symbol EURUSD --side BUY [--lots 0.01]` is a round trip: it opens ONE position after a typed
  confirmation, then closes it with the configured exit method (close-all button, or an opposite order)
  when you press Enter. The fill and its unmanaged RECONCILE position are written in one transaction;
  anything unconfirmed is recorded for reconciliation. It refuses in live mode unless
  `--i-know-this-is-live`, and always while the kill switch is engaged, while the desk has any active
  position or unresolved order, for `specs_verified: false` (demo/live), or without a reference price.
* Every command loads `<project root>/.env` first (existing environment variables win). `kill` works even
  when the config does not validate: it falls back to `data_dir` from the raw YAML.
* `backtest --csv PATH | --synthetic N [--symbol S] [--json]`.
* `kill [--reason]`, `resume`, `status` (summary from the store + kill file; works while the desk is
  running) and `audit-verify`.
* `init-config [--path config/desk.yaml]` copies the example config without overwriting anything.
* Exit codes (`scripts/windows/run-desk.ps1` relies on them): 0 = clean exit, 1 = unexpected error,
  2 = configuration error, 3 = another instance is already running, 4 = preflight failed or the operator
  quit at the login gate, 130 = interrupted (Ctrl+C) before the engine started.


#### `backtest.py`

`run_backtest(cfg: DeskConfig, bars: Mapping[str, Sequence[Bar]]) -> BacktestReport` uses the real scout,
planner, calendar, sizing and risk gate. The LLM is never called. A signal at a bar close fills at the
**next bar's open**, plus or minus half the typical spread and slippage. Exits follow `PositionBook` rules
(`protective_orders` decides stop-level vs bar-close exits, as in live). Uses a `ManualClock` driven by
bar time. The report shows trades (list), count, win rate, average R, expectancy R, profit factor, max
drawdown (account currency and %), total return %, signals seen, and skipped rounds by reason.
`format_report(report) -> str` prints a plain-text table.
