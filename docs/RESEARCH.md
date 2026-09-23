# Research · as of 2026-09-23

**How to read this:**
- **Screen numbers** come from `trader research`, which pulls primary APIs: DefiLlama, Hyperliquid, CoinPaprika and alternative.me. Every value has a URL and fetch time; see [`research/snapshots/2026-09-23.json`](../research/snapshots/2026-09-23.json).
- **Catalysts, unlocks and bear cases** come from web research. **Most of those sources are secondary** (news sites, CoinMarketCap/CoinStats AI summaries, unlock trackers) and were **not verified on-chain**.
- **Labels:** `(derived)` marks a source's own calculation, `UNVERIFIED` marks anything unconfirmed, and `(est.)` marks estimates.
- **Nothing here is invented.** Where no source was found, the text says so.

## 1 · Market regime

| Input | Value | Source (fetched 2026-09-23) |
|---|---|---|
| BTC / ETH vs 200d EMA | both above | Hyperliquid `candleSnapshot` 1d |
| Stablecoin supply | $311.7B, **+1.1% 30d**, −0.1% 90d | stablecoins.llama.fi/stablecoincharts/all |
| BTC dominance | 56.6% | api.coinpaprika.com/v1/global |
| Total crypto mcap | $3.00T, −2.3% 24h | api.coinpaprika.com/v1/global |
| Fear & Greed | 71 "Greed" (30d avg 66.6) | api.alternative.me/fng |
| BTC / ETH / SOL funding | ~11% APR each (the Hyperliquid baseline, so not crowded) | Hyperliquid `metaAndAssetCtxs` |
| BTC perp OI (Hyperliquid) | $3.27B | Hyperliquid `metaAndAssetCtxs` |

**Rule-based label: `risk_on`** (BTC and ETH above their 200d EMA, stablecoin supply growing).

