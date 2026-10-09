from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from rigs import Rig, desk_cfg

from amongus_desk.dashboard.server import CSP, AuthGuard, create_app, sse_events
from amongus_desk.domain import IntentStatus

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    cfg = desk_cfg(
        tmp_path,
        dashboard={"allowed_hosts": ["testserver", "127.0.0.1", "localhost"], "max_auth_failures": 3},
    )
    r = Rig(tmp_path, cfg=cfg)  # streams the breakout bar: one open paper position
    asyncio.run(r.boot_and_run())
    assert len(r.engine.book.active()) == 1
    yield r
    r.close()


@pytest.fixture
def client(rig: Rig) -> TestClient:
    return TestClient(create_app(rig.engine, rig.cfg, TOKEN))


def test_public_routes_and_security_headers(client: TestClient) -> None:
    index = client.get("/")
    assert index.status_code == 200 and "Among Us Trading Desk" in index.text
    assert index.headers["content-security-policy"] == CSP
    assert index.headers["x-frame-options"] == "DENY"
    assert index.headers["x-content-type-options"] == "nosniff"
    assert index.headers["referrer-policy"] == "no-referrer"
    for asset in ("app.js", "styles.css", "favicon.svg"):
        assert client.get(f"/static/{asset}").status_code == 200
    health = client.get("/api/health")
    assert health.json() == {"ok": True} and health.headers["cache-control"] == "no-store"
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


def test_api_requires_token(client: TestClient) -> None:
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    ok = client.get("/api/state", headers=AUTH)
    assert ok.status_code == 200 and ok.json()["mode"] == "paper"


def test_lockout_after_failures(rig: Rig) -> None:
    client = TestClient(create_app(rig.engine, rig.cfg, TOKEN))
    for _ in range(3):
        assert client.get("/api/state", headers={"Authorization": "Bearer nope"}).status_code == 401
    # locked out, even with the right token, until the window passes
    assert client.get("/api/state", headers=AUTH).status_code == 429


def test_auth_guard_window_expires() -> None:
    now = [0.0]
    guard = AuthGuard(TOKEN, max_failures=2, window_seconds=60, now=lambda: now[0])
    guard._failures["1.2.3.4"].extend([0.0, 1.0])  # noqa: SLF001
    assert len(guard._prune("1.2.3.4")) == 2  # noqa: SLF001
    now[0] = 62.0
    assert len(guard._prune("1.2.3.4")) == 0  # noqa: SLF001
    with pytest.raises(ValueError):
        AuthGuard("short", 1, 1)


def test_host_allow_list_blocks_rebinding(rig: Rig) -> None:
    client = TestClient(create_app(rig.engine, rig.cfg, TOKEN), base_url="http://evil.example")
    r = client.get("/api/health")
    assert r.status_code == 400 and r.json() == {"error": "host not allowed"}
    assert r.headers["content-security-policy"] == CSP
    ok = TestClient(create_app(rig.engine, rig.cfg, TOKEN), base_url="http://127.0.0.1:8765")
    assert ok.get("/api/health").status_code == 200


def test_kill_and_resume(client: TestClient, rig: Rig) -> None:
    r = client.post("/api/kill", headers=AUTH, json={"reason": "lunch"})
    assert r.status_code == 200 and rig.kill.engaged()
    assert rig.kill.state()["by"].startswith("dashboard@")
    assert client.post("/api/resume", headers=AUTH, json={"confirm": "yes"}).status_code == 400
    assert client.post("/api/resume", headers=AUTH, json={"confirm": "RESUME"}).status_code == 200
    assert not rig.kill.engaged()


def test_validation_rejects_unknown_fields(client: TestClient) -> None:
    r = client.post("/api/kill", headers=AUTH, json={"reason": "x", "extra": 1})
    assert r.status_code == 422
    assert client.post("/api/equity", headers=AUTH, json={"equity": -5}).status_code == 422
    assert client.post("/api/meeting", headers=AUTH, json={"symbol": "eurusd lower"}).status_code == 422


def test_panic_requires_exact_confirmation(client: TestClient, rig: Rig) -> None:
    assert client.post("/api/panic", headers=AUTH, json={"confirm": "panic"}).status_code == 400
    r = client.post("/api/panic", headers=AUTH, json={"confirm": "PANIC"})
    assert r.status_code == 200 and r.json()["result"]["status"] == IntentStatus.CONFIRMED.value
    assert rig.kill.engaged() and rig.engine.book.active() == []


def test_operator_errors_are_409(client: TestClient) -> None:
    r = client.post("/api/reconcile", headers=AUTH, json={"id": "nope", "outcome": "filled"})
    assert r.status_code == 409 and "nothing to reconcile" in r.json()["error"]
    r = client.post("/api/meeting", headers=AUTH, json={"symbol": "XAUUSD"})
    assert r.status_code == 409


def test_equity_and_meeting(client: TestClient) -> None:
    assert client.post("/api/equity", headers=AUTH, json={"equity": 9876.5}).status_code == 200
    state = client.get("/api/state", headers=AUTH).json()
    assert state["equity"]["estimate"] == pytest.approx(9876.5)
    r = client.post("/api/meeting", headers=AUTH, json={"symbol": "EURUSD", "force_side": "BUY"})
    assert r.status_code == 200 and r.json()["result"]["trigger"] == "manual"


def test_state_shape_matches_frontend_contract(client: TestClient) -> None:
    state = client.get("/api/state", headers=AUTH).json()
    for key in (
        "mode",
        "venue",
        "dry_run",
        "kill_switch",
        "reconcile",
        "degraded",
        "health",
        "equity",
        "limits",
        "positions",
        "crew",
        "meetings",
        "intents",
        "instruments",
        "llm",
    ):
        assert key in state, key
    assert len(state["crew"]) == 6 and {"name", "color", "vote", "summary"} <= set(state["crew"][0])
    assert state["positions"][0]["unrealized_pnl"] is not None


@pytest.mark.anyio
async def test_sse_frames(rig: Rig) -> None:
    hub = rig.engine.hub
    disconnected = {"value": False}

    async def is_disconnected() -> bool:
        return disconnected["value"]

    gen = sse_events(hub, is_disconnected)
    assert await gen.__anext__() == ": connected\n\n"
    hub.publish("meeting", {"symbol": "EURUSD", "decision": "SKIP_ROUND"})
    frame = await asyncio.wait_for(gen.__anext__(), 2)
    event_line, data_line, *_ = frame.split("\n")
    assert event_line == "event: meeting"
    payload: dict[str, Any] = json.loads(data_line.removeprefix("data: "))
    assert payload["type"] == "meeting" and payload["data"]["symbol"] == "EURUSD"
    disconnected["value"] = True
    hub.publish("bar", {})
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(gen.__anext__(), 2)
    assert hub.subscriber_count == 0


def test_events_endpoint_requires_auth(client: TestClient) -> None:
    assert client.get("/api/events").status_code == 401
