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
    "trade_horizons": [],         # prediction-driven trading is retired: the scalper (scalp_*) is the only thing that opens trades,
    "sudden_move_pct": 0.35,
    "sudden_move_window_min": 15,
    "volume_spike_x": 3.0,
    "trade_variant": "market",
    "autopilot_enabled": True,     # master kill switch for the scalper (paper). Off = no new entries and open trades are flattened
    "retrain_min_days": 30, "retrain_min_new_scored": 100, "retrain_holdout_days": 14, "retrain_min_improvement": 0.002,
    "retrain_max_p_value": 0.10, "pattern_min_examples": 30, "news_trigger_importance": 60, "signal_cooldown_min": 3,
    # LEGACY (only used when legacy_predictions_enabled): the old 1h/24h/48h direction models. They no longer trade.
    "short_prediction_interval_min": 1,           # how often a fresh 1h prediction is logged (capped by real tick cadence)
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
    # ---- short-term scalper (crypto_ai/scalp): 1-minute decisions, net-P&L-trained model, one decision engine ----
    "legacy_predictions_enabled": False,   # the old 1h/24h/48h direction models no longer trade or run in `tick`
    "scalp_hold_bars": 15,             # maximum minutes a trade may live (the ~5-15 minute question)
    "scalp_atr_bars": 14,              # 1m bars in the ATR that sizes stops/targets
    "scalp_stop_mult": 1.0,            # default stop distance = this x unit, unit = ATR% x sqrt(hold bars); policy can override per coin
    "scalp_tp_mult": 1.6,              # default target distance in the same unit
    "scalp_trail_act_r": 0.8,          # trailing stop arms once price is this many multiples of the ORIGINAL risk in profit
    "scalp_trail_mult": 0.7,           # once armed it trails this x unit behind the best price seen
    "scalp_reversal_bars": 3,          # early exit: return over this many 1m bars ...
    "scalp_reversal_mult": 0.5,        # ... this far (x ATR% x sqrt(bars)) against the position ends the trade
    "scalp_nofollow_bars": 4,          # cut a trade that has shown no follow-through after this many minutes ...
    "scalp_nofollow_mfe_r": 0.15,      # ... if it has never been this many R in profit and is under water
    "scalp_min_edge_pct": 0.03,        # predicted NET edge (after fees+slippage) required to enter; the model's validated threshold can only raise it
    "scalp_min_net_target_pct": 0.05,  # the target must clear round-trip costs by at least this much or the trade is rejected outright
    "scalp_allow_shorts": True,        # simulated shorts (paper only; no funding modelled, same fee schedule)
    "scalp_position_pct": 30.0,        # max % of account equity in one position
    "scalp_risk_pct": 0.5,             # a full stop-out never loses more than this % of equity
    "scalp_max_positions": 3,
    "scalp_cooldown_min": 5,           # minutes before the same coin may be re-entered after an exit
    "scalp_daily_loss_limit_pct": 3.0, # no new entries for the rest of the (UTC) day once the day is down this much
    "scalp_max_spread_pct": 0.10,
    "scalp_setup_min_trades": 30, "scalp_setup_block_z": 1.0, "scalp_setup_window_days": 14,   # block a setup only after this many trades AND mean + z*stderr < 0
    "scalp_train_days": 30, "scalp_holdout_days": 5, "scalp_retrain_min_hours": 12,
    # ---- next-candle confirmation: a prediction made at a candle's close only trades if the NEXT candle agrees (see engine.confirm_signal) ----
    "scalp_confirm_required": True,    # False = enter on the prediction alone (kept only so the two behaviours can be compared)
    "scalp_confirm_min_move_atr": 0.25,   # the confirmation candle must close at least this many 1-minute ATRs in the predicted direction ...
    "scalp_confirm_max_adverse_atr": 1.0, # ... must not have traded this far AGAINST it on the way ...
    "scalp_confirm_max_chase_atr": 2.0,   # ... and must not already have run this far (chasing a move that has happened)
    "scalp_confirm_edge_keep": 1.0,    # the model's fresh predicted net edge must still be >= this x the entry threshold
    "scalp_confirm_max_delay_s": 90,   # a confirmation candle older than this (a late or stalled worker) expires the signal: no trade
    # ---- liquidity, exposure and loss limits (the account is never risked blindly) ----
    "scalp_min_liquidity_usd": 50000.0,   # traded dollar volume over the last 15 minutes below this => the coin is too thin to trade right now
    "scalp_max_participation_pct": 1.0,   # a position never exceeds this % of the last 15 minutes' dollar volume
    "scalp_impact_coef": 1.0,          # size-dependent slippage: extra % per side = coef x 1m ATR% x sqrt(position / 15-minute dollar volume)
    "scalp_max_exposure_pct": 60.0,    # total open notional never exceeds this % of equity
    "scalp_max_drawdown_pct": 8.0,     # no new entries while equity is this far below its 7-day high (a cooling-off, not a permanent stop)
    # ---- news context (crypto_ai/scalp/news.py): independent agreeing sources raise confidence, conflicting or opposing news blocks/shrinks the trade ----
    "scalp_news_enabled": True,
    "scalp_news_window_min": 90,       # only news detected this recently counts
    "scalp_news_half_life_min": 45,    # a headline's weight halves every this many minutes
    "scalp_news_min_score": 15.0,      # |net weighted impact| below this = no directional news
    "scalp_news_block_score": 40.0,    # opposing news at least this strong blocks the trade; weaker opposition halves its size
    "scalp_news_conflict_min": 25.0,   # both bullish and bearish weight at least this much (and within 60% of each other) = conflicting sources
    "scalp_min_train_rows": 20000, "scalp_min_oos_trades": 20, "scalp_promote_min_gain_pct": 0.0, "scalp_promote_max_p": 0.20,
    "learn_window_start_h": 5, "learn_window_end_h": 24,       # local hours: learning runs 05:00 through 23:59
    "local_timezone": "",              # IANA name (e.g. America/New_York); empty = LOCAL_TZ env var, else this machine's zone
    # ---- real-time microstructure research (crypto_ai/scalp/micro_*): NOT wired into live trading yet ----
    "maker_fee_pct": 0.4,               # Coinbase does not offer a rebate at typical retail volume - set this to YOUR actual maker tier if lower
    "micro_raw_retention_hours": 48,    # book_ticks/trade_ticks older than this are compacted into micro_bars_1s, then deleted
    "micro_depth_retention_days": 7,
    "micro_fill_horizon_s": 30,         # how long a simulated maker order waits for a fill before it's cancelled (a real "no trade" outcome)
    "micro_hold_after_fill_s": 30,      # how long a position is held (then exits at market) once a maker fill happens, or after a market entry
    # ---- optional AI analysis layer (crypto_ai/llm.py, OpenRouter, $0 models only) - ANALYSIS ONLY, never trading ----
    "llm_enabled": False,               # off by default; only takes effect if OPENROUTER_API_KEY is also set
}

