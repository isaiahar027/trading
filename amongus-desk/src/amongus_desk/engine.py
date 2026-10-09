"""The desk engine: feed → crew meeting → write-ahead intent → venue → position book → exits.

Safety properties the engine maintains:

* Every order is persisted as PENDING before the venue is called, and as SUBMITTING (write-ahead) right
  before the irreversible click. On restart PENDING becomes ABORTED (never reached the venue) and
  SUBMITTING/UNCONFIRMED become *reconcile items*. New trades stay blocked until the operator answers them.
* Orders are never retried automatically. A failed *close* is retried on the next bar (an ABORTED close
  provably did nothing), and the operator is alerted every time.
* The kill switch blocks new entries only; exits and PANIC still run.
* The desk never logs in to the broker. When the venue reports the terminal missing or the wrong account,
  trading halts and the operator is asked to log in themselves.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any, Literal

from .audit import AuditLog
from .clock import Clock
from .config import DeskConfig
from .crew import MeetingContext
from .domain import (
    CREW_DISPLAY,
    Bar,
    CrewId,
    ExecutionReport,
    ExitReason,
    FinalAction,
    IntentStatus,
    MeetingRecord,
    Mode,
    OrderIntent,
    Position,
    PositionStatus,
    Side,
    VenueHealth,
    Vote,
)
from .events import EventHub
from .execution.base import ExecutionVenue
from .killswitch import KillSwitch
from .llm.base import TradeReviewer
from .marketdata.base import MarketDataFeed
from .meeting import EmergencyMeeting, build_forced_signal
from .money import estimate_pnl
from .news import NewsCalendar
from .notify import Notifier, Priority
from .positions import ExitDue, PositionBook
from .risk import RiskGate
from .store import Store
from .strategy import BarHistory, BreakoutScout, ChartsPlanner

log = logging.getLogger(__name__)

EQUITY_KV = "equity_base"
HISTORY_DAYS = 30
_RECONCILE_STATUSES = (IntentStatus.SUBMITTING, IntentStatus.UNCONFIRMED)


class OperatorActionError(Exception):
    """An operator action was refused; the message is shown to the operator verbatim."""


class DeskEngine:
    def __init__(
        self,
        cfg: DeskConfig,
        *,
        feed: MarketDataFeed,
        venue: ExecutionVenue,
        reviewer: TradeReviewer,
        store: Store,
        audit: AuditLog,
        notifier: Notifier,
        hub: EventHub,
        clock: Clock,
        killswitch: KillSwitch,
        calendar: NewsCalendar,
    ) -> None:
        self._cfg = cfg
        self._feed = feed
        self._venue = venue
        self._reviewer = reviewer
        self._store = store
        self._audit = audit
        self._notifier = notifier
        self._hub = hub
        self._clock = clock
        self._killswitch = killswitch
        self._calendar = calendar
        self._instruments = {i.symbol: i for i in cfg.instruments}
        warmup = cfg.strategy.warmup_bars
        self._history = BarHistory(maxlen=max(500, 3 * warmup), timeframe_minutes=cfg.strategy.timeframe_minutes)
        self._scout = BreakoutScout(cfg.strategy)
        self._gate = RiskGate(cfg.risk, cfg.strategy)
        self._meeting = EmergencyMeeting(
            cfg, planner=ChartsPlanner(cfg.strategy), calendar=calendar, gate=self._gate, reviewer=reviewer
        )
        self._book = PositionBook(cfg)
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._started_at: datetime | None = None
        self._degraded: dict[str, str] = {}
        self._venue_health: VenueHealth | None = None
        self._in_flight: set[str] = set()
        self._exit_retry: dict[str, ExitReason] = {}
        self._crew: dict[CrewId, dict[str, Any]] = {
            c: {"vote": Vote.HOLD.value, "summary": "", "at": None} for c in CrewId
        }
        self._daily_loss_alerted: date | None = None
        self._last_kill_state: bool | None = None

    # ====================================================================================== lifecycle

    @property
    def book(self) -> PositionBook:
        return self._book

    @property
    def hub(self) -> EventHub:
        return self._hub

    @property
    def config(self) -> DeskConfig:
        return self._cfg

    @property
    def history(self) -> BarHistory:
        return self._history

    async def start(self) -> None:
        now = self._clock.now()
        self._started_at = now
        self._load_book(now)
        self._startup_reconciliation()
        symbols = [i.symbol for i in self._cfg.enabled_instruments]
        try:
            warm = await self._feed.warmup(symbols, self._history_size())
        except Exception as exc:
            self._set_degraded("feed", f"market data warmup failed: {exc}")
            log.exception("feed warmup failed")
            warm = {}
        for bars in warm.values():
            for bar in bars:
                self._history.add(bar)
        await self._check_venue(preflight=True)
        self._last_kill_state = self._killswitch.engaged()
        self._audit.append(
            "desk.start",
            {
                "mode": self._cfg.mode.value,
                "venue": self._venue.name,
                "dry_run": self._cfg.execution.dry_run,
                "warmup_bars": {s: len(b) for s, b in warm.items()},
                "active_positions": [p.id for p in self._book.active()],
            },
        )
        self._hub.publish("state", {"reason": "started"})
        log.info("desk started in %s mode with %d active positions", self._cfg.mode.value, len(self._book.active()))

    async def run(self) -> None:
        """Consume the feed until :meth:`stop` is called or the feed ends."""
        watchdog = asyncio.create_task(self._watchdog(), name="desk-watchdog")
        try:
            async for bar in self._feed.stream():
                if self._stop.is_set():
                    break
                try:
                    await self.on_bar(bar)
                except Exception:
                    # A bug in one bar's processing must not kill the desk; it is logged and alerted.
                    log.exception("error while processing %s bar %s", bar.symbol, bar.start.isoformat())
                    await self._alert("Desk error", f"processing a {bar.symbol} bar failed; see the log", "high")
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog

    async def stop(self) -> None:
        self._stop.set()
        with contextlib.suppress(Exception):
            await self._feed.aclose()
        self._audit.append("desk.stop", {})

    def _history_size(self) -> int:
        return max(self._cfg.strategy.warmup_bars + 5, self._cfg.llm.recent_bars + 5)

    def _load_book(self, now: datetime) -> None:
        base = self._store.get_kv(EQUITY_KV)
        base_equity: float | None = None
        base_at: datetime | None = None
        if isinstance(base, dict) and "equity" in base and "at" in base:
            try:
                base_equity = float(base["equity"])
                base_at = datetime.fromisoformat(str(base["at"]))
            except (TypeError, ValueError):
                log.error("ignoring malformed equity base in the store: %r", base)
                base_equity, base_at = None, None
        since = now - timedelta(days=HISTORY_DAYS)
        if base_at is not None and base_at < since:
            since = base_at
        history = self._store.closed_positions_since(since)
        self._book = PositionBook(
            self._cfg, self._store.active_positions(), history, equity_base=base_equity, equity_base_at=base_at
        )

    def _startup_reconciliation(self) -> None:
        for row in self._store.intents_with_status(IntentStatus.PENDING):
            self._store.set_intent_status(row.intent.id, IntentStatus.ABORTED)
            self._audit.append(
                "intent.status", {"id": row.intent.id, "status": "ABORTED", "why": "restart: never sent"}
            )
        unresolved = {row.intent.id for row in self._store.intents_with_status(*_RECONCILE_STATUSES)}
        for pos in self._book.active():
            if pos.status is PositionStatus.CLOSING and pos.close_intent_id not in unresolved:
                # The close never reached the venue (or finished): the position is still open.
                updated = self._book.set_status(pos.id, PositionStatus.OPEN)
                self._store.upsert_position(updated)
        if unresolved:
            log.warning("%d order(s) need reconciliation after restart: %s", len(unresolved), sorted(unresolved))

    # ====================================================================================== bars

    async def on_bar(self, bar: Bar) -> None:
        inst = self._instruments.get(bar.symbol)
        if inst is None or not inst.enabled:
            return
        if not self._history.add(bar):
            return
        self._degraded.pop(f"feed:{bar.symbol}", None)
        self._hub.publish("bar", {"symbol": bar.symbol, "start": bar.start.isoformat(), "close": bar.close})
        async with self._lock:
            await self._process_exits(bar)
            await self._check_daily_loss()
            result = self._scout.evaluate(self._history.bars(bar.symbol))
            self._crew[CrewId.CYAN_SCOUT] = {
                "vote": Vote.PROCEED.value if result.signal else Vote.HOLD.value,
                "summary": f"{bar.symbol}: {result.reason}",
                "at": self._clock.now().isoformat(),
            }
            if result.signal is None:
                return
            meeting = await self._meeting.convene(result.signal, self._context(bar.symbol))
            await self._after_meeting(meeting)

    def _context(self, symbol: str) -> MeetingContext:
        now = self._clock.now()
        last = self._history.last(symbol)
        return MeetingContext(
            now=now,
            mode=self._cfg.mode,
            instrument=self._instruments[symbol],
            bars=self._history.bars(symbol),
            quote=self._feed.latest_quote(symbol),
            snapshot=self._snapshot(now),
            last_bar_end=last.end if last else None,
        )

    def _snapshot(self, now: datetime) -> Any:
        return self._book.snapshot(
            now,
            kill_switch=self._killswitch.engaged(),
            kill_reason=self._killswitch.reason(),
            reconcile_pending=[item["id"] for item in self._reconcile_items()],
            degraded=list(self._degraded.values()),
        )

    async def _after_meeting(self, meeting: MeetingRecord) -> None:
        self._store.add_meeting(meeting)
        self._audit.append(
            "meeting",
            {
                "id": meeting.id,
                "symbol": meeting.symbol,
                "trigger": meeting.trigger,
                "decision": meeting.decision.value,
                "lots": meeting.lots,
                "reasons": meeting.reasons,
                "votes": {r.crew_id.value: r.vote.value for r in meeting.reports},
            },
        )
        at = meeting.called_at.isoformat()
        for report in meeting.reports:
            self._crew[report.crew_id] = {"vote": report.vote.value, "summary": report.summary, "at": at}
        self._hub.publish("meeting", {"id": meeting.id, "symbol": meeting.symbol, "decision": meeting.decision.value})
        if meeting.decision is not FinalAction.SKIP_ROUND:
            await self._execute_open(meeting)

    # ====================================================================================== orders

    def _write_ahead(self, intent: OrderIntent) -> Callable[[], None]:
        """Callback the venue runs (possibly on its GUI thread) right before the irreversible click."""

        def before_click() -> None:
            self._store.set_intent_status(intent.id, IntentStatus.SUBMITTING)
            self._audit.append("intent.status", {"id": intent.id, "status": "SUBMITTING"})

        return before_click

    def _new_intent(self, **fields: Any) -> OrderIntent:
        intent = OrderIntent(mode=self._cfg.mode, created_at=self._clock.now(), **fields)
        self._store.add_intent(intent)
        self._audit.append("intent.created", intent.model_dump(mode="json"))
        return intent

    def _record_report(self, intent: OrderIntent, report: ExecutionReport) -> ExecutionReport:
        """Persist the venue's report, forcing UNCONFIRMED if it contradicts the write-ahead marker."""
        row = self._store.get_intent(intent.id)
        current = row.status if row else IntentStatus.PENDING
        if current is IntentStatus.SUBMITTING and report.status in (IntentStatus.ABORTED, IntentStatus.DRY_RUN):
            # The click had been announced; the venue can't prove nothing happened -> be conservative.
            report = report.model_copy(
                update={
                    "status": IntentStatus.UNCONFIRMED,
                    "message": f"{report.message} (reported {report.status.value} after the write-ahead marker)",
                }
            )
        try:
            self._store.set_intent_status(intent.id, report.status, report)
        except ValueError:
            log.exception("could not record %s for intent %s", report.status, intent.id)
        self._audit.append(
            "intent.status",
            {
                "id": intent.id,
                "status": report.status.value,
                "message": report.message,
                "stage": report.stage,
                "fill_price": report.fill_price,
                "operator_abort": report.operator_abort,
            },
        )
        self._hub.publish("intent", {"id": intent.id, "status": report.status.value, "purpose": intent.purpose})
        return report

    async def _submit(self, intent: OrderIntent, call: Callable[[], Any]) -> ExecutionReport:
        self._in_flight.add(intent.id)
        try:
            report: ExecutionReport = await call()
        except Exception as exc:
            # Venues must not raise; if one does, we can't know whether it clicked.
            log.exception("venue raised for intent %s", intent.id)
            row = self._store.get_intent(intent.id)
            clicked = row is not None and row.status is IntentStatus.SUBMITTING
            now = self._clock.now()
            report = ExecutionReport(
                intent_id=intent.id,
                status=IntentStatus.UNCONFIRMED if clicked else IntentStatus.ABORTED,
                venue=self._venue.name,
                started_at=now,
                finished_at=now,
                message=f"venue error: {type(exc).__name__}: {exc}",
                stage="venue_exception",
            )
        finally:
            self._in_flight.discard(intent.id)
        report = self._record_report(intent, report)
        if report.operator_abort:
            self._killswitch.engage("operator fail-safe triggered during an order", by="fail-safe")
            self._hub.publish("kill", {"reason": "fail-safe"})
        return report

    async def _execute_open(self, meeting: MeetingRecord) -> None:
        plan = meeting.plan
        if plan is None or meeting.lots is None:
            return
        side = Side.BUY if meeting.decision is FinalAction.EXECUTE_BUY else Side.SELL
        intent = self._new_intent(
            purpose="open",
            meeting_id=meeting.id,
            symbol=meeting.symbol,
            side=side,
            lots=meeting.lots,
            entry_ref=plan.entry_ref,
            stop_price=plan.stop_price,
            target_price=plan.target_price,
        )
        quote = self._feed.latest_quote(meeting.symbol)
        report = await self._submit(intent, lambda: self._venue.submit(intent, quote, self._write_ahead(intent)))
        await self._apply_open(intent, report)

    async def _apply_open(self, intent: OrderIntent, report: ExecutionReport) -> None:
        label = f"{intent.side.value if intent.side else ''} {intent.lots} {intent.symbol}"
        status = report.status
        if status is IntentStatus.CONFIRMED:
            pos = self._book.add_open(
                intent,
                report,
                protective=self._venue.supports_protective_orders,
                at=report.clicked_at or self._clock.now(),
            )
            self._store.upsert_position(pos)
            self._audit.append("position.opened", pos.model_dump(mode="json"))
            self._hub.publish("position", {"id": pos.id, "status": pos.status.value})
            await self._alert(
                f"Opened {label}",
                f"entry {pos.entry_price} ({pos.entry_price_source}), SL {pos.stop_price}, TP {pos.target_price}",
            )
        elif status is IntentStatus.UNCONFIRMED:
            await self._alert(
                f"Order UNCONFIRMED: {label}",
                f"{report.message}. Check UniTrader and answer on the dashboard (filled / not filled). "
                "New trades are halted until you do.",
                "urgent",
                dedup_key=f"unconfirmed:{intent.id}",
            )
        elif status is IntentStatus.REJECTED:
            await self._alert(f"Order rejected: {label}", report.message, "high")
        elif status is IntentStatus.DRY_RUN:
            await self._alert(f"Dry run: would {label}", report.message, "low")
        else:
            await self._alert(f"Order not sent: {label}", f"{report.message} (stage {report.stage})", "default")

    # ====================================================================================== exits

    async def _process_exits(self, bar: Bar) -> None:
        dues = self._book.mark_bar(bar)
        for active in self._book.active():
            if active.symbol == bar.symbol:
                self._store.upsert_position(active)
        due_ids = {d.position.id for d in dues}
        for pos_id, reason in list(self._exit_retry.items()):
            retry = self._book.get(pos_id)
            if retry is None or retry.symbol != bar.symbol or retry.status is not PositionStatus.OPEN:
                self._exit_retry.pop(pos_id, None)
                continue
            if pos_id not in due_ids:
                dues.append(ExitDue(retry, reason, bar.close))
        for due in dues:
            await self._exit(due, bar)

    async def _exit(self, due: ExitDue, bar: Bar) -> None:
        pos = self._book.get(due.position.id)
        if pos is None or pos.status is not PositionStatus.OPEN:
            return
        if pos.protective_at_broker and due.reason in (ExitReason.STOP, ExitReason.TARGET):
            closed = self._book.close(pos.id, price=due.price, source="feed_estimate", reason=due.reason, at=bar.end)
            self._exit_retry.pop(pos.id, None)
            await self._closed(closed, note="broker-side stop/target assumed filled; confirm in UniTrader")
            return
        intent = self._new_intent(
            purpose="close",
            meeting_id=None,
            position_id=pos.id,
            symbol=pos.symbol,
            side=pos.side.opposite,
            lots=pos.lots,
            entry_ref=due.price,
            stop_price=None,
            target_price=None,
        )
        closing = self._book.set_status(pos.id, PositionStatus.CLOSING, close_intent_id=intent.id)
        self._store.upsert_position(closing)
        quote = self._feed.latest_quote(pos.symbol)
        report = await self._submit(
            intent, lambda: self._venue.close_position(closing, intent, quote, self._write_ahead(intent))
        )
        await self._apply_close(pos.id, intent, report, due.reason, due.price)

    async def _apply_close(
        self, position_id: str, intent: OrderIntent, report: ExecutionReport, reason: ExitReason, est_price: float
    ) -> None:
        status = report.status
        if status is IntentStatus.CONFIRMED:
            price = report.fill_price if report.fill_price is not None else est_price
            source = report.fill_price_source if report.fill_price is not None else "feed_estimate"
            closed = self._book.close(position_id, price=price, source=source, reason=reason, at=self._clock.now())
            self._exit_retry.pop(position_id, None)
            await self._closed(closed)
        elif status is IntentStatus.UNCONFIRMED:
            pos = self._book.set_status(position_id, PositionStatus.RECONCILE)
            self._store.upsert_position(pos)
            self._exit_retry.pop(position_id, None)
            await self._alert(
                f"Close UNCONFIRMED: {pos.symbol}",
                f"{report.message}. Check UniTrader: is the position closed? Answer on the dashboard.",
                "urgent",
                dedup_key=f"unconfirmed:{intent.id}",
            )
        else:
            pos = self._book.set_status(position_id, PositionStatus.OPEN)
            self._store.upsert_position(pos)
            self._exit_retry[position_id] = reason
            await self._alert(
                f"Could not close {pos.symbol} ({reason.value})",
                f"{report.message}. The desk retries at the next bar; close it in UniTrader yourself if needed.",
                "urgent",
                dedup_key=f"close-failed:{position_id}",
            )

    async def _closed(self, closed: Position, note: str = "") -> None:
        self._store.upsert_position(closed)
        self._audit.append("position.closed", closed.model_dump(mode="json"))
        self._hub.publish("position", {"id": closed.id, "status": closed.status.value})
        ccy = self._cfg.risk.account_currency
        pnl = closed.pnl or 0.0
        exit_reason = closed.exit_reason.value if closed.exit_reason else "?"
        await self._alert(
            f"Closed {closed.side.value} {closed.lots} {closed.symbol} ({exit_reason})",
            f"exit {closed.exit_price}, P&L est. {pnl:+.2f} {ccy}" + (f". {note}" if note else ""),
        )

    async def _check_daily_loss(self) -> None:
        now = self._clock.now()
        snap = self._snapshot(now)
        if not self._gate.daily_loss_breached(snap):
            return
        if self._daily_loss_alerted == now.date():
            return
        self._daily_loss_alerted = now.date()
        day_pnl = snap.realized_pnl_today + snap.unrealized_pnl
        await self._alert(
            "Daily loss limit reached",
            f"today's P&L est. {day_pnl:+.2f} {self._cfg.risk.account_currency}; no new trades today",
            "high",
        )
        self._audit.append("risk.daily_loss", {"pnl": day_pnl})
        if self._cfg.risk.flatten_on_daily_loss and self._book.active():
            await self._panic_locked(by="daily-loss", reason=ExitReason.DAILY_LOSS)

    # ====================================================================================== watchdog

    async def _watchdog(self) -> None:
        interval = (
            self._cfg.execution.unitrader.health_interval_seconds if self._cfg.execution.venue == "unitrader" else 30.0
        )
        while not self._stop.is_set():
            await self._clock.sleep(interval)
            try:
                await self.check_health()
            except Exception:
                log.exception("watchdog check failed")

    async def check_health(self) -> None:
        """One watchdog pass: venue health, feed staleness and external kill-switch changes."""
        await self._check_venue(preflight=False)
        now = self._clock.now()
        tf = timedelta(minutes=self._cfg.strategy.timeframe_minutes)
        limit = tf * self._cfg.feed.max_staleness_bars
        for inst in self._cfg.enabled_instruments:
            key = f"feed:{inst.symbol}"
            if not inst.in_session(now) or not self._calendar.check(inst, now).ok:
                self._degraded.pop(key, None)
                continue
            last = self._history.last(inst.symbol)
            if last is None or now - last.end > limit:
                age = (
                    "no bars yet"
                    if last is None
                    else f"last bar closed {(now - last.end).total_seconds() / 60:.0f} min ago"
                )
                self._set_degraded(key, f"market data for {inst.symbol} is stale ({age})")
                await self._alert("Market data stale", self._degraded[key], "high", dedup_key=key)
            else:
                self._degraded.pop(key, None)
        engaged = self._killswitch.engaged()
        if engaged != self._last_kill_state:
            self._last_kill_state = engaged
            self._hub.publish("kill" if engaged else "resume", {"source": "kill file"})
            self._audit.append("kill.state", {"engaged": engaged, "reason": self._killswitch.reason()})

    async def _check_venue(self, *, preflight: bool) -> None:
        try:
            health = await (self._venue.preflight() if preflight else self._venue.health())
        except Exception as exc:
            log.exception("venue health check raised")
            self._set_degraded("venue", f"venue health check failed: {exc}")
            return
        was_ok = self._venue_health.ok if self._venue_health else None
        self._venue_health = health
        if health.ok:
            if self._degraded.pop("venue", None) is not None:
                self._audit.append("health.recovered", {"venue": health.venue})
                self._hub.publish("health", {"ok": True})
                await self._alert("Terminal OK again", "all venue checks pass; trading can continue", "default")
            return
        details = "; ".join(f"{c.name}: {c.detail}" for c in health.failures)
        self._set_degraded("venue", f"execution venue not ready: {details}")
        if was_ok is not False:
            self._audit.append("health.failed", {"venue": health.venue, "failures": [c.name for c in health.failures]})
            self._hub.publish("health", {"ok": False})
            if self._cfg.mode in (Mode.DEMO, Mode.LIVE):
                account = "DEMO" if self._cfg.mode is Mode.DEMO else "LIVE"
                await self._alert(
                    "UniTrader needs you",
                    f"Trading is paused. If UniTrader is closed or logged out, log in to your {account} account "
                    f"yourself (the desk never handles your password). Details: {details}",
                    "urgent",
                    dedup_key="venue-health",
                )
            else:
                await self._alert("Venue not ready", details, "high", dedup_key="venue-health")

    def _set_degraded(self, key: str, message: str) -> None:
        if self._degraded.get(key) != message:
            log.warning("degraded: %s", message)
        self._degraded[key] = message

    # ====================================================================================== operator

    async def kill(self, reason: str, *, by: str) -> None:
        self._killswitch.engage(reason or "operator", by=by)
        self._last_kill_state = True
        self._audit.append("operator.kill", {"by": by, "reason": reason})
        self._hub.publish("kill", {"reason": reason, "by": by})
        await self._alert("Kill switch engaged", f"by {by}: {reason}. No new trades.", "high")

    async def resume(self, *, by: str) -> None:
        pending = self._reconcile_items()
        if pending:
            raise OperatorActionError(
                f"{len(pending)} order(s) still need reconciling; check UniTrader and answer them first"
            )
        self._killswitch.release(by=by)
        self._last_kill_state = False
        self._audit.append("operator.resume", {"by": by})
        self._hub.publish("resume", {"by": by})
        await self._alert("Trading resumed", f"by {by}", "default")

    async def panic(self, *, by: str) -> ExecutionReport | None:
        self._killswitch.engage(f"PANIC by {by}", by=by)
        self._last_kill_state = True
        self._hub.publish("kill", {"reason": "panic", "by": by})
        async with self._lock:
            return await self._panic_locked(by=by, reason=ExitReason.PANIC)

    async def _panic_locked(self, *, by: str, reason: ExitReason) -> ExecutionReport:
        self._audit.append("operator.panic", {"by": by, "reason": reason.value})
        intent = self._new_intent(
            purpose="close_all",
            meeting_id=None,
            symbol="*",
            side=None,
            lots=None,
            entry_ref=None,
            stop_price=None,
            target_price=None,
        )
        targets = [p for p in self._book.active() if p.status in (PositionStatus.OPEN, PositionStatus.CLOSING)]
        for pos in targets:
            self._store.upsert_position(
                self._book.set_status(pos.id, PositionStatus.CLOSING, close_intent_id=intent.id)
            )
        report = await self._submit(intent, lambda: self._venue.close_all(intent, self._write_ahead(intent)))
        now = self._clock.now()
        if report.status is IntentStatus.CONFIRMED:
            for pos in targets:
                current = self._book.get(pos.id)
                price = current.last_price if current and current.last_price else pos.entry_price
                closed = self._book.close(pos.id, price=price, source="feed_estimate", reason=reason, at=now)
                self._exit_retry.pop(pos.id, None)
                await self._closed(closed, note="closed by close-all; exit price is an estimate")
            await self._alert("PANIC: close-all sent", report.message, "urgent")
        elif report.status is IntentStatus.UNCONFIRMED:
            for pos in targets:
                self._store.upsert_position(self._book.set_status(pos.id, PositionStatus.RECONCILE))
            await self._alert(
                "PANIC close-all UNCONFIRMED",
                f"{report.message}. Check UniTrader and close anything still open yourself, then reconcile.",
                "urgent",
            )
        else:
            for pos in targets:
                self._store.upsert_position(self._book.set_status(pos.id, PositionStatus.OPEN))
            await self._alert(
                "PANIC could not close positions",
                f"{report.message}. Close open positions in UniTrader yourself. Kill switch is engaged.",
                "urgent",
            )
        return report

    async def reconcile(
        self,
        item_id: str,
        outcome: Literal["filled", "not_filled"],
        *,
        fill_price: float | None,
        by: str,
    ) -> None:
        if fill_price is not None and not fill_price > 0:
            raise OperatorActionError("fill price must be positive")
        async with self._lock:
            row = self._store.get_intent(item_id)
            if row is not None:
                if row.status not in _RECONCILE_STATUSES or item_id in self._in_flight:
                    raise OperatorActionError(f"order {item_id} does not need reconciling ({row.status.value})")
                await self._reconcile_intent(row.intent, row.report, outcome, fill_price)
            else:
                pos = self._book.get(item_id)
                if pos is None:
                    raise OperatorActionError(f"nothing to reconcile with id {item_id}")
                await self._reconcile_position(pos, outcome, fill_price)
            self._audit.append(
                "operator.reconcile", {"id": item_id, "outcome": outcome, "fill_price": fill_price, "by": by}
            )
            self._hub.publish("state", {"reason": "reconciled"})

    async def _reconcile_intent(
        self,
        intent: OrderIntent,
        report: ExecutionReport | None,
        outcome: Literal["filled", "not_filled"],
        fill_price: float | None,
    ) -> None:
        now = self._clock.now()
        filled = outcome == "filled"
        status = IntentStatus.RECONCILED_FILLED if filled else IntentStatus.RECONCILED_NOT_FILLED
        base = report or ExecutionReport(
            intent_id=intent.id, status=status, venue=self._venue.name, started_at=now, finished_at=now, message=""
        )
        resolved = base.model_copy(
            update={
                "status": status,
                "finished_at": now,
                "message": f"operator: {'filled' if filled else 'not filled'}",
                "fill_price": fill_price if fill_price is not None else base.fill_price,
                "fill_price_source": "operator" if fill_price is not None else base.fill_price_source,
            }
        )
        self._store.set_intent_status(intent.id, status, resolved)
        self._audit.append("intent.status", {"id": intent.id, "status": status.value, "by": "operator"})

        affected = [p for p in self._book.active() if p.close_intent_id == intent.id]
        if intent.purpose == "open":
            if filled:
                pos = self._book.add_open(intent, resolved, protective=self._venue.supports_protective_orders, at=now)
                self._store.upsert_position(pos)
                self._audit.append("position.opened", pos.model_dump(mode="json"))
        elif filled:
            for pos in affected:
                price = fill_price or pos.last_price or pos.entry_price
                source: Literal["operator", "feed_estimate"] = "operator" if fill_price else "feed_estimate"
                closed = self._book.close(pos.id, price=price, source=source, reason=ExitReason.RECONCILED, at=now)
                await self._closed(closed, note="confirmed by the operator")
        else:
            for pos in affected:
                self._store.upsert_position(self._book.set_status(pos.id, PositionStatus.OPEN))

    async def _reconcile_position(
        self, pos: Position, outcome: Literal["filled", "not_filled"], fill_price: float | None
    ) -> None:
        """For a position: 'filled' means 'it is closed at the broker', 'not_filled' means 'still open'."""
        if outcome == "filled":
            price = fill_price or pos.last_price or pos.entry_price
            source: Literal["operator", "feed_estimate"] = "operator" if fill_price else "feed_estimate"
            closed = self._book.close(
                pos.id, price=price, source=source, reason=ExitReason.MANUAL, at=self._clock.now()
            )
            self._exit_retry.pop(pos.id, None)
            await self._closed(closed, note="marked closed by the operator")
        else:
            self._store.upsert_position(self._book.set_status(pos.id, PositionStatus.OPEN))

    async def set_equity(self, equity: float, *, by: str) -> None:
        if not equity > 0:
            raise OperatorActionError("equity must be positive")
        now = self._clock.now()
        self._book.set_equity_base(equity, now)
        self._store.set_kv(EQUITY_KV, {"equity": equity, "at": now.isoformat(), "by": by})
        self._audit.append("operator.equity", {"equity": equity, "by": by})
        self._hub.publish("equity", {"equity": equity})

    async def manual_meeting(self, symbol: str, *, by: str, force_side: Side | None = None) -> MeetingRecord:
        if not self._cfg.dashboard.allow_manual_meetings:
            raise OperatorActionError("manual meetings are disabled (dashboard.allow_manual_meetings)")
        inst = self._instruments.get(symbol)
        if inst is None or not inst.enabled:
            raise OperatorActionError(f"{symbol} is not an enabled instrument")
        if force_side is not None and self._cfg.mode is not Mode.PAPER:
            raise OperatorActionError("forcing a direction is only allowed in paper mode")
        bars = self._history.bars(symbol)
        if not bars:
            raise OperatorActionError(f"no market data for {symbol} yet")
        async with self._lock:
            self._audit.append("operator.meeting", {"symbol": symbol, "by": by, "force_side": force_side})
            if force_side is not None:
                try:
                    signal = build_forced_signal(bars, force_side, self._cfg.strategy)
                except ValueError as exc:
                    raise OperatorActionError(str(exc)) from exc
                meeting = await self._meeting.convene(signal, self._context(symbol), trigger="manual")
            else:
                result = self._scout.evaluate(bars)
                if result.signal is not None:
                    meeting = await self._meeting.convene(result.signal, self._context(symbol), trigger="manual")
                else:
                    meeting = await self._meeting.no_signal_round(symbol, self._context(symbol), result)
            await self._after_meeting(meeting)
            return meeting

    # ====================================================================================== state

    def _reconcile_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for row in self._store.intents_with_status(*_RECONCILE_STATUSES):
            if row.intent.id in self._in_flight:
                continue
            it = row.intent
            items.append(
                {
                    "id": it.id,
                    "kind": "intent",
                    "purpose": it.purpose,
                    "symbol": it.symbol,
                    "side": it.side.value if it.side else None,
                    "lots": str(it.lots) if it.lots is not None else None,
                    "status": row.status.value,
                    "created_at": it.created_at.isoformat(),
                    "message": row.report.message
                    if row.report
                    else "the desk restarted while this order was in flight",
                }
            )
        linked = {item["id"] for item in items}
        for pos in self._book.active():
            if pos.status is PositionStatus.RECONCILE and pos.close_intent_id not in linked:
                items.append(
                    {
                        "id": pos.id,
                        "kind": "position",
                        "purpose": "close",
                        "symbol": pos.symbol,
                        "side": pos.side.value,
                        "lots": str(pos.lots),
                        "status": pos.status.value,
                        "created_at": pos.opened_at.isoformat(),
                        "message": "is this position still open in UniTrader?",
                    }
                )
        return items

    def _position_view(self, pos: Position) -> dict[str, Any]:
        view = pos.model_dump(mode="json")
        if pos.last_price is not None:
            inst = self._instruments[pos.symbol]
            view["unrealized_pnl"] = round(estimate_pnl(pos.side, pos.lots, pos.entry_price, pos.last_price, inst), 2)
        else:
            view["unrealized_pnl"] = None
        return view

    def state(self) -> dict[str, Any]:
        now = self._clock.now()
        snap = self._snapshot(now)
        ks = self._killswitch.state()
        day_pnl = snap.realized_pnl_today + snap.unrealized_pnl
        loss_pct = (-day_pnl / snap.day_start_equity * 100) if snap.day_start_equity > 0 and day_pnl < 0 else 0.0
        feed_status: dict[str, Any]
        try:
            feed_status = self._feed.status()
        except Exception as exc:  # noqa: BLE001 - status must never break the dashboard
            feed_status = {"last_error": str(exc)}
        instruments = []
        for inst in self._cfg.instruments:
            last = self._history.last(inst.symbol)
            instruments.append(
                {
                    "symbol": inst.symbol,
                    "enabled": inst.enabled,
                    "specs_verified": inst.specs_verified,
                    "in_session": inst.in_session(now),
                    "last_bar": {"start": last.start.isoformat(), "close": last.close} if last else None,
                }
            )
        return {
            "mode": self._cfg.mode.value,
            "venue": self._venue.name,
            "dry_run": self._cfg.execution.dry_run,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "now": now.isoformat(),
            "kill_switch": {"engaged": ks["engaged"], "reason": ks["reason"], "by": ks["by"], "at": ks["at"]},
            "reconcile": self._reconcile_items(),
            "degraded": list(self._degraded.values()),
            "health": {
                "venue": self._venue_health.model_dump(mode="json") if self._venue_health else None,
                "feed": feed_status,
            },
            "equity": {
                "estimate": round(snap.equity, 2),
                "day_start": round(snap.day_start_equity, 2),
                "realized_today": round(snap.realized_pnl_today, 2),
                "unrealized": round(snap.unrealized_pnl, 2),
                "currency": self._cfg.risk.account_currency,
            },
            "limits": {
                "trades_today": snap.trades_today,
                "max_trades_per_day": self._cfg.risk.max_trades_per_day,
                "open_positions": len(snap.open_positions),
                "max_open_positions": self._cfg.risk.max_open_positions,
                "daily_loss_pct": round(loss_pct, 3),
                "max_daily_loss_pct": self._cfg.risk.max_daily_loss_pct,
            },
            "positions": [self._position_view(p) for p in self._book.active()],
            "crew": [
                {"id": c.value, **CREW_DISPLAY[c], **self._crew[c]}
                for c in (
                    CrewId.CYAN_SCOUT,
                    CrewId.LIME_CHARTS,
                    CrewId.YELLOW_NEWS,
                    CrewId.RED_SKEPTIC,
                    CrewId.PINK_SENTIMENT,
                    CrewId.BLACK_CHIEF,
                )
            ],
            "meetings": [m.model_dump(mode="json") for m in self._store.recent_meetings(20)],
            "intents": [
                {
                    "intent": r.intent.model_dump(mode="json"),
                    "status": r.status.value,
                    "report": r.report.model_dump(mode="json") if r.report else None,
                }
                for r in self._store.recent_intents(20)
            ],
            "instruments": instruments,
            "llm": self._reviewer.usage_today(),
        }

    # ====================================================================================== alerts

    async def _alert(
        self, title: str, message: str, priority: Priority = "default", *, dedup_key: str | None = None
    ) -> None:
        self._hub.publish("alert", {"title": title, "message": message, "priority": priority})
        try:
            await self._notifier.notify(title, message, priority=priority, dedup_key=dedup_key)
        except Exception:  # notifiers must not raise, but never let an alert break trading
            log.exception("notifier failed")
