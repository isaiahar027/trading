"""UniTrader execution venue: guarded GUI automation of the operator's own, logged-in terminal.

UniTrader has no API, so an order is a mouse click on the operator's screen. This venue only clicks
after it has verified, from the screen and against the operator's calibration (``desk calibrate``),
that it is about to click the right button on the right panel of the right window on the right
account, and it reports honestly what it could and could not verify afterwards:

* anything wrong **before** the click gives ``ABORTED`` — nothing reached the broker;
* once the click happened the result is ``CONFIRMED``, ``REJECTED`` or ``UNCONFIRMED`` — never
  ``ABORTED``; when in doubt it is ``UNCONFIRMED`` and the operator reconciles;
* an order click is never retried.

Every coordinate, region and reference image comes from the calibrated :class:`LayoutFile`; nothing
about the real terminal is assumed here. The desk never logs in: when the window or the account badge
is missing, it stops and tells the operator to log in themselves.

All GUI work runs on ONE dedicated worker thread (``ThreadPoolExecutor(max_workers=1)``) behind a
``threading.Lock``, so orders, closes and health checks never interleave and the event loop never
blocks. ``on_before_click`` (the engine's write-ahead marker) is executed on the event-loop thread
while the worker waits for it, immediately before the click.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, Literal, NoReturn

from PIL import Image

from ...clock import Clock
from ...config import InstrumentConfig, UniTraderConfig
from ...domain import (
    ExecutionReport,
    HealthCheck,
    IntentStatus,
    Mode,
    OrderIntent,
    Position,
    PriceSource,
    Quote,
    Side,
    VenueHealth,
)
from ...money import format_lots, parse_lots
from ..base import BeforeClick, HaltCheck
from . import vision
from .backend import BackendError, GuiBackend, OperatorAbortError, WindowInfo
from .layout import DialogLayout, LayoutFile, PanelLayout, Point, Region, TemplateRef

log = logging.getLogger(__name__)

#: Seconds between the two baseline captures of the positions indicator (step 7).
STABILITY_INTERVAL_SECONDS = 0.3
#: Consecutive polls that must show the indicator changed before a fill counts as confirmed.
CONFIRM_SAMPLES = 2
#: Largest mean-colour difference (0..255 per channel) a template match tolerates (see vision.mean_color_delta).
MAX_TEMPLATE_COLOR_DELTA = 32.0
#: Spacing / cap of the grid of points probed with ``root_window_at`` to prove nothing covers a region.
GRID_STEP_PX = 20
GRID_MAX_POINTS = 32
#: Real seconds the worker waits for the write-ahead hook to run on the event loop.
WRITE_AHEAD_TIMEOUT_SECONDS = 10.0
#: Real seconds added to ``max_order_seconds`` before the event loop gives up on a stuck worker.
HARD_TIMEOUT_GRACE_SECONDS = 5.0
#: Typed into an SL/TP field the desk must empty, and read back, before it is deleted again: an empty field copies
#: nothing, exactly like a field whose copy does not work, so the probe proves the field takes keys and copies.
CLEAR_PROBE = "0"

_VENUE = "unitrader"


class _Abort(Exception):
    """Stop the flow before the click; becomes ``ABORTED``."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


class _WriteAheadError(Exception):
    """The write-ahead hook failed, timed out or could not be scheduled."""


class _FlowControl:
    """Shared between the event loop and the worker: who may still click."""

    def __init__(self, loop: asyncio.AbstractEventLoop | None) -> None:
        self.loop = loop
        self._lock = threading.Lock()
        self._abandoned = False
        self._click_started = False

    def abandon(self) -> bool:
        """Forbid any further click; returns whether a click had already started."""
        with self._lock:
            self._abandoned = True
            return self._click_started

    def begin_click(self) -> bool:
        """Atomically claim the right to click (False when the caller already gave up)."""
        with self._lock:
            if self._abandoned:
                return False
            self._click_started = True
            return True

    @property
    def abandoned(self) -> bool:
        with self._lock:
            return self._abandoned

    @property
    def click_started(self) -> bool:
        with self._lock:
            return self._click_started


@dataclass(slots=True)
class _Outcome:
    status: IntentStatus
    message: str
    stage: str
    fill_price: float | None = None
    fill_price_source: PriceSource = "none"


@dataclass(frozen=True, slots=True)
class _Check:
    name: str
    ok: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)
    window: WindowInfo | None = None


@dataclass(frozen=True, slots=True)
class _FieldEntry:
    name: str  # "lots", "stop_loss", "take_profit"
    point: Point
    text: str  # what is typed ("" for a field that must be emptied)
    expected: Decimal | None  # the value it must read back as; None: it must read back empty

    @property
    def stage(self) -> str:
        return "lot_entry" if self.name == "lots" else "protective_fields"


@dataclass(frozen=True, slots=True)
class _PanelOrder:
    symbol: str
    side: Side
    panel: PanelLayout
    fields: tuple[_FieldEntry, ...]
    check_halt: bool


@dataclass(frozen=True, slots=True)
class _ClickPlan:
    point: Point
    what: str
    baseline: Image.Image
    dialog: DialogLayout | None
    check_halt: bool
    label: tuple[str, PanelLayout] | None


@dataclass(slots=True)
class _WriteAheadState:
    cancelled: bool = False
    started: bool = False
    ok: bool = False
    error: BaseException | None = None


class _Flow:
    """Bookkeeping for one order flow: steps, evidence, timings."""

    def __init__(self, clock: Clock, intent: OrderIntent, ctl: _FlowControl, deadline: float) -> None:
        self.clock = clock
        self.intent = intent
        self.ctl = ctl
        self.deadline = deadline
        self.started_at = clock.now()
        self.t0 = clock.monotonic()
        self.steps: list[dict[str, Any]] = []
        self.evidence: dict[str, Any] = {"steps": self.steps}
        self.win: WindowInfo | None = None
        self.geometry_ok = False
        self.input_used = False
        self.clicked_at: datetime | None = None
        self.operator_abort = False
        self.clipboard_touched = False
        self.images: dict[str, Image.Image] = {}

    @property
    def clicked(self) -> bool:
        return self.ctl.click_started

    def record(self, step: str, ok: bool, detail: str, **data: Any) -> None:
        entry: dict[str, Any] = {
            "step": step,
            "ok": ok,
            "detail": detail,
            "t": round(self.clock.monotonic() - self.t0, 3),
        }
        entry.update(data)
        self.steps.append(entry)
        (log.debug if ok else log.info)("intent %s step %s ok=%s: %s", self.intent.id, step, ok, detail)

    def fail(self, stage: str, message: str, **data: Any) -> NoReturn:
        self.record(stage, False, message, **data)
        raise _Abort(stage, message)

    def check(self, stage: str, check: _Check) -> None:
        """Record a check; abort the flow at ``stage`` when it failed."""
        self.record(check.name, check.ok, check.detail, **check.data)
        if not check.ok:
            raise _Abort(stage, check.detail)


def _safe_component(text: str) -> str:
    """One safe path component (no separators, no dots) for the evidence folder of an intent."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", text)[:100] or "intent"


def _geometry(win: WindowInfo) -> tuple[int, int, int, int, int]:
    return win.client_left, win.client_top, win.client_width, win.client_height, win.dpi


def _describe(text: str) -> str:
    """Read-back text for messages: numbers only. Anything else may be the operator's data and is never echoed."""
    return repr(text[:32]) if parse_lots(text) is not None else f"{len(text)} characters, not shown"


def _window_data(win: WindowInfo) -> dict[str, Any]:
    return {
        "handle": win.handle,
        "title": win.title,
        "pid": win.pid,
        "process_name": win.process_name,
        "client": [win.client_left, win.client_top, win.client_width, win.client_height],
        "dpi": win.dpi,
    }


def _consume_future_result(fut: asyncio.Future[Any]) -> None:
    if not fut.cancelled():
        fut.exception()  # mark as retrieved; the worker never raises, this only silences asyncio


def _task_cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _grid(start: int, length: int) -> list[int]:
    """Sample coordinates along one axis: both ends plus points at most ``GRID_STEP_PX`` apart."""
    if length <= 1:
        return [start]
    count = min(GRID_MAX_POINTS, max(2, math.ceil((length - 1) / GRID_STEP_PX) + 1))
    return sorted({start + round(i * (length - 1) / (count - 1)) for i in range(count)})


