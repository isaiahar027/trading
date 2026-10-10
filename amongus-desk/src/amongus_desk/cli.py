"""``desk`` command-line interface.

Exit codes: 0 ok, 1 unexpected error, 2 configuration error, 3 another instance is running,
4 preflight failed / operator quit at the login gate, 130 interrupted.

The desk never asks for or handles broker credentials. In demo/live mode ``run`` and ``preflight`` first
check the terminal on screen; if UniTrader is missing or showing the wrong account, they tell you to log
in yourself and wait for you.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import threading
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal, InvalidOperation
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConfigError, DeskConfig, load_config, load_dotenv, project_root
from .domain import IntentStatus, Mode, OrderIntent, PositionStatus, Side, VenueHealth, utc_now
from .runtime import AlreadyRunningError, SingleInstanceLock, configure_logging

log = logging.getLogger(__name__)

EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_RUNNING, EXIT_PREFLIGHT, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 130
DEFAULT_CONFIG = "config/desk.yaml"

Prompt = Callable[[str], str]
Say = Callable[[str], None]


# ============================================================================================ helpers


def _load(args: argparse.Namespace) -> DeskConfig:
    return load_config(Path(args.config))


def _account(cfg: DeskConfig) -> str:
    return "LIVE (real money)" if cfg.mode is Mode.LIVE else "DEMO"


def _print_health(health: VenueHealth, say: Say) -> None:
    for check in health.checks:
        say(f"  {'✔' if check.ok else '✖'} {check.name}: {check.detail}")


async def login_gate(
    preflight: Callable[[], Awaitable[VenueHealth]],
    cfg: DeskConfig,
    *,
    interactive: bool,
    prompt: Prompt = input,
    say: Say = print,
    poll_seconds: float = 30.0,
    on_wait: Callable[[str], Awaitable[None]] | None = None,
    max_polls: int | None = None,
) -> bool:
    """Wait until the terminal passes preflight. The operator logs in; this only looks and asks.

    It re-checks on its own every ``poll_seconds`` and sends ``on_wait`` (the phone alert) on the first
    failure, interactive or not: a desk restarted unattended in a console must not sit silently at a
    prompt. Interactively, Enter re-checks at once and ``q`` quits.
    """
    polls = 0
    answer: asyncio.Future[str] | None = None
    while True:
        health = await preflight()
        if health.ok:
            say("✔ UniTrader preflight passed:")
            _print_health(health, say)
            return True
        say("✖ UniTrader is not ready:")
        _print_health(health, say)
        message = (
            f"Open UniTrader and log in to your {_account(cfg)} account yourself — this tool never asks for, "
            "stores or types your password — and open the one-click panels you calibrated."
        )
        say(message)
        if on_wait is not None and polls == 0:
            await on_wait(message)
        polls += 1
        if max_polls is not None and polls >= max_polls:
            return False
        if interactive:
            if answer is None:
                say(f"Re-checking every {poll_seconds:.0f} s. Press Enter to re-check now, or type q to quit.")
                answer = _ask_in_background(prompt, "> ")
            done, _ = await asyncio.wait({answer}, timeout=poll_seconds)
            if answer in done:
                try:
                    text = answer.result()
                except EOFError:  # no console after all: keep polling unattended
                    interactive = False
                    continue
                finally:
                    answer = None
                if text.strip().lower() in {"q", "quit", "exit"}:
                    return False
        else:
            say(f"(not interactive: re-checking in {poll_seconds:.0f} s)")
            await asyncio.sleep(poll_seconds)


def _ask_in_background(prompt: Prompt, text: str) -> asyncio.Future[str]:
    """Run a blocking prompt in a daemon thread, so an unanswered prompt never keeps the process alive."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def deliver(result: str | None, error: BaseException | None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result or "")

    def worker() -> None:
        try:
            result: str | None = prompt(text)
            error: BaseException | None = None
        except BaseException as exc:  # noqa: BLE001 - handed to the waiting coroutine
            result, error = None, exc
        with contextlib.suppress(RuntimeError):  # the loop may be gone if the gate passed meanwhile
            loop.call_soon_threadsafe(deliver, result, error)

    threading.Thread(target=worker, name="login-gate-prompt", daemon=True).start()
    return future


