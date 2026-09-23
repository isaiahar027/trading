"""Calibration metrics. Pure functions, no I/O."""
from __future__ import annotations

from ..jev.schema import Calibration


def brier_multiclass(probs: dict, outcome: str) -> float:
    return sum((p - (1.0 if k == outcome else 0.0)) ** 2 for k, p in probs.items())


def brier_binary(p: float, y: bool) -> float:
    return (p - (1.0 if y else 0.0)) ** 2


def reliability(pairs: list[tuple[float, bool]], edges: list[float]) -> list[dict]:
    """Bin (predicted p, outcome) pairs; report mean predicted vs observed per bin."""
    rows = []
    for i, lo in enumerate(edges):
        hi = edges[i + 1] if i + 1 < len(edges) else 1.0000001
        b = [(p, y) for p, y in pairs if lo <= p < hi]
        rows.append({"bin": f"[{lo:.2f},{min(hi, 1):.2f})", "n": len(b),
                     "mean_p": sum(p for p, _ in b) / len(b) if b else None,
                     "observed": sum(y for _, y in b) / len(b) if b else None})
    return rows


def fit_calibration(pairs: list[tuple[float, bool]], base: Calibration, min_samples: int = 20) -> Calibration:
    """Refit the piecewise map from (raw Jev p, bracket won?) pairs, enforcing monotonicity."""
    win, n = [], []
    for row in reliability(pairs, base.edges):
        n.append(row["n"])
        win.append(row["observed"] if row["n"] >= min_samples else None)
    # pool-adjacent-violators on the bins that have data, so higher p never maps lower
    idx = [i for i, w in enumerate(win) if w is not None]
    blocks = [[win[i], n[i], [i]] for i in idx]
    merged = True
    while merged:
        merged = False
        for j in range(len(blocks) - 1):
            if blocks[j][0] > blocks[j + 1][0]:
                a, b = blocks[j], blocks[j + 1]
                tot = a[1] + b[1]
                blocks[j] = [(a[0] * a[1] + b[0] * b[1]) / tot, tot, a[2] + b[2]]
                del blocks[j + 1]
                merged = True
                break
    for val, _, members in blocks:
        for i in members:
            win[i] = round(val, 4)
    return Calibration(edges=list(base.edges), win_rate=win, samples=n, prior_shrink=base.prior_shrink)