# Settings the dashboard/user may change. Everything else is owned by the code: a database row for any other key
# (left over from an older version, e.g. trade_horizons=[24,48], sudden_move_pct=1.0) is IGNORED instead of silently
# overriding the intended strategy. Add a key here on purpose when a Settings control for it is added.
USER_EDITABLE = {
    "starting_balance", "trading_fee_pct", "slippage_pct", "use_real_spread", "autopilot_enabled",
    "max_spread_pct_to_trade", "scalp_allow_shorts", "scalp_position_pct", "scalp_risk_pct", "scalp_max_positions",
    "scalp_daily_loss_limit_pct", "scalp_min_edge_pct", "local_timezone", "learn_window_start_h", "learn_window_end_h",
    "scalp_max_exposure_pct", "scalp_max_drawdown_pct", "scalp_min_liquidity_usd", "scalp_max_participation_pct",
}   # (scalp_confirm_required is deliberately NOT here: "no confirmation, no trade" is code-owned, a stale row cannot switch it off)
SAFE_DEFAULT_FALSE = {"autopilot_enabled"}   # unparseable kill-switch value => OFF, never "on by accident"

# Hard limits for every value the dashboard (or a hand-edited database row) may set. A typo such as scalp_position_pct=500 or a
# negative fee must never reach the trading engine; out-of-range values are clamped, not rejected, so the worker keeps running.
SETTING_BOUNDS = {
    "starting_balance": (10.0, 1e9), "trading_fee_pct": (0.0, 5.0), "slippage_pct": (0.0, 5.0), "max_spread_pct_to_trade": (0.0, 2.0),
    "scalp_position_pct": (0.5, 50.0), "scalp_risk_pct": (0.05, 2.0), "scalp_max_positions": (1, 3), "scalp_daily_loss_limit_pct": (0.5, 20.0),
    "scalp_min_edge_pct": (0.0, 2.0), "learn_window_start_h": (0, 23), "learn_window_end_h": (1, 24),
    "scalp_max_exposure_pct": (5.0, 100.0), "scalp_max_drawdown_pct": (1.0, 50.0), "scalp_min_liquidity_usd": (0.0, 1e9),
    "scalp_max_participation_pct": (0.01, 10.0),
}

PAPER_ONLY = True   # there is no exchange order code anywhere in this project; see require_paper_only()


def require_paper_only() -> None:
    """Every trade this project makes is simulated. Real trading is not a setting: it would need new, explicitly written
    order code and exchange keys, none of which exist. If someone sets REAL_TRADING_ENABLED expecting it to do something,
    refuse loudly instead of letting them believe real orders are (or are not) being placed."""
    if os.environ.get("REAL_TRADING_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        raise RuntimeError("REAL_TRADING_ENABLED is set, but this project is paper-trading only and has no real-order code. Unset it.")


def as_bool(v, default: bool = False) -> bool:
    """Strict boolean: real bools, JSON strings "true"/"false", 0/1. Anything else falls to `default`.
    (`bool("false")` is True in Python - exactly the bug this exists to prevent.)"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        t = v.strip().strip('"').lower()
        if t in ("true", "t", "1", "yes", "on"):
            return True
        if t in ("false", "f", "0", "no", "off", ""):
            return False
    return default


def merge_settings(db_rows: dict) -> dict:
    """Code defaults, overlaid ONLY by allow-listed database rows, each coerced to the default's type. A value that
    cannot be coerced is dropped (default wins); the kill switch fails to OFF."""
    cfg = dict(DEFAULT_SETTINGS)
    for k, v in db_rows.items():
        if k not in USER_EDITABLE or k not in cfg:
            continue
        d = cfg[k]
        try:
            if isinstance(d, bool):
                cfg[k] = as_bool(v, default=False if k in SAFE_DEFAULT_FALSE else d)
            elif isinstance(d, int):
                cfg[k] = int(float(v))
            elif isinstance(d, float):
                cfg[k] = float(v)
            elif isinstance(d, str):
                cfg[k] = str(v)
            if k in SETTING_BOUNDS and not isinstance(d, (bool, str)):
                lo, hi = SETTING_BOUNDS[k]
                x = cfg[k]
                cfg[k] = d if x != x else type(d)(min(hi, max(lo, x)))          # NaN -> the default; otherwise clamp into range
        except (TypeError, ValueError, OverflowError):
            cfg[k] = False if k in SAFE_DEFAULT_FALSE else d
    return cfg


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    return url
