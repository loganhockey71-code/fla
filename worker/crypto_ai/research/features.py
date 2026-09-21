"""Turn stored research data into numeric model features, strictly point-in-time.

  * macro (FRED): a value is visible only from `available_at` (observation date + publication lag).
  * events / prediction markets: visible only from `detected_at` / `captured_at` - the moment WE saw them.
Rows earlier than the first research event we ever detected get NaN for event features (unknown), never 0, so the
model can't learn from history we didn't actually have. There is deliberately no hand-written 'research adjustment':
research only ever enters as columns of a model that is scored against the market-only model.
"""
import numpy as np
import pandas as pd

MACRO_FEATURES = [
    "macro_ff_level", "macro_ff_chg_30d", "macro_y2_level", "macro_y2_chg_30d", "macro_y10_level", "macro_y10_chg_30d",
    "macro_curve_2s10s", "macro_cpi_yoy", "macro_cpi_yoy_chg", "macro_core_yoy", "macro_unrate", "macro_unrate_chg_3m",
    "macro_payrolls_chg", "macro_claims_chg",
]
EVENT_FEATURES = [
    "ev_impact_24h", "ev_impact_72h", "ev_count_24h", "ev_official_24h", "ev_max_impact_24h", "ev_confirmations_24h",
    "pm_max_move_24h", "pm_dir_move_24h",
]
RESEARCH_FEATURES = MACRO_FEATURES + EVENT_FEATURES
LATE_NEWS_HOURS = 6
IMPACT_COL = {"BTC": "btc_impact_score", "ETH": "eth_impact_score", "XRP": "xrp_impact_score"}


def load(db) -> dict:
    macro = {}
    for r in db.all("select series_id, obs_date, value, available_at from macro_data order by available_at"):
        macro.setdefault(r["series_id"], []).append(r)
    macro = {k: pd.DataFrame(v) for k, v in macro.items()}
    ev = pd.DataFrame(db.all("""select detected_at, affected_coins, btc_impact_score, eth_impact_score, xrp_impact_score,
        source_credibility_score, novelty_score, confidence_score, independent_confirmations, origin_tier, title, event_category,
        importance_score, kind, published_at from research_events order by detected_at"""))
    if len(ev):
        # Backfill guard: news we only noticed hours after publication is not fresh news at detection time.
        late = (ev["kind"] == "news") & ((ev["detected_at"] - ev["published_at"]) > pd.Timedelta(hours=LATE_NEWS_HOURS))
        ev = ev[~late].reset_index(drop=True)
    pm = pd.DataFrame(db.all("select platform, market_id, title, coins, direction, yes_prob, volume_24h, captured_at "
                             "from prediction_market_snapshots order by captured_at"))
    start = ev["detected_at"].min() if len(ev) else None
    return {"macro": macro, "events": ev, "pm": pm, "start": pd.Timestamp(start) if start is not None else None}


def _derived(sid: str, df: pd.DataFrame) -> pd.DataFrame:
    v = df["value"].astype(float).reset_index(drop=True)
    out = pd.DataFrame({"available_at": pd.to_datetime(df["available_at"], utc=True).reset_index(drop=True)})
    if sid == "DFF":
        out["macro_ff_level"], out["macro_ff_chg_30d"] = v, v - v.shift(30)
    elif sid == "DGS2":
        out["macro_y2_level"], out["macro_y2_chg_30d"] = v, v - v.shift(21)
    elif sid == "DGS10":
        out["macro_y10_level"], out["macro_y10_chg_30d"] = v, v - v.shift(21)
    elif sid == "CPIAUCSL":
        yoy = (v / v.shift(12) - 1) * 100
        out["macro_cpi_yoy"], out["macro_cpi_yoy_chg"] = yoy, yoy - yoy.shift(1)
    elif sid == "CPILFESL":
        out["macro_core_yoy"] = (v / v.shift(12) - 1) * 100
    elif sid == "UNRATE":
        out["macro_unrate"], out["macro_unrate_chg_3m"] = v, v - v.shift(3)
    elif sid == "PAYEMS":
        out["macro_payrolls_chg"] = v.diff()
    elif sid == "ICSA":
        out["macro_claims_chg"] = v.rolling(4).mean() / v.shift(4).rolling(4).mean() - 1
    return out


