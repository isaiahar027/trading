"""Build the desk's components from a config (used by the CLI; tests build components directly)."""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from .audit import AuditLog
from .clock import Clock, SystemClock
from .config import ConfigError, DeskConfig
from .engine import DeskEngine
from .events import EventHub
from .execution.base import ExecutionVenue
from .killswitch import KillSwitch
from .llm.base import DisabledReviewer, TradeReviewer
from .marketdata.base import MarketDataFeed
from .news import NewsCalendar
from .notify import Notifier, build_notifier
from .store import Store

log = logging.getLogger(__name__)

TOKEN_FILE = "dashboard_token"  # noqa: S105 - a file name, not a secret


def data_dir(cfg: DeskConfig) -> Path:
    path = cfg.resolve_path(cfg.data_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_dashboard_token(cfg: DeskConfig) -> tuple[str, bool]:
    """The dashboard token: ``DESK_DASHBOARD_TOKEN`` if set, else a random one persisted under ``data_dir``.

    Returns ``(token, newly_generated)``.
    """
    if cfg.secrets.dashboard_token is not None:
        token = cfg.secrets.dashboard_token.get_secret_value()
        if len(token) < 16:
            raise ConfigError("DESK_DASHBOARD_TOKEN must be at least 16 characters (24+ recommended)")
        return token, False
    path = data_dir(cfg) / TOKEN_FILE
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 16:
            return token, False
    token = secrets.token_urlsafe(32)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    with contextlib.suppress(OSError):
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(path)
    return token, True


def build_feed(cfg: DeskConfig, clock: Clock) -> MarketDataFeed:
    tf = cfg.strategy.timeframe_minutes
    instruments = cfg.enabled_instruments
    kind = cfg.feed.kind
    if kind == "synthetic":
        from .marketdata.synthetic import SyntheticFeed  # noqa: PLC0415 - optional component

        return SyntheticFeed(cfg.feed, instruments, tf, clock)
    if kind == "csv":
        from .marketdata.csv_replay import CsvReplayFeed  # noqa: PLC0415

        assert cfg.feed.csv.path is not None  # enforced by config validation
        return CsvReplayFeed(cfg.resolve_path(cfg.feed.csv.path), tf, symbols=[i.symbol for i in instruments])
    if kind == "twelvedata":
        from .marketdata.twelvedata import TwelveDataFeed  # noqa: PLC0415

        key = cfg.secrets.twelvedata_api_key
        if key is None:
            raise ConfigError("feed.kind twelvedata needs the TWELVEDATA_API_KEY environment variable")
        return TwelveDataFeed(cfg.feed, instruments, tf, key.get_secret_value(), clock)
    raise ConfigError(f"unknown feed kind {kind}")


def build_reviewer(cfg: DeskConfig, clock: Clock) -> TradeReviewer:
    if not cfg.llm.enabled:
        return DisabledReviewer()
    from .llm.reviewer import ClaudeReviewer  # noqa: PLC0415

    return ClaudeReviewer(cfg.llm, clock)


def build_venue(cfg: DeskConfig, clock: Clock, killswitch: KillSwitch, feed: MarketDataFeed) -> ExecutionVenue:
    instruments = {i.symbol: i for i in cfg.instruments}
    if cfg.execution.venue == "paper":
        from .execution.paper import PaperVenue  # noqa: PLC0415

        return PaperVenue(instruments, cfg.execution.paper, clock, killswitch.engaged, feed.latest_quote)
    from .execution.unitrader.backend import GuiBackend  # noqa: PLC0415
    from .execution.unitrader.layout import LayoutFile  # noqa: PLC0415
    from .execution.unitrader.venue import UniTraderVenue  # noqa: PLC0415

    ut = cfg.execution.unitrader
    backend: GuiBackend
    if ut.backend == "fake":
        from .execution.unitrader.fake import FakeGuiBackend, FakeTerminal, calibrate_fake  # noqa: PLC0415

        log.warning("execution.unitrader.backend is 'fake': clicking a SIMULATED terminal, not UniTrader")
        terminal = FakeTerminal([i.symbol for i in cfg.enabled_instruments], badge="DEMO")
        layout = calibrate_fake(terminal, data_dir(cfg) / "simulated-layout")
        backend = FakeGuiBackend(terminal, real_sleep=True)
    else:
        layout_path = cfg.resolve_path(ut.layout_path)
        if not layout_path.exists():
            raise ConfigError(
                f"no calibrated layout at {layout_path}: log in to UniTrader yourself, then run `desk calibrate`"
            )
        layout = LayoutFile.load(layout_path)
        from .execution.unitrader.win32_backend import Win32Backend  # noqa: PLC0415

        backend = Win32Backend(pause_seconds=ut.action_delay_seconds)
    missing = [i.symbol for i in cfg.enabled_instruments if i.symbol not in layout.layout.panels]
    if missing:
        raise ConfigError(f"the calibrated layout has no panel for {', '.join(missing)}; re-run `desk calibrate`")
    return UniTraderVenue(
        ut,
        layout,
        backend,
        cfg.mode,
        instruments,
        clock,
        killswitch.engaged,
        data_dir(cfg) / "evidence",
        dry_run=cfg.execution.dry_run,
    )


@dataclass
class Desk:
    cfg: DeskConfig
    clock: Clock
    store: Store
    audit: AuditLog
    killswitch: KillSwitch
    hub: EventHub
    notifier: Notifier
    feed: MarketDataFeed
    venue: ExecutionVenue
    reviewer: TradeReviewer
    calendar: NewsCalendar
    engine: DeskEngine

    async def aclose(self) -> None:
        for closer in (self.feed.aclose, self.venue.aclose, self.notifier.aclose):
            with contextlib.suppress(Exception):
                await closer()
        aclose = getattr(self.reviewer, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()
        self.audit.close()
        self.store.close()


def build_desk(cfg: DeskConfig, *, clock: Clock | None = None) -> Desk:
    clock = clock or SystemClock()
    root = data_dir(cfg)
    store = Store(root / "desk.db")
    audit = AuditLog(root / "audit.jsonl", clock=clock)
    killswitch = KillSwitch(root, clock=clock)
    hub = EventHub(clock)
    notifier = build_notifier(cfg.notify, cfg.secrets, title_prefix=f"[{cfg.mode.value.upper()}] ", clock=clock)
    feed = build_feed(cfg, clock)
    venue = build_venue(cfg, clock, killswitch, feed)
    reviewer = build_reviewer(cfg, clock)
    calendar = NewsCalendar(cfg.news, cfg.root_dir)
    engine = DeskEngine(
        cfg,
        feed=feed,
        venue=venue,
        reviewer=reviewer,
        store=store,
        audit=audit,
        notifier=notifier,
        hub=hub,
        clock=clock,
        killswitch=killswitch,
        calendar=calendar,
    )
    return Desk(cfg, clock, store, audit, killswitch, hub, notifier, feed, venue, reviewer, calendar, engine)
