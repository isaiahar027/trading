"""Minimal HTTP client for TypeSafe's System One endpoint (POST /v1/systemone).

Wire format per https://docs.typesafe.ai/api.md. We call HTTP directly (instead of the
typesafe SDK) so the latency budget and failure behaviour are ours: the live loop must
never block on the reflex. Any failure -> JevDecision.abstain(), which fails closed.
"""
from __future__ import annotations

import os
import random
import time

import requests

from ..config import JevConfig
from .decision import JevDecision, MalformedAnswer, parse_answers
from .schema import JevSchema

RETRYABLE = {429, 500, 502, 503, 504, 529}


class JevError(RuntimeError):
    pass


class JevClient:
    def __init__(self, cfg: JevConfig, api_key: str | None = None, session: requests.Session | None = None):
        self.cfg = cfg
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY", "")
        if not self.api_key:
            raise JevError("TYPESAFE_API_KEY not set (get one at https://console.typesafe.ai)")
        self.http = session or requests.Session()

    def evaluate(self, state: dict, questions: dict) -> tuple[dict, str, dict, float]:
        body = {"state": state, "model": self.cfg.model, "questions": questions}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        t0 = time.monotonic()
        last_err = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                r = self.http.post(self.cfg.url, json=body, headers=headers, timeout=self.cfg.timeout_s)
            except requests.RequestException as e:
                last_err = JevError(f"transport: {e}")
            else:
                if r.status_code == 200:
                    d = r.json()
                    return d["answers"], d.get("model", self.cfg.model), d.get("usage", {}), (time.monotonic() - t0) * 1e3
                if r.status_code not in RETRYABLE:
                    raise JevError(f"HTTP {r.status_code}: {r.text[:300]}")
                last_err = JevError(f"HTTP {r.status_code}")
                ra = r.headers.get("retry-after")
                if ra:
                    try:
                        time.sleep(min(float(ra), 1.0))
                        continue
                    except ValueError:
                        pass
            if attempt < self.cfg.max_retries:
                time.sleep(min(0.1 * 2 ** attempt + random.random() * 0.05, 1.0))
        raise last_err or JevError("unknown failure")


class JevReflex:
    """The reflex: snapshot + schema -> typed decision, one call, fail-closed."""

    def __init__(self, client: JevClient):
        self.client = client

    def decide(self, state: dict, schema: JevSchema) -> JevDecision:
        try:
            answers, model, usage, ms = self.client.evaluate(state, schema.questions)
            return parse_answers(answers, model, ms, usage)
        except (JevError, MalformedAnswer, KeyError, ValueError) as e:
            return JevDecision.abstain(f"jev_error: {e}")
