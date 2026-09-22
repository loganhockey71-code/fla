import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
load_dotenv()

SYMBOLS = ["BTC", "ETH", "XRP"]
PRODUCTS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "XRP": "XRP-USD"}
COINGECKO_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "XRP": "ripple"}
HORIZONS = [24, 48]
BAR = 900  # seconds; the model works on 15-minute candles

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
    "hold_band_pct_24h": 1.5,
    "hold_band_pct_48h": 2.0,
    "trade_horizons": [24, 48],
    "sudden_move_pct": 1.0,
    "sudden_move_window_min": 15,
    "volume_spike_x": 3.0,
    "trade_variant": "market",
    "autopilot_enabled": True,     # the AI trades the manual paper account while you are away (turn off in Settings)
    "retrain_min_days": 30, "retrain_min_new_scored": 100, "retrain_holdout_days": 14, "retrain_min_improvement": 0.002,
    "retrain_max_p_value": 0.10, "pattern_min_examples": 30, "news_trigger_importance": 60, "signal_cooldown_min": 30,
}


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    return url
