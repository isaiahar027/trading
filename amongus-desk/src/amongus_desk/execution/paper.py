"""Simulated venue for paper mode and backtests. No broker connection of any kind."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from ..clock import Clock
from ..config import InstrumentConfig, PaperConfig
from ..domain import (
    ExecutionReport,
    HealthCheck,
    IntentStatus,
    OrderIntent,
    Position,
    Quote,
    Side,
    VenueHealth,
)
from .base import BeforeClick, HaltCheck

log = logging.getLogger(__name__)


class PaperVenue:
    name = "paper"
    supports_protective_orders = True  # simulated stops/targets behave like broker-side orders

    def __init__(
        self,
        instruments: Mapping[str, InstrumentConfig],
        cfg: PaperConfig,
        clock: Clock,
        is_halted: HaltCheck,
        quote_source: Callable[[str], Quote | None],
    ) -> None:
        self._instruments = dict(instruments)
        self._cfg = cfg
        self._clock = clock
        self._is_halted = is_halted
        self._quote_source = quote_source

    async def preflight(self) -> VenueHealth:
        return self._health()

    async def health(self) -> VenueHealth:
        return self._health()

    def _health(self) -> VenueHealth:
        return VenueHealth(
            venue=self.name,
            ok=True,
            checked_at=self._clock.now(),
            checks=[HealthCheck(name="paper", ok=True, detail="simulated venue; nothing is sent to a broker")],
        )

    def fill_price(self, symbol: str, side: Side, quote: Quote | None, reference: float | None) -> float | None:
        """Price a market order: ask (+ slippage) for buys, bid (- slippage) for sells."""
        inst = self._instruments.get(symbol)
        if inst is None:
            return None
        quote = quote or self._quote_source(symbol)
        slip = self._cfg.slippage_pips * inst.pip_size
        if quote is not None:
            price = quote.ask + slip if side is Side.BUY else quote.bid - slip
        elif reference is not None and reference > 0:
            half = inst.typical_spread_price / 2
            price = reference + half + slip if side is Side.BUY else reference - half - slip
        else:
            return None
        return round(price, inst.price_decimals) if price > 0 else None

    async def submit(self, intent: OrderIntent, quote: Quote | None, on_before_click: BeforeClick) -> ExecutionReport:
        if intent.purpose != "open" or intent.side is None or intent.lots is None:
            return self._report(intent, IntentStatus.ABORTED, "not an opening order", stage="validate")
        if self._is_halted():
            return self._report(intent, IntentStatus.ABORTED, "kill switch engaged", stage="halted")
        return self._fill(intent, intent.side, quote, intent.entry_ref, on_before_click)

    async def close_position(
        self, position: Position, intent: OrderIntent, quote: Quote | None, on_before_click: BeforeClick
    ) -> ExecutionReport:
        reference = position.last_price if position.last_price is not None else position.entry_price
        return self._fill(intent, position.side.opposite, quote, reference, on_before_click, symbol=position.symbol)

    async def close_all(self, intent: OrderIntent, on_before_click: BeforeClick) -> ExecutionReport:
        started = self._clock.now()
        try:
            on_before_click()
        except Exception as exc:  # noqa: BLE001 - the write-ahead hook failing must abort, whatever the cause
            return self._report(intent, IntentStatus.ABORTED, f"write-ahead failed: {exc}", stage="write_ahead")
        now = self._clock.now()
        return ExecutionReport(
            intent_id=intent.id,
            status=IntentStatus.CONFIRMED,
            venue=self.name,
            started_at=started,
            finished_at=now,
            clicked_at=now,
            message="paper: all simulated positions closed",
        )

    def _fill(
        self,
        intent: OrderIntent,
        side: Side,
        quote: Quote | None,
        reference: float | None,
        on_before_click: BeforeClick,
        *,
        symbol: str | None = None,
    ) -> ExecutionReport:
        started = self._clock.now()
        sym = symbol or intent.symbol
        price = self.fill_price(sym, side, quote, reference)
        if price is None:
            return self._report(intent, IntentStatus.ABORTED, f"no price available for {sym}", stage="price")
        try:
            on_before_click()
        except Exception as exc:  # noqa: BLE001
            return self._report(intent, IntentStatus.ABORTED, f"write-ahead failed: {exc}", stage="write_ahead")
        now = self._clock.now()
        evidence: dict[str, Any] = {"side": side.value, "price": price}
        log.info("paper fill %s %s %s @ %s", intent.purpose, side.value, sym, price)
        return ExecutionReport(
            intent_id=intent.id,
            status=IntentStatus.CONFIRMED,
            venue=self.name,
            started_at=started,
            finished_at=now,
            clicked_at=now,
            fill_price=price,
            fill_price_source="venue",
            message=f"paper {side.value} {sym} filled at {price}",
            evidence=evidence,
        )

    def _report(self, intent: OrderIntent, status: IntentStatus, message: str, *, stage: str) -> ExecutionReport:
        now = self._clock.now()
        return ExecutionReport(
            intent_id=intent.id,
            status=status,
            venue=self.name,
            started_at=now,
            finished_at=now,
            message=message,
            stage=stage,
        )

    async def aclose(self) -> None:
        return None
