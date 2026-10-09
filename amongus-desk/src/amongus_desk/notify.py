"""Operator alerts (phone push via ntfy and/or Telegram). Never raises into the trading path.

ntfy messages use ntfy's JSON publishing endpoint (``POST <server>/`` with ``topic``, ``title``,
``message``, ``priority``, ``tags``) so titles may contain any Unicode. Telegram uses the Bot API
``sendMessage`` method. Secrets (topic, bot token) never appear in logs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, Protocol

import httpx2

from .clock import Clock, SystemClock
from .config import NotifyConfig, Secrets

log = logging.getLogger(__name__)

Priority = Literal["low", "default", "high", "urgent"]
_NTFY_PRIORITY: dict[str, int] = {"low": 2, "default": 3, "high": 4, "urgent": 5}
_TELEGRAM_LIMIT = 4000  # Bot API allows 4096 characters per message


class Notifier(Protocol):
    async def notify(
        self, title: str, message: str, *, priority: Priority = "default", dedup_key: str | None = None
    ) -> None: ...

    async def aclose(self) -> None: ...


class NullNotifier:
    """Records alerts in memory (tests) and logs them."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Priority]] = []

    async def notify(
        self, title: str, message: str, *, priority: Priority = "default", dedup_key: str | None = None
    ) -> None:
        self.sent.append((title, message, priority))
        log.info("alert (not delivered, no channel configured): %s — %s", title, message)

    async def aclose(self) -> None:
        return None


class AlertNotifier:
    """Fan-out to the configured channels with per-key de-duplication."""

    def __init__(
        self,
        cfg: NotifyConfig,
        secrets: Secrets,
        *,
        title_prefix: str = "",
        clock: Clock | None = None,
        client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._cfg = cfg
        self._ntfy_topic = secrets.ntfy_topic.get_secret_value() if secrets.ntfy_topic else None
        self._tg_token = secrets.telegram_bot_token.get_secret_value() if secrets.telegram_bot_token else None
        self._tg_chat = secrets.telegram_chat_id
        self._prefix = title_prefix
        self._clock = clock or SystemClock()
        self._client = client or httpx2.AsyncClient(timeout=cfg.request_timeout_seconds)
        self._owns_client = client is None
        self._last_sent: dict[str, float] = {}
        self._stats: dict[str, Any] = {"sent": 0, "failed": 0, "suppressed": 0, "last_error": None}

    @property
    def channels(self) -> list[str]:
        out = []
        if self._ntfy_topic:
            out.append("ntfy")
        if self._tg_token and self._tg_chat:
            out.append("telegram")
        return out

    def status(self) -> dict[str, Any]:
        return {"channels": self.channels, **self._stats}

    async def notify(
        self, title: str, message: str, *, priority: Priority = "default", dedup_key: str | None = None
    ) -> None:
        key = dedup_key or f"{title}\n{message}"
        now = self._clock.monotonic()
        last = self._last_sent.get(key)
        if last is not None and now - last < self._cfg.min_interval_seconds:
            self._stats["suppressed"] += 1
            return
        self._last_sent[key] = now
        full_title = f"{self._prefix}{title}"
        sends = []
        if self._ntfy_topic:
            sends.append(self._send_ntfy(full_title, message, priority))
        if self._tg_token and self._tg_chat:
            sends.append(self._send_telegram(full_title, message))
        if not sends:
            log.info("alert (no channel configured): %s — %s", full_title, message)
            return
        results = await asyncio.gather(*sends, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                self._stats["failed"] += 1
                self._stats["last_error"] = type(result).__name__
                log.warning("alert delivery failed: %s", type(result).__name__)
            else:
                self._stats["sent"] += 1

    async def _send_ntfy(self, title: str, message: str, priority: Priority) -> None:
        payload = {
            "topic": self._ntfy_topic,
            "title": title,
            "message": message,
            "priority": _NTFY_PRIORITY[priority],
            "tags": ["rotating_light"] if priority in ("high", "urgent") else ["robot"],
        }
        response = await self._client.post(self._cfg.ntfy_server.rstrip("/") + "/", json=payload)
        if response.status_code >= 400:
            raise _DeliveryError(f"ntfy HTTP {response.status_code}")

    async def _send_telegram(self, title: str, message: str) -> None:
        text = f"{title}\n{message}"[:_TELEGRAM_LIMIT]
        response = await self._client.post(
            f"https://api.telegram.org/bot{self._tg_token}/sendMessage",
            json={"chat_id": self._tg_chat, "text": text, "disable_web_page_preview": True},
        )
        if response.status_code >= 400:
            raise _DeliveryError(f"telegram HTTP {response.status_code}")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class _DeliveryError(Exception):
    pass


def build_notifier(
    cfg: NotifyConfig, secrets: Secrets, *, title_prefix: str = "", clock: Clock | None = None
) -> Notifier:
    """An :class:`AlertNotifier` when a channel is configured, else a :class:`NullNotifier` that only logs."""
    if secrets.ntfy_topic or (secrets.telegram_bot_token and secrets.telegram_chat_id):
        return AlertNotifier(cfg, secrets, title_prefix=title_prefix, clock=clock)
    return NullNotifier()
