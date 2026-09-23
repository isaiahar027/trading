"""Primary-source fetchers for the research layer. Every value carries its URL and fetch time.

Sources (all public, no key):
  DefiLlama   https://api.llama.fi, https://stablecoins.llama.fi  (fees, revenue, holders revenue, TVL, mcap, stables)
  Hyperliquid https://api.hyperliquid.xyz/info                     (perp funding, OI, volume, candles)
  CoinPaprika https://api.coinpaprika.com/v1/global, /v1/tickers   (total mcap, BTC dominance, token mcap + supply)
  alternative.me https://api.alternative.me/fng/                   (Fear & Greed index)
Not covered by free primary APIs, and therefore NOT fabricated: token unlock schedules,
holder concentration, active users. The report marks these as operator-to-verify.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

UA = {"User-Agent": "trader-research/0.1"}


@dataclass(frozen=True)
class Sourced:
    value: object
    url: str
    fetched_at: str


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_json(url: str, timeout: float = 30, tries: int = 4):
    for i in range(tries):
        try:
            r = requests.get(url, timeout=timeout, headers=UA)
            if r.status_code == 200:
                return r.json()
            if r.status_code not in (429, 500, 502, 503, 504):
                r.raise_for_status()
        except requests.RequestException:
            if i == tries - 1:
                raise
        time.sleep(2 ** i)
    raise RuntimeError(f"failed: {url}")


def fetch(url: str) -> Sourced:
    return Sourced(get_json(url), url, now_iso())


DL_PROTOCOLS = "https://api.llama.fi/protocols"
DL_FEES = "https://api.llama.fi/overview/fees?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"
DL_REVENUE = DL_FEES + "&dataType=dailyRevenue"
DL_HOLDERS = DL_FEES + "&dataType=dailyHoldersRevenue"
DL_STABLES = "https://stablecoins.llama.fi/stablecoincharts/all"
PAPRIKA_GLOBAL = "https://api.coinpaprika.com/v1/global"
FNG = "https://api.alternative.me/fng/?limit=30"
PAPRIKA_TICKERS = "https://api.coinpaprika.com/v1/tickers"
