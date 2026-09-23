"""Fundamental screen for asymmetric setups: tokens whose price looks disconnected from
cash flows (fees/revenue), where value demonstrably accrues to holders, and which are
liquid enough on Hyperliquid perps to trade in either direction.

This is a CANDIDATE generator. It computes ratios from primary data and nothing else. The
thesis, catalysts and bear case for each finalist are written by a human or the brain and
must cite dated sources (see docs/RESEARCH.md).
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median

from ..data.hyperliquid import HyperliquidData
from .sources import DL_FEES, DL_HOLDERS, DL_PROTOCOLS, DL_REVENUE, PAPRIKA_TICKERS, fetch, now_iso

MIN_MCAP = 100e6
MIN_HL_VOLUME = 5e6
# Fee streams that belong to a chain or a side product, not to the token's own protocol economics.
EXCLUDED_CATEGORIES = {"Bridge", "Canonical Bridge", "Chain", "Launchpad", "Onchain Voting", "Liquid Staking"}


def _by_parent(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(lambda: {"total30d": 0.0, "total60dto30d": 0.0, "names": []})
    for p in rows:
        key = p.get("parentProtocol") or f"id#{p.get('defillamaId') or p.get('id')}"
        agg = out[key]
        agg["total30d"] += p.get("total30d") or 0.0
        agg["total60dto30d"] += p.get("total60dto30d") or 0.0
        agg["names"].append(p.get("name"))
        if (p.get("total30d") or 0) >= agg.get("top_fees", -1):
            agg["top_fees"], agg["category"] = p.get("total30d") or 0, p.get("category")
    return out


def screen(hl: HyperliquidData | None = None, top: int = 15) -> dict:
    hl = hl or HyperliquidData()
    protocols, fees, rev, holders, tickers = (fetch(u) for u in (DL_PROTOCOLS, DL_FEES, DL_REVENUE, DL_HOLDERS,
                                                                 PAPRIKA_TICKERS))
    # best-ranked ticker per symbol (symbols collide; the larger asset is almost always the intended one)
    market: dict[str, dict] = {}
    for t in sorted(tickers.value, key=lambda t: t.get("rank") or 10**9):
        market.setdefault(t["symbol"].upper(), t)
    fees_p, rev_p, hold_p = (_by_parent(s.value["protocols"]) for s in (fees, rev, holders))

    # token-level identity: symbol, mcap, tvl, category, keyed like the fees data
    tokens: dict[str, dict] = {}
    for p in protocols.value:
        key = p.get("parentProtocol") or f"id#{p.get('id')}"
        sym, mcap = (p.get("symbol") or "").upper(), p.get("mcap") or 0
        if not sym or sym == "-":
            continue
        t = tokens.setdefault(key, {"symbol": sym, "name": p.get("name"), "category": p.get("category"),
                                    "mcap": 0.0, "tvl": 0.0, "slug": p.get("slug")})
        t["mcap"] = max(t["mcap"], mcap)
        t["tvl"] += p.get("tvl") or 0.0
    ctxs = hl.contexts()

    rows = []
    for key, t in tokens.items():
        mk = market.get(t["symbol"])
        if mk:
            q = mk["quotes"]["USD"]
            t["mcap"] = q.get("market_cap") or t["mcap"]
            circ = (q.get("market_cap") or 0) / q["price"] if q.get("price") else 0
            total = mk.get("max_supply") or mk.get("total_supply") or 0
            t["circ_ratio"] = round(min(circ / total, 1.0), 3) if total and circ else None
            t["ret_7d"] = q.get("percent_change_7d")
        if t["mcap"] < MIN_MCAP or key not in fees_p:
            continue
        hl_ctx = ctxs.get(t["symbol"]) or ctxs.get("k" + t["symbol"])
        if hl_ctx is None or hl_ctx.day_volume_usd < MIN_HL_VOLUME:
            continue
        f30 = fees_p[key]["total30d"]
        r30 = rev_p.get(key, {}).get("total30d", 0.0)
        h30 = hold_p.get(key, {}).get("total30d", 0.0)
        prev = fees_p[key]["total60dto30d"]
        if f30 <= 0:
            continue
        rows.append({
            "symbol": t["symbol"], "name": t["name"], "category": fees_p[key].get("category") or t["category"], "mcap": round(t["mcap"]),
            "tvl": round(t["tvl"]), "fees_30d": round(f30), "revenue_30d": round(r30), "holders_rev_30d": round(h30),
            "p_f": round(t["mcap"] / (f30 * 12.17), 2),
            "p_s": round(t["mcap"] / (r30 * 12.17), 2) if r30 > 0 else None,
            "p_holders": round(t["mcap"] / (h30 * 12.17), 2) if h30 > 0 else None,
            "accrual": round(h30 / f30, 3),                      # share of fees reaching token holders
            "fees_growth_30d": round(f30 / prev - 1, 3) if prev > 0 else None,
            "mcap_tvl": round(t["mcap"] / t["tvl"], 2) if t["tvl"] > 0 else None,
            "circ_ratio": t.get("circ_ratio"),                  # circulating / max supply: <0.5 = large overhang
            "ret_7d_pct": t.get("ret_7d"),
            "hl_volume_24h": round(hl_ctx.day_volume_usd),
            "hl_funding_apr_pct": round(hl_ctx.funding * 24 * 365 * 100, 2),
            "hl_oi_usd": round(hl_ctx.open_interest * hl_ctx.mark),
            "defillama": f"https://defillama.com/protocol/{t['slug']}",
        })

    # One row per token symbol: keep the fee stream that is actually that token's protocol.
    best: dict[str, dict] = {}
    for r in rows:
        if r["category"] in EXCLUDED_CATEGORIES:
            continue
        if r["symbol"] not in best or r["fees_30d"] > best[r["symbol"]]["fees_30d"]:
            best[r["symbol"]] = r
    rows = list(best.values())

    # Score: cheap on holder cash flow vs peers, growing, with real accrual. Ranks, not magic numbers.
    def rank(key, reverse=False):
        vals = sorted((r[key] for r in rows if r[key] is not None), reverse=reverse)
        return {id(r): (vals.index(r[key]) / max(len(vals) - 1, 1) if r[key] is not None else 1.0) for r in rows}
    cheap = rank("p_f")
    growth = rank("fees_growth_30d", reverse=True)
    for r in rows:
        accrual_pen = 0.0 if r["accrual"] >= 0.2 else 0.5
        overhang_pen = 0.5 if (r["circ_ratio"] or 1) < 0.5 else 0.0
        r["score"] = round(1 - (0.4 * cheap[id(r)] + 0.3 * growth[id(r)] + 0.2 * accrual_pen + 0.1 * overhang_pen), 3)
    rows.sort(key=lambda r: -r["score"])
    med_pf = median([r["p_f"] for r in rows]) if rows else None
    return {
        "fetched_at": now_iso(),
        "sources": [DL_PROTOCOLS, DL_FEES, DL_REVENUE, DL_HOLDERS, PAPRIKA_TICKERS, "https://api.hyperliquid.xyz/info metaAndAssetCtxs"],
        "universe_size": len(rows), "median_p_f": med_pf,
        "filters": {"min_mcap": MIN_MCAP, "min_hl_volume_24h": MIN_HL_VOLUME, "fees_30d": ">0"},
        "candidates": rows[:top],
        "not_covered": ["token unlock calendar (circ_ratio is only a proxy)", "holder concentration", "active users"],
    }
