import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
load_dotenv()

SYMBOLS = ["BTC", "ETH", "XRP"]
PRODUCTS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "XRP": "XRP-USD"}
COINGECKO_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "XRP": "ripple"}
HORIZONS = [1, 24, 48]  # 1h added for a fast/short-term signal; 24/48h are the original, evaluated horizons
SHORT_HORIZON_H = 1
BAR = 900  # seconds; the model works on 15-minute candles
TRAIN_DAYS = 1095  # ~3 years of free Coinbase history: more market regimes than one ~9-month stretch

# Mirrors the `settings` table defaults so the code works before anything is edited in the UI.
DEFAULT_SETTINGS = {
    "starting_balance": 1000.0,
    "trading_fee_pct": 0.4,
    "slippage_pct": 0.10,
    "use_real_spread": True,
    "normal_position_pct": 10.0,
    "high_conf_position_pct": 20.0,
    "high_confidence_threshold": 75.0,
    "signal_threshold_pct": 52.0,
    "prediction_interval_h": 6.0,
    "max_spread_pct_to_trade": 0.30,
    "hold_band_pct_1h": 0.4,
    "hold_band_pct_24h": 1.5,
    "hold_band_pct_48h": 2.0,
    "trade_horizons": [1],
    "sudden_move_pct": 0.35,
    "sudden_move_window_min": 15,
    "volume_spike_x": 3.0,
    "trade_variant": "market",
    "autopilot_enabled": True,     # the AI trades the manual paper account while you are away (turn off in Settings)
    "retrain_min_days": 30, "retrain_min_new_scored": 100, "retrain_holdout_days": 14, "retrain_min_improvement": 0.002,
    "retrain_max_p_value": 0.10, "pattern_min_examples": 30, "news_trigger_importance": 60, "signal_cooldown_min": 3,
    # Short-horizon (1h) signal: an earlier backtest found no profitable threshold on it after fees, so it used to
    # be logged only. Now driving trade_horizons AND the "standing" read (publish_standing/_fresh_model_read) on
    # purpose, at the user's explicit request for many more (and likely less profitable) trades a day instead of
    # a handful. This is a known trade-off, not a new backtest result - watch prediction_results/paper_trades to
    # see whether it's actually worth it, and dial short_prediction_interval_min/sudden_move_pct back down if not.
    "short_prediction_interval_min": 1,           # how often a fresh 1h prediction is logged (capped by real tick cadence)
    "autopilot_trade_window_enabled": False,      # off by default now: trade around the clock instead of a UTC window
    "autopilot_trade_window_start_h": 0,          # UTC hour. Adjust these two if you want your own local hours instead.
    "autopilot_trade_window_end_h": 17,
    # ---- risk management (see risk.py): volatility-sized stops/targets, trailing stops, risk-based sizing ----
    "atr_period_bars": 14,             # 15m bars averaged for the ATR% used to size stops/targets
    "stop_loss_atr_mult": 1.5,         # stop distance = this x ATR%
    "take_profit_atr_mult": 2.5,       # target distance = this x ATR% (ratio to the stop mult is the "expected" reward:risk)
    "min_reward_risk_ratio": 1.5,      # has_edge() rejects a trade whose planned reward:risk (after costs) falls short
    "trailing_activation_r": 1.0,      # trailing stop arms once price is this many multiples of the ORIGINAL risk in profit
    "trailing_atr_mult": 1.0,          # once armed, the stop trails this x ATR% behind the highest price seen
    "momentum_reversal_lookback_bars": 3,   # early-exit check: return over this many 15m bars
    "momentum_reversal_atr_mult": 0.5,      # ... counts as a reversal once it's this many ATRs against the position
    "max_risk_pct_per_trade": 1.0,     # position sizing never risks more than this % of the account on one stop-out
}


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    return url
