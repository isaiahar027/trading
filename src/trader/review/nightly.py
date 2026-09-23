"""Nightly review: read every fill and every miss, measure calibration, propose the next schema.

What it does, in order:
  1. Load decisions + fills from the journal.
  2. Label every decision after the fact, in code, from later prices in the journal:
       - direction outcome: long/short/neutral by forward return vs 0.5 ATR
       - bracket outcome: would a trade in Jev's direction have hit target before stop?
  3. Score: multi-class Brier for direction, binary Brier for calibrated p_win on real
     trades, win rate by setup_quality bucket, and per-gate "miss" accounting (winners
     skipped vs losers avoided by each entry gate).
  4. Refit the calibration map (monotone) and write a PENDING schema version.
  5. Optionally ask the brain (Opus 5.5) to rewrite the worst question's wording. Its
     output must keep every question id, type and option set; anything else is rejected.
Nothing here touches risk limits or policy thresholds. Promotion is an operator act
(`trader schema approve`), or `--auto-promote-calibration` for calibration-only changes.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from ..jev.schema import JevSchema, SchemaStore
from ..journal import Journal
from .calibration import brier_binary, brier_multiclass, fit_calibration, reliability


@dataclass
class Labeled:
    rec: dict
    direction_outcome: str | None
    bracket_win: bool | None


def _day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def label_decisions(decisions: list[dict], stop_atr: float, target_atr: float, horizon: int) -> list[Labeled]:
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for d in decisions:
        by_sym[d["symbol"]].append(d)
    out = []
    for sym, ds in by_sym.items():
        ds.sort(key=lambda r: r["ts"])
        pxs = [r["numeric"]["px"] for r in ds]
        for i, r in enumerate(ds):
            px, a = r["numeric"]["px"], r["numeric"]["atr"]
            path = pxs[i + 1:i + 1 + horizon]
            if len(path) < horizon:
                out.append(Labeled(r, None, None))  # not enough future yet
                continue
            fwd = path[min(5, len(path)) - 1] / px - 1
            thr = 0.5 * a / px
            dir_out = "long" if fwd > thr else "short" if fwd < -thr else "neutral"
            jdir = r["jev"]["direction"]
            win = None
            if jdir in ("long", "short"):
                s = 1 if jdir == "long" else -1
                win = False
                for p in path:
                    if (p - px) * s <= -stop_atr * a:
                        break
                    if (p - px) * s >= target_atr * a:
                        win = True
                        break
            out.append(Labeled(r, dir_out, win))
    return out


def review(cfg: Config, journal: Journal, store: SchemaStore, day: str | None = None,
           use_brain: bool = True, auto_promote_calibration: bool = False) -> dict:
    recs = journal.read(day)
    all_recs = journal.read() if day else recs  # labels need the future beyond `day`
    decisions = [r for r in all_recs if r["kind"] == "decision" and r["jev"]["source"] != "abstain"]
    labeled = label_decisions(decisions, cfg.policy.stop_atr, cfg.policy.target_atr, cfg.policy.max_hold_bars)
    todays = [l for l in labeled if not day or _day(l.rec["ts"]) == day]

    fills = [r for r in recs if r["kind"] == "fill"]
    report: dict = {"day": day or "all", "decisions": len(todays), "fills": len(fills), "symbols": {}}
    proposals = []

    for sym in sorted({l.rec["symbol"] for l in labeled}):
        sl = [l for l in todays if l.rec["symbol"] == sym and l.direction_outcome]
        cum = [l for l in labeled if l.rec["symbol"] == sym and l.bracket_win is not None]
        s: dict = {"labeled": len(sl)}
        if sl:
            s["direction_brier"] = round(sum(brier_multiclass(l.rec["jev"]["direction_probs"], l.direction_outcome)
                                             for l in sl) / len(sl), 4)
            s["naive_brier"] = round(sum(brier_multiclass({"long": 1 / 3, "short": 1 / 3, "neutral": 1 / 3},
                                                          l.direction_outcome) for l in sl) / len(sl), 4)
        pairs = [(l.rec["jev"]["direction_probs"][l.rec["jev"]["direction"]], l.bracket_win) for l in cum]
        try:
            schema = store.active(sym)
        except FileNotFoundError:
            continue
        s["reliability"] = reliability(pairs, schema.calibration.edges)
        by_setup = defaultdict(list)
        for l in cum:
            by_setup[min(3, int(l.rec["jev"]["setup_quality"]))].append(l.bracket_win)
        s["win_rate_by_setup"] = {k: {"n": len(v), "win": round(sum(v) / len(v), 3)} for k, v in sorted(by_setup.items())}
        gate_misses: dict = defaultdict(lambda: {"skipped_winners": 0, "avoided_losers": 0})
        for l in cum:
            if l.rec["action"] == "no_trade":
                for reason in l.rec["reasons"]:
                    key = reason.split(" ")[0]
                    gate_misses[key]["skipped_winners" if l.bracket_win else "avoided_losers"] += 1
        s["gate_misses"] = dict(gate_misses)
        traded = [l for l in cum if l.rec["action"].startswith("enter_") and not l.rec.get("blocked")]
        if traded:
            s["p_win_brier"] = round(sum(brier_binary(l.rec["p_win"], l.bracket_win) for l in traded) / len(traded), 4)
        new_cal = fit_calibration(pairs, schema.calibration)
        s["calibration"] = {"old": schema.calibration.win_rate, "new": new_cal.win_rate, "samples": new_cal.samples}
        nxt = JevSchema(sym, store.latest_version(sym) + 1, schema.thesis, schema.questions, new_cal,
                        notes=f"calibration refit from {len(pairs)} labeled decisions")
        rewrite = propose_rewrite(cfg, nxt, s) if use_brain and os.environ.get("ANTHROPIC_API_KEY") and sl else None
        if rewrite is not None:
            proposals.append(str(store.propose(rewrite, rewrite.notes)))  # wording changes always need approval
        elif new_cal.win_rate != schema.calibration.win_rate:
            if auto_promote_calibration:
                store.save(nxt, activate=True)
                s["promoted"] = nxt.version
            else:
                proposals.append(str(store.propose(nxt, nxt.notes)))
        report["symbols"][sym] = s

    report["proposals"] = proposals
    out_dir = Path(cfg.run.state_dir) / "reviews"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{report['day']}.json").write_text(json.dumps(report, indent=2, default=str))
    return report


REWRITE_SYSTEM = (
    "You maintain the question wording for a fast classifier (Jev, a System One model) that judges "
    "crypto market snapshots. Jev reads literally, is weak at arithmetic and date math, and does best "
    "with exact conditions and a clear criterion per option. Given calibration statistics, rewrite the "
    "instructions and criteria of the questions to be more literal and better aligned with outcomes. "
    "You must keep every question id, its type, and its exact option names; score questions must keep "
    "exactly four levels ordered 0..3. Do not introduce numbers the snapshot does not contain."
)


def propose_rewrite(cfg: Config, schema: JevSchema, stats: dict) -> JevSchema | None:
    try:
        import anthropic
    except ImportError:
        return None
    client = anthropic.Anthropic()
    fmt = {"type": "object", "properties": {"questions_json": {"type": "string"}, "rationale": {"type": "string"}},
           "required": ["questions_json", "rationale"], "additionalProperties": False}
    try:
        resp = client.messages.create(
            model=cfg.brain.model, max_tokens=16000, system=REWRITE_SYSTEM,
            output_config={"effort": cfg.brain.effort, "format": {"type": "json_schema", "schema": fmt}},
            messages=[{"role": "user", "content": json.dumps({"questions": schema.questions, "stats": stats})}],
        )
        if resp.stop_reason == "refusal":
            return None
        d = json.loads(next(b.text for b in resp.content if b.type == "text"))
        new_q = json.loads(d["questions_json"])
    except Exception:  # proposal is optional; the review must still complete
        return None
    if not same_shape(schema.questions, new_q):
        return None
    return JevSchema(schema.symbol, schema.version, schema.thesis, new_q, schema.calibration,
                     notes=f"{schema.notes}; brain rewrite: {d['rationale'][:2000]}")


def same_shape(old: dict, new: dict) -> bool:
    if set(old) != set(new):
        return False
    for k, q in old.items():
        n = new[k]
        if n.get("type") != q["type"] or "instructions" not in n:
            return False
        if q["type"] == "choice" and set(n.get("criteria", {})) != set(q["criteria"]):
            return False
        if q["type"] == "score" and len(n.get("criteria", [])) != len(q["criteria"]):
            return False
    return True
