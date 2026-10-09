"""Operator alerts (phone push). Implementations must never raise into the trading path."""

from __future__ import annotations

from typing import Literal, Protocol

Priority = Literal["low", "default", "high", "urgent"]


class Notifier(Protocol):
    async def notify(
        self, title: str, message: str, *, priority: Priority = "default", dedup_key: str | None = None
    ) -> None: ...

    async def aclose(self) -> None: ...


class NullNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Priority]] = []

    async def notify(
        self, title: str, message: str, *, priority: Priority = "default", dedup_key: str | None = None
    ) -> None:
        self.sent.append((title, message, priority))

    async def aclose(self) -> None:
        return None
