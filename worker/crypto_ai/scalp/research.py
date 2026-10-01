"""Is there ANY repeatable short-term edge in this data? A pre-registered, walk-forward research harness.

Protocol (fixed before looking at results, so the answer can't be tuned into existence):
  * dev  = everything except the last 14 days, split in time into halves A and B.  HOLDOUT = the last 14 days, touched ONCE at the end.
  * a candidate is SELECTED on A only, must CONFIRM on B (same sign, enough trades), and only then is scored on the holdout.
  * every variant that was tried is counted and reported: with N tries, some will look good by luck.
  * costs are the real settings (0.4% fee + 0.1% slippage per side + assumed spread). Gross (pre-cost) expectancy is always shown
    next to net, because "is there any directional information at all" and "does it survive costs" are different questions.
"""
import numpy as np
import pandas as pd

from . import costs

DEV_HOLDOUT_DAYS = 14
MIN_TRADES = 30


# ---------------------------------------------------------------- splits
def split(o: pd.DataFrame) -> dict:
    end = o.index.max()
    cut = end - pd.Timedelta(days=DEV_HOLDOUT_DAYS)
    dev = o[o.index <= cut]
    mid = dev.index.min() + (cut - dev.index.min()) / 2
    return {"A": dev[dev.index <= mid], "B": dev[dev.index > mid], "dev": dev, "H": o[o.index > cut]}


# ---------------------------------------------------------------- simple signal sets (long_mask, short_mask), all causal
def _sig(d, long_, short_):
    return np.asarray(long_, bool), np.asarray(short_, bool)


def setups() -> dict:
    S = {}
    for k in (1.5, 2.5, 4.0):
        S[f"momentum_follow z3>={k}"] = lambda d, k=k: _sig(d, (d.z3 >= k) & (d.z1 > 0), (d.z3 <= -k) & (d.z1 < 0))
        S[f"momentum_fade z3>={k}"] = lambda d, k=k: _sig(d, (d.z3 <= -k) & (d.z1 < 0), (d.z3 >= k) & (d.z1 > 0))
    for t in (0.2, 0.3, 0.5):
        S[f"bigmove_follow |1m|>={t}%"] = lambda d, t=t: _sig(d, (d.ret_1m >= t), (d.ret_1m <= -t))
        S[f"bigmove_fade |1m|>={t}%"] = lambda d, t=t: _sig(d, (d.ret_1m <= -t), (d.ret_1m >= t))
    S["breakout+volume (20-bar break, vol5m>=1.5x)"] = lambda d: _sig(d, (d.brk_hi20 == 1) & (d.vol_rel_5m >= 1.5), (d.brk_lo20 == 1) & (d.vol_rel_5m >= 1.5))
    S["breakout_fade (failed break)"] = lambda d: _sig(d, (d.brk_lo20 == 1) & (d.vol_rel_5m >= 1.5), (d.brk_hi20 == 1) & (d.vol_rel_5m >= 1.5))
    for k in (2.0, 3.0):
        vz = lambda d: d.dist_vwap_15m / (d.atr_1m_pct * np.sqrt(15))
        S[f"vwap_reversion |z|>={k}"] = lambda d, k=k, vz=vz: _sig(d, vz(d) <= -k, vz(d) >= k)
        S[f"vwap_trend |z|>={k}"] = lambda d, k=k, vz=vz: _sig(d, vz(d) >= k, vz(d) <= -k)
    S["rsi_reversion (<20 / >80)"] = lambda d: _sig(d, d.rsi_1m < 20, d.rsi_1m > 80)
    S["rsi_momentum (>80 / <20)"] = lambda d: _sig(d, d.rsi_1m > 80, d.rsi_1m < 20)
    S["volume_spike_follow (1m vol>=3x)"] = lambda d: _sig(d, (d.vol_rel_1m >= 3) & (d.ret_1m > 0), (d.vol_rel_1m >= 3) & (d.ret_1m < 0))
    S["volume_spike_fade (1m vol>=3x)"] = lambda d: _sig(d, (d.vol_rel_1m >= 3) & (d.ret_1m < 0), (d.vol_rel_1m >= 3) & (d.ret_1m > 0))
    S["trend_continuation (EMA9>21>50, 15m trend, 2 candles)"] = lambda d: _sig(
        d, (d.ema9_21 > 0) & (d.ema21_50 > 0) & (d.trend_15m > 0) & (d.run_len >= 2), (d.ema9_21 < 0) & (d.ema21_50 < 0) & (d.trend_15m < 0) & (d.run_len <= -2))
    S["run_follow (>=4 same-colour candles)"] = lambda d: _sig(d, d.run_len >= 4, d.run_len <= -4)
    S["run_fade (>=4 same-colour candles)"] = lambda d: _sig(d, d.run_len <= -4, d.run_len >= 4)
    lag = lambda d: (d.symbol != "BTC")
    S["btc_lead_lag (alt lags a BTC 3m move)"] = lambda d: _sig(d, lag(d) & (d.btc_ret_3m >= 0.15) & (d.ret_3m < 0.3 * d.btc_ret_3m), lag(d) & (d.btc_ret_3m <= -0.15) & (d.ret_3m > 0.3 * d.btc_ret_3m))
    S["btc_lead_lag_1m (alt lags a BTC 1m move)"] = lambda d: _sig(d, lag(d) & (d.btc_ret_1m >= 0.08) & (d.ret_1m < 0.3 * d.btc_ret_1m), lag(d) & (d.btc_ret_1m <= -0.08) & (d.ret_1m > 0.3 * d.btc_ret_1m))
    return S


