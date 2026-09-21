"""Turns many post-mortems into statistically-checked patterns ('when X was present, we were wrong more often').

A pattern is only ever a *suggestion for a human to review*. Nothing here edits a model. A situation needs at least
`pattern_min_examples` predictions and must survive a multiple-testing correction before it is called 'confirmed'.
"""
import json

import pandas as pd
from scipy.stats import fisher_exact

from .postmortem import load_context, macro_in_window, market_context, path_stats, pm_shifts, vol_ratio, volume_stats

SITUATIONS = {
    "volume_shock": ("a volume spike (3x normal) during the biggest move",
                     "Test a volume-spike feature, or widen the HOLD band when 1h volume is far above normal."),
    "market_move": ("another major coin moved 2%+ in the window",
                    "Give BTC direction more weight for ETH/XRP, or condition the call on BTC momentum."),
    "macro_release": ("a CPI / jobs / unemployment release landed in the window",
                      "Add a release-calendar feature and widen HOLD around big macro days."),
    "volatility_jump": ("volatility doubled versus the 24h before the call",
                        "Add volatility-of-volatility features; scale the HOLD band by expected volatility."),
    "event_after_call": ("a high-impact event was detected after the call",
                         "Speed up event ingestion; test event features once there are 90+ days of event history."),
    "prediction_market_move": ("a prediction market repriced by 8+ points",
                               "Test the prediction-market move as a feature once enough history has accumulated."),
}


def flags_for(pred: dict, frames: dict, ev: pd.DataFrame, pm: pd.DataFrame, macro: pd.DataFrame) -> dict:
    """Outcome-independent situation flags, computed identically for right and wrong predictions."""
    sym, t0, t1 = pred["symbol"], pd.Timestamp(pred["created_at"]), pd.Timestamp(pred["target_time"])
    df = frames[sym]
    ret = pred["actual_return_pct"] / 100
    path = path_stats(df, pred["price_at_prediction"], t0, t1)
    big = (path.get("biggest_hour") or {})
    vs = volume_stats(df, t0, t1)
    others = market_context(frames, sym, t0, t1, ret if ret else 1e-9)["others_return"]
    move = max((abs(v) for v in others.values() if v is not None), default=0)
    return {
        "volume_shock": bool(vs.get("max_spike", 0) >= 3 and abs(big.get("return") or 0) >= 0.01),
        "market_move": bool(move >= 0.02),
        "macro_release": bool(macro_in_window(macro, t0, t1)),
        "volatility_jump": bool((vol_ratio(df, t0, t1) or 0) >= 2),
        "event_after_call": _event_after(ev, sym, t0, t1),
        "prediction_market_move": bool(pm_shifts(pm, sym, t0, t1, 1) or pm_shifts(pm, sym, t0, t1, -1)),
    }


def _event_after(ev: pd.DataFrame, sym: str, t0, t1) -> bool:
    if not len(ev):
        return False
    col = {"BTC": "btc_impact_score", "ETH": "eth_impact_score", "XRP": "xrp_impact_score"}[sym]
    w = ev[(ev["detected_at"] > t0) & (ev["detected_at"] <= t1)]
    return bool(any(sym in c and abs(v) >= 30 for c, v in zip(w["affected_coins"], w[col])))


def _stats(rows: list[dict], tag: str) -> dict:
    # lift is floored at "one error in n+1" for the comparison group, so it never divides by zero
    w = [r for r in rows if r["flags"][tag]]
    wo = [r for r in rows if not r["flags"][tag]]
    a, b = sum(r["wrong"] for r in w), sum(not r["wrong"] for r in w)
    c, d = sum(r["wrong"] for r in wo), sum(not r["wrong"] for r in wo)
    p = float(fisher_exact([[a, b], [c, d]], alternative="greater")[1]) if (a + b) and (c + d) else 1.0
    rw, rwo = (a / (a + b) if a + b else None), (c / (c + d) if c + d else None)
    return {"n_with": a + b, "n_wrong_with": a, "n_without": c + d, "n_wrong_without": c, "rate_with": rw, "rate_without": rwo,
            "lift": (rw / max(rwo, 1 / (c + d + 1))) if rw is not None and rwo is not None else None, "p": p, "examples": [r["id"] for r in w if r["wrong"]][:3]}


def update_patterns(db, cfg: dict, frames: dict | None = None) -> int:
    """Recompute pattern statistics from ALL scored market-model predictions (right and wrong)."""
    preds = db.all("""select p.id, p.symbol, p.horizon_h, p.signal, p.created_at, p.target_time, p.price_at_prediction, r.actual_return_pct, r.signal_correct
                      from predictions p join prediction_results r on r.prediction_id = p.id where p.variant = 'market'""")
    if not preds:
        return 0
    if frames is None:
        from .data import load_frames
        frames = load_frames(db, days=400)
    ev, pm, macro = load_context(db)
    rows = []
    for p in preds:
        try:
            rows.append({"id": str(p["id"]), "symbol": p["symbol"], "h": p["horizon_h"], "wrong": not p["signal_correct"], "flags": flags_for(p, frames, ev, pm, macro)})
        except Exception:
            continue
    scopes = [("ALL", None)] + [("ALL", h) for h in (24, 48)] + [(s, None) for s in ("BTC", "ETH", "XRP")]
    tests = []
    for scope, h in scopes:
        sel = [r for r in rows if (scope == "ALL" or r["symbol"] == scope) and (h is None or r["h"] == h)]
        for tag in SITUATIONS:
            tests.append((scope, h, tag, _stats(sel, tag)))
    testable = max(1, sum(1 for *_, s in tests if s["n_with"] >= cfg["pattern_min_examples"] and s["n_without"] >= cfg["pattern_min_examples"]))
    n = 0
    for scope, h, tag, s in tests:
        enough = s["n_with"] >= cfg["pattern_min_examples"] and s["n_without"] >= cfg["pattern_min_examples"]
        lift_ok = (s["lift"] or 0) >= 1.2
        status = ("insufficient" if not enough else "confirmed" if lift_ok and s["p"] < 0.05 / testable else "candidate" if lift_ok and s["p"] < 0.10 else "noise")
        key = f"{scope}|{h or 'both'}|{tag}"
        desc, suggestion = SITUATIONS[tag]
        db.run("""insert into learned_patterns (key, scope, horizon_h, tag, description, n_with_tag, n_wrong_with, n_without, n_wrong_without,
                     error_rate_with, error_rate_without, lift, p_value, status, suggestion, examples)
                  values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  on conflict (key) do update set n_with_tag=excluded.n_with_tag, n_wrong_with=excluded.n_wrong_with, n_without=excluded.n_without,
                     n_wrong_without=excluded.n_wrong_without, error_rate_with=excluded.error_rate_with, error_rate_without=excluded.error_rate_without,
                     lift=excluded.lift, p_value=excluded.p_value, status=excluded.status, suggestion=excluded.suggestion, examples=excluded.examples, updated_at=now()""",
               [key, scope, h, tag, desc, s["n_with"], s["n_wrong_with"], s["n_without"], s["n_wrong_without"], s["rate_with"], s["rate_without"],
                s["lift"], s["p"], status, suggestion if status in ("candidate", "confirmed") else None, json.dumps(s["examples"])])
        n += 1
    return n
