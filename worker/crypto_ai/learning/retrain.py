"""Champion / challenger retraining.

Rules (all enforced in code):
  * Nothing retrains because of a single mistake. A retrain is only ATTEMPTED after `retrain_min_days` since the last
    training AND `retrain_min_new_scored` newly scored live predictions, and at most once per holdout window.
  * The challenger trains ONLY on data that ends before a held-out window. Champion and challenger are then scored on
    that same window, which neither model has seen.
  * The challenger replaces the champion only if it is clearly better there (log-loss gain, consistent across
    horizons, statistically supported by a paired daily sign test on unseen days, not worse-calibrated, and better than a coin flip).
  * Every attempt is recorded in model_challenges, promoted or not. The champion keeps running unless the gate passes.
"""
import json
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from scipy.stats import binomtest
from sklearn.metrics import brier_score_loss, roc_auc_score

from ..config import BAR, HORIZONS, SYMBOLS
from ..features import MODEL_FEATURES, compute_features, make_labels
from ..model import predict_frame, simulate, train_symbol

LN2 = math.log(2)
MIN_HOLDOUT_ROWS = 200
EPS = 1e-6


def _logloss(y: np.ndarray, p: np.ndarray) -> np.ndarray:
    p = np.clip(p, EPS, 1 - EPS)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def holdout_frame(feats: pd.DataFrame, horizon_h: int, cols: list[str], hold_start, now) -> pd.DataFrame:
    """Hourly rows strictly after hold_start whose outcome is already known."""
    d = feats[cols].join(make_labels(feats, horizon_h))
    d = d[(d.index.minute == 0) & (d.index > hold_start) & (d.index <= now - pd.Timedelta(hours=horizon_h))].dropna(subset=["y"])
    return d


def score(d: pd.DataFrame, p: np.ndarray, horizon_h: int, cfg: dict) -> dict:
    y = d["y"].values
    oos = pd.DataFrame({"p": p, "fwd_ret": d["fwd_ret"].values}, index=d.index)
    return {"n": int(len(d)), "logloss": float(_logloss(y, p).mean()), "brier": float(brier_score_loss(y, p)),
            "auc": float(roc_auc_score(y, p)) if len(set(y)) == 2 and np.ptp(p) > 0 else 0.5,
            "directional_accuracy": float(((p > 0.5) == (y == 1)).mean()), "strategy": simulate(oos, horizon_h, cfg)}


def compare(champion: dict, challenger: dict, feats: pd.DataFrame, cols_champ: list[str], cols_chal: list[str], hold_start, now, cfg: dict) -> dict:
    """Score both models on identical unseen rows and apply the promotion gate. Pure: no database access."""
    per_h, daily = {}, []
    for h in HORIZONS:
        d = holdout_frame(feats, h, sorted(set(cols_champ) | set(cols_chal)), hold_start, now)
        if len(d) < MIN_HOLDOUT_ROWS:
            return {"decision": "insufficient_data", "reason": f"only {len(d)} unseen rows for the {h}h horizon (need {MIN_HOLDOUT_ROWS})", "per_horizon": {}}
        pc, pn = predict_frame(champion, h, d), predict_frame(challenger, h, d)
        per_h[str(h)] = {"champion": score(d, pc, h, cfg), "challenger": score(d, pn, h, cfg)}
        diff = pd.Series((pc - d["y"].values) ** 2 - (pn - d["y"].values) ** 2, index=d.index)     # Brier: bounded, so no single hour can dominate
        g = diff.groupby(diff.index.normalize())
        daily.append(g.mean()[g.size() >= 12])                          # full days only; hourly rows overlap, days are the unit of evidence
    days = pd.concat(daily, axis=1).mean(axis=1).dropna()
    gain = {h: per_h[h]["champion"]["logloss"] - per_h[h]["challenger"]["logloss"] for h in per_h}
    mean_gain = float(np.mean(list(gain.values())))
    wins, n_days = int((days > 0).sum()), int((days != 0).sum())
    p_val = float(binomtest(wins, n_days, 0.5, alternative="greater").pvalue) if n_days >= 5 else 1.0     # sign test: robust to heavy tails and streaky days
    chal_ll = float(np.mean([per_h[h]["challenger"]["logloss"] for h in per_h]))
    brier_ok = all(per_h[h]["challenger"]["brier"] <= per_h[h]["champion"]["brier"] + 0.001 for h in per_h)
    checks = {
        "mean_logloss_gain": (mean_gain, mean_gain >= cfg["retrain_min_improvement"]),
        "not_worse_on_any_horizon": ({h: round(g, 5) for h, g in gain.items()}, all(g >= 0 for g in gain.values())),
        "challenger_wins_days": (f"{wins}/{n_days} unseen days, sign-test p={p_val:.3f}", p_val <= cfg["retrain_max_p_value"]),
        "calibration_not_worse": (brier_ok, brier_ok),
        "beats_coin_flip_on_unseen": (chal_ll, chal_ll < LN2 - 0.001),
    }
    failed = [k for k, (_, ok) in checks.items() if not ok]
    return {"decision": "rejected" if failed else "promoted", "per_horizon": per_h, "daily_days": int(len(days)), "days_won": wins,
            "checks": {k: {"value": v, "passed": bool(ok)} for k, (v, ok) in checks.items()},
            "reason": ("challenger not clearly better on unseen data: failed " + ", ".join(failed)) if failed
            else f"challenger won {wins} of {n_days} unseen days (sign-test p={p_val:.3f}) with a log-loss gain of {mean_gain:.4f}"}