def _config_warnings(cfg: DeskConfig) -> list[str]:
    warnings: list[str] = []
    if cfg.mode in (Mode.DEMO, Mode.LIVE):
        for inst in cfg.enabled_instruments:
            if not inst.specs_verified:
                warnings.append(
                    f"{inst.symbol}: specs_verified is false — copy contract size / lot step / min lot from "
                    "UniTrader's symbol specification; the desk will not trade it until you do"
                )
        if cfg.execution.dry_run:
            warnings.append("execution.dry_run is true: every check runs, nothing is typed or clicked")
    has_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if cfg.llm.enabled and not has_key:
        warnings.append(
            f"llm.enabled but ANTHROPIC_API_KEY is not set: every Claude review will fail and Pink will "
            f"{'veto' if cfg.llm.failure_policy == 'veto' else 'abstain'} (failure_policy)"
        )
    if not (cfg.secrets.ntfy_topic or (cfg.secrets.telegram_bot_token and cfg.secrets.telegram_chat_id)):
        warnings.append("no phone alerts configured (DESK_NTFY_TOPIC or DESK_TELEGRAM_*)")
    return warnings


# ============================================================================================ commands


def cmd_init_config(args: argparse.Namespace) -> int:
    target = Path(args.path)
    if target.exists():
        print(f"{target} already exists; not overwriting it")
        return EXIT_OK
    target.parent.mkdir(parents=True, exist_ok=True)
    source = resources.files("amongus_desk").joinpath("templates/desk.example.yaml")
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"wrote {target} (mode: paper, synthetic feed). Edit it, then: desk run -c {target}")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    from .killswitch import KillSwitch  # noqa: PLC0415
    from .store import Store  # noqa: PLC0415
    from .wiring import data_dir  # noqa: PLC0415

    cfg = _load(args)
    root = data_dir(cfg)
    kill = KillSwitch(root).state()
    store = Store(root / "desk.db")
    try:
        active = store.active_positions()
        unresolved = store.intents_with_status(IntentStatus.SUBMITTING, IntentStatus.UNCONFIRMED)
        recent = store.recent_intents(10)
        meetings = store.recent_meetings(5)
    finally:
        store.close()
    if args.json:
        print(
            json.dumps(
                {
                    "mode": cfg.mode.value,
                    "kill_switch": kill,
                    "positions": [p.model_dump(mode="json") for p in active],
                    "reconcile": [r.intent.model_dump(mode="json") | {"status": r.status.value} for r in unresolved],
                    "recent_intents": [r.intent.model_dump(mode="json") | {"status": r.status.value} for r in recent],
                    "recent_meetings": [m.model_dump(mode="json") for m in meetings],
                },
                indent=2,
            )
        )
        return EXIT_OK
    print(f"mode: {cfg.mode.value}   kill switch: {'ENGAGED — ' + str(kill['reason']) if kill['engaged'] else 'off'}")
    print(f"open positions: {len(active)}")
    for p in active:
        levels = f"@ {p.entry_price} SL {p.stop_price} TP {p.target_price}"
        print(f"  {p.symbol} {p.side.value} {p.lots} {levels} [{p.status.value}]")
    reconcile_positions = [p for p in active if p.status is PositionStatus.RECONCILE]
    if unresolved or reconcile_positions:
        count = len(unresolved) + len(reconcile_positions)
        print(f"NEEDS RECONCILING ({count}): check UniTrader, then answer on the dashboard")
        for r in unresolved:
            it = r.intent
            print(f"  order {it.id} {it.purpose} {it.symbol} {it.side} {it.lots} [{r.status.value}]")
        for p in reconcile_positions:
            print(f"  position {p.id} {p.symbol} {p.side.value} {p.lots}")
    print("recent orders:")
    for r in recent:
        msg = r.report.message if r.report else ""
        print(
            f"  {r.intent.created_at:%Y-%m-%d %H:%M} {r.intent.purpose:9} {r.intent.symbol:7} {r.status.value:22} {msg}"
        )
    print("recent meetings:")
    for m in meetings:
        print(f"  {m.called_at:%Y-%m-%d %H:%M} {m.symbol:7} {m.decision.value:12} {'; '.join(m.reasons)[:100]}")
    return EXIT_OK


