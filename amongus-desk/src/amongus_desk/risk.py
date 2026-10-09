"""Red Skeptic's math: position sizing and the hard risk gate.

Sizing (:func:`size_position`) is deterministic and never rounds a size *up*: the result is rounded down
to the instrument's lot step, capped, and refused (``lots=None``) when it falls below the minimum lot.
The gate (:class:`RiskGate`) lists every reason not to open a new position; any violation means no trade.

Lot arithmetic (``quantize_lots`` and friends) lives in :mod:`amongus_desk.money`; this module reuses it.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal, localcontext
from typing import Any

from .config import InstrumentConfig, RiskConfig, StrategyConfig
from .domain import Mode, PositionStatus, RiskSnapshot, Side, SizingResult, TradePlan
from .money import quantize_lots

log = logging.getLogger(__name__)

#: Working precision for the Decimal sizing arithmetic (inputs are floats with <= 17 significant digits).
_SIZING_PRECISION = 40
#: Relative tolerance when comparing |entry - stop| with the plan's stop distance (float noise only).
_STOP_DISTANCE_REL_TOL = 1e-9
#: Absolute tolerance for "spread > max spread" in pips (``0.0003 / 0.0001`` is ``2.9999999999999996``).
_SPREAD_PIPS_TOL = 1e-9

CAP_INSTRUMENT_MAX_LOT = "instrument.max_lot"
CAP_RISK_MAX_LOTS = "risk.max_lots_per_order"
CAP_MAX_NOTIONAL = "risk.max_notional_per_order"


def _dec(value: float) -> Decimal:
    """Exact decimal of the float's shortest repr (``0.1`` -> ``Decimal("0.1")``, not the binary expansion)."""
    return Decimal(repr(float(value)))


def _finite(value: float) -> bool:
    return isinstance(value, int | float) and math.isfinite(value)


def size_position(plan: TradePlan, instrument: InstrumentConfig, risk: RiskConfig, equity: float) -> SizingResult:
    """Size a position so that hitting the stop loses at most ``risk_per_trade_pct`` of ``equity``.

    ``risk_amount = equity * risk_per_trade_pct / 100`` and
    ``per_lot_risk = (stop_distance + spread_price) * contract_size * quote_to_account_rate``. Lots are
    ``risk_amount / per_lot_risk`` rounded **down** to ``lot_step``, capped at
    ``min(instrument.max_lot, risk.max_lots_per_order)`` and by ``risk.max_notional_per_order``. A size
    below ``min_lot`` gives ``lots=None`` (it is never rounded up to the minimum). Invalid inputs give
    ``lots=None`` with a reason.

    Conservative details: the spread comes from ``plan.spread_price``; if ``|entry_ref - stop_price|`` is
    larger than ``plan.stop_distance`` (beyond float noise), the larger distance is used so the real stop
    can never be farther away than the one that was sized; a stop on the wrong side of the entry is invalid.
    """
    ccy = risk.account_currency
    safe_equity = float(equity) if _finite(equity) else 0.0

    problems = _sizing_input_problems(plan, instrument, equity)
    if problems:
        reason = "invalid sizing inputs: " + "; ".join(problems)
        log.warning("%s: %s", plan.symbol, reason)
        return SizingResult(lots=None, equity=safe_equity, risk_amount=0.0, per_lot_risk=0.0, reason=reason)

    notes: list[str] = []
    stop_distance = plan.stop_distance
    actual_distance = abs(plan.entry_ref - plan.stop_price)
    if actual_distance > stop_distance * (1 + _STOP_DISTANCE_REL_TOL):
        notes.append(
            f"stop price is {actual_distance:.10g} from entry, farther than the planned {stop_distance:.10g}; "
            "sizing to the farther stop"
        )
        stop_distance = actual_distance

    step = instrument.lot_step
    with localcontext() as ctx:
        ctx.prec = _SIZING_PRECISION
        # Round every intermediate towards the smaller position: budget down, per-lot loss up, quotient down.
        ctx.rounding = ROUND_DOWN
        risk_amount = _dec(equity) * _dec(risk.risk_per_trade_pct) / Decimal(100)
        ctx.rounding = ROUND_UP
        per_lot_risk = (
            (_dec(stop_distance) + _dec(plan.spread_price))
            * _dec(instrument.contract_size)
            * _dec(instrument.quote_to_account_rate)
        )
        notional_per_lot = (
            _dec(plan.entry_ref) * _dec(instrument.contract_size) * _dec(instrument.quote_to_account_rate)
        )
        ctx.rounding = ROUND_DOWN
        raw_lots = risk_amount / per_lot_risk
        lots = quantize_lots(raw_lots, step)

        capped_by: str | None = None
        if risk.max_lots_per_order <= instrument.max_lot:
            lot_cap, lot_cap_name = risk.max_lots_per_order, CAP_RISK_MAX_LOTS
        else:
            lot_cap, lot_cap_name = instrument.max_lot, CAP_INSTRUMENT_MAX_LOT
        lot_cap = quantize_lots(lot_cap, step)
        if lots > lot_cap:
            notes.append(f"capped from {lots} to {lot_cap} lots by {lot_cap_name}")
            lots, capped_by = lot_cap, lot_cap_name

        notional_cap: Decimal | None = None
        if risk.max_notional_per_order is not None:
            notional_cap = quantize_lots(_dec(risk.max_notional_per_order) / notional_per_lot, step)
            if lots > notional_cap:
                notes.append(
                    f"capped from {lots} to {notional_cap} lots by {CAP_MAX_NOTIONAL} "
                    f"({risk.max_notional_per_order:.2f} {ccy}; {float(notional_per_lot):.2f} {ccy} per lot)"
                )
                lots, capped_by = notional_cap, CAP_MAX_NOTIONAL

    risk_amount_f = float(risk_amount)
    per_lot_f = float(per_lot_risk)
    base = (
        f"risk {risk_amount_f:.2f} {ccy} ({risk.risk_per_trade_pct}% of {safe_equity:.2f}) / per-lot risk "
        f"{per_lot_f:.2f} {ccy} = {float(raw_lots):.4f} lots"
    )
    if lots <= 0 or lots < instrument.min_lot:
        why = f"; {'; '.join(notes)}" if notes else ""
        reason = f"{base}{why}; {lots} lots is below the minimum lot {instrument.min_lot} (never rounded up): no trade"
        log.info("%s: %s", plan.symbol, reason)
        return SizingResult(
            lots=None,
            equity=safe_equity,
            risk_amount=risk_amount_f,
            per_lot_risk=per_lot_f,
            capped_by=capped_by,
            reason=reason,
        )

    at_risk = float(lots) * per_lot_f
    detail = f"{base} -> {lots} lots (rounded down to step {step}; about {at_risk:.2f} {ccy} at the stop)"
    reason = f"{detail}; {'; '.join(notes)}" if notes else detail
    return SizingResult(
        lots=lots,
        equity=safe_equity,
        risk_amount=risk_amount_f,
        per_lot_risk=per_lot_f,
        capped_by=capped_by,
        reason=reason,
    )


