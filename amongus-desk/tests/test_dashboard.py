from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from amongus_desk.dashboard.server import CSP, STATIC_DIR, AuthGuard, create_app, loads_finite, sse_events
from amongus_desk.domain import IntentStatus
from rigs import Rig, desk_cfg

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
JSON_AUTH = {**AUTH, "Content-Type": "application/json"}


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


def test_lockout_never_refuses_the_right_token(rig: Rig) -> None:
    # Every client behind the SSH tunnel is 127.0.0.1: wrong tokens from one of them (a stale tab, a local
    # process) must not keep the operator away from kill / panic.
    client = TestClient(create_app(rig.engine, rig.cfg, TOKEN))
    for _ in range(3):
        assert client.get("/api/state", headers={"Authorization": "Bearer nope"}).status_code == 401
    locked = client.get("/api/state", headers={"Authorization": "Bearer nope"})
    assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 0
    assert client.get("/api/state", headers=AUTH).status_code == 200
    assert client.post("/api/kill", headers=AUTH, json={"reason": "x"}).status_code == 200
    assert client.post("/api/panic", headers=AUTH, json={"confirm": "PANIC"}).status_code == 200
    assert client.get("/api/state", headers={"Authorization": "Bearer nope"}).status_code == 429


def test_requests_without_a_token_are_not_counted(rig: Rig) -> None:
    # e.g. <img src="http://127.0.0.1:8765/api/state"> on any web page: no Authorization header at all
    client = TestClient(create_app(rig.engine, rig.cfg, TOKEN))
    for headers in [{}] * 10 + [{"Authorization": f"Basic {TOKEN}"}, {"Authorization": "Bearer "}]:
        assert client.get("/api/state", headers=headers).status_code == 401
    for _ in range(3):  # max_auth_failures is 3: only real guesses count
        assert client.get("/api/state", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/state", headers={"Authorization": "Bearer nope"}).status_code == 429


def _auth_request(header: str | None, client: str = "1.2.3.4") -> Request:
    headers = [] if header is None else [(b"authorization", header.encode())]
    return Request({"type": "http", "method": "GET", "path": "/api/state", "headers": headers, "client": (client, 1)})


def _status(guard: AuthGuard, header: str | None, client: str = "1.2.3.4") -> int:
    try:
        guard(_auth_request(header, client))
    except HTTPException as exc:
        return exc.status_code
    return 200


def test_auth_guard_window_and_lockout() -> None:
    now = [0.0]
    guard = AuthGuard(TOKEN, max_failures=2, window_seconds=60, now=lambda: now[0])
    assert [_status(guard, "Bearer wrong") for _ in range(3)] == [401, 401, 429]
    assert _status(guard, f"Bearer {TOKEN}") == 200  # locked out, still accepted
    assert _status(guard, None) == 401 and _status(guard, "Bearer wrong", client="5.6.7.8") == 401  # per IP
    now[0] = 30.0
    assert _status(guard, "Bearer wrong") == 429  # a wrong token during a lockout replaces the oldest failure
    now[0] = 61.0  # the failure from t=0 has aged out, the one from t=30 has not
    assert [_status(guard, "Bearer wrong") for _ in range(2)] == [401, 429]
    now[0] = 200.0
    assert _status(guard, "Bearer wrong") == 401
    assert len(guard._failures["1.2.3.4"]) <= 2  # bounded however long the guessing goes on
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


@pytest.mark.parametrize("number", ["1e400", "-1e400", "Infinity", "-Infinity", "NaN", "0", "-1", "1e300"])
def test_non_finite_or_absurd_numbers_are_422(client: TestClient, rig: Rig, number: str) -> None:
    pos_id = rig.engine.book.active()[0].id
    body = f'{{"id": "{pos_id}", "outcome": "filled", "fill_price": {number}}}'
    assert client.post("/api/reconcile", headers=JSON_AUTH, content=body).status_code == 422
    assert client.post("/api/equity", headers=JSON_AUTH, content=f'{{"equity": {number}}}').status_code == 422
    assert client.post("/api/kill", headers=JSON_AUTH, content=f'{{"reason": {number}}}').status_code == 422
    assert len(rig.engine.book.active()) == 1 and not rig.kill.engaged()
    state = client.get("/api/state", headers=AUTH)
    assert state.status_code == 200 and state.json()["equity"]["estimate"] == pytest.approx(10_000, rel=0.1)


def test_finite_json_parser() -> None:
    assert loads_finite(b'{"a": 1.5, "b": [2, -3e2]}') == {"a": 1.5, "b": [2, -300.0]}
    for raw in ("1e400", "[NaN]", '{"x": -Infinity}'):
        with pytest.raises(json.JSONDecodeError):
            loads_finite(raw)


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


# ------------------------------------------------------------------------------------------- the page itself


class _FormButtons(HTMLParser):
    """Submit buttons of each <form>, in tree order (the first one is what Enter in a field "clicks")."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: dict[str, list[dict[str, str | None]]] = {}
        self._form: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "form":
            self._form = a.get("id") or f"form{len(self.forms)}"
            self.forms[self._form] = []
        elif self._form is not None and (
            (tag == "button" and (a.get("type") or "submit") == "submit")
            or (tag == "input" and a.get("type") in ("submit", "image"))
        ):
            self.forms[self._form].append(a)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._form = None


def _form_buttons() -> dict[str, list[dict[str, str | None]]]:
    parser = _FormButtons()
    parser.feed((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
    return parser.forms


def test_every_form_submits_through_its_action_button() -> None:
    # Enter in a text field submits through the form's first submit button, so a form with Cancel as a submit
    # button in front of OK turns "type a reason, press Enter" into a silent cancel.
    forms = _form_buttons()
    assert set(forms) == {"token-form", "confirm-form", "meeting-form", "equity-form"}
    for form_id, buttons in forms.items():
        assert len(buttons) == 1, form_id
    assert forms["confirm-form"][0]["id"] == "confirm-ok"


def test_page_script_never_uses_inner_html() -> None:
    source = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in source.replace("never innerHTML", "") and "insertAdjacentHTML" not in source


# A tiny fake DOM, enough to run app.js in Node: elements are looked up by id (created on first use), timers
# run on a manual clock and fetch() is scripted per scenario. The test prints what the page did as JSON.
_HARNESS = r"""
import { readFileSync } from "node:fs";
import vm from "node:vm";

const [appPath, inputPath] = process.argv.slice(2);
const input = JSON.parse(readFileSync(inputPath, "utf8"));
const TOKEN_KEY = "amongus-desk.token";

class FakeNode {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.listeners = {};
    this.classes = new Set();
    this.text = "";
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.open = false;
    this.style = {};
    const classes = this.classes;
    this.classList = {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c, on) => ((on ?? !classes.has(c)) ? classes.add(c) : classes.delete(c)),
    };
  }
  get firstChild() { return this.children[0] || null; }
  get offsetWidth() { return 0; }
  set className(v) { this.classes.clear(); for (const c of String(v).split(/\s+/)) if (c) this.classes.add(c); }
  get className() { return [...this.classes].join(" "); }
  set textContent(v) { this.children = []; this.text = String(v); }
  get textContent() { return this.text + this.children.map((c) => c.textContent).join(""); }
  append(...nodes) { this.children.push(...nodes); }
  removeChild(node) { this.children.splice(this.children.indexOf(node), 1); return node; }
  setAttribute(key, value) { if (key === "value") this.value = String(value); }
  addEventListener(type, fn, opts) { (this.listeners[type] ||= []).push({ fn, once: Boolean(opts && opts.once) }); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((l) => l.fn !== fn); }
  dispatch(type, extra = {}) {
    const event = { type, target: this, preventDefault() {}, ...extra };
    for (const l of [...(this.listeners[type] || [])]) {
      if (l.once) this.removeEventListener(type, l.fn);
      l.fn(event);
    }
  }
  click() { this.dispatch("click"); }
  focus() {}
  showModal() { this.open = true; }
  close() { if (this.open) { this.open = false; setImmediate(() => this.dispatch("close")); } }
  findAll(pred, out = []) {
    for (const c of this.children) {
      if (pred(c)) out.push(c);
      c.findAll(pred, out);
    }
    return out;
  }
}

