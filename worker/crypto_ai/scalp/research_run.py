"""`python -m crypto_ai.cli scalp-research`: the whole edge investigation, reproducibly, on cached 1-minute candles.

Sections (each prints plain tables; the holdout is only touched by `holdout`, once, at the end):
  facts     how big are 1-15 minute moves versus what a round trip costs?
  timing    does following or fading a big 1-minute move pay (entry timing)?
  ic        which features carry ANY rank information about the next 1-5 minutes, and is it stable across halves A and B?
  baselines every simple setup x holding period (fixed exit), halves A and B, gross vs net
  grid      entry x hold x stop x target x trail, selected on A by NET expectancy, confirmed on B
  model     LightGBM vs the simple features, trained on A, scored on B
  holdout   structures selected on A by GROSS expectancy, scored ONCE on the untouched last 14 days
"""
import itertools

import numpy as np
import pandas as pd

from . import data, research as R
from .features import FEATURES
from .opportunities import EXTRA, build_opportunities

PD = dict(width=250, max_rows=500)


def _show(df):
    with pd.option_context("display.width", PD["width"], "display.max_rows", PD["max_rows"]):
        print(df.round(4).to_string(index=False))


def load(cfg, days=75, log=print):
    frames = data.load_frames(days, log=log)
    o = build_opportunities(frames, cfg)
    return frames, o, R.split(o)


def facts(sp):
    dev, rows = sp["dev"], []
    for sym in ("BTC", "ETH", "XRP"):
        d = dev[dev.symbol == sym]
        for h in (1, 3, 5, 15):
            rows.append({"coin": sym, "hold_min": h, "avg best move %": d[f"long_mfe_{h}"].mean(), "gross long %": d[f"long_gross_{h}"].mean(),
                         "net long % (real costs)": d[f"long_net_{h}"].mean(), "P(best move >= 1%)": (d[f"long_mfe_{h}"] >= 1).mean()})
    print("Random-entry facts (development period). A round trip costs ~1.0% at the current settings.")
    _show(pd.DataFrame(rows))


def timing(sp):
    dev = sp["dev"]
    sgn = np.sign(dev["ret_1m"]).replace(0, np.nan)
    rows = []
    for lo, hi in ((0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.5), (0.5, 1.0), (1.0, 99)):
        m = (dev["abs_ret_1m"] >= lo) & (dev["abs_ret_1m"] < hi) & sgn.notna()
        d, s = dev[m], sgn[m]
        for h in (1, 3, 5, 15):
            f = np.where(s > 0, d[f"long_gross_{h}"], d[f"short_gross_{h}"])
            rows.append({"1m move %": f"{lo}-{hi}", "hold": h, "n": int(m.sum()), "FOLLOW gross %": f.mean(), "FADE gross %": -f.mean(), "follow win%": (f > 0).mean()})
    print("Entry timing: trade WITH the last 1-minute move (follow) or AGAINST it (fade); gross = before costs")
    _show(pd.DataFrame(rows))


def ic(sp):
    feats = [f for f in FEATURES + EXTRA if f not in ("hour_utc", "day_of_week", "coin_id")]
    rows = []
    for f in feats:
        r = {"feature": f}
        for h in (1, 3, 5):
            for part in ("A", "B"):
                d = sp[part]
                r[f"IC{h}_{part}"] = d[f].corr(d[f"long_gross_{h}"], method="spearman")
        rows.append(r)
    df = pd.DataFrame(rows)
    df["stable"] = [all(np.sign(r[f"IC{h}_A"]) == np.sign(r[f"IC{h}_B"]) for h in (1, 3, 5)) and min(abs(r["IC1_A"]), abs(r["IC1_B"]), abs(r["IC3_A"]), abs(r["IC3_B"])) > 0.005 for _, r in df.iterrows()]
    df["mag"] = df[["IC1_A", "IC1_B", "IC3_A", "IC3_B"]].abs().mean(axis=1)
    print("Rank information of each feature about the next 1/3/5-minute gross return of a LONG (negative = the feature predicts a reversal)")
    _show(df.sort_values("mag", ascending=False).head(15))


def baselines(sp):
    S, rows = R.setups(), []
    for name, fn in S.items():
        for h in (1, 2, 3, 5, 10, 15):
            r = {"setup": name, "hold": h}
            for part in ("A", "B"):
                d = sp[part]
                lm, sm = fn(d)
                s = R.stats(R.take(d, lm, sm, h), R.days_of(d))
                r.update({f"n_{part}": s["n"], f"gross_{part}": s["gross"], f"net_{part}": s["net"], f"win_{part}": s["win"], f"pf_{part}": s["pf"]})
            rows.append(r)
    df = pd.DataFrame(rows)
    df["gross_min"] = df[["gross_A", "gross_B"]].min(axis=1)
    ok = df[(df.n_A >= R.MIN_TRADES) & (df.n_B >= R.MIN_TRADES)]
    print(f"{len(df)} variants tried. Top by the weaker half's GROSS expectancy (n>=30 in each half):")
    _show(ok.sort_values("gross_min", ascending=False).head(15)[["setup", "hold", "n_A", "n_B", "gross_A", "gross_B", "net_A", "net_B", "win_A", "win_B"]])
    print(f"gross>0 in both halves: {int((ok.gross_min > 0).sum())} of {len(ok)}; net>0 in both halves: {int(((ok.net_A > 0) & (ok.net_B > 0)).sum())}; best net seen: A {df.net_A.max():.3f}%  B {df.net_B.max():.3f}%")


