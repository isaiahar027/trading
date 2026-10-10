from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from amongus_desk.clock import ManualClock
from amongus_desk.config import LlmConfig
from amongus_desk.domain import BreakoutSignal, Side, TradePlan, Vote
from amongus_desk.llm.base import ReviewContext
from amongus_desk.llm.reviewer import SERVER_FALLBACK_BETA, SYSTEM_PROMPT, VERDICT_SCHEMA, ClaudeReviewer
from factories import T0

SIGNAL = BreakoutSignal(
    symbol="EURUSD",
    side=Side.BUY,
    bar_start=T0,
    detected_at=T0 + timedelta(minutes=5),
    close=1.1005,
    channel_high=1.1000,
    channel_low=1.0980,
    atr=0.0003,
    atr_slow=0.0005,
    range_atr=2.1,
    extension_atr=0.4,
    squeeze_percentile=0.2,
    strength=0.7,
    reasons=["close above channel"],
)
PLAN = TradePlan(
    symbol="EURUSD",
    side=Side.BUY,
    entry_ref=1.1005,
    stop_price=1.09975,
    target_price=1.1020,
    stop_distance=0.00075,
    atr=0.0005,
    target_r=2.0,
    time_stop_bars=24,
    spread_price=0.0001,
)
CONTEXT = ReviewContext(
    symbol="EURUSD",
    timeframe_minutes=5,
    session="in session",
    recent_bars=[{"t": T0.isoformat(), "o": 1.1, "h": 1.1006, "l": 1.0999, "c": 1.1005}],
    spread_price=0.0001,
    open_positions=0,
    trades_today=0,
    daily_pnl_pct=0.0,
)


def response(text: str | None = None, *, stop_reason: str = "end_turn", verdict: dict[str, Any] | None = None) -> Any:
    if text is None and verdict is not None:
        text = json.dumps(verdict)
    content = [SimpleNamespace(type="thinking", thinking="")]
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=900, output_tokens=120),
        model="claude-opus-5-5",
        _request_id="req_123",
    )


APPROVE = {"verdict": "APPROVE", "confidence": 0.8, "concerns": [], "summary": "Clean squeeze breakout."}
VETO = {"verdict": "VETO", "confidence": 0.7, "concerns": ["spike bar"], "summary": "Looks like a stop run."}


class FakeMessages:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeClient:
    def __init__(self, result: Any) -> None:
        self.messages = FakeMessages(result)
        self.beta = SimpleNamespace(messages=self.messages)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def reviewer(result: Any, **cfg: Any) -> tuple[ClaudeReviewer, FakeClient, ManualClock]:
    client = FakeClient(result)
    clock = ManualClock(T0)
    return ClaudeReviewer(LlmConfig(enabled=True, **cfg), clock, client=client), client, clock  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_approve_maps_to_proceed_and_request_shape() -> None:
    rev, client, _ = reviewer(response(verdict=APPROVE))
    out = await rev.review(SIGNAL, PLAN, CONTEXT)
    assert out.vote is Vote.PROCEED and out.verdict is not None and out.verdict.verdict == "APPROVE"
    assert out.request_id == "req_123" and out.input_tokens == 900 and out.latency_ms is not None
    (call,) = client.messages.calls
    assert call["model"] == "claude-opus-5-5"
    assert call["system"] == SYSTEM_PROMPT
    assert call["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": VERDICT_SCHEMA}}
    assert call["betas"] == [SERVER_FALLBACK_BETA] and call["fallbacks"] == "default"
    assert "temperature" not in call and "top_p" not in call
    payload = json.loads(call["messages"][0]["content"])
    assert payload["task"] == "review_breakout_trade" and payload["plan"]["stop_price"] == 1.09975
    assert call["messages"][0]["role"] == "user"


def test_schema_is_strict_compatible() -> None:
    assert VERDICT_SCHEMA["additionalProperties"] is False
    assert set(VERDICT_SCHEMA["required"]) == set(VERDICT_SCHEMA["properties"])
    dumped = json.dumps(VERDICT_SCHEMA)
    for forbidden in ("minimum", "maximum", "maxLength", "minLength", "maxItems"):
        assert forbidden not in dumped
    # nothing in the schema lets the model change the trade
    assert set(VERDICT_SCHEMA["properties"]) == {"verdict", "confidence", "concerns", "summary"}