class UniTraderVenue:
    """:class:`~amongus_desk.execution.base.ExecutionVenue` that clicks the operator's UniTrader terminal."""

    name = _VENUE

    def __init__(  # noqa: PLR0917 - positional signature fixed by docs/ARCHITECTURE.md
        self,
        cfg: UniTraderConfig,
        layout: LayoutFile,
        backend: GuiBackend,
        mode: Mode,
        instruments: Mapping[str, InstrumentConfig],
        clock: Clock,
        is_halted: HaltCheck,
        evidence_dir: Path,
        dry_run: bool = False,
        *,
        hard_timeout_seconds: float | None = None,
    ) -> None:
        self._cfg = cfg
        self._layout = layout
        self._backend = backend
        self._mode = mode
        self._instruments = dict(instruments)
        self._clock = clock
        self._is_halted = is_halted
        self._evidence_dir = evidence_dir
        self._dry_run = dry_run
        self.supports_protective_orders = cfg.protective_orders == "panel_fields"
        self._hard_timeout = (
            hard_timeout_seconds
            if hard_timeout_seconds is not None
            else cfg.max_order_seconds + HARD_TIMEOUT_GRACE_SECONDS
        )
        self._health_timeout = cfg.max_order_seconds + HARD_TIMEOUT_GRACE_SECONDS + 30.0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="unitrader-gui")
        self._gui_lock = threading.Lock()
        self._templates: dict[str, Image.Image] = {}  # touched only on the worker thread
        self._closed = False

    @property
    def dry_run(self) -> bool:
        """True when every pre-click check that needs no typing runs, but nothing is ever typed or clicked."""
        return self._dry_run

    # ================================================================== public API (event loop)
    async def preflight(self) -> VenueHealth:
        """Static layout checks plus steps 1-6 for every calibrated panel. Never types or clicks."""
        return await self._run_health(thorough=True)

    async def health(self) -> VenueHealth:
        """Steps 1-6 for every calibrated panel (watchdog). Never types or clicks."""
        return await self._run_health(thorough=False)

    async def submit(self, intent: OrderIntent, quote: Quote | None, on_before_click: BeforeClick) -> ExecutionReport:
        """Open a position at market through the calibrated one-click panel (steps 1-14)."""
        deadline = self._clock.monotonic() + self._cfg.max_order_seconds
        try:
            order = self._open_order(intent)
        except _Abort as exc:
            return self._quick_report(intent, IntentStatus.ABORTED, exc.message, exc.stage)
        price = self._price_estimate(order.symbol, order.side, quote, intent.entry_ref)
        return await self._dispatch(
            intent,
            deadline,
            lambda flow: self._drive(flow, lambda f: self._prepare_panel_order(f, order), on_before_click, price),
        )

    async def close_position(
        self, position: Position, intent: OrderIntent, quote: Quote | None, on_before_click: BeforeClick
    ) -> ExecutionReport:
        """Close one position with the configured ``exit_method``. Ignores the kill switch (PANIC must work)."""
        deadline = self._clock.monotonic() + self._cfg.max_order_seconds
        reference = position.last_price if position.last_price is not None else position.entry_price
        price = self._price_estimate(position.symbol, position.side.opposite, quote, reference)
        try:
            self._validate_common(intent, "close")
            if intent.position_id is not None and intent.position_id != position.id:
                raise _Abort("validate", f"close intent is for position {intent.position_id}, not {position.id}")
            if self._cfg.exit_method == "close_all":
                self._require_close_all_button()
                return await self._dispatch(
                    intent, deadline, lambda flow: self._drive(flow, self._prepare_close_all, on_before_click, price)
                )
            order = self._closing_order(position)
        except _Abort as exc:
            return self._quick_report(intent, IntentStatus.ABORTED, exc.message, exc.stage)
        return await self._dispatch(
            intent,
            deadline,
            lambda flow: self._drive(flow, lambda f: self._prepare_panel_order(f, order), on_before_click, price),
        )

    async def close_all(self, intent: OrderIntent, on_before_click: BeforeClick) -> ExecutionReport:
        """Click the calibrated close-all button. Ignores the kill switch (this is how PANIC flattens)."""
        deadline = self._clock.monotonic() + self._cfg.max_order_seconds
        try:
            self._validate_common(intent, "close_all")
            self._require_close_all_button()
        except _Abort as exc:
            return self._quick_report(intent, IntentStatus.ABORTED, exc.message, exc.stage)
        return await self._dispatch(
            intent, deadline, lambda flow: self._drive(flow, self._prepare_close_all, on_before_click, None)
        )

    async def aclose(self) -> None:
        """Stop accepting work; queued (not yet started) GUI jobs are cancelled."""
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ================================================================== validation (event loop)
    @property
    def _account_label(self) -> str:
        return self._mode.value.upper()

    def _expected_kind(self) -> Literal["demo", "live"] | None:
        if self._mode is Mode.DEMO:
            return "demo"
        if self._mode is Mode.LIVE:
            return "live"
        return None

    def _validate_common(self, intent: OrderIntent, purpose: str) -> None:
        if self._closed:
            raise _Abort("closed", "the UniTrader venue is shut down")
        if intent.purpose != purpose:
            raise _Abort("validate", f"expected a {purpose!r} intent, got {intent.purpose!r}")
        if self._expected_kind() is None:
            raise _Abort("validate", f"mode {self._mode.value!r} does not use the UniTrader terminal")
        if intent.mode is not self._mode:
            raise _Abort(
                "validate", f"intent was created for mode {intent.mode.value!r} but the venue runs {self._mode.value!r}"
            )

    def _require_close_all_button(self) -> None:
        if self._layout.layout.close_all_button is None:
            raise _Abort(
                "validate",
                "no close-all button is calibrated: close the position(s) manually in UniTrader "
                "(or re-run desk calibrate and capture the close-all button)",
            )

    def _instrument_and_panel(self, symbol: str) -> tuple[InstrumentConfig, PanelLayout]:
        inst = self._instruments.get(symbol)
        if inst is None:
            raise _Abort("validate", f"unknown instrument {symbol!r}")
        panel = self._layout.layout.panels.get(symbol)
        if panel is None:
            raise _Abort("validate", f"no calibrated panel for {symbol}: re-run desk calibrate for it")
        return inst, panel

    def _lot_field(self, inst: InstrumentConfig, panel: PanelLayout, lots: Decimal) -> _FieldEntry:
        if not lots.is_finite() or lots <= 0:
            raise _Abort("validate", f"invalid lot size {lots}")
        if lots < inst.min_lot or lots > inst.max_lot:
            raise _Abort("validate", f"{lots} lots is outside [{inst.min_lot}, {inst.max_lot}] for {inst.symbol}")
        try:
            text = format_lots(lots, inst.lot_step, self._cfg.decimal_separator)
        except ValueError as exc:
            raise _Abort("validate", f"cannot type {lots} lots: {exc}") from exc
        return _FieldEntry("lots", panel.lot_input, text, lots)

    def _require_verifiable_entry(self) -> None:
        problem = self._cfg.field_entry_problem()  # rejected by the config validator; re-checked for safety
        if problem is not None:
            raise _Abort("validate", problem)

    def _open_order(self, intent: OrderIntent) -> _PanelOrder:
        self._validate_common(intent, "open")
        self._require_verifiable_entry()
        if intent.side is None or intent.lots is None:
            raise _Abort("validate", "an opening order needs a side and a lot size")
        inst, panel = self._instrument_and_panel(intent.symbol)
        if not inst.enabled:
            raise _Abort("validate", f"{intent.symbol} is disabled in the config")
        fields = [self._lot_field(inst, panel, intent.lots)]
        if self._cfg.protective_orders == "panel_fields":
            fields.extend(self._protective_fields(intent, inst, panel, intent.side))
        else:
            fields.extend(self._emptied_fields(panel))
        return _PanelOrder(intent.symbol, intent.side, panel, tuple(fields), check_halt=True)

    def _closing_order(self, position: Position) -> _PanelOrder:
        if not self._cfg.netting_account_confirmed:
            raise _Abort(
                "validate",
                "exit_method 'opposite_order' only closes on a netting account; set netting_account_confirmed "
                "or close the position manually",
            )
        if self._cfg.protective_orders == "panel_fields":
            raise _Abort(
                "validate",
                "an opposite order would be sent with whatever the panel's SL/TP fields still contain and the "
                "desk cannot verify they are empty: close the position manually (or use exit_method: close_all)",
            )
        self._require_verifiable_entry()
        inst, panel = self._instrument_and_panel(position.symbol)
        fields = (self._lot_field(inst, panel, position.lots), *self._emptied_fields(panel))
        return _PanelOrder(position.symbol, position.side.opposite, panel, fields, check_halt=False)

    @staticmethod
    def _emptied_fields(panel: PanelLayout) -> list[_FieldEntry]:
        """protective_orders: none: an order carries whatever the panel's SL/TP fields still hold (a manual test
        trade, an earlier panel_fields run), so every calibrated SL/TP input is emptied and verified empty."""
        return [
            _FieldEntry(name, point, "", None)
            for name, point in (("stop_loss", panel.sl_input), ("take_profit", panel.tp_input))
            if point is not None
        ]

    def _protective_fields(
        self, intent: OrderIntent, inst: InstrumentConfig, panel: PanelLayout, side: Side
    ) -> list[_FieldEntry]:
        if panel.sl_input is None or panel.tp_input is None:
            raise _Abort(
                "validate",
                f"protective_orders: panel_fields but the SL/TP inputs of the {inst.symbol} panel are not "
                "calibrated: re-run desk calibrate",
            )
        stop, target, ref = intent.stop_price, intent.target_price, intent.entry_ref
        if stop is None or target is None:
            raise _Abort("validate", "protective_orders: panel_fields needs a stop and a target price")
        if (
            not (stop > 0 and target > 0)
            or (side is Side.BUY and not stop < target)
            or (side is Side.SELL and not stop > target)
        ):
            raise _Abort("validate", f"stop {stop} / target {target} are inconsistent for a {side.value}")
        if ref is not None and not (min(stop, target) < ref < max(stop, target)):
            raise _Abort("validate", f"entry reference {ref} is not between stop {stop} and target {target}")
        sep = self._cfg.decimal_separator
        if self._cfg.protective_units == "price":
            quantum = Decimal(1).scaleb(-inst.price_decimals)
            sl = Decimal(str(stop)).quantize(quantum, rounding=ROUND_HALF_EVEN)
            tp = Decimal(str(target)).quantize(quantum, rounding=ROUND_HALF_EVEN)
        else:
            if ref is None:
                raise _Abort("validate", "protective_units: pips needs the entry reference price")
            pip = Decimal(str(inst.pip_size))
            tenth = Decimal("0.1")
            # Round the stop distance DOWN so the typed stop is never wider than the sized one.
            sl = (abs(Decimal(str(ref)) - Decimal(str(stop))) / pip).quantize(tenth, rounding=ROUND_DOWN)
            tp = (abs(Decimal(str(target)) - Decimal(str(ref))) / pip).quantize(tenth, rounding=ROUND_HALF_EVEN)
            if sl <= 0 or tp <= 0:
                raise _Abort("validate", "stop/target distance rounds to zero pips")

        def text(value: Decimal) -> str:
            raw = format(value, "f")
            return raw.replace(".", sep) if sep != "." else raw

        return [
            _FieldEntry("stop_loss", panel.sl_input, text(sl), sl),
            _FieldEntry("take_profit", panel.tp_input, text(tp), tp),
        ]

    def _price_estimate(self, symbol: str, side: Side, quote: Quote | None, reference: float | None) -> float | None:
        """Estimated fill: the feed's ask (buy) / bid (sell), else the reference price."""
        price: float | None = None
        if quote is not None and quote.symbol == symbol:
            price = quote.ask if side is Side.BUY else quote.bid
        elif reference is not None and reference > 0:
            price = reference
        if price is None:
            return None
        inst = self._instruments.get(symbol)
        return round(price, inst.price_decimals) if inst is not None else price

    def _quick_report(
        self,
        intent: OrderIntent,
        status: IntentStatus,
        message: str,
        stage: str,
        *,
        started_at: datetime | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> ExecutionReport:
        now = self._clock.now()
        if status is IntentStatus.ABORTED:
            log.warning("intent %s ABORTED at %s: %s", intent.id, stage, message)
        else:
            log.error("intent %s %s at %s: %s", intent.id, status.value, stage, message)
        return ExecutionReport(
            intent_id=intent.id,
            status=status,
            venue=self.name,
            started_at=started_at or now,
            finished_at=now,
            message=message,
            stage=stage,
            evidence=evidence or {"steps": []},
        )

    # ================================================================== dispatch to the worker
    async def _dispatch(
        self, intent: OrderIntent, deadline: float, job: Callable[[_Flow], ExecutionReport]
    ) -> ExecutionReport:
        if self._closed:
            return self._quick_report(intent, IntentStatus.ABORTED, "the UniTrader venue is shut down", "closed")
        loop = asyncio.get_running_loop()
        ctl = _FlowControl(loop)
        started_at = self._clock.now()

        def run() -> ExecutionReport:
            with self._gui_lock:
                try:
                    return job(_Flow(self._clock, intent, ctl, deadline))
                except Exception as exc:  # last-resort net: a programming error must not hide a click
                    log.exception("intent %s: GUI flow crashed", intent.id)
                    status = IntentStatus.UNCONFIRMED if ctl.click_started else IntentStatus.ABORTED
                    return self._quick_report(
                        intent, status, f"internal error in the GUI flow: {type(exc).__name__}: {exc}", "error"
                    )

        try:
            cfut: Future[ExecutionReport] = self._executor.submit(run)
        except RuntimeError:
            return self._quick_report(intent, IntentStatus.ABORTED, "the UniTrader venue is shut down", "closed")
        afut = asyncio.wrap_future(cfut, loop=loop)
        afut.add_done_callback(_consume_future_result)
        try:
            return await asyncio.wait_for(asyncio.shield(afut), timeout=self._hard_timeout)
        except TimeoutError:
            clicked = ctl.abandon()
            evidence = {"steps": [], "watchdog_timeout_seconds": self._hard_timeout}
            if clicked:
                return self._quick_report(
                    intent,
                    IntentStatus.UNCONFIRMED,
                    f"the GUI worker did not finish within {self._hard_timeout:.0f}s after clicking; "
                    "check the terminal and reconcile",
                    "watchdog_timeout",
                    started_at=started_at,
                    evidence=evidence,
                )
            return self._quick_report(
                intent,
                IntentStatus.ABORTED,
                f"the GUI worker did not respond within {self._hard_timeout:.0f}s; nothing was clicked",
                "watchdog_timeout",
                started_at=started_at,
                evidence=evidence,
            )
        except asyncio.CancelledError:
            clicked = ctl.abandon()
            if afut.cancelled() and not clicked and not _task_cancelling():
                return self._quick_report(
                    intent, IntentStatus.ABORTED, "the UniTrader venue shut down before the order started", "closed"
                )
            log.error("intent %s: submit cancelled (click started: %s)", intent.id, clicked)
            raise

    async def _run_health(self, *, thorough: bool) -> VenueHealth:
        if self._closed:
            return self._health_result([HealthCheck(name="venue", ok=False, detail="the UniTrader venue is shut down")])
        loop = asyncio.get_running_loop()
        ctl = _FlowControl(loop)

        def run() -> VenueHealth:
            with self._gui_lock:
                if ctl.abandoned:
                    return self._health_result(
                        [HealthCheck(name="gui_worker", ok=False, detail="health check abandoned (worker was busy)")]
                    )
                return self._health_job(thorough)

        try:
            cfut: Future[VenueHealth] = self._executor.submit(run)
        except RuntimeError:
            return self._health_result([HealthCheck(name="venue", ok=False, detail="the UniTrader venue is shut down")])
        afut = asyncio.wrap_future(cfut, loop=loop)
        afut.add_done_callback(_consume_future_result)
        try:
            return await asyncio.wait_for(asyncio.shield(afut), timeout=self._health_timeout)
        except TimeoutError:
            ctl.abandon()
            return self._health_result(
                [
                    HealthCheck(
                        name="gui_worker",
                        ok=False,
                        detail=f"the GUI worker did not answer within {self._health_timeout:.0f}s (busy or stuck); "
                        "check the desk's log and the terminal",
                    )
                ]
            )
        except asyncio.CancelledError:
            ctl.abandon()
            if afut.cancelled() and not _task_cancelling():
                return self._health_result(
                    [HealthCheck(name="venue", ok=False, detail="the UniTrader venue is shut down")]
                )
            raise

    def _health_result(self, checks: Sequence[HealthCheck]) -> VenueHealth:
        return VenueHealth(
            venue=self.name,
            ok=bool(checks) and all(c.ok for c in checks),
            checked_at=self._clock.now(),
            checks=list(checks),
        )

    # ================================================================== flows (worker thread)
    def _drive(
        self,
        flow: _Flow,
        prepare: Callable[[_Flow], _ClickPlan | None],
        on_before_click: BeforeClick,
        price: float | None,
    ) -> ExecutionReport:
        outcome: _Outcome
        try:
            self._checkpoint(flow)
            plan = prepare(flow)
            if plan is None:
                outcome = _Outcome(
                    IntentStatus.DRY_RUN,
                    "dry run: every pre-click check passed, including the re-verification right before the click; "
                    "nothing was typed or clicked (so the lot read-back was not exercised)",
                    "dry_run",
                )
            else:
                self._click(flow, plan, on_before_click)
                outcome = self._after_click(flow, plan, price)
        except _Abort as exc:
            outcome = self._failure(flow, exc.stage, exc.message)
        except OperatorAbortError as exc:
            flow.operator_abort = True
            flow.record("operator_abort", False, f"operator fail-safe: {exc}")
            outcome = self._failure(flow, "operator_abort", f"the operator fail-safe fired ({exc})")
        except Exception as exc:
            log.exception("intent %s: unexpected error in the GUI flow", flow.intent.id)
            flow.record("error", False, f"{type(exc).__name__}: {exc}")
            outcome = self._failure(flow, "error", f"unexpected error: {type(exc).__name__}: {exc}")
        self._finish(flow, outcome)
        return self._final_report(flow, outcome)

    def _failure(self, flow: _Flow, stage: str, message: str) -> _Outcome:
        if flow.clicked:  # never ABORTED once the click started (venue invariant 4)
            return _Outcome(
                IntentStatus.UNCONFIRMED, f"after the click: {message}; check the terminal and reconcile", stage
            )
        return _Outcome(IntentStatus.ABORTED, f"{message} (nothing was clicked)", stage)

    def _final_report(self, flow: _Flow, outcome: _Outcome) -> ExecutionReport:
        report = ExecutionReport(
            intent_id=flow.intent.id,
            status=outcome.status,
            venue=self.name,
            started_at=flow.started_at,
            finished_at=self._clock.now(),
            clicked_at=flow.clicked_at,
            fill_price=outcome.fill_price,
            fill_price_source=outcome.fill_price_source,
            message=outcome.message,
            stage=outcome.stage,
            operator_abort=flow.operator_abort,
            evidence=flow.evidence,
        )
        level = logging.INFO if outcome.status in (IntentStatus.CONFIRMED, IntentStatus.DRY_RUN) else logging.WARNING
        if outcome.status is IntentStatus.UNCONFIRMED:
            level = logging.ERROR
        log.log(level, "intent %s %s at %s: %s", flow.intent.id, outcome.status.value, outcome.stage, outcome.message)
        return report

    def _checkpoint(self, flow: _Flow) -> None:
        """Between pre-click steps: stop when the caller gave up or the time budget is spent."""
        if flow.ctl.abandoned:
            flow.fail("abandoned", "the caller stopped waiting for this order")
        if self._clock.monotonic() >= flow.deadline:
            flow.fail("deadline", f"max_order_seconds ({self._cfg.max_order_seconds:g}s) exceeded before the click")

    def _halted(self) -> bool:
        try:
            return bool(self._is_halted())
        except Exception:
            log.exception("kill-switch check failed; treating it as engaged")
            return True

    def _delay(self) -> None:
        if self._cfg.action_delay_seconds > 0:
            self._backend.sleep(self._cfg.action_delay_seconds)

    # ------------------------------------------------------------------ steps 1-5 (shared)
    def _precheck(self, flow: _Flow, *, check_halt: bool, one_click: bool) -> WindowInfo:
        # 1. kill switch (opening orders only) and an unlocked input desktop
        if check_halt:
            if self._halted():
                flow.fail("halted", "kill switch engaged: opening orders are blocked")
            flow.record("halted", True, "kill switch not engaged")
        flow.check("input_desktop", self._check_desktop())
        self._checkpoint(flow)
        # 5a. the layout must have been calibrated on the account kind the mode expects (static, cheap)
        flow.check("account_kind", self._check_account_kind())
        # 2. exactly one terminal window
        located = self._check_window()
        flow.check("locate_window", located)
        if located.window is None:
            flow.fail("locate_window", "UniTrader window not found")
        flow.win = located.window
        flow.evidence["window"] = _window_data(located.window)
        self._checkpoint(flow)
        # 3. restore if minimized; client size and DPI must equal the calibration (never resize)
        checks, win = self._check_geometry(located.window)
        for c in checks:
            flow.check("client_geometry", c)
        if win is None:
            flow.fail("client_geometry", "the UniTrader window geometry could not be verified")
        flow.win = win
        flow.geometry_ok = True
        self._checkpoint(flow)
        # 4. foreground
        flow.check("focus", self._check_focus(win))
        self._checkpoint(flow)
        # 5b. account badge (+ optional one-click indicator)
        flow.check("account_badge", self._check_badge(win))
        if one_click and self._layout.layout.one_click_indicator is not None:
            flow.check("one_click_indicator", self._check_one_click(win))
        self._checkpoint(flow)
        return win

    def _baseline(self, flow: _Flow, win: WindowInfo) -> Image.Image:
        """Step 7: two captures of the positions indicator ~0.3 s apart."""
        region = self._layout.layout.positions_indicator
        if not self._region_unobstructed(win, region):
            flow.fail("stability", "something covers the positions indicator; close it and retry")
        first = self._capture_region(win, region)
        self._backend.sleep(STABILITY_INTERVAL_SECONDS)
        second = self._capture_region(win, region)
        frac = vision.changed_fraction(first, second)
        if self._cfg.stability_check and frac > 0.0:
            flow.fail(
                "stability",
                f"the positions indicator changed by itself ({frac:.1%} of its pixels in "
                f"{STABILITY_INTERVAL_SECONDS:g}s): a position may be opening/closing, or the calibrated region "
                "is not static; check the terminal (or re-calibrate the indicator)",
                changed=round(frac, 4),
            )
        flow.record("stability", True, "positions indicator is stable", changed=round(frac, 4))
        self._checkpoint(flow)
        return second

    def _prepare_panel_order(self, flow: _Flow, order: _PanelOrder) -> _ClickPlan | None:
        win = self._precheck(flow, check_halt=order.check_halt, one_click=True)
        # 6. the panel label must match (protects against reordered / covered panels)
        flow.check("verify_panel_label", self._check_label(win, order.symbol, order.panel))
        # 7. baseline of the positions indicator
        baseline = self._baseline(flow, win)
        button = order.panel.buy_button if order.side is Side.BUY else order.panel.sell_button
        plan = _ClickPlan(
            point=button,
            what=f"{order.side.value} button of the {order.symbol} panel",
            baseline=baseline,
            dialog=self._layout.layout.order_dialog,
            check_halt=order.check_halt,
            label=(order.symbol, order.panel),
        )
        flow.evidence["order"] = {
            "symbol": order.symbol,
            "side": order.side.value,
            "fields": {e.name: e.text for e in order.fields},
        }
        # 8. before the first click into the panel: no dialog left open, nothing on top of any field
        self._guard_input(flow, win, order, order.fields, "pre_input_verify")
        flow.record("pre_input_verify", True, "window, focus, dialogs, fields and panel label verified before input")
        if self._dry_run:
            # The same read-only re-verification a real order runs right before its click; then stop.
            self._final_verify(flow, win, plan)
            flow.record("dry_run", True, "dry run: stopping before typing")
            return None
        # 9. lot (and SL/TP) entry with clipboard read-back, then a read-back of what the terminal committed
        typed: dict[str, str | None] = {}
        for entry in order.fields:
            typed[entry.name] = self._enter_value(flow, win, order, entry)
            self._checkpoint(flow)
        self._verify_committed(flow, win, order, typed)
        # 10. re-verify right before the click
        self._final_verify(flow, win, plan)
        return plan

    def _prepare_close_all(self, flow: _Flow) -> _ClickPlan | None:
        win = self._precheck(flow, check_halt=False, one_click=False)
        baseline = self._baseline(flow, win)
        point = self._layout.layout.close_all_button
        if point is None:  # validated before dispatch; kept for type narrowing
            flow.fail("validate", "no close-all button is calibrated: close the position(s) manually")
        plan = _ClickPlan(
            point=point,
            what="close-all button",
            baseline=baseline,
            dialog=self._layout.layout.close_all_dialog,
            check_halt=False,
            label=None,
        )
        self._final_verify(flow, win, plan)
        if self._dry_run:
            flow.record("dry_run", True, "dry run: stopping before the close-all click")
            return None
        return plan

    # ------------------------------------------------------------------ steps 8-9
    def _guard_input(
        self, flow: _Flow, win: WindowInfo, order: _PanelOrder, entries: Sequence[_FieldEntry], stage: str
    ) -> None:
        """Right before any click or key press into the panel: is the input going where the desk thinks it goes?

        A dialog that is already open, or any window on top of a field, would take the input: a confirmation's
        button may sit right over a field (the click would execute an earlier order) and a key goes to whatever
        has the focus (Enter to a dialog's default button). So the window must be where it was, in front, with no
        calibrated dialog or error showing, nothing covering the field(s) and the panel label still matching.
        """
        self._require_same_window(flow, win, stage)
        foreground = self._backend.foreground_window()
        if foreground != win.handle:
            flow.fail(
                stage,
                f"the UniTrader window lost the foreground before typing into the {order.symbol} panel "
                f"(foreground: {foreground})",
            )
        # ONE screenshot for the dialogs and the panel label: this guard runs before every input.
        popups = self._popup_templates()
        shots = self._capture_regions(win, [order.panel.label.region, *(ref.region for _, ref in popups)])
        for (name, ref), shot in zip(popups, shots[1:], strict=True):
            if self._image_matches(shot, ref):
                flow.fail(
                    stage,
                    f"{name} is already showing before typing into the {order.symbol} panel: deal with it in "
                    "UniTrader yourself, then retry",
                )
        for entry in entries:
            sx, sy = self._screen(win, entry.point)
            owner = self._backend.root_window_at(sx, sy)
            if owner != win.handle:
                flow.fail(
                    stage,
                    f"something else is on top of the {entry.name} field of the {order.symbol} panel (window "
                    f"{owner} at {sx},{sy}); close it and retry",
                )
        label = self._check_label(win, order.symbol, order.panel, shot=shots[0])
        if not label.ok:
            flow.fail(stage, label.detail, **label.data)

    def _enter_value(self, flow: _Flow, win: WindowInfo, order: _PanelOrder, entry: _FieldEntry) -> str | None:
        """Type one value (or empty an SL/TP field), read it back, then commit it with ``commit_key``.

        Returns the text the field read back right before the commit (None or "": it copied nothing; None also
        without a read-back).
        """
        self._guard_input(flow, win, order, (entry,), entry.stage)
        sx, sy = self._screen(win, entry.point)
        flow.input_used = True
        self._backend.click(sx, sy)
        self._delay()
        self._backend.hotkey("ctrl", "a")
        self._delay()
        readback: str | None = None
        if entry.expected is None:
            readback = self._empty_field(flow, win, order, entry)
        else:
            self._backend.type_text(entry.text)
            self._delay()
            flow.record(f"{entry.name}_typed", True, f"typed {entry.text!r} into the {entry.name} field")
            if self._cfg.lot_readback == "clipboard":
                readback = self._copy_field(flow, win, order, entry)
                self._expect_value(flow, entry, readback, committed=False)
            else:
                flow.record(f"{entry.name}_readback", True, "read-back disabled (lot_readback: none)")
        self._commit(flow, win, order, entry)
        return readback

    def _empty_field(self, flow: _Flow, win: WindowInfo, order: _PanelOrder, entry: _FieldEntry) -> str | None:
        """protective_orders: none: delete whatever an SL/TP field holds (its text is selected already).

        Returns what the emptied field copies (None or "": nothing), or None without a read-back.
        """
        if self._cfg.lot_readback != "clipboard":
            self._press(flow, win, order, entry, "backspace")
            flow.record(
                f"{entry.name}_cleared", True, f"emptied the {entry.name} field (not verified: lot_readback: none)"
            )
            return None
        # Prove first that the field takes the keys and copies (an empty field copies nothing, just like a broken
        # copy): type the probe over its text and read exactly the probe back...
        self._backend.type_text(CLEAR_PROBE)
        self._delay()
        text = self._copy_field(flow, win, order, entry)
        if text is None or parse_lots(text) != Decimal(CLEAR_PROBE):
            flow.fail(
                entry.stage,
                f"the {entry.name} field could not be emptied safely: the probe {CLEAR_PROBE!r} typed into it reads "
                f"back as {'nothing' if text is None else _describe(text)}, so the desk cannot prove the field is "
                "empty. Empty the panel's SL/TP fields in UniTrader yourself and check that ctrl+a/ctrl+c work in them",
            )
        # ...then delete it (the read-back left all of it selected): now copying nothing means "empty".
        self._press(flow, win, order, entry, "backspace")
        text = self._copy_field(flow, win, order, entry)
        self._expect_empty(flow, entry, text, committed=False)
        return text

    def _commit(self, flow: _Flow, win: WindowInfo, order: _PanelOrder, entry: _FieldEntry) -> None:
        if self._cfg.commit_key != "none":
            self._press(flow, win, order, entry, self._cfg.commit_key)

    def _press(self, flow: _Flow, win: WindowInfo, order: _PanelOrder, entry: _FieldEntry, key: str) -> None:
        """A key goes wherever the keyboard focus is: guarded like a click."""
        self._guard_input(flow, win, order, (entry,), entry.stage)
        self._backend.press(key)
        self._delay()

    def _copy_field(self, flow: _Flow, win: WindowInfo, order: _PanelOrder, entry: _FieldEntry) -> str | None:
        """Read a field through the clipboard: a fresh sentinel, then click, ctrl+a, ctrl+c.

        Returns the copied text (stripped), or None when nothing was copied (the sentinel is still there). The
        operator's clipboard is never read or restored: the sentinel replaces it and :meth:`_finish` empties it.
        """
        self._guard_input(flow, win, order, (entry,), entry.stage)
        sx, sy = self._screen(win, entry.point)
        sentinel = f"amongus-desk-readback-{uuid.uuid4().hex}"
        flow.clipboard_touched = True
        self._backend.set_clipboard_text(sentinel)
        self._backend.click(sx, sy)
        self._delay()
        self._backend.hotkey("ctrl", "a")
        self._delay()
        self._backend.hotkey("ctrl", "c")
        self._delay()
        got = self._backend.get_clipboard_text()
        if got is None:
            flow.fail(
                entry.stage,
                f"{entry.name} read-back failed: the clipboard holds no text after the copy (another program may be "
                "using the clipboard)",
            )
        text = got.strip()
        return None if text == sentinel else text

    def _expect_value(self, flow: _Flow, entry: _FieldEntry, text: str | None, *, committed: bool) -> None:
        when = f" after {self._cfg.commit_key} committed it" if committed else ""
        if text is None:
            flow.fail(
                entry.stage,
                f"{entry.name} read-back failed{when}: the clipboard still holds the sentinel, so the field could not "
                "be selected/copied (ctrl+a/ctrl+c had no effect)",
            )
        value = parse_lots(text)
        if value is None:
            # Never echo non-numeric clipboard text into reports, logs or alerts: if anything else wrote to the
            # clipboard in between, it could be the operator's own data (e.g. a password copied for the login).
            flow.fail(
                entry.stage,
                f"{entry.name} read-back mismatch{when}: typed {entry.text!r} but the field does not read back as a "
                f"number ({_describe(text)})",
            )
        shown = text[:32]
        if value != entry.expected:
            hint = ""
            if committed:
                hint = (
                    ": the terminal changed the value when it committed it (it rounds, clamps or reads numbers in "
                    "another format). Check "
                    + ("lot_step/min_lot/max_lot" if entry.name == "lots" else "price_decimals/protective_units")
                    + " and decimal_separator against UniTrader"
                )
            flow.fail(
                entry.stage,
                f"{entry.name} read-back mismatch{when}: typed {entry.text!r} but the field shows {shown!r}{hint}",
                readback=shown,
            )
        step = f"{entry.name}_committed" if committed else f"{entry.name}_readback"
        flow.record(step, True, f"field reads back {shown!r}{when}", readback=shown)

    def _expect_empty(self, flow: _Flow, entry: _FieldEntry, text: str | None, *, committed: bool) -> None:
        when = f" after {self._cfg.commit_key} committed it" if committed else ""
        if text:
            flow.fail(
                entry.stage,
                f"the {entry.name} field is not empty{when}: it reads back as {_describe(text)}. With "
                "protective_orders: none an order is only sent while the panel's SL/TP fields are empty: empty them "
                "in UniTrader yourself, then retry",
            )
        step = f"{entry.name}_committed" if committed else f"{entry.name}_cleared"
        flow.record(step, True, f"the {entry.name} field is empty{when}")

    def _verify_committed(
        self, flow: _Flow, win: WindowInfo, order: _PanelOrder, typed: Mapping[str, str | None]
    ) -> None:
        """9b. Read every field back once more, after the terminal committed it (still before the click).

        The first read-back only proves that the keystrokes arrived. A terminal may round, clamp or re-parse a value
        when the edit is committed, and the order goes out with the committed value. ``typed`` holds what each field
        read back right before its commit.
        """
        if self._cfg.lot_readback != "clipboard":
            return
        for entry in order.fields:
            text = self._copy_field(flow, win, order, entry)
            if entry.expected is None:
                self._expect_empty(flow, entry, text, committed=True)
            else:
                self._expect_value(flow, entry, text, committed=True)
            # Leave every field as the configured flow does; committing the value it just showed changes nothing...
            self._commit(flow, win, order, entry)
            before, shown = typed[entry.name] or "", text or ""
            if shown != before:
                # ...unless the commit changed the text: then the next commit (this one, or leaving the field for
                # the click) parses the terminal's own display, which need not read back as the same number (shown
                # as "0,10" but parsed with "." as the decimal point, 0.10 becomes 10). It must not change again.
                again = self._copy_field(flow, win, order, entry)
                if (again or "") != shown:
                    flow.fail(
                        entry.stage,
                        f"{entry.name} changed again when {self._cfg.commit_key} committed it a second time: "
                        f"{_describe(shown)} became {'nothing' if again is None else _describe(again)}. The "
                        "terminal does not read back the numbers it shows: check decimal_separator against UniTrader",
                    )
                flow.record(f"{entry.name}_stable", True, f"field still reads back {_describe(shown)}")
            self._checkpoint(flow)

    # ------------------------------------------------------------------ step 10
    def _require_same_window(self, flow: _Flow, win: WindowInfo, stage: str) -> None:
        current = self._backend.get_window(win.handle)
        if current is None:
            flow.fail(stage, "the UniTrader window disappeared")
        if current.minimized or _geometry(current) != _geometry(win):
            flow.fail(
                stage,
                f"the UniTrader window moved, resized or minimized since the checks ({_geometry(win)} -> "
                f"{_geometry(current)})",
            )

    def _final_verify(self, flow: _Flow, win: WindowInfo, plan: _ClickPlan) -> None:
        """Step 10, read-only (a dry run runs it too): nothing changed and nothing covers the button."""
        flow.check("pre_click_verify", self._check_desktop())
        self._require_same_window(flow, win, "pre_click_verify")
        foreground = self._backend.foreground_window()
        if foreground != win.handle:
            flow.fail("pre_click_verify", f"the UniTrader window lost the foreground (foreground: {foreground})")
        if plan.label is not None:
            symbol, panel = plan.label
            flow.check("pre_click_verify", self._check_label(win, symbol, panel))
        # A dialog that is already open would swallow our click, or be confirmed by the post-click logic as if
        # our order had caused it (a stale confirmation would execute a *previous* order), and a stale error
        # dialog would turn into a false REJECTED. The desk never handles a dialog it did not cause.
        for name, ref in self._popup_templates():
            if self._template_matches(win, ref):
                flow.fail(
                    "pre_click_verify",
                    f"{name} is already showing before the click: deal with it in UniTrader yourself, then retry",
                )
        # The fill check compares against the step-7 baseline; if the indicator moved since then (another
        # position opening/closing, or our own typing showing up in the region) a later change proves nothing.
        region = self._layout.layout.positions_indicator
        if not self._region_unobstructed(win, region):
            flow.fail("pre_click_verify", "something covers the positions indicator; close it and retry")
        drift = vision.changed_fraction(plan.baseline, self._capture_region(win, region))
        moved = drift > 0.0 if self._cfg.stability_check else drift >= self._cfg.change_threshold
        if moved:
            flow.fail(
                "pre_click_verify",
                f"the positions indicator changed ({drift:.1%} of its pixels) between its baseline and the click: a "
                "position may be opening/closing, or the calibrated region is not static; check the terminal",
                changed=round(drift, 4),
            )
        sx, sy = self._screen(win, plan.point)
        owner = self._backend.root_window_at(sx, sy)
        if owner != win.handle:
            flow.fail(
                "pre_click_verify",
                f"something else is on top of the {plan.what} (window {owner} at {sx},{sy}); close it and retry",
            )
        if plan.check_halt and self._halted():
            flow.fail("halted", "kill switch engaged before the click")
        flow.record(
            "pre_click_verify", True, f"window, focus, label, dialogs, positions indicator and {plan.what} verified"
        )

    # ------------------------------------------------------------------ step 11
    def _click(self, flow: _Flow, plan: _ClickPlan, on_before_click: BeforeClick) -> None:
        self._checkpoint(flow)
        needed = self._cfg.fill_timeout_seconds + (self._cfg.dialog_timeout_seconds if plan.dialog else 0.0)
        remaining = flow.deadline - self._clock.monotonic()
        if remaining < needed:
            flow.fail(
                "deadline",
                f"only {remaining:.1f}s of max_order_seconds left but confirming the result needs up to {needed:.1f}s",
            )
        win = flow.win
        if win is None:
            flow.fail("click", "internal error: no verified window")
        if self._cfg.save_evidence:
            try:
                flow.images["before"] = self._capture_client(win)
            except Exception as exc:  # noqa: BLE001 - evidence must never block or break the order
                log.warning("intent %s: before-click evidence capture failed: %s", flow.intent.id, exc)
        try:
            self._write_ahead(flow.ctl, on_before_click)
        except _WriteAheadError as exc:
            flow.fail("write_ahead", f"write-ahead failed: {exc}")
        flow.record("write_ahead", True, "write-ahead marker persisted")
        if plan.check_halt and self._halted():
            flow.fail("halted", "kill switch engaged right before the click")
        # The hook may block (disk, database); never click past the budget that the check above granted.
        remaining = flow.deadline - self._clock.monotonic()
        if remaining < needed:
            flow.fail(
                "deadline",
                f"the write-ahead took too long: only {remaining:.1f}s of max_order_seconds left but confirming the "
                f"result needs up to {needed:.1f}s",
            )
        if not flow.ctl.begin_click():
            flow.fail("abandoned", "the caller stopped waiting for this order")
        sx, sy = self._screen(win, plan.point)
        flow.input_used = True
        try:
            self._backend.click(sx, sy)
        finally:
            flow.clicked_at = self._clock.now()
        flow.record("click", True, f"clicked the {plan.what} at {sx},{sy}")

    def _write_ahead(self, ctl: _FlowControl, callback: BeforeClick) -> None:
        """Run ``callback`` on the event-loop thread and wait for it (venue invariant 3)."""
        loop = ctl.loop
        if loop is None:
            try:
                callback()
            except Exception as exc:
                raise _WriteAheadError(f"{type(exc).__name__}: {exc}") from exc
            return
        gate = threading.Lock()
        done = threading.Event()
        state = _WriteAheadState()

        def runner() -> None:
            with gate:
                if state.cancelled:
                    return
                state.started = True
            try:
                if ctl.abandoned:
                    state.error = _WriteAheadError("the caller stopped waiting for this order")
                else:
                    callback()
                    state.ok = True
            except Exception as exc:  # noqa: BLE001 - reported to the worker, which aborts the order
                state.error = exc
            finally:
                done.set()

        try:
            loop.call_soon_threadsafe(runner)
        except RuntimeError as exc:
            raise _WriteAheadError(f"event loop unavailable: {exc}") from exc
        if not done.wait(WRITE_AHEAD_TIMEOUT_SECONDS):
            with gate:
                if not state.started:
                    state.cancelled = True
                    raise _WriteAheadError(f"the hook did not run within {WRITE_AHEAD_TIMEOUT_SECONDS:g}s")
            if not done.wait(WRITE_AHEAD_TIMEOUT_SECONDS):
                raise _WriteAheadError("the hook did not finish in time")
        if state.error is not None:
            raise _WriteAheadError(f"{type(state.error).__name__}: {state.error}")
        if not state.ok:
            raise _WriteAheadError("the hook did not complete")

    # ------------------------------------------------------------------ steps 12-13 (never ABORTED)
    def _after_click(self, flow: _Flow, plan: _ClickPlan, price: float | None) -> _Outcome:
        try:
            return self._confirm(flow, plan, price)
        except OperatorAbortError as exc:
            flow.operator_abort = True
            flow.record("operator_abort", False, f"operator fail-safe after the click: {exc}")
            return _Outcome(
                IntentStatus.UNCONFIRMED,
                "the operator fail-safe fired after the click; check the terminal and reconcile",
                "operator_abort",
            )
        except Exception as exc:
            log.exception("intent %s: error after the click", flow.intent.id)
            flow.record("error", False, f"{type(exc).__name__}: {exc}")
            return _Outcome(
                IntentStatus.UNCONFIRMED,
                f"error after the click ({type(exc).__name__}: {exc}); check the terminal and reconcile",
                "error",
            )

    def _confirm(self, flow: _Flow, plan: _ClickPlan, price: float | None) -> _Outcome:
        cfg = self._cfg
        lay = self._layout.layout
        win = flow.win
        if win is None:
            raise BackendError("internal error: no verified window after the click")
        poll = cfg.fill_poll_interval_seconds
        now = self._clock.monotonic()
        dialog_state: Literal["none", "pending", "confirmed", "absent"] = "pending" if plan.dialog else "none"
        dialog_deadline = now + cfg.dialog_timeout_seconds
        fill_deadline = min(now + cfg.fill_timeout_seconds, flow.deadline)
        max_iterations = math.ceil((cfg.dialog_timeout_seconds + cfg.fill_timeout_seconds) / poll) + 4
        error_seen: str | None = None
        streak = 0
        last_change = 0.0
        samples = 0
        skipped = 0
        for _ in range(max_iterations):
            if self._caller_gone(flow):
                break
            # error dialogs (12)
            if error_seen is None:
                for i, ref in enumerate(lay.error_templates):
                    if self._template_matches(win, ref):
                        error_seen = f"error_templates[{i}]"
                        if self._caller_gone(flow):
                            break
                        flow.record("error_dialog", False, f"{error_seen} matched after the click; pressing esc")
                        self._backend.press("esc")
                        self._delay()
                        break
            # confirmation dialog (12)
            if dialog_state == "pending" and plan.dialog is not None and error_seen is None:
                if self._template_matches(win, plan.dialog.template):
                    if self._caller_gone(flow):
                        break
                    cx, cy = self._screen(win, plan.dialog.confirm_button)
                    self._backend.click(cx, cy)
                    dialog_state = "confirmed"
                    flow.record("order_dialog", True, f"confirmation dialog matched; clicked confirm at {cx},{cy}")
                    fill_deadline = min(self._clock.monotonic() + cfg.fill_timeout_seconds, flow.deadline)
                elif self._clock.monotonic() >= dialog_deadline:
                    dialog_state = "absent"
                    flow.record("order_dialog", True, "no confirmation dialog appeared")
            # positions indicator (13)
            change = self._indicator_change(win, plan)
            if change is None:
                streak = 0
                skipped += 1
            else:
                samples += 1
                last_change = change
                streak = streak + 1 if change >= cfg.change_threshold else 0
                if streak >= CONFIRM_SAMPLES:
                    break
            now = self._clock.monotonic()
            if now >= flow.deadline or (now >= fill_deadline and dialog_state != "pending"):
                break
            self._backend.sleep(poll)
        changed = streak >= CONFIRM_SAMPLES
        flow.evidence["fill_check"] = {
            "changed": changed,
            "last_change": round(last_change, 4),
            "threshold": cfg.change_threshold,
            "samples": samples,
            "skipped": skipped,
            "dialog": dialog_state,
            "error": error_seen,
        }
        flow.record("fill_check", changed, f"positions indicator change {last_change:.1%} over {samples} sample(s)")
        if changed and error_seen is not None:
            return _Outcome(
                IntentStatus.UNCONFIRMED,
                "an error dialog appeared but the positions indicator also changed; check the terminal and reconcile",
                "fill",
            )
        if changed:
            return _Outcome(
                IntentStatus.CONFIRMED,
                f"{plan.what} clicked and the positions indicator changed ({last_change:.1%})",
                "confirmed",
                fill_price=price,
                fill_price_source="feed_estimate" if price is not None else "none",
            )
        if error_seen is not None and samples > 0:
            return _Outcome(
                IntentStatus.REJECTED,
                f"the terminal showed an error after the click ({error_seen}) and the positions indicator did not "
                "change",
                "rejected",
            )
        if error_seen is not None:
            # An error dialog alone proves nothing if the indicator was never readable afterwards.
            detail = (
                f"the terminal showed an error after the click ({error_seen}) but the positions indicator could "
                "not be checked"
            )
        elif dialog_state == "pending":
            detail = "the confirmation dialog never appeared"
        elif samples == 0:
            detail = (
                "the positions indicator could not be checked (terminal not in front, indicator covered, account "
                "badge not visible or the confirmation dialog still showing)"
            )
        else:
            detail = f"the positions indicator did not change within {cfg.fill_timeout_seconds:g}s"
        return _Outcome(IntentStatus.UNCONFIRMED, f"{detail}; check the terminal and reconcile", "fill")

    @staticmethod
    def _caller_gone(flow: _Flow) -> bool:
        """After the click: True (and recorded) once the caller gave up on this order.

        The watchdog has then already reported UNCONFIRMED and the operator may be reconciling from the terminal;
        a late confirm click would create a position nobody tracks, so the worker stops touching the terminal.
        """
        if not flow.ctl.abandoned:
            return False
        if not any(step["step"] == "abandoned" for step in flow.steps):
            flow.record("abandoned", False, "the caller stopped waiting after the click; no further input")
        return True

    def _indicator_change(self, win: WindowInfo, plan: _ClickPlan) -> float | None:
        """Changed fraction of the positions indicator, or None when the sample can't be trusted.

        The indicator, the account badge and (if calibrated) the confirmation dialog come from ONE screenshot:
        the sample only counts when that screenshot provably shows our terminal on the calibrated account (a
        black/garbage capture, e.g. from a minimized remote-desktop session, would otherwise look like a big
        change) and our confirmation dialog is no longer drawn over it.
        """
        lay = self._layout.layout
        region = lay.positions_indicator
        if not self._backend.input_desktop_available() or self._backend.foreground_window() != win.handle:
            return None
        if not self._region_unobstructed(win, region):
            return None
        refs = [lay.account_badge] + ([plan.dialog.template] if plan.dialog is not None else [])
        shots = self._capture_regions(win, [region, *(ref.region for ref in refs)])
        if not self._backend.input_desktop_available():
            return None
        if not self._image_matches(shots[1], lay.account_badge):
            return None
        if plan.dialog is not None and self._image_matches(shots[2], plan.dialog.template):
            return None
        return vision.changed_fraction(plan.baseline, shots[0])

    # ------------------------------------------------------------------ step 14
    def _finish(self, flow: _Flow, outcome: _Outcome) -> None:
        """Park the mouse, empty the clipboard (if the read-back used it) and save evidence. Never raises."""
        win = flow.win
        if win is not None and (flow.input_used or flow.clicked):
            try:
                px, py = self._screen(win, self._layout.layout.park_point)
                self._backend.move_mouse(px, py)
                flow.record("park_mouse", True, f"mouse parked at {px},{py}")
            except OperatorAbortError as exc:
                flow.operator_abort = True
                flow.record("park_mouse", False, f"operator fail-safe while parking the mouse: {exc}")
                if flow.clicked and outcome.status is not IntentStatus.UNCONFIRMED:
                    outcome.status = IntentStatus.UNCONFIRMED
                    outcome.message = (
                        f"the operator fail-safe fired after the click ({outcome.message}); "
                        "check the terminal and reconcile"
                    )
                    outcome.stage = "operator_abort"
                    outcome.fill_price = None
                    outcome.fill_price_source = "none"
            except Exception as exc:  # noqa: BLE001 - parking is best effort
                log.warning("intent %s: could not park the mouse: %s", flow.intent.id, exc)
                flow.record("park_mouse", False, f"could not park the mouse: {exc}")
        if flow.clipboard_touched:
            # The read-back replaced the clipboard with a sentinel and the fields' text. What was there before is
            # never read or put back: it may be a password the operator pasted into the login, and writing it back
            # would keep it around and drop a password manager's "not in clipboard history" marks. Leave it empty.
            try:
                self._backend.set_clipboard_text("")
                flow.record("clipboard", True, "clipboard emptied")
            except Exception as exc:  # noqa: BLE001 - best effort
                log.warning("intent %s: could not empty the clipboard: %s", flow.intent.id, exc)
                flow.record("clipboard", False, f"could not empty the clipboard: {exc}")
        if self._cfg.save_evidence:
            self._save_evidence(flow)

    def _save_evidence(self, flow: _Flow) -> None:
        win = flow.win
        try:
            if win is not None and flow.geometry_ok and self._backend.input_desktop_available():
                name = "after" if flow.clicked else "snapshot"
                flow.images[name] = self._capture_client(win)
        except Exception as exc:  # noqa: BLE001 - evidence is best effort
            log.warning("intent %s: evidence capture failed: %s", flow.intent.id, exc)
        if not flow.images:
            return
        folder = self._evidence_dir / _safe_component(flow.intent.id)
        files: dict[str, str] = {}
        try:
            folder.mkdir(parents=True, exist_ok=True)
            for name, img in flow.images.items():
                path = folder / f"{name}.png"
                img.save(path, format="PNG")
                files[name] = str(path)
        except (OSError, ValueError) as exc:
            log.warning("intent %s: could not save evidence to %s: %s", flow.intent.id, folder, exc)
            flow.evidence["evidence_error"] = str(exc)
        flow.evidence["files"] = files

    # ================================================================== health (worker thread)
    def _health_job(self, thorough: bool) -> VenueHealth:
        checks: list[HealthCheck] = []

        def add(c: _Check) -> bool:
            checks.append(HealthCheck(name=c.name, ok=c.ok, detail=c.detail, data=c.data))
            return c.ok

        try:
            if thorough:
                for c in self._static_checks():
                    add(c)
            add(self._check_account_kind())
            if self._expected_kind() is None:
                return self._health_result(checks)
            if not add(self._check_desktop()):
                return self._health_result(checks)
            located = self._check_window()
            if not add(located) or located.window is None:
                return self._health_result(checks)
            geometry, win = self._check_geometry(located.window)
            results = [add(c) for c in geometry]
            if not all(results) or win is None:
                return self._health_result(checks)
            if not add(self._check_focus(win)):
                return self._health_result(checks)
            add(self._check_badge(win))
            if self._layout.layout.one_click_indicator is not None:
                add(self._check_one_click(win))
            for symbol, panel in self._layout.layout.panels.items():
                add(self._check_label(win, symbol, panel))
        except OperatorAbortError as exc:
            add(
                _Check(
                    "gui",
                    False,
                    f"the operator fail-safe fired during the check ({exc}); move the mouse away "
                    "from the screen corner and retry",
                )
            )
        except Exception as exc:
            log.exception("UniTrader health check failed")
            add(_Check("gui", False, f"health check error: {type(exc).__name__}: {exc}"))
        return self._health_result(checks)

    def _static_checks(self) -> list[_Check]:
        lay = self._layout.layout
        cfg = self._cfg
        out: list[_Check] = [
            _Check(
                "layout",
                True,
                f"layout calibrated at {lay.calibrated_at.isoformat()} on a {lay.account_kind.upper()} account: "
                f"client {lay.client_width}x{lay.client_height} at {lay.dpi} DPI, {len(lay.panels)} panel(s)",
            )
        ]
        refs: dict[str, TemplateRef] = dict(self._layout.all_templates())
        if lay.order_dialog is not None:
            refs["order_dialog"] = lay.order_dialog.template
        if lay.close_all_dialog is not None:
            refs["close_all_dialog"] = lay.close_all_dialog.template
        for i, ref in enumerate(lay.error_templates):
            refs[f"error_templates[{i}]"] = ref
        for name, ref in refs.items():
            out.append(self._check_template_file(f"template:{name}", ref))
        for symbol, inst in self._instruments.items():
            if not inst.enabled:
                continue
            panel = lay.panels.get(symbol)
            if panel is None:
                out.append(
                    _Check(
                        f"instrument:{symbol}",
                        False,
                        f"{symbol} is enabled but has no calibrated panel: re-run desk calibrate for it or disable it",
                    )
                )
            elif cfg.protective_orders == "panel_fields":
                if panel.sl_input is None or panel.tp_input is None:
                    out.append(
                        _Check(
                            f"instrument:{symbol}",
                            False,
                            f"protective_orders: panel_fields but the {symbol} panel has no calibrated SL/TP inputs: "
                            "re-run desk calibrate",
                        )
                    )
                else:
                    out.append(_Check(f"instrument:{symbol}", True, "panel calibrated"))
            elif panel.sl_input is None or panel.tp_input is None:
                out.append(
                    _Check(
                        f"instrument:{symbol}",
                        True,
                        f"panel calibrated. WARNING: its SL/TP inputs are not calibrated, so the desk cannot empty "
                        f"them. If the {symbol} panel has stop-loss/take-profit fields, keep them EMPTY in UniTrader "
                        "(with protective_orders: none every order carries whatever they hold), or re-run desk "
                        "calibrate and record them",
                        {"warning": True},
                    )
                )
            else:
                out.append(
                    _Check(
                        f"instrument:{symbol}",
                        True,
                        "panel calibrated; its SL/TP fields are emptied and verified empty before every order "
                        "(protective_orders: none)",
                    )
                )
        if cfg.exit_method == "close_all":
            if lay.close_all_button is None:
                out.append(
                    _Check(
                        "exit_method",
                        False,
                        "exit_method close_all needs a calibrated close-all button: re-run desk calibrate and capture "
                        "it (otherwise positions can only be closed manually)",
                    )
                )
            else:
                out.append(_Check("exit_method", True, "close-all button calibrated"))
        elif cfg.protective_orders == "panel_fields":
            out.append(
                _Check(
                    "exit_method",
                    False,
                    "exit_method opposite_order cannot be combined with protective_orders: panel_fields (the desk "
                    "cannot verify the SL/TP fields are empty for the closing order); use exit_method: close_all",
                )
            )
        elif not cfg.netting_account_confirmed:
            out.append(
                _Check("exit_method", False, "exit_method opposite_order requires netting_account_confirmed: true")
            )
        else:
            note = "" if lay.close_all_button else "; no close-all button: PANIC can only engage the kill switch"
            out.append(_Check("exit_method", True, f"opposite-order exits on a netting account{note}"))
        problem = cfg.field_entry_problem()
        if problem is not None:
            out.append(_Check("field_entry", False, problem))
        elif cfg.lot_readback == "clipboard":
            out.append(
                _Check(
                    "field_entry",
                    True,
                    f"every field is read back through the clipboard after typing and again after {cfg.commit_key} "
                    "committed it (the clipboard is emptied afterwards, never restored)",
                )
            )
        else:
            out.append(_Check("field_entry", True, "lot_readback: none: typed values are not verified"))
        has_dialog = lay.order_dialog is not None or lay.close_all_dialog is not None
        needed = cfg.fill_timeout_seconds + (cfg.dialog_timeout_seconds if has_dialog else 0.0)
        if cfg.max_order_seconds <= needed:
            out.append(
                _Check(
                    "timing",
                    False,
                    f"max_order_seconds ({cfg.max_order_seconds:g}) must exceed fill_timeout_seconds"
                    f"{' + dialog_timeout_seconds' if has_dialog else ''} ({needed:g}) to leave time for the checks "
                    "before the click",
                )
            )
        else:
            out.append(
                _Check("timing", True, f"{cfg.max_order_seconds - needed:g}s of max_order_seconds for pre-click checks")
            )
        if self._dry_run:
            out.append(
                _Check(
                    "dry_run",
                    True,
                    "dry run: every pre-click check runs, up to the re-verification right before the click, but "
                    "nothing is typed or clicked (so the lot read-back is not exercised)",
                )
            )
        return out

    # ================================================================== individual checks (worker thread)
    def _check_desktop(self) -> _Check:
        if self._backend.input_desktop_available():
            return _Check("input_desktop", True, "input desktop available")
        return _Check(
            "input_desktop",
            False,
            "session locked/disconnected: the Windows desktop is not available for input or screenshots. Unlock the "
            "session yourself and keep it unlocked (see docs/WINDOWS_VPS.md), then retry.",
            {"needs_desktop": True},
        )

    def _check_account_kind(self) -> _Check:
        expected = self._expected_kind()
        kind = self._layout.layout.account_kind
        if expected is None:
            return _Check("account_kind", False, f"mode {self._mode.value!r} does not use the UniTrader terminal")
        if kind != expected:
            return _Check(
                "account_kind",
                False,
                f"wrong account / not logged in: the layout was calibrated on a {kind.upper()} account but the desk "
                f"runs in {self._mode.value} mode. Log in to your {self._account_label} account in UniTrader yourself "
                "and re-run desk calibrate.",
                {"layout_account_kind": kind, "mode": self._mode.value, "needs_login": True},
            )
        return _Check("account_kind", True, f"layout calibrated on the {kind.upper()} account, as the mode requires")

    def _check_window(self) -> _Check:
        lay = self._layout.layout
        login = f"open UniTrader and log in to your {self._account_label} account yourself, then retry."
        try:
            pattern = re.compile(lay.window_title_regex)
        except re.error as exc:
            return _Check("window", False, f"window_title_regex is invalid ({exc}): re-run desk calibrate")
        titled = [w for w in self._backend.list_windows() if w.visible and pattern.fullmatch(w.title)]
        wanted = lay.process_name
        if wanted:
            matching = [w for w in titled if w.process_name is not None and w.process_name.lower() == wanted.lower()]
        else:
            matching = titled
        if len(matching) == 1:
            win = matching[0]
            return _Check(
                "window", True, f"found {win.title!r} ({win.process_name or 'process unknown'})", _window_data(win), win
            )
        if len(matching) > 1:
            return _Check(
                "window",
                False,
                f"{len(matching)} windows match the calibrated UniTrader window: log in to UniTrader / close duplicate "
                "windows so exactly one terminal is open, then retry.",
                {"count": len(matching)},
            )
        if titled:
            procs = sorted({w.process_name or "unknown" for w in titled})
            return _Check(
                "window",
                False,
                f"UniTrader window not found: windows with a matching title belong to {', '.join(procs)}, not "
                f"{wanted}. Close them, {login}",
                {"needs_login": True, "processes": procs},
            )
        return _Check("window", False, f"UniTrader window not found: {login}", {"needs_login": True})

    def _check_geometry(self, win: WindowInfo) -> tuple[list[_Check], WindowInfo | None]:
        lay = self._layout.layout
        out: list[_Check] = []
        if win.minimized:
            self._backend.restore_window(win.handle)
            self._delay()
            refreshed = self._backend.get_window(win.handle)
            if refreshed is None:
                out.append(_Check("restore", False, "the UniTrader window disappeared while restoring it"))
                return out, None
            if refreshed.minimized:
                out.append(_Check("restore", False, "the UniTrader window is minimized and could not be restored"))
                return out, None
            out.append(_Check("restore", True, "restored the minimized window (no resize)"))
            win = refreshed
        size = (win.client_width, win.client_height)
        want = (lay.client_width, lay.client_height)
        if size == want:
            out.append(_Check("client_size", True, f"client area {size[0]}x{size[1]} as calibrated"))
        else:
            out.append(
                _Check(
                    "client_size",
                    False,
                    f"the UniTrader client area is {size[0]}x{size[1]} but the layout was calibrated at "
                    f"{want[0]}x{want[1]}: restore the window to the calibrated size (the desk never resizes it) or "
                    "re-run desk calibrate.",
                    {"actual": list(size), "calibrated": list(want)},
                )
            )
        if win.dpi == lay.dpi:
            out.append(_Check("dpi", True, f"{win.dpi} DPI as calibrated"))
        else:
            out.append(
                _Check(
                    "dpi",
                    False,
                    f"the UniTrader window runs at {win.dpi} DPI but the layout was calibrated at {lay.dpi} DPI: set "
                    "the Windows display scaling back to the calibrated level or re-run desk calibrate.",
                    {"actual": win.dpi, "calibrated": lay.dpi},
                )
            )
        return out, (win if all(c.ok for c in out) else None)

    def _check_focus(self, win: WindowInfo) -> _Check:
        cfg = self._cfg
        for attempt in range(1, cfg.focus_retries + 1):
            self._backend.activate_window(win.handle)
            self._delay()
            if self._backend.foreground_window() == win.handle:
                return _Check("focus", True, f"UniTrader is the foreground window (attempt {attempt})")
            if attempt < cfg.focus_retries and cfg.focus_retry_delay_seconds > 0:
                self._backend.sleep(cfg.focus_retry_delay_seconds)
        return _Check(
            "focus",
            False,
            f"could not bring the UniTrader window to the foreground after {cfg.focus_retries} attempt(s): close any "
            "dialog or window in front of it, then retry.",
        )

    def _check_badge(self, win: WindowInfo) -> _Check:
        acct = self._account_label

        def failed(score: float, threshold: float) -> str:
            return (
                f"wrong account / not logged in: the account badge does not match the calibrated {acct} badge "
                f"(score {score:.2f} < {threshold:.2f}). Log in to your {acct} account in UniTrader yourself, "
                "then retry."
            )

        check = self._check_template("account_badge", self._layout.layout.account_badge, win, failed)
        if not check.ok and "score" in check.data:
            return _Check(check.name, False, check.detail, {**check.data, "needs_login": True})
        return check

    def _check_one_click(self, win: WindowInfo) -> _Check:
        ref = self._layout.layout.one_click_indicator
        if ref is None:
            return _Check("one_click_indicator", True, "no one-click indicator calibrated")

        def failed(score: float, threshold: float) -> str:
            return (
                f"the one-click trading indicator does not match the calibration (score {score:.2f} < "
                f"{threshold:.2f}): enable one-click trading in UniTrader as it was during calibration, then retry."
            )

        return self._check_template("one_click_indicator", ref, win, failed)

    def _check_label(
        self, win: WindowInfo, symbol: str, panel: PanelLayout, *, shot: Image.Image | None = None
    ) -> _Check:
        def failed(score: float, threshold: float) -> str:
            return (
                f"the {symbol} panel label does not match the calibration (score {score:.2f} < {threshold:.2f}): the "
                "panel was moved, swapped, covered or closed. Restore the panel layout you calibrated (or re-run desk "
                "calibrate), then retry."
            )

        return self._check_template(f"panel:{symbol}", panel.label, win, failed, shot=shot)

    def _check_template_file(self, name: str, ref: TemplateRef) -> _Check:
        try:
            img = self._template(ref)
        except (OSError, ValueError) as exc:
            return _Check(
                name, False, f"reference image {ref.image} could not be loaded ({exc}): re-run desk calibrate"
            )
        if vision.is_blank(img):
            return _Check(
                name, False, f"reference image {ref.image} is blank and identifies nothing: re-run desk calibrate"
            )
        return _Check(name, True, f"reference image {ref.image} loaded")

    def _check_template(
        self,
        name: str,
        ref: TemplateRef,
        win: WindowInfo,
        failed: Callable[[float, float], str],
        *,
        shot: Image.Image | None = None,
    ) -> _Check:
        """Match ``ref`` against a fresh capture of its region (or ``shot``, a capture of exactly that region)."""
        file_check = self._check_template_file(name, ref)
        if not file_check.ok:
            return file_check
        score, delta, threshold = self._match(win, ref, shot)
        data = {"score": round(score, 4), "threshold": threshold, "color_delta": round(delta, 2)}
        if score >= threshold and delta <= MAX_TEMPLATE_COLOR_DELTA:
            return _Check(name, True, f"matches the calibration (score {score:.3f})", data)
        if score >= threshold:
            detail = failed(score, threshold) + f" (the colours differ by {delta:.0f} levels)"
            return _Check(name, False, detail, data)
        return _Check(name, False, failed(score, threshold), data)

    def _match(self, win: WindowInfo, ref: TemplateRef, shot: Image.Image | None = None) -> tuple[float, float, float]:
        template = self._template(ref)
        if shot is None:
            shot = self._capture_region(win, ref.region)
        threshold = ref.threshold if ref.threshold is not None else self._cfg.template_threshold
        return vision.similarity(shot, template), vision.mean_color_delta(shot, template), threshold

    def _template_matches(self, win: WindowInfo, ref: TemplateRef) -> bool:
        """Strict boolean match of a fresh capture (an unloadable/blank template never matches)."""
        if self._usable_template(ref) is None:
            return False
        return self._image_matches(self._capture_region(win, ref.region), ref)

    def _usable_template(self, ref: TemplateRef) -> Image.Image | None:
        try:
            template = self._template(ref)
        except (OSError, ValueError) as exc:
            log.warning("template %s unusable: %s", ref.image, exc)
            return None
        return None if vision.is_blank(template) else template

    def _image_matches(self, shot: Image.Image, ref: TemplateRef) -> bool:
        """Does an already captured image of ``ref.region`` match the reference (same rule as the checks)?"""
        template = self._usable_template(ref)
        if template is None or shot.size != template.size:
            return False
        threshold = ref.threshold if ref.threshold is not None else self._cfg.template_threshold
        score = vision.similarity(shot, template)
        return score >= threshold and vision.mean_color_delta(shot, template) <= MAX_TEMPLATE_COLOR_DELTA

    def _popup_templates(self) -> list[tuple[str, TemplateRef]]:
        """Calibrated dialogs and error messages: none of them may be showing before a click."""
        lay = self._layout.layout
        out: list[tuple[str, TemplateRef]] = []
        if lay.order_dialog is not None:
            out.append(("the order confirmation dialog", lay.order_dialog.template))
        if lay.close_all_dialog is not None:
            out.append(("the close-all confirmation dialog", lay.close_all_dialog.template))
        out.extend((f"an error message (error_templates[{i}])", ref) for i, ref in enumerate(lay.error_templates))
        return out

    # ================================================================== helpers (worker thread)
    def _template(self, ref: TemplateRef) -> Image.Image:
        cached = self._templates.get(ref.image)
        if cached is None or cached.size != (ref.region.w, ref.region.h):
            cached = self._layout.load_template(ref)
            self._templates[ref.image] = cached
        return cached

    @staticmethod
    def _screen(win: WindowInfo, point: Point) -> tuple[int, int]:
        return win.client_left + point.x, win.client_top + point.y

    def _capture_region(self, win: WindowInfo, region: Region) -> Image.Image:
        img = self._backend.capture(win.client_left + region.x, win.client_top + region.y, region.w, region.h)
        if img.size != (region.w, region.h):
            raise BackendError(f"capture returned {img.size[0]}x{img.size[1]}, expected {region.w}x{region.h}")
        return img if img.mode == "RGB" else img.convert("RGB")

    def _capture_regions(self, win: WindowInfo, regions: Sequence[Region]) -> list[Image.Image]:
        """Crops of several client regions taken from ONE screenshot of their bounding box (atomic)."""
        x0 = min(r.x for r in regions)
        y0 = min(r.y for r in regions)
        x1 = max(r.x + r.w for r in regions)
        y1 = max(r.y + r.h for r in regions)
        img = self._backend.capture(win.client_left + x0, win.client_top + y0, x1 - x0, y1 - y0)
        if img.size != (x1 - x0, y1 - y0):
            raise BackendError(f"capture returned {img.size[0]}x{img.size[1]}, expected {x1 - x0}x{y1 - y0}")
        rgb = img if img.mode == "RGB" else img.convert("RGB")
        return [rgb.crop((r.x - x0, r.y - y0, r.x - x0 + r.w, r.y - y0 + r.h)) for r in regions]

    def _capture_client(self, win: WindowInfo) -> Image.Image:
        return self._backend.capture(win.client_left, win.client_top, win.client_width, win.client_height)

    def _region_unobstructed(self, win: WindowInfo, region: Region) -> bool:
        """Every point of a grid over the region (~20 px apart, corners included) belongs to our window."""
        xs = _grid(win.client_left + region.x, region.w)
        ys = _grid(win.client_top + region.y, region.h)
        return all(self._backend.root_window_at(x, y) == win.handle for y in ys for x in xs)
