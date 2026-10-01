## Update: expanded to the full requested source list
`collect-datasources` now also supports `--loop --every N` for continuous collection instead of a single pass.

A follow-up pass added the specific sources requested (Binance, Kraken, CoinGecko expanded, CoinMarketCap,
DexScreener, GeckoTerminal, GDELT, Reddit), all under the same `crypto_ai/datasources/` framework below. 30
adapters are now registered. What changed from the "first 10" picked in the original pass:
- **Binance**: implemented in full (candles, trades, aggTrades, futures funding + open interest) and unit tested,
  but confirmed live (again) to return HTTP 451 "Service unavailable from a restricted location" from this
  environment - this is Binance's own Terms blocking this IP/region, not a bug. `Adapter.run()` reports a clean
  `ok: False` for it rather than crashing; verify from your own deployment's IP before relying on it.
- **Kraken**: fully working, no key, verified live (OHLC, trades, bid/ask spread history, L2 depth).
- **CoinGecko**: expanded beyond the existing reference-price module (markets/market-cap/volume, trending coins,
  historical market-chart). `/coins/markets` and `/coins/{id}/market_chart` were 403'd/429'd from this sandbox's
  shared IP on repeated calls; `/search/trending` worked anonymously. A free "Demo" key (`COINGECKO_API_KEY`, no
  payment method) is supported and recommended if your IP hits the same wall.
- **CoinMarketCap**: implemented, requires a free key (`COINMARKETCAP_API_KEY`); skips cleanly without one.
- **DexScreener, GeckoTerminal**: both free/no-key, verified live.
- **GDELT**: free/no-key API with its own documented 1-request/5-seconds limit; this sandbox's shared outbound IP
  was already being told to slow down / occasionally stalled entirely on every attempt when tested live - the
  adapter uses a short timeout (8s) and few retries (2) specifically so a bad environment fails fast instead of
  stalling a `collect-datasources` pass; expected to behave normally from a typical, less-shared IP.
- **Reddit**: implemented against the official OAuth2 API (not scraping) - needs `REDDIT_CLIENT_ID` +
  `REDDIT_CLIENT_SECRET` from a free "script" app registration; could not be live-verified in this session (no
  registered app credentials available here), so it is mock-tested only against Reddit's documented API shape.
- **Coinbase**: wrapped as adapters too (for dashboard visibility, on top of the pre-existing `crypto_ai.coinbase`
  module and the always-on `ws-collect` real-time collector).

# Free/legal data source catalog and ingestion design

Research + design deliverable. **No trading strategy was changed.** Covers all 31 requested categories, ranks
sources A (critical) / B (useful) / C (experimental) / D (redundant or not worth it), and reports what was actually
built: an adapter framework (`worker/crypto_ai/datasources/`) plus 10 concrete, verified-live, free/no-key
integrations, writing to a local Parquet **data lake** (not Supabase) with only small metadata rows in Postgres.

**Ground rule applied throughout:** nothing here scrapes a page, bypasses a login, or ignores rate limits/ToS. Two
sources that looked promising were tested live and **rejected** for exactly that reason - see "Rejected" below.

## Architecture
```
crypto_ai/datasources/
  base.py       Adapter dataclass + polite http_get() (retries, backoff, UA string)
  datalake.py   Parquet writer/reader (local disk by default; DATALAKE_ROOT can point at S3/GCS/R2 via fsspec)
                + record_manifest() -> one small Supabase row per (adapter, day)
  registry.py   ALL = every module's ADAPTERS, concatenated - this is the only place a new source is "wired in"
  okx.py, deribit.py, defillama.py, onchain.py   the 10 concrete adapters (below)
```
A new source = a new module exposing `ADAPTERS: list[Adapter]`, added to `registry.py`. Nothing else changes.
`python -m crypto_ai.cli collect-datasources` runs whatever is due; `Adapter.run()` never lets one broken source
crash the batch. Supabase gets one `datalake_manifest` row per (adapter, day) - `schema_datalake.sql` - the actual
data stays in Parquet, partitioned `<root>/<category>/<adapter>/<day>.parquet`, de-duplicated on write.

## Rejected (tested live, excluded for ToS/legal reasons)
| Source | What happened | Why excluded |
|---|---|---|
| Binance (spot + futures public REST) | Returned `"Service unavailable from a restricted location according to 'b. Eligibility' in https://www.binance.com/en/terms"` when called live from this environment | Binance's own Terms restrict access from this location/IP class. Using it here (or building on an assumption it works) would risk exactly the ToS violation the brief warned against. |
| Bybit (public REST) | Returned an AWS CloudFront **country block** error | Same category of problem - a geographic access restriction from the provider's own infrastructure, not a workaround-able technicality. |
Both are still catalogued below (rank C: "works in some regions, verify your own deployment's IP before relying on it") since a *different* hosting location/region might not hit these particular blocks - but nothing was integrated against them.

