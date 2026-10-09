from __future__ import annotations

import json
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from amongus_desk.backtest import BacktestReport, BacktestTrade, format_report, run_backtest
from amongus_desk.config import DeskConfig, Secrets
from amongus_desk.domain import Bar, Side
from factories import SMALL_STRATEGY, T0, breakout_series, eurusd, make_bar
from rigs import desk_cfg


def cfg(tmp_path: Path, **over: Any) -> DeskConfig:
    return desk_cfg(tmp_path, **over)


def follow(bars: list[Bar], o: float, h: float, low: float, c: float, symbol: str = "EURUSD") -> Bar:
    return make_bar(len(bars), o, h, low, c, symbol=symbol)


def test_signal_fills_at_next_open_and_exits_at_target(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    bars = breakout_series(1)
    sig_close = bars[-1].close
    nxt = follow(bars, sig_close + 0.0002, sig_close + 0.0040, sig_close + 0.0001, sig_close + 0.0035)
    bars.append(nxt)
    report = run_backtest(c, {"EURUSD": bars})
    assert report.signals == 1 and report.count == 1, report.skipped
    trade = report.trades[0]
    half = c.instrument("EURUSD").typical_spread_price / 2 + c.execution.paper.slippage_pips * 0.0001
    assert trade.entry_time == nxt.start  # never at the signal bar's close
    assert trade.entry_price == pytest.approx(nxt.open + half, abs=1e-5)
    assert trade.exit_reason == "TARGET"
    assert trade.exit_price == pytest.approx(trade.target_price - half, abs=1e-9)
    assert trade.pnl > 0 and trade.r_multiple > 1
    assert report.win_rate == 1.0 and report.ending_equity > c.risk.starting_equity


def test_gap_through_stop_skips(tmp_path: Path) -> None:
    bars = breakout_series(1)
    sig_close = bars[-1].close
    bars.append(follow(bars, sig_close - 0.0050, sig_close - 0.0049, sig_close - 0.0060, sig_close - 0.0055))
    report = run_backtest(cfg(tmp_path), {"EURUSD": bars})
    assert report.count == 0 and report.skipped["fill: opened through the stop"] == 1


def test_software_managed_exit_uses_bar_close(tmp_path: Path) -> None:
    c = cfg(tmp_path, mode="demo", execution={"venue": "unitrader", "unitrader": {"backend": "fake"}})
    bars = breakout_series(1)
    sig = bars[-1].close
    bars.append(follow(bars, sig, sig + 0.0001, sig - 0.0001, sig))
    bars.append(follow(bars, sig, sig + 0.0001, sig - 0.0030, sig - 0.0010))  # stop touched, closes higher
    report = run_backtest(c, {"EURUSD": bars})
    (trade,) = report.trades
    half = c.instrument("EURUSD").typical_spread_price / 2 + c.execution.paper.slippage_pips * 0.0001
    assert trade.exit_reason == "STOP" and trade.exit_price == pytest.approx(sig - 0.0010 - half)


def test_open_position_is_marked_at_the_end(tmp_path: Path) -> None:
    bars = breakout_series(1)
    sig = bars[-1].close
    bars.append(follow(bars, sig, sig + 0.0002, sig - 0.0002, sig + 0.0001))
    report = run_backtest(cfg(tmp_path), {"EURUSD": bars})
    (trade,) = report.trades
    assert trade.closed_at_end and trade.exit_reason == "END"


def test_simultaneous_signals_respect_max_open_positions(tmp_path: Path) -> None:
    raw = {
        "data_dir": str(tmp_path / "d"),
        "instruments": [eurusd(), eurusd(symbol="GBPUSD", feed_symbol="GBP/USD")],
        "strategy": {**SMALL_STRATEGY, "min_stop_spread_multiple": 2.0},
        "risk": {"starting_equity": 10_000, "max_lots_per_order": "0.50"},
        "secrets": Secrets(),
    }
    c = DeskConfig.model_validate(raw)
    a = breakout_series(1)
    b = breakout_series(1, symbol="GBPUSD")
    for series in (a, b):
        last = series[-1].close
        series.append(follow(series, last, last + 0.0001, last - 0.0001, last, symbol=series[-1].symbol))
    report = run_backtest(c, {"EURUSD": a, "GBPUSD": b})
    assert report.signals == 2 and report.count == 1
    assert report.skipped["Red Skeptic: max_open_positions (pending entry)"] == 1


def test_disabled_symbols_and_empty_input(tmp_path: Path) -> None:
    report = run_backtest(cfg(tmp_path), {"XAUUSD": [make_bar(0, 1, 1, 1, 1, symbol="XAUUSD")]})
    assert report.bars == 0 and report.count == 0
    assert "trades          0" in format_report(report)


@pytest.mark.anyio
async def test_runs_inside_an_event_loop(tmp_path: Path) -> None:
    bars = breakout_series(1)
    report = run_backtest(cfg(tmp_path), {"EURUSD": bars})
    assert report.signals == 1


def _trade(pnl: float, risk: float = 10.0, minutes: int = 0) -> BacktestTrade:
    t = T0 + timedelta(minutes=minutes)
    return BacktestTrade(
        symbol="EURUSD",
        side=Side.BUY,
        lots=Decimal("0.10"),
        entry_time=t,
        entry_price=1.1,
        stop_price=1.099,
        target_price=1.102,
        exit_time=t,
        exit_price=1.1,
        exit_reason="TARGET" if pnl > 0 else "STOP",
        pnl=pnl,
        risk_amount=risk,
    )


def test_statistics() -> None:
    report = BacktestReport(starting_equity=1000.0)
    report.trades = [_trade(20, minutes=1), _trade(-10, minutes=2), _trade(-10, minutes=3), _trade(30, minutes=4)]
    report.skipped = Counter({"Lime Charts: against the trend": 3})
    assert report.win_rate == 0.5
    assert report.avg_r == pytest.approx((2 - 1 - 1 + 3) / 4)
    assert report.expectancy_r == pytest.approx(0.5 * 2.5 - 0.5 * 1.0)
    assert report.profit_factor == pytest.approx(50 / 20)
    dd, dd_pct = report.max_drawdown
    assert dd == pytest.approx(20.0) and dd_pct == pytest.approx(20 / 1020 * 100)
    assert report.total_return_pct == pytest.approx(3.0)
    data = report.to_dict()
    json.dumps(data)
    assert data["trades"] == 4 and data["skipped"] == {"Lime Charts: against the trend": 3}
    text = format_report(report)
    assert "expectancy" in text and "too few to judge" in text and "against the trend" in text


def test_profit_factor_edge_cases() -> None:
    empty = BacktestReport(starting_equity=1000.0)
    assert empty.profit_factor is None and empty.expectancy_r == 0.0
    only_wins = BacktestReport(starting_equity=1000.0, trades=[_trade(5)])
    assert only_wins.profit_factor == float("inf") and only_wins.to_dict()["profit_factor"] == "inf"
