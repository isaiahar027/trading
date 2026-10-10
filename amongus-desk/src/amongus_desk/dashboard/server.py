"""Dashboard HTTP server (FastAPI): read-only state, a live event stream and a few guarded operator actions.

Security model:

* Listens on 127.0.0.1 by default; reach it through an SSH tunnel or Tailscale (see docs/WINDOWS_VPS.md).
* Every ``/api`` route except ``/api/health`` needs ``Authorization: Bearer <token>`` (constant-time compare).
  The right token is always accepted. Repeated wrong tokens from one client IP get HTTP 429 for a while.
* The token travels only in a header, never in a URL or cookie, so cross-site requests can't use it (no CSRF)
  and it never lands in logs or browser history. The event stream is read with ``fetch()`` for that reason.
* A Host allow-list blocks DNS-rebinding; strict CSP and other headers on every response; no API docs.
* Request bodies are parsed with a JSON parser that refuses NaN, Infinity and numbers that overflow to
  infinity (``1e400``), and no float field accepts a non-finite value.
* There is no "place a trade" endpoint. The operator can stop (kill / panic), resume, reconcile, sync equity
  and ask the crew to hold a meeting; the crew still decides.
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any, Literal, NoReturn

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import DeskConfig
from ..domain import Side
from ..engine import DeskEngine, OperatorActionError
from ..events import EventHub

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
KEEPALIVE_SECONDS = 15.0

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'"
)
_SECURITY_HEADERS: list[tuple[bytes, bytes]] = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]


class SecurityMiddleware:
    """Pure ASGI middleware (safe for streaming responses): Host allow-list + security headers."""

    def __init__(self, app: ASGIApp, allowed_hosts: list[str]) -> None:
        self.app = app
        self.allowed = {h.lower() for h in allowed_hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = ""
        for name, value in scope.get("headers", []):
            if name == b"host":
                host = value.decode("latin-1").strip().lower()
                break
        hostname = host[1 : host.index("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0]
        if hostname not in self.allowed:
            response = JSONResponse({"error": "host not allowed"}, status_code=400)
            await self._with_headers(response, scope, receive, send)
            return
        is_api = scope.get("path", "").startswith("/api")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(_SECURITY_HEADERS)
                if is_api:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)

    async def _with_headers(self, response: Response, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *_SECURITY_HEADERS]
            await send(message)

        await response(scope, receive, send_wrapper)


class AuthGuard:
    """Bearer-token check with a per-client lockout for wrong tokens.

    The right token is always accepted, even from a locked-out IP: every client behind the SSH tunnel (and
    behind Docker's published port) arrives as the same IP, so a lockout that refused the right token would
    let any web page or local process that can reach the port keep the operator away from kill / panic.
    A request without a Bearer credential guessed nothing and is not counted. Brute force is stopped by the
    token's entropy (16+ characters; the generated one is 43); the lockout answers further wrong tokens with
    429 and keeps them out of the log.
    """

    def __init__(
        self, token: str, max_failures: int, window_seconds: int, now: Callable[[], float] = time.monotonic
    ) -> None:
        if len(token) < 16:
            raise ValueError("dashboard token is too short")
        self._token = token.encode()
        self._max = max_failures
        self._window = window_seconds
        self._now = now
        # bounded: wrong tokens during a lockout replace the oldest entry, so the lockout lasts while they go on
        self._failures: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=max_failures))

    def _prune(self, key: str) -> deque[float]:
        q = self._failures[key]
        cutoff = self._now() - self._window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def __call__(self, request: Request) -> str:
        client = request.client.host if request.client else "unknown"
        scheme, _, given = request.headers.get("authorization", "").partition(" ")
        given = given.strip()
        if scheme.lower() != "bearer" or not given:
            raise _unauthorized()  # no credential: nothing was guessed, so nothing is counted
        if hmac.compare_digest(given.encode(), self._token):
            return f"dashboard@{client}"
        q = self._prune(client)
        locked = len(q) >= self._max
        q.append(self._now())
        if locked:
            log.debug("dashboard: wrong token from locked-out %s", client)
            retry_after = max(1, math.ceil(q[0] + self._window - self._now()))
            raise HTTPException(
                status_code=429,
                detail="too many wrong tokens; try again later",
                headers={"Retry-After": str(retry_after)},
            )
        log.warning("dashboard auth failure from %s (%d in window)", client, len(q))
        raise _unauthorized()


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="missing or invalid token", headers={"WWW-Authenticate": "Bearer"})


class _NonFiniteNumberError(ValueError):
    pass


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):  # "1e400" parses to inf
        raise _NonFiniteNumberError(text)
    return value


def _reject_constant(name: str) -> NoReturn:  # NaN, Infinity, -Infinity
    raise _NonFiniteNumberError(name)


def loads_finite(raw: bytes | str) -> Any:
    """``json.loads`` that refuses NaN, Infinity and numbers that overflow to infinity (``JSONDecodeError``)."""
    try:
        return json.loads(raw, parse_float=_finite_float, parse_constant=_reject_constant)
    except _NonFiniteNumberError as exc:
        doc = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        raise json.JSONDecodeError(f"non-finite number {exc}", doc, 0) from exc


class _FiniteJsonRequest(Request):
    async def json(self) -> Any:
        if not hasattr(self, "_json"):
            self._json = loads_finite(await self.body())
        return self._json


class _FiniteJsonRoute(APIRoute):
    """Parses request bodies with :func:`loads_finite`; FastAPI answers its ``JSONDecodeError`` with 422.

    The stock parser turns ``1e400`` into ``inf``. Pydantic would reject it, but the 422 body echoes the input
    and a non-finite float can't be rendered as JSON, so the client would get a 500 instead.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def finite_json_handler(request: Request) -> Response:
            return await handler(_FiniteJsonRequest(request.scope, request.receive))

        return finite_json_handler


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class KillBody(_Body):
    reason: str = Field(default="dashboard", max_length=300)


class ConfirmBody(_Body):
    confirm: str = Field(max_length=20)


class ReconcileBody(_Body):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    outcome: Literal["filled", "not_filled"]
    fill_price: float | None = Field(default=None, gt=0, lt=1e9)  # bound keeps the P&L math finite


class EquityBody(_Body):
    equity: float = Field(gt=0, lt=1e12)


class MeetingBody(_Body):
    symbol: str = Field(pattern=r"^[A-Z0-9._]{2,20}$")
    force_side: Side | None = None


async def sse_events(hub: EventHub, is_disconnected: Callable[[], Awaitable[bool]]) -> AsyncIterator[str]:
    """Server-sent events: one ``event:``/``data:`` frame per hub event, comments as keep-alives."""
    async with hub.subscribe() as sub:
        yield ": connected\n\n"
        while True:
            event = await sub.next_within(KEEPALIVE_SECONDS)
            if await is_disconnected():
                return
            if event is None:
                yield ": keep-alive\n\n"
                continue
            payload = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
            yield f"event: {event.type}\ndata: {payload}\n\n"


def create_app(engine: DeskEngine, cfg: DeskConfig, token: str) -> FastAPI:
    app = FastAPI(title="Among Us Trading Desk", docs_url=None, redoc_url=None, openapi_url=None)
    app.router.route_class = _FiniteJsonRoute  # before any route is added
    guard = AuthGuard(token, cfg.dashboard.max_auth_failures, cfg.dashboard.auth_failure_window_seconds)
    operator = Depends(guard)

    async def run_action(action: Awaitable[Any]) -> JSONResponse:
        try:
            result = await action
        except OperatorActionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        body: dict[str, Any] = {"ok": True}
        if isinstance(result, BaseModel):
            body["result"] = result.model_dump(mode="json")
        return JSONResponse(body)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/api/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/state")
    async def state(_who: str = operator) -> JSONResponse:
        return JSONResponse(engine.state())

    @app.get("/api/events")
    async def events(request: Request, _who: str = operator) -> StreamingResponse:
        return StreamingResponse(
            sse_events(engine.hub, request.is_disconnected),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.post("/api/kill")
    async def kill(body: KillBody, who: str = operator) -> JSONResponse:
        return await run_action(engine.kill(body.reason, by=who))

    @app.post("/api/resume")
    async def resume(body: ConfirmBody, who: str = operator) -> JSONResponse:
        if body.confirm != "RESUME":
            return JSONResponse({"error": 'type "RESUME" to confirm'}, status_code=400)
        return await run_action(engine.resume(by=who))

    @app.post("/api/panic")
    async def panic(body: ConfirmBody, who: str = operator) -> JSONResponse:
        if body.confirm != "PANIC":
            return JSONResponse({"error": 'confirm must be "PANIC"'}, status_code=400)
        return await run_action(engine.panic(by=who))

    @app.post("/api/reconcile")
    async def reconcile(body: ReconcileBody, who: str = operator) -> JSONResponse:
        return await run_action(engine.reconcile(body.id, body.outcome, fill_price=body.fill_price, by=who))

    @app.post("/api/equity")
    async def equity(body: EquityBody, who: str = operator) -> JSONResponse:
        return await run_action(engine.set_equity(body.equity, by=who))

    @app.post("/api/meeting")
    async def meeting(body: MeetingBody, who: str = operator) -> JSONResponse:
        return await run_action(engine.manual_meeting(body.symbol, by=who, force_side=body.force_side))

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_middleware(SecurityMiddleware, allowed_hosts=cfg.dashboard.allowed_hosts)
    return app
