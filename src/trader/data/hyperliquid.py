"""Hyperliquid public market data (no key needed). Docs: https://hyperliquid.gitbook.io"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

from ..state_engine import interval_ms
from ..market import Candle, Level, MarketCtx, OrderBook

INFO_URL = "https://api.hyperliquid.xyz/info"


class HyperliquidData:
    def __init__(self, url: str = INFO_URL, session: requests.Session | None = None, timeout: float = 10.0):
        self.url = url
        self.http = session or requests.Session()
        self.timeout = timeout

    def _post(self, body: dict):
        for attempt in range(6):
            try:
                r = self.http.post(self.url, json=body, timeout=self.timeout)
                if r.status_code == 200:
                    return r.json()
                if r.status_code not in (429, 500, 502, 503, 504):
                    r.raise_for_status()
            except requests.RequestException:
                if attempt == 5:
                    raise
            time.sleep(2 ** attempt)
        raise RuntimeError(f"hyperliquid info failed: {body.get('type')}")

    def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
        """Paginates (the API caps each response). Returns candles sorted by open time."""
        out: dict[int, Candle] = {}
        step = interval_ms(interval) * 4000
        t = start_ms
        while t < end_ms:
            raw = self._post({"type": "candleSnapshot",
                              "req": {"coin": coin, "interval": interval, "startTime": t, "endTime": min(t + step, end_ms)}})
            for c in raw:
                out[c["t"]] = Candle(c["t"], c["T"], float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"]), float(c["v"]))
            t += step
        return [out[k] for k in sorted(out)]

    def l2_book(self, coin: str) -> OrderBook:
        raw = self._post({"type": "l2Book", "coin": coin})
        bids, asks = raw["levels"]
        # Stamp with local receipt time: that is when we could first know it. The venue's own
        # timestamp can be ahead of our clock (skew) and would falsely trip the lookahead guard.
        received = int(time.time() * 1000)
        return OrderBook(min(int(raw["time"]), received),
                         tuple(Level(float(l["px"]), float(l["sz"])) for l in bids),
                         tuple(Level(float(l["px"]), float(l["sz"])) for l in asks))

    def contexts(self) -> dict[str, MarketCtx]:
        meta, ctxs = self._post({"type": "metaAndAssetCtxs"})
        now = int(time.time() * 1000)
        out = {}
        for asset, c in zip(meta["universe"], ctxs):
            if asset.get("isDelisted") or c.get("markPx") is None:
                continue
            out[asset["name"]] = MarketCtx(now, float(c["funding"]), float(c["openInterest"]), float(c["markPx"]),
                                           float(c["oraclePx"]), float(c.get("premium") or 0.0),
                                           float(c["dayNtlVlm"]), float(c["prevDayPx"]))
        return out

    def funding_history(self, coin: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        out, t = [], start_ms
        while t < end_ms:
            raw = self._post({"type": "fundingHistory", "coin": coin, "startTime": t, "endTime": end_ms})
            if not raw:
                break
            out += [(int(r["time"]), float(r["fundingRate"])) for r in raw]
            nt = int(raw[-1]["time"]) + 1
            if nt <= t:
                break
            t = nt
            time.sleep(0.3)  # the info endpoint rate-limits long paginations
        return out


def cache_candles(path: Path, candles: list[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(c.__dict__) for c in candles))


def load_candles(path: Path) -> list[Candle]:
    return [Candle(**json.loads(l)) for l in path.read_text().splitlines() if l.strip()]
