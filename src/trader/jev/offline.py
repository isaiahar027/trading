"""Deterministic offline stand-in for Jev.

NOT Jev. It exists so that (a) backtests can run the exact same pipeline without an API
key or per-call cost, and (b) the system has a documented baseline to beat. It reads only
`jev_state` (the same payload Jev would get) and returns the same typed JevDecision, with
confidence computed using TypeSafe's published choice-confidence shape.
"""
from __future__ import annotations

import math

from .decision import JevDecision
from .schema import JevSchema

TREND = {"strong_down": -2, "down": -1, "flat": 0, "up": 1, "strong_up": 2}
FUNDING = {"shorts_crowded": -2, "shorts_paying": -1, "neutral": 0, "longs_paying": 1, "longs_crowded": 2}
IMB = {"heavy_asks": -2, "asks": -1, "balanced": 0, "bids": 1, "heavy_bids": 2}


def choice_confidence(probs: dict) -> float:
    n = len(probs)
    return max(0.0, min(1.0, (n * max(probs.values()) - 1) / (n - 1)))


def softmax(logits: dict) -> dict:
    m = max(logits.values())
    ex = {k: math.exp(v - m) for k, v in logits.items()}
    z = sum(ex.values())
    return {k: v / z for k, v in ex.items()}


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


class OfflineReflex:
    def __init__(self, temperature: float = 1.0):
        self.temperature = temperature

    def decide(self, state: dict, schema: JevSchema) -> JevDecision:
        trend = TREND[state["trend"]]
        r = state["ret%"]
        mom = _sign(r["6b"]) + _sign(r["24b"])
        perp = state.get("perp", {})
        fund = FUNDING.get(perp.get("funding", "neutral"), 0)
        imb = IMB.get(state.get("book", {}).get("imbalance", "balanced"), 0)
        vol = state["vol"]
        btc = state.get("btc", {})
        btc_trend = TREND.get(btc.get("trend", "flat"), 0)

        score = 0.9 * trend + 0.6 * mom + 0.3 * imb + 0.3 * btc_trend
        crowd_penalty = 0.8 * max(0, abs(fund) - 1)  # only extremes matter
        long_l = score - (crowd_penalty if fund > 0 else 0)
        short_l = -score - (crowd_penalty if fund < 0 else 0)
        neutral_l = 1.6 + (1.0 if vol == "extreme" else 0) + (0.8 if trend == 0 else 0)
        t = self.temperature
        dprobs = softmax({"long": long_l / t, "short": short_l / t, "neutral": neutral_l / t})
        direction = max(dprobs, key=dprobs.get)

        crash = vol == "extreme" and (r["24b"] < -8 or btc_trend == -2)
        rlog = {
            "trending": 1.2 * abs(trend) + (0.5 if mom * _sign(trend) == 2 else 0),
            "mean_reverting": 1.5 if trend == 0 else 0.3,
            "high_vol": {"low": -1, "normal": 0, "high": 1.5, "extreme": 2.5}[vol],
            "crisis": 4.0 if crash else -1.0,
        }
        rprobs = softmax(rlog)

        toxic = 0.1
        if state.get("volume") == "climactic":
            toxic += 0.3
        if vol == "extreme":
            toxic += 0.3
        if state.get("range24") in ("at_low", "at_high") and state.get("volume") in ("heavy", "climactic"):
            toxic += 0.2
        toxic = min(toxic, 0.95)

        agree = sum([abs(trend) >= 1, abs(mom) == 2 and _sign(mom) == _sign(trend),
                     _sign(imb) == _sign(trend) and imb != 0, fund * _sign(trend) < 2,
                     vol in ("low", "normal")])
        setup = min(3.0, max(0.0, agree - 1.5)) if direction != "neutral" else min(1.0, agree / 4)

        acct = state["acct"]
        dd, day = acct["dd%"], acct["day%"]
        upnl = state["pos"]["upnl_R"]
        if dd > 10 or day < -2.5 or upnl < -0.8:
            risk = {"safe": 0.05, "near_limit": 0.15, "reduce": 0.80}
        elif dd > 5 or day < -1.5:
            risk = {"safe": 0.15, "near_limit": 0.75, "reduce": 0.10}
        else:
            risk = {"safe": 0.90, "near_limit": 0.08, "reduce": 0.02}

        return JevDecision(
            regime=max(rprobs, key=rprobs.get), regime_conf=choice_confidence(rprobs), regime_probs=rprobs,
            direction=direction, direction_conf=choice_confidence(dprobs), direction_probs=dprobs,
            toxic_flow=toxic, setup_quality=float(setup), setup_conf=0.7,
            risk_state=max(risk, key=risk.get), risk_conf=choice_confidence(risk), model="offline-heuristic-v1",
            source="offline",
        )
