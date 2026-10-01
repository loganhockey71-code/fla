"""Causal features from 1-minute candles, with 5m and 15m context.

Look-ahead rule (same as ../features.py): row index = the OPEN time of a 1m candle; every value in the row is computed
from candles that had CLOSED by open+1m. A decision made from row t trades at the NEXT candle's open. Higher-timeframe
bars are only used once they have fully closed (aligned by their close time). tests/test_scalp.py proves that changing
future candles never changes an earlier row.
"""
import numpy as np
import pandas as pd

COIN_ID = {"BTC": 0, "ETH": 1, "XRP": 2}
ATR_BARS = 14
LOOKBACK_BARS = 1500          # 1m candles a live decision needs so every feature (24h volatility) is filled in
ONE_MIN = pd.Timedelta(minutes=1)

FEATURES = [
    "ret_1m", "ret_3m", "ret_5m", "ret_15m", "ret_30m", "ret_60m",
    "atr_1m_pct", "sigma_30m", "vol_ratio", "vol_rel_1m", "vol_rel_5m",
    "rsi_1m", "rsi_5m", "macd_hist_5m", "trend_15m", "ret_15m_bar",
    "dist_vwap_15m", "dist_vwap_60m", "dist_high_30m", "dist_low_30m", "range_pos_60m", "efficiency_30m",
    "body_pct", "upper_wick", "lower_wick", "close_loc", "up_bar_share_10m",
    "btc_ret_5m", "btc_ret_15m", "rel_ret_5m", "hour_utc", "day_of_week", "coin_id",
    # lead/lag (majors often move first, alts follow within minutes) and signed-volume proxies from candle shape
    "btc_ret_1m", "btc_ret_3m", "eth_ret_1m", "eth_ret_3m", "rel_ret_1m", "rel_ret_3m", "signed_vol_5m", "signed_vol_15m",
]


