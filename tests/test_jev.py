import json

import pytest
import requests

from trader.config import JevConfig
from trader.jev.client import JevClient, JevError, JevReflex
from trader.jev.decision import MalformedAnswer, parse_answers
from trader.jev.offline import OfflineReflex, choice_confidence
from trader.jev.schema import JevSchema, SchemaStore, compile_schema

# Shapes copied from https://docs.typesafe.ai/api.md response examples
ANSWERS = {
    "regime": {"type": "choice", "choice": "trending", "confidence": 0.7,
               "probabilities": {"trending": 0.8, "mean_reverting": 0.1, "high_vol": 0.1, "crisis": 0.0}},
    "direction": {"type": "choice", "choice": "long", "confidence": 0.88,
                  "probabilities": {"long": 0.92, "short": 0.03, "neutral": 0.05}},
    "toxic_flow": {"type": "noul", "noul": 0.12},
    "setup_quality": {"type": "score", "score": 2.3, "confidence": 0.6, "legend": {"0": "a", "1": "b", "2": "c", "3": "d"},
                      "probabilities": {"0": 0.0, "1": 0.1, "2": 0.5, "3": 0.4}},
    "risk_state": {"type": "choice", "choice": "safe", "confidence": 0.9,
                   "probabilities": {"safe": 0.95, "near_limit": 0.05, "reduce": 0.0}},
}


class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok():
    return FakeResp(200, {"model": "jev-1.13.0", "answers": ANSWERS, "usage": {"input_tokens": 300, "output_tokens": 30}})


def test_parse_answers():
    d = parse_answers(ANSWERS, "jev-1.13.0")
    assert d.direction == "long" and d.p_direction == 0.92 and d.setup_quality == 2.3 and d.toxic_flow == 0.12
    assert d.confidence == 0.7


@pytest.mark.parametrize("mutate", [
    lambda a: a.pop("direction"),
    lambda a: a["direction"].update(type="score"),
    lambda a: a["direction"]["probabilities"].pop("neutral"),
    lambda a: a["regime"]["probabilities"].update(trending=0.5),
    lambda a: a["toxic_flow"].update(noul=1.5),
])
def test_malformed_answers_rejected(mutate):
    a = json.loads(json.dumps(ANSWERS))
    mutate(a)
    with pytest.raises(MalformedAnswer):
        parse_answers(a, "jev")


def test_request_wire_format():
    sess = FakeSession([ok()])
    schema = compile_schema("SOL", {"bias": "long", "thesis": "t", "invalidation": "i"})
    d = JevReflex(JevClient(JevConfig(), api_key="k", session=sess)).decide({"sym": "SOL"}, schema)
    call = sess.calls[0]
    assert call["url"] == "https://api.typesafe.ai/v1/systemone"
    assert call["headers"]["Authorization"] == "Bearer k"
    assert call["json"]["model"] == "jev-1.13.0" and call["json"]["state"] == {"sym": "SOL"}
    assert set(call["json"]["questions"]) == {"regime", "direction", "toxic_flow", "setup_quality", "risk_state"}
    assert call["timeout"] == 2.0 and d.source == "jev"


def test_retries_on_429_then_succeeds():
    sess = FakeSession([FakeResp(429), FakeResp(529), ok()])
    d = JevReflex(JevClient(JevConfig(max_retries=2), api_key="k", session=sess)).decide({}, compile_schema("X", {}))
    assert d.source == "jev" and len(sess.calls) == 3


def test_fails_closed():
    for resp in ([FakeResp(401)], [requests.ConnectionError("down")] * 3, [FakeResp(200, {"answers": {}})]):
        d = JevReflex(JevClient(JevConfig(), api_key="k", session=FakeSession(resp))).decide({}, compile_schema("X", {}))
        assert d.source == "abstain" and d.direction == "neutral" and d.risk_state == "reduce"


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(JevError):
        JevClient(JevConfig())


def test_schema_valid_for_api():
    q = compile_schema("SOL", {"bias": "short", "thesis": "x", "invalidation": "y"}).questions
    for k, v in q.items():
        assert v["type"] in ("choice", "score", "noul") and v["instructions"]
        if v["type"] == "score":
            assert 2 <= len(v["criteria"]) <= 10
        if v["type"] == "choice":
            assert len(v["criteria"]) <= 255


def test_schema_store_roundtrip(tmp_path):
    st = SchemaStore(tmp_path)
    s = compile_schema("SOL", {"bias": "long"})
    st.save(s, activate=True)
    assert st.active("SOL").digest == s.digest
    s2 = JevSchema("SOL", 2, s.thesis, s.questions)
    p = st.propose(s2, "why")
    assert st.approve(p).version == 2 and st.active("SOL").version == 2 and not p.exists()


def test_offline_reflex_types(portfolio):
    from trader.state_engine import StateEngine
    from .conftest import make_candles
    c = make_candles(300, drift=0.004, vol=0.002)
    snap = StateEngine().build("SOL", c[-1].t_close + 1, c, portfolio, 1e4, 1e4)
    d = OfflineReflex().decide(snap.jev_state, compile_schema("SOL", {}))
    assert d.source == "offline" and abs(sum(d.direction_probs.values()) - 1) < 1e-9
    assert choice_confidence({"a": 1.0, "b": 0.0}) == 1.0 and choice_confidence({"a": 0.5, "b": 0.5}) == 0.0
