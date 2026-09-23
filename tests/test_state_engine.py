import pytest

from trader.state_engine import InsufficientData, LookaheadError, StateEngine, estimate_tokens
from trader.market import Position

from .conftest import H, make_book, make_candles, make_ctx


def build(eng, candles, pf, now, **kw):
    return eng.build("SOL", now, candles, pf, 10_000, 10_000, **kw)


def test_full_snapshot_under_400_tokens(portfolio):
    c = make_candles(500)
    now = c[-1].t_close + 1
    portfolio.positions["SOL"] = Position("SOL", qty=-3.0, entry_px=c[-1].c * 1.01, stop_px=c[-1].c * 1.05)
    s = build(StateEngine(), c, portfolio, now, book=make_book(c[-1].c, now - 10), ctx=make_ctx(now - 10),
              anchor={"trend": "strong_down", "vol": "extreme", "24b%": -12.34})
    assert s.tokens < 400
    assert estimate_tokens(s.jev_state) == s.tokens
    assert s.jev_state["pos"]["side"] == "short"
    assert {"book", "perp", "btc", "acct"} <= set(s.jev_state)


def test_future_candles_are_excluded(portfolio):
    c = make_candles(200)
    now = c[149].t_close + 1
    a = build(StateEngine(), c[:150], portfolio, now)
    b = build(StateEngine(), c, portfolio, now)  # same decision time, extra future data present
    assert a.jev_state == b.jev_state and a.numeric == b.numeric


def test_forming_candle_is_not_used(portfolio):
    c = make_candles(200)
    now = c[-1].t_close  # last candle not yet closed at this instant
    s = build(StateEngine(), c, portfolio, now)
    assert s.numeric["close"] == c[-2].c


def test_book_from_future_raises(portfolio):
    c = make_candles(200)
    now = c[-1].t_close + 1
    with pytest.raises(LookaheadError):
        build(StateEngine(), c, portfolio, now, book=make_book(100, now + 1))
    with pytest.raises(LookaheadError):
        build(StateEngine(), c, portfolio, now, ctx=make_ctx(now + 5))


def test_insufficient_history(portfolio):
    c = make_candles(30)
    with pytest.raises(InsufficientData):
        build(StateEngine(), c, portfolio, c[-1].t_close + 1)


def test_deterministic(portfolio):
    c = make_candles(300)
    now = c[-1].t_close + 1
    assert build(StateEngine(), c, portfolio, now).jev_state == build(StateEngine(), c, portfolio, now).jev_state


def test_trend_labels_follow_drift(portfolio):
    up = make_candles(300, drift=0.004, vol=0.002)
    down = make_candles(300, drift=-0.004, vol=0.002)
    eng = StateEngine()
    assert build(eng, up, portfolio, up[-1].t_close + 1).jev_state["trend"] in ("up", "strong_up")
    assert build(eng, down, portfolio, down[-1].t_close + 1).jev_state["trend"] in ("down", "strong_down")