# ---------------------------------------------------------------- trade selection + statistics
def take(d: pd.DataFrame, long_m, short_m, h: int) -> pd.DataFrame:
    """One trade at a time PER COIN (a new entry only after the previous one's hold has ended). Fixed-horizon exit at the close of
    minute h. Returns one row per trade: ts, symbol, direction, gross %, net %, MFE %, MAE %, regime."""
    sym_arr = d["symbol"].to_numpy()
    rows = []
    for sym in np.unique(sym_arr):
        sel = np.flatnonzero(sym_arr == sym)
        lm, sm = long_m[sel], short_m[sel]
        cand = np.flatnonzero(lm | sm)
        g = d.iloc[sel]
        k = 0
        while k < len(cand):
            i = cand[k]
            is_long = lm[i]
            p = "long" if is_long else "short"
            r = g.iloc[i]
            rows.append((g.index[i], sym, p, r[f"{p}_gross_{h}"], r[f"{p}_net_{h}"], r[f"{p}_mfe_{h}"], r[f"{p}_mae_{h}"], r["regime"]))
            k = np.searchsorted(cand, i + h + 1)
    return pd.DataFrame(rows, columns=["ts", "symbol", "dir", "gross", "net", "mfe", "mae", "regime"])


def stats(t: pd.DataFrame, days: float | None = None) -> dict:
    n = len(t)
    if n == 0:
        return {"n": 0, "win": np.nan, "gross": np.nan, "net": np.nan, "pf": np.nan, "total": 0.0, "dd": 0.0, "per_day": 0.0}
    x = t["net"].to_numpy()
    w, l = x[x > 0].sum(), -x[x <= 0].sum()
    eq = np.cumsum(x[np.argsort(t["ts"].to_numpy(), kind="stable")])
    return {"n": n, "win": float((x > 0).mean()), "gross": float(t["gross"].mean()), "net": float(x.mean()), "pf": float(w / l) if l else np.inf,
            "total": float(x.sum()), "dd": float((np.maximum.accumulate(eq) - eq).max()), "per_day": n / days if days else np.nan}


def days_of(d: pd.DataFrame) -> float:
    return max((d.index.max() - d.index.min()).total_seconds() / 86400, 1e-9)


