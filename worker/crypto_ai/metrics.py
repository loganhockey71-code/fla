"""Accuracy / performance snapshots written to performance_metrics.

Prediction slices measure the SIGNALS (gross, before costs). The 'paper' slice measures the
simulated account (net of fees, spread and slippage). They are different questions - keep both.
"""
import numpy as np
import pandas as pd

from .config import HORIZONS, SYMBOLS

SLICES = ["all", "BUY", "HOLD", "SELL", "high_conf"]


def max_drawdown(series: list[float]) -> float | None:
    """Largest peak-to-trough fall of a cumulative curve (same units as the curve)."""
    if not series:
        return None
    arr = np.asarray(series, dtype=float)
    return float(np.max(np.maximum.accumulate(arr) - arr))


def _pf(wins: list[float], losses: list[float]):
    return float(sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None


def prediction_slice(df: pd.DataFrame) -> dict:
    """df: joined predictions+results rows for one slice, ordered by time."""
    n = len(df)
    sig_ret = df.apply(lambda r: r["actual_return_pct"] if r["signal"] == "BUY"
                       else (-r["actual_return_pct"] if r["signal"] == "SELL" else np.nan), axis=1).dropna().tolist()
    wins = [x for x in sig_ret if x > 0]
    losses = [x for x in sig_ret if x <= 0]
    return {
        "total_predictions": n,
        "correct_predictions": int(df["signal_correct"].sum()) if n else 0,
        "directional_accuracy": float(df["directional_correct"].mean()) if n else None,
        "win_rate": float(len(wins) / len(sig_ret)) if sig_ret else None,
        "avg_win": float(np.mean(wins)) if wins else None,
        "avg_loss": float(np.mean(losses)) if losses else None,
        "profit_factor": _pf(wins, losses),
        "max_drawdown": max_drawdown(np.cumsum(sig_ret).tolist()),
        "best_trade": float(max(sig_ret)) if sig_ret else None,
        "worst_trade": float(min(sig_ret)) if sig_ret else None,
        "extra": {"basis": "signal return %, gross of costs; SELL counted as if it profits from a fall",
                  "range_hit_rate": float(df["in_range"].mean()) if n else None},
    }


def paper_slice(trades: list[dict], equity: list[float]) -> dict:
    pnl = [t["pnl_pct"] for t in trades]
    usd = [t["pnl_usd"] for t in trades]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    peak, dd = -1e18, 0.0
    for v in equity:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak * 100 if peak > 0 else 0)
    return {
        "total_predictions": len(trades), "correct_predictions": len(wins),
        "directional_accuracy": None,
        "win_rate": float(len(wins) / len(pnl)) if pnl else None,
        "avg_win": float(np.mean(wins)) if wins else None,
        "avg_loss": float(np.mean(losses)) if losses else None,
        "profit_factor": _pf([u for u in usd if u > 0], [u for u in usd if u <= 0]),
        "max_drawdown": dd if equity else None,
        "best_trade": float(max(usd)) if usd else None,
        "worst_trade": float(min(usd)) if usd else None,
        "extra": {"basis": "closed paper trades, net of fees/spread/slippage; drawdown is % of portfolio value; "
                           "best/worst are USD", "pnl_usd_total": float(sum(usd))},
    }


def _slices(df: pd.DataFrame, variant: str) -> list[dict]:
    out = []
    empty = pd.DataFrame(columns=["signal", "actual_return_pct", "signal_correct", "directional_correct", "in_range"])
    for scope in ["ALL"] + SYMBOLS:
        for h in [None] + HORIZONS:
            sub = df
            if len(df):
                sub = df if scope == "ALL" else df[df["symbol"] == scope]
                sub = sub if h is None else sub[sub["horizon_h"] == h]
            for sl in SLICES:
                part = sub
                if len(sub):
                    part = sub if sl == "all" else (sub[sub["high_confidence"]] if sl == "high_conf" else sub[sub["signal"] == sl])
                m = prediction_slice(part) if len(part) else prediction_slice(empty)
                out.append({"scope": scope, "horizon_h": h, "slice": sl, "variant": variant, **m})
    return out


def recompute(db) -> None:
    rows = db.all("""select p.symbol, p.horizon_h, p.signal, p.bullish_prob, p.created_at, p.variant,
                            r.actual_return_pct, r.directional_correct, r.signal_correct, r.in_range, r.high_confidence
                     from predictions p join prediction_results r on r.prediction_id = p.id order by p.target_time""")
    df = pd.DataFrame(rows)
    out = []
    for variant in ("market", "research"):
        out += _slices(df[df["variant"] == variant] if len(df) else df, variant)
    trades = db.all("select * from paper_trades where status='closed' and account='ai' order by closed_at")
    equity = [r["total_value"] for r in db.all("select total_value from portfolio order by ts")]
    for scope in ["ALL"] + SYMBOLS:
        tr = trades if scope == "ALL" else [t for t in trades if t["symbol"] == scope]
        out.append({"scope": scope, "horizon_h": None, "slice": "paper", "variant": "market",
                    **paper_slice(tr, equity if scope == "ALL" else [])})
    import json
    db.run("delete from performance_metrics")
    for m in out:
        m = dict(m)
        m["extra"] = json.dumps(m["extra"])
        cols = list(m)
        db.run(f"insert into performance_metrics ({','.join(cols)}) values ({','.join(['%s'] * len(cols))})",
               [m[c] for c in cols])