def cmd_kill(args: argparse.Namespace) -> int:
    """Engage the kill switch. This must work even when the config does not validate (e.g. a secret is
    missing in this shell), so it falls back to reading only ``data_dir`` from the YAML."""
    from .killswitch import KillSwitch  # noqa: PLC0415
    from .wiring import data_dir  # noqa: PLC0415

    try:
        root = data_dir(_load(args))
    except ConfigError as exc:
        fallback = _raw_data_dir(Path(args.config))
        if fallback is None:
            raise
        print(f"! the config does not validate ({exc}); engaging the kill switch in {fallback} anyway")
        fallback.mkdir(parents=True, exist_ok=True)
        root = fallback
    KillSwitch(root).engage(args.reason or "cli", by="cli")
    print("kill switch ENGAGED: no new trades (open positions keep their stops). `desk resume` to continue.")
    return EXIT_OK


def _raw_data_dir(config_path: Path) -> Path | None:
    """``data_dir`` straight from the YAML (no validation), resolved like :func:`load_config` does."""
    import yaml  # noqa: PLC0415

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    value = raw.get("data_dir", "data") if isinstance(raw, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return path if path.is_absolute() else project_root(config_path) / path


def cmd_resume(args: argparse.Namespace) -> int:
    from .killswitch import KillSwitch  # noqa: PLC0415
    from .store import Store  # noqa: PLC0415
    from .wiring import data_dir  # noqa: PLC0415

    cfg = _load(args)
    root = data_dir(cfg)
    store = Store(root / "desk.db")
    try:
        pending = store.intents_with_status(IntentStatus.SUBMITTING, IntentStatus.UNCONFIRMED)
        reconcile_positions = [p for p in store.active_positions() if p.status is PositionStatus.RECONCILE]
    finally:
        store.close()
    if pending or reconcile_positions:
        print(f"refusing: {len(pending) + len(reconcile_positions)} item(s) need reconciling first (see `desk status`)")
        return EXIT_PREFLIGHT
    KillSwitch(root).release(by="cli")
    print("kill switch released")
    return EXIT_OK


def cmd_audit_verify(args: argparse.Namespace) -> int:
    from .audit import AuditLog  # noqa: PLC0415
    from .wiring import data_dir  # noqa: PLC0415

    cfg = _load(args)
    result = AuditLog.verify(data_dir(cfg) / "audit.jsonl")
    print(("OK: " if result.ok else "TAMPERED/CORRUPT: ") + result.message)
    return EXIT_OK if result.ok else EXIT_ERROR


def cmd_backtest(args: argparse.Namespace) -> int:
    from .backtest import format_report, run_backtest  # noqa: PLC0415

    cfg = _load(args)
    if args.csv:
        from .marketdata.csv_replay import load_csv_bars  # noqa: PLC0415

        bars = load_csv_bars(Path(args.csv), timeframe_minutes=cfg.strategy.timeframe_minutes)
    else:
        bars = asyncio.run(_synthetic_bars(cfg, args.synthetic))
    if args.symbol:
        bars = {k: v for k, v in bars.items() if k == args.symbol}
    report = run_backtest(cfg, bars)
    print(json.dumps(report.to_dict(), indent=2) if args.json else format_report(report))
    return EXIT_OK


async def _synthetic_bars(cfg: DeskConfig, count: int) -> dict[str, list[Any]]:
    from datetime import timedelta  # noqa: PLC0415

    from .clock import ManualClock  # noqa: PLC0415
    from .marketdata.synthetic import SyntheticFeed  # noqa: PLC0415

    start = utc_now().replace(second=0, microsecond=0) - timedelta(minutes=cfg.strategy.timeframe_minutes * count)
    feed_cfg = cfg.feed.model_copy(
        update={"synthetic": cfg.feed.synthetic.model_copy(update={"interval_seconds": 0.0})}
    )
    clock = ManualClock(start)
    feed = SyntheticFeed(feed_cfg, cfg.enabled_instruments, cfg.strategy.timeframe_minutes, clock, max_bars=count)
    out: dict[str, list[Any]] = {i.symbol: [] for i in cfg.enabled_instruments}
    async for bar in feed.stream():
        out.setdefault(bar.symbol, []).append(bar)
    await feed.aclose()
    return out


def cmd_preflight(args: argparse.Namespace) -> int:
    cfg = _load(args)
    for warning in _config_warnings(cfg):
        print(f"! {warning}")
    if cfg.mode is Mode.PAPER:
        print("paper mode: nothing to check on screen (no GUI automation in paper mode)")
        return EXIT_OK
    from .wiring import build_desk  # noqa: PLC0415

    desk = build_desk(cfg)

    async def go() -> bool:
        try:
            if args.wait_for_login:
                return await login_gate(desk.venue.preflight, cfg, interactive=sys.stdin.isatty())
            health = await desk.venue.preflight()
            _print_health(health, print)
            if not health.ok:
                print(f"Log in to UniTrader ({_account(cfg)} account) yourself and re-run, or use --wait-for-login.")
            return health.ok
        finally:
            await desk.aclose()

    return EXIT_OK if asyncio.run(go()) else EXIT_PREFLIGHT


def cmd_calibrate(args: argparse.Namespace) -> int:
    from .execution.unitrader.calibrate import CalibrationError, run_calibration  # noqa: PLC0415

    cfg = _load(args)
    out = Path(args.out) if args.out else cfg.resolve_path(cfg.execution.unitrader.layout_path)
    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else [i.symbol for i in cfg.enabled_instruments]
    )
    if args.simulate:
        # The interactive wizard needs a real mouse over a real window; for the built-in fake terminal the
        # layout is generated programmatically instead.
        from .execution.unitrader.fake import FakeTerminal, calibrate_fake  # noqa: PLC0415

        layout = calibrate_fake(FakeTerminal(symbols, badge="DEMO"), out.parent / "simulated-layout")
        print(f"SIMULATION: wrote a layout for the built-in FAKE terminal under {layout.base_dir}.")
        print("It describes the simulator, not UniTrader; calibrate on the VPS for the real terminal.")
        return EXIT_OK
    from .execution.unitrader.win32_backend import Win32Backend, Win32UnavailableError  # noqa: PLC0415

    try:
        backend = Win32Backend(pause_seconds=cfg.execution.unitrader.action_delay_seconds)
    except Win32UnavailableError as exc:
        print(f"cannot calibrate here: {exc}")
        return EXIT_PREFLIGHT
    try:
        result = run_calibration(cfg, backend, out, symbols, countdown=args.countdown)
    except CalibrationError as exc:
        print(f"calibration stopped: {exc}")
        return EXIT_PREFLIGHT
    return EXIT_OK if result else EXIT_PREFLIGHT


