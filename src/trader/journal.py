"""Append-only JSONL journal: every snapshot, decision, policy result, risk verdict, fill.

The nightly review reads only this. If it is not in the journal, it did not happen.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path


def _default(o):
    if is_dataclass(o):
        return asdict(o)
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


class Journal:
    def __init__(self, root: str | Path):
        self.root = Path(root) / "journal"
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, ts_ms: int) -> Path:
        return self.root / (datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y-%m-%d") + ".jsonl")

    def write(self, kind: str, ts_ms: int, **payload) -> None:
        rec = {"kind": kind, "ts": ts_ms, "wall": time.time(), **payload}
        with self.path_for(ts_ms).open("a") as f:
            f.write(json.dumps(rec, default=_default, separators=(",", ":")) + "\n")

    def read(self, day: str | None = None) -> list[dict]:
        files = [self.root / f"{day}.jsonl"] if day else sorted(self.root.glob("*.jsonl"))
        out = []
        for p in files:
            if p.exists():
                out += [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
        return out