@pytest.mark.anyio
async def test_veto_maps_to_eject() -> None:
    rev, _, _ = reviewer(response(verdict=VETO))
    out = await rev.review(SIGNAL, PLAN, CONTEXT)
    assert out.vote is Vote.EJECT and out.error is None and out.verdict is not None
    assert rev.usage_today()["vetoes"] == 1


@pytest.mark.anyio
async def test_server_fallback_off_drops_beta_fields() -> None:
    rev, client, _ = reviewer(
        response(verdict=APPROVE), server_fallback=False, model="claude-haiku-5-5", effort="medium"
    )
    await rev.review(SIGNAL, PLAN, CONTEXT)
    (call,) = client.messages.calls
    assert "betas" not in call and "fallbacks" not in call
    assert call["model"] == "claude-haiku-5-5" and call["output_config"]["effort"] == "medium"


@pytest.mark.parametrize(
    ("resp", "fragment"),
    [
        (response(stop_reason="refusal"), "refusal"),
        (response(verdict=APPROVE, stop_reason="max_tokens"), "max_tokens"),
        (response("not json at all"), "not JSON"),
        (response("[1, 2]"), "not a JSON object"),
        (response(verdict={"verdict": "MAYBE", "confidence": 0.5, "concerns": [], "summary": "?"}), "invalid fields"),
        (response(verdict={"verdict": "APPROVE"}), "invalid fields"),
        (response(None), "no text block"),
    ],
)
@pytest.mark.anyio
async def test_bad_answers_fail_closed(resp: Any, fragment: str) -> None:
    rev, _, _ = reviewer(resp)
    out = await rev.review(SIGNAL, PLAN, CONTEXT)
    assert out.vote is Vote.EJECT and out.error is not None and fragment in out.error
    assert rev.usage_today()["failures"] == 1


def _status_error(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(code, request=request), body=None)


