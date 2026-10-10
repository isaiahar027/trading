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
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal, InvalidOperation
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConfigError, DeskConfig, load_config
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
    """Wait until the terminal passes preflight. The operator logs in; this only looks and asks."""
    polls = 0
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
        if interactive:
            answer = await asyncio.to_thread(prompt, "Press Enter to re-check, or type q to quit: ")
            if answer.strip().lower() in {"q", "quit", "exit"}:
                return False
        else:
            if on_wait is not None and polls == 0:
                await on_wait(message)
            polls += 1
            if max_polls is not None and polls >= max_polls:
                return False
            say(f"(not interactive: re-checking in {poll_seconds:.0f} s)")
            await asyncio.sleep(poll_seconds)


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
    if cfg.llm.enabled:
        import os  # noqa: PLC0415

        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            warnings.append("llm.enabled but ANTHROPIC_API_KEY is not set (Pink will follow failure_policy)")
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
    from .killswitch import KillSwitch  # noqa: PLC0415
    from .wiring import data_dir  # noqa: PLC0415

    cfg = _load(args)
    KillSwitch(data_dir(cfg)).engage(args.reason or "cli", by="cli")
    print("kill switch ENGAGED: no new trades (open positions keep their stops). `desk resume` to continue.")
    return EXIT_OK


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
    """Round trip: open ONE position, then close it with the calibrated close method when you press Enter.

    Anything that can't be confirmed is recorded so the desk asks you to reconcile it on its next run.
    """
    from .positions import PositionBook  # noqa: PLC0415
    from .wiring import build_desk  # noqa: PLC0415

    desk = build_desk(cfg)
    try:
        health = await desk.venue.preflight()
        _print_health(health, print)
        if not health.ok:
            print(f"preflight failed: log in to UniTrader ({_account(cfg)} account) yourself and retry")
            return EXIT_PREFLIGHT
        last = None
        warm = await desk.feed.warmup([symbol], 1)
        if warm.get(symbol):
            last = warm[symbol][-1].close
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

        report = await desk.venue.submit(intent, desk.feed.latest_quote(symbol), before_click)
        desk.store.set_intent_status(intent.id, report.status, report)
        print(f"result: {report.status.value} — {report.message}")
        if report.status is not IntentStatus.CONFIRMED:
            if report.status is IntentStatus.UNCONFIRMED:
                print("UNCONFIRMED: check UniTrader; the desk will ask you to reconcile this order on the next run")
            return EXIT_OK if report.status in (IntentStatus.DRY_RUN, IntentStatus.ABORTED) else EXIT_ERROR
        # A test order is a real position with no stop/target: until it is closed it is recorded as RECONCILE,
        # so a desk started meanwhile halts and asks about it instead of managing it.
        book = PositionBook(cfg)
        ref = report.fill_price or last or 0.0
        recorded = intent.model_copy(update={"stop_price": ref, "target_price": ref})
        pos = book.add_open(recorded, report, protective=False, at=report.clicked_at or desk.clock.now())
        pos = book.set_status(pos.id, PositionStatus.RECONCILE)
        desk.store.upsert_position(pos)
        if not assume_yes:
            try:
                await asyncio.to_thread(input, "Press Enter to close it with the calibrated close method: ")
            except (KeyboardInterrupt, EOFError):
                print("left open: close it in UniTrader yourself, then mark it closed on the dashboard")
                return EXIT_OK
        close_intent = OrderIntent(
            purpose="close_all",
            meeting_id=None,
            symbol="*",
            side=None,
            lots=None,
            entry_ref=None,
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

        closed_report = await desk.venue.close_all(close_intent, before_close)
        desk.store.set_intent_status(close_intent.id, closed_report.status, closed_report)
        print(f"close: {closed_report.status.value} — {closed_report.message}")
        if closed_report.status is IntentStatus.CONFIRMED:
            from .domain import ExitReason  # noqa: PLC0415

            price = closed_report.fill_price or ref
            desk.store.upsert_position(
                book.close(pos.id, price=price, source="feed_estimate", reason=ExitReason.MANUAL, at=desk.clock.now())
            )
        else:
            desk.store.upsert_position(book.set_status(pos.id, PositionStatus.RECONCILE))
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
