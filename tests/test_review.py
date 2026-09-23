import pytest

from trader.jev.schema import Calibration
from trader.review.calibration import brier_binary, brier_multiclass, fit_calibration, reliability
from trader.review.nightly import label_decisions, same_shape
from trader.jev.schema import compile_schema


def test_brier():
    assert brier_multiclass({"long": 1, "short": 0, "neutral": 0}, "long") == 0
    assert brier_multiclass({"long": 1 / 3, "short": 1 / 3, "neutral": 1 / 3}, "short") == pytest.approx(2 / 3)
    assert brier_binary(0.7, True) == pytest.approx(0.09)


def test_fit_is_monotone_and_needs_samples():
    pairs = [(0.82, True)] * 30 + [(0.82, False)] * 10 + [(0.87, True)] * 10 + [(0.87, False)] * 20 \
        + [(0.96, True)] * 5
    cal = fit_calibration(pairs, Calibration())
    rates = [w for w in cal.win_rate if w is not None]
    assert rates == sorted(rates)
    assert cal.win_rate[4] is None  # 5 samples < 20: no evidence yet
    assert cal.apply(0.97) == Calibration().apply(0.97)


def test_calibration_blends_toward_observed():
    cal = Calibration(win_rate=[None, 0.4, None, None, None], samples=[0, 300, 0, 0, 0])
    assert 0.4 < cal.apply(0.82) < 0.45
    assert reliability([(0.82, True), (0.83, False)], cal.edges)[1]["observed"] == 0.5


def _dec(i, px, direction="long", atr=1.0):
    return {"kind": "decision", "symbol": "X", "ts": i, "numeric": {"px": px, "atr": atr}, "action": "no_trade",
            "reasons": ["setup_quality 1"], "jev": {"direction": direction, "setup_quality": 1,
                                                    "direction_probs": {"long": .9, "short": .05, "neutral": .05}}}


def test_bracket_labels():
    up = [_dec(i, 100 + i) for i in range(10)]           # target (+3) before stop (-2)
    lab = label_decisions(up, 2, 3, horizon=5)
    assert lab[0].bracket_win is True and lab[0].direction_outcome == "long"
    down = [_dec(i, 100 - i) for i in range(10)]
    assert label_decisions(down, 2, 3, horizon=5)[0].bracket_win is False
    assert label_decisions(up, 2, 3, horizon=5)[-1].bracket_win is None  # no future yet


def test_rewrite_must_keep_shape():
    q = compile_schema("X", {}).questions
    import copy
    good = copy.deepcopy(q)
    good["direction"]["instructions"] = "reworded"
    assert same_shape(q, good)
    bad = copy.deepcopy(q)
    bad["direction"]["criteria"]["strong_long"] = "new option"
    assert not same_shape(q, bad)
    bad2 = copy.deepcopy(q)
    del bad2["toxic_flow"]
    assert not same_shape(q, bad2)