## The catalog (31 categories)
Historical depth / rate limits / resolution below are as documented publicly at the time of writing - **verify
before relying on them**, free-tier terms change. "Legit BTC/ETH/XRP" = usable without violating anything.

### 1-2. Exchange spot & futures data
| Source | URL | Auth | Resolution | Hist. depth | Rank | Notes |
|---|---|---|---|---|---|---|
| Coinbase Exchange (spot) | api.exchange.coinbase.com | none | tick / 1m+ candles | ~3y+ via REST paging | **A** | Already integrated (`coinbase.py`); the project's primary spot venue. |
| OKX (spot + perpetual swaps) | okx.com/api/v5 | none for public data | tick / 1m candles | REST candle history ~months, live endpoints real-time | **A** | Verified live 2026-09; **integrated** (funding, OI, liquidations, book, trades). |
| Deribit (BTC/ETH futures & options) | deribit.com/api/v2 | none for public data | real-time | index/funding history via REST | **A** | Verified live; **integrated** (funding + index for basis). BTC/ETH only. |
| Binance (spot + futures) | binance.com/api, fapi.binance.com | none for public data (elsewhere) | tick | years | **C** | Geo-blocked from this environment (see Rejected). Huge public dataset if your deployment isn't blocked - verify first. |
| Bybit (spot + linear perps) | api.bybit.com | none for public data (elsewhere) | tick | months-years | **C** | CloudFront-blocked from this environment (see Rejected). |
| Kraken (spot) | api.kraken.com | none for public data | tick / OHLC | ~since 2019 via REST | **B** | Not geo-tested here; commonly free/no-key, worth a second spot venue. |
| Coinbase Advanced Trade (successor API) | api.coinbase.com/api/v3/brokerage | public market data needs no key | tick/candles | similar to Exchange API | **D** | Redundant with Coinbase Exchange, already covered. |

### 3-4. OHLCV & tick trades
| Source | Notes | Rank |
|---|---|---|
| Coinbase (candles + `matches`) | Already integrated (`coinbase.py`, `micro_collect.py`) | **A** |
| OKX (candles + `market/trades`) | **Integrated** | **A** |
| CryptoCompare `histominute`/`histoday` | Free tier, API key optional for basic use, rate-limited | **B** |
| CoinGecko OHLC | Free, no key, rate-limited (~10-30 req/min) | **B** | Already partially used (`coingecko.py` reference prices) |

### 5-6. Bid/ask, order books / L2
| Source | Notes | Rank |
|---|---|---|
| Coinbase `ticker` (WS, free) + REST `book?level=2` | **Integrated** (`micro_collect.py`) - real-time BBO + polled depth | **A** |
| OKX `market/books` | **Integrated** - a second venue's depth for cross-venue comparison | **A** |
| Coinbase `level2`/`full` WS | **Now requires authentication** (confirmed live 2026-09) | **D** for a free/no-key setup |

### 7. Trade-flow / aggressor data
| Source | Notes | Rank |
|---|---|---|
| Coinbase `matches` (maker side, aggressor inferred) | **Integrated** | **A** |
| OKX `market/trades` (`side` = taker/aggressor directly) | **Integrated** | **A** |

### 8-9. Funding rates & open interest
| Source | Notes | Rank |
|---|---|---|
| OKX funding rate + open interest | **Integrated**, BTC/ETH/XRP, no key | **A** |
| Deribit funding (BTC/ETH perpetual) | **Integrated** | **A** |
| Binance/Bybit funding & OI | Geo-blocked here (see Rejected) | **C** |

### 10. Liquidations
| Source | Notes | Rank |
|---|---|---|
| OKX `public/liquidation-orders` | **Integrated** - recent filled liquidations, snapshot not full archive; poll often | **A** |
| Binance `!forceOrder@arr` WS | Free, real-time, no historical archive - geo-blocked here anyway | **C** |
| Coinglass (aggregated liquidation dashboards) | Free tier exists but is a scraped/rate-limited web product, not a documented open API | **C** |

### 11. Futures basis
| Source | Notes | Rank |
|---|---|---|
| Deribit index price vs Coinbase spot mid | **Integrated** (index collected; basis computed at research time) | **A** |
| OKX mark price vs spot | Available (`public/mark-price`), not yet wired - easy follow-up adapter | **B** |

