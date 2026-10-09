from __future__ import annotations

from decimal import Decimal

import pytest

from amongus_desk.clock import ManualClock
from amongus_desk.config import PaperConfig
from amongus_desk.domain import IntentStatus, Mode, OrderIntent, Position, Quote, Side
from amongus_desk.execution.paper import PaperVenue
from factories import T0, make_instrument


def _venue(*, halted: bool = False, quote: Quote | None = None) -> PaperVenue:
    inst = make_instrument()
    return PaperVenue(
        {inst.symbol: inst},
        PaperConfig(slippage_pips=0.2),
        ManualClock(T0),
        is_halted=lambda: halted,
        quote_source=lambda _s: quote,
    )


def _open(side: Side = Side.BUY, **over: object) -> OrderIntent:
    data: dict[str, object] = {
        "meeting_id": "m",
        "symbol": "EURUSD",
        "side": side,
        "lots": Decimal("0.10"),
        "entry_ref": 1.10000,
        "stop_price": 1.09850,
        "target_price": 1.10300,
        "mode": Mode.PAPER,
        "created_at": T0,
    }
    data.update(over)
    return OrderIntent.model_validate(data)


class Hook:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def __call__(self) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("disk full")


@pytest.mark.anyio
async def test_buy_fills_at_ask_plus_slippage() -> None:
    quote = Quote("EURUSD", 1.10000, 1.10010, T0)
    hook = Hook()
    rep = await _venue().submit(_open(Side.BUY), quote, hook)
    assert rep.status is IntentStatus.CONFIRMED and rep.fill_price_source == "venue"
    assert rep.fill_price == pytest.approx(1.10012)
    assert hook.calls == 1 and rep.clicked_at is not None


@pytest.mark.anyio
async def test_sell_fills_at_bid_minus_slippage_and_quote_source_fallback() -> None:
    quote = Quote("EURUSD", 1.10000, 1.10010, T0)
    rep = await _venue(quote=quote).submit(_open(Side.SELL), None, Hook())
    assert rep.fill_price == pytest.approx(1.09998)


@pytest.mark.anyio
async def test_no_quote_uses_reference_and_typical_spread() -> None:
    rep = await _venue().submit(_open(Side.BUY), None, Hook())
    # 1.10000 + half of 1.0 pip + 0.2 pip slippage
    assert rep.fill_price == pytest.approx(1.10007)


@pytest.mark.anyio
async def test_halted_aborts_without_write_ahead() -> None:
    hook = Hook()
    rep = await _venue(halted=True).submit(_open(), None, hook)
    assert rep.status is IntentStatus.ABORTED and rep.stage == "halted" and hook.calls == 0


@pytest.mark.anyio
async def test_write_ahead_failure_aborts_without_fill() -> None:
    rep = await _venue().submit(_open(), None, Hook(fail=True))
    assert rep.status is IntentStatus.ABORTED and rep.stage == "write_ahead" and rep.fill_price is None


@pytest.mark.anyio
async def test_invalid_or_unpriceable_intents_abort() -> None:
    hook = Hook()
    rep = await _venue().submit(_open(symbol="GBPUSD"), None, hook)
    assert rep.status is IntentStatus.ABORTED and rep.stage == "price"
    rep = await _venue().submit(_open(entry_ref=None), None, hook)
    assert rep.status is IntentStatus.ABORTED
    close = _open().model_copy(update={"purpose": "close"})
    rep = await _venue().submit(close, None, hook)
    assert rep.status is IntentStatus.ABORTED and rep.stage == "validate"
    assert hook.calls == 0


@pytest.mark.anyio
async def test_close_position_runs_even_when_halted() -> None:
    pos = Position(
        intent_id="i",
        symbol="EURUSD",
        side=Side.BUY,
        lots=Decimal("0.10"),
        entry_price=1.1,
        entry_price_source="venue",
        stop_price=1.099,
        target_price=1.103,
        opened_at=T0,
        time_stop_bars=24,
        last_price=1.1020,
    )
    hook = Hook()
    close = _open(Side.SELL).model_copy(update={"purpose": "close", "position_id": pos.id})
    rep = await _venue(halted=True).close_position(pos, close, None, hook)
    assert rep.status is IntentStatus.CONFIRMED and hook.calls == 1
    assert rep.fill_price == pytest.approx(1.1020 - 0.00005 - 0.00002)


@pytest.mark.anyio
async def test_close_all_and_health() -> None:
    venue = _venue(halted=True)
    intent = OrderIntent(
        purpose="close_all",
        meeting_id=None,
        symbol="*",
        side=None,
        lots=None,
        entry_ref=None,
        stop_price=None,
        target_price=None,
        mode=Mode.PAPER,
        created_at=T0,
    )
    hook = Hook()
    assert (await venue.close_all(intent, hook)).status is IntentStatus.CONFIRMED and hook.calls == 1
    assert (await venue.close_all(intent, Hook(fail=True))).status is IntentStatus.ABORTED
    health = await venue.preflight()
    assert health.ok and health.checks[0].name == "paper"
    assert (await venue.health()).ok
    await venue.aclose()
