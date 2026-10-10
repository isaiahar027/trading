"""The desk engine: feed → crew meeting → write-ahead intent → venue → position book → exits.

Safety properties the engine maintains:

* Every order is persisted as PENDING before the venue is called, and as SUBMITTING (write-ahead) right
  before the irreversible click. On restart PENDING becomes ABORTED (never reached the venue) and
  SUBMITTING/UNCONFIRMED become *reconcile items*. New trades stay blocked until the operator answers them.
* Orders are never retried automatically. Only a close that provably did nothing (ABORTED before the
  click) is sent again on the next bar; a close that was clicked and not confirmed goes to the operator.
* The desk never infers a fill: when the feed reaches a broker-side SL/TP, the position is put to the
  operator ("is it still open?") instead of being booked as closed.
* The kill switch blocks new entries only; exits and PANIC still run. Engaging it abandons a crew meeting
  that is still waiting for Claude, so PANIC never queues behind a slow review.
* The desk never logs in to the broker. When the venue reports the terminal missing or the wrong account,
  trading halts and the operator is asked to log in themselves.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from .audit import AuditLog
from .clock import Clock
from .config import ConfigError, DeskConfig, InstrumentConfig
from .crew import MeetingContext
from .domain import (
    CREW_DISPLAY,
    Bar,
    BreakoutSignal,
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
from .money import estimate_pnl, trading_day_start
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
_FILLED_STATUSES = (IntentStatus.CONFIRMED, IntentStatus.RECONCILED_FILLED)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


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
        # Exits that are due but not sent yet: a close that ABORTED before its click, or an exit that became
        # due while the desk was down. Sent on the next bar of the symbol.
        self._pending_exits: dict[str, ExitReason] = {}
        self._meeting_task: asyncio.Task[MeetingRecord] | None = None
        self._crew: dict[CrewId, dict[str, Any]] = {
            c: {"vote": Vote.HOLD.value, "summary": "", "at": None} for c in CrewId
        }
        self._daily_loss_alerted: datetime | None = None  # trading day of the last daily-loss alert
        self._daily_loss_flattened: datetime | None = None  # trading day the daily-loss flatten went out
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

    def managed_instruments(self) -> list[InstrumentConfig]:
        """Instruments the desk needs bars for: the enabled ones plus any with an active position.

        ``enabled: false`` means "no new entries"; an open position on such an instrument keeps its exits.
        """
        held = {p.symbol for p in self._book.active()}
        return [i for i in self._cfg.instruments if i.enabled or i.symbol in held]

    async def start(self) -> None:
        now = self._clock.now()
        self._started_at = now
        self._load_book(now)
        unknown = sorted({p.symbol for p in self._book.active()} - set(self._instruments))
        if unknown:
            raise ConfigError(
                f"the desk has active positions on {', '.join(unknown)}, which are not in the config; add the "
                "instrument(s) back (enabled: false is enough) so their exits keep running"
            )
        await self._startup_reconciliation()
        symbols = [i.symbol for i in self.managed_instruments()]
        try:
            warm = await self._feed.warmup(symbols, self._history_size())
        except Exception as exc:
            self._set_degraded("feed", f"market data warmup failed: {exc}")
            log.exception("feed warmup failed")
            warm = {}
        for bars in warm.values():
            for bar in bars:
                self._history.add(bar)
        await self._catch_up(warm)
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
        # Without an operator equity sync, equity is starting equity + ALL realized P&L, so load everything.
        since = _EPOCH if base_at is None else min(base_at, now - timedelta(days=HISTORY_DAYS))
        history = self._store.closed_positions_since(since)
        self._book = PositionBook(
            self._cfg, self._store.active_positions(), history, equity_base=base_equity, equity_base_at=base_at
        )

    async def _startup_reconciliation(self) -> None:
        """Repair what a crash between two writes can leave behind, never guessing in the risky direction."""
        for row in self._store.intents_with_status(IntentStatus.PENDING):
            self._store.set_intent_status(row.intent.id, IntentStatus.ABORTED)
            self._audit.append(
                "intent.status", {"id": row.intent.id, "status": "ABORTED", "why": "restart: never sent"}
            )
        unresolved = {row.intent.id for row in self._store.intents_with_status(*_RECONCILE_STATUSES)}
        now = self._clock.now()
        for pos in self._book.active():
            if pos.status is not PositionStatus.CLOSING or pos.close_intent_id in unresolved:
                continue  # an unresolved close stays CLOSING: its order is a reconcile item
            close_row = self._store.get_intent(pos.close_intent_id) if pos.close_intent_id else None
            status = close_row.status if close_row else None
            if status in _FILLED_STATUSES:
                # The close was confirmed but the desk stopped before it recorded the position as closed.
                price = pos.last_price or pos.entry_price
                closed = self._book.close(
                    pos.id, price=price, source="feed_estimate", reason=ExitReason.RECONCILED, at=now
                )
                await self._closed(closed, note="its close was confirmed before the restart; exit price estimated")
            elif status is IntentStatus.REJECTED:
                note = "its close was clicked before the restart and the terminal showed an error"
                await self._ask_operator(pos, note)
            else:  # the close never reached the venue: the position is still open
                self._store.upsert_position(self._book.set_status(pos.id, PositionStatus.OPEN))
        known = self._store.position_intent_ids()
        for row in self._store.intents_with_status(*_FILLED_STATUSES):
            if row.intent.purpose != "open" or row.intent.id in known or row.report is None:
                continue
            # A fill was recorded but the desk stopped before it recorded the position.
            try:
                pos = self._book.add_unmanaged(
                    row.intent,
                    row.report,
                    at=row.report.clicked_at or row.updated_at,
                    note="this order filled just before the desk stopped and its position was never recorded",
                )
            except ValueError as exc:
                log.critical("cannot rebuild the position of filled order %s: %s", row.intent.id, exc)
                self._set_degraded(f"orphan:{row.intent.id}", f"order {row.intent.id} filled but has no position")
                await self._alert(
                    f"Unrecorded position: {row.intent.symbol}",
                    f"order {row.intent.id} filled but the desk has no record of its position ({exc}). Check "
                    "UniTrader and close it yourself if it is still open.",
                    "urgent",
                )
                continue
            self._store.upsert_position(pos)
            self._audit.append("position.recovered", pos.model_dump(mode="json"))
            await self._alert(
                f"Check {pos.symbol}: position recovered after a restart",
                f"{pos.reconcile_note}. Is it still open in UniTrader? Answer on the dashboard.",
                "urgent",
                dedup_key=f"check:{pos.id}",
            )
        if unresolved:
            log.warning("%d order(s) need reconciliation after restart: %s", len(unresolved), sorted(unresolved))

    async def _catch_up(self, warm: dict[str, list[Bar]]) -> None:
        """Apply bars that closed while the desk was down to the open positions.

        Broker-side SL/TP levels crossed meanwhile are put to the operator; software exits that became due
        are sent on the first live bar (at the price then, not at a stale one).
        """
        bars = sorted((b for series in warm.values() for b in series), key=lambda b: (b.end, b.symbol))
        for bar in bars:
            for due in self._book.mark_bar(bar):
                if due.position.protective_at_broker and due.reason in (ExitReason.STOP, ExitReason.TARGET):
                    await self._protective_level_reached(due, while_down=True)
                else:
                    self._pending_exits.setdefault(due.position.id, due.reason)
        for pos in self._book.active():
            self._store.upsert_position(pos)
        if self._pending_exits:
            log.warning("exits due from bars that closed while the desk was down: %s", self._pending_exits)

    # ====================================================================================== bars

    async def on_bar(self, bar: Bar) -> None:
        inst = self._instruments.get(bar.symbol)
        if inst is None:
            return
        if not inst.enabled and not any(p.symbol == bar.symbol for p in self._book.active()):
            return
        if not self._history.add(bar):
            return
        self._degraded.pop(f"feed:{bar.symbol}", None)
        self._hub.publish("bar", {"symbol": bar.symbol, "start": bar.start.isoformat(), "close": bar.close})
        async with self._lock:
            await self._process_exits(bar)
            await self._check_daily_loss()
            if not inst.enabled:
                return  # exits only: a disabled instrument takes no new entries
            age = self._clock.now() - bar.end
            if age >= self._timeframe():
                # A newer bar has already closed (feed catch-up): trading this one would size the plan on an
                # old close and send it at today's price.
                self._crew[CrewId.CYAN_SCOUT] = {
                    "vote": Vote.HOLD.value,
                    "summary": f"{bar.symbol}: bar closed {age.total_seconds() / 60:.0f} min ago; not trading on it",
                    "at": self._clock.now().isoformat(),
                }
                return
            result = self._scout.evaluate(self._history.bars(bar.symbol))
            self._crew[CrewId.CYAN_SCOUT] = {
                "vote": Vote.PROCEED.value if result.signal else Vote.HOLD.value,
                "summary": f"{bar.symbol}: {result.reason}",
                "at": self._clock.now().isoformat(),
            }
            if result.signal is None:
                return
            meeting = await self._convene(result.signal, bar.symbol)
            if meeting is not None:
                await self._after_meeting(meeting, signal_bar_end=bar.end)

    def _timeframe(self) -> timedelta:
        return timedelta(minutes=self._cfg.strategy.timeframe_minutes)

    async def _convene(
        self, signal: BreakoutSignal, symbol: str, *, trigger: Literal["breakout", "manual"] = "breakout"
    ) -> MeetingRecord | None:
        """Hold the meeting as a task that kill/panic can cancel; ``None`` if it was abandoned."""
        task = asyncio.create_task(
            self._meeting.convene(signal, self._context(symbol), trigger=trigger), name=f"meeting-{symbol}"
        )
        self._meeting_task = task
        try:
            await asyncio.wait({task})
        finally:
            self._meeting_task = None
            if not task.done():  # we are being cancelled ourselves (shutdown)
                task.cancel()
        if task.cancelled():
            log.warning("%s meeting abandoned: the kill switch was engaged during the review", symbol)
            self._audit.append("meeting.abandoned", {"symbol": symbol, "why": "kill switch engaged during the review"})
            return None
        return task.result()

    def _abandon_meeting(self) -> None:
        task = self._meeting_task
        if task is not None and not task.done():
            task.cancel()

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

    async def _after_meeting(self, meeting: MeetingRecord, *, signal_bar_end: datetime | None = None) -> None:
        self._store.add_meeting(meeting)
        self._audit.append(
            "meeting",
            {
                "id": meeting.id,
                "symbol": meeting.symbol,
                "trigger": meeting.trigger,
                "decision": meeting.decision.value,
                "bar_close": meeting.signal.detected_at if meeting.signal else None,
                "called_at": meeting.called_at,
                "lots": meeting.lots,
                "reasons": meeting.reasons,
                "votes": {r.crew_id.value: r.vote.value for r in meeting.reports},
            },
        )
        at = meeting.called_at.isoformat()
        for report in meeting.reports:
            self._crew[report.crew_id] = {"vote": report.vote.value, "summary": report.summary, "at": at}
        self._hub.publish("meeting", {"id": meeting.id, "symbol": meeting.symbol, "decision": meeting.decision.value})
        if meeting.decision is FinalAction.SKIP_ROUND:
            return
        if signal_bar_end is not None:
            age = self._clock.now() - signal_bar_end
            if age >= self._timeframe():
                # The review took so long that the next bar has closed: the plan's entry and stop are stale.
                self._audit.append("meeting.stale", {"id": meeting.id, "age_seconds": age.total_seconds()})
                await self._alert(
                    f"Skipped {meeting.symbol}: the plan went stale",
                    f"the crew decided {age.total_seconds():.0f} s after the bar closed; not sending an old plan",
                    "default",
                )
                return
        await self._execute_open(meeting)

    # ====================================================================================== orders

    def _write_ahead(self, intent: OrderIntent) -> Callable[[], None]:
        """Callback the venue runs right before the irreversible click (event-loop or worker thread)."""

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
                "fill_price_source": report.fill_price_source,
                "operator_abort": report.operator_abort,
                "started_at": report.started_at,
                "clicked_at": report.clicked_at,
                "finished_at": report.finished_at,
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
        for pos_id, reason in list(self._pending_exits.items()):
            pending = self._book.get(pos_id)
            if pending is None or pending.status is not PositionStatus.OPEN:
                self._pending_exits.pop(pos_id, None)
                continue
            if pending.symbol == bar.symbol and pos_id not in due_ids:
                dues.append(ExitDue(pending, reason, bar.close))
        for due in dues:
            await self._exit(due, bar)

    async def _exit(self, due: ExitDue, bar: Bar) -> None:
        pos = self._book.get(due.position.id)
        if pos is None or pos.status is not PositionStatus.OPEN:
            return
        if pos.protective_at_broker and due.reason in (ExitReason.STOP, ExitReason.TARGET):
            await self._protective_level_reached(due, while_down=False)
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

    async def _protective_level_reached(self, due: ExitDue, *, while_down: bool) -> None:
        """The feed reached a broker-side SL/TP. The feed is not the broker's price, so don't book it: ask.

        In paper mode the simulated broker fills on this same feed, so there the level IS the fill.
        """
        pos = due.position
        if self._cfg.execution.venue == "paper":
            at = self._clock.now() if while_down else (pos.last_marked_bar_end or self._clock.now())
            closed = self._book.close(pos.id, price=due.price, source="feed_estimate", reason=due.reason, at=at)
            self._pending_exits.pop(pos.id, None)
            await self._closed(closed, note="simulated broker-side stop/target")
            return
        level = pos.stop_price if due.reason is ExitReason.STOP else pos.target_price
        when = " while the desk was down" if while_down else ""
        note = (
            f"the feed reached its {'stop' if due.reason is ExitReason.STOP else 'target'} {level}{when}; the "
            "broker's SL/TP has probably closed it, but the desk can't see UniTrader"
        )
        self._pending_exits.pop(pos.id, None)
        await self._ask_operator(pos, note, suggested_exit=due)

    async def _ask_operator(self, pos: Position, note: str, *, suggested_exit: ExitDue | None = None) -> None:
        """Put a position to the operator: it keeps counting as exposure and new entries wait for the answer."""
        updated = self._book.set_status(pos.id, PositionStatus.RECONCILE, note=note, suggested_exit=suggested_exit)
        self._store.upsert_position(updated)
        self._audit.append("position.check", {"id": pos.id, "note": note})
        self._hub.publish("position", {"id": pos.id, "status": updated.status.value})
        await self._alert(
            f"Check {pos.symbol} {pos.side.value} {pos.lots} in UniTrader",
            f"{note}. Is it still open? Answer on the dashboard; new trades wait for your answer.",
            "urgent",
            dedup_key=f"check:{pos.id}",
        )

    async def _apply_close(
        self, position_id: str, intent: OrderIntent, report: ExecutionReport, reason: ExitReason, est_price: float
    ) -> None:
        status = report.status
        if status is IntentStatus.CONFIRMED:
            price = report.fill_price if report.fill_price is not None else est_price
            source = report.fill_price_source if report.fill_price is not None else "feed_estimate"
            closed = self._book.close(position_id, price=price, source=source, reason=reason, at=self._clock.now())
            self._pending_exits.pop(position_id, None)
            await self._closed(closed)
        elif status is IntentStatus.UNCONFIRMED:
            pos = self._book.set_status(
                position_id, PositionStatus.RECONCILE, note=f"its {reason.value} close could not be confirmed"
            )
            self._store.upsert_position(pos)
            self._pending_exits.pop(position_id, None)
            await self._alert(
                f"Close UNCONFIRMED: {pos.symbol}",
                f"{report.message}. Check UniTrader: is the position closed? Answer on the dashboard.",
                "urgent",
                dedup_key=f"unconfirmed:{intent.id}",
            )
        elif status in (IntentStatus.ABORTED, IntentStatus.DRY_RUN):
            # Nothing was clicked, so sending the close again on the next bar is safe.
            pos = self._book.set_status(position_id, PositionStatus.OPEN)
            self._store.upsert_position(pos)
            self._pending_exits[position_id] = reason
            await self._alert(
                f"Could not close {pos.symbol} ({reason.value})",
                f"{report.message}. The desk retries at the next bar; close it in UniTrader yourself if needed.",
                "urgent",
                dedup_key=f"close-failed:{position_id}",
            )
        else:
            # REJECTED: the close WAS clicked and the terminal showed an error. Clicking again could open a
            # reverse position on a netting account, so the operator decides.
            self._pending_exits.pop(position_id, None)
            current = self._book.get(position_id)
            if current is not None:
                await self._ask_operator(
                    current, f"its {reason.value} close was clicked and the terminal showed an error"
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
        day = trading_day_start(now, self._cfg.risk.day_rollover_utc)
        if self._daily_loss_alerted != day:
            self._daily_loss_alerted = day
            day_pnl = snap.realized_pnl_today + snap.unrealized_pnl
            await self._alert(
                "Daily loss limit reached",
                f"today's P&L est. {day_pnl:+.2f} {self._cfg.risk.account_currency}; no new trades today",
                "high",
            )
            self._audit.append("risk.daily_loss", {"pnl": day_pnl})
        flattenable = [p for p in self._book.active() if p.status in (PositionStatus.OPEN, PositionStatus.CLOSING)]
        if self._cfg.risk.flatten_on_daily_loss and flattenable and self._daily_loss_flattened != day:
            report = await self._panic_locked(by="daily-loss", reason=ExitReason.DAILY_LOSS)
            if report.status in (IntentStatus.CONFIRMED, IntentStatus.UNCONFIRMED):
                self._daily_loss_flattened = day  # otherwise (nothing was clicked) try again on the next bar

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
        for inst in self.managed_instruments():
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
            needs_login = any(c.data.get("needs_login") for c in health.failures)
            if self._cfg.mode in (Mode.DEMO, Mode.LIVE) and needs_login:
                account = "DEMO" if self._cfg.mode is Mode.DEMO else "LIVE"
                await self._alert(
                    "UniTrader needs you",
                    f"Trading is paused. Open UniTrader and log in to your {account} account yourself (the desk "
                    f"never handles your password). Details: {details}",
                    "urgent",
                    dedup_key="venue-health",
                )
            elif self._cfg.mode in (Mode.DEMO, Mode.LIVE):
                await self._alert(
                    "Terminal not ready",
                    f"Trading is paused until every check passes. Details: {details}",
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
        self._abandon_meeting()
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
        self._abandon_meeting()  # don't wait for a Claude review to finish before closing everything
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
            closed_positions = []
            for pos in targets:  # record every close before any (slow) alert goes out
                current = self._book.get(pos.id)
                price = current.last_price if current and current.last_price else pos.entry_price
                closed = self._book.close(pos.id, price=price, source="feed_estimate", reason=reason, at=now)
                self._pending_exits.pop(pos.id, None)
                self._store.upsert_position(closed)
                closed_positions.append(closed)
            for closed in closed_positions:
                await self._closed(closed, note="closed by close-all; exit price is an estimate")
            await self._alert("PANIC: close-all sent", report.message, "urgent")
        elif report.status is IntentStatus.UNCONFIRMED:
            for pos in targets:
                self._store.upsert_position(
                    self._book.set_status(pos.id, PositionStatus.RECONCILE, note="close-all could not be confirmed")
                )
            await self._alert(
                "PANIC close-all UNCONFIRMED",
                f"{report.message}. Check UniTrader and close anything still open yourself, then reconcile.",
                "urgent",
            )
        else:
            # Nothing was clicked: put every position back exactly as it was (a CLOSING position keeps the link
            # to its own unresolved close order, so reconciling that order still finds it).
            for pos in targets:
                self._book.restore(pos)
                self._store.upsert_position(pos)
            halted = " The kill switch is engaged." if self._killswitch.engaged() else ""
            await self._alert(
                "PANIC could not close positions",
                f"{report.message}. Close open positions in UniTrader yourself.{halted}",
                "urgent",
                dedup_key="panic-failed",
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
        if fill_price is not None and not (math.isfinite(fill_price) and fill_price > 0):
            raise OperatorActionError("fill price must be a positive number")
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
                if pos.status is not PositionStatus.RECONCILE:
                    raise OperatorActionError(
                        f"position {item_id} is {pos.status.value}, not waiting for a reconcile answer"
                    )
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
        if intent.purpose == "open":
            if not filled:
                self._store.set_intent_status(intent.id, status, resolved)
                self._audit.append("intent.status", {"id": intent.id, "status": status.value, "by": "operator"})
                return
            if resolved.fill_price is None and intent.entry_ref is None:
                raise OperatorActionError("the desk has no price for this order: enter the fill price from UniTrader")
            if intent.stop_price is not None and intent.target_price is not None:
                pos = self._book.add_open(intent, resolved, protective=self._venue.supports_protective_orders, at=now)
            else:  # a test order: no stop/target, so the desk only tracks it and keeps asking about it
                pos = self._book.add_unmanaged(
                    intent, resolved, at=now, note="a test order you confirmed as filled; close it in UniTrader"
                )
            try:
                self._store.resolve_intent(intent.id, status, resolved, [pos])
            except Exception:
                self._book.remove(pos.id)
                raise
            self._audit.append("intent.status", {"id": intent.id, "status": status.value, "by": "operator"})
            self._audit.append("position.opened", pos.model_dump(mode="json"))
            self._hub.publish("position", {"id": pos.id, "status": pos.status.value})
            return

        linked = [p for p in self._book.active() if p.close_intent_id == intent.id]
        if not filled:
            # Only positions still linked to THIS close go back to OPEN (a later close-all may own them now).
            reopened = [self._book.set_status(p.id, PositionStatus.OPEN) for p in linked]
            self._store.resolve_intent(intent.id, status, resolved, reopened)
            self._audit.append("intent.status", {"id": intent.id, "status": status.value, "by": "operator"})
            return
        affected = linked + [
            p for p in self._book.active() if p.id == intent.position_id and p.close_intent_id != intent.id
        ]
        closed_positions = []
        for pos in affected:
            price = fill_price or pos.last_price or pos.entry_price
            source: Literal["operator", "feed_estimate"] = "operator" if fill_price else "feed_estimate"
            closed_positions.append(
                self._book.close(pos.id, price=price, source=source, reason=ExitReason.RECONCILED, at=now)
            )
            self._pending_exits.pop(pos.id, None)
        self._store.resolve_intent(intent.id, status, resolved, closed_positions)
        self._audit.append("intent.status", {"id": intent.id, "status": status.value, "by": "operator"})
        for closed in closed_positions:
            await self._closed(closed, note="confirmed by the operator")

    async def _reconcile_position(
        self, pos: Position, outcome: Literal["filled", "not_filled"], fill_price: float | None
    ) -> None:
        """For a position: 'filled' means 'it is closed at the broker', 'not_filled' means 'still open'."""
        if outcome == "filled":
            price = fill_price or pos.suggested_exit_price or pos.last_price or pos.entry_price
            source: Literal["operator", "feed_estimate"] = "operator" if fill_price else "feed_estimate"
            reason = pos.suggested_exit_reason or ExitReason.MANUAL
            closed = self._book.close(pos.id, price=price, source=source, reason=reason, at=self._clock.now())
            self._pending_exits.pop(pos.id, None)
            await self._closed(closed, note="marked closed by the operator")
        else:
            self._store.upsert_position(self._book.set_status(pos.id, PositionStatus.OPEN))

    async def set_equity(self, equity: float, *, by: str) -> None:
        if not (math.isfinite(equity) and equity > 0):
            raise OperatorActionError("equity must be a positive number")
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
                held: MeetingRecord | None = await self._convene(signal, symbol, trigger="manual")
            else:
                result = self._scout.evaluate(bars)
                if result.signal is not None:
                    held = await self._convene(result.signal, symbol, trigger="manual")
                else:
                    held = await self._meeting.no_signal_round(symbol, self._context(symbol), result)
            if held is None:
                raise OperatorActionError("the meeting was abandoned because the kill switch was engaged")
            await self._after_meeting(held)
            return held

    # ====================================================================================== state

    def _reconcile_items(self) -> list[dict[str, Any]]:
        """What the operator must check in UniTrader. Each item carries the question and the meaning of its
        two answers, so the dashboard never has to guess what "filled" means for it."""
        items: list[dict[str, Any]] = []
        for row in self._store.intents_with_status(*_RECONCILE_STATUSES):
            if row.intent.id in self._in_flight:
                continue
            it = row.intent
            what = f"{it.side.value if it.side else ''} {it.lots} {it.symbol}".strip()
            if it.purpose == "open":
                question = f"Did this {what} order open a position in UniTrader?"
                options = _options("Yes, a position opened", "No, nothing opened")
            elif it.purpose == "close":
                question = f"Did this close ({what}) go through in UniTrader?"
                options = _options("Yes, it is closed", "No, it is still open")
            else:
                question = "Did close-all go through in UniTrader (no desk positions left open)?"
                options = _options("Yes, everything closed", "No, positions are still open")
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
                    "question": question,
                    "options": options,
                }
            )
        linked = {item["id"] for item in items}
        for pos in self._book.active():
            if pos.status is PositionStatus.RECONCILE and pos.close_intent_id not in linked:
                note = pos.reconcile_note or "the desk can't tell whether it is still open"
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
                        "message": note,
                        "question": f"Is this {pos.side.value} {pos.lots} {pos.symbol} position still open in "
                        "UniTrader?",
                        # For a position, "filled" = its close happened (it is closed); "not_filled" = still open.
                        "options": _options("No, it is closed", "Yes, it is still open"),
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


def _options(filled_label: str, not_filled_label: str) -> list[dict[str, Any]]:
    return [
        {"outcome": "filled", "label": filled_label, "needs_price": True},
        {"outcome": "not_filled", "label": not_filled_label, "needs_price": False},
    ]
