"""`python -m crypto_ai.cli micro-research`: builds the microstructure research dataset from whatever has been
collected (`ws-collect`), then tests the 8 simple strategies with the SAME chronological A/B + sealed-holdout
protocol as `scalp-research` (crypto_ai.scalp.research.split, reused as-is - it only needs a datetime index) - and
compares market-order vs maker-limit-order vs no-trade for the candidates each strategy selects.

Refuses to pretend a conclusion is possible when there simply is not enough data yet: prints how much was collected
and what is still needed instead of running (and definitely instead of tuning) a backtest on a few hours of data.
"""
import numpy as np
import pandas as pd

from . import maker, micro_compact, micro_features as MF, micro_setups, research as R

MIN_HOURS_FOR_SPLIT = 24     # below this, only descriptive stats are shown - no A/B/holdout backtest is run
HOLD_SECONDS = [1, 5, 10, 30, 60]
MIN_TRADES = 20


def _show(df):
    with pd.option_context("display.width", 240, "display.max_rows", 300):
        print(df.round(4).to_string(index=False))


# ---------------------------------------------------------------- loading (DB)
def load_all(db, symbols) -> dict:
    bars, depth, trades = {}, {}, {}
    for s in symbols:
        compacted = db.all("select symbol, ts, mid_open, mid_high, mid_low, mid_close, spread_avg, spread_max, n_quotes, n_trades, buy_volume, sell_volume, vwap "
                           "from micro_bars_1s where symbol=%s order by ts", [s])
        raw_q = db.all("select symbol, ts, mid, spread_pct from book_ticks where symbol=%s order by ts", [s])
        raw_t = db.all("select symbol, ts, price, size, aggressor from trade_ticks where symbol=%s order by ts", [s])
        rq = pd.DataFrame(raw_q, columns=["symbol", "ts", "mid", "spread_pct"])
        rt = pd.DataFrame(raw_t, columns=["symbol", "ts", "price", "size", "aggressor"])
        for df in (rq, rt):
            if len(df):
                df["ts"] = pd.to_datetime(df["ts"], utc=True)
        fresh = micro_compact.build_1s_bars(rq, rt)
        b = pd.concat([pd.DataFrame(compacted), fresh], ignore_index=True) if compacted else fresh
        if len(b):
            b["ts"] = pd.to_datetime(b["ts"], utc=True)
            b = b.sort_values("ts").drop_duplicates("ts", keep="last")
        bars[s] = b
        d = db.all("select symbol, ts, imbalance_5bp, imbalance_25bp, bid_depth_5bp, ask_depth_5bp from depth_snapshots where symbol=%s order by ts", [s])
        depth[s] = pd.DataFrame(d, columns=["symbol", "ts", "imbalance_5bp", "imbalance_25bp", "bid_depth_5bp", "ask_depth_5bp"])
        if len(depth[s]):
            depth[s]["ts"] = pd.to_datetime(depth[s]["ts"], utc=True)
        trades[s] = rt.sort_values("ts").reset_index(drop=True)
    return {"bars": bars, "depth": depth, "trades": trades}


def coverage(loaded: dict) -> dict:
    out = {}
    for s, b in loaded["bars"].items():
        if len(b):
            out[s] = {"first": b["ts"].min(), "last": b["ts"].max(), "hours": (b["ts"].max() - b["ts"].min()).total_seconds() / 3600, "seconds_with_activity": len(b)}
        else:
            out[s] = {"first": None, "last": None, "hours": 0.0, "seconds_with_activity": 0}
    return out


# ---------------------------------------------------------------- signal selection + evaluation
def take_signals(df: pd.DataFrame, long_m, short_m, h: int) -> pd.DataFrame:
    """One entry at a time per symbol (next entry only after this one's h-second hold ends)."""
    sym_arr = df["symbol"].to_numpy()
    rows = []
    for sym in np.unique(sym_arr):
        sel = np.flatnonzero(sym_arr == sym)
        lm, sm = long_m[sel], short_m[sel]
        cand = np.flatnonzero(lm | sm)
        g = df.iloc[sel]
        k = 0
        while k < len(cand):
            i = cand[k]
            is_long = lm[i]
            gl = g[f"gross_long_{h}s"].iloc[i]
            rows.append((g.index[i], sym, "long" if is_long else "short", gl if is_long else -gl, g["mid_close"].iloc[i], g["spread_pct"].iloc[i]))
            k = np.searchsorted(cand, i + h + 1)
    return pd.DataFrame(rows, columns=["ts", "symbol", "dir", "gross", "mid0", "spread0"])


