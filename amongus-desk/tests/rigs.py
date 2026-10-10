"""Shared engine test rig: in-memory feed, scriptable venue, and a fully wired DeskEngine."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from amongus_desk.audit import AuditLog
from amongus_desk.clock import ManualClock
from amongus_desk.config import DeskConfig, Secrets
from amongus_desk.domain import (
    Bar,
    ExecutionReport,
    HealthCheck,
    IntentStatus,
    OrderIntent,
    Position,
    Quote,
    VenueHealth,
)
from amongus_desk.engine import DeskEngine
from amongus_desk.events import EventHub
from amongus_desk.execution.paper import PaperVenue
from amongus_desk.killswitch import KillSwitch
from amongus_desk.llm.base import DisabledReviewer
from amongus_desk.news import NewsCalendar
from amongus_desk.notify import NullNotifier
from amongus_desk.store import Store
from factories import SMALL_STRATEGY, T0, breakout_series, eurusd, make_bar


class ListFeed:
    name = "list"

    def __init__(self, warm: dict[str, list[Bar]], live: Sequence[Bar], on_yield: Callable[[Bar], None]) -> None:
        self._warm = warm
        self._live = list(live)
        self._on_yield = on_yield

    async def warmup(self, symbols: Sequence[str], bars: int) -> dict[str, list[Bar]]:
        return {s: self._warm[s][-bars:] for s in symbols if s in self._warm}

    async def stream(self) -> AsyncIterator[Bar]:
        for bar in self._live:
            self._on_yield(bar)
            yield bar

    def latest_quote(self, symbol: str) -> Quote | None:
        return None

    def status(self) -> dict[str, Any]:
        return {"name": "list"}

    async def aclose(self) -> None:
        return None


class ScriptedVenue:
    """Returns scripted statuses; ``click`` controls whether the write-ahead hook runs first."""

    name = "scripted"

    def __init__(
        self, clock: ManualClock, status: IntentStatus, *, click: bool = True, protective: bool = False
    ) -> None:
        self.clock = clock
        self.status = status
        self.click = click
        self.supports_protective_orders = protective
        self.raise_after_click = False
        self.calls: list[str] = []
        self.health_ok = True
        self.operator_abort = False

    def _report(self, intent: OrderIntent, price: float | None = None) -> ExecutionReport:
        now = self.clock.now()
        return ExecutionReport(
            intent_id=intent.id,
            status=self.status,
            venue=self.name,
            started_at=now,
            finished_at=now,
            clicked_at=now if self.click else None,
            fill_price=price if self.status is IntentStatus.CONFIRMED else None,
            fill_price_source="feed_estimate" if self.status is IntentStatus.CONFIRMED else "none",
            message=f"scripted {self.status.value}",
            operator_abort=self.operator_abort,
        )

    async def _go(
        self, kind: str, intent: OrderIntent, before: Callable[[], None], price: float | None
    ) -> ExecutionReport:
        self.calls.append(kind)
        if self.click:
            before()
            if self.raise_after_click:
                raise RuntimeError("GUI exploded after the click")
        return self._report(intent, price)

    async def preflight(self) -> VenueHealth:
        return await self.health()

    async def health(self) -> VenueHealth:
        check = HealthCheck(
            name="window",
            ok=self.health_ok,
            detail="ok" if self.health_ok else "UniTrader window not found: log in to UniTrader yourself",
            data={} if self.health_ok else {"needs_login": True},
        )
        return VenueHealth(venue=self.name, ok=self.health_ok, checked_at=self.clock.now(), checks=[check])

    async def submit(
        self, intent: OrderIntent, quote: Quote | None, on_before_click: Callable[[], None]
    ) -> ExecutionReport:
        return await self._go("submit", intent, on_before_click, intent.entry_ref)

    async def close_position(
        self, position: Position, intent: OrderIntent, quote: Quote | None, on_before_click: Callable[[], None]
    ) -> ExecutionReport:
        return await self._go("close", intent, on_before_click, intent.entry_ref)

    async def close_all(self, intent: OrderIntent, on_before_click: Callable[[], None]) -> ExecutionReport:
        return await self._go("close_all", intent, on_before_click, None)

    async def aclose(self) -> None:
        return None


def desk_cfg(tmp_path: Path, **over: Any) -> DeskConfig:
    raw: dict[str, Any] = {
        "data_dir": str(tmp_path / "data"),
        "instruments": [eurusd()],
        "strategy": {**SMALL_STRATEGY, "min_stop_spread_multiple": 2.0, "time_stop_bars": 50},
        "risk": {"starting_equity": 10_000, "max_lots_per_order": "0.50", "risk_per_trade_pct": 0.5},
    }
    raw.update(over)
    raw["secrets"] = Secrets()
    return DeskConfig.model_validate(raw)


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        *,
        cfg: DeskConfig | None = None,
        venue: Callable[[ManualClock], Any] | None = None,
        live: Sequence[Bar] = (),
        reviewer: Any | None = None,
    ) -> None:
        self.cfg = cfg or desk_cfg(tmp_path)
        self.clock = ManualClock(T0)
        self.bars = breakout_series(1)
        data = Path(self.cfg.data_dir)
        self.store = Store(data / "desk.db")
        self.audit = AuditLog(data / "audit.jsonl", fsync=False, clock=self.clock)
        self.kill = KillSwitch(data, clock=self.clock)
        self.notifier = NullNotifier()
        self.hub = EventHub(self.clock)
        inst = self.cfg.instrument("EURUSD")
        self.venue = (
            venue(self.clock)
            if venue
            else PaperVenue(
                {inst.symbol: inst}, self.cfg.execution.paper, self.clock, self.kill.engaged, lambda _s: None
            )
        )
        warm = {"EURUSD": self.bars[:-1]}
        stream = list(live) if live else [self.bars[-1]]
        self.feed = ListFeed(warm, stream, lambda b: self.clock.set(b.end + timedelta(seconds=2)))
        self.engine = DeskEngine(
            self.cfg,
            feed=self.feed,
            venue=self.venue,
            reviewer=reviewer or DisabledReviewer(),
            store=self.store,
            audit=self.audit,
            notifier=self.notifier,
            hub=self.hub,
            clock=self.clock,
            killswitch=self.kill,
            calendar=NewsCalendar(self.cfg.news, tmp_path),
        )

    async def boot_and_run(self) -> None:
        self.clock.set(self.bars[-2].end + timedelta(seconds=1))
        await self.engine.start()
        await self.engine.run()

    def titles(self) -> list[str]:
        return [t for t, _m, _p in self.notifier.sent]

    def close(self) -> None:
        self.audit.close()
        self.store.close()


def target_bar(rig: Rig, index_offset: int = 0) -> Bar:
    last = rig.bars[-1]
    i = len(rig.bars) + index_offset
    return make_bar(i, last.close, last.close + 0.0030, last.close - 0.0001, last.close + 0.0028)


def stop_bar(rig: Rig, index_offset: int = 0) -> Bar:
    last = rig.bars[-1]
    i = len(rig.bars) + index_offset
    return make_bar(i, last.close, last.close + 0.0001, last.close - 0.0030, last.close - 0.0028)
