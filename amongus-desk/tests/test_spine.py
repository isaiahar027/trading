"""Tests for the spine: domain types, config validation, money helpers, layout model, event hub, clocks."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from PIL import Image

from amongus_desk.clock import ManualClock
from amongus_desk.config import (
    LIVE_ACK_PHRASE,
    ConfigError,
    DeskConfig,
    Secrets,
    SessionWindow,
    load_config,
)
from amongus_desk.domain import Bar, IntentStatus, Mode, Quote, Side, ensure_utc
from amongus_desk.events import EventHub
from amongus_desk.execution.unitrader.layout import Layout, LayoutFile
from amongus_desk.money import estimate_pnl, format_lots, parse_lots, quantize_lots, trading_day_start
from factories import T0, eurusd, make_bar, make_config, make_instrument

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "desk.example.yaml"


# --------------------------------------------------------------------------------------------- domain


def test_side_helpers() -> None:
    assert Side.BUY.sign == 1 and Side.SELL.sign == -1
    assert Side.BUY.opposite is Side.SELL and Side.SELL.opposite is Side.BUY


def test_bar_validation() -> None:
    bar = make_bar(0, 1.1, 1.2, 1.0, 1.15)
    assert bar.end - bar.start == timedelta(minutes=5)
    assert bar.range == pytest.approx(0.2)
    with pytest.raises(ValueError, match="inconsistent OHLC"):
        make_bar(0, 1.1, 1.05, 1.0, 1.15)  # high below close
    with pytest.raises(ValueError, match="timezone-aware"):
        Bar("EURUSD", datetime(2026, 1, 1), 5, 1, 1, 1, 1)
    with pytest.raises(ValueError, match="positive"):
        make_bar(0, 1.0, 1.0, 0.0, 1.0)


def test_quote_validation() -> None:
    q = Quote("EURUSD", 1.1000, 1.1002, T0)
    assert q.mid == pytest.approx(1.1001)
    assert q.spread == pytest.approx(0.0002)
    with pytest.raises(ValueError):
        Quote("EURUSD", 1.1002, 1.1000, T0)


def test_ensure_utc_converts_offsets() -> None:
    eastern = datetime(2026, 1, 1, 7, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert ensure_utc(eastern) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_intent_status_flags() -> None:
    assert not IntentStatus.PENDING.is_final and not IntentStatus.SUBMITTING.is_final
    assert IntentStatus.UNCONFIRMED.needs_reconcile and IntentStatus.SUBMITTING.needs_reconcile
    assert not IntentStatus.ABORTED.needs_reconcile and IntentStatus.ABORTED.is_final


# --------------------------------------------------------------------------------------------- config


def _raw(**over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {"instruments": [eurusd()], "risk": {"starting_equity": 10000}}
    raw.update(over)
    return raw


def test_example_config_loads_and_is_paper() -> None:
    cfg = load_config(EXAMPLE, env={})
    assert cfg.mode is Mode.PAPER
    assert cfg.execution.venue == "paper"
    assert [i.symbol for i in cfg.enabled_instruments] == ["EURUSD", "GBPUSD"]
    # Nothing in the example claims verified broker specs.
    assert all(not i.specs_verified for i in cfg.instruments)
    assert cfg.resolve_path(Path("data")) == EXAMPLE.parents[1] / "data"


def test_secrets_never_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "desk.yaml"
    path.write_text(yaml.safe_dump({**_raw(), "secrets": {"dashboard_token": "x" * 30}}))
    with pytest.raises(ConfigError, match="environment variables"):
        load_config(path, env={})


def test_secrets_from_env() -> None:
    s = Secrets.from_env({"DESK_DASHBOARD_TOKEN": " s3cr3t-value ", "TWELVEDATA_API_KEY": ""})
    assert s.dashboard_token is not None and s.dashboard_token.get_secret_value() == "s3cr3t-value"
    assert s.twelvedata_api_key is None
    assert "s3cr3t" not in repr(s)


def test_live_requires_ack_and_protection() -> None:
    raw = _raw(mode="live", execution={"venue": "unitrader"})
    with pytest.raises(ValueError, match="DESK_LIVE_ACK"):
        DeskConfig.model_validate({**raw, "secrets": Secrets()})
    with pytest.raises(ValueError, match="protective_orders"):
        DeskConfig.model_validate({**raw, "secrets": Secrets(live_ack=LIVE_ACK_PHRASE)})
    protected = _raw(mode="live", execution={"venue": "unitrader", "unitrader": {"protective_orders": "panel_fields"}})
    with pytest.raises(ValueError, match="needs real market data"):
        DeskConfig.model_validate({**protected, "secrets": Secrets(live_ack=LIVE_ACK_PHRASE)})
    ok = DeskConfig.model_validate(
        {**protected, "feed": {"kind": "twelvedata"}, "secrets": Secrets(live_ack=LIVE_ACK_PHRASE)}
    )
    assert ok.mode is Mode.LIVE


def test_demo_on_the_real_terminal_needs_real_market_data() -> None:
    real = {"mode": "demo", "execution": {"venue": "unitrader", "unitrader": {"backend": "win32"}}}
    for kind in ("synthetic", "csv"):
        feed = {"kind": kind, "csv": {"path": "bars.csv"}} if kind == "csv" else {"kind": kind}
        with pytest.raises(ValueError, match="needs real market data"):
            DeskConfig.model_validate({**_raw(**real), "feed": feed, "secrets": Secrets()})
    assert DeskConfig.model_validate({**_raw(**real), "feed": {"kind": "twelvedata"}, "secrets": Secrets()})
    # The built-in simulated terminal may run on synthetic prices.
    simulated = {"mode": "demo", "execution": {"venue": "unitrader", "unitrader": {"backend": "fake"}}}
    assert DeskConfig.model_validate({**_raw(**simulated), "secrets": Secrets()})


def test_dotenv_loader_never_overrides_and_skips_blanks(tmp_path: Path) -> None:
    from amongus_desk.config import load_dotenv, project_root

    (tmp_path / ".env").write_text(
        "# comment\nANTHROPIC_API_KEY=\nTWELVEDATA_API_KEY='abc'\n"
        'export DESK_NTFY_TOPIC="t o p"\nX=keep\nnot a line\n',
        encoding="utf-8",
    )
    env = {"X": "already"}
    assert load_dotenv(tmp_path / ".env", env) == ["TWELVEDATA_API_KEY", "DESK_NTFY_TOPIC"]
    assert env == {"X": "already", "TWELVEDATA_API_KEY": "abc", "DESK_NTFY_TOPIC": "t o p"}
    assert load_dotenv(tmp_path / "missing.env", env) == []
    (tmp_path / "config").mkdir()
    assert project_root(tmp_path / "config" / "desk.yaml") == tmp_path.resolve()
    assert project_root(tmp_path / "desk.yaml") == tmp_path.resolve()


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"mode": "demo"}, "requires execution.venue: unitrader"),
        ({"execution": {"venue": "unitrader"}}, "requires execution.venue: paper"),
        (
            {
                "mode": "demo",
                "execution": {"venue": "unitrader"},
                "risk": {"starting_equity": 1, "max_open_positions": 2},
            },
            "close_all",
        ),
        (
            {"mode": "demo", "execution": {"venue": "unitrader", "unitrader": {"exit_method": "opposite_order"}}},
            "netting",
        ),
        ({"instruments": [eurusd(), eurusd()]}, "unique"),
        ({"instruments": [eurusd(enabled=False)]}, "at least one instrument"),
        ({"instruments": [eurusd(quote_currency="JPY")]}, "quote_to_account_rate"),
        ({"feed": {"kind": "csv"}}, "feed.csv.path"),
        ({"dashboard": {"host": "0.0.0.0"}}, "DESK_DASHBOARD_TOKEN"),
        ({"risk": {"starting_equity": 1, "risk_per_trade_pct": 2.5}}, "less than or equal to 2"),
        (
            {
                "mode": "demo",
                "execution": {
                    "venue": "unitrader",
                    "unitrader": {
                        "exit_method": "opposite_order",
                        "netting_account_confirmed": True,
                        "protective_orders": "panel_fields",
                    },
                },
            },
            "can't be combined",
        ),
        ({"execution": {"unitrader": {"max_order_seconds": 10, "fill_timeout_seconds": 8}}}, "max_order_seconds"),
        ({"feed": {"synthetic": {"start": "2026-10-05T08:00:00"}}}, "timezone-aware"),
    ],
)
def test_unsafe_configs_rejected(over: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        DeskConfig.model_validate(_raw(**over))


def test_public_dashboard_with_token_is_allowed() -> None:
    cfg = DeskConfig.model_validate(
        {**_raw(dashboard={"host": "0.0.0.0"}), "secrets": Secrets.from_env({"DESK_DASHBOARD_TOKEN": "t" * 32})}
    )
    assert cfg.dashboard.host == "0.0.0.0"


def test_instrument_lot_validation() -> None:
    with pytest.raises(ValueError, match="multiples of lot_step"):
        make_instrument(lot_step="0.1", min_lot="0.15")
    with pytest.raises(ValueError, match="min_lot exceeds max_lot"):
        make_instrument(min_lot="1.0", max_lot="0.5")


def test_session_window_crossing_midnight() -> None:
    w = SessionWindow(days=[4], start=time(22, 0), end=time(2, 0))  # Friday 22:00 -> Saturday 02:00
    fri = datetime(2026, 10, 9, 23, 0, tzinfo=UTC)
    sat_early = datetime(2026, 10, 10, 1, 0, tzinfo=UTC)
    sat_late = datetime(2026, 10, 10, 3, 0, tzinfo=UTC)
    thu_early = datetime(2026, 10, 8, 1, 0, tzinfo=UTC)  # belongs to Wednesday's session -> not in days
    assert w.contains(fri) and w.contains(sat_early)
    assert not w.contains(sat_late) and not w.contains(thu_early)


def test_load_config_reports_readable_errors(tmp_path: Path) -> None:
    path = tmp_path / "desk.yaml"
    path.write_text(yaml.safe_dump({"instruments": [], "risk": {}}))
    with pytest.raises(ConfigError) as exc:
        load_config(path, env={})
    assert "instruments" in str(exc.value) and "risk.starting_equity" in str(exc.value)
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml", env={})
    (tmp_path / "bad.yaml").write_text("a: [unclosed")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(tmp_path / "bad.yaml", env={})


# --------------------------------------------------------------------------------------------- money


@pytest.mark.parametrize(
    ("value", "step", "expected"),
    [("0.3333", "0.01", "0.33"), ("0.019", "0.01", "0.01"), ("0.009", "0.01", "0.00"), ("1.99", "0.1", "1.9")],
)
def test_quantize_lots_rounds_down(value: str, step: str, expected: str) -> None:
    assert quantize_lots(Decimal(value), Decimal(step)) == Decimal(expected)


def test_format_and_parse_lots() -> None:
    assert format_lots(Decimal("0.1"), Decimal("0.01")) == "0.10"
    assert format_lots(Decimal("0.3"), Decimal("0.01"), ",") == "0,30"
    with pytest.raises(ValueError):
        format_lots(Decimal("0.015"), Decimal("0.01"))
    with pytest.raises(ValueError):
        format_lots(Decimal("0"), Decimal("0.01"))
    assert parse_lots(" 0,10 ") == Decimal("0.10")
    assert parse_lots("0.10") == Decimal("0.1")
    assert parse_lots("") is None and parse_lots("abc") is None and parse_lots("NaN") is None


def test_estimate_pnl_signs() -> None:
    inst = make_instrument()
    assert estimate_pnl(Side.BUY, Decimal("0.10"), 1.1000, 1.1010, inst) == pytest.approx(10.0)
    assert estimate_pnl(Side.SELL, Decimal("0.10"), 1.1000, 1.1010, inst) == pytest.approx(-10.0)


def test_trading_day_start() -> None:
    rollover = time(22, 0)
    assert trading_day_start(datetime(2026, 10, 5, 23, 0, tzinfo=UTC), rollover) == datetime(
        2026, 10, 5, 22, 0, tzinfo=UTC
    )
    assert trading_day_start(datetime(2026, 10, 5, 21, 0, tzinfo=UTC), rollover) == datetime(
        2026, 10, 4, 22, 0, tzinfo=UTC
    )


# --------------------------------------------------------------------------------------------- layout


def _layout_dict() -> dict[str, Any]:
    return {
        "window_title_regex": "^Terminal",
        "client_width": 800,
        "client_height": 600,
        "dpi": 96,
        "account_kind": "demo",
        "account_badge": {"region": {"x": 10, "y": 10, "w": 40, "h": 12}, "image": "templates/badge.png"},
        "positions_indicator": {"x": 0, "y": 500, "w": 100, "h": 20},
        "panels": {
            "EURUSD": {
                "lot_input": {"x": 100, "y": 100},
                "buy_button": {"x": 150, "y": 140},
                "sell_button": {"x": 60, "y": 140},
                "label": {"region": {"x": 50, "y": 60, "w": 60, "h": 14}, "image": "templates/eurusd.png"},
            }
        },
        "park_point": {"x": 700, "y": 20},
        "calibrated_at": "2026-10-01T10:00:00Z",
    }


def test_layout_round_trip_and_templates(tmp_path: Path) -> None:
    lf = LayoutFile(Layout.model_validate(_layout_dict()), tmp_path)
    (tmp_path / "templates").mkdir()
    Image.new("RGB", (40, 12), "white").save(tmp_path / "templates" / "badge.png")
    Image.new("RGB", (10, 10), "white").save(tmp_path / "templates" / "eurusd.png")
    lf.save(tmp_path / "layout.yaml")
    again = LayoutFile.load(tmp_path / "layout.yaml")
    assert again.layout == lf.layout
    assert again.load_template(again.layout.account_badge).size == (40, 12)
    with pytest.raises(ValueError, match="region is 60x14"):
        again.load_template(again.layout.panels["EURUSD"].label)
    assert set(again.all_templates()) == {"account_badge", "panel:EURUSD"}


def test_layout_rejects_points_outside_client_and_path_escape(tmp_path: Path) -> None:
    bad = _layout_dict()
    bad["park_point"] = {"x": 900, "y": 20}
    with pytest.raises(ValueError, match="park_point"):
        Layout.model_validate(bad)
    escaping = _layout_dict()
    escaping["account_badge"]["image"] = "../../etc/passwd"
    lf = LayoutFile(Layout.model_validate(escaping), tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        lf.template_path(lf.layout.account_badge)


# --------------------------------------------------------------------------------------------- events & clocks


@pytest.mark.anyio
async def test_event_hub_fan_out_and_slow_subscriber() -> None:
    hub = EventHub(ManualClock(T0), history=3, queue_size=2)
    received: list[str] = []

    async def consume() -> None:
        async with hub.subscribe() as sub:
            async for event in sub:
                received.append(event.type)
                if len(received) == 2:
                    return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    assert hub.subscriber_count == 1
    hub.publish("a")
    hub.publish("b")
    hub.publish("c")  # queue full for the subscriber -> dropped, never blocks
    await asyncio.wait_for(task, 1)
    assert received == ["a", "b"]
    assert [e.type for e in hub.recent()] == ["a", "b", "c"]
    assert hub.subscriber_count == 0
    async with hub.subscribe() as sub:
        assert await sub.next_within(0.01) is None  # keep-alive path
        hub.publish("d")
        got = await sub.next_within(0.5)
        assert got is not None and got.type == "d"
    assert hub.subscriber_count == 0


@pytest.mark.anyio
async def test_manual_clock() -> None:
    clock = ManualClock(T0)
    await clock.sleep(30)
    assert clock.now() == T0 + timedelta(seconds=30)
    assert clock.monotonic() == 30
    clock.set(T0)  # going backwards in wall time never moves the monotonic clock backwards
    assert clock.monotonic() == 30


def test_make_config_factory_is_valid() -> None:
    cfg = make_config()
    assert cfg.instrument("EURUSD").specs_verified is True
    with pytest.raises(KeyError):
        cfg.instrument("XAUUSD")