const byIdMap = new Map();
const byId = (id) => { if (!byIdMap.has(id)) byIdMap.set(id, new FakeNode("div")); return byIdMap.get(id); };
const storage = new Map();
let now = 0;
let seq = 0;
const timers = new Map();
const calls = [];
let respond = () => [200, {}];

// an event stream that stays open (sends nothing) until the page aborts it
function streamResponse(signal) {
  const read = () => new Promise((_ok, fail) => signal.addEventListener("abort", () => fail(new Error("abort"))));
  return { status: 200, ok: true, body: { getReader: () => ({ read }) } };
}

async function fakeFetch(path, opts = {}) {
  const headers = opts.headers || {};
  const body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({ path, method: opts.method || "GET", auth: headers.Authorization || null, body });
  const [status, payload] = respond(path, opts);
  if (path === "/api/events" && status === 200) return streamResponse(opts.signal);
  return { status, ok: status >= 200 && status < 300, body: null, json: async () => payload };
}

const flush = async () => { for (let i = 0; i < 30; i++) await new Promise((r) => setImmediate(r)); };
async function advance(ms) {
  const end = now + ms;
  for (;;) {
    await flush();
    let next = null;
    for (const [id, t] of timers) if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t];
    if (!next) break;
    const [id, t] = next;
    now = t.at;
    if (t.every) t.at += t.every; else timers.delete(id);
    t.fn();
  }
  now = end;
  await flush();
}

