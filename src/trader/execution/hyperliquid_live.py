"""Live execution on Hyperliquid perps via the official SDK (optional dependency).

Guarded three ways before it can even be constructed:
  1. config run.mode == "live"
  2. every AgenKit phase gate approved (state/approvals.json, see trader.approvals)
  3. env TRADER_LIVE_CONFIRM == "I_ACCEPT_REAL_LOSSES"
Use an API ("agent") wallet with no withdrawal rights, never the main key.
Orders are IOC limits with a bounded slippage price (the SDK's market_open/close).
"""
from __future__ import annotations

import os
import time

from ..market import Fill, OrderBook

LIVE_CONFIRM = "I_ACCEPT_REAL_LOSSES"


class LiveTradingNotAllowed(RuntimeError):
    pass


class HyperliquidBroker:
    def __init__(self, max_slippage: float = 0.005, taker_fee_bps: float = 4.5, testnet: bool = False):
        if os.environ.get("TRADER_LIVE_CONFIRM") != LIVE_CONFIRM:
            raise LiveTradingNotAllowed(f"set TRADER_LIVE_CONFIRM={LIVE_CONFIRM} to route real orders")
        try:
            import eth_account
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
            from hyperliquid.utils import constants
        except ImportError as e:
            raise LiveTradingNotAllowed("pip install -e '.[live]' for live trading") from e
        key, address = os.environ.get("HL_SECRET_KEY"), os.environ.get("HL_ACCOUNT_ADDRESS")
        if not key or not address:
            raise LiveTradingNotAllowed("HL_SECRET_KEY and HL_ACCOUNT_ADDRESS must be set")
        url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        wallet = eth_account.Account.from_key(key)
        self.exchange = Exchange(wallet, url, account_address=address)
        self.info = Info(url, skip_ws=True)
        self.address = address
        self.max_slippage = max_slippage
        self.fee_bps = taker_fee_bps
        self.sz_decimals = {a["name"]: a["szDecimals"] for a in self.info.meta()["universe"]}

    def account_equity(self) -> float:
        return float(self.info.user_state(self.address)["marginSummary"]["accountValue"])

    def positions(self) -> dict[str, float]:
        out = {}
        for p in self.info.user_state(self.address)["assetPositions"]:
            out[p["position"]["coin"]] = float(p["position"]["szi"])
        return out

    def execute(self, symbol, qty, ref_px, ts, reason, book: OrderBook | None = None, decision_id=""):
        sz = round(abs(qty), self.sz_decimals[symbol])
        if sz == 0:
            return None
        res = self.exchange.market_open(symbol, qty > 0, sz, px=None, slippage=self.max_slippage)
        if res.get("status") != "ok":
            raise RuntimeError(f"order rejected: {res}")
        statuses = res["response"]["data"]["statuses"]
        filled = [s["filled"] for s in statuses if "filled" in s]
        if not filled:
            return None  # IOC not filled; nothing happened
        f = filled[0]
        fqty = float(f["totalSz"]) * (1 if qty > 0 else -1)
        px = float(f["avgPx"])
        return Fill(symbol, fqty, px, abs(fqty) * px * self.fee_bps / 1e4, int(time.time() * 1000), reason, decision_id)

    # ---- venue-side protective stop: survives this process dying
    def round_px(self, symbol: str, px: float) -> float:
        # Hyperliquid perps: <= 5 significant figures and <= (6 - szDecimals) decimals
        return round(float(f"{px:.5g}"), 6 - self.sz_decimals[symbol])

    def protect(self, symbol: str, qty: float, stop_px: float) -> int | None:
        """Place a reduce-only stop-market trigger for an open position of signed size qty."""
        is_buy = qty < 0  # closing a short buys
        px = self.round_px(symbol, stop_px)
        res = self.exchange.order(symbol, is_buy, round(abs(qty), self.sz_decimals[symbol]), px,
                                  {"trigger": {"triggerPx": px, "isMarket": True, "tpsl": "sl"}}, reduce_only=True)
        if res.get("status") != "ok":
            raise RuntimeError(f"protective stop rejected: {res}")
        st = res["response"]["data"]["statuses"][0]
        return st.get("resting", {}).get("oid")

    def unprotect(self, symbol: str, oid: int) -> None:
        self.exchange.cancel(symbol, oid)
