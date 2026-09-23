"""End-to-end: the same Engine the live loop uses, driven over synthetic markets."""
import math

import pytest

from trader.approvals import Approvals
from trader.backtest import run_backtest
from trader.brain import Brain, EscalationDesk, Verdict
from trader.config import BrainConfig, Config
from trader.engine import Engine, MarketData
from trader.execution.paper import PaperBroker
from trader.jev.decision import JevDecision
from trader.jev.offline import OfflineReflex
from trader.jev.schema import compile_schema
from trader.journal import Journal
from trader.market import Candle, Portfolio
from trader.risk import KillSwitch, RiskManager

from .conftest import H, T0, make_candles


class AlwaysLong:
    """A maximally overconfident fake reflex: always long, always sure."""
    def decide(self, state, schema):
        return JevDecision("trending", 0.9, {}, "long", 0.95, {"long": 0.97, "short": 0.01, "neutral": 0.02},
                           0.05, 3.0, 0.9, "safe", 0.95, model="fake", source="jev")


def crash_candles():
    up = make_candles(200, drift=0.001, vol=0.003, seed=3)
    out, px = list(up), up[-1].c
    for i in range(200, 400):  # steady 1%/bar bleed: the overconfident reflex keeps buying
        o, px = px, px * math.exp(-0.01)
        out.append(Candle(T0 + i * H, T0 + (i + 1) * H - 1, o, o * 1.001, px * 0.999, px, 1000))
    return out


def test_risk_layer_survives_an_overconfident_model(tmp_path):
    cfg = Config()
    c = crash_candles()
    res = run_backtest(cfg, {"X": compile_schema("X", {})}, {"X": c}, AlwaysLong(), state_dir=str(tmp_path))
    assert res.n_trades > 0
    # hard limit is 15%; allow one bar of gap-through on top, never a blow-up
    assert res.max_drawdown < 0.17
    assert res.total_return > -0.17


def test_offline_backtest_runs_and_journals(tmp_path):
    cfg = Config()
    c = {"BTC": make_candles(400, seed=1), "ETH": make_candles(400, seed=2, start=50)}
    res = run_backtest(cfg, {s: compile_schema(s, {}) for s in c}, c, OfflineReflex(), state_dir=str(tmp_path))
    kinds = {r["kind"] for r in Journal(tmp_path).read()}
    assert {"decision", "equity"} <= kinds and len(res.equity_curve) > 300


def test_kill_switch_file_flattens_everything(tmp_path):
    cfg = Config()
    c = make_candles(300, drift=0.002, vol=0.002)
    pf = Portfolio(10_000)
    ks = KillSwitch(tmp_path)
    ks.arm()
    eng = Engine(cfg, {"X": compile_schema("X", {})}, AlwaysLong(), PaperBroker(cfg.costs), pf,
                 RiskManager(cfg.risk, ks, 10_000, c[0].t_open), Journal(tmp_path))
    now = c[-2].t_close + 1
    eng.step(now, {"X": MarketData(c[:-1])})
    assert pf.positions["X"].qty > 0
    ks.trip("operator")
    rep = eng.step(c[-1].t_close + 1, {"X": MarketData(c)})
    assert pf.positions["X"].qty == 0 and rep.fills and "kill" in rep.fills[0].reason


class SlowBrain(Brain):
    def __init__(self):
        super().__init__(BrainConfig())
        self._client = None


def test_brain_fails_closed_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    v = Brain(BrainConfig()).review({"x": 1})
    assert v.action == "pause" and v.source == "fallback"


def test_escalation_blocks_entries_until_resolved(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    desk = EscalationDesk(SlowBrain(), cooldown_s=3600)
    assert desk.maybe_escalate("X", {}, now=1000)
    assert desk.blocks_entry("X", now=1000)
    assert not desk.maybe_escalate("X", {}, now=1001)  # cooldown/pending
    desk.pending["X"].result()
    verdicts = desk.collect(now=1002)
    assert verdicts["X"].action == "pause" and desk.blocks_entry("X", now=1002 + 3000)
    assert not desk.blocks_entry("X", now=1002 + 5 * 3600)


def test_protective_stop_placed_for_live_style_broker(tmp_path):
    cfg = Config()

    class Protecting(PaperBroker):
        placed = []
        def protect(self, symbol, qty, stop_px):
            self.placed.append((symbol, qty, stop_px))
            return 42
        def unprotect(self, symbol, oid):
            self.placed.append(("cancel", oid))

    c = make_candles(300, drift=0.002, vol=0.002)
    ks = KillSwitch(tmp_path)
    ks.arm()
    b = Protecting(cfg.costs)
    eng = Engine(cfg, {"X": compile_schema("X", {})}, AlwaysLong(), b, Portfolio(10_000),
                 RiskManager(cfg.risk, ks, 10_000, c[0].t_open), Journal(tmp_path))
    eng.step(c[-1].t_close + 1, {"X": MarketData(c)})
    sym, qty, stop = b.placed[0]
    assert sym == "X" and qty > 0 and stop < c[-1].c
    eng.flatten("X", c[-1].c, c[-1].t_close + 2, "exit: test", __import__("trader.engine").engine.StepReport())
    assert b.placed[-1] == ("cancel", 42)


def test_approval_gates_in_order(tmp_path):
    a = Approvals(tmp_path)
    with pytest.raises(ValueError):
        a.approve("3-plan", "op")
    for g in ("1-spec", "2-architecture", "3-plan", "4-build", "5-review"):
        a.approve(g, "op")
    assert a.missing() == ["6-ship"] and not a.all_approved()
    a.approve("6-ship", "op")
    assert a.all_approved()
