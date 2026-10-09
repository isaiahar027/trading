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
| `Notifier`, `NullNotifier` | `notify.py` |
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
   before every click. `live` needs `DESK_LIVE_ACK` plus a layout calibrated on the live account.
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
- extension `(close_t - donchian_high_t) / ATR_{t-1} <= max_extension_atr` (don't chase);
- squeeze: Bollinger bandwidth percentile at `t-1` over the previous `squeeze_lookback` values
  `<= max_squeeze_percentile` (`1.0` disables);
- cooldown: no second signal on the same symbol within `signal_cooldown_bars` bars of the last signal;
- `strength` in [0,1] combines extension, range expansion and squeeze tightness; `reasons` explain it.

Charts rules: with `require_trend_alignment`, a long needs `close > EMA(trend_ema_period)` and an EMA slope
over `trend_slope_bars` of `>= 0` (mirror for shorts); otherwise vote EJECT. Plan: `entry_ref = signal.close`,
`stop_distance = stop_atr_mult * signal.atr`, stop/target from `target_r`. EJECT if
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
                                                 # create it by hand, e.g. from the VPS desktop)
    def reason(self) -> str | None
    def engage(self, reason: str, by: str) -> None    # atomic write of JSON {reason, by, at}
    def release(self, by: str) -> None
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
`CONFIRMED` with `fill_price_source="venue"`. Halted gives `ABORTED`. `supports_protective_orders = True`.

### `positions.py`

```python
@dataclass(frozen=True) class ExitDue: position: Position; reason: ExitReason; price: float
class PositionBook:
    def __init__(self, cfg: DeskConfig, active: Iterable[Position] = (), history: Iterable[Position] = ()) -> None
    def active(self) -> list[Position]
    def add_open(self, intent: OrderIntent, report: ExecutionReport, *, protective: bool, at: datetime) -> Position
    def mark_bar(self, bar: Bar) -> list[ExitDue]
    def set_status(self, position_id: str, status: PositionStatus, *, close_intent_id: str | None = None) -> Position
    def close(self, position_id: str, *, price: float, source: PriceSource, reason: ExitReason, at: datetime) -> Position
    def remove(self, position_id: str) -> Position          # reconciled "never filled"
    def adjust_equity(self, delta: float, note: str) -> None
    def snapshot(self, now: datetime, *, kill_switch: bool, kill_reason: str | None,
                 reconcile_pending: list[str], degraded: list[str]) -> RiskSnapshot
```
Exit rules per closed bar (long; mirror for short): `low <= stop` gives STOP. Otherwise `high >= target` gives
TARGET. Both in the same bar gives STOP (conservative). Otherwise `bars_held >= time_stop_bars` gives TIME.
Exit price: with `protective_at_broker`, the stop/target level (or the bar open if it gapped through).
Software-managed positions use the bar close, because the desk only acts at bar close. P&L comes from
`risk.estimate_pnl`. Equity = starting equity + all realized estimates + operator adjustments. Unrealized
uses `last_price`. `trades_today` counts positions opened since `trading_day_start`.

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

1. halted? → ABORTED. `input_desktop_available()` false → ABORTED ("session locked/disconnected").
2. Locate the window: exactly one window whose title matches `window_title_regex` (and `process_name` if
   set). None or several → ABORTED with "log in to UniTrader / close duplicate windows" guidance.
3. Restore if minimized. Client size and DPI must equal the layout → else ABORTED (never resize the window).
4. Activate. `foreground_window()` must equal the handle (`focus_retries` attempts) → else ABORTED.
5. Account badge must match the template (threshold), AND `layout.account_kind` must equal the mode
   (`demo`↔DEMO, `live`↔LIVE) → else ABORTED "wrong account / not logged in". Optional one-click indicator.
6. Panel label template for the symbol must match → else ABORTED (protects against reordered/covered panels).
7. Positions-indicator baseline captured twice ~0.3 s apart; if `stability_check` and they differ → ABORTED.
8. `dry_run` → DRY_RUN (nothing typed).
9. Lot entry: click field, ctrl+a, type `format_lots`, then readback through the clipboard. Put a sentinel
   on the clipboard first; after click/ctrl+a/ctrl+c the field text must parse to the same Decimal. A
   sentinel left behind means the readback did not work: treat it as a mismatch. Then commit_key. With
   `protective_orders: panel_fields` also fill the SL/TP fields (same readback).
10. Re-verify foreground + panel label, and check that `root_window_at(button)` is our window (nothing on top).
    Check `is_halted()` again.
11. `on_before_click()` (write-ahead), click BUY/SELL, then record `clicked_at`. From here on, never ABORTED.
12. Optional order dialog: if its template appears within `dialog_timeout_seconds`, click confirm.
    Error templates appearing → REJECTED (press esc).
13. Poll the positions indicator until `changed_fraction >= change_threshold` (CONFIRMED) or
    `fill_timeout_seconds` passes (UNCONFIRMED). Fill price = feed quote/entry_ref,
    `fill_price_source="feed_estimate"`.
14. Park the mouse at `park_point`. Save before/after evidence PNGs under `evidence_dir/<intent_id>/` when
    `save_evidence`.

`OperatorAbortError` before the click → ABORTED with `operator_abort=True`. After the click it gives
UNCONFIRMED with `operator_abort=True`. Any other exception before the click → ABORTED. After the click →
UNCONFIRMED. A whole flow is bounded by `max_order_seconds`. `close_position` uses `exit_method`
(`close_all` button + optional dialog + indicator change, or an `opposite_order` panel order).
`close_all` clicks the close-all button (no button calibrated → ABORTED, "close manually").
`preflight()` and `health()` run steps 1-6 for every calibrated panel without typing or clicking. They
report one `HealthCheck` per item, with remediation text, e.g. "UniTrader window not found: open UniTrader
and log in to your DEMO account yourself, then retry."

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
`AsyncAnthropic(timeout=cfg.timeout_seconds, max_retries=cfg.max_retries)`. Handle `stop_reason ==
"refusal"` and `"max_tokens"`. Parse the first text block as JSON and validate with `ReviewVerdict`
(`concerns` truncated to 5, `summary` to 600 chars, client side). Typed SDK errors are caught most-specific
first (`AuthenticationError`, `PermissionDeniedError`, `NotFoundError`, `BadRequestError`, `RateLimitError`,
`APIStatusError`, `APITimeoutError`, `APIConnectionError`). Any failure → `failure_policy` (`veto` → EJECT,
`abstain` → HOLD) with `error` set. The daily call cap (`max_calls_per_day`, UTC day) gives the policy
outcome without calling. Log `_request_id` and usage. The system prompt states that the data is untrusted
numeric JSON, that the reviewer can only APPROVE or VETO, and when to veto.

### Wave 2/3 (built on top of the above)

* `crew.py` / `meeting.py`: crewmate classes producing `CrewReport`s and `EmergencyMeeting.convene(...)
  -> MeetingRecord` with an Among Us-style transcript. Pink runs only when every deterministic crewmate
  votes PROCEED.
* `engine.py`: `DeskEngine` wires feed, history, scout, meeting, store, audit, position book, venue,
  kill switch, notifier and event hub. Startup reconciliation: PENDING intents → ABORTED (never reached
  the venue); SUBMITTING/UNCONFIRMED → reconcile required (trading halted until the operator resolves
  it). Watchdog loop. Operator actions: kill, resume, panic, reconcile, adjust equity, manual meeting.
* `dashboard/`: FastAPI app factory, bearer-token auth (`hmac.compare_digest`), Host allow-list,
  security headers/CSP, auth-failure lockout, SSE via fetch streaming (no token in URLs), and a mobile-first
  Among Us UI.
* `cli.py`: `desk run | preflight | calibrate | test-order | backtest | kill | resume | status | audit-verify | init-config`.
  The interactive login gate lives in `run`/`preflight` for demo/live.
* `backtest.py`: same scout/charts/calendar/risk/position code on CSV or synthetic bars, fills at the
  next bar open, report of trades, win rate, expectancy (R), profit factor, max drawdown.