def net_taker(entries: pd.DataFrame, cfg: dict) -> pd.Series:
    from . import costs
    d = np.where(entries["dir"] == "long", 1, -1)
    ef = costs.fill(entries["mid0"].to_numpy(), d, True, entries["spread0"].to_numpy(), cfg["slippage_pct"])
    exit_mid = entries["mid0"].to_numpy() * (1 + entries["gross"].to_numpy() / 100 * d)   # gross already direction-signed vs entry mid
    xf = costs.fill(exit_mid, d, False, entries["spread0"].to_numpy(), cfg["slippage_pct"])
    return pd.Series(costs.net_pct(ef, xf, d, cfg["trading_fee_pct"]), index=entries.index)


def evaluate_execution(entries: pd.DataFrame, loaded: dict, cfg: dict) -> pd.DataFrame:
    """market vs maker vs no-trade for a (small) set of selected entries - the only place the maker-fill simulator
    is actually run, since it needs the raw trade stream per entry."""
    out = []
    for sym, grp in entries.groupby("symbol"):
        trades = loaded["trades"].get(sym, pd.DataFrame(columns=["ts", "price", "size", "aggressor"]))
        depth = loaded["depth"].get(sym, pd.DataFrame(columns=["ts", "bid_depth_5bp", "ask_depth_5bp"]))
        bars = loaded["bars"].get(sym)
        mid_series = MF.regular_grid(bars).set_index("ts")["mid_close"] if bars is not None and len(bars) else pd.Series(dtype=float)
        for r in grp.itertuples():
            side = "buy" if r.dir == "long" else "sell"
            half_spread = r.spread0 / 200 * r.mid0
            limit_price = r.mid0 - half_spread if side == "buy" else r.mid0 + half_spread
            di = depth[depth.ts <= r.ts]
            qa = (di["bid_depth_5bp"].iloc[-1] if side == "buy" else di["ask_depth_5bp"].iloc[-1]) if len(di) else 0.0
            res = maker.compare_execution(side, r.ts, r.mid0, r.spread0, limit_price, qa or 0.0, trades, mid_series, cfg,
                                          cfg["micro_fill_horizon_s"], cfg["micro_hold_after_fill_s"])
            out.append({"ts": r.ts, "symbol": sym, "dir": r.dir, **res})
    return pd.DataFrame(out)


def summarize_execution(ex: pd.DataFrame) -> dict:
    if ex.empty:
        return {"n": 0}
    fill_rate = ex["maker_filled"].mean()
    filled = ex[ex["maker_filled"]]
    return {"n": len(ex), "fill_rate": float(fill_rate), "avg_market_net_pct": float(ex["market_net_pct"].mean()),
            "avg_maker_net_pct_if_filled": float(filled["maker_net_pct"].mean()) if len(filled) else None,
            "avg_maker_net_pct_incl_no_fill": float(ex["maker_net_pct"].fillna(0.0).mean()),
            "avg_time_to_fill_s": float(filled["maker_time_to_fill_s"].mean()) if len(filled) else None,
            "maker_beats_market_rate": float((filled["maker_net_pct"] > ex.loc[filled.index, "market_net_pct"]).mean()) if len(filled) else None}


