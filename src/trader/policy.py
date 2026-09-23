"""Policy: turns a Jev judgment + numeric snapshot into an order intent.

Pure function of its inputs. Every gate and every size is computed here from config.
The decision object carries no sizes or prices, so the model cannot size a trade.

Entry gates (all must hold):
  setup_quality >= 2, direction in {long, short}, direction confidence > 0.80,
  risk_state == safe, regime != crisis, toxic_flow below threshold.
Sizing: fractional Kelly on the CALIBRATED win probability for a bracket trade
  (stop = k_s*ATR, target = k_t*ATR, net of round-trip costs), capped at quarter Kelly.
Exits are code-owned: bracket stop/target, time stop, risk_state reduce, crisis, flip.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import KELLY_HARD_CAP, CostConfig, PolicyConfig
from .jev.decision import JevDecision
from .jev.schema import Calibration
from .state_engine import Snapshot
from .market import OrderIntent, Portfolio


@dataclass(frozen=True)
class PolicyResult:
    intent: OrderIntent | None
    action: str                 # enter_long | enter_short | exit | reduce | hold | no_trade
    reasons: tuple[str, ...]
    p_win: float = 0.0
    kelly: float = 0.0
    escalate: bool = False


def kelly_fraction(p: float, b: float) -> float:
    """Full-Kelly fraction of bankroll to RISK on a bet paying b:1 with win prob p."""
    if b <= 0:
        return 0.0
    return max(0.0, p - (1 - p) / b)


def entry_gates(d: JevDecision, cfg: PolicyConfig) -> list[str]:
    failed = []
    if d.source == "abstain":
        failed.append(f"reflex abstained ({d.model})")
    if d.setup_quality < cfg.min_setup_quality:
        failed.append(f"setup_quality {d.setup_quality:.2f} < {cfg.min_setup_quality}")
    if d.direction not in ("long", "short"):
        failed.append("direction neutral")
    if d.direction_conf <= cfg.min_direction_confidence:
        failed.append(f"direction confidence {d.direction_conf:.2f} <= {cfg.min_direction_confidence}")
    if d.risk_state != "safe":
        failed.append(f"risk_state {d.risk_state}")
    if d.regime == "crisis":
        failed.append("regime crisis")
    if d.toxic_flow >= cfg.max_toxic_flow:
        failed.append(f"toxic_flow {d.toxic_flow:.2f} >= {cfg.max_toxic_flow}")
    return failed


def should_escalate(d: JevDecision, cfg: PolicyConfig) -> bool:
    return d.source != "offline" and (d.regime == "crisis" or d.direction_conf < cfg.escalate_below_confidence)


def decide(
    snap: Snapshot,
    d: JevDecision,
    portfolio: Portfolio,
    cfg: PolicyConfig,
    costs: CostConfig,
    calibration: Calibration,
    decision_id: str = "",
) -> PolicyResult:
    n = snap.numeric
    px, a = n["px"], n["atr"]
    sym = snap.symbol
    pos = portfolio.positions.get(sym)
    escalate = should_escalate(d, cfg)

    # ---- manage an open position first (exits never need model confidence)
    if pos and pos.qty:
        side = pos.side
        reasons = []
        if (side > 0 and px <= pos.stop_px) or (side < 0 and px >= pos.stop_px):
            reasons.append("stop hit")
        elif (side > 0 and px >= pos.target_px) or (side < 0 and px <= pos.target_px):
            reasons.append("target hit")
        elif pos.bars_held >= cfg.max_hold_bars:
            reasons.append("time stop")
        elif d.source == "abstain":
            reasons.append("reflex unavailable while in position")
        elif d.regime == "crisis":
            reasons.append("crisis regime")
        elif d.direction in ("long", "short") and (1 if d.direction == "long" else -1) != side \
                and d.direction_conf > cfg.min_direction_confidence:
            reasons.append("confident direction flip")
        if reasons:
            return PolicyResult(OrderIntent(sym, -pos.qty, px, True, "exit: " + ", ".join(reasons),
                                            decision_id=decision_id), "exit", tuple(reasons), escalate=escalate)
        if d.risk_state == "reduce":
            half = -pos.qty / 2
            return PolicyResult(OrderIntent(sym, half, px, True, "reduce: risk_state reduce", decision_id=decision_id),
                                "reduce", ("risk_state reduce",), escalate=escalate)
        return PolicyResult(None, "hold", ("position open, no exit condition",), escalate=escalate)

    # ---- flat: entry gates
    failed = entry_gates(d, cfg)
    if failed:
        return PolicyResult(None, "no_trade", tuple(failed), escalate=escalate)

    side = 1 if d.direction == "long" else -1
    stop_dist, target_dist = cfg.stop_atr * a, cfg.target_atr * a
    cost_px = costs.round_trip * px
    b = (target_dist - cost_px) / (stop_dist + cost_px)   # net payoff ratio after costs
    p_win = calibration.apply(d.p_direction)
    f_full = kelly_fraction(p_win, b)
    f = f_full * min(cfg.kelly_fraction, KELLY_HARD_CAP)
    if f <= 0:
        return PolicyResult(None, "no_trade", (f"no edge after costs: p_win={p_win:.3f} b={b:.2f}",),
                            p_win=p_win, escalate=escalate)
    risk_usd = f * portfolio.equity
    qty = side * risk_usd / (stop_dist + cost_px)
    intent = OrderIntent(
        sym, qty, px, False, f"enter {d.direction}: p_win={p_win:.3f} b={b:.2f} kelly={f:.4f}",
        stop_px=px - side * stop_dist, target_px=px + side * target_dist, entry_prob=p_win, decision_id=decision_id,
    )
    return PolicyResult(intent, f"enter_{d.direction}", (), p_win=p_win, kelly=f, escalate=escalate)
