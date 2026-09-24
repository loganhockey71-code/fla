# Crypto AI Lab — does this AI predictor actually work?

Private **paper-trading** app for BTC, ETH and XRP. It makes 24h/48h predictions with LightGBM models (trained on all three coins at once, calibrated per coin), "trades" $1,000 of **fake** money at **real** Coinbase prices (fees, spread and slippage included), scores every prediction when its window ends, and shows whether the results are distinguishable from luck at 30/60/90/180 days.

There is no exchange or brokerage code anywhere. Nothing here can place a real order.

```
supabase/schema.sql   database (run once)          web/     Next.js dashboard  -> Vercel (free)
worker/crypto_ai/     Python worker + ML            .github/workflows/  free scheduler for the worker
```

## Setup (~15 min, $0)

1. **Supabase**: create a free project. SQL Editor → run `supabase/schema.sql`, then `supabase/schema_research.sql`, then `supabase/schema_manual.sql`, then `supabase/schema_learning.sql`, then `supabase/schema_cashplan.sql`. Copy the *Transaction pooler* connection string (Project Settings → Database).
2. **Train the first models** (once, on your machine):
   ```bash
   cd worker
   pip install -r requirements.txt
   cp ../.env.example ../.env        # fill in DATABASE_URL
   python -m crypto_ai.cli research --force   # FRED history + Congress + feeds + prediction markets (needs FRED_API_KEY, CONGRESS_API_KEY in .env)
   python -m crypto_ai.cli train      # downloads ~3 years of free Coinbase candles, ~10 min
   python -m crypto_ai.cli tick       # first predictions + paper trades
   ```
3. **Keep it running** — pick one:
   - *GitHub Actions* (free, needs a PUBLIC repo): push this folder to a repo, add secrets `DATABASE_URL`, `FRED_API_KEY`, `CONGRESS_API_KEY`. `worker.yml` runs `tick` every 15 min. (A private repo only gets 2,000 free Actions min/month, ~2 min/run - that covers hourly, not 15-min. Make the repo public for unlimited free minutes, or widen the cron back out if it must stay private.)
   - *Your own machine* (near real-time): `python -m crypto_ai.cli loop --every 300`
4. **Dashboard**: import the repo in Vercel, root directory `web`, env vars `DATABASE_URL` and `APP_PASSWORD` (not the research keys: the dashboard never uses them). The site refuses to serve without a password.

Local dashboard: `cd web && npm install && npm run dev` (needs `DATABASE_URL`).

## What one `tick` does
collect price/spread/order book/flow → ingest news → detect sudden moves → close paper trades whose horizon ended → score predictions whose window ended → make new predictions if due (default every 6 h) → act on BUY/SELL → snapshot portfolio → recompute metrics.

