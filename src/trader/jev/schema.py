"""Typed Jev decision schema, compiled per finalist.

One schema = the five questions the reflex asks on every candle, sent in ONE request
(Jev evaluates every question in parallel and in isolation against the same state).
Questions are written literally and point at snapshot fields by name, following the
TypeSafe guidance: literal conditions, criteria per option, no arithmetic.

A schema also carries a calibration map (Jev probability -> empirically observed win
rate), fitted by the nightly review. Policy sizes off the calibrated number.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

REGIMES = ("trending", "mean_reverting", "high_vol", "crisis")
DIRECTIONS = ("long", "short", "neutral")
RISK_STATES = ("safe", "near_limit", "reduce")
SETUP_LEVELS = (
    "0: no setup. Signals conflict or there is nothing to trade.",
    "1: weak setup. One supporting signal, others neutral or opposed.",
    "2: good setup. Trend, positioning and flow mostly agree on one direction and nothing in `perp` or `book` contradicts it.",
    "3: excellent setup. Trend, momentum, positioning and flow all agree, volatility is not `extreme`, and the move is not already at the end of its range in the trade direction.",
)


@dataclass
class Calibration:
    """Piecewise-constant map from Jev direction probability to observed win rate.

    edges are bin lower bounds; win_rate[i] applies to p in [edges[i], edges[i+1]).
    Until the review has enough samples, the map is the identity shrunk toward 0.5.
    """
    edges: list[float] = field(default_factory=lambda: [0.0, 0.8, 0.85, 0.9, 0.95])
    win_rate: list[float | None] = field(default_factory=lambda: [None] * 5)
    samples: list[int] = field(default_factory=lambda: [0] * 5)
    prior_shrink: float = 0.5  # before evidence: p_cal = 0.5 + shrink * (p - 0.5)

    def apply(self, p: float) -> float:
        i = max(j for j, e in enumerate(self.edges) if p >= e)
        observed = self.win_rate[i]
        prior = 0.5 + self.prior_shrink * (p - 0.5)
        if observed is None:
            return prior
        n = self.samples[i]
        w = n / (n + 30)  # blend toward the observed rate as samples accumulate
        return w * observed + (1 - w) * prior


@dataclass
class JevSchema:
    symbol: str
    version: int
    thesis: dict                      # bias, one-line thesis, invalidation (from research)
    questions: dict
    calibration: Calibration = field(default_factory=Calibration)
    notes: str = ""

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.questions, sort_keys=True).encode()).hexdigest()[:12]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "JevSchema":
        d = json.loads(text)
        d["calibration"] = Calibration(**d.get("calibration", {}))
        return cls(**d)


def build_questions(thesis: dict) -> dict:
    ctx = {
        "thesis_bias": thesis.get("bias", "neutral"),
        "thesis": thesis.get("thesis", ""),
        "invalidated_if": thesis.get("invalidation", ""),
    }
    return {
        "regime": {
            "type": "choice",
            "instructions": "Which market regime does this snapshot show for `sym`? Use `trend`, `vol`, `ret%`, `range24`, `perp` and `btc` if present.",
            "criteria": {
                "trending": "`trend` is up/strong_up or down/strong_down and returns in `ret%` point the same way; volatility is not extreme.",
                "mean_reverting": "`trend` is flat or returns in `ret%` disagree in sign; price is oscillating inside its range.",
                "high_vol": "`vol` is high or extreme, or `volume` is climactic, but there is no sign of disorderly liquidation.",
                "crisis": "Disorderly market: `vol` is extreme AND large negative returns, or `btc` trend is strong_down with extreme vol, or funding/premium are at extremes consistent with forced liquidations.",
            },
        },
        "direction": {
            "type": "choice",
            "instructions": {
                "question": "Over the next several bars, which position in `sym` is most likely to be profitable? Weigh the snapshot most; use `context` only as a tie-breaker, and pick neutral if the snapshot contradicts `context.invalidated_if`.",
                "context": ctx,
            },
            "criteria": {
                "long": "Trend, momentum and flow favor higher prices, and `perp.funding` is not longs_crowded.",
                "short": "Trend, momentum and flow favor lower prices, and `perp.funding` is not shorts_crowded.",
                "neutral": "No clear edge, signals conflict, or the move is already exhausted.",
            },
        },
        "toxic_flow": {
            "type": "noul",
            "instructions": "Is the order flow for `sym` toxic right now, meaning a new position would likely be run over by informed or forced flow (liquidation cascade, one-sided heavy book against the move, climactic volume at a range extreme)?",
            "criteria": {"true": "Flow is toxic; entering now is dangerous.", "false": "Flow looks orderly."},
        },
        "setup_quality": {
            "type": "score",
            "instructions": "How good is the trade setup in `sym` right now, in whichever direction is best?",
            "criteria": list(SETUP_LEVELS),
        },
        "risk_state": {
            "type": "choice",
            "instructions": "Given the account state in `acct` and position in `pos`, how should risk be treated right now?",
            "criteria": {
                "safe": "`acct.dd%` is below 5 and `acct.day%` is above -1.5, and any open position in `pos` is not deeply losing.",
                "near_limit": "`acct.dd%` is between 5 and 10, or `acct.day%` is between -1.5 and -2.5.",
                "reduce": "`acct.dd%` is above 10, or `acct.day%` is below -2.5, or `pos.upnl_R` is below -0.8.",
            },
        },
    }


def compile_schema(symbol: str, thesis: dict, version: int = 1) -> JevSchema:
    return JevSchema(symbol=symbol, version=version, thesis=thesis, questions=build_questions(thesis))


class SchemaStore:
    """schemas/<SYM>/v###.json, schemas/<SYM>/ACTIVE -> version, schemas/pending/ for proposals."""

    def __init__(self, root: str | Path = "schemas"):
        self.root = Path(root)

    def path(self, symbol: str, version: int) -> Path:
        return self.root / symbol / f"v{version:03d}.json"

    def save(self, schema: JevSchema, activate: bool = False) -> Path:
        p = self.path(schema.symbol, schema.version)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(schema.to_json())
        if activate:
            (p.parent / "ACTIVE").write_text(str(schema.version))
        return p

    def active(self, symbol: str) -> JevSchema:
        d = self.root / symbol
        version = int((d / "ACTIVE").read_text().strip())
        return JevSchema.from_json(self.path(symbol, version).read_text())

    def latest_version(self, symbol: str) -> int:
        d = self.root / symbol
        vs = [int(p.stem[1:]) for p in d.glob("v*.json")] if d.exists() else []
        return max(vs, default=0)

    def propose(self, schema: JevSchema, rationale: str) -> Path:
        p = self.root / "pending" / f"{schema.symbol}-v{schema.version:03d}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.loads(schema.to_json())
        payload["_rationale"] = rationale
        p.write_text(json.dumps(payload, indent=2))
        return p

    def approve(self, pending_file: str | Path) -> JevSchema:
        d = json.loads(Path(pending_file).read_text())
        d.pop("_rationale", None)
        schema = JevSchema.from_json(json.dumps(d))
        self.save(schema, activate=True)
        Path(pending_file).unlink()
        return schema
