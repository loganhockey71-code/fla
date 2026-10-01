# Maker-order microstructure research (pivot from taker-only edge search)

`EDGE_RESEARCH.md` found no net edge for a market-taking strategy on 1-minute candles at real costs. This is a
**separate, additive** track: whether resting (maker) limit orders, priced from real bid/ask and trade-print data,
can create a net edge that a market order cannot - because a maker captures (part of) the spread instead of paying
it, at the cost of an uncertain, possibly-never fill and adverse selection (you get filled disproportionately when
the market is about to move against you). Nothing here changes the live trading strategy; it is a research track only.

## 1. What data is now collected
`python -m crypto_ai.cli ws-collect` (run continuously on your own machine - GitHub Actions' 5-minute cron cannot
hold a persistent connection) opens Coinbase Exchange's free public WebSocket feed for BTC-USD/ETH-USD/XRP-USD:

- **`ticker`** channel (free, no auth): real-time best bid/ask, sizes, last trade price -> `book_ticks`, written only
  when the touch actually changes (throttled; see `micro_collect.quote_changed`).
- **`matches`** channel (free, no auth): every trade print, with size and the **aggressor** side (the maker's `side`
  flipped - same convention as the existing `coinbase.buy_pressure`) -> `trade_ticks`.
- **Order-book depth**: `level2`/`level3`/`full` WebSocket channels **now require authentication** (confirmed live,
  2026-09) - this is new since the original candle-only design and is not something a free/no-key setup can avoid.
  The free substitute is the REST `/products/{id}/book?level=2` snapshot (already used elsewhere in this project),
  polled every 2 seconds per coin -> `depth_snapshots` (depth and imbalance at 5bp and 25bp of mid).
- Existing 1-minute candles and features are untouched and still collected by `tick`/`scalp`.
- BTC/ETH/XRP relationships: not collected separately - computed from the aligned per-second series of all three at
  research time (they share one clock).

Schema: `supabase/schema_microstructure.sql` (new tables only; run once, safe to re-run).

## 2. Storage
Raw ticks are kept only briefly and then **compacted**, not retained forever - `python -m crypto_ai.cli
compact-microstructure` (or run it periodically yourself) rolls `book_ticks`/`trade_ticks` older than
`micro_raw_retention_hours` (default 48h) into `micro_bars_1s` - one row per symbol per **active** second, not a
dense per-second grid - then deletes the compacted raw rows. `depth_snapshots` is trimmed after
`micro_depth_retention_days` (default 7d). Estimated steady state, all three coins (see the schema file for the
per-table math): **low hundreds of MB**, not gigabytes. This is a deliberate trade-off against keeping raw ticks
forever, which would run into the low hundreds of thousands of rows per day just for trades.

