"""Build the desk's components from a config (used by the CLI; tests build components directly)."""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .audit import AuditLog
from .clock import Clock, SystemClock
from .config import ConfigError, DeskConfig, InstrumentConfig
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

    The file is made readable by the current user only (see :func:`_restrict_to_owner`), also when it already
    exists. Returns ``(token, newly_generated)``.
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
            _restrict_to_owner(path)
            return token, False
    token = secrets.token_urlsafe(32)
    tmp = path.with_suffix(".tmp")
    os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
    _restrict_to_owner(tmp)  # before the token is in it
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    tmp.replace(path)  # a rename keeps the mode / ACL
    return token, True


def _restrict_to_owner(path: Path) -> None:
    """Best effort: only the current user may read ``path`` (mode 0600 on POSIX; an ACL on Windows)."""
    if sys.platform == "win32":
        _restrict_acl_to_current_user(path)
    else:
        with contextlib.suppress(OSError):
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _restrict_acl_to_current_user(path: Path) -> None:
    """Windows: drop the inherited ACL entries of ``path`` and grant the current user full control, nobody else.

    Windows ignores the 0600 mode (``chmod`` only sets the read-only flag), so without this the file inherits
    the folder's ACL, which under ``C:\\`` lets every local user read it. Logs a warning when it fails.
    """
    user = os.environ.get("USERNAME", "")
    if not user:
        log.warning("USERNAME is not set: could not restrict %s to the current user; other accounts may read it", path)
        return
    domain = os.environ.get("USERDOMAIN", "")
    account = f"{domain}\\{user}" if domain else user
    # the full path, so a stray icacls.exe in the working directory or on PATH can't stand in for it
    icacls = os.environ.get("SYSTEMROOT", r"C:\Windows") + r"\System32\icacls.exe"
    try:
        result = subprocess.run(  # noqa: S603 - fixed program, list argv, no shell
            [icacls, str(path), "/inheritance:r", "/grant:r", f"{account}:F"],
            capture_output=True,
            text=True,
            errors="replace",  # icacls prints in the console code page
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not restrict %s to %s (%s); other accounts on this machine may read it", path, account, exc)
        return
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit code {result.returncode}"
        log.warning("could not restrict %s to %s (icacls: %s); other accounts may read it", path, account, detail)


def managed_instruments(cfg: DeskConfig, store: Store) -> list[InstrumentConfig]:
    """Enabled instruments plus any instrument that still has an active position (its exits need bars)."""
    held = {p.symbol for p in store.active_positions()}
    return [i for i in cfg.instruments if i.enabled or i.symbol in held]


def build_feed(cfg: DeskConfig, clock: Clock, instruments: list[InstrumentConfig] | None = None) -> MarketDataFeed:
    tf = cfg.strategy.timeframe_minutes
    instruments = cfg.enabled_instruments if instruments is None else instruments
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


def build_reviewer(cfg: DeskConfig, clock: Clock, store: Store | None = None) -> TradeReviewer:
    if not cfg.llm.enabled:
        return DisabledReviewer()
    from .llm.reviewer import ClaudeReviewer  # noqa: PLC0415

    return ClaudeReviewer(cfg.llm, clock, state=store)


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
    feed = build_feed(cfg, clock, managed_instruments(cfg, store))
    venue = build_venue(cfg, clock, killswitch, feed)
    reviewer = build_reviewer(cfg, clock, store)
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