def cmd_test_order(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if cfg.mode is Mode.LIVE and not args.i_know_this_is_live:
        print("refusing: mode is LIVE (real money). Add --i-know-this-is-live if you really mean it.")
        return EXIT_CONFIG
    try:
        inst = cfg.instrument(args.symbol)
    except KeyError:
        print(f"{args.symbol} is not in the config")
        return EXIT_CONFIG
    if cfg.mode in (Mode.DEMO, Mode.LIVE) and not inst.specs_verified:
        print(
            f"refusing: {inst.symbol} has specs_verified: false. Copy contract size, lot step and min lot from "
            "UniTrader's symbol specification into the config and set specs_verified: true first."
        )
        return EXIT_CONFIG
    if not inst.enabled:
        print(f"refusing: {inst.symbol} is disabled in the config")
        return EXIT_CONFIG
    for warning in _config_warnings(cfg):
        print(f"! {warning}")
    try:
        lots = Decimal(args.lots) if args.lots else inst.min_lot
    except InvalidOperation:
        print(f"invalid lots {args.lots!r}")
        return EXIT_CONFIG
    cap = min(inst.max_lot, cfg.risk.max_lots_per_order)
    if lots < inst.min_lot or lots > cap or lots % inst.lot_step != 0:
        print(f"lots must be a multiple of {inst.lot_step} between {inst.min_lot} and {cap}")
        return EXIT_CONFIG
    side = Side(args.side.upper())
    if cfg.mode is Mode.PAPER:
        venue_name = "the paper venue"
    elif cfg.execution.unitrader.backend == "fake":
        venue_name = "the SIMULATED terminal (backend: fake), not UniTrader"
    else:
        venue_name = f"UniTrader ({_account(cfg)} account)"
    phrase = f"{side.value} {inst.symbol}"
    print(f"About to place ONE market {side.value} of {lots} {inst.symbol} on {venue_name}.")
    if not args.yes:
        answer = input(f'Type "{phrase}" to confirm: ')
        if answer.strip().upper() != phrase:
            print("not confirmed; nothing was sent")
            return EXIT_OK
    with SingleInstanceLock(_lock_path(cfg)):
        return asyncio.run(_test_order(cfg, inst.symbol, side, lots, assume_yes=args.yes))


async def _test_order(cfg: DeskConfig, symbol: str, side: Side, lots: Decimal, *, assume_yes: bool) -> int:
    """Round trip: open ONE position, then close it with the configured exit method when you press Enter.

    Refused while the desk has anything open or unresolved (a close-all would flatten it behind the desk's
    back). Anything that can't be confirmed is recorded so the desk asks you to reconcile it on its next run.
    """
    from .domain import ExitReason  # noqa: PLC0415
    from .positions import PositionBook  # noqa: PLC0415
    from .wiring import build_desk  # noqa: PLC0415

    desk = build_desk(cfg)
    try:
        if desk.killswitch.engaged():
            print(f"refusing: the kill switch is engaged ({desk.killswitch.reason()}); `desk resume` first")
            return EXIT_PREFLIGHT
        active = desk.store.active_positions()
        unresolved = desk.store.intents_with_status(IntentStatus.SUBMITTING, IntentStatus.UNCONFIRMED)
        if active or unresolved:
            print(
                f"refusing: the desk has {len(active)} active position(s) and {len(unresolved)} unresolved "
                "order(s). Close or reconcile them first (see `desk status`)."
            )
            return EXIT_PREFLIGHT
        health = await desk.venue.preflight()
        _print_health(health, print)
        if not health.ok:
            print(f"preflight failed: log in to UniTrader ({_account(cfg)} account) yourself and retry")
            return EXIT_PREFLIGHT
        warm = await desk.feed.warmup([symbol], 1)
        quote = desk.feed.latest_quote(symbol)
        last = warm[symbol][-1].close if warm.get(symbol) else (quote.mid if quote is not None else None)
        if last is None:
            print("refusing: no market data for a reference price right now; retry when the feed is up")
            return EXIT_PREFLIGHT
        intent = OrderIntent(
            meeting_id=None,
            symbol=symbol,
            side=side,
            lots=lots,
            entry_ref=last,
            stop_price=None,
            target_price=None,
            mode=cfg.mode,
            created_at=desk.clock.now(),
        )
        desk.store.add_intent(intent)
        desk.audit.append("cli.test_order", intent.model_dump(mode="json"))

        def before_click() -> None:
            desk.store.set_intent_status(intent.id, IntentStatus.SUBMITTING)

        report = await desk.venue.submit(intent, quote, before_click)
        print(f"result: {report.status.value} — {report.message}")
        book = PositionBook(cfg)
        if report.status is not IntentStatus.CONFIRMED:
            desk.store.set_intent_status(intent.id, report.status, report)
            if report.status is IntentStatus.UNCONFIRMED:
                print("UNCONFIRMED: check UniTrader; the desk will ask you to reconcile this order on the next run")
            return EXIT_OK if report.status in (IntentStatus.DRY_RUN, IntentStatus.ABORTED) else EXIT_ERROR
        # A test order is a real position with no stop/target: it is recorded as RECONCILE (the desk tracks
        # it but never manages it), in the same transaction as the fill, so a desk started meanwhile halts
        # and asks about it.
        pos = book.add_unmanaged(
            intent, report, at=report.clicked_at or desk.clock.now(), note="a test order (desk test-order)"
        )
        desk.store.resolve_intent(intent.id, IntentStatus.CONFIRMED, report, [pos])
        close_all = cfg.execution.venue == "unitrader" and cfg.execution.unitrader.exit_method == "close_all"
        method = "the close-all button (it closes EVERY position in the terminal)" if close_all else "an opposite order"
        if not assume_yes:
            try:
                await asyncio.to_thread(input, f"Press Enter to close it with {method}: ")
            except (KeyboardInterrupt, EOFError):
                print("left open: close it in UniTrader yourself, then mark it closed on the dashboard")
                return EXIT_OK
        close_intent = OrderIntent(
            purpose="close_all" if close_all else "close",
            meeting_id=None,
            position_id=None if close_all else pos.id,
            symbol="*" if close_all else symbol,
            side=None if close_all else side.opposite,
            lots=None if close_all else lots,
            entry_ref=None if close_all else pos.entry_price,
            stop_price=None,
            target_price=None,
            mode=cfg.mode,
            created_at=desk.clock.now(),
        )
        desk.store.add_intent(close_intent)
        closing = book.set_status(pos.id, PositionStatus.CLOSING, close_intent_id=close_intent.id)
        desk.store.upsert_position(closing)

        def before_close() -> None:
            desk.store.set_intent_status(close_intent.id, IntentStatus.SUBMITTING)

        if close_all:
            closed_report = await desk.venue.close_all(close_intent, before_close)
        else:
            quote = desk.feed.latest_quote(symbol)
            closed_report = await desk.venue.close_position(closing, close_intent, quote, before_close)
        print(f"close: {closed_report.status.value} — {closed_report.message}")
        if closed_report.status is IntentStatus.CONFIRMED:
            price = closed_report.fill_price or pos.entry_price
            closed = book.close(
                pos.id, price=price, source="feed_estimate", reason=ExitReason.MANUAL, at=desk.clock.now()
            )
            desk.store.resolve_intent(close_intent.id, closed_report.status, closed_report, [closed])
        else:
            desk.store.set_intent_status(close_intent.id, closed_report.status, closed_report)
            desk.store.upsert_position(
                book.set_status(pos.id, PositionStatus.RECONCILE, note="the test order's close was not confirmed")
            )
            print("could not confirm the close: check UniTrader, close it yourself if needed, then reconcile")
        return EXIT_OK
    finally:
        await desk.aclose()


def _lock_path(cfg: DeskConfig) -> Path:
    from .wiring import data_dir  # noqa: PLC0415

    return data_dir(cfg) / "desk.lock"


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load(args)
    from .wiring import data_dir  # noqa: PLC0415

    log_file = configure_logging(data_dir(cfg), args.log_level)
    print(f"Among Us Trading Desk {__version__} — mode {cfg.mode.value.upper()} — log: {log_file}")
    for warning in _config_warnings(cfg):
        print(f"! {warning}")
    with SingleInstanceLock(_lock_path(cfg)):
        return asyncio.run(_run(cfg, dashboard=not args.no_dashboard))


async def _run(cfg: DeskConfig, *, dashboard: bool) -> int:
    import uvicorn  # noqa: PLC0415

    from .dashboard.server import create_app  # noqa: PLC0415
    from .wiring import build_desk, ensure_dashboard_token  # noqa: PLC0415

    desk = build_desk(cfg)
    try:
        if cfg.mode in (Mode.DEMO, Mode.LIVE):

            async def alert(message: str) -> None:
                await desk.notifier.notify("UniTrader needs you", message, priority="urgent", dedup_key="login-gate")

            if not await login_gate(desk.venue.preflight, cfg, interactive=sys.stdin.isatty(), on_wait=alert):
                print("stopped at the login gate; nothing was traded")
                return EXIT_PREFLIGHT
        await desk.engine.start()
        tasks: list[asyncio.Task[Any]] = [asyncio.create_task(desk.engine.run(), name="engine")]
        server: uvicorn.Server | None = None
        if dashboard and cfg.dashboard.enabled:
            token, generated = ensure_dashboard_token(cfg)
            app = create_app(desk.engine, cfg, token)
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=cfg.dashboard.host,
                    port=cfg.dashboard.port,
                    log_level="warning",
                    access_log=False,
                    server_header=False,
                    proxy_headers=False,
                    lifespan="off",
                )
            )
            tasks.append(asyncio.create_task(server.serve(), name="dashboard"))
            print(f"dashboard: http://{cfg.dashboard.host}:{cfg.dashboard.port}/")
            if generated:
                print(f"dashboard token (saved in {cfg.resolve_path(cfg.data_dir) / 'dashboard_token'}): {token}")
            else:
                print("dashboard token: from DESK_DASHBOARD_TOKEN or the data/dashboard_token file")
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await desk.engine.stop()
            if server is not None:
                server.should_exit = True
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                raise task.exception()  # type: ignore[misc]
        return EXIT_OK
    finally:
        await desk.aclose()