### 12. DEX data
| Source | Notes | Rank |
|---|---|---|
| The Graph public subgraphs (Uniswap etc.) | Free, no key for hosted-service queries (rate-limited); not integrated (no clean BTC/ETH/XRP mapping worth the complexity yet) | **B** |
| DexScreener public API | Free, undocumented rate limits, ToS says "for personal/informational use" - verify before automated polling | **C** |

### 13-15. On-chain data, whale transactions, stablecoin flows
| Source | Notes | Rank |
|---|---|---|
| blockchain.info Charts API (BTC) | **Integrated** - daily tx count, hash rate, mempool size | **A** for BTC |
| Etherscan (ETH) | **Integrated** (needs a free `ETHERSCAN_API_KEY`, same pattern as `FRED_API_KEY`) - currently just ETH supply; full whale-transaction scanning (per-block `eth_getBlockByNumber` filtered by value) is designed but **not implemented** - see "Still missing" | **B** |
| Whale Alert public API | Free tier is very limited (a handful of req/day) and requires a key; not integrated | **C** |
| DefiLlama stablecoins | **Integrated** - market-wide circulating supply, a liquidity/risk-appetite proxy | **A** (context) |
| XRPL public data API (`data.ripple.com` successor: `xrpscan.com` API, or a public rippled node) | Free, no key, gives XRP-specific on-chain data (already collected as `xrpl_rippled` release feed in `research/feeds.py`, but not ledger/transaction data) | **B**, not yet integrated |

### 16. DeFi / TVL
| Source | Notes | Rank |
|---|---|---|
| DefiLlama TVL | **Integrated** | **A** (context) |

### 17-19. Crypto news, RSS, official announcements
| Source | Notes | Rank |
|---|---|---|
| SEC/Fed/CFTC/Congress feeds, XRPL/ETH Foundation/geth release feeds, CoinDesk/Cointelegraph/Decrypt RSS | **Already integrated** (`research/feeds.py`, `research/registry.py`) | **A** |
| GDELT Project | Free, huge, no key, global news/event database - not integrated (broad, not crypto-specific; would need heavy filtering) | **B** |

