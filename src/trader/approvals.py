"""AgenKit-style phase gates. Live trading requires every gate approved by the operator.

The six phases mirror the AgenKit /agenkit workflow (spec -> architecture -> plan ->
test-first build -> review -> ship). Approval is a human act recorded in
state/approvals.json; no code path in the agent writes approvals on its own.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

GATES = ("1-spec", "2-architecture", "3-plan", "4-build", "5-review", "6-ship")


class Approvals:
    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir) / "approvals.json"

    def load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def approve(self, gate: str, who: str, note: str = "") -> None:
        if gate not in GATES:
            raise ValueError(f"unknown gate {gate}; one of {GATES}")
        d = self.load()
        idx = GATES.index(gate)
        missing = [g for g in GATES[:idx] if g not in d]
        if missing:
            raise ValueError(f"approve earlier gates first: {missing}")
        d[gate] = {"by": who, "ts": time.time(), "note": note}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d, indent=2))

    def missing(self) -> list[str]:
        d = self.load()
        return [g for g in GATES if g not in d]

    def all_approved(self) -> bool:
        return not self.missing()