# ============================================================================================ main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="desk", description="Among Us multi-agent trading desk")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func: Callable[[argparse.Namespace], int], help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        if name != "init-config":
            p.add_argument("-c", "--config", default=DEFAULT_CONFIG, help=f"config file (default {DEFAULT_CONFIG})")
        return p

    p = add("run", cmd_run, "run the engine and the dashboard (login gate first in demo/live)")
    p.add_argument("--no-dashboard", action="store_true")
    p.add_argument("--log-level", default="INFO")
    p = add("preflight", cmd_preflight, "check config, layout, terminal window and account badge (never clicks)")
    p.add_argument("--wait-for-login", action="store_true", help="wait for you to log in to UniTrader, then re-check")
    p = add("calibrate", cmd_calibrate, "capture the UniTrader layout from your screen (hover, don't click)")
    p.add_argument("--simulate", action="store_true", help="calibrate the built-in fake terminal instead")
    p.add_argument("--symbols", help="comma-separated symbols (default: enabled instruments)")
    p.add_argument("--countdown", type=int, default=3)
    p.add_argument("--out", help="layout output path (default execution.unitrader.layout_path)")
    p = add("test-order", cmd_test_order, "round trip: open ONE position, then close it when you press Enter")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["BUY", "SELL", "buy", "sell"])
    p.add_argument("--lots", help="default: the instrument's min_lot")
    p.add_argument("--yes", action="store_true", help="skip the typed confirmations")
    p.add_argument("--i-know-this-is-live", action="store_true")
    p = add("backtest", cmd_backtest, "run the strategy and risk rules on historical bars")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--csv", help="CSV with symbol,start,open,high,low,close[,volume]")
    group.add_argument("--synthetic", type=int, metavar="N", help="N synthetic bars per symbol")
    p.add_argument("--symbol")
    p.add_argument("--json", action="store_true")
    p = add("kill", cmd_kill, "engage the kill switch (no new trades)")
    p.add_argument("--reason")
    add("resume", cmd_resume, "release the kill switch (refused while orders need reconciling)")
    p = add("status", cmd_status, "positions, orders, reconcile items and kill switch")
    p.add_argument("--json", action="store_true")
    add("audit-verify", cmd_audit_verify, "verify the hash-chained audit journal")
    p = add("init-config", cmd_init_config, "write an example config without overwriting anything")
    p.add_argument("--path", default=DEFAULT_CONFIG)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, "config", None):
            # Same .env the Windows run script loads, so every command sees the same secrets. Variables
            # already set in the environment win.
            load_dotenv(project_root(args.config) / ".env", os.environ)
        code: int = args.func(args)
        return code
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except AlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_RUNNING
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception:
        log.exception("unexpected error")
        print("unexpected error; see the log (data/logs/desk.log)", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