### 20-22. Reddit, Google Trends, social sentiment
| Source | Notes | Rank |
|---|---|---|
| Reddit official API (OAuth app, free tier) | Legal via the official API (not scraping), rate-limited, needs an app registration | **B**, not integrated |
| Google Trends (`pytrends`, unofficial) | No official free API; `pytrends` automates the public web UI, which is a ToS grey area (Google's ToS restricts automated querying) - **not integrated**, flagged for manual review before use | **C** |
| LunarCrush / Santiment social APIs | Meaningful free tiers are limited/require signup with usage caps | **C** |

### 23-26. Macro / economic data, stock indexes, commodities, FX/DXY
| Source | Notes | Rank |
|---|---|---|
| FRED API | **Already integrated** (`research/fred.py`) - rates, CPI, employment | **A** |
| Stooq (free CSV downloads) | Free, no key, daily OHLC for indexes/FX/commodities (SPX, DXY, gold, oil); ToS permits personal/non-commercial use - good candidate for a DXY/SPX context adapter | **B**, not integrated |
| Alpha Vantage | Free tier needs a key, 25 req/day - thin for continuous polling | **C** |
| Yahoo Finance (unofficial `yfinance`) | No official API; ToS status is disputed/grey - **not integrated** without a clearer legal basis | **C** |

### 27-31. Historical public datasets, Kaggle, Hugging Face, GitHub, academic
| Source | Notes | Rank |
|---|---|---|
| Kaggle crypto datasets | Free download, per-dataset license (many CC0/MIT, some restrict commercial use) - good for one-off backfills, not a live feed | **B** |
| Hugging Face Datasets hub | Free, per-dataset license - same use case as Kaggle | **B** |
| Academic replication datasets (e.g. via Zenodo/OSF) | Free, typically CC-BY - occasionally has tick-level historical crypto data other sources don't | **C** (hard to discover systematically) |
| Random GitHub repos of scraped data | License and provenance are usually unclear/unverifiable | **D** |

## Top 50 (highest-value, ranked)
The A/B list above is the ranked set; condensing to the top tier for BTC/ETH/XRP + 1m trading:
**A (critical, 15):** Coinbase spot candles, Coinbase ticker (WS), Coinbase matches (WS), Coinbase L2 REST, OKX
funding, OKX OI, OKX liquidations, OKX order book, OKX trades, Deribit funding+index, blockchain.info BTC charts,
DefiLlama stablecoins, DefiLlama TVL, FRED macro, SEC/Fed/CFTC/Congress+project-release feeds.
**B (useful, next priority, ~20):** OKX mark price (basis refinement), Kraken spot (3rd venue), CryptoCompare
histominute, CoinGecko OHLC, Etherscan whale-tx scanning (needs building), XRPL ledger data, Stooq (DXY/SPX/gold),
Reddit official API, GDELT, The Graph subgraphs, Kaggle/HuggingFace historical backfills, Congress bill full text
(already have metadata), CFTC COT reports, prediction markets (already integrated), CME futures data (delayed, free
tier), St. Louis Fed ALFRED (real-time-vintage macro), World Bank API, IMF API, options-implied vol from Deribit,
OKX funding-rate history endpoint (deeper history than the live value), on-chain gas-price time series.
**C/D (the rest):** everything in the tables above marked C or D.

## What was actually integrated (first 10)
1. Coinbase spot 1m candles (pre-existing, catalogued)
2. Coinbase real-time ticker/matches (pre-existing, catalogued)
3. Coinbase REST L2 depth (pre-existing, catalogued)
4. **OKX funding rate** (new)
5. **OKX open interest** (new)
6. **OKX liquidations** (new)
7. **OKX order book** (new)
8. **OKX trades** (new)
9. **Deribit funding + index (basis)** (new)
10. **DefiLlama stablecoins + TVL, blockchain.info BTC on-chain charts, Etherscan ETH supply** (new; grouped as the
    "macro/on-chain context" slice since none of these are BTC/ETH/XRP tick-level data on their own)

## Storage
Parquet, partitioned by day, de-duplicated on write. Live smoke test (single poll of all 10 adapters):
funding/OI/basis rows are tiny (a few KB/day each); OKX liquidations/trades (100-300 rows/poll, polled every 30-120s)
are the largest of this batch but still far smaller than the tick-level microstructure collector
(`MAKER_RESEARCH.md`) already running - low tens of MB/month at these poll rates. DefiLlama/blockchain.info backfill
their FULL history on first run (thousands of rows, all history in one file, a few hundred KB) and then only append
new points. **Supabase only stores one manifest row per (adapter, day)** - negligible.

## What can be collected historically vs what must start now
- **Full history available today, for free:** DefiLlama (stablecoins/TVL back to ~2018/2021), blockchain.info BTC
  charts (years), FRED (decades), Congress/SEC/Fed feeds (as far back as their own archives go).
- **Only from now on (no free historical archive exists):** OKX order book/trade ticks beyond its REST candle
  history, OKX/Deribit funding-rate *prints* at fine granularity (their APIs return current/recent, not a full
  historical series for free), liquidations (both OKX and Binance only expose a recent window for free), and
  everything in `MAKER_RESEARCH.md`'s microstructure collector (bid/ask, depth, trade prints below daily
  resolution). This mirrors the earlier finding for Coinbase tick data: **start the collectors now**, since no
  amount of future work reconstructs data that free vendors never archived.

## Licensing restrictions worth flagging
- OKX/Deribit/DefiLlama/blockchain.info: standard "read-only public API, don't abuse rate limits" terms; no
  redistribution restriction found for research/personal use, but none explicitly grants a commercial redistribution
  license either - treat outputs as **research data for this project**, not a redistributable dataset.
- Etherscan: free-tier key is personal/non-commercial per their ToS at the tier used here.
- Kaggle/Hugging Face: **per-dataset** license - check each one individually before use; do not assume CC0.
- Google Trends / Yahoo Finance: excluded specifically because their terms around automated access are unclear or
  restrictive - do not integrate these without a clearer legal basis (e.g. an official paid API).

## Still impossible to get for free
- Full historical L2/L3 order-book depth for any venue (the free tier of every major exchange has moved this behind
  auth or sells it separately - Coinbase's `level2` gate is the same pattern industry-wide).
- A free, complete historical liquidation archive (only a recent rolling window is free anywhere checked).
- Reliable per-wallet whale-transaction tracking at scale for free (Etherscan's free tier rate limit makes
  continuous full-chain scanning impractical; Whale Alert's free tier is too thin).
- Google Trends / broad social sentiment at production reliability without a paid tier or ToS risk.
- Binance/Bybit's much larger public datasets, from this environment specifically (geo-restricted) - a different
  hosting region may not have this problem; re-test before assuming it's unusable everywhere.