# ---------------------------------------------------------------- vectorised path simulator (stop / target / trail / time) on candle arrays
def simulate_paths(arr: dict, pos: np.ndarray, d: np.ndarray, h: int, sl: float, tp: float, trail: bool, cfg: dict, spread: float) -> dict:
    """Entry at the OPEN of bar pos+1; per bar: gap/stop first, then target (stop wins a same-candle touch), then excursion + trailing update
    at the close (same semantics as engine.step_position). sl/tp in % of entry; trail arms at 0.8R and trails 0.7 x initial risk.
    d = direction array (+1/-1). Returns net %, gross %, exit code (1 stop, 2 target, 3 time, 4 trail) arrays."""
    o, hi, lo, cl = arr["o"], arr["h"], arr["l"], arr["c"]
    m = len(pos)
    e = o[pos + 1]
    stop = e * (1 - d * sl / 100)
    tgt = e * (1 + d * tp / 100)
    best = e.copy()
    exit_mid = np.full(m, np.nan)
    code = np.zeros(m, int)
    alive = np.ones(m, bool)
    risk = np.abs(e - stop)
    trailing = np.zeros(m, bool)
    for j in range(h):
        b = pos + 1 + j
        bo, bh, bl, bc = o[b], hi[b], lo[b], cl[b]
        stop_hit = np.where(d > 0, bl <= stop, bh >= stop) & alive
        tp_hit = np.where(d > 0, bh >= tgt, bl <= tgt) & alive & ~stop_hit
        gap = np.where(d > 0, np.minimum(stop, bo), np.maximum(stop, bo))
        exit_mid[stop_hit] = gap[stop_hit]
        code[stop_hit] = np.where(trailing[stop_hit], 4, 1)
        exit_mid[tp_hit] = tgt[tp_hit]
        code[tp_hit] = 2
        alive &= ~(stop_hit | tp_hit)
        fav = np.where(d > 0, bh, bl)
        best = np.where(alive, np.where(d > 0, np.maximum(best, fav), np.minimum(best, fav)), best)
        if trail:
            armed = alive & (d * (best - e) >= 0.8 * risk)
            trailing |= armed
            cand = best * (1 - d * 0.7 * (risk / e) )
            better = armed & np.where(d > 0, cand > stop, cand < stop)
            stop = np.where(better, cand, stop)
        if j == h - 1:
            t = alive
            exit_mid[t] = bc[t]
            code[t] = 3
    sl_pct, fee = cfg["slippage_pct"], cfg["trading_fee_pct"]
    gross = d * (exit_mid / e - 1) * 100
    net = np.where(d > 0,
                   costs.net_pct(costs.fill(e, 1, True, spread, sl_pct), costs.fill(exit_mid, 1, False, spread, sl_pct), 1, fee),
                   costs.net_pct(costs.fill(e, -1, True, spread, sl_pct), costs.fill(exit_mid, -1, False, spread, sl_pct), -1, fee))
    return {"net": net, "gross": gross, "code": code}


def frame_arrays(frames: dict) -> dict:
    return {s: {"o": f["open"].to_numpy(), "h": f["high"].to_numpy(), "l": f["low"].to_numpy(), "c": f["close"].to_numpy(), "idx": f.index} for s, f in frames.items()}


def trades_with_exits(d: pd.DataFrame, long_m, short_m, h: int, sl: float, tp: float, trail: bool, arrays: dict, cfg: dict) -> pd.DataFrame:
    """Like `take`, but exits by stop/target/trail/time instead of a fixed-horizon close. One trade at a time per coin (hold = h)."""
    sym_arr = d["symbol"].to_numpy()
    rows = []
    for sym in np.unique(sym_arr):
        sel = np.flatnonzero(sym_arr == sym)
        lm, sm = long_m[sel], short_m[sel]
        cand = np.flatnonzero(lm | sm)
        keep, k = [], 0
        while k < len(cand):
            keep.append(cand[k])
            k = np.searchsorted(cand, cand[k] + h + 1)
        if not keep:
            continue
        keep = np.array(keep)
        ts = d.index[sel][keep]
        a = arrays[sym]
        pos = a["idx"].get_indexer(ts)
        ok = (pos >= 0) & (pos + 1 + h < len(a["c"]))
        pos, ts, keep = pos[ok], ts[ok], keep[ok]
        dirn = np.where(lm[keep], 1, -1)
        r = simulate_paths(a, pos, dirn, h, sl, tp, trail, cfg, costs.ASSUMED_SPREAD_PCT.get(sym, 0.0))
        rows.append(pd.DataFrame({"ts": ts, "symbol": sym, "dir": np.where(dirn > 0, "long", "short"), "gross": r["gross"], "net": r["net"],
                                  "exit": r["code"], "regime": d["regime"].to_numpy()[sel][keep]}))
    return pd.concat(rows) if rows else pd.DataFrame(columns=["ts", "symbol", "dir", "gross", "net", "exit", "regime"])