# ---------------------------------------------------------------- report
def run(db, cfg: dict, symbols, log=print) -> dict:
    loaded = load_all(db, symbols)
    cov = coverage(loaded)
    log("Coverage collected so far:")
    for s, c in cov.items():
        log(f"  {s}: {c['hours']:.2f}h, {c['seconds_with_activity']:,} active seconds" + (f" ({c['first']} -> {c['last']})" if c["first"] is not None else " (nothing yet)"))
    total_hours = max((c["hours"] for c in cov.values()), default=0.0)
    result = {"coverage": cov}
    causal = MF.build(loaded["bars"], loaded["depth"], with_outcomes=False)
    if causal.empty:
        log("\nNo usable seconds yet - run `ws-collect` first (it needs to actually observe a quote or a trade).")
        return result
    bstats = {s: MF.bounce_stats(causal[causal.symbol == s]) for s in symbols if (causal.symbol == s).any()}
    log("\nBid-ask-bounce diagnostics (lag-1 autocorrelation of 1-second mid returns; negative = bounce/reversal-dominated):")
    for s, b in bstats.items():
        log(f"  {s}: n={b['n']}, autocorr={b['autocorr_lag1']}, sign-flip share={b['flip_share']}")
    result["bounce"] = bstats
    if total_hours < MIN_HOURS_FOR_SPLIT:
        log(f"\nOnly {total_hours:.2f}h collected (need >= {MIN_HOURS_FOR_SPLIT}h for a meaningful chronological A/B + holdout split).")
        log("Reporting descriptive stats only - NOT running the backtest yet, and NOT concluding anything about edge.")
        rows = []
        for sym in symbols:
            sub = causal[causal.symbol == sym]
            feasible = [h for h in HOLD_SECONDS if h < len(sub) - 2]
            if not feasible:
                continue
            o = MF.add_outcomes(sub, horizons=feasible)
            for h in feasible:
                gcol = f"gross_long_{h}s"
                rows.append({"symbol": sym, "hold_s": h, "n_rows": len(o), "avg |move| %": o[gcol].abs().mean(), "P(move>=0.05%)": (o[gcol].abs() >= 0.05).mean()})
        if rows:
            _show(pd.DataFrame(rows))
        else:
            log(f"(not even {HOLD_SECONDS[0]}s of usable forward-looking seconds yet - this is a pipeline check, not a research run)")
        result["status"] = "insufficient_data"
        return result
    df = MF.build(loaded["bars"], loaded["depth"], with_outcomes=True)
    sp = R.split(df)
    S = micro_setups.setups()
    rows = []
    all_entries = {}
    for name, fn in S.items():
        for h in HOLD_SECONDS:
            if f"gross_long_{h}s" not in sp["A"].columns:
                continue
            lmA, smA = fn(sp["A"])
            tA = take_signals(sp["A"], lmA, smA, h)
            if len(tA) < MIN_TRADES:
                continue
            tA["net"] = net_taker(tA, cfg)
            lmB, smB = fn(sp["B"])
            tB = take_signals(sp["B"], lmB, smB, h)
            tB["net"] = net_taker(tB, cfg) if len(tB) else tB.get("net", pd.Series(dtype=float))
            sA, sB = R.stats(tA.rename(columns={"gross": "gross"}).assign(net=tA["net"]), R.days_of(sp["A"])), R.stats(tB.assign(net=tB["net"]) if len(tB) else tB, R.days_of(sp["B"]) if len(tB) else 1)
            rows.append({"setup": name, "hold_s": h, "n_A": sA["n"], "gross_A": sA["gross"], "net_A": sA["net"], "n_B": sB["n"], "gross_B": sB["gross"], "net_B": sB["net"]})
            all_entries[(name, h)] = (tA, tB)
    result["baselines"] = pd.DataFrame(rows)
    log(f"\n{len(rows)} setup x hold_s variants with >= {MIN_TRADES} trades on A:")
    if rows:
        _show(result["baselines"].sort_values("gross_B", ascending=False).head(20))
    stable = result["baselines"][(result["baselines"].n_B >= MIN_TRADES) & (np.sign(result["baselines"].net_A) == np.sign(result["baselines"].net_B)) & (result["baselines"].net_A > 0)]
    log(f"\nCandidates with the SAME-SIGN positive net (taker) expectancy in both A and B: {len(stable)}")
    if len(stable):
        _show(stable)
        best_key = (stable.iloc[0]["setup"], int(stable.iloc[0]["hold_s"]))
        tA, tB = all_entries[best_key]
        H = sp["H"]
        lmH, smH = S[best_key[0]](H)
        tH = take_signals(H, lmH, smH, best_key[1])
        log(f"\nBest candidate '{best_key[0]}' @ {best_key[1]}s on the SEALED holdout: n={len(tH)}")
        if len(tH) >= 5:
            tH["net"] = net_taker(tH, cfg)
            _show(pd.DataFrame([R.stats(tH, R.days_of(H))]))
            ex = evaluate_execution(tH, loaded, cfg)
            summary = summarize_execution(ex)
            log("\nMarket vs maker vs no-trade on those same holdout entries:")
            log(str(summary))
            result["holdout"] = {"stats": R.stats(tH, R.days_of(H)), "execution": summary}
    else:
        log("No setup had the same-sign positive taker expectancy in both halves: nothing qualifies for the sealed holdout.")
        result["holdout"] = None
    result["status"] = "evaluated"
    return result
