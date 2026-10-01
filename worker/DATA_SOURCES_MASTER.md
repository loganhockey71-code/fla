# Master free-data-source map for Crypto AI Lab (research only — no code, no trading change)

Covers all 8 requested categories for BTC/ETH/XRP. **Nothing here claims a factor predicts price** — this is a
collection map; whether any of it carries real information is a separate, later, out-of-sample question (see
`EDGE_RESEARCH.md`/`MAKER_RESEARCH.md` for how that testing is actually done in this project). Sources already
implemented in `worker/crypto_ai/` are marked **(integrated)**. Live-verified in this session are marked ✅; a small
number could not be checked live from this sandbox (blocked/needs credentials I don't have) and are marked ⚠️ with
the reason — treat those as "should work, verify from your own deployment," not as confirmed.

Labels used throughout: **FREE** (no key, no cost) · **FREE WITH LIMITS** (rate/volume capped but usable) ·
**KEY REQUIRED** (free key/signup, no payment method) · **PAID** (no usable free tier) · **UNVERIFIED** (couldn't
confirm live, or terms are unclear).

---
## 1. Crypto market data

| Data | Best free source | Backup | Type | Key? | Historical | Real-time | BTC/ETH/XRP | Quality | Notes |
|---|---|---|---|---|---|---|---|---|---|
| Price/OHLCV | Coinbase Exchange **(integrated)** ✅ | OKX **(integrated)** ✅, Kraken **(integrated)** ✅, Binance **(integrated, code only)** ⚠️ geo-451 here, KuCoin ✅, Gate.io ✅, MEXC ✅, Bitget ✅ | REST | NO KEY | ~3y+ (Coinbase), months-years others | yes | all 3 | high | docs: `docs.cdp.coinbase.com/exchange`, `okx.com/docs-v5`, `docs.kraken.com/api` |
| Momentum/returns | Derived from the above | — | computed | — | same as above | yes | all 3 | high | not a separate source - computed in `scalp/features.py` |
| Volume | Same exchange APIs above | CoinGecko/CoinPaprika (aggregated) | REST | NO KEY (CoinPaprika ✅), FREE WITH LIMITS (CoinGecko, 403/429 seen from this sandbox) | months-years | yes | all 3 | high | |
| Volatility | Derived (ATR/realised vol from OHLCV) | Deribit implied vol (BTC/ETH options) **(integrated: index only)** ✅ | REST | NO KEY | live-only for computed; Deribit has history via REST | yes | BTC/ETH (implied); all 3 (realised) | high | `docs.deribit.com` |
| BTC dominance, ETH/BTC ratio | CoinGecko `/global` | CoinPaprika `/global` ✅ | REST | NO KEY | point-in-time only (no free history endpoint found) | yes | n/a (market-wide) | medium | `api.coingecko.com/api/v3/global`, `api.coinpaprika.com/v1/global` |
| Funding rates | OKX **(integrated)** ✅ | Deribit (BTC/ETH) **(integrated)** ✅, Binance (code ready) ⚠️ | REST | NO KEY | recent window (no full free archive) | yes | BTC/ETH/XRP (OKX); BTC/ETH (Deribit) | high | |
| Open interest | OKX **(integrated)** ✅ | Binance (code ready) ⚠️ | REST | NO KEY | recent window | yes | BTC/ETH/XRP | high | |
| Liquidations | OKX **(integrated)** ✅ | Binance `!forceOrder` WS (code ready) ⚠️ | REST/WS | NO KEY | recent window only, everywhere | yes | BTC/ETH/XRP | medium (no free archive) | |
| Exchange inflows/outflows | **No good free API found** | Approximate via manual tracking of known exchange wallets on block explorers | manual | — | n/a | n/a | possible for BTC/ETH; hard for XRP | low (labour-intensive) | CryptoQuant/Glassnode/Nansen do this well but are PAID |
| Spot/futures volume | Same exchange APIs above | CoinGecko/CoinPaprika | REST | NO KEY (exchanges, CoinPaprika) | months-years | yes | all 3 | high | |
| Order books/L2, bid/ask, spread, depth | Coinbase (REST L2 + WS ticker) **(integrated)** ✅ | OKX **(integrated)** ✅, Kraken **(integrated)** ✅ | REST + WS | NO KEY | none free (see below) | yes | all 3 | high | Coinbase `level2`/`full` WS now **requires auth** (confirmed live 2026-09); REST L2 snapshot still free |
| Trade flow / aggressor side | Coinbase `matches` WS **(integrated)** ✅ | OKX `market/trades` **(integrated)** ✅, Kraken **(integrated)** ✅ | WS/REST | NO KEY | none free beyond REST windows | yes | all 3 | high | |

---
## 2. Traditional markets

| Data | Best free source | Backup | Type | Key? | Historical | Real-time | Quality | Notes |
|---|---|---|---|---|---|---|---|---|
| S&P 500 | **FRED** `SP500` **(integrated key)** ✅ | Stooq ⚠️ now bot-blocked (JS challenge, confirmed live) | REST | KEY REQUIRED (free) | ~10y (FRED daily close) | delayed (EOD) | high | `fred.stlouisfed.org/docs/api` |
| Nasdaq | FRED `NASDAQCOM` | Stooq ⚠️ blocked | REST | KEY REQUIRED (free) | ~10y | delayed | high | |
| Gold | FRED `GOLDPMGBD228NLBM` | Stooq ⚠️ blocked | REST | KEY REQUIRED (free) | decades | delayed | high | |
| Oil (WTI) | FRED `DCOILWTICO` | EIA API (free, key required) | REST | KEY REQUIRED (free) | decades | delayed | high | |
| Natural gas | FRED `DHHNGSP` | EIA API | REST | KEY REQUIRED (free) | decades | delayed | high | |
| DXY / USD strength | FRED `DTWEXBGS` (trade-weighted dollar, close proxy for DXY) | — | REST | KEY REQUIRED (free) | decades | delayed | medium (proxy, not the exact ICE DXY) | |
| Treasury yields | FRED `DGS10`, `DGS2`, etc. | Treasury.gov `fiscaldata.treasury.gov` API ✅ (no key) | REST | KEY REQUIRED (FRED, free) / NO KEY (Treasury.gov) | decades | delayed | high | `fiscaldata.treasury.gov/api-documentation` |
| VIX | FRED `VIXCLS` | Stooq ⚠️ blocked | REST | KEY REQUIRED (free) | decades | delayed | high | |
| Individual stocks (NVDA, AAPL, AMZN, TSLA, COIN, MSTR) | **No fully free/no-key source confirmed working** here | Alpha Vantage (free key, 25 req/day) UNVERIFIED live this session; Financial Modeling Prep (free key, limited) UNVERIFIED; Stooq ⚠️ blocked; Yahoo Finance unofficial - **excluded**, ToS unclear (same stance as before) | REST | KEY REQUIRED (free tier) | varies (Alpha Vantage: 20y daily) | delayed | medium, unverified this session | This is the weakest link in "traditional markets" - no confirmed free/no-key/no-ToS-risk source for single-name equities |

---
## 3. Fed / macroeconomics

| Data | Best free source | Backup | Type | Key? | Historical | BTC/ETH/XRP relevance test needed | Notes |
|---|---|---|---|---|---|---|---|
| Fed rate decisions, statements, speeches | Federal Reserve press RSS **(integrated)** ✅ | FRED `FEDFUNDS`/`DFEDTARU` | RSS + REST | NO KEY (RSS) / KEY REQUIRED (FRED, free) | decades | yes | already integrated |
| Rate expectations (market-implied) | CME FedWatch (public web tool, no documented free API) UNVERIFIED | — | — | — | — | — | no confirmed free API; the CME FedWatch page itself is free to view, not to poll programmatically |
| CPI, PCE, PPI | FRED (`CPIAUCSL`, `PCEPI`, `PPIACO`) **(integrated key)** ✅ | BLS API ✅ (CPI/PPI; NO KEY for low-volume use, KEY REQUIRED for higher limits) | REST | KEY REQUIRED (FRED) / NO KEY-with-limits (BLS) | decades | yes | BLS v2: unregistered = 25 queries/day, 20y history; registered free key = 500/day |
| Jobs / NFP, unemployment | BLS API ✅ (`LNS14000000` unemployment confirmed live) | FRED (`PAYEMS`, `UNRATE`) | REST | NO KEY-with-limits (BLS) / KEY REQUIRED (FRED) | decades | yes | |
| GDP | FRED (`GDP`) | BEA API (needs free key, "UserID") | REST | KEY REQUIRED (both) | decades | yes | BEA confirmed to need a key (empty response without one) |
| Retail sales | FRED (`RSAFS`) | Census Bureau API (free, no key for many series) | REST | KEY REQUIRED (FRED) | decades | yes | |
| Consumer sentiment | FRED (`UMCSENT`, U. Michigan) | Conference Board (no clean free API found) | REST | KEY REQUIRED (FRED) | decades | yes | |
| Treasury data (auctions, debt, yield curve) | `fiscaldata.treasury.gov` API ✅ | FRED | REST | NO KEY | decades | yes | fully free, no key, confirmed live |
| Other macro (housing, PMI, etc.) | FRED (has hundreds of series) | — | REST | KEY REQUIRED (free) | decades | yes | one key covers essentially all of this category |

---
## 4. Government / politics / geopolitics

| Data | Best free source | Backup | Type | Key? | Historical | Notes |
|---|---|---|---|---|---|---|
| White House / presidential news | **No dedicated structured API**; use news/RSS layer | GDELT **(integrated)** ✅, Federal Register ✅ | RSS/REST | NO KEY | GDELT: recent; Federal Register: since ~1994 | |
| Congress (bills, votes, hearings) | Congress.gov API **(integrated key)** ✅ | GovTrack (free, no key, unofficial mirror) UNVERIFIED | REST | KEY REQUIRED (free) | since 1973 | already integrated |
| SEC | SEC press RSS + EDGAR full-text search **(integrated)** ✅ | — | RSS/REST | NO KEY | EDGAR full text: 2001+ | |
| CFTC | CFTC press RSS **(integrated)** ✅ | CFTC Socrata public datasets (Commitments of Traders) ✅ NO KEY | RSS/REST | NO KEY | COT: decades | `publicreporting.cftc.gov` confirmed live; verify the exact dataset ID for Bitcoin-futures COT specifically before relying on it |
| Treasury (sanctions, policy) | `fiscaldata.treasury.gov`, Treasury press RSS | OFAC sanctions list (free, no key, CSV/XML) UNVERIFIED live this session | REST/RSS/download | NO KEY | varies | |
| Crypto regulation | SEC/CFTC feeds above + Congress bill tracking (already integrated, filters for crypto/stablecoin keywords) | — | — | — | — | already integrated |
| Tariffs | Federal Register ✅ (executive orders, USTR notices published there) | USTR.gov (no clean API) | REST | NO KEY | since ~1994 | |
| Sanctions | OFAC SDN list (Treasury, free download, no key) UNVERIFIED live | — | download | NO KEY | current list only (not point-in-time history for free) | |
| Legislation | Congress.gov **(integrated)** | Federal Register (regulations) ✅ | REST | KEY REQUIRED / NO KEY | good | |
| Government shutdowns, elections, wars/geopolitics | **No dedicated free structured API** - these surface through the news/RSS/GDELT layer only | — | — | — | — | do not fabricate a dedicated feed; this is a real gap, addressed by news volume/sentiment, not a purpose-built dataset |

---
## 5. Crypto news

| Data | Best free source | Backup | Type | Key? | Notes |
|---|---|---|---|---|---|
| ETF approvals/flows | SEC EDGAR (13F/N-1A filings, free) UNVERIFIED for real-time flow granularity | Farside Investors public ETF flow tables (free website, no documented API) UNVERIFIED | manual/REST | NO KEY | daily ETF net-flow numbers are widely published for free on the web but no single confirmed structured free API for BTC/ETH ETF flows specifically - flag for further research |
| Exchange hacks, lawsuits, stablecoin issues, M&A, halving, upgrades, partnerships, protocol events | Official project/exchange RSS + GDELT + CoinDesk/Cointelegraph/Decrypt RSS **(integrated)** ✅ | — | RSS | NO KEY | already integrated; this is inherently a news-layer category, not a structured-data one |
| Regulatory/exchange announcements | Same RSS layer, plus SEC/CFTC feeds **(integrated)** | — | RSS | NO KEY | |

---
## 6. On-chain

| Data | Best free source | Backup | Type | Key? | BTC | ETH | XRP | Notes |
|---|---|---|---|---|---|---|---|---|
| Whale transactions | Etherscan (scan blocks, filter by value) **(integrated: supply only so far)** ✅ | Whale Alert API - **PAID/FREE WITH LIMITS**, too thin to poll continuously for free | REST | KEY REQUIRED (Etherscan, free) | via blockchain.info raw tx data (no built-in whale filter) | via Etherscan `eth_getBlockByNumber` scan | via XRPL public node / XRPScan ✅ (no key) | full whale-tx scanning needs building (per-block scan), not yet implemented |
| Exchange deposits/withdrawals | Same limitation as category 1's "exchange inflows/outflows" - no turnkey free API | | | | | | | |
| Active addresses | blockchain.info Charts (BTC) **(integrated)** ✅ | Etherscan doesn't expose this directly for free; would need block-scanning | REST | NO KEY (BTC) | ✅ | approximate only | no free source found | |
| Transaction volume | blockchain.info Charts (BTC) **(integrated)** ✅ | Etherscan gas/tx count stats (free, key required) | REST | NO KEY (BTC) / KEY REQUIRED (ETH) | ✅ | ✅ | XRPL public node/XRPScan ✅ | |
| Stablecoin supply | DefiLlama `/stablecoincharts` **(integrated)** ✅ | — | REST | NO KEY | n/a | n/a | n/a | market-wide, not per-coin |
| Miner behavior | **No confirmed free API** (hash rate is available; miner wallet flows are not, for free) | blockchain.info `hash-rate` chart **(integrated)** ✅ (proxy only) | REST | NO KEY | partial (hash rate only) | n/a | n/a | |
| Long-term holder activity | **No free source found** (Glassnode/CryptoQuant proprietary metric, PAID) | | | | | | | |
| Exchange balances | **No free source found** at useful granularity | | | | | | | |
| Large wallet movements | Same as whale transactions above | | | | | | | |
| Network fees | blockchain.info `mempool-size`/fee charts (BTC) **(integrated: some metrics)** ✅ | Etherscan gas oracle (free, key required) | REST | NO KEY (BTC) / KEY REQUIRED (ETH) | ✅ | ✅ | XRPL fees are near-zero/fixed, not meaningful | |
| Blockchain activity (general) | blockchain.info Charts (BTC) **(integrated)** ✅ | Etherscan stats, XRPL public node | REST | mixed | ✅ | partial | ✅ | |

---
## 7. Social / attention

| Data | Best free source | Backup | Type | Key? | Notes |
|---|---|---|---|---|---|
| Reddit | Official Reddit API **(integrated)** ✅ (needs the user's own free app credentials - not tested live this session, no credentials available) | — | REST (OAuth) | KEY REQUIRED (free app) | not scraping - official API |
| X/Twitter | **Effectively no usable free tier as of X's current API pricing** | — | — | — | flagged as UNVERIFIED/PAID; do not rely on it without re-checking X's current terms |
| News sentiment | GDELT (tone score built into GDELT's data) **(integrated, tone not yet parsed)** ✅ | Official RSS + optional LLM scoring (`crypto_ai/llm.py`, $0 models only) | REST | NO KEY | GDELT's tone field is free; LLM-based scoring is an add-on, not a data source |
| Google Trends | **Not implemented** - no official free API; the only free method (`pytrends`) automates Google's web UI, which its Terms restrict on automated querying | — | — | — | explicitly excluded, consistent with prior research; revisit only if Google ships an official API |
| YouTube attention | YouTube Data API v3 (free key, ~10,000 quota units/day) UNVERIFIED live this session | — | REST | KEY REQUIRED (free) | can pull view/like/comment counts for tracked crypto channels/search terms |
| News volume | GDELT article counts **(integrated)** ✅ | RSS feed counts (already integrated sources) | REST | NO KEY | |
| Fear & Greed Index | **alternative.me** `/fng/` API ✅ confirmed live, free, no key | CoinMarketCap has its own F&G index (needs CMC key) | REST | NO KEY | crypto-market-wide, not per-coin |
| Social volume/sentiment (aggregated) | **No good free turnkey source** (LunarCrush/Santiment are paid/thin-free) | Build your own from Reddit + GDELT + RSS volume (above) | — | — | |

---
## 8. Supply / demand

| Data | Best free source | Backup | Type | Key? | BTC | ETH | XRP | Notes |
|---|---|---|---|---|---|---|---|---|
| Circulating supply | CoinGecko/CoinPaprika ✅ | Etherscan (ETH) **(integrated)** ✅, blockchain.info (BTC) | REST | NO KEY (CoinPaprika, blockchain.info) / KEY REQUIRED (Etherscan) | all 3 | ✅ | ✅ | |
| New issuance | Derived from supply deltas above | Per-chain block reward math (BTC halving schedule is public/deterministic) | computed | — | ✅ | ✅ | ✅ | |
| Token unlocks | **No confirmed official free API** - unlock-calendar sites exist but a documented free API wasn't verified this session | DefiLlama may have relevant endpoints - worth checking their evolving docs | — | UNVERIFIED | n/a (BTC/XRP have no unlock schedule; XRP has Ripple's monthly escrow releases, which XRPL ledger data can show directly) | n/a | via XRPL public data | |
| Staking | beaconcha.in API (ETH staking, free tier) UNVERIFIED live this session | — | REST | NO KEY (basic) | n/a | ✅ | n/a (XRP/BTC don't have native staking) | |
| Burns | Etherscan (ETH gas burn since EIP-1559) **(integrated, not yet parsed for this)** ✅ | — | REST | KEY REQUIRED (free) | n/a | ✅ | n/a | |
| ETF demand | Same as category 5's ETF flows - no single confirmed free structured API | | | | | | | |
| Whale accumulation/distribution | Same limitations as on-chain whale tracking above | | | | | | | |
| Exchange supply | Same as "exchange balances" above - no free source found | | | | | | | |
| Large holder concentration | **No free source found** (rich-list APIs are mostly paid or explorer-website-only without a documented free API) | XRPScan has a public rich-list page; no confirmed API for it | — | UNVERIFIED | | | | |

---
## Historical dataset repositories (for backfill, not live collection)
| Source | License | Notes |
|---|---|---|
| Kaggle | Per-dataset (often CC0/MIT; some restrict commercial use) | Free download; check each dataset's license before use |
| Hugging Face Datasets | Per-dataset | Same caveat as Kaggle |
| GitHub (community-scraped datasets) | Often unclear/unverifiable provenance | **D-rank** - avoid unless license and provenance are explicit |
| AWS Open Data / Google Public Datasets (BigQuery public crypto datasets exist, e.g. `bigquery-public-data.crypto_bitcoin`) | Free tier (BigQuery has a free monthly query quota) | UNVERIFIED live this session; a genuinely promising free historical on-chain source worth investigating next |
| Academic (Zenodo, OSF) | Usually CC-BY | Hard to discover systematically; occasional tick-level historical crypto datasets |
| Exchange historical downloads | Free (Binance/Kraken/Coinbase publish historical CSV/ZIP archives) | Binance's is geo-restricted the same way its API is, from this environment |

---
# MASTER TABLE

| CATEGORY | DATA | SOURCE | FREE? | API KEY? | HISTORICAL? | REAL-TIME? | UPDATE FREQ | BTC | ETH | XRP | QUALITY | NOTES |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Crypto market | Price/OHLCV | Coinbase | FREE | NO | ~3y+ | yes | tick | ✅ | ✅ | ✅ | high | integrated |
| Crypto market | Price/OHLCV (2nd venue) | OKX | FREE | NO | months | yes | tick | ✅ | ✅ | ✅ | high | integrated |
| Crypto market | Price/OHLCV (3rd venue) | Kraken | FREE | NO | months-years | yes | tick | ✅ | ✅ | ✅ | high | integrated |
| Crypto market | Price (more venues) | KuCoin/Gate.io/MEXC/Bitget | FREE | NO | varies | yes | tick | ✅ | ✅ | ✅ | medium-high | verified live, not yet integrated |
| Crypto market | Price (geo-restricted here) | Binance | FREE | NO | years | yes | tick | ✅ | ✅ | ✅ | high | code ready, 451 in this sandbox |
| Crypto market | Funding rate | OKX, Deribit | FREE | NO | recent window | yes | ~8h | ✅ | ✅ | ✅(OKX) | high | integrated |
| Crypto market | Open interest | OKX | FREE | NO | recent window | yes | live | ✅ | ✅ | ✅ | high | integrated |
| Crypto market | Liquidations | OKX | FREE | NO | recent window | yes | live | ✅ | ✅ | ✅ | medium | integrated |
| Crypto market | Order book/spread | Coinbase, OKX, Kraken | FREE | NO | none free | yes | live | ✅ | ✅ | ✅ | high | integrated |
| Crypto market | BTC dominance | CoinGecko/CoinPaprika | FREE WITH LIMITS | NO | point-in-time | yes | live | n/a | n/a | n/a | medium | |
| Crypto market | Exchange in/outflow | none confirmed free | PAID (proprietary) | — | — | — | — | ⚠️ | ⚠️ | ⚠️ | low | gap |
| Traditional | S&P/Nasdaq/Gold/Oil/NatGas/DXY-proxy/10Y/VIX | FRED | FREE (key req.) | YES (free) | decades | delayed (EOD) | daily | n/a | n/a | n/a | high | integrated |
| Traditional | Treasury data | fiscaldata.treasury.gov | FREE | NO | decades | delayed | daily | n/a | n/a | n/a | high | new |
| Traditional | Individual stocks (NVDA etc.) | none confirmed free/no-key | UNVERIFIED | YES (free tier, e.g. Alpha Vantage) | varies | delayed | daily | n/a | n/a | n/a | unverified | weakest link |
| Macro | CPI/PPI/jobs | BLS | FREE WITH LIMITS | NO (25/day) or YES (500/day) | decades | delayed | monthly | n/a | n/a | n/a | high | new |
| Macro | GDP | FRED / BEA | FREE (key req.) | YES (free) | decades | delayed | quarterly | n/a | n/a | n/a | high | |
| Macro | Fed statements/speeches | Fed RSS | FREE | NO | archive | near-live | event | n/a | n/a | n/a | high | integrated |
| Government | Congress bills | Congress.gov | FREE (key req.) | YES (free) | since 1973 | daily | daily | n/a | n/a | n/a | high | integrated |
| Government | SEC/CFTC press | official RSS | FREE | NO | archive | near-live | event | n/a | n/a | n/a | high | integrated |
| Government | COT reports | CFTC Socrata | FREE | NO | decades | weekly | weekly | ✅(verify code) | — | — | medium | new, verify BTC-specific dataset id |
| Government | Federal Register | federalregister.gov | FREE | NO | since 1994 | daily | daily | n/a | n/a | n/a | high | new |
| Government | Sanctions (OFAC) | treasury.gov | FREE | NO | current list only | n/a | as-updated | n/a | n/a | n/a | medium | unverified live |
| Crypto news | Official/media RSS | project + media RSS | FREE | NO | archive | near-live | event | ✅ | ✅ | ✅ | high | integrated |
| Crypto news | Global news volume | GDELT | FREE | NO | recent | near-live | 15min | ✅ | ✅ | ✅ | medium | integrated (rate-limited on shared IPs) |
| On-chain | BTC chain metrics | blockchain.info | FREE | NO | years | daily | daily | ✅ | n/a | n/a | high | integrated |
| On-chain | ETH supply | Etherscan | FREE (key req.) | YES (free) | current | live | live | n/a | ✅ | n/a | medium | integrated |
| On-chain | XRPL ledger data | public rippled node / XRPScan | FREE | NO | full ledger history | live | live | n/a | n/a | ✅ | high | **integrated** (`xrpl_ledger_stats`, `xrpl_fee_stats`) |
| On-chain | Stablecoin supply | DefiLlama | FREE | NO | years | daily | daily | n/a | n/a | n/a | high | integrated |
| On-chain | Whale transactions | block-scanning (Etherscan/XRPL) | FREE (key req. for ETH) | YES (Etherscan) / NO (XRPL) | build-your-own | near-live | per-block | partial | build needed | build needed | unbuilt | design only, not implemented |
| Social | Reddit | official Reddit API | FREE (key req.) | YES (free) | recent | near-live | live | ✅ | ✅ | ✅ | high | integrated, needs your app creds |
| Social | Fear & Greed | alternative.me | FREE | NO | since 2018 | daily | daily | n/a | n/a | n/a | high | new, confirmed live |
| Social | Google Trends | none (ToS risk) | UNVERIFIED | — | — | — | — | — | — | — | n/a | not implemented, by design |
| Social | X/Twitter | none free | PAID | — | — | — | — | — | — | — | n/a | not implemented |
| Social | YouTube attention | YouTube Data API v3 | FREE WITH LIMITS | YES (free) | n/a (current stats) | near-live | on request | n/a | n/a | n/a | unverified | new |
| Supply/demand | Circulating supply | CoinGecko/CoinPaprika/Etherscan/blockchain.info | FREE (mixed key reqs) | mixed | current+ | yes | live-daily | ✅ | ✅ | ✅ | high | integrated (mixed) |
| Supply/demand | ETH staking | beaconcha.in | FREE | NO | current | near-live | live | n/a | ✅ | n/a | unverified | new |
| Supply/demand | Token unlocks | none confirmed free | UNVERIFIED | — | — | — | — | — | — | — | n/a | gap |
| Supply/demand | Exchange supply/holder concentration | none confirmed free | PAID (proprietary) | — | — | — | — | — | — | — | n/a | gap |

---
# RECOMMENDED FREE DATA STACK
Chosen to avoid duplicating the same underlying data across sources, prioritizing already-integrated pieces:

1. **Price/OHLCV/order book/trades/funding/OI/liquidations**: Coinbase (primary), OKX (secondary venue + derivatives), Kraken (third venue) — all already integrated. Do NOT also add KuCoin/Gate.io/MEXC/Bitget/Binance for the SAME data unless cross-venue arbitrage/consensus pricing becomes a specific research question — they'd be redundant for a single-venue-suffices signal.
2. **Macro**: FRED alone covers S&P 500, Nasdaq, gold, oil, nat gas, a DXY proxy, Treasury yields, VIX, CPI/PCE/PPI, GDP, retail sales, consumer sentiment — one key, one integration, minimal duplication. Add `fiscaldata.treasury.gov` (no key) for Treasury-specific detail FRED doesn't carry, and BLS (no key, low-volume) as a CPI/jobs cross-check/backup only.
3. **Government/political**: keep the existing Congress/SEC/CFTC/Fed RSS layer; add Federal Register (free, no key) for executive orders/tariffs/regulations, and CFTC's Socrata COT datasets (free, no key) — do not add a dedicated "politics" API, because none exists; this category is inherently news-driven.
4. **Crypto news/social**: keep the existing RSS + GDELT layer; add alternative.me's Fear & Greed Index (trivial, free, no key, high-value single number); add Reddit (already integrated, just needs your own free app credentials) — skip Google Trends and X/Twitter entirely (ToS/cost problems, not worth the risk for uncertain value).
5. **On-chain**: blockchain.info (BTC), Etherscan (ETH), and now a public XRPL node (`xrpl_ledger_stats`/`xrpl_fee_stats`) for XRP — the on-chain gap for XRP is closed.
6. **Supply/demand**: CoinGecko/CoinPaprika for circulating supply (redundant with each other - pick CoinPaprika since it worked without any rate-limit issue this session); Etherscan already covers ETH issuance/burn math.
7. **Individual stocks (NVDA/AAPL/etc.)**: this is the one category without a clean free/no-key/no-ToS-risk answer — recommend Alpha Vantage (free key, thin limits) ONLY if single-name equity correlation becomes a specific research question; otherwise skip it, since it adds a key and integration cost for a factor not yet shown to matter.
8. **Explicitly NOT recommended**: CryptoCompare (now requires a key where it didn't before, and duplicates CoinGecko/CoinPaprika); Stooq (now bot-blocked); Whale Alert (too rate-limited to be useful free); Yahoo Finance (ToS risk); X/Twitter (no usable free tier); token-unlock/exchange-balance/holder-concentration APIs (none confirmed free - this is a genuine data gap, not an oversight).

---
# API KEYS I NEED
Only the keys that unlock something with no free/no-key equivalent already covering the same ground:

```
FRED_API_KEY=            # macro: S&P/Nasdaq/gold/oil/DXY-proxy/yields/VIX/CPI/GDP/etc. - one key, huge coverage
CONGRESS_API_KEY=        # bills/hearings/votes (already required, unchanged)
ETHERSCAN_API_KEY=       # ETH supply/gas/whale-tx-scanning (already required, unchanged)
COINGECKO_API_KEY=       # optional - only if anonymous CoinGecko calls get rate-limited for your IP
COINMARKETCAP_API_KEY=   # optional - only if you want CMC's own Fear&Greed/market data alongside CoinPaprika
REDDIT_CLIENT_ID=        # + REDDIT_CLIENT_SECRET - official OAuth app, both free
REDDIT_CLIENT_SECRET=
OPENROUTER_API_KEY=      # optional - $0-model analysis layer (already implemented, unchanged)
GEMINI_API_KEY=          # reserved only - not wired into any code yet
```
Not included because they work with NO key: Coinbase, OKX, Kraken, CoinPaprika, DexScreener, GeckoTerminal,
Deribit, DefiLlama, blockchain.info, GDELT, alternative.me (Fear & Greed), fiscaldata.treasury.gov, Federal
Register, CFTC Socrata datasets, BLS (at the unregistered 25-query/day tier), XRPL public nodes/XRPScan.

Not recommended to add at all (no free/no-key/no-ToS-risk answer exists yet): a dedicated individual-stock API,
Google Trends, X/Twitter, Whale Alert, exchange-balance/token-unlock/holder-concentration APIs.
