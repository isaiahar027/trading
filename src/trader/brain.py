"""The BRAIN: Claude Opus 5.5 for slow, deep re-reads. Never on the hot path.

Escalations fire when Jev's confidence drops below the escalation threshold or the regime
flips to crisis. While an escalation is pending, the symbol takes no new risk (code rule).
The brain's answer is constrained by a JSON schema to risk-REDUCING actions only:
it can pause a symbol or flatten it; it cannot open, enlarge, or change any limit.
Any failure (no key, network, refusal, bad JSON) resolves to "pause" - fail closed.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from .config import BrainConfig

ESCALATION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["no_change", "pause", "flatten"]},
        "pause_hours": {"type": "integer", "enum": [0, 1, 4, 12, 24]},
        "reasoning": {"type": "string"},
        "what_would_change_my_mind": {"type": "string"},
    },
    "required": ["action", "pause_hours", "reasoning", "what_would_change_my_mind"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are the slow, careful reviewer for an autonomous crypto trading system. A fast model "
    "flagged low confidence or a crisis regime. You see the deterministic market snapshot, the fast "
    "model's typed answers, the current position and the research thesis. Decide whether the symbol "
    "should keep trading under its normal rules (no_change), stop opening new positions for a while "
    "(pause), or close its position now (flatten). You cannot open or enlarge positions or change "
    "limits. Prefer survival over profit: when evidence is ambiguous, pause."
)


@dataclass(frozen=True)
class Verdict:
    action: str
    pause_hours: int
    reasoning: str
    source: str

    @classmethod
    def fail_closed(cls, why: str) -> "Verdict":
        return cls("pause", 4, f"fail-closed: {why}", "fallback")


class Brain:
    def __init__(self, cfg: BrainConfig):
        self.cfg = cfg
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="brain")
        self._client = None
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                import anthropic
                self._client = anthropic.Anthropic()
            except ImportError:
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def submit(self, packet: dict) -> Future:
        return self.pool.submit(self.review, packet)

    def review(self, packet: dict) -> Verdict:
        if self._client is None:
            return Verdict.fail_closed("brain unavailable (no ANTHROPIC_API_KEY or anthropic not installed)")
        try:
            import anthropic
            resp = self._client.messages.create(
                model=self.cfg.model,
                max_tokens=16000,
                system=SYSTEM,
                output_config={"effort": self.cfg.effort,
                               "format": {"type": "json_schema", "schema": ESCALATION_SCHEMA}},
                messages=[{"role": "user", "content": json.dumps(packet, default=str)}],
            )
            if resp.stop_reason == "refusal":
                return Verdict.fail_closed("model refused")
            text = next(b.text for b in resp.content if b.type == "text")
            d = json.loads(text)
            return Verdict(d["action"], int(d["pause_hours"]), d["reasoning"], self.cfg.model)
        except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.APIStatusError) as e:
            return Verdict.fail_closed(f"api error: {type(e).__name__}")
        except (StopIteration, json.JSONDecodeError, KeyError, ValueError) as e:
            return Verdict.fail_closed(f"bad response: {e}")


class EscalationDesk:
    """Tracks pending escalations and pauses per symbol. Pure bookkeeping + the brain."""

    def __init__(self, brain: Brain, cooldown_s: float):
        self.brain = brain
        self.cooldown_s = cooldown_s
        self.pending: dict[str, Future] = {}
        self.last: dict[str, float] = {}
        self.paused_until: dict[str, float] = {}

    def maybe_escalate(self, symbol: str, packet: dict, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if symbol in self.pending or now - self.last.get(symbol, float("-inf")) < self.cooldown_s:
            return False
        self.last[symbol] = now
        self.pending[symbol] = self.brain.submit(packet)
        return True

    def collect(self, now: float | None = None) -> dict[str, Verdict]:
        now = time.time() if now is None else now
        done = {}
        for sym, fut in list(self.pending.items()):
            if fut.done():
                v = fut.result()
                del self.pending[sym]
                if v.action in ("pause", "flatten"):
                    self.paused_until[sym] = now + max(v.pause_hours, 1) * 3600
                done[sym] = v
        return done

    def blocks_entry(self, symbol: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return symbol in self.pending or self.paused_until.get(symbol, 0) > now