GRID = list(itertools.product((3, 5, 10, 15), (0.15, 0.3, 0.6, 1.0), (0.3, 0.6, 1.0, 1.5, 2.5), (False, True)))


def grid(sp, arrays, cfg, by="net", min_n=30, names=None, with_holdout=False):
    S = R.setups()
    out, tried = {}, 0
    for nm, fn in S.items():
        if names and nm not in names:
            continue
        lmA, smA = fn(sp["A"])
        if (lmA | smA).sum() < 150:
            continue
        best = None
        for h, sl, tp, tr in GRID:
            sA = R.stats(R.trades_with_exits(sp["A"], lmA, smA, h, sl, tp, tr, arrays, cfg), R.days_of(sp["A"]))
            tried += 1
            if sA["n"] >= min_n and (best is None or sA[by] > best[0][by]):
                best = (sA, (h, sl, tp, tr))
        if best is None:
            continue
        rec = {"params": best[1], "A": best[0]}
        for part in ("B", "H") if with_holdout else ("B",):
            d = sp[part]
            lm, sm = fn(d)
            rec[part] = R.stats(R.trades_with_exits(d, lm, sm, *best[1][:3], best[1][3], arrays, cfg), R.days_of(d))
        out[nm] = rec
    rows = []
    for nm, r in out.items():
        h, sl, tp, tr = r["params"]
        row = {"setup": nm, "hold": h, "stop%": sl, "tp%": tp, "trail": tr, "n_A": r["A"]["n"], f"{by}_A": r["A"][by], "n_B": r["B"]["n"], "gross_B": r["B"]["gross"], "net_B": r["B"]["net"]}
        if with_holdout:
            H = r["H"]
            row.update({"n_H": H["n"], "gross_H": H["gross"], "net_H": H["net"], "win_H": H["win"], "PF_H": H["pf"], "net_pnl_H_%": H["total"], "per_day_H": H["per_day"]})
        rows.append(row)
    print(f"{tried} entry x hold x stop x target x trail variants tried (selected on half A by {by.upper()} expectancy, confirmed on B" + (", scored once on the sealed holdout)" if with_holdout else ")"))
    _show(pd.DataFrame(rows).sort_values("gross_B", ascending=False))


def model(sp, holdout=False):
    import lightgbm as lgb
    cols = FEATURES + EXTRA
    P = dict(objective="regression", n_estimators=250, learning_rate=0.03, num_leaves=15, min_child_samples=300, subsample=0.7, subsample_freq=1,
             colsample_bytree=0.7, reg_lambda=10, verbose=-1, n_jobs=4, random_state=7)
    train, test = (sp["dev"], sp["H"]) if holdout else (sp["A"], sp["B"])
    rows = []
    for h in (1, 3, 5, 10):
        m = lgb.LGBMRegressor(**P).fit(train[cols], train[f"long_gross_{h}"].clip(-1, 1))
        p = m.predict(test[cols])
        g = test[f"long_gross_{h}"].to_numpy()
        base = {f: test[f].corr(test[f"long_gross_{h}"], method="spearman") for f in ("rsi_1m", "dist_vwap_60m", "dist_ema9")}
        for q in (0.99, 0.999):
            sel = np.abs(p) >= np.quantile(np.abs(p), q)
            dg = np.sign(p[sel]) * g[sel]
            rows.append({"hold": h, "model IC": pd.Series(p).corr(pd.Series(g), method="spearman"), "best single-feature IC": min(base.values()), "top": f"{(1 - q) * 100:.1f}%", "n": int(sel.sum()),
                         "avg gross %": dg.mean(), "win%": (dg > 0).mean()})
    print("LightGBM vs single simple features (" + ("trained on dev, scored on the sealed holdout" if holdout else "trained on A, scored on B") + ")")
    _show(pd.DataFrame(rows))


def run(cfg, sections, days=75):
    frames, o, sp = load(cfg, days)
    arrays = R.frame_arrays(frames)
    for sec in sections:
        print("\n" + "=" * 100 + f"\n## {sec}\n" + "=" * 100)
        if sec == "facts":
            facts(sp)
        elif sec == "timing":
            timing(sp)
        elif sec == "ic":
            ic(sp)
        elif sec == "baselines":
            baselines(sp)
        elif sec == "grid":
            grid(sp, arrays, cfg, by="net")
        elif sec == "model":
            model(sp)
        elif sec == "holdout":
            grid(sp, arrays, cfg, by="gross", min_n=100, with_holdout=True)
            model(sp, holdout=True)
