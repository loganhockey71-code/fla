"""Numerical features from 15-minute candles.

Look-ahead rule: a feature row is labelled with `asof` = candle open + 15 min (the moment that
candle CLOSED). Every value in the row is computed from candles that closed at or before `asof`.
tests/test_features.py proves that changing future candles never changes an earlier row.
"""
import numpy as np
import pandas as pd

from .config import BAR

BARS_1H, BARS_4H, BARS_24H = 4, 16, 96

# What the LightGBM model is trained on. Only things that exist in the historical candle record,
# so training and live prediction see exactly the same kind of inputs (no train/serve skew).
MODEL_FEATURES = [
    "ret_15m", "ret_1h", "ret_4h", "ret_24h",
    "vol_rel_1h", "vol_chg_4h",
    "volatility_4h", "volatility_24h", "vol_ratio",
    "rsi_14", "rsi_14h",
    "macd_hist", "macd_hist_1h",
    "dist_sma_20", "dist_sma_96", "sma_20_96",
    "btc_ret_1h", "btc_ret_24h",
    "corr_btc_24h", "corr_eth_24h", "corr_xrp_24h",
]

# Recorded at prediction time but NOT model inputs in v1: Coinbase offers no free historical
# order-book / taker-flow data, so these can only be learned from what we log going forward.
MICROSTRUCTURE_FEATURES = ["ret_1m", "ret_5m", "spread_pct", "ob_imbalance", "buy_pressure", "volume_24h"]

LABELS = {
    "ret_15m": "15m return", "ret_1h": "1h return", "ret_4h": "4h return", "ret_24h": "24h return",
    "vol_rel_1h": "1h volume vs 24h average", "vol_chg_4h": "4h volume change",
    "volatility_4h": "4h volatility", "volatility_24h": "24h volatility", "vol_ratio": "short/long volatility",
    "rsi_14": "RSI(14)", "rsi_14h": "RSI(14h)", "macd_hist": "MACD histogram", "macd_hist_1h": "slow MACD histogram",
    "dist_sma_20": "price vs 5h average", "dist_sma_96": "price vs 24h average", "sma_20_96": "5h vs 24h average",
    "btc_ret_1h": "BTC 1h move", "btc_ret_24h": "BTC 24h move",
    "corr_btc_24h": "correlation with BTC", "corr_eth_24h": "correlation with ETH", "corr_xrp_24h": "correlation with XRP",
}
PCT_FEATURES = {"ret_15m", "ret_1h", "ret_4h", "ret_24h", "btc_ret_1h", "btc_ret_24h", "dist_sma_20",
                "dist_sma_96", "sma_20_96", "macd_hist", "macd_hist_1h", "volatility_4h", "volatility_24h"}


def describe(name: str, value: float) -> str:
    label = LABELS.get(name, name.replace("_", " "))
    if value is None or not np.isfinite(value):
        return f"{label} unavailable"
    if name in PCT_FEATURES:
        return f"{label} {value * 100:+.2f}%"
    return f"{label} {value:.2f}"


def _rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    ru = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    rd = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rsi = 100 - 100 / (1 + ru / rd.replace(0, np.nan))
    return rsi.where(rd != 0, 100.0)


def _macd_hist(close: pd.Series, fast: int, slow: int, sig: int) -> pd.Series:
    macd = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    return (macd - macd.ewm(span=sig, adjust=False).mean()) / close


def compute_features(frames: dict[str, pd.DataFrame], symbol: str) -> pd.DataFrame:
    """frames: symbol -> closed 15m candles (index = open time). Returns features indexed by `asof`."""
    df = frames[symbol]
    idx = df.index
    c, v = df["close"], df["volume"]
    lr = np.log(c).diff()
    f = pd.DataFrame(index=idx)

    f["ret_15m"] = c.pct_change(1)
    f["ret_1h"] = c.pct_change(BARS_1H)
    f["ret_4h"] = c.pct_change(BARS_4H)
    f["ret_24h"] = c.pct_change(BARS_24H)

    v1h, v4h = v.rolling(BARS_1H).sum(), v.rolling(BARS_4H).sum()
    f["vol_rel_1h"] = (v1h / (v.rolling(BARS_24H).sum() / 24)).clip(upper=20)
    f["vol_chg_4h"] = np.log((v4h + 1) / (v4h.shift(BARS_4H) + 1))

    f["volatility_4h"] = lr.rolling(BARS_4H).std()
    f["volatility_24h"] = lr.rolling(BARS_24H).std()
    f["vol_ratio"] = f["volatility_4h"] / f["volatility_24h"]

    f["rsi_14"] = _rsi(c, 14)
    f["rsi_14h"] = _rsi(c, 56)
    f["macd_hist"] = _macd_hist(c, 12, 26, 9)
    f["macd_hist_1h"] = _macd_hist(c, 48, 104, 36)

    sma20, sma96 = c.rolling(20).mean(), c.rolling(96).mean()
    f["dist_sma_20"] = c / sma20 - 1
    f["dist_sma_96"] = c / sma96 - 1
    f["sma_20_96"] = sma20 / sma96 - 1

    btc = frames["BTC"]["close"].reindex(idx)
    f["btc_ret_1h"] = btc.pct_change(BARS_1H)
    f["btc_ret_24h"] = btc.pct_change(BARS_24H)

    for other in ("BTC", "ETH", "XRP"):
        col = f"corr_{other.lower()}_24h"
        if other == symbol:
            f[col] = 1.0
        else:
            olr = np.log(frames[other]["close"].reindex(idx)).diff()
            f[col] = lr.rolling(BARS_24H).corr(olr)

    f = f.replace([np.inf, -np.inf], np.nan)
    f.index = idx + pd.Timedelta(seconds=BAR)  # asof = the moment the candle closed
    f.index.name = "asof"
    f["close"] = c.values
    return f


def make_labels(features: pd.DataFrame, horizon_h: int) -> pd.DataFrame:
    """Forward return over the horizon. Uses future prices by design - ONLY for training targets,
    never for anything fed to a live prediction."""
    steps = horizon_h * 3600 // BAR
    fwd = features["close"].shift(-steps) / features["close"] - 1
    return pd.DataFrame({"fwd_ret": fwd, "y": (fwd > 0).astype(float).where(fwd.notna())})
