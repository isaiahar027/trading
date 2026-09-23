"""Typed decision returned by the reflex. Contains judgments only: no sizes, no prices."""
from __future__ import annotations

from dataclasses import dataclass, field

from .schema import DIRECTIONS, REGIMES, RISK_STATES


class MalformedAnswer(ValueError):
    pass


@dataclass(frozen=True)
class JevDecision:
    regime: str
    regime_conf: float
    regime_probs: dict
    direction: str
    direction_conf: float
    direction_probs: dict
    toxic_flow: float
    setup_quality: float
    setup_conf: float
    risk_state: str
    risk_conf: float
    model: str
    source: str = "jev"          # "jev" | "offline" | "abstain"
    latency_ms: float = 0.0
    usage: dict = field(default_factory=dict)

    @property
    def p_direction(self) -> float:
        return self.direction_probs.get(self.direction, 0.0)

    @property
    def confidence(self) -> float:
        """The confidence used for escalation: the weakest of the gating answers."""
        return min(self.direction_conf, self.regime_conf, self.risk_conf)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def abstain(cls, reason: str) -> "JevDecision":
        """Used when the reflex is unavailable/late. Fails closed: neutral, reduce."""
        return cls("crisis", 0.0, {}, "neutral", 0.0, {}, 1.0, 0.0, 0.0, "reduce", 0.0,
                   model=reason, source="abstain")


def _check_probs(name: str, probs: dict, options) -> None:
    if set(probs) != set(options):
        raise MalformedAnswer(f"{name}: options {sorted(probs)} != {sorted(options)}")
    total = sum(probs.values())
    if not 0.98 <= total <= 1.02:
        raise MalformedAnswer(f"{name}: probabilities sum to {total}")


def parse_answers(answers: dict, model: str, latency_ms: float = 0.0, usage: dict | None = None) -> JevDecision:
    try:
        reg, dir_, tox, setup, risk = (answers[k] for k in
                                       ("regime", "direction", "toxic_flow", "setup_quality", "risk_state"))
    except KeyError as e:
        raise MalformedAnswer(f"missing answer {e}") from None
    for name, a, t in (("regime", reg, "choice"), ("direction", dir_, "choice"), ("toxic_flow", tox, "noul"),
                       ("setup_quality", setup, "score"), ("risk_state", risk, "choice")):
        if a.get("type") != t:
            raise MalformedAnswer(f"{name}: expected {t}, got {a.get('type')}")
    _check_probs("regime", reg["probabilities"], REGIMES)
    _check_probs("direction", dir_["probabilities"], DIRECTIONS)
    _check_probs("risk_state", risk["probabilities"], RISK_STATES)
    noul = float(tox["noul"])
    score = float(setup["score"])
    if not 0 <= noul <= 1 or not 0 <= score <= 3:
        raise MalformedAnswer(f"out of range: noul={noul} score={score}")
    return JevDecision(
        regime=reg["choice"], regime_conf=float(reg["confidence"]), regime_probs=reg["probabilities"],
        direction=dir_["choice"], direction_conf=float(dir_["confidence"]), direction_probs=dir_["probabilities"],
        toxic_flow=noul, setup_quality=score, setup_conf=float(setup["confidence"]),
        risk_state=risk["choice"], risk_conf=float(risk["confidence"]),
        model=model, source="jev", latency_ms=latency_ms, usage=usage or {},
    )
