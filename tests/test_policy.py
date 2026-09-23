import pytest

from trader.config import Config, CostConfig, PolicyConfig
from trader.jev.decision import JevDecision
from trader.jev.schema import Calibration
from trader.policy import decide, entry_gates, kelly_fraction, should_escalate
from trader.state_engine import Snapshot
from trader.market import Position

CAL = Calibration()


def dec(**kw):
    base = dict(regime="trending", regime_conf=0.8, regime_probs={}, direction="long", direction_conf=0.9,
                direction_probs={"long": 0.93, "short": 0.02, "neutral": 0.05}, toxic_flow=0.1, setup_quality=2.5,
                setup_conf=0.7, risk_state="safe", risk_conf=0.9, model="jev-1.13.0")
    base.update(kw)
    return JevDecision(**base)


def snap(px=100.0, atr=1.0):
    return Snapshot("SOL", 0, 0, {}, {"px": px, "atr": atr})


def test_kelly_math():
    assert kelly_fraction(0.5, 1.0) == 0.0
    assert kelly_fraction(0.6, 1.0) == pytest.approx(0.2)
    assert kelly_fraction(0.3, 1.5) == 0.0


@pytest.mark.parametrize("kw,msg", [
    ({"setup_quality": 1.9}, "setup_quality"),
    ({"direction": "neutral"}, "neutral"),
    ({"direction_conf": 0.80}, "confidence"),   # must be ABOVE 0.80
    ({"risk_state": "near_limit"}, "risk_state"),
    ({"regime": "crisis"}, "crisis"),
    ({"toxic_flow": 0.6}, "toxic"),
])
def test_each_gate_blocks(kw, msg, portfolio):
    r = decide(snap(), dec(**kw), portfolio, PolicyConfig(), CostConfig(), CAL)
    assert r.intent is None and any(msg in x for x in r.reasons)


def test_entry_sizing_is_fractional_kelly(portfolio):
    cfg, costs = PolicyConfig(), CostConfig()
    r = decide(snap(), dec(), portfolio, cfg, costs, CAL)
    assert r.action == "enter_long" and r.intent.qty > 0
    stop, tgt, cost = 2.0, 3.0, costs.round_trip * 100
    b = (tgt - cost) / (stop + cost)
    p = CAL.apply(0.93)
    f = 0.25 * (p - (1 - p) / b)
    assert r.kelly == pytest.approx(f)
    assert r.intent.qty == pytest.approx(f * 10_000 / (stop + cost))
    assert r.intent.stop_px == pytest.approx(98.0) and r.intent.target_px == pytest.approx(103.0)


def test_kelly_fraction_hard_capped_even_if_config_bypassed(portfolio):
    greedy = PolicyConfig.__new__(PolicyConfig)
    object.__setattr__(greedy, "__dict__", {**PolicyConfig().__dict__, "kelly_fraction": 1.0})
    r1 = decide(snap(), dec(), portfolio, greedy, CostConfig(), CAL)
    r2 = decide(snap(), dec(), portfolio, PolicyConfig(), CostConfig(), CAL)
    assert r1.kelly == pytest.approx(r2.kelly)


def test_no_edge_after_costs(portfolio):
    expensive = CostConfig(taker_fee_bps=200, slippage_bps=200)
    r = decide(snap(), dec(), portfolio, PolicyConfig(), expensive, CAL)
    assert r.intent is None and "no edge" in r.reasons[0]


def test_short_entry(portfolio):
    d = dec(direction="short", direction_probs={"long": 0.02, "short": 0.93, "neutral": 0.05})
    r = decide(snap(), d, portfolio, PolicyConfig(), CostConfig(), CAL)
    assert r.intent.qty < 0 and r.intent.stop_px > 100 > r.intent.target_px


def _open(portfolio, qty=1.0):
    portfolio.positions["SOL"] = Position("SOL", qty=qty, entry_px=100, stop_px=98 if qty > 0 else 102,
                                          target_px=103 if qty > 0 else 97)


@pytest.mark.parametrize("px,d,reason", [
    (97.9, dec(), "stop hit"),
    (103.1, dec(), "target hit"),
    (100.5, dec(regime="crisis"), "crisis"),
    (100.5, JevDecision.abstain("down"), "reflex unavailable"),
    (100.5, dec(direction="short", direction_conf=0.9), "flip"),
])
def test_exits(portfolio, px, d, reason):
    _open(portfolio)
    r = decide(snap(px=px), d, portfolio, PolicyConfig(), CostConfig(), CAL)
    assert r.action == "exit" and r.intent.reduce_only and r.intent.qty == -1.0 and reason in r.intent.reason


def test_time_stop_and_reduce(portfolio):
    _open(portfolio)
    portfolio.positions["SOL"].bars_held = 48
    assert decide(snap(), dec(), portfolio, PolicyConfig(), CostConfig(), CAL).action == "exit"
    portfolio.positions["SOL"].bars_held = 1
    r = decide(snap(), dec(risk_state="reduce"), portfolio, PolicyConfig(), CostConfig(), CAL)
    assert r.action == "reduce" and r.intent.qty == -0.5


def test_escalation_rules():
    cfg = PolicyConfig()
    assert should_escalate(dec(direction_conf=0.5), cfg)
    assert should_escalate(dec(regime="crisis"), cfg)
    assert not should_escalate(dec(), cfg)
    assert not should_escalate(dec(direction_conf=0.5, source="offline"), cfg)


def test_decision_carries_no_size():
    fields = set(JevDecision.__dataclass_fields__)
    assert not fields & {"qty", "size", "notional", "price", "leverage", "stop", "target"}
