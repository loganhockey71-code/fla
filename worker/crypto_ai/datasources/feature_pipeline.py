"""Combines the datasets collected in `datalake.py` with the existing 1-minute candle feature grid
(`crypto_ai.scalp.features`), by timestamp, for RESEARCH ONLY. This produces candidate features to test - it does
NOT change what the live model trains on. A candidate only ever reaches `crypto_ai.scalp.features.FEATURES` (the
production list) after a human adds it there, having first run `evaluate_candidate_feature` below and seen it
survive the SAME chronological A/B + sealed-holdout protocol as `crypto_ai/scalp/research.py` - "track feature
importance and out-of-sample performance before assuming a feature is useful", per the project brief.

Causality: every join here is an AS-OF merge with `direction="backward"` - a candle at time t can only see a
datasource row whose own timestamp is <= t. `tests/test_feature_pipeline.py` proves this by rewriting the future of
a joined series and checking past rows never change (same style as the project's other look-ahead tests).
"""
import numpy as np
import pandas as pd

from . import datalake


def asof_join(base: pd.DataFrame, other: pd.DataFrame, other_ts_col: str, value_cols: list[str], prefix: str) -> pd.DataFrame:
    """`base` is indexed by decision timestamp (ascending). Attaches the latest known value of each `value_cols`
    column from `other` as of each base timestamp - never a future one."""
    out = base.copy()
    if other is None or other.empty:
        for c in value_cols:
            out[f"{prefix}_{c}"] = np.nan
        return out
    o = other[[other_ts_col, *value_cols]].dropna(subset=[other_ts_col]).sort_values(other_ts_col)
    o = o.rename(columns={other_ts_col: "_ts", **{c: f"{prefix}_{c}" for c in value_cols}})
    merged = pd.merge_asof(out.reset_index().sort_values(out.index.name or "index"), o, left_on=out.index.name or "index", right_on="_ts", direction="backward")
    merged = merged.set_index(out.index.name or "index").drop(columns=["_ts"], errors="ignore")
    return merged


def load_candidate_features(symbol: str, start: pd.Timestamp, end: pd.Timestamp, base: pd.DataFrame, lake_root: str | None = None) -> pd.DataFrame:
    """`base` = an existing causal feature frame indexed by decision timestamp (e.g. crypto_ai.scalp.features'
    or micro_features' output for `symbol`). Returns `base` plus as-of-joined candidate columns from every
    datasource category that has data for this symbol in [start, end]. Purely additive; never touches `base`'s
    existing columns."""
    out = base
    funding = datalake.read("funding_rates", "okx_funding_rate", start, end, root=lake_root)
    out = asof_join(out, funding[funding.symbol == symbol] if len(funding) else funding, "ts", ["funding_rate"], "funding")
    oi = datalake.read("open_interest", "okx_open_interest", start, end, root=lake_root)
    out = asof_join(out, oi[oi.symbol == symbol] if len(oi) else oi, "ts", ["oi_usd"], "oi")
    liq = datalake.read("liquidations", "okx_liquidations", start, end, root=lake_root)
    if len(liq):
        liq_1h = liq[liq.symbol == symbol].set_index("ts").resample("1h")["size"].sum().reset_index().rename(columns={"size": "liq_notional_1h"})
        out = asof_join(out, liq_1h, "ts", ["liq_notional_1h"], "liq")
    else:
        out["liq_liq_notional_1h"] = np.nan
    stables = datalake.read("stablecoin_flows", "defillama_stablecoins", start, end, root=lake_root)
    out = asof_join(out, stables, "ts", ["total_circulating_usd"], "macro")
    if symbol == "BTC":
        onchain = datalake.read("onchain_metrics", "blockchain_info_btc_charts", start, end, root=lake_root)
        if len(onchain):
            wide = onchain.pivot_table(index="ts", columns="metric", values="value").reset_index()
            out = asof_join(out, wide, "ts", [c for c in wide.columns if c != "ts"], "onchain")
    return out


# ---------------------------------------------------------------- feature-importance / out-of-sample gate
def evaluate_candidate_feature(df: pd.DataFrame, candidate_col: str, label_col: str, min_rows: int = 200) -> dict:
    """Is `candidate_col` worth adding to the production FEATURES list? Rank-IC with `label_col` (a net-outcome
    label, e.g. crypto_ai.scalp.labels' net_long/net_short) computed separately on each half of a chronological
    A/B split - SAME sign in both halves AND |IC| above a noise floor in both = 'promising, worth a holdout check';
    anything else = 'not shown to help', which is the expected, honest default for most candidates.
    """
    d = df.dropna(subset=[candidate_col, label_col]).sort_index()
    n = len(d)
    if n < min_rows:
        return {"n": n, "verdict": "insufficient_data", "ic_a": None, "ic_b": None}
    mid = d.index[n // 2]
    a, b = d[d.index < mid], d[d.index >= mid]
    ic_a = a[candidate_col].corr(a[label_col], method="spearman")
    ic_b = b[candidate_col].corr(b[label_col], method="spearman")
    stable = pd.notna(ic_a) and pd.notna(ic_b) and np.sign(ic_a) == np.sign(ic_b) and min(abs(ic_a), abs(ic_b)) > 0.01
    return {"n": n, "n_a": len(a), "n_b": len(b), "ic_a": float(ic_a) if pd.notna(ic_a) else None,
            "ic_b": float(ic_b) if pd.notna(ic_b) else None, "verdict": "promising_check_holdout" if stable else "not_shown_to_help"}