@pytest.mark.parametrize(
    ("exc", "fragment"),
    [
        (_status_error(anthropic.AuthenticationError, 401), "API key rejected"),
        (_status_error(anthropic.PermissionDeniedError, 403), "permission denied"),
        (_status_error(anthropic.NotFoundError, 404), "not found"),
        (_status_error(anthropic.BadRequestError, 400), "rejected the request"),
        (_status_error(anthropic.RateLimitError, 429), "rate limit"),
        (_status_error(anthropic.InternalServerError, 529), "HTTP 529"),
        (anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com")), "timed out"),
        (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")), "network"),
    ],
)
@pytest.mark.anyio
async def test_sdk_errors_follow_policy(exc: BaseException, fragment: str) -> None:
    rev, _, _ = reviewer(exc)
    out = await rev.review(SIGNAL, PLAN, CONTEXT)
    assert out.vote is Vote.EJECT and fragment in (out.error or "")
    rev2, _, _ = reviewer(exc, failure_policy="abstain")
    assert (await rev2.review(SIGNAL, PLAN, CONTEXT)).vote is Vote.HOLD


@pytest.mark.anyio
async def test_daily_cap_and_reset() -> None:
    rev, client, clock = reviewer(response(verdict=APPROVE), max_calls_per_day=2)
    for _ in range(2):
        assert (await rev.review(SIGNAL, PLAN, CONTEXT)).vote is Vote.PROCEED
    capped = await rev.review(SIGNAL, PLAN, CONTEXT)
    assert capped.vote is Vote.EJECT and "cap reached" in (capped.error or "")
    assert len(client.messages.calls) == 2
    clock.advance(timedelta(days=1).total_seconds())
    assert (await rev.review(SIGNAL, PLAN, CONTEXT)).vote is Vote.PROCEED
    assert rev.usage_today()["calls"] == 1 and len(client.messages.calls) == 3


@pytest.mark.anyio
async def test_zero_cap_never_calls() -> None:
    rev, client, _ = reviewer(response(verdict=APPROVE), max_calls_per_day=0, failure_policy="abstain")
    assert (await rev.review(SIGNAL, PLAN, CONTEXT)).vote is Vote.HOLD
    assert client.messages.calls == []


@pytest.mark.anyio
async def test_truncation_and_clamping() -> None:
    long = {
        "verdict": "VETO",
        "confidence": 3.5,
        "concerns": [f"c{i}" * 100 for i in range(9)],
        "summary": "s" * 2000,
    }
    rev, _, _ = reviewer(response(verdict=long))
    out = await rev.review(SIGNAL, PLAN, CONTEXT)
    assert out.verdict is not None
    assert out.verdict.confidence == 1.0
    assert len(out.verdict.concerns) == 5 and all(len(c) <= 200 for c in out.verdict.concerns)
    assert len(out.verdict.summary) == 600


@pytest.mark.anyio
async def test_usage_accounting_and_close() -> None:
    rev, client, _ = reviewer(response(verdict=APPROVE))
    await rev.review(SIGNAL, PLAN, CONTEXT)
    await rev.review(SIGNAL, PLAN, CONTEXT)
    usage = rev.usage_today()
    assert usage["calls"] == 2 and usage["input_tokens"] == 1800 and usage["output_tokens"] == 240
    assert usage["enabled"] is True and usage["model"] == "claude-opus-5-5"
    await rev.aclose()
    assert client.closed


@pytest.mark.parametrize(
    "exc",
    [
        TypeError("Could not resolve authentication method. Expected either api_key or auth_token to be set."),
        json.JSONDecodeError("Expecting value", "", 0),
        RuntimeError("unexpected"),
    ],
)
@pytest.mark.anyio
async def test_any_other_error_still_follows_the_failure_policy(exc: BaseException) -> None:
    rev, _, _ = reviewer(exc, failure_policy="abstain")
    out = await rev.review(SIGNAL, PLAN, CONTEXT)
    assert out.vote is Vote.HOLD and type(exc).__name__ in (out.error or "")
    usage = rev.usage_today()
    assert usage["failures"] == 1 and usage["last_error"] == out.error


@pytest.mark.anyio
async def test_missing_content_is_a_failure_not_a_crash() -> None:
    resp = response(verdict=APPROVE)
    resp.content = None
    rev, _, _ = reviewer(resp)
    out = await rev.review(SIGNAL, PLAN, CONTEXT)
    assert out.vote is Vote.EJECT and "no text block" in (out.error or "")


class SlowMessages(FakeMessages):
    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        await asyncio.sleep(10)  # e.g. the SDK retrying a stalled request
        return self.result


@pytest.mark.anyio
async def test_total_review_time_is_bounded_including_retries() -> None:
    rev, client, _ = reviewer(response(verdict=APPROVE), timeout_seconds=0.05)
    client.messages = client.beta.messages = SlowMessages(response(verdict=APPROVE))  # type: ignore[assignment]
    started = asyncio.get_running_loop().time()
    out = await rev.review(SIGNAL, PLAN, CONTEXT)
    assert asyncio.get_running_loop().time() - started < 2
    assert out.vote is Vote.EJECT and "took longer than" in (out.error or "")


@pytest.mark.anyio
async def test_cancelling_a_review_propagates() -> None:
    rev, client, _ = reviewer(response(verdict=APPROVE), timeout_seconds=30)
    client.messages = client.beta.messages = SlowMessages(response(verdict=APPROVE))  # type: ignore[assignment]
    task = asyncio.create_task(rev.review(SIGNAL, PLAN, CONTEXT))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class KV:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def get_kv(self, key: str) -> Any | None:
        return self.data.get(key)

    def set_kv(self, key: str, value: Any) -> None:
        self.data[key] = json.loads(json.dumps(value))


@pytest.mark.anyio
async def test_daily_cap_survives_a_restart() -> None:
    kv = KV()
    clock = ManualClock(T0)
    first = ClaudeReviewer(
        LlmConfig(enabled=True, max_calls_per_day=2),
        clock,
        client=FakeClient(response(verdict=VETO)),
        state=kv,  # type: ignore[arg-type]
    )
    await first.review(SIGNAL, PLAN, CONTEXT)
    await first.review(SIGNAL, PLAN, CONTEXT)
    client = FakeClient(response(verdict=APPROVE))
    restarted = ClaudeReviewer(LlmConfig(enabled=True, max_calls_per_day=2), clock, client=client, state=kv)  # type: ignore[arg-type]
    out = await restarted.review(SIGNAL, PLAN, CONTEXT)
    assert "cap reached" in (out.error or "") and client.messages.calls == []
    usage = restarted.usage_today()
    assert usage["calls"] == 2 and usage["vetoes"] == 2 and usage["input_tokens"] == 1800
    clock.advance(timedelta(days=1).total_seconds())
    assert (await restarted.review(SIGNAL, PLAN, CONTEXT)).vote is Vote.PROCEED
