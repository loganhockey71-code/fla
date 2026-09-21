"""Turns live data + the active model into an immutable prediction row."""
import math
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import coinbase
from .db import JsonList
from .config import BAR, HORIZONS, PRODUCTS, SYMBOLS
from .features import compute_features
from .model import predict_probability
from .paper import act_on_prediction
from .research import features as rfeat


def decide_signal(bull: float, spread_pct: float | None, cfg: dict) -> tuple[str, str | None]:
    """BUY/SELL only when the calibrated probability clears the threshold; otherwise HOLD."""
    thr = cfg["signal_threshold_pct"] / 100
    bear = 1 - bull
    if spread_pct is not None and spread_pct > cfg["max_spread_pct_to_trade"]:
        return "HOLD", f"spread {spread_pct:.2f}% is too wide to trade"
    if bull >= thr:
        return "BUY", None
    if bear >= thr:
        return "SELL", None
    return "HOLD", None


def expected_range(price: float, closes: pd.Series, horizon_h: int) -> tuple[float, float, float]:
    """~68% band from the last 3 days of realised 15-minute volatility scaled to the horizon."""
    lr = np.log(closes).diff().dropna().iloc[-288:]
    sigma = float(lr.std() * math.sqrt(horizon_h * 3600 / BAR))
    return price * math.exp(-sigma), price * math.exp(sigma), sigma


def explain(signal: str, bull: float, horizon_h: int, drivers: list[dict], gate: str | None, sigma: float) -> str:
    bear = 1 - bull
    head = {
        "BUY": f"BUY: model puts {bull:.0%} on a higher price in {horizon_h}h.",
        "SELL": f"SELL: model puts {bear:.0%} on a lower price in {horizon_h}h.",
        "HOLD": f"HOLD: no clear edge ({bull:.0%} up / {bear:.0%} down over {horizon_h}h).",
    }[signal]
    if gate:
        head += f" Overridden to HOLD because {gate}."
    why = "; ".join(f"{d['text']} ({d['pushes']})" for d in drivers)
    return f"{head} Main drivers: {why}. Typical {horizon_h}h swing at current volatility: ±{sigma * 100:.1f}%."


def live_frames(now: datetime) -> dict[str, pd.DataFrame]:
    start = now - timedelta(seconds=BAR * 620)
    return {s: coinbase.closed_only(coinbase.candles(PRODUCTS[s], BAR, start, now), BAR, now) for s in SYMBOLS}


def micro_snapshot(symbol: str) -> dict:
    """Live order-book / flow snapshot. Recorded with the prediction; not a v1 model input."""
    t = coinbase.ticker(PRODUCTS[symbol])
    out = {"price": t["price"], "bid": t["bid"], "ask": t["ask"], "spread_pct": t["spread_pct"],
           "volume_24h": t["volume_24h"]}
    for key, fn in (("ob_imbalance", coinbase.order_book_imbalance), ("buy_pressure", coinbase.buy_pressure)):
        try:
            out[key] = fn(PRODUCTS[symbol])
        except Exception:
            out[key] = None
    return out


def due(db, symbol: str, horizon_h: int, cfg: dict) -> bool:
    r = db.one("select max(created_at) as t from predictions where symbol=%s and horizon_h=%s and variant='market'", [symbol, horizon_h])
    if not r or r["t"] is None:
        return True
    return datetime.now(timezone.utc) - r["t"] >= timedelta(hours=cfg["prediction_interval_h"]) - timedelta(minutes=5)


def run_predictions(db, cfg: dict, force: bool = False) -> list[dict]:
    """One run = for each coin/horizon, a 'market' prediction (candles only) and a 'research' prediction (candles + macro
    + events), sharing a run_id. Both are logged and scored; only cfg['trade_variant'] (default 'market') paper-trades,
    so research can never move money until it has proven itself."""
    now = datetime.now(timezone.utc)
    todo = [(s, h) for s in SYMBOLS for h in HORIZONS if force or due(db, s, h, cfg)]
    if not todo:
        return []
    frames = live_frames(now)
    snaps = {s: micro_snapshot(s) for s in SYMBOLS}
    prices = {s: v["price"] for s, v in snaps.items()}
    rdata = rfeat.load(db)
    run_id = uuid.uuid4()
    made = []
    for symbol in sorted({s for s, _ in todo}):
        models = {v: db.one("select * from model_versions where symbol=%s and variant=%s and is_active", [symbol, v])
                  for v in ("market", "research")}
        if not models["market"]:
            print(f"[predict] no active market model for {symbol} - run `train` first")
            continue
        feats = compute_features(frames, symbol)
        feats = feats[feats.index <= pd.Timestamp(now)]
        row = feats.iloc[-1]
        cutoff = feats.index[-1].to_pydatetime()
        t_research = pd.Timestamp(datetime.now(timezone.utc))                      # research context as of this instant
        rrow = rfeat.research_frame(rdata, symbol, pd.DatetimeIndex([t_research]), hourly_only=False).iloc[0]
        full_row = pd.concat([row, rrow])
        rsnap = rfeat.snapshot(rdata, symbol, t_research, rrow)
        snap = snaps[symbol]
        micro = {k: snap.get(k) for k in ("spread_pct", "ob_imbalance", "buy_pressure", "volume_24h")}
        for h in [h for s, h in todo if s == symbol]:
            lo, hi, sigma = expected_range(snap["price"], frames[symbol]["close"], h)
            for variant, model in models.items():
                if not model:
                    continue
                bull, drivers = predict_probability(model, h, full_row)
                signal, gate = decide_signal(bull, snap["spread_pct"], cfg)
                created = datetime.now(timezone.utc)
                pred = {
                    "created_at": created, "symbol": symbol, "horizon_h": h, "target_time": created + timedelta(hours=h),
                    "price_at_prediction": snap["price"], "signal": signal,
                    "bullish_prob": bull, "bearish_prob": 1 - bull, "confidence": max(bull, 1 - bull),
                    "range_low": lo, "range_high": hi,
                    "reasons": JsonList(d["text"] + f" ({d['pushes']})" for d in drivers),
                    "explanation": explain(signal, bull, h, drivers, gate, sigma),
                    "model_version_id": model["id"], "model_version": model["version"],
                    "features": {"model_inputs": {k: (None if pd.isna(full_row[k]) else float(full_row[k])) for k in model["feature_names"]},
                                 "microstructure": micro, "drivers": drivers},
                    "data_cutoff": cutoff, "variant": variant, "run_id": run_id,
                    "research_features": {**rsnap, "used_by_model": variant == "research"},
                }
                pred["id"] = db.insert("predictions", pred, returning="id")
                made.append(pred)
    for pred in made:
        if pred["variant"] == cfg.get("trade_variant", "market"):
            act_on_prediction(db, pred, snaps[pred["symbol"]], prices, cfg)
    return made
