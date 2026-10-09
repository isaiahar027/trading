from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from amongus_desk.config import RiskConfig, StrategyConfig
from amongus_desk.domain import Mode, Position, PositionStatus, RiskSnapshot, Side, SizingResult, TradePlan
from amongus_desk.risk import CAP_INSTRUMENT_MAX_LOT, CAP_MAX_NOTIONAL, CAP_RISK_MAX_LOTS, RiskGate, size_position
from factories import T0, make_config, make_instrument


def plan(
    side: Side = Side.BUY, *, entry: float = 1.10000, stop_pips: float = 15, spread_pips: float = 1.0, **over: Any
) -> TradePlan:
    dist = stop_pips * 0.0001
    data: dict[str, Any] = {
        "symbol": "EURUSD",
        "side": side,
        "entry_ref": entry,
        "stop_price": round(entry - side.sign * dist, 5),
        "target_price": round(entry + side.sign * 2 * dist, 5),
        "stop_distance": dist,
        "atr": dist / 1.5,
        "target_r": 2.0,
        "time_stop_bars": 24,
        "spread_price": spread_pips * 0.0001,
    }
    data.update(over)
    return TradePlan.model_validate(data)


def risk(**over: Any) -> RiskConfig:
    return RiskConfig.model_validate({"starting_equity": 10_000, "max_lots_per_order": "1.00", **over})


# ------------------------------------------------------------------------------------------- sizing


def test_worked_example_rounds_down() -> None:
    # 10 000 * 0.5% = 50 USD budget; (15 + 1 pips) * 100 000 units = 160 USD per lot -> 0.3125 lots
    res = size_position(plan(), make_instrument(max_lot="1.00"), risk(risk_per_trade_pct=0.5), 10_000)
    assert res.risk_amount == pytest.approx(50.0)
    assert res.per_lot_risk == pytest.approx(160.0)
    assert res.lots == Decimal("0.31")  # 0.3125 rounded DOWN
    assert res.capped_by is None and "rounded down" in res.reason


def test_never_rounds_up_to_min_lot() -> None:
    res = size_position(plan(stop_pips=200), make_instrument(min_lot="0.01"), risk(risk_per_trade_pct=0.1), 1_000)
    # 1 USD budget / (201 pips * 10 USD) per lot = 0.0005 lots -> below 0.01
    assert res.lots is None and "never rounded up" in res.reason


@pytest.mark.parametrize(
    ("inst_max", "risk_max", "expected", "cap"),
    [("0.10", "1.00", "0.10", CAP_INSTRUMENT_MAX_LOT), ("1.00", "0.05", "0.05", CAP_RISK_MAX_LOTS)],
)
def test_lot_caps(inst_max: str, risk_max: str, expected: str, cap: str) -> None:
    res = size_position(
        plan(), make_instrument(max_lot=inst_max), risk(max_lots_per_order=risk_max, risk_per_trade_pct=2), 100_000
    )
    assert res.lots == Decimal(expected) and res.capped_by == cap


def test_notional_cap() -> None:
    # 0.20 lots of EURUSD at 1.1 = 22 000 USD notional; cap 11 000 -> 0.10 lots
    res = size_position(
        plan(), make_instrument(max_lot="1.00"), risk(risk_per_trade_pct=2, max_notional_per_order=11_000), 20_000
    )
    assert res.lots == Decimal("0.10") and res.capped_by == CAP_MAX_NOTIONAL


def test_quote_conversion_applies() -> None:
    inst = make_instrument(quote_currency="JPY", quote_to_account_rate=0.0067, pip_size=0.01, max_lot="5.00")
    p = plan(symbol="EURUSD", entry=160.00, stop_pips=0, stop_price=159.80, stop_distance=0.20, spread_price=0.02)
    res = size_position(p, inst, risk(risk_per_trade_pct=1, max_lots_per_order="5.00"), 10_000)
    # per lot: 0.22 * 100 000 * 0.0067 = 147.4 USD; 100 / 147.4 = 0.678 -> 0.67
    assert res.per_lot_risk == pytest.approx(147.4)
    assert res.lots == Decimal("0.67")


def test_farther_actual_stop_is_used() -> None:
    p = plan(stop_price=1.09700)  # 30 pips away while stop_distance says 15
    res = size_position(p, make_instrument(max_lot="1.00"), risk(risk_per_trade_pct=0.5), 10_000)
    assert res.per_lot_risk == pytest.approx((0.0030 + 0.0001) * 100_000)
    assert "farther" in res.reason


@pytest.mark.parametrize(
    "bad",
    [
        {"equity": 0},
        {"equity": float("nan")},
        {"plan_over": {"stop_price": 1.10100}},  # BUY stop above entry
        {"plan_over": {"symbol": "GBPUSD"}},
        {"plan_over": {"spread_price": -0.0001}},
    ],
)
def test_invalid_inputs(bad: dict[str, Any]) -> None:
    p = plan(**bad.get("plan_over", {}))
    res = size_position(p, make_instrument(), risk(), bad.get("equity", 10_000))
    assert res.lots is None and "invalid sizing inputs" in res.reason


def test_sell_plan_sizes_like_buy() -> None:
    buy = size_position(plan(Side.BUY), make_instrument(max_lot="1.00"), risk(risk_per_trade_pct=0.5), 10_000)
    sell = size_position(plan(Side.SELL), make_instrument(max_lot="1.00"), risk(risk_per_trade_pct=0.5), 10_000)
    assert buy.lots == sell.lots


# ------------------------------------------------------------------------------------------- gate


