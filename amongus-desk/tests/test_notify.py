from __future__ import annotations

import json

import httpx2
import pytest

from amongus_desk.clock import ManualClock
from amongus_desk.config import NotifyConfig, Secrets
from amongus_desk.notify import AlertNotifier, NullNotifier, build_notifier
from factories import T0

SECRETS = Secrets.from_env(
    {"DESK_NTFY_TOPIC": "desk-topic-xyz", "DESK_TELEGRAM_BOT_TOKEN": "123:abc", "DESK_TELEGRAM_CHAT_ID": "42"}
)


def _notifier(handler, secrets: Secrets = SECRETS, interval: int = 300):  # type: ignore[no-untyped-def]
    clock = ManualClock(T0)
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    n = AlertNotifier(
        NotifyConfig(min_interval_seconds=interval), secrets, title_prefix="[DEMO] ", clock=clock, client=client
    )
    return n, clock


@pytest.mark.anyio
async def test_sends_to_ntfy_json_and_telegram() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"ok": True})

    n, _ = _notifier(handler)
    await n.notify("🚨 Order UNCONFIRMED", "check the terminal", priority="urgent")
    assert len(seen) == 2
    ntfy = next(r for r in seen if r.url.host == "ntfy.sh")
    body = json.loads(ntfy.content)
    assert body == {
        "topic": "desk-topic-xyz",
        "title": "[DEMO] 🚨 Order UNCONFIRMED",
        "message": "check the terminal",
        "priority": 5,
        "tags": ["rotating_light"],
    }
    tg = next(r for r in seen if r.url.host == "api.telegram.org")
    assert tg.url.path == "/bot123:abc/sendMessage"
    assert json.loads(tg.content)["chat_id"] == "42"
    assert n.status()["sent"] == 2 and n.status()["channels"] == ["ntfy", "telegram"]


@pytest.mark.anyio
async def test_dedup_window() -> None:
    count = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal count
        count += 1
        return httpx2.Response(200)

    n, clock = _notifier(handler, Secrets.from_env({"DESK_NTFY_TOPIC": "t"}), interval=300)
    await n.notify("a", "b", dedup_key="login")
    await n.notify("a", "different text, same key", dedup_key="login")
    assert count == 1 and n.status()["suppressed"] == 1
    clock.advance(301)
    await n.notify("a", "b", dedup_key="login")
    assert count == 2


@pytest.mark.anyio
async def test_failures_never_raise_and_do_not_leak_secrets(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "ntfy.sh":
            raise httpx2.ConnectError("no route", request=request)
        return httpx2.Response(500)

    n, _ = _notifier(handler)
    await n.notify("t", "m")
    assert n.status()["failed"] == 2
    assert "123:abc" not in caplog.text and "desk-topic-xyz" not in caplog.text


@pytest.mark.anyio
async def test_telegram_text_is_truncated() -> None:
    sent: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content)["text"])
        return httpx2.Response(200)

    n, _ = _notifier(handler, Secrets.from_env({"DESK_TELEGRAM_BOT_TOKEN": "1:x", "DESK_TELEGRAM_CHAT_ID": "9"}))
    await n.notify("t", "x" * 10_000)
    assert len(sent[0]) == 4000


@pytest.mark.anyio
async def test_build_notifier_and_null() -> None:
    assert isinstance(build_notifier(NotifyConfig(), Secrets()), NullNotifier)
    real = build_notifier(NotifyConfig(), SECRETS)
    assert isinstance(real, AlertNotifier)
    await real.aclose()
    null = NullNotifier()
    await null.notify("t", "m", priority="high")
    assert null.sent == [("t", "m", "high")]