# ---------------------------------------------------------------- orchestration
def due(db, cfg: dict, symbol: str, variant: str, force: bool = False) -> tuple[bool, dict, dict | None]:
    champ = db.one("select * from model_versions where symbol=%s and variant=%s and is_active", [symbol, variant])
    if not champ:
        return False, {"why": "no active model"}, None
    now = datetime.now(timezone.utc)
    days = (now - champ["trained_at"]).total_seconds() / 86400
    new = db.one("""select count(*) n from predictions p join prediction_results r on r.prediction_id = p.id
                    where p.symbol=%s and p.variant=%s and p.created_at > %s""", [symbol, variant, champ["trained_at"]])["n"]
    last = db.one("select max(created_at) t from model_challenges where symbol=%s and variant=%s and decision in ('promoted','rejected')", [symbol, variant])["t"]
    info = {"days_since_training": round(days, 1), "new_scored_predictions": new, "min_days": cfg["retrain_min_days"], "min_new_scored": cfg["retrain_min_new_scored"]}
    if force:
        return True, {**info, "forced": True}, champ
    if days < cfg["retrain_min_days"]:
        return False, {**info, "why": f"only {days:.0f} of {cfg['retrain_min_days']} days since last training"}, champ
    if new < cfg["retrain_min_new_scored"]:
        return False, {**info, "why": f"only {new} of {cfg['retrain_min_new_scored']} new scored predictions"}, champ
    if last and (now - last).days < cfg["retrain_holdout_days"]:
        return False, {**info, "why": "already challenged within the last holdout window"}, champ
    return True, info, champ


def challenge(db, cfg: dict, symbol: str, variant: str, frames: dict, rdata: dict | None, champ: dict, info: dict) -> dict:
    now = datetime.now(timezone.utc)
    hold_start = pd.Timestamp(now - timedelta(days=cfg["retrain_holdout_days"])).floor("h")
    train_frames = {s: df[df.index + pd.Timedelta(seconds=BAR) <= hold_start] for s, df in frames.items()}   # nothing at/after the holdout
    if champ["train_end"] is not None and pd.Timestamp(champ["train_end"]) > hold_start:
        res = {"decision": "insufficient_data", "reason": "the current model was trained inside the holdout window", "per_horizon": {}}
        chal = None
    else:
        chal = train_symbol(symbol, train_frames, cfg, variant, rdata)
        feats = compute_features(frames, symbol)
        if variant == "research":
            from ..research.features import RESEARCH_FEATURES, research_frame
            feats = feats.join(research_frame(rdata, symbol, feats.index))
        res = compare(champ, chal, feats, champ["feature_names"], chal["feature_names"], hold_start, pd.Timestamp(now), cfg)
    db.insert("model_challenges", {"symbol": symbol, "variant": variant, "champion_version": champ["version"], "challenger_version": chal["version"] if chal else None,
                                   "trigger_info": info, "holdout_start": hold_start.to_pydatetime(), "holdout_end": now, "metrics": json.loads(json.dumps(res, default=float)),
                                   "decision": res["decision"], "reason": res["reason"]})
    if res["decision"] == "promoted":
        from ..db import JsonList
        chal["backtest_metrics"]["promotion"] = {"replaced": champ["version"], "reason": res["reason"]}
        db.run("update model_versions set is_active=false where symbol=%s and variant=%s and is_active", [symbol, variant])
        db.insert("model_versions", {**chal, "feature_names": JsonList(chal["feature_names"]), "is_active": True})
    return res


def maybe_retrain(db, cfg: dict, force: bool = False, only: tuple[str, str] | None = None) -> list[dict]:
    from .data import load_frames
    out, frames, rdata = [], None, None
    for variant in ("market", "research"):
        for symbol in SYMBOLS:
            if only and only != (symbol, variant):
                continue
            ok, info, champ = due(db, cfg, symbol, variant, force)
            if not ok:
                out.append({"symbol": symbol, "variant": variant, "decision": "not_due", "reason": info.get("why", "")})
                continue
            if frames is None:
                frames = load_frames(db, days=400, topup=True)
                from ..research import features as rfeat
                rdata = rfeat.load(db)
            r = challenge(db, cfg, symbol, variant, frames, rdata, champ, info)
            out.append({"symbol": symbol, "variant": variant, "decision": r["decision"], "reason": r["reason"]})
    return out