## The app: five pages
**Dashboard** (BTC/ETH/XRP price cards with 24h change and chart, one AI signal table, news and market impact, your paper portfolio and recent trades) · **Signals** (live signals, prediction history, accuracy, learning) · **News** (research events, sudden moves, source health) · **Trades** (your manual trading, plus the AI's own test account) · **Settings** (settings and system health). Old links redirect.

## "What should I do with the cash?"
Whenever you sell in the Trades page, the app immediately analyses the money that was freed up and saves a recommendation: keep it as cash, buy one coin, or split it. It scores each coin from the current AI signal and confidence, recent news, sudden-event risk and market stress, caps any single coin at 40% of your portfolio, and shows a **safer** option (keep all cash) and a **more aggressive** one (a 60/40 split of the top two). **Nothing is invested until you press a confirm button**; a plan can only run once, and it buys at the live price with the normal fees and slippage. Its advice is only as good as the AI's signals, which are unproven; when every signal is near 50% it says so and defaults to keeping cash.

## Your move + AI autopilot (Trades page)
**Your move** turns the AI signal into advice that fits what you hold: with none of a coin, HOLD becomes **WAIT** ("don't invest now, stay in cash"), BUY stays **BUY**, and SELL/REDUCE become **STAY OUT** (nothing to sell). If you hold it: HOLD / ADD / REDUCE / SELL.

**Autopilot** lets the AI trade your manual paper account all the time, on its own — it runs on a timer (every worker `tick`, and instantly on a sudden event in `watch`) whether or not you're at your computer, not just while you're away. It follows the same current signal the dashboard shows and acts on each signal once: buys 10% of the account (20% if very confident), never more than 40% in one coin or more than your cash, sells all on SELL and half on REDUCE, and ignores an ordinary sell on a position younger than 2 h. Every autopilot trade is tagged and listed under **AI autopilot** with why, plus a "last checked" line that warns if the worker stopped, and win rate / average P&L per sale / profit locked in / open positions so you can judge whether it's actually working. Turn it off with the button there (`autopilot_enabled` setting). Still fake money and unproven signals: it is a test, not a strategy.

## Real-time signals (the Signals tab, "What to do now" on the dashboard)
The system watches BTC/ETH/XRP for a **fast price move** (default 1% inside 5/15/30 min), a **volume spike** (3x with a move, or 6x alone), an extreme **order-book imbalance**, and **major news** (official or importance 60+). On a trigger it investigates (did the other coins move? was there volume? is there news or a regulator announcement?), refreshes the model read, and publishes **BUY / HOLD / REDUCE / SELL** with every scoring term listed. REDUCE = sell about half, SELL = exit; falls count fully, rallies count half (don't chase). Severe negative official news (importance 85+) exits on its own. It is a rule-based overlay: it never edits the logged predictions and is **unproven** (each signal stores its price so outcomes can be measured). `python -m crypto_ai.cli watch` runs it about once a minute; the hourly GitHub job checks once per run.

## Self-learning (the Learning tab)
1. Every prediction is tracked and scored (predictions, results, metrics, health).
2. Every **wrong** prediction gets an append-only **post-mortem**: price path, volume spike, what BTC/the other coins did, events and macro releases in the window, which top drivers pointed the wrong way, what was visible *before* the move began, and the 25 most similar past situations (using only outcomes already known at the time of the call).
3. **Patterns** are statistics over many predictions, never single mistakes: a situation needs 30+ examples each side and a Fisher test corrected for multiple comparisons before it is `confirmed`. Patterns are suggestions for a human; nothing is applied automatically.
4. **Retraining** is attempted only after 30 days AND 100 newly scored predictions, at most once per 14-day window. A challenger trains only on data before the window; champion and challenger are then scored on that same unseen window and the challenger replaces the champion only if it is better in log-loss on both horizons, wins a paired daily sign test, is not worse-calibrated, and beats a coin flip. Every attempt is recorded (`model_challenges`), promoted or not.
`python -m crypto_ai.cli learn [--force-retrain]` runs it by hand; `tick` runs it automatically.

## Manual paper trading (Trades > My trading)
Buy and sell BTC/ETH/XRP any time with fake money, all on one page. **Buy** with a dollar amount (quick $25/$50/$100/Max). **Sell** 25% / 50% / 75% / all, or a dollar amount, from each coin card or the **Your holdings** table, or press **Sell everything** (asks to confirm). Same live price, real spread, 0.4% fee and 0.1% slippage as the AI, no leverage, spot only. It is a **separate account** (its own $1,000) so your trades never change the AI's results. **Profit & loss** shows profit locked in from sales (kept when you rebuy), profit on what you still hold, an average cost that restarts on each new purchase, and a running total per sale. Every trade stores what the AI was saying at that moment. Manual positions never close automatically; you sell them.

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
- **Spot, long-only.** BUY opens a long that exits at the horizon; SELL can only close an existing long (otherwise logged as *skipped*). Shorting would need leverage/margin, which is excluded.
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
python -m crypto_ai.cli train [--days N]   # new model version per coin (old versions kept)
python -m crypto_ai.cli tick               # one full cycle
python -m crypto_ai.cli loop --every 300   # forever
python -m crypto_ai.cli status             # row counts
python -m crypto_ai.cli research [--force] [--source fred_api]   # poll due research sources
python -m crypto_ai.cli research-loop      # forever, each source at its own interval
python -m crypto_ai.cli watch [--every 60]  # near-real-time sudden-event watcher
python -m crypto_ai.cli learn [--force-retrain]  # post-mortems, patterns, guarded retrain check
python -m crypto_ai.cli selfcheck         # read-only end-to-end check of the running system (also see the /health page)
cd worker && python -m pytest              # 70 tests; set TEST_DATABASE_URL to a FRESH SCRATCH Postgres to also run the dedupe + full paper-trading e2e tests
```