def _sizing_input_problems(plan: TradePlan, instrument: InstrumentConfig, equity: float) -> list[str]:
    problems: list[str] = []
    if plan.symbol != instrument.symbol:
        problems.append(f"plan symbol {plan.symbol} does not match instrument {instrument.symbol}")
    if not _finite(equity) or equity <= 0:
        problems.append(f"equity must be a positive number (got {equity!r})")
    for name in ("entry_ref", "stop_price", "stop_distance"):
        value = getattr(plan, name)
        if not _finite(value) or value <= 0:
            problems.append(f"{name} must be a positive number (got {value!r})")
    if not _finite(plan.spread_price) or plan.spread_price < 0:
        problems.append(f"spread_price must be zero or positive (got {plan.spread_price!r})")
    for name in ("contract_size", "quote_to_account_rate"):
        value = getattr(instrument, name)
        if not _finite(value) or value <= 0:
            problems.append(f"{instrument.symbol}.{name} must be a positive number (got {value!r})")
    if _finite(plan.entry_ref) and _finite(plan.stop_price):
        if plan.side is Side.BUY and not plan.stop_price < plan.entry_ref:
            problems.append(f"a BUY stop ({plan.stop_price}) must be below the entry ({plan.entry_ref})")
        if plan.side is Side.SELL and not plan.stop_price > plan.entry_ref:
            problems.append(f"a SELL stop ({plan.stop_price}) must be above the entry ({plan.entry_ref})")
    return problems


@dataclass(frozen=True)
class GateResult:
    """Outcome of :meth:`RiskGate.check`: ``ok`` only when ``violations`` is empty."""

    ok: bool
    violations: list[str]
    details: dict[str, Any]