def _rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def true_range_pct(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr / prev * 100


def _htf(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Higher-timeframe bars indexed by their CLOSE time (open + minutes), complete bars only."""
    r = df.resample(f"{minutes}min", label="left", closed="left")
    out = pd.DataFrame({"open": r["open"].first(), "high": r["high"].max(), "low": r["low"].min(),
                        "close": r["close"].last(), "volume": r["volume"].sum(), "n": r["close"].count()})
    out = out[out["n"] == minutes].drop(columns="n")           # a partial bar (gap or still forming) is never used
    out.index = out.index + pd.Timedelta(minutes=minutes)
    return out


def _asof_close(series: pd.Series, close_times: pd.DatetimeIndex) -> np.ndarray:
    """Value of a close-time-indexed series as known at each 1m close time (last bar closed at or before it)."""
    return series.reindex(series.index.union(close_times)).ffill().reindex(close_times).to_numpy()


def base_features(df: pd.DataFrame) -> pd.DataFrame:
    """Per-coin features (no cross-asset). `df` = closed 1m candles, ascending, index tz-aware UTC."""
    c, h, l, o, v = df["close"], df["high"], df["low"], df["open"], df["volume"]
    lr = np.log(c).diff()
    f = pd.DataFrame(index=df.index)
    for n in (1, 3, 5, 15, 30, 60):
        f[f"ret_{n}m"] = (c / c.shift(n) - 1) * 100
    f["atr_1m_pct"] = true_range_pct(df).rolling(ATR_BARS).mean()
    f["sigma_30m"] = lr.rolling(30).std() * 100
    f["vol_ratio"] = lr.rolling(30).std() / lr.rolling(1440, min_periods=720).std()
    f["vol_rel_1m"] = v / v.rolling(60).mean()
    v5 = v.rolling(5).sum()
    f["vol_rel_5m"] = v5 / v5.rolling(1440, min_periods=720).mean()
    f["rsi_1m"] = _rsi(c, 14)

    close_times = df.index + ONE_MIN
    b5, b15 = _htf(df, 5), _htf(df, 15)
    if len(b5):
        m = b5["close"].ewm(span=12, adjust=False).mean() - b5["close"].ewm(span=26, adjust=False).mean()
        hist5 = (m - m.ewm(span=9, adjust=False).mean()) / b5["close"] * 100
        f["rsi_5m"] = _asof_close(_rsi(b5["close"], 14), close_times)
        f["macd_hist_5m"] = _asof_close(hist5, close_times)
    else:
        f["rsi_5m"] = f["macd_hist_5m"] = np.nan
    if len(b15):
        fast, slow = b15["close"].rolling(4).mean(), b15["close"].rolling(12).mean()
        f["trend_15m"] = _asof_close((fast / slow - 1) * 100, close_times)
        f["ret_15m_bar"] = _asof_close((b15["close"] / b15["close"].shift(1) - 1) * 100, close_times)
    else:
        f["trend_15m"] = f["ret_15m_bar"] = np.nan

    tp = (h + l + c) / 3
    for n in (15, 60):
        vw = (tp * v).rolling(n).sum() / v.rolling(n).sum()
        f[f"dist_vwap_{n}m"] = (c / vw - 1) * 100
    hi30, lo30, hi60, lo60 = h.rolling(30).max(), l.rolling(30).min(), h.rolling(60).max(), l.rolling(60).min()
    f["dist_high_30m"] = (c / hi30 - 1) * 100
    f["dist_low_30m"] = (c / lo30 - 1) * 100
    f["range_pos_60m"] = (c - lo60) / (hi60 - lo60).replace(0, np.nan)
    net30 = (c - c.shift(30)).abs()
    path30 = c.diff().abs().rolling(30).sum()
    f["efficiency_30m"] = net30 / path30.replace(0, np.nan)
    rng = (h - l).replace(0, np.nan)
    f["body_pct"] = (c - o) / o * 100
    f["upper_wick"] = (h - np.maximum(c, o)) / rng
    f["lower_wick"] = (np.minimum(c, o) - l) / rng
    f["close_loc"] = (c - l) / rng
    f["up_bar_share_10m"] = (c > o).astype(float).rolling(10).mean()
    signed = ((c - o) / rng).fillna(0.0) * v                      # candle-shape proxy for buy-minus-sell volume (no taker split in candles)
    for n in (5, 15):
        f[f"signed_vol_{n}m"] = signed.rolling(n).sum() / v.rolling(n).sum().replace(0, np.nan)
    f["hour_utc"] = df.index.hour
    f["day_of_week"] = df.index.dayofweek
    return f


def compute_features(frames: dict[str, pd.DataFrame], symbol: str, base: dict | None = None) -> pd.DataFrame:
    """Features for `symbol` (adds BTC/ETH cross-asset context). Keeps open/high/low/close/volume for downstream use.
    `base` = optional {symbol: base_features(...)} so a caller doing every coin computes each coin's base features once."""
    base = base if base is not None else {}
    get = lambda s: base[s] if s in base else base.setdefault(s, base_features(frames[s]))
    own = get(symbol)
    out = own.copy()
    for lead, tag, windows in (("BTC", "btc", (1, 3, 5, 15)), ("ETH", "eth", (1, 3))):
        for n in windows:
            out[f"{tag}_ret_{n}m"] = get(lead)[f"ret_{n}m"].reindex(own.index) if lead in frames else np.nan
    out["rel_ret_1m"] = own["ret_1m"] - out["btc_ret_1m"]
    out["rel_ret_3m"] = own["ret_3m"] - out["btc_ret_3m"]
    out["rel_ret_5m"] = own["ret_5m"] - out["btc_ret_5m"]
    out["coin_id"] = COIN_ID[symbol]
    for k in ("open", "high", "low", "close", "volume"):
        out[k] = frames[symbol][k]
    # NOT a model input: how many dollars actually traded in the last 15 minutes. The risk engine sizes against it (a position
    # must be a small share of it) and refuses coins that are too thin. Causal: only closed candles up to this row.
    out["dollar_vol_15m"] = (out["close"] * out["volume"]).rolling(15).sum()
    return out


# ---------------------------------------------------------------- regime + setup tags (pure, per row)
def _ok(x) -> bool:
    return x is not None and np.isfinite(x)


def regime_of(vol_ratio, efficiency) -> str:
    """Market regime at entry: volatility bucket x trend/chop. NaN-safe (unknown -> 'normal_chop')."""
    vr = vol_ratio if _ok(vol_ratio) else 1.0
    er = efficiency if _ok(efficiency) else 0.0
    vol = "high_vol" if vr >= 1.5 else "low_vol" if vr <= 0.7 else "normal"
    return f"{vol}_{'trend' if er >= 0.35 else 'chop'}"


def setup_of(row, direction: int) -> str:
    """Rule-based label for WHY a trade looks attractive; used to track which kinds of setup actually earn money."""
    g = row.get
    d = direction
    vol_up = _ok(g("vol_rel_5m")) and g("vol_rel_5m") >= 1.5
    if d > 0 and _ok(g("dist_high_30m")) and g("dist_high_30m") > -0.01 and vol_up:
        return "breakout_up"
    if d < 0 and _ok(g("dist_low_30m")) and g("dist_low_30m") < 0.01 and vol_up:
        return "breakdown"
    r1, r5, r15 = g("ret_1m"), g("ret_5m"), g("ret_15m")
    if all(_ok(x) for x in (r1, r5, r15)) and d * r1 > 0 and d * r5 > 0 and d * r15 > 0:
        vw = g("dist_vwap_15m")
        if vol_up and _ok(vw) and d * vw > 0:
            return "vwap_break_volume"
        return "momentum"
    rsi = g("rsi_1m")
    if _ok(rsi) and ((d > 0 and rsi < 30) or (d < 0 and rsi > 70)):
        return "reversion"
    return "model_other"
