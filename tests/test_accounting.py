import pytest

from trader.config import CostConfig
from trader.execution.paper import PaperBroker
from trader.market import Fill, Level, OrderBook, Portfolio


def test_round_trip_pnl_and_fees():
    pf = Portfolio(1000)
    pf.apply_fill(Fill("X", 2, 100, 0.1, 0, "open"))
    pf.marks["X"] = 110
    assert pf.equity == pytest.approx(1000 - 0.1 + 20)
    pf.apply_fill(Fill("X", -2, 110, 0.1, 1, "close"))
    assert pf.realized_pnl == pytest.approx(20) and pf.equity == pytest.approx(1019.8)
    assert pf.positions["X"].qty == 0


def test_short_and_flip():
    pf = Portfolio(1000)
    pf.apply_fill(Fill("X", -1, 100, 0, 0, "short"))
    pf.apply_fill(Fill("X", 3, 90, 0, 1, "flip"))
    assert pf.realized_pnl == pytest.approx(10)
    assert pf.positions["X"].qty == 2 and pf.positions["X"].entry_px == 90


def test_paper_broker_walks_book_and_charges_costs():
    book = OrderBook(0, (Level(99, 1),), (Level(101, 1), Level(102, 1)))
    b = PaperBroker(CostConfig(taker_fee_bps=10, slippage_bps=0))
    f = b.execute("X", 2, 100, 0, "t", book)
    assert f.px == pytest.approx(101.5) and f.fee == pytest.approx(2 * 101.5 * 0.001)
    f = PaperBroker(CostConfig(taker_fee_bps=0, slippage_bps=10)).execute("X", -1, 100, 0, "t")
    assert f.px == pytest.approx(99.9)
