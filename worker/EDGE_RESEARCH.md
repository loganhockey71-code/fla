# Short-term edge research (1-15 minute holds, BTC/ETH/XRP)

Reproduce: `cd worker && python -m crypto_ai.cli scalp-research` (sections: facts, timing, ic, baselines, grid, model, holdout).
Data: 75 days of real Coinbase 1-minute candles to 2026-09-29 (108k bars per coin). Costs: your real settings, 0.4% fee + 0.1% slippage per side
plus an assumed spread (BTC 0.01%, ETH 0.02%, XRP 0.05%) = **about 1.0% per round trip**. Nothing was lowered.

**Protocol.** Development = first ~61 days, split in time into halves A and B. Holdout = last 14 days, sealed until the final step.
A candidate is selected on A only, must confirm on B, and is scored on the holdout once. Everything tried is counted.
Dataset: `opportunities.py` (321,789 opportunities x 120 columns: every bar, both directions, MFE/MAE at 1/2/3/5/10/15 min, gross and net return,
target-before-stop flags, best possible target, worst adverse move, all features, regime).

## Verdict
There is a **real but tiny** short-term mean-reversion signal (gross), and **no edge after costs**. The signal is 10-50x smaller than the cost of trading it.
No setup, hold, stop, target, trailing choice, asset, direction or regime had positive expectancy at the real costs in any partition.

## 1. The cost hurdle (development period, random entries)
| coin | avg best move in 5 min | P(best move >= 1% within 5 min) | net per random trade |
|---|---|---|---|
| BTC | 0.064% | 0.06% | -1.008% |
| ETH | 0.088% | 0.16% | -1.017% |
| XRP | 0.117% | 0.57% | -1.045% |
Even with a perfect exit, a 5-minute trade needs a move roughly 10x the typical one just to break even.

## 2. Entry timing (are we late?)
After a >=0.5% 1-minute move, *following* it earns +0.05-0.10% gross over 1-3 minutes (n=402 events in 61 days, ~6.6/day); *fading* it is negative there.
After 0.2-0.3% moves the drift is ~0 (+0.006%). Across all features the dominant information is the opposite: **extension predicts reversal**.
Entering after the move is not the reason for failure: the move that has already happened is worth less than the round-trip cost either way.

## 3. Feature information (rank IC with next 1/3/5-minute gross return; A / B / sealed holdout)
Consistent sign in both halves, all three coins, all horizons: `rsi_1m` (-0.043 / -0.024; holdout -0.030), `dist_vwap_60m` (-0.037 / -0.025; holdout -0.039),
`dist_ema9`, `range_pos_60m` (holdout -0.035), `ema9_21`, `ret_60m`, `rsi_5m`, `dist_vwap_15m`, `z5`. 26 of 41 features kept the same sign in A and B. All of them say
"the more extended the price, the more it gives back". IC of 0.03 is real information; it is worth a few hundredths of a percent per trade.
Not useful: volume acceleration, candle body/wick, consecutive candles, BTC lead/lag (weak, unstable), signed-volume proxies.

## 4. Simple baselines vs LightGBM
162 setup x hold variants (fixed exit), 114 with >=30 trades in each half: 15 had positive GROSS in both halves, **0 had positive NET**; best net was -0.76% per trade.
Best gross (weaker half): bigmove_fade >=0.2% (10m) +0.025%, bigmove_fade >=0.3% (2m) +0.006%, btc_lead_lag (3m) +0.006%, breakout_fade (10m) +0.004%.
**LightGBM is not better than the simple features.** Trained on A, scored on B: rank IC 0.004-0.017. Trained on all dev, scored on the sealed holdout: IC 0.008-0.026,
top 0.1% of signals +0.01 to +0.14% gross on ~60 trades. A single RSI or VWAP-distance feature has IC -0.03 to -0.039 on the same holdout. A one-line rule beats the model.

