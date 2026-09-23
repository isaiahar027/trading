import math
import random

import pytest

from trader.config import Config
from trader.market import Candle, Level, MarketCtx, OrderBook, Portfolio

H = 3_600_000
T0 = 1_750_000_000_000 // H * H


def make_candles(n=300, start=100.0, drift=0.0, vol=0.01, seed=1, t0=T0):
    rnd = random.Random(seed)
    out, px = [], start
    for i in range(n):
        o = px
        px = o * math.exp(drift + vol * rnd.gauss(0, 1))
        hi, lo = max(o, px) * (1 + vol / 3), min(o, px) * (1 - vol / 3)
        out.append(Candle(t0 + i * H, t0 + (i + 1) * H - 1, o, hi, lo, px, 1000 + rnd.random() * 100))
    return out


def make_book(mid, ts, depth=10, sz=5.0, tick=0.01):
    return OrderBook(ts, tuple(Level(mid - tick * (i + 1), sz) for i in range(depth)),
                     tuple(Level(mid + tick * (i + 1), sz) for i in range(depth)))


def make_ctx(ts, mark=100.0, funding=0.0000125):
    return MarketCtx(ts, funding, 1e6, mark, mark, 0.0001, 5e8, mark)


@pytest.fixture
def cfg():
    return Config()


@pytest.fixture
def portfolio():
    return Portfolio(cash=10_000.0)
