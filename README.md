# Crypto AI Lab: an autonomous short-term paper scalper for BTC, ETH and XRP

Private **paper-trading** app. Its job is to answer one question, over and over through the day: **"is there a tradable edge RIGHT NOW over the next several minutes, after fees and slippage?"** It decides on 1-minute Coinbase candles (5m/15m context), trades $1,000 of **fake** money at **real** prices (fees, spread and slippage included), manages every trade with adaptive stops/targets/trailing stops, records MFE/MAE and a post-mortem for each one, and keeps learning during the day. It no longer predicts 24h/48h direction.

There is no exchange or brokerage code anywhere. Nothing here can place a real order, and there is no "real trading" setting: it would need new, explicitly written order code and exchange keys, none of which exist. `config.require_paper_only()` makes the worker refuse to run if `REAL_TRADING_ENABLED` is set in the environment (so nobody believes real orders are being placed), and `cli selfcheck` plus a unit test scan every source file for order / withdrawal / wallet / broker code.

**Read this first - the honest state of the edge.** At the default simulated costs (0.4% fee + 0.1% slippage per side, about 1% round trip) a 5-15 minute BTC/ETH/XRP move essentially never pays for itself, and in the walk-forward backtests below the model did not find a validated positive-net edge at any cost tier tried. The system is built so that this shows up as *not trading* instead of as losses: it only opens a trade when its own out-of-sample-validated model predicts a net edge after costs, and it does not force a trade count. See [The scalper](#the-scalper) for the numbers.

```
supabase/schema.sql   database (run once)          web/     Next.js dashboard  -> Vercel (free)
worker/crypto_ai/     Python worker + ML            .github/workflows/  free scheduler for the worker
```

## Setup (~15 min, $0)

1. **Supabase**: create a free project. SQL Editor → run `supabase/schema.sql`, then `supabase/schema_research.sql`, then `supabase/schema_manual.sql`, then `supabase/schema_learning.sql`, then `supabase/schema_cashplan.sql`, then `supabase/schema_short_horizon.sql`, then `supabase/schema_risk.sql`, then **`supabase/schema_scalp.sql`** (scalper tables incl. `scalp_signals`, where every prediction is recorded and graded + cleanup of stale settings rows; **safe to re-run, and you must re-run it after updating**), then optionally **`supabase/schema_microstructure.sql`** (only if you'll run `ws-collect`) and **`supabase/schema_datalake.sql`** (only if you'll run `collect-datasources`). Copy the *Transaction pooler* connection string (Project Settings → Database).
2. **Bootstrap the scalper** (once, on your machine): `python -m crypto_ai.cli scalp-train` downloads 30 days of 1-minute candles, stores them, trains the trade-outcome model and promotes it **only if** it earns net P&L on an unseen holdout. If it does not, the scalper correctly stays flat. (The legacy 24h/48h models below are optional and no longer trade.)
   Optional legacy models:
   ```bash
   cd worker
   pip install -r requirements.txt
   cp ../.env.example ../.env        # fill in DATABASE_URL
   python -m crypto_ai.cli research --force   # FRED history + Congress + feeds + prediction markets (needs FRED_API_KEY, CONGRESS_API_KEY in .env)
   python -m crypto_ai.cli train      # downloads ~3 years of free Coinbase candles, ~10 min
   python -m crypto_ai.cli tick       # first predictions + paper trades
   ```
3. **Keep it running** — pick one:
   - *GitHub Actions* (free, needs a PUBLIC repo): push this folder to a repo, add secrets `DATABASE_URL`, `FRED_API_KEY`, `CONGRESS_API_KEY`, and optionally `OPENROUTER_API_KEY` (Settings > Secrets and variables > Actions > New repository secret). `worker.yml` runs `tick --burst 240` every 5 min (each run makes a scalper pass every 20 s for ~4 min and replays every 1-minute candle it missed). Set the repo variable `LOCAL_TZ` (e.g. `America/New_York`) so the learning window is your local time. (A private repo only gets 2,000 free Actions min/month - that covers hourly, not 5-min. Make the repo public for unlimited free minutes, or widen the cron back out if it must stay private.)
   - *Your own machine* (a guaranteed cadence, unlike GitHub's best-effort scheduler): `python -m crypto_ai.cli scalp --every 20`
4. **Dashboard**: import the repo in Vercel, root directory `web`, env vars `DATABASE_URL` and `APP_PASSWORD` (not the research keys: the dashboard never uses them). The site refuses to serve without a password.

Local dashboard: `cd web && npm install && npm run dev` (needs `DATABASE_URL`).

## The scalper
Code: `worker/crypto_ai/scalp/`. Dashboard: **Trades > Scalper**. Everything is paper trading.

```
1m candles (+5m/15m context) + fresh news -> features -> model predicts NET edge (after fees+slippage+spread) for a long and a short
   -> entry gate (cost gate, edge threshold, re-arm, cooldown, exposure, drawdown, daily loss, spread, liquidity, news, blocked setups)
   -> the prediction is only a SIGNAL (recorded in scalp_signals); WAIT for the NEXT 1-minute candle to confirm it
   -> confirmed: trade at the live price, sized by balance / volatility / liquidity / fees / spread / slippage / maximum loss. Not confirmed: NO TRADE.
   -> adaptive stop / target / trailing stop, sized from current volatility x a learned per-coin multiplier
   -> managed candle by candle on the high/low (intrabar) -> closed trade with MFE/MAE, costs, exit reason, entry conditions
   -> every signal (traded or not) graded right/wrong against what the market did AFTER it; post-mortems + rolling statistics +
      guarded retraining/exit tuning (05:00-23:59 local)
```

**One decision engine.** `engine.py` (`entry_decision`, `step_position`, `close_position`) is the only thing that decides or manages a trade. The label builder, the backtest, the exit optimiser and the live worker all call it; `tests/test_scalp.py` proves the engine reproduces the label outcomes exactly. The old score overlay in `realtime.py` still writes news/shock alerts for the dashboard but places no trades; the old prediction-driven trading and the old autopilot are gone.

**Predict, then wait for the next candle to confirm (`engine.confirm_signal`).** A prediction made at a candle's close never trades by itself. When the *next* 1-minute candle has closed the engine checks that it (1) closed in the predicted direction, (2) followed through by at least 0.25 one-minute ATRs, (3) did not first trade more than 1 ATR against the call, (4) did not already run more than 2 ATRs (chasing), and (5) the model's fresh predicted edge is still at least the entry threshold. Any failure means **no trade**, and the reason is recorded (`candle_against`, `no_follow_through`, `reversal`, `chased`, `edge_gone`). A confirmation that arrives later than 90 s after that candle closed (a late worker) expires instead of trading. Live, the backtest and the exit-policy learner all use this same function, so what is measured is what trades. Confirmation cannot be switched off by a settings row (`scalp_confirm_required` is code-owned).

**Every prediction is recorded and graded, right or wrong (`scalp_signals`).** Whenever the model's predicted net edge clears its threshold the prediction is stored with the conditions at that moment (features, named candle patterns such as hammer / engulfing / breakout, news context) and its fate: `confirmed` (traded), `failed` (the next candle refused it, with the reason), `blocked` (a liquidity / news / cost gate refused it, with the reason), or `expired`. Once the hold window has passed, each one is **graded using only candles after the decision**: right / wrong / flat, whether it would have paid after costs, best/worst excursion, plus a plain-English lesson ("wrong and correctly not traded: the next candle did not confirm it (reversal)", "right but did NOT trade: that gate cost a winner", "right direction but a cost casualty"). The learning loop then studies which combinations (candle pattern x volume x trend x news alignment x confirmation strength) actually earned money after costs, kept only if the sign holds in both halves of the window, and reports whether next-candle confirmation is really separating winners from losers.

**News is part of the decision (`scalp/news.py`).** The research layer classifies every item for BTC/ETH/XRP (which coin, bullish / bearish / neutral, importance, source credibility, independent source families). For each coin the scalper weighs fresh events (credibility x confidence x novelty x freshness, half-life 45 min), counts the *independent* source groups on the winning side (five outlets rewriting one press release count once), and decides: **conflicting sources -> no trade**; strong news against the trade -> blocked, weak opposition -> half size; agreeing news -> up to 25% larger size (only with 2+ independent groups; never beyond the risk caps); important official news with no readable direction -> half size. Only events detected before the decision are visible (no look-ahead). There is no historical news, so the backtest runs without it; the news layer is judged on live signals, which store the context they were taken in.

**The account is never risked blindly (`engine.size_position`, `config.SETTING_BOUNDS`).** Position size is the smallest of: your max position % of equity, free cash (no leverage), the size at which a full stop-out *including fees, spread, slippage and the order's own market impact* loses at most your max-loss % of equity, a small share of the last 15 minutes' dollar volume (liquidity), and what is left of your total-exposure cap. A coin that traded less than the minimum liquidity in 15 minutes is skipped; bigger orders pay more slippage (`costs.impact_pct`). Further limits: daily loss limit (open losses count), a drawdown halt from the 7-day equity high, max open positions, max spread. Every dashboard setting is clamped to a safe range (here, in the dashboard and in the worker, kept identical by a test). A large account is allowed to take large positions; it is never allowed to put the whole account at risk.

**Opportunity, not price.** The model's inputs are all percentages and ratios, so a $0.01 coin and a $100,000 coin that move alike look identical (tested); candidates are ranked by predicted *net* edge, then liquidity, never by price or name.

**A model trained on trade outcomes, not direction.** For every 1m bar and each direction, `labels.py` simulates a stop/target/time-limited trade entered at the next open and records its **net %** after fees, slippage and spread, plus MFE and MAE. Two LightGBM regressors (long, short; pooled over BTC/ETH/XRP) learn that net outcome (squared error on net P&L, not log-loss on up/down). A trade needs a predicted net edge above a **threshold chosen on out-of-sample validation net P&L** (positive mean whose one-stderr lower bound is also positive, with enough trades). If no threshold qualifies the model does not trade.

**Costs are inside the decision.** A trade whose target does not clear round-trip costs (`fees + slippage + spread`, both legs) by a margin is rejected outright (`cost_gate`), and position size is cut so a full stop-out *including costs* loses at most 0.5% of the account. Profitable-before-costs, losing-after-costs trades are counted separately in every report ("cost casualties").

**Adaptive exits.** Stop and target distances = current 1m ATR% x sqrt(hold minutes) x a multiplier (default 1.0 / 1.6, learned per coin and per regime within bounds). A trailing stop arms at 0.8R and trails behind the best price. Losers are cut by the stop, a momentum reversal, or no follow-through; a trade that just drifts ends at a 15-minute time stop. **A stop is never widened** (`tighten_stop` refuses; tested). If one candle touches both stop and target the stop is taken; a gap through the stop fills at the open; reversal exits fill at the next open.

**Trade frequency.** There is no quota. Duplicate entries from one lingering signal are blocked (a direction must re-arm once its edge relaxes, plus a 5-minute per-coin cooldown, one position per coin, max 3 open, a daily loss limit).

**Every closed trade records** asset, entry/exit price and time, duration, direction, initial and final stop, take-profit, trailing behaviour, MFE/MAE (also in R), fees, slippage, gross and net P&L, exit reason, profitable flag, and the market features / regime / setup at entry (`scalp_trades`).

**Self-learning (05:00-23:59 local; set `local_timezone` or `LOCAL_TZ`).** It never retrains on one trade:
1. **Post-mortem** for every closed trade (`scalp_lessons`), e.g. *"BTC long failed because momentum reversed within 2 min"*, *"XRP short worked when volume was 2.1x normal and price was below VWAP"*, *"XRP long was right but lost: 0.60 USD of costs on a 0.40 USD gain"*.
2. **Rolling statistics** per coin / direction / setup / regime over the last 14 days; a setup is **blocked** only with 30+ trades and mean net + 1 stderr < 0 (it ages out of the window and gets a fresh probation sample). Aggregated as plain-English insights on the dashboard.
3. **Exit policy**: walk-forward search of stop/target/trail multipliers on **net** P&L, replaying the model's out-of-sample entries through the real engine; scored by the worst half of the window; applied only if it beats the incumbent by a margin. **The result is written to `scalp_state.exit_policy` and read by every new live trade** (this replaces the old report-only `optimize-exits`).
4. **Retraining**: a challenger is trained on data before an unseen holdout and promoted only if it nets more than the champion on that holdout, has enough trades and a positive mean, and wins a paired day-level bootstrap; every attempt is logged.

### What was fixed
| Problem | Fix |
|---|---|
| Stale database settings overriding the intended horizons | `db.settings()` now overlays only an allow-list (`config.USER_EDITABLE`) of database rows, each type-coerced; retired rows (`trade_horizons`, `sudden_move_pct`, ...) are ignored, and `schema_scalp.sql` deletes them. Tested. |
| `autopilot_enabled` string/boolean kill switch | strict `as_bool` (a JSON string `"false"` is off; unparseable => **off**), in the worker and in the dashboard (`web/lib/db.ts`); the switch also flattens open scalper trades. Tested end to end. |
| GitHub Actions not really every 5 minutes | GitHub's scheduler is best-effort and can't be fixed from here. The worker is now cadence-tolerant: every run replays all 1m candles since each trade last saw one (a late/dropped run cannot miss a stop touched in between), `tick --burst 240` makes a pass every 20 s for ~4 min, and a heartbeat is shown on the dashboard. For a guaranteed cadence run `cli scalp` yourself. |
| No intrabar precision | 1m high/low replay live and in backtests; the live price also covers the still-forming minute. Known gap: the candle that was already running when a trade opened is skipped (only the live price covers it). |
| Two decision engines that disagree | one engine (above); the old ones no longer trade. |
| Model not optimised for trading P&L | net-outcome labels + net-P&L threshold selection + champion/challenger on net P&L. |
| `optimize-exits` not feeding back | now writes the live exit policy (above). |

### Results (real Coinbase 1-minute candles, 75 days to 2026-09-29, walk-forward, 36 out-of-sample days)
Six expanding-window folds: train on all earlier data, choose the edge threshold on the next 6 days, then score the following 6 days untouched. Costs per side are your settings (0.4% fee + 0.1% slippage) plus an assumed 0.01-0.05% spread (historical bid/ask isn't available).

| | **Strategy as built, your costs** | **Strategy as built, cheap costs (0.05% + 0.01%)** | *Diagnostic: forced trading, your costs* | *Diagnostic: forced trading, cheap costs* |
|---|---|---|---|---|
| total trades | **0** | **0** | 145 | 137 |
| trades / day | 0 | 0 | 4.0 | 3.8 |
| win rate | - | - | 6.9% | 24.8% |
| net P&L | $0.00 | $0.00 | -$278.83 (-27.9%) | -$47.53 (-4.8%) |
| profit factor | - | - | 0.04 | 0.57 |
| max drawdown | 0% | 0% | 27.9% | 4.8% |
| avg win / avg loss | - | - | +$1.12 / -$2.15 | +$1.85 / -$1.07 |
| avg trade duration | - | - | 6.4 min | 6.3 min |
| fees / slippage+spread | $0 | $0 | $224.94 / $65.50 | $38.85 / $21.57 |
| gross P&L before costs | - | - | +$11.61 | +$12.89 |
| avg MFE / MAE | - | - | +0.47% / -0.30% | +0.40% / -0.29% |
| BTC / ETH / XRP net | 0 / 0 / 0 | 0 / 0 / 0 | -$60 / -$83 / -$135 | -$6 / -$18 / -$23 |

The first two columns are the real strategy: in every fold the model found **no threshold with a positive net edge after costs**, so it correctly took no trades. The last two are **diagnostics that are not the strategy**: they force the model's top 0.5% signals to trade with no validation, no edge floor and no cost gate (`cli scalp-backtest --diagnostic-top 0.005`) to show what trading without an edge costs. They show why the strategy stays flat: before costs those 140-odd trades earned about +$12; costs were $60-$290. By regime (your costs, diagnostic): high_vol_chop 56 trades -$92, high_vol_trend 64 -$134, normal_trend 14 -$33, normal_chop 9 -$14, low_vol 2 -$6; every regime lost. Full per-setup and per-exit tables come out of `scalp-backtest`.

**Re-run on 2026-10-01 with next-candle confirmation, and with longer holds.** Same 75-day / 36-day out-of-sample walk-forward, real Coinbase candles. Confirmation on or off, 15-minute hold, your costs: **0 trades** (no threshold ever earned a positive net edge, so there is nothing to confirm). Longer holds, to test whether a bigger expected move can beat fixed costs: **60-minute hold at your costs: 0 trades; 180-minute hold at your costs: 0 trades; 60-minute hold at a cheap tier (0.10% fee + 0.02% slippage, 0.24% round trip): 0 trades.** In all of them the validation step found no edge threshold with a positive mean whose lower error bound is also positive, in any of the six folds. Reproduce with `python -m crypto_ai.cli scalp-backtest --days 75 [--hold-bars 60] [--fee 0.1 --slip 0.02] [--no-confirm]`.

**Full free-data research map:** see [worker/DATA_SOURCES_MASTER.md](worker/DATA_SOURCES_MASTER.md) - all 8 requested categories (crypto market, traditional markets, macro, government/politics, crypto news, on-chain, social, supply/demand) for BTC/ETH/XRP, a master table, a recommended stack, and the exact API keys needed. Research/planning only - no new code from this pass.

**Free-data expansion (new, separate track):** see [worker/DATA_SOURCES.md](worker/DATA_SOURCES.md) - a catalog of free/legal data sources across 31 categories and an adapter framework (`worker/crypto_ai/datasources/`) with 30 concrete integrations (Coinbase, OKX, Kraken, Binance, CoinGecko, CoinMarketCap, DexScreener, GeckoTerminal, Deribit, DefiLlama, on-chain, GDELT, Reddit) writing to a local Parquet data lake, not Supabase. A **Data** tab on the dashboard shows each source's status, last fetch, records, and errors. Research/ingestion only; no trading change.

**Maker-order pivot (new, separate track):** see [worker/MAKER_RESEARCH.md](worker/MAKER_RESEARCH.md) - a free real-time collector (`ws-collect`) plus a maker-fill simulator, testing whether RESTING limit orders can find a net edge a market order can't. Does not change live trading; needs real running time (days/weeks) to reach a conclusion.

**Follow-up investigation:** see [worker/EDGE_RESEARCH.md](worker/EDGE_RESEARCH.md) (`python -m crypto_ai.cli scalp-research`): a tiny gross mean-reversion signal exists (~+0.02-0.10% per trade) but nothing survives the ~1% cost, and simple rules beat LightGBM.

**What this does and does not say.** In 75 days, no feature set I tried (candle/volume/VWAP structure, 5m/15m context, BTC/ETH lead-lag at 1-3 minutes, signed-volume proxies; 5- and 15-minute holds; two cost tiers) gave out-of-sample predicted edge that realised net profit; the model's rank correlation with realised net outcome was about 0 (-0.05 to +0.06). The last 14 days were held out of all feature experiments. That is evidence there is no cheap, candle-only edge at these horizons and costs, not proof that none exists: the things that could carry one (order-book imbalance, taker flow, funding, tick data) have no free history, so they are recorded going forward (`market_data`) but can't be backtested yet. At ~1% round-trip cost a 5-15 minute move essentially never pays for itself, so ~20 trades/day is not reachable *honestly* at the current cost settings; the levers are the cost model (real maker/limit fills), longer holds, or a genuinely new data source. The system re-evaluates the model every 12 h in the learning window and will start trading by itself the first time a challenger proves a net edge on an unseen holdout.

### Known limitations
- Shorts are a paper simulation (no funding, same fees); turn them off in Settings for long-only.
- Historical replays assume a per-coin spread (BTC 0.01%, ETH 0.02%, XRP 0.05%); live trading uses the real ticker spread.
- Labels model a plain stop/target/time trade; trailing, reversal and no-follow-through exits are engine-only (the backtest includes them, the label does not).
- Backtest drawdown is marked to market at candle closes.
- GitHub Actions timing cannot be guaranteed (see above).

## The app: five pages
**Dashboard** (BTC/ETH/XRP price cards with 24h change and chart, one AI signal table, news and market impact, your paper portfolio and recent trades) · **Signals** (live signals, prediction history, accuracy, learning) · **News** (research events, sudden moves, source health) · **Trades** (the **Scalper**, your manual trading, and the old AI test account) · **Settings** (settings and system health). Old links redirect.

## "What should I do with the cash?"
Whenever you sell in the Trades page, the app immediately analyses the money that was freed up and saves a recommendation: keep it as cash, buy one coin, or split it. It scores each coin from the current AI signal and confidence, recent news, sudden-event risk and market stress, caps any single coin at 40% of your portfolio, and shows a **safer** option (keep all cash) and a **more aggressive** one (a 60/40 split of the top two). **Nothing is invested until you press a confirm button**; a plan can only run once, and it buys at the live price with the normal fees and slippage. Its advice is only as good as the AI's signals, which are unproven; when every signal is near 50% it says so and defaults to keeping cash.

## News and shock alerts (no longer trade)
`realtime.py` still watches BTC/ETH/XRP for a fast price move, a volume spike, an extreme order-book imbalance and major official news, and publishes BUY/HOLD/REDUCE/SELL *alerts* with every scoring term listed (Signals tab, "What to do now"). They are information for you and do not trade. The scalper reads the classified `research_events` directly (see *News is part of the decision* above), and only `scalp/engine.py` opens or closes trades.

## Manual paper trading (Trades > My trading)
Buy and sell BTC/ETH/XRP any time with fake money, all on one page. **Buy** with a dollar amount (quick $25/$50/$100/Max). **Sell** 25% / 50% / 75% / all, or a dollar amount, from each coin card or the **Your holdings** table, or press **Sell everything** (asks to confirm). Same live price, real spread, 0.4% fee and 0.1% slippage as the AI, no leverage, spot only. It is a **separate account** (its own $1,000) so your trades never change the AI's results. **Profit & loss** shows profit locked in from sales (kept when you rebuy), profit on what you still hold, an average cost that restarts on each new purchase, and a running total per sale. Every trade stores what the AI was saying at that moment. Manual positions never close automatically; you sell them. (The old "AI autopilot" that traded this account is gone; the switch on this page is now the scalper's kill switch.)

## Research layer (free, read-only)
Sources and trust tier (1 = most trusted) - polled on their own schedule by `python -m crypto_ai.cli research` / `research-loop`:

| Tier | Sources | Interval |
|---|---|---|
| 1 official government | SEC (press, statements), Federal Reserve (press, speeches), CFTC (press, enforcement), Congress.gov API, FRED API | 5 min / 5 / 5 / 12 / 45 |
| 2 official project | XRP Ledger `rippled` releases, Ethereum Foundation blog, go-ethereum releases | 5 min |
| 3 financial news | CNBC Finance, MarketWatch | 10 min |
| 4 crypto media | CoinDesk, Cointelegraph, Decrypt | 5 min |
| 5 prediction markets | Polymarket, Kalshi (public endpoints only; no accounts, wallets or orders) | 5 min |
| 6 social/unverified | never ingested | - |

- **One story = one event.** Rewrites of the same headline are folded into one `research_events` row (`event_duplicates` keeps the copies). Copies inside one source family (five outlets rewriting one press release) add **zero** independent confirmations; a primary source + press, or + a market, counts as 2. A more credible source arriving later becomes the origin.
- **FRED** (`macro_data`): fed funds, 2y/10y yields, CPI, core CPI, unemployment, payrolls, jobless claims from 2019. Each value is only visible from `available_at` (observation date + publication lag), so the model never sees a number before the market could have.
- **Congress.gov** (`legislative_items`): crypto/stablecoin/SEC/CFTC/market-structure bills with actions, committees, amendments, hearings and House votes. A state hash means an unchanged bill never produces a new event.
- **Prediction markets** (`prediction_market_snapshots`): implied probability, volume and liquidity are stored; a >=8-point move creates an event. They are **one feature only** and never trigger BUY/SELL.
- **How research reaches the model:** two models per coin run side by side and are both logged and scored. `market` (candles only) paper-trades. `research` (same + macro + event features) is *shadow*: it never trades until you choose to (`trade_variant` setting). Every prediction stores the exact research snapshot that existed at that instant (`predictions.research_features`). The Performance page pairs the two by `run_id` and tests whether the difference is more than noise.
- Event features are `NaN` (unknown) before the first event was detected - history is never faked - so the research model will match the market model until real research history accumulates.

## Rules that keep the test honest
| Rule | How it is enforced |
|---|---|
| Predictions can't be edited | DB triggers reject UPDATE/DELETE/TRUNCATE on `predictions` and `prediction_results`; closed trades are frozen too |
| No look-ahead | features use only closed candles (`asof` = candle close); `tests/test_core.py` rewrites the future and asserts past features don't change; DB `CHECK (data_cutoff <= created_at)` |
| Exactly what was known | each prediction stores the full feature vector, model version and data cutoff |
| Backtest ≠ live | backtests live only in `model_versions.backtest_metrics` and are shown in a separate, labelled section |
| No fake precision | calibration is centered and can never flip the model; "high confidence" (≥75%) is reported separately and will usually be empty |
| Luck check | dashboard reports a p-value vs a coin flip and a verdict ("Too early" below 100 scored predictions) |
| Free-source hierarchy | SEC/Fed/CFTC/Congress = `official`, project blogs = `project`, crypto RSS = `media` (importance capped at 65). No social/rumor feeds are ingested |

## Design decisions you may want to change
- **Legacy prediction account and your manual account: spot, long-only.** (The scalper additionally simulates shorts - see above.) BUY opens a long that exits at the horizon, an opposing SELL signal, or a risk-management exit (see below), whichever comes first; SELL can only close an existing long (otherwise logged as *skipped*). Shorting would need leverage/margin, which is excluded.
- **Position limits**: 10% of portfolio (20% if confidence ≥ 75%), capped by cash. One open position per coin per horizon.
- **Signal threshold 52%** (Settings). Calibrated probabilities from crypto models sit near 50%; at 55% the current models almost never trade, which would leave nothing to test.
- **What the model learns from** (chosen by testing every change on the same unseen year of real data, Sept 2025 - Sept 2026):
  - *3 years of history* instead of ~9 months, and *one pooled model* for BTC, ETH and XRP (with `coin_id` as an input), which triples the data. Calibration and backtest metrics stay per coin.
  - *Multi-day features* (3/7/30-day returns, 7-day volatility and volume, distance from the 30-day high/low, BTC's 7-day move) plus *hour of day / day of week*.
  - Together these raised the mean out-of-sample AUC from ~0.52 to ~0.56. That is a small but consistent edge in direction, still **not** enough to beat ~1% round-trip costs in the backtest at any signal threshold.
  - Tried and rejected because they made it worse: dropping small moves from training (a "dead zone"), a cost-aware "up more than fees" label, weighting big moves, the Crypto Fear & Greed index, and multi-day features with only 9 months of history (they memorise regimes).
- **Order-book / spread / buy-sell pressure** are collected and stored with every prediction, but v1 models train only on candle features: Coinbase has no free history of order-book data, so training on it would mean training on nothing. After a few months of logged snapshots they can be added as model inputs.
- **Coinbase, not Binance**: Binance.com blocks US IPs. REST polling is used instead of WebSockets so it works on free schedulers.
- Sudden-move detection reads 1-minute candles, so even a 15-min scheduled run can miss the very start of a fast move; run `loop` locally for real-time alerts.

## Commands
```bash
python -m crypto_ai.cli scalp-train [--days 30]   # store 1m history + train the trade-outcome model (guarded promotion)
python -m crypto_ai.cli scalp-research [--section grid]   # taker/candle edge investigation + sealed holdout (see worker/EDGE_RESEARCH.md)
python -m crypto_ai.cli ws-collect [--seconds N]   # real-time microstructure collector (run continuously; see worker/MAKER_RESEARCH.md)
python -m crypto_ai.cli compact-microstructure     # roll aged raw ticks into 1-second bars
python -m crypto_ai.cli micro-research             # maker vs taker vs no-trade edge investigation on collected data
python -m crypto_ai.cli collect-datasources        # poll free data-source adapters (funding, OI, liquidations, basis, on-chain; see worker/DATA_SOURCES.md)
python -m crypto_ai.cli ask "question"              # ad-hoc research question via OpenRouter ($0 models only; analysis only)
python -m crypto_ai.cli scalp-backtest [--fee F --slip S --days 75 --out r.json]   # walk-forward backtest through the live engine
python -m crypto_ai.cli tick [--burst 240 --every 20]   # one full cycle (+ scalper passes for --burst seconds)
python -m crypto_ai.cli scalp --every 20   # scalper forever (local)
python -m crypto_ai.cli optimize-exits     # walk-forward exit search; the result is applied to new live trades
python -m crypto_ai.cli train [--days N]   # LEGACY 24h/48h models (no longer trade)
python -m crypto_ai.cli loop --every 300   # tick forever
python -m crypto_ai.cli status             # row counts
python -m crypto_ai.cli research [--force] [--source fred_api]   # poll due research sources
python -m crypto_ai.cli research-loop      # forever, each source at its own interval
python -m crypto_ai.cli watch [--every 60]  # near-real-time sudden-event watcher
python -m crypto_ai.cli learn              # scalper learning pass now (stats, exit policy, guarded retrain)
python -m crypto_ai.cli selfcheck         # read-only end-to-end check of the running system (also see the /health page)
cd worker && python -m pytest              # tests; set TEST_DATABASE_URL to a FRESH SCRATCH Postgres to also run the dedupe + full paper-trading e2e tests
```

## Optional AI analysis layer (OpenRouter, $0 models only)
`worker/crypto_ai/llm.py` can add AI-written commentary on top of the deterministic system: a short summary of recent
news, one extra sentence on a completed trade's post-mortem (`scalp_lessons.details.llm_note`), and an `ask` CLI
command for ad-hoc research questions. **It never places, sizes, or times a trade** - every trading decision stays in
`crypto_ai.scalp`'s deterministic/ML code, and this module isn't even imported by the live trading path.

- Only **$0** OpenRouter models are ever called: `free_models()` reads OpenRouter's own live price list and keeps only
  entries priced exactly zero; `chat()` refuses to call anything else, so a config mistake can't route to a paid model.
- Off by default even with a key present: set `llm_enabled: true` (there's no Settings UI toggle yet - insert the row
  directly, or wait for one) AND set `OPENROUTER_API_KEY`.
- Timeouts, capped retries with backoff on 429/5xx, and graceful failure: any problem (no key, no free models, network
  down, malformed reply) returns `None` instead of raising - nothing in the pipeline depends on this working.
- Where to put the key:
  - **Local**: add `OPENROUTER_API_KEY=` (and optionally `OPENROUTER_MODEL=`) to your `.env` (see `.env.example`). Never commit it.
  - **GitHub Actions**: repo Settings > Secrets and variables > Actions > New repository secret, named `OPENROUTER_API_KEY`;
    it's already wired into `worker.yml`'s `env:` block.
  - **Vercel**: **not needed.** The dashboard (`web/`) never calls OpenRouter - keep this worker-side only, the same
    rule as `FRED_API_KEY`/`CONGRESS_API_KEY`.
- Get a free key at https://openrouter.ai/keys - no payment method required to use only $0 models.

## Tests
```bash
cd worker && python -m pytest tests -q           # pure logic; database tests skip unless TEST_DATABASE_URL is set
python tests/run_scratch_tests.py                 # builds a throwaway real Postgres (needs `pip install pgserver`) and runs EVERY database test
cd web && npm test && npm run typecheck           # dashboard unit tests (Node 22+) and TypeScript (the 3 database-backed web tests run in the scratch-database step above)
```
`tests/test_scalp_confirm_risk.py` covers next-candle confirmation, sizing/liquidity/exposure/drawdown, settings clamping, news weighting, grading (including that grading cannot see the future) and the paper-only guard; `tests/test_scalp_confirm_live.py` drives the live loop (predict -> confirm -> trade -> grade) against a real Postgres, including an entry-side failure that must never leave an open trade unmanaged.