def macro_frame(data: dict, index: pd.DatetimeIndex) -> pd.DataFrame:
    """Point-in-time macro features for every timestamp in `index` (UTC)."""
    base = pd.DataFrame({"asof": index}).sort_values("asof")
    for sid, df in data["macro"].items():
        d = _derived(sid, df).sort_values("available_at")
        if len(d.columns) == 1:
            continue
        base = pd.merge_asof(base, d, left_on="asof", right_on="available_at", direction="backward").drop(columns="available_at")
    base = base.set_index("asof").reindex(index)
    for c in MACRO_FEATURES:
        if c not in base:
            base[c] = np.nan
    base["macro_curve_2s10s"] = base["macro_y10_level"] - base["macro_y2_level"]
    return base[MACRO_FEATURES]


def _event_row(ev: pd.DataFrame, pm: pd.DataFrame, symbol: str, t: pd.Timestamp) -> dict:
    row = dict.fromkeys(EVENT_FEATURES, 0.0)
    if len(ev):
        seen = ev[(ev["detected_at"] <= t) & (ev["detected_at"] > t - pd.Timedelta(hours=72))]
        seen = seen[seen["affected_coins"].apply(lambda c: symbol in c)]
        if len(seen):
            imp = seen[IMPACT_COL[symbol]].astype(float)
            w = (seen["source_credibility_score"] / 100) * (seen["novelty_score"] / 100) * (seen["confidence_score"] / 100)
            score = imp * w
            d24 = seen["detected_at"] > t - pd.Timedelta(hours=24)
            row["ev_impact_72h"] = float(score.sum())
            row["ev_impact_24h"] = float(score[d24].sum())
            row["ev_count_24h"] = float(d24.sum())
            row["ev_official_24h"] = float((d24 & (seen["origin_tier"] <= 2)).sum())
            if d24.any():
                row["ev_max_impact_24h"] = float(imp[d24].iloc[int(np.argmax(imp[d24].abs().values))])
                row["ev_confirmations_24h"] = float(seen.loc[d24, "independent_confirmations"].mean())
    if len(pm):
        win = pm[(pm["captured_at"] <= t) & (pm["captured_at"] > t - pd.Timedelta(hours=24))]
        win = win[win["coins"].apply(lambda c: symbol in c)]
        moves, dirs = [], []
        for _, g in win.groupby(["platform", "market_id"]):
            if len(g) >= 2:
                mv = float(g["yes_prob"].iloc[-1] - g["yes_prob"].iloc[0])
                moves.append(abs(mv))
                dirs.append(mv * float(g["direction"].iloc[-1]))
        if moves:
            row["pm_max_move_24h"] = max(moves)
            row["pm_dir_move_24h"] = float(np.mean(dirs))
    return row


def event_frame(data: dict, symbol: str, index: pd.DatetimeIndex, hourly_only: bool = True) -> pd.DataFrame:
    """Event features per timestamp. NaN before we started collecting. `hourly_only` skips non-hour rows (the
    training set only uses hourly rows) so backfilling 20k rows stays fast."""
    out = pd.DataFrame(np.nan, index=index, columns=EVENT_FEATURES)
    start = data["start"]
    if start is None:
        return out
    for t in index:
        if t < start or (hourly_only and t.minute != 0):
            continue
        r = _event_row(data["events"], data["pm"], symbol, t)
        out.loc[t, EVENT_FEATURES] = [r[c] for c in EVENT_FEATURES]
    return out


def research_frame(data: dict, symbol: str, index: pd.DatetimeIndex, hourly_only: bool = True) -> pd.DataFrame:
    return pd.concat([macro_frame(data, index), event_frame(data, symbol, index, hourly_only)], axis=1)


def snapshot(data: dict, symbol: str, asof: pd.Timestamp, row: pd.Series) -> dict:
    """Everything the research layer knew at `asof`, stored verbatim with each prediction."""
    ev = data["events"]
    top = []
    if len(ev):
        seen = ev[(ev["detected_at"] <= asof) & (ev["detected_at"] > asof - pd.Timedelta(hours=24))]
        seen = seen[seen["affected_coins"].apply(lambda c: symbol in c)]
        seen = seen.reindex(seen[IMPACT_COL[symbol]].abs().sort_values(ascending=False).index).head(5)
        top = [{"title": r["title"][:140], "category": r["event_category"], "impact": int(r[IMPACT_COL[symbol]]), "importance": int(r["importance_score"]),
                "credibility": int(r["source_credibility_score"]), "detected_at": r["detected_at"].isoformat()} for _, r in seen.iterrows()]
    clean = lambda k: None if pd.isna(row.get(k)) else float(row[k])
    return {"asof": asof.isoformat(), "macro": {k: clean(k) for k in MACRO_FEATURES}, "events": {k: clean(k) for k in EVENT_FEATURES},
            "top_events_24h": top, "research_collecting_since": data["start"].isoformat() if data["start"] is not None else None}
