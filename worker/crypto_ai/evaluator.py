"""Scores predictions once their 24h/48h window has ended. Results are written exactly once."""
from datetime import datetime, timedelta, timezone

from . import coinbase
from .config import PRODUCTS


def score_prediction(pred: dict, actual_price: float, cfg: dict) -> dict:
    """Pure: compare what was predicted with what the real price did."""
    ret = actual_price / pred["price_at_prediction"] - 1
    band = cfg[f"hold_band_pct_{pred['horizon_h']}h"]
    sig = pred["signal"]
    if sig == "BUY":
        correct = ret > 0
    elif sig == "SELL":
        correct = ret < 0
    else:
        correct = abs(ret) * 100 < band
    return {
        "prediction_id": pred["id"],
        "actual_price": actual_price,
        "actual_return_pct": ret * 100,
        "directional_correct": (ret > 0) == (pred["bullish_prob"] > 0.5),
        "signal_correct": bool(correct),
        "in_range": pred["range_low"] <= actual_price <= pred["range_high"],
        "hold_band_pct": band,
        "high_confidence": pred["confidence"] * 100 >= cfg["high_confidence_threshold"],
    }


def evaluate_due(db, cfg: dict) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
    rows = db.all("""select p.* from predictions p
                     left join prediction_results r on r.prediction_id = p.id
                     where r.prediction_id is null and p.target_time <= %s order by p.target_time""", [cutoff])
    n = 0
    for p in rows:
        try:
            actual = coinbase.price_at(PRODUCTS[p["symbol"]], p["target_time"])
        except Exception as e:  # network hiccup: retry on the next run
            print(f"[evaluate] {p['symbol']} {p['id']}: {e}")
            continue
        if actual is None:
            continue
        db.insert("prediction_results", score_prediction(p, actual, cfg), returning="prediction_id",
                  on_conflict="on conflict (prediction_id) do nothing")
        n += 1
    return n