**The macro backdrop argues against leaning into it:**
- **Rate hike:** the Fed raised 25bp to 3.75–4.00% on 2026-09-16, its first hike since 2023. PCE was ~3.7% and Brent above $91 ([cryptotimes 09-17](https://www.cryptotimes.io/2026/09/17/spot-bitcoin-etfs-see-746m-outflow-during-clarity-act-and-fomc-pressure/), [crypto.news 09-03](https://crypto.news/fed-rate-hike-september-crypto-thesis-etf-demand/)).
- **Next FOMC:** Oct 27–28 ([federalreserve.gov](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)). Before the Sept decision, futures priced ~39% odds of an October hike (UNVERIFIED as of today).
- **ETF flows:** spot BTC ETFs lost ~$746M on Sep 15–16 and gained ~$433M on Sep 18. BTC's move to ~$86.6K on Sep 22 was driven by a short squeeze.
- **Regulation:** the CLARITY Act failed cloture 49–50 (Sep 15). The SEC granted a 5-year tokenized-stock "innovation exemption" (Sep 17) ([sec.gov](https://www.sec.gov/newsroom/press-releases/2026-90-sec-issues-innovation-exemption-facilitate-trading-tokenized-nms-stock-request-comment)).

**Net view:** trend is up, but rates are rising and the rally is squeeze-driven into a live FOMC. Size stays at the code defaults; nothing in this regime justifies raising limits.

## 2 · Screen (primary data)
Filters: market cap ≥ $100M, 30d fees > 0, listed on Hyperliquid perps with ≥ $5M 24h volume, chain/bridge/launchpad fee streams excluded.

| Sym | Mcap | P/F | P/holders-rev | Accrual | 30d fee growth | Circ/max | HL 24h vol |
|---|---|---|---|---|---|---|---|
| AERO | $0.67B | 3.1 | 3.9 | 0.81 | +205% | 0.50 | $10M |
| UNI | $5.69B | 2.2 | 29.2 | 0.08 | +134% | 0.62 | $148M |
| PUMP | $1.87B | 0.95 | 6.3 | 0.15 | +38% | 0.47 | $87M |
| ASTER | $1.14B | 14.7 | 20.3 | 0.73 | +31% | 0.21 ⚠ | $10M |
| JUP | $0.95B | 4.0 | 22.0 | 0.18 ⚠ | +30% | 0.33 | $9M |
| HYPE | $20.7B | 21.9 | 28.1 | 0.78 | +40% | 0.22 | $750M |
| ENA | $2.08B | 8.8 | n/a | 0.00 | +28% | 0.67 | $55M |
| AAVE | $2.14B | 4.8 | n/a ⚠ | 0.00 ⚠ | +19% | 0.96 | $31M |

⚠ = the research below contradicts the data feed:
- **AAVE:** DefiLlama shows 0 holders revenue but buybacks are live.
- **JUP:** holders revenue is likely overstated.
- **ASTER:** the fee base is suspect and the supply data conflicts.

The screen also surfaced MET, LIT, ARB and others. MET's symbol match is ambiguous (Meteora vs Metronome), so it was not researched.

## 3 · Finalists (compiled into Jev schemas: `schemas/<SYM>/v001.json`)

Each finalist's thesis and invalidation are passed to Jev as `context` in the `direction` question, **as a tie-breaker only**. Code, not the thesis, decides size.

### PUMP · bias **long**
- **Accrual:**
  - Since 2026-04-29, 50% of net revenue has been locked into buy-and-burn for 12 months.
  - That day, $370M of PUMP (36% of circulating) was burned ([CoinDesk 04-29](https://www.coindesk.com/markets/2026/04/29/pump-fun-burns-36-of-pump-supply-in-usd370-million-wipe-locks-50-revenue-into-ongoing-buybacks)).
  - Buybacks run $1M+ a day; YTD revenue is $322M ([CMC 09-22](https://coinmarketcap.com/cmc-ai/pump-fun/latest-updates/)).
- **Supply:** ~6.875B PUMP (0.69%) unlocks monthly to team and investors on Oct 12, Nov 12 and Dec 12 ([cryptobriefing 08-10](https://cryptobriefing.com/pump-fun-pump-token-unlock-pressure/)). That is roughly $10–16M a month (est.) against ~$30M+ a month of buybacks (est.).
- **Catalysts:**
  - *Confirmed:* monthly unlock on Oct 12; fee split clarified on Sep 22.
  - *Speculative:* multi-chain expansion, a perps integration.
- **Bear case:**
  - Revenue is pure Solana-memecoin beta and can collapse fast.
  - Launchpad competition compresses fees.
  - **RICO claims against the founders survived** the Aug 31 SDNY ruling ([cryptotimes 09-01](https://www.cryptotimes.io/2026/09/01/judge-dismisses-pump-fun-securities-claims-clears-solana-labs-and-foundation/)).
  - The buyback commitment ends around Apr 2027.
- **Invalidation:** buybacks fall well below $1M a day for 2+ weeks; a large insider unlock hits exchanges; an adverse RICO ruling.
- **Priced in?** Not obviously. There was no September re-rating, and past buybacks failed to lift the price, which is itself a warning.

### ENA · bias **short**
- **Accrual:** none live.
  - The fee switch passed on 2026-09-02, but buybacks only start when USDe reaches $7.5B. It is ~$4.1–4.2B today.
  - Burning of bought-back ENA is not confirmed ([Bankless](https://www.bankless.com/read/news/ethena-fee-switch-vote-ties-ena-buybacks-to-usde-growth), [unlocks.app 09-01](https://insights.unlocks.app/ethena-bought-out-its-sellers-and-deleted-the-investor-unlock-calendar-the-buyback-replacing-it-arms-at-7-5b-usde/)).
- **Supply:** a single investor release on **2026-10-05** of ~1.41B ENA, about 14% of circulating (derived, [cryptoticker 09-06](https://cryptoticker.io/en/ethena-ena-investor-unlock/)). After that, the team receives 93.75M a month until Mar 2028.
- **Catalyst:**
  - *Confirmed:* the Oct 5 unlock.
  - *Speculative:* USDe growth; the Aave/Avalanche collateral listing.
- **Bear case against the short:**
  - The Foundation OTC-bought tokens from 14 seed investors (amounts undisclosed), so fewer tokens than feared may be sold.
  - The unlock has been public since late August and may be priced.
  - Squeeze risk in a risk-on tape.
- **Invalidation:** after Oct 5, on-chain data shows little supply moving to exchanges, or USDe climbs quickly toward $6B+ while ENA holds.

### HYPE · bias **neutral**
- **Accrual:** ~97% of fees go to Assistance Fund buybacks, treated as burned since the 2025-12-27 vote. This is the strongest mechanism in the set.
- **Supply:** ~9.92M HYPE (~1%) vests monthly on the 6th (Oct 6, Nov 6, Dec 6), though claims have historically been low ([tokenomist](https://tokenomist.ai/hyperliquid/unlock-events), [cryptoticker 08-27](https://cryptoticker.io/en/hyperliquid-hype-unlock-dilution/)).
- **Catalysts:**
  - *Confirmed (announced):* Payward/Kraken's plan for regulated US perps via Bitnomial on HIP-3. It needs regulatory approval, so the date is UNVERIFIED ([crypto.news 09-23](https://crypto.news/hyperliquid-open-interest-reaches-record-18-billion-whats-driving-activity/)).
  - *Speculative:* a CFTC onshore path.
- **Bear case:**
  - The most expensive name here (P/F ~22), trading at an ATH (~$95, Sep 21) after a $300M short squeeze.
  - Record OI of $18B means leverage-driven fees that reverse in a deleveraging.
- **Invalidation:** 30d fees fall more than 30% with OI below $12B; heavy claims on the Oct 6 unlock that move to exchanges.
- **Priced in?** Largely (+26% in 7 days).

### UNI · bias **neutral**
- **Accrual:** the fee switch is live on v2, v3 and Unichain via TokenJar burns. v4 is pending. About $118M a year goes to burns (est.) against a $5.7B market cap.
- **Catalysts:**
  - *Confirmed:* SEC exemption (Sep 17); **CME UNI futures on Oct 19**, pending review ([CME 09-22](https://www.cmegroup.com/media-room/press-releases/2026/9/22/cme_group_to_expandcryptoderivativessuitewithbitcoincashandunisw.html)).
  - *Speculative:* a v4 fee-switch vote; a spot ETF (UNVERIFIED).
- **Bear case:**
  - +49% in a week to ~$9.05 ([cryptoticker 09-19](https://cryptoticker.io/en/uniswap-uni-price-jump-fee-switch/)).
  - At ~29x holders revenue, the fee growth coincides with a volatility burst.
  - A classic sell-the-news setup around Oct 19.
- **Invalidation (of any long):** price back below ~$6.7 after the CME launch; a failed v4 vote.

### AERO · bias **neutral**
- **Accrual:** veAERO lockers get 100% of voted-pool fees and bribes, and the Protocol Growth Fund buys and locks AERO ([CMC 09-03](https://coinmarketcap.com/top-stories/6a98e3c8cd14b2494210b9c5/)).
- **Supply:** no cliffs, but emissions run at ~10.9% a year.
- **Catalysts:**
  - *Confirmed:* Protocol 28 vote (Sep 16).
  - *Speculative:* the Aero (Aerodrome+Velodrome) merger launch. It was guided for Q2 2026 and has slipped; the audit contest started Aug 30.
- **Bear case:**
  - The +205% fee jump is tokenized-stock volume on Base ([ethnews 09-08](https://ethnews.com/aerodrome-aero-jumps-20-on-tokenized-stock-volume-surge/)), which the SEC's permissioned-AMM path could route to Uniswap v4.
  - Emissions have been ~1.75x revenue.
  - Price is already +58% since Aug 1.
- **Invalidation:** fees revert to the prior-30d level; the Aero launch slips further.

### AAVE · bias **neutral**
- **Accrual:** Aavenomics 3.0 buybacks have been live since 2026-06-27, at ~$30M a year (cut from $50M), plus ~$58M a year to stakers. **DefiLlama shows 0; that is wrong** ([phemex 07-12](https://phemex.com/blogs/aave-surging-automated-buyback-engine)).
- **Bear case:**
  - The Apr 2026 rsETH/Kelp exploit left $124–230M of potential bad debt ([CoinDesk 04-20](https://www.coindesk.com/tech/2026/04/20/aave-could-face-up-to-usd230-million-in-losses-after-kelp-dao-bridge-exploit-triggers-defi-chaos)).
  - Deposits fell from $48.5B to $30.7B.
  - Buybacks are only ~1–2% of market cap a year.
  - Rate hikes cut leverage demand.
- **Catalysts:** none dated. V4 listings are at the proposal stage.
- **Invalidation:** another collateral exploit; a buyback pause; deposits below the post-exploit lows.

## 4 · Watchlist (not compiled; not traded)
- **JUP:**
  - Buybacks were halted 2026-01-03 ([KuCoin](https://www.kucoin.com/news/flash/jupiter-halts-70m-buyback-plan-amid-persistent-jup-price-decline)), and the Litterbox now adds only ~1M JUP a month. DefiLlama's holders revenue looks overstated.
  - Paused team tokens can be re-enabled by vote.
  - Re-evaluate if buybacks resume on-chain.
- **ASTER:**
  - DefiLlama delisted Aster's volume in Oct 2025 over suspected wash trading ([Yahoo](https://finance.yahoo.com/news/defillama-delist-aster-volume-data-101345708.html)), so fee-based ratios are low-trust.
  - The team unlock was deferred to 2027-09-17.
  - The screen's supply ratio (0.22) conflicts with a report of ~7.9B circulating out of 8B.

## 5 · Refresh procedure
1. `trader research` produces a new snapshot.
2. Diff it against this page: accrual changes, new unlocks, fee trend reversals.
3. Edit `research/finalists.json`, then run `trader compile --force`. This creates new schema versions, which take effect on restart.
4. Re-check every invalidation line above. **If one has triggered, set that symbol's bias to neutral or remove it. Do not wait for the stop.**