def snapshot(**over: Any) -> RiskSnapshot:
    data: dict[str, Any] = {
        "now": T0,
        "equity": 10_000.0,
        "day_start_equity": 10_000.0,
        "realized_pnl_today": 0.0,
        "unrealized_pnl": 0.0,
        "trades_today": 0,
        "open_positions": [],
        "consecutive_losses": 0,
        "last_trade_at": None,
        "last_loss_at": None,
    }
    data.update(over)
    return RiskSnapshot.model_validate(data)


def sizing(lots: str | None = "0.10") -> SizingResult:
    return SizingResult(
        lots=Decimal(lots) if lots else None, equity=10_000, risk_amount=25, per_lot_risk=160, reason="sized"
    )


def open_position(symbol: str = "EURUSD") -> Position:
    return Position(
        intent_id="i",
        symbol=symbol,
        side=Side.BUY,
        lots=Decimal("0.10"),
        entry_price=1.1,
        entry_price_source="venue",
        stop_price=1.099,
        target_price=1.103,
        opened_at=T0 - timedelta(hours=1),
        time_stop_bars=24,
        status=PositionStatus.OPEN,
    )


def gate(**risk_over: Any) -> RiskGate:
    return RiskGate(risk(**risk_over), StrategyConfig())


def check(
    g: RiskGate,
    *,
    snap: RiskSnapshot | None = None,
    size: SizingResult | None = None,
    mode: Mode = Mode.PAPER,
    spread: float = 0.0001,
    last_bar_end: Any = "default",
    **inst: Any,
):  # type: ignore[no-untyped-def]
    lbe = T0 - timedelta(minutes=1) if last_bar_end == "default" else last_bar_end
    return g.check(
        plan(),
        make_instrument(**inst),
        size or sizing(),
        snap or snapshot(),
        last_bar_end=lbe,
        spread_price=spread,
        mode=mode,
    )


def test_clean_pass() -> None:
    res = check(gate())
    assert res.ok and res.violations == []
    assert res.details["lots"] == "0.10"


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"snap": snapshot(kill_switch=True, kill_reason="panic")}, "kill switch"),
        ({"snap": snapshot(reconcile_pending=["abc"])}, "reconcile pending"),
        ({"snap": snapshot(degraded=["UniTrader window not found"])}, "degraded"),
        ({"snap": snapshot(open_positions=[open_position("GBPUSD")])}, "max_open_positions"),
        ({"snap": snapshot(trades_today=4)}, "max_trades_per_day"),
        ({"snap": snapshot(realized_pnl_today=-100, unrealized_pnl=-50.01)}, "daily loss limit"),
        ({"snap": snapshot(day_start_equity=0)}, "daily loss cannot be evaluated"),
        (
            {"snap": snapshot(consecutive_losses=3, last_loss_at=T0 - timedelta(minutes=10))},
            "loss streak",
        ),
        ({"snap": snapshot(last_trade_at=T0 - timedelta(minutes=5))}, "min_minutes_between_trades"),
        (
            {"snap": snapshot(last_loss_at_by_symbol={"EURUSD": T0 - timedelta(minutes=30)})},
            "symbol cooldown",
        ),
        ({"spread": 0.0004}, "exceeds max_spread_pips"),
        ({"spread": float("nan")}, "spread is not a valid"),
        ({"last_bar_end": T0 - timedelta(minutes=30)}, "stale market data"),
        ({"last_bar_end": None}, "no market data yet"),
        ({"size": sizing(None)}, "no position size"),
        ({"size": sizing("0.015")}, "not a multiple of the lot step"),
        ({"size": sizing("0.60")}, "exceeds the lot cap"),
        ({"enabled": False}, "disabled"),
        ({"mode": Mode.DEMO, "specs_verified": False}, "specs are not verified"),
    ],
)
def test_each_violation(kwargs: dict[str, Any], fragment: str) -> None:
    res = check(gate(), **kwargs)
    assert not res.ok
    assert any(fragment in v for v in res.violations), res.violations


def test_per_symbol_limit_with_higher_total() -> None:
    g = gate(max_open_positions=3, max_positions_per_symbol=1)
    res = check(g, snap=snapshot(open_positions=[open_position("EURUSD")]))
    assert [v for v in res.violations if "per_symbol" in v]
    assert not [v for v in res.violations if "max_open_positions" in v]


def test_paper_mode_ignores_unverified_specs() -> None:
    assert check(gate(), mode=Mode.PAPER, specs_verified=False).ok


def test_loss_streak_cooldown_expires() -> None:
    snap = snapshot(consecutive_losses=3, last_loss_at=T0 - timedelta(minutes=241))
    assert check(gate(), snap=snap).ok
    no_time = snapshot(consecutive_losses=3, last_loss_at=None)
    assert any("loss streak" in v for v in check(gate(), snap=no_time).violations)


def test_daily_loss_boundary_and_helper() -> None:
    g = gate(max_daily_loss_pct=1.5)
    at_limit = snapshot(realized_pnl_today=-150.0)
    assert g.daily_loss_breached(at_limit)
    assert not g.daily_loss_breached(snapshot(realized_pnl_today=-149.99))
    assert not check(g, snap=at_limit).ok


def test_spread_exactly_at_max_is_allowed() -> None:
    assert check(gate(), spread=0.0003).ok  # 3.0 pips == max_spread_pips (float noise tolerated)


def test_multiple_violations_are_all_reported() -> None:
    res = check(gate(), snap=snapshot(kill_switch=True, trades_today=9), size=sizing(None))
    assert len(res.violations) >= 3


def test_factory_config_is_consistent() -> None:
    cfg = make_config()
    assert cfg.risk.max_lots_per_order == Decimal("0.50")
