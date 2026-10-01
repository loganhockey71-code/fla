"""Short-term paper-trading scalper. PAPER TRADING ONLY - there is no exchange/broker order code anywhere.

    costs       fills, fees, slippage, net P&L (one definition used by labels, backtest and live)
    features    causal 1-minute features with 5m/15m context
    labels      trade-outcome labels: what a stop/target/time-limited trade entered NOW would net after costs
    model       LightGBM trained on those net outcomes, walk-forward validated, threshold chosen on net P&L
    engine      THE decision engine: entry gate, adaptive exits, bar-by-bar position management
    backtest    event-driven replay through the same engine
    exit_policy walk-forward search of exit multipliers that is written back and read by the live engine
    learn       post-mortems, setup statistics, guarded retraining (05:00-23:59 local)
    live        the database wrapper `tick` / `scalp` call
"""