const ctx = {
  console, AbortController, TextDecoder, Node: FakeNode,
  fetch: fakeFetch,
  setTimeout: (fn, ms = 0) => { timers.set(++seq, { at: now + Math.max(0, ms), fn, every: 0 }); return seq; },
  setInterval: (fn, ms) => { timers.set(++seq, { at: now + ms, fn, every: ms }); return seq; },
  clearTimeout: (id) => timers.delete(id),
  localStorage: {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: (k) => storage.delete(k),
  },
  document: {
    readyState: "complete", visibilityState: "visible", title: "",
    getElementById: byId,
    createElement: (tag) => new FakeNode(tag),
    createElementNS: (_ns, tag) => new FakeNode(tag),
    createTextNode: (text) => { const n = new FakeNode("#text"); n.text = String(text); return n; },
    addEventListener: () => {},
  },
};
ctx.window = ctx;
const buttons = (node) => node.findAll((n) => n.tagName === "BUTTON");
const posts = () => calls.filter((c) => c.method === "POST").map((c) => ({ path: c.path, body: c.body }));

function boot(token) {
  if (token) storage.set(TOKEN_KEY, token);
  vm.createContext(ctx);
  vm.runInContext(readFileSync(appPath, "utf8"), ctx);
}

const out = {};
const healthy = (path) => (path === "/api/state" ? [200, input.state] : [200, { ok: true }]);
if (input.scenario === "rejected") {
  respond = () => [input.status, { detail: "nope" }];
  boot("s".repeat(32));
  await advance(120000);
  out.requests = calls.length;
  out.stored = ctx.localStorage.getItem(TOKEN_KEY);
  out.dialogOpen = byId("token-dialog").open;
  out.error = byId("token-error").textContent;
  out.alerts = byId("alerts").textContent;
  byId("token-input").value = input.token;  // the operator pastes the right one
  respond = healthy;
  byId("token-form").dispatch("submit");
  await advance(16000);
  out.after = calls.slice(out.requests).map((c) => [c.path, c.auth]);
} else if (input.scenario === "reconcile") {
  respond = healthy;
  boot(input.token);
  await advance(100);
  const cards = byId("reconcile-list").children;
  out.cards = cards.map((card) => ({
    buttons: buttons(card).map((b) => b.textContent),
    inputs: card.findAll((n) => n.tagName === "INPUT").length,
    question: card.findAll((n) => n.classes.has("question")).map((n) => n.textContent)[0] ?? null,
  }));
  const answer = async (card, label, price) => {
    const field = card.findAll((n) => n.tagName === "INPUT")[0];
    if (field) field.value = price;
    const button = buttons(card).find((b) => b.textContent === label);
    if (!button) return null;  // the Python side reports the labels it did find
    button.click();
    await advance(10);
    const text = byId("confirm-text").textContent;
    byId("confirm-form").dispatch("submit", { submitter: byId("confirm-ok") });
    await advance(10);
    return text;
  };
  out.confirmText = await answer(cards[0], input.answers[0], "1.2345");
  await answer(cards[0], input.answers[1], "1.2345");
  await answer(cards[2], input.answers[2], "");
  out.posts = posts();
} else if (input.scenario === "dialogs") {
  respond = healthy;
  boot(input.token);
  await advance(100);
  const form = byId("confirm-form");
  const dialog = byId("confirm-dialog");
  // Enter in the Reason field: the browser submits through the form's default (first submit) button
  const submitter = input.submitter.id
    ? byId(input.submitter.id)
    : Object.assign(new FakeNode("button"), { value: input.submitter.value });
  byId("btn-kill").click();
  await advance(10);
  byId("confirm-input").value = "lunch";
  form.dispatch("submit", { submitter });
  await advance(10);
  out.enter = { posts: posts(), open: dialog.open };
  byId("btn-kill").click();
  await advance(10);
  byId("confirm-cancel").click();
  await advance(10);
  out.cancel = { posts: posts().length, open: dialog.open, toast: byId("toast").textContent };
  byId("btn-kill").click();
  await advance(10);
  dialog.dispatch("cancel");  // Escape
  dialog.close();
  await advance(10);
  out.escape = { posts: posts().length, open: dialog.open };
  byId("btn-resume").disabled = false;
  byId("btn-resume").click();
  await advance(10);
  byId("confirm-input").value = "resume";
  form.dispatch("submit", { submitter });
  await advance(10);
  out.resumeWrong = { posts: posts().length, open: dialog.open };
  byId("confirm-input").value = "RESUME";
  form.dispatch("submit", { submitter });
  await advance(10);
  out.resume = { posts: posts().slice(-1), open: dialog.open };
}
process.stdout.write(JSON.stringify(out));
"""

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")


def run_page(tmp_path: Path, scenario: dict[str, Any]) -> dict[str, Any]:
    harness = tmp_path / "harness.mjs"
    harness.write_text(_HARNESS, encoding="utf-8")
    data = tmp_path / "input.json"
    data.write_text(json.dumps({"token": TOKEN, **scenario}), encoding="utf-8")
    node = shutil.which("node")
    assert node is not None
    done = subprocess.run(
        [node, str(harness), str(STATIC_DIR / "app.js"), str(data)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    result: dict[str, Any] = json.loads(done.stdout)
    return result


@needs_node
@pytest.mark.parametrize("status", [401, 429])
def test_page_stops_polling_after_the_token_is_rejected(tmp_path: Path, status: int) -> None:
    out = run_page(tmp_path, {"scenario": "rejected", "status": status})
    # one /api/state and one /api/events, then nothing for two minutes (it used to poll every 15 s)
    assert out["requests"] == 2
    assert out["stored"] is None and out["dialogOpen"] is True
    assert "Token rejected" in out["error"] and "paste the token again" in out["error"]
    assert ("too many wrong tokens" in out["error"]) == (status == 429)
    assert "Token rejected" in out["alerts"]
    # pasting the right token starts polling and streaming again, with that token
    assert ["/api/state", f"Bearer {TOKEN}"] in out["after"] and ["/api/events", f"Bearer {TOKEN}"] in out["after"]


@needs_node
def test_reconcile_cards_show_the_servers_question_and_options(tmp_path: Path, client: TestClient) -> None:
    state = client.get("/api/state", headers=AUTH).json()
    base = {"symbol": "EURUSD", "side": "BUY", "lots": "0.01", "status": "RECONCILE", "created_at": state["now"]}
    state["reconcile"] = [
        {
            **base,
            "id": "pos1",
            "kind": "position",
            "message": "the desk can't tell whether it is still open",
            "question": "Is this BUY 0.01 EURUSD position still open in UniTrader?",
            "options": [
                {"outcome": "filled", "label": "No, it is closed", "needs_price": True},
                {"outcome": "not_filled", "label": "Yes, it is still open", "needs_price": False},
            ],
        },
        {
            **base,
            "id": "int1",
            "kind": "intent",
            "message": "",
            "question": "Did close-all go through in UniTrader?",
            "options": [
                {"outcome": "filled", "label": "Yes, everything closed", "needs_price": False},
                {"outcome": "not_filled", "label": "No, positions are still open", "needs_price": False},
            ],
        },
        {**base, "id": "pos2", "kind": "position", "message": "is this position still open in UniTrader?"},
        {**base, "id": "int2", "kind": "intent", "message": "could not verify"},
    ]
    out = run_page(
        tmp_path,
        {
            "scenario": "reconcile",
            "state": state,
            "answers": ["No, it is closed", "Yes, it is still open", "No, it is closed"],
        },
    )
    cards = out["cards"]
    assert cards[0] == {
        "buttons": ["No, it is closed", "Yes, it is still open"],
        "inputs": 1,
        "question": "Is this BUY 0.01 EURUSD position still open in UniTrader?",
    }
    assert cards[1]["buttons"] == ["Yes, everything closed", "No, positions are still open"] and cards[1]["inputs"] == 0
    # without question / options from the server: a position card still asks the right way round
    assert cards[2]["buttons"] == ["No, it is closed", "Yes, it is still open"]
    assert cards[2]["question"] == "Is this position still open in UniTrader?"
    assert cards[3]["buttons"] == ["It filled", "Nothing happened"] and cards[3]["inputs"] == 1
    assert "No, it is closed" in out["confirmText"] and "still open in UniTrader?" in out["confirmText"]
    assert out["posts"] == [
        {"path": "/api/reconcile", "body": {"id": "pos1", "outcome": "filled", "fill_price": 1.2345}},
        {"path": "/api/reconcile", "body": {"id": "pos1", "outcome": "not_filled", "fill_price": None}},
        {"path": "/api/reconcile", "body": {"id": "pos2", "outcome": "filled", "fill_price": None}},
    ]


@needs_node
def test_enter_in_the_kill_reason_engages_the_kill_switch(tmp_path: Path, client: TestClient) -> None:
    default = _form_buttons()["confirm-form"][0]  # what Enter "clicks"
    state = client.get("/api/state", headers=AUTH).json()
    submitter = {"id": default.get("id"), "value": default.get("value") or ""}
    out = run_page(tmp_path, {"scenario": "dialogs", "state": state, "submitter": submitter})
    assert out["enter"] == {"posts": [{"path": "/api/kill", "body": {"reason": "lunch"}}], "open": False}
    assert out["cancel"]["posts"] == 1 and not out["cancel"]["open"] and "NOT engaged" in out["cancel"]["toast"]
    assert out["escape"] == {"posts": 1, "open": False}
    assert out["resumeWrong"] == {"posts": 1, "open": True}  # Resume still needs RESUME typed exactly
    assert out["resume"] == {"posts": [{"path": "/api/resume", "body": {"confirm": "RESUME"}}], "open": False}