## 3. What can be tested immediately vs what needs more data
**Immediately** (a live pipeline validation run is in this session's report below): bid-ask-bounce diagnostics,
raw spread/imbalance/trade-flow descriptive stats, and the maker-fill simulator's mechanics.
**Needs real duration** (this is new data, not something that can be reconstructed from history - Coinbase does not
sell historical order-book/tick data for free): a statistically meaningful A/B + sealed-holdout backtest needs
**at least several days, ideally 2-4+ weeks**, of continuous collection to cover different regimes and get enough
independent trade opportunities per candidate strategy. `micro-research` enforces a floor
(`MIN_HOURS_FOR_SPLIT`, default 24h combined) below which it refuses to run the backtest and prints only descriptive
stats - it will not manufacture a conclusion from too little data.

## 4. The maker-fill simulator (`crypto_ai/scalp/maker.py`)
Does **not** assume every limit order fills. Given a resting order's price and the depth already queued ahead of it
(from the periodic depth snapshot - the closest free proxy for queue position), it fills only once enough
**opposing-aggressor** trade volume prints at or through that price (`simulate_maker_fill`); if that volume never
arrives within a configurable horizon (`micro_fill_horizon_s`, default 30s) the order times out - a real "no trade"
outcome, counted separately from wins and losses. This is a documented **approximation**, not exact L3 queue
tracking (which needs the now-authenticated feed): it likely overstates the fill rate a little, since other
traders' orders can join the queue ahead of ours after we place it and we cannot see that without L3 data.

## 5. Market vs maker vs no-trade
`maker.compare_execution` prices all three for the same hypothetical entry: a market order (real measured spread +
slippage + fee, both legs), a maker order (fills or times out per above; a filled position exits with a market order
after `micro_hold_after_fill_s`, i.e. the assumption is you manage risk actively once you are in, not that you rest
a second passive order to get out), and doing nothing (0%). `micro_research.evaluate_execution` runs this only for
the actual entries a candidate strategy selects (it is the expensive step; not run for every second of history).

## 6. Simple strategies tested before any model (`crypto_ai/scalp/micro_setups.py`)
Order-book imbalance, trade-flow imbalance, spread compression/expansion, liquidity sweep (follow and fade),
short-term reversal, short-term momentum, VWAP distance (reversion and trend), and volume acceleration - all at
1-30 second scales, all pure functions on the per-second feature grid (`micro_features.py`), same shape as the
candle-scale `research.setups()`. No LightGBM model has been built for this track yet - per the brief, these simple
rules are tested first, and a model is only worth adding if they show something a model needs to improve on.

## 7. Protocol
Identical to `EDGE_RESEARCH.md`: chronological development period split into halves A and B, a **sealed holdout**
touched once at the end, real costs throughout (`trading_fee_pct`, `slippage_pct`, real measured spread - never an
assumed one, since we now record the actual spread). `maker_fee_pct` defaults to the same value as the taker fee:
**Coinbase does not offer a rebate at typical retail volume**, so no negative/rebate number is invented here - set
it to your real maker-tier rate if you have one.

## 8. Result of the pipeline-validation run in this session
25 minutes of `ws-collect` against real, live Coinbase data (BTC/ETH/XRP), then `micro-research`:

```
Coverage collected: BTC 0.42h (1,483 active seconds), ETH 0.42h (824), XRP 0.42h (1,103)
Bid-ask-bounce (lag-1 autocorr of 1s returns): BTC +0.149, ETH +0.087, XRP -0.041
  (positive = short-term momentum/persistence over this window, not bounce; XRP alone showed slight reversal - far
  too little data to read anything into the difference)
Average |move| over 60s: BTC 0.029%, ETH 0.039%, XRP 0.054% - all comfortably under the ~1% round-trip cost, exactly
  the same magnitude problem EDGE_RESEARCH.md found on 1-minute candles.
Correctly refused to run the A/B + sealed-holdout backtest (0.42h << the 24h floor) - reported descriptive stats
  only, as designed, and drew NO conclusion about a maker edge.
```

This proves the collector, schema, compaction, feature builder and maker-fill simulator all work end-to-end on live
data. It is **tens of minutes, not weeks**, and is reported honestly as insufficient for any edge conclusion.

## 9. Summary of what was asked at the end
- **Maker strategy with demonstrated positive out-of-sample expectancy:** none - not enough data has been collected
  yet to even attempt the test (need `ws-collect` running for days-to-weeks first).
- **Realistic trades/day, net expectancy/trade, drawdown, fees/rebates:** not yet determinable; `micro-research`
  will report all of these (per setup, per coin, per direction) once `MIN_HOURS_FOR_SPLIT` is cleared.
- **Fees/rebates:** `maker_fee_pct` defaults to the same value as the taker fee - Coinbase does not offer a rebate
  at typical retail volume, so no rebate number is fabricated; set it to your real tier if you have a lower one.
- **Biggest remaining limitation:** no free real-time L3 order-book feed (Coinbase now gates it behind auth), so
  queue position is approximated from a periodic depth snapshot rather than tracked exactly - `maker.py`'s docstring
  states this overstates the fill rate somewhat. The second-biggest: simply needing real running time, which no
  amount of engineering substitutes for.