## 5. Exit structure (stop, target, trailing, hold)
2,240 combinations across 14 setups: hold {3,5,10,15} x stop {0.15,0.3,0.6,1.0}% x target {0.3,0.6,1.0,1.5,2.5}% x trailing {off,on}, selected on A by NET expectancy.
Best net on A was -0.98% per trade; on B every one was negative (-0.94% to -1.08%). Win rates ~0%: with a 1% cost a trade essentially needs a >1% favourable move.
MFE/MAE: for the follow-a-0.5%-move setup, average best excursion +0.72% vs worst -0.48% over 5 minutes, but the close captures only ~10% of it (+0.07%).
Exit structure moves gross by hundredths of a percent; it cannot fix a 1% cost. (Early exits on momentum reversal look bad only because they realise costs on a
selected set of already-losing trades; time-stop survivors look good for the same selection reason. Neither is an edge.)

## 6. Sealed holdout (last 14 days), structures selected on A by gross expectancy
| Setup (best structure from A) | Trades | Win rate | Avg gross | Avg net | Profit factor | Net (sum of % on notional) | Trades/day |
|---|---:|---:|---:|---:|---:|---:|---:|
| bigmove_fade >=0.2%, 10m, stop 1.0% / target 1.0% | 838 | ~0% | +0.021% | -1.019% | 0.00 | -854% | 60 |
| breakout_fade, 15m, 1.0% / 0.3% | 699 | ~0% | +0.022% | -1.005% | 0.00 | -703% | 50 |
| rsi_reversion, 15m, 1.0% / 1.0% | 208 | ~0% | +0.043% | -0.984% | 0.00 | -205% | 15 |
| momentum_follow z3>=1.5, 10m, 0.15% / 0.3% | 1,360 | ~0% | +0.010% | -1.015% | 0.00 | -1,380% | 97 |
| breakout+volume, 15m, trail | 699 | ~0% | +0.009% | -1.018% | 0.00 | -712% | 50 |
| volume_spike_fade, 15m, 1.0% / 0.6% | 1,589 | ~0% | +0.009% | -1.019% | 0.00 | -1,619% | 114 |
Holdout gross was positive for 13 of 16 structures (+0.002% to +0.043%; 2 slightly negative, 1 flat): the reversion signal generalises. Net was negative for all 16.

## 7. Slices (development period, real costs, fixed 10-minute exit for bigmove_fade >=0.2%)
| slice | trades/day | avg gross | avg net |
|---|---:|---:|---:|
| all | 36 | +0.026% | -1.010% |
| BTC / ETH / XRP | 5 / 10 / 21 | +0.022% / +0.006% / +0.038% | -0.989% / -1.015% / -1.012% |
| long (buy dips) / short (sell spikes) | 17 / 19 | +0.050% / +0.005% | -0.985% / -1.032% |
| high_vol_trend regime | 4 | +0.093% (A +0.107%, B +0.084%) | -0.934% |
| high_vol_chop / normal_chop / normal_trend | 8 / 16 / 5 | +0.020% / +0.011% / +0.025% | ~-1.01% |
Strongest evidence: long side (dip buying), high-volatility trending regimes, XRP. All of it is gross.

## 8. What breaks even
The best slices earn about +0.02% to +0.10% gross per trade. A round trip must therefore cost **under ~0.02-0.10%** to break even: a 0.24% "cheap" tier loses ~0.2% per trade,
0.12% loses ~0.09%. That is below the quoted spread of XRP (0.05% assumed). Also note the coin with the widest spread (XRP) shows the largest "edge": a 1-minute mean reversion in
last-trade prices is partly **bid-ask bounce**, which a taker pays for rather than earns. Whether any of it is capturable with resting (maker) orders cannot be tested without order-book history.

## 9. Still missing from the data
Historical bid/ask and full order-book depth (the bounce vs true-reversion question, and queue position for maker fills); trade-by-trade taker-side flow; funding/liquidation data;
tick data (1-minute candles hide the path inside a minute); realistic maker-fee and rebate assumptions; more than 75 days (only ~3 volatile episodes). The worker already stores
ticker/order-book snapshots with each tick, but at 5-minute spacing that is too sparse to backtest.
