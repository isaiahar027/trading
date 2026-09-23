"""Macro market regime from primary data: BTC/ETH trend, dominance, stablecoin liquidity,
funding, open interest and sentiment. Deterministic rules; the output is a label plus the
evidence behind it, so a human (or the brain) can disagree with specific inputs.
"""
from __future__ import annotations

import time

from ..data.hyperliquid import HyperliquidData
from ..state_engine import ema
from .sources import DL_STABLES, FNG, PAPRIKA_GLOBAL, Sourced, fetch, now_iso

DAY = 86_400_000


def trend_report(hl: HyperliquidData, coin: str) -> dict:
    end = int(time.time() * 1000)
    c = hl.candles(coin, "1d", end - 400 * DAY, end)
    closes = [x.c for x in c if x.t_close < end]
    px = closes[-1]
    e50, e200 = ema(closes[-200:], 50), ema(closes[-200:], 200)
    return {
        "coin": coin, "close": px, "ema50": round(e50, 2), "ema200": round(e200, 2),
        "ret_30d": round(px / closes[-31] - 1, 4), "ret_90d": round(px / closes[-91] - 1, 4),
        "above_200": px > e200, "golden": e50 > e200, "bars": len(closes),
        "source": "https://api.hyperliquid.xyz/info candleSnapshot 1d", "fetched_at": now_iso(),
    }


def stablecoin_liquidity() -> dict:
    s: Sourced = fetch(DL_STABLES)
    rows = s.value
    def usd(r):
        return float(r["totalCirculatingUSD"].get("peggedUSD", 0))
    now, d30, d90 = usd(rows[-1]), usd(rows[-31]), usd(rows[-91])
    return {"total_usd": round(now), "chg_30d": round(now / d30 - 1, 4), "chg_90d": round(now / d90 - 1, 4),
            "source": s.url, "fetched_at": s.fetched_at}


def dominance() -> dict:
    s = fetch(PAPRIKA_GLOBAL)
    return {"btc_dominance_pct": s.value["bitcoin_dominance_percentage"],
            "total_mcap_usd": s.value["market_cap_usd"], "mcap_chg_24h_pct": s.value["market_cap_change_24h"],
            "source": s.url, "fetched_at": s.fetched_at}


def sentiment() -> dict:
    s = fetch(FNG)
    vals = [int(x["value"]) for x in s.value["data"]]
    return {"fear_greed": vals[0], "fear_greed_30d_avg": round(sum(vals) / len(vals), 1),
            "label": s.value["data"][0]["value_classification"], "source": s.url, "fetched_at": s.fetched_at}


def derivatives(hl: HyperliquidData) -> dict:
    ctxs = hl.contexts()
    out = {}
    for coin in ("BTC", "ETH", "SOL"):
        c = ctxs[coin]
        out[coin] = {"funding_apr_pct": round(c.funding * 24 * 365 * 100, 2),
                     "open_interest_usd": round(c.open_interest * c.mark),
                     "day_volume_usd": round(c.day_volume_usd), "premium_bps": round(c.premium * 1e4, 2)}
    total_oi = sum(c.open_interest * c.mark for c in ctxs.values())
    return {"majors": out, "hl_total_oi_usd": round(total_oi),
            "source": "https://api.hyperliquid.xyz/info metaAndAssetCtxs", "fetched_at": now_iso()}


def classify(btc: dict, eth: dict, stables: dict, deriv: dict, fng: dict) -> tuple[str, list[str]]:
    """risk_on | neutral | risk_off | capitulation, with the evidence that drove it."""
    score, why = 0, []
    for t in (btc, eth):
        if t["above_200"]:
            score += 1; why.append(f"{t['coin']} above 200d EMA")
        else:
            score -= 1; why.append(f"{t['coin']} below 200d EMA")
        if t["ret_30d"] < -0.15:
            score -= 1; why.append(f"{t['coin']} 30d {t['ret_30d']:+.0%}")
    if stables["chg_30d"] > 0.01:
        score += 1; why.append(f"stablecoin supply growing {stables['chg_30d']:+.1%} 30d")
    elif stables["chg_30d"] < -0.01:
        score -= 1; why.append(f"stablecoin supply shrinking {stables['chg_30d']:+.1%} 30d")
    btc_f = deriv["majors"]["BTC"]["funding_apr_pct"]
    if btc_f > 30:
        score -= 1; why.append(f"BTC funding crowded long ({btc_f:.0f}% APR)")
    if fng["fear_greed"] <= 15:
        why.append(f"extreme fear ({fng['fear_greed']})")
        if btc["ret_30d"] < -0.2:
            return "capitulation", why
    label = "risk_on" if score >= 2 else "risk_off" if score <= -2 else "neutral"
    return label, why


def market_regime(hl: HyperliquidData | None = None) -> dict:
    hl = hl or HyperliquidData()
    btc, eth = trend_report(hl, "BTC"), trend_report(hl, "ETH")
    stables, dom, fng, deriv = stablecoin_liquidity(), dominance(), sentiment(), derivatives(hl)
    label, why = classify(btc, eth, stables, deriv, fng)
    return {"label": label, "evidence": why, "btc": btc, "eth": eth, "stablecoins": stables,
            "dominance": dom, "sentiment": fng, "derivatives": deriv,
            "macro": "NOT FETCHED: rates, DXY, and the macro calendar need an operator or brain read with dated sources"}