class RiskGate:
    """Hard limits and system health checks that every new position must pass (stateless, thread-safe)."""

    def __init__(self, risk: RiskConfig, strategy: StrategyConfig) -> None:
        self._risk = risk
        self._strategy = strategy

    def check(
        self,
        plan: TradePlan,
        instrument: InstrumentConfig,
        sizing: SizingResult,
        snapshot: RiskSnapshot,
        *,
        last_bar_end: datetime | None,
        spread_price: float,
        mode: Mode,
    ) -> GateResult:
        """Return every violation that forbids opening ``plan`` now (``snapshot.now`` is "now")."""
        risk = self._risk
        now = snapshot.now
        violations: list[str] = []
        details: dict[str, Any] = {"symbol": plan.symbol, "mode": str(mode), "now": now.isoformat()}

        # --- system state ---------------------------------------------------------------------------
        if snapshot.kill_switch:
            violations.append(f"kill switch engaged: {snapshot.kill_reason or 'no reason given'}")
        if snapshot.reconcile_pending:
            ids = ", ".join(snapshot.reconcile_pending[:5])
            more = "" if len(snapshot.reconcile_pending) <= 5 else f" (+{len(snapshot.reconcile_pending) - 5} more)"
            violations.append(
                f"reconcile pending for {len(snapshot.reconcile_pending)} item(s): {ids}{more}; "
                "check the terminal and resolve them first"
            )
        if snapshot.degraded:
            violations.append("system health degraded: " + "; ".join(snapshot.degraded))

        # --- instrument -----------------------------------------------------------------------------
        if plan.symbol != instrument.symbol:
            violations.append(f"plan symbol {plan.symbol} does not match instrument {instrument.symbol}")
        if not instrument.enabled:
            violations.append(f"instrument {instrument.symbol} is disabled in the config")
        if mode in (Mode.DEMO, Mode.LIVE) and not instrument.specs_verified:
            violations.append(
                f"{instrument.symbol} contract specs are not verified: copy contract size, lot step and min/max "
                f"lot from the broker's own symbol specification into the config and set specs_verified: true "
                f"before trading in {mode} mode"
            )

        # --- exposure limits ------------------------------------------------------------------------
        active = [p for p in snapshot.open_positions if p.status is not PositionStatus.CLOSED]
        on_symbol = [p for p in active if p.symbol == plan.symbol]
        details.update(
            open_positions=len(active), positions_on_symbol=len(on_symbol), trades_today=snapshot.trades_today
        )
        if len(active) >= risk.max_open_positions:
            violations.append(f"max_open_positions reached: {len(active)} open (limit {risk.max_open_positions})")
        if len(on_symbol) >= risk.max_positions_per_symbol:
            violations.append(
                f"max_positions_per_symbol reached for {plan.symbol}: {len(on_symbol)} open "
                f"(limit {risk.max_positions_per_symbol})"
            )
        if snapshot.trades_today >= risk.max_trades_per_day:
            violations.append(
                f"max_trades_per_day reached: {snapshot.trades_today} today (limit {risk.max_trades_per_day})"
            )

        # --- loss limits ----------------------------------------------------------------------------
        breached, day_pnl, limit = self._daily_loss(snapshot)
        details.update(
            daily_pnl=day_pnl if math.isfinite(day_pnl) else None,
            daily_loss_limit=limit if math.isfinite(limit) else None,
        )
        if breached:
            if math.isfinite(day_pnl) and math.isfinite(limit) and limit > 0:
                violations.append(
                    f"daily loss limit hit: realized + unrealized {day_pnl:.2f} {risk.account_currency} <= "
                    f"-{limit:.2f} ({risk.max_daily_loss_pct}% of day-start equity "
                    f"{snapshot.day_start_equity:.2f})"
                )
            else:
                violations.append("daily loss cannot be evaluated (invalid equity / P&L figures): no new trades")

        streak_left = self._streak_cooldown_left(snapshot)
        details.update(consecutive_losses=snapshot.consecutive_losses)
        if streak_left is not None:
            details["loss_streak_cooldown_left_minutes"] = round(streak_left, 2)
            violations.append(
                f"loss streak: {snapshot.consecutive_losses} consecutive losses (limit {risk.max_consecutive_losses}); "
                f"cooling down for {streak_left:.0f} more minute(s)"
            )

        if snapshot.last_trade_at is not None and risk.min_minutes_between_trades > 0:
            since = _minutes_between(snapshot.last_trade_at, now)
            details["minutes_since_last_trade"] = round(since, 2)
            if since < risk.min_minutes_between_trades:
                violations.append(
                    f"min_minutes_between_trades: last trade {since:.1f} min ago "
                    f"(need {risk.min_minutes_between_trades})"
                )

        last_loss_symbol = snapshot.last_loss_at_by_symbol.get(plan.symbol)
        if last_loss_symbol is not None and risk.symbol_cooldown_minutes_after_loss > 0:
            since = _minutes_between(last_loss_symbol, now)
            details["minutes_since_symbol_loss"] = round(since, 2)
            if since < risk.symbol_cooldown_minutes_after_loss:
                violations.append(
                    f"symbol cooldown after a loss on {plan.symbol}: {since:.1f} min ago "
                    f"(need {risk.symbol_cooldown_minutes_after_loss})"
                )

        # --- market conditions ----------------------------------------------------------------------
        if not _finite(spread_price) or spread_price < 0:
            violations.append(f"spread is not a valid price distance ({spread_price!r})")
        else:
            spread_pips = spread_price / instrument.pip_size
            details["spread_pips"] = round(spread_pips, 4)
            max_spread = instrument.max_spread_pips
            if max_spread is not None and spread_pips > max_spread + _SPREAD_PIPS_TOL:
                violations.append(f"spread {spread_pips:.2f} pips exceeds max_spread_pips {max_spread:g}")

        stale_after = timedelta(minutes=self._strategy.stale_after_bars * self._strategy.timeframe_minutes)
        if last_bar_end is None:
            violations.append(f"no market data yet for {plan.symbol} (no closed bar received)")
        else:
            age = now - last_bar_end
            details["data_age_seconds"] = round(age.total_seconds(), 3)
            if age > stale_after:
                violations.append(
                    f"stale market data for {plan.symbol}: last bar closed {age.total_seconds() / 60:.1f} min ago "
                    f"(limit {stale_after.total_seconds() / 60:.0f} min = {self._strategy.stale_after_bars} bars)"
                )

        # --- size -----------------------------------------------------------------------------------
        violations.extend(self._size_violations(instrument, sizing))
        details["lots"] = str(sizing.lots) if sizing.lots is not None else None
        details["capped_by"] = sizing.capped_by

        ok = not violations
        if not ok:
            log.info("risk gate blocked %s: %s", plan.symbol, " | ".join(violations))
        return GateResult(ok=ok, violations=violations, details=details)

    def daily_loss_breached(self, snapshot: RiskSnapshot) -> bool:
        """``realized_pnl_today + unrealized_pnl <= -max_daily_loss_pct% of day_start_equity``.

        Fails closed: non-finite figures or a non-positive day-start equity count as breached.
        """
        return self._daily_loss(snapshot)[0]

    # ---------------------------------------------------------------------------------------- helpers
    def _daily_loss(self, snapshot: RiskSnapshot) -> tuple[bool, float, float]:
        day_pnl = snapshot.realized_pnl_today + snapshot.unrealized_pnl
        start = snapshot.day_start_equity
        if not (math.isfinite(day_pnl) and math.isfinite(start)) or start <= 0:
            return True, day_pnl, math.nan
        limit = start * self._risk.max_daily_loss_pct / 100
        return day_pnl <= -limit, day_pnl, limit

    def _streak_cooldown_left(self, snapshot: RiskSnapshot) -> float | None:
        """Minutes of loss-streak cooldown left, or ``None`` when the streak does not block."""
        risk = self._risk
        if snapshot.consecutive_losses < risk.max_consecutive_losses or risk.loss_streak_cooldown_minutes <= 0:
            return None
        if snapshot.last_loss_at is None:
            # A streak without a timestamp cannot be timed out: block for the full cooldown (fail closed).
            return float(risk.loss_streak_cooldown_minutes)
        since = _minutes_between(snapshot.last_loss_at, snapshot.now)
        if since >= risk.loss_streak_cooldown_minutes:
            return None
        return risk.loss_streak_cooldown_minutes - since

    def _size_violations(self, instrument: InstrumentConfig, sizing: SizingResult) -> list[str]:
        lots = sizing.lots
        if lots is None or lots <= 0:
            return [f"no position size: {sizing.reason}"]
        problems: list[str] = []
        if lots % instrument.lot_step != 0:
            problems.append(f"size {lots} is not a multiple of the lot step {instrument.lot_step}")
        if lots < instrument.min_lot:
            problems.append(f"size {lots} is below the minimum lot {instrument.min_lot}")
        cap = min(instrument.max_lot, self._risk.max_lots_per_order)
        if lots > cap:
            problems.append(f"size {lots} exceeds the lot cap {cap}")
        return problems


def _minutes_between(earlier: datetime, later: datetime) -> float:
    """Minutes from ``earlier`` to ``later`` (negative if ``earlier`` is in the future, which keeps cooldowns on)."""
    return (later - earlier).total_seconds() / 60
