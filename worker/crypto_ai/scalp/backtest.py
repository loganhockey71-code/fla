"""Event-driven replay of the scalper through the SAME engine that trades live (engine.entry_decision / step_position /
close_position), one 1-minute candle at a time across all three coins with a shared account.

Honest by construction:
  * a signal formed from candle t's close is filled at candle t+1's OPEN (never at a price already gone)
  * with confirmation on (the live default) a prediction from candle t's close only trades if candle t+1 CONFIRMS it
    (engine.confirm_signal); the fill is then candle t+2's open. Failed confirmations are counted in `rejects` as confirm_<reason>
  * there is no historical news, so the backtest runs with no news gate (the live news layer is judged on live signals)
  * every exit is checked against the candle's high/low (intrabar); stop beats target when both are touched
  * fees, slippage and an assumed historical spread are charged on both legs (same costs.py as labels and live)
  * `walk_forward` trains only on data before each test block, picks the edge threshold on a separate validation block, and
    scores the test block untouched. Nothing reported here was used to fit or to choose anything.
"""
import numpy as np
import pandas as pd

from . import costs, engine
from .exit_policy import learn_policy
from .features import compute_features
from .model import build_dataset, fit_bundle, pick_threshold, predict_edges, predict_frame
from ..config import SYMBOLS

ONE_MIN = pd.Timedelta(minutes=1)


def replay(frames: dict[str, pd.DataFrame], pred: dict[str, pd.DataFrame], thr: float | dict, cfg: dict, policy: dict | None = None,
           start_equity: float | None = None, blocked: frozenset = frozenset(), t0=None, t1=None) -> dict:
    """`pred[sym]` = feature frame with edge_long/edge_short columns (index = candle open ts). Replays [t0, t1)."""
    start_equity = cfg["starting_balance"] if start_equity is None else start_equity
    syms = [s for s in SYMBOLS if s in pred]
    idx = pred[syms[0]].index
    for s in syms[1:]:
        idx = idx.intersection(pred[s].index)
    if t0 is not None:
        idx = idx[idx >= pd.Timestamp(t0)]
    if t1 is not None:
        idx = idx[idx < pd.Timestamp(t1)]
    A = {}
    for s in syms:
        p = pred[s].reindex(idx)
        A[s] = {"o": p["open"].to_numpy(), "h": p["high"].to_numpy(), "l": p["low"].to_numpy(), "c": p["close"].to_numpy(),
                "el": p["edge_long"].to_numpy(), "es": p["edge_short"].to_numpy(), "df": p}
    thr_of = (lambda s: thr[s]) if isinstance(thr, dict) else (lambda s: thr)
    cash = start_equity
    pos: dict[str, engine.Position] = {}
    gate = {s: engine.new_gate_state() for s in syms}
    pending: dict[str, dict] = {}                                          # signals waiting for their confirmation candle
    trades, curve, rejects = [], [], {}
    day, day_start = None, start_equity
    peaks: dict = {}
    confirm = bool(cfg["scalp_confirm_required"])
    for i in range(len(idx) - 1):
        ts = idx[i]
        if ts.date() != day:
            day, day_start = ts.date(), cash + sum(engine.mark_to_market_pct(p, A[p.symbol]["o"][i]) / 100 * p.notional for p in pos.values())
        for s in syms:
            a = A[s]
            p = pos.get(s)
            if p is not None:
                lo = max(0, i - int(cfg["scalp_reversal_bars"]))
                bar = {"ts": ts, "open": a["o"][i], "high": a["h"][i], "low": a["l"][i], "close": a["c"][i]}
                ex = engine.step_position(p, bar, pd.Series(a["c"][lo:i + 1]), cfg)
                if ex:
                    is_open_fill = p.pending_exit is not None and ex["reason"] == p.pending_exit
                    exit_ts = ts if is_open_fill else ts + ONE_MIN
                    tr = engine.close_position(p, ex["reason"], ex["mid"], exit_ts, costs.ASSUMED_SPREAD_PCT.get(s, 0.0), cfg)
                    cash += tr["net_pnl_usd"]
                    trades.append(tr)
                    del pos[s]
                    gate[s]["last_exit_ts"] = exit_ts
        equity_now = cash + sum(engine.mark_to_market_pct(p, A[p.symbol]["c"][i]) / 100 * p.notional for p in pos.values())
        peaks[ts.date()] = max(peaks.get(ts.date(), 0.0), equity_now)
        top = max(v for k, v in peaks.items() if (ts.date() - k).days < 7)
        dd = max(0.0, (top - equity_now) / top * 100) if top > 0 else 0.0
        ready = []
        for s in syms:
            a = A[s]
            el, es = a["el"][i], a["es"][i]
            if not (np.isfinite(el) and np.isfinite(es)):
                pending.pop(s, None)
                continue
            t_s = thr_of(s)
            eff = max(cfg["scalp_min_edge_pct"], t_s if np.isfinite(t_s) else np.inf)
            row = a["df"].iloc[i].to_dict()
            pend = pending.pop(s, None)
            if pend is not None:                                           # candle i is the confirmation candle for the signal raised at i-1
                d = pend["d"]
                res = engine.confirm_signal(d, pend["ref"], pend["atr"], {"open": a["o"][i], "high": a["h"][i], "low": a["l"][i], "close": a["c"][i]},
                                            el if d > 0 else es, eff, cfg)
                if not res["ok"]:
                    rejects[f"confirm_{res['reason']}"] = rejects.get(f"confirm_{res['reason']}", 0) + 1
                    continue
                rejects["confirmed"] = rejects.get("confirmed", 0) + 1
                ready.append((s, d, {1: el, -1: es}, row, True))
                continue
            engine.update_arming(gate[s], {1: el, -1: es}, eff)
            if max(el, es if cfg["scalp_allow_shorts"] else -np.inf) < eff:
                continue
            ctx = _ctx(pos, A, i, cash, day_start, dd, s)
            dec = engine.entry_decision(s, row, {1: el, -1: es}, a["o"][i + 1], costs.ASSUMED_SPREAD_PCT.get(s, 0.0), idx[i + 1], gate[s], ctx, cfg, policy, t_s, blocked)
            if not dec["enter"]:
                rejects[dec["reason"]] = rejects.get(dec["reason"], 0) + 1
                continue
            gate[s]["armed"][dec["direction"]] = False
            rejects["signals"] = rejects.get("signals", 0) + 1
            if confirm:
                pending[s] = {"d": dec["direction"], "ref": float(a["c"][i]), "atr": float(row["atr_1m_pct"])}
            else:
                ready.append((s, dec["direction"], {1: el, -1: es}, row, True))
        ready.sort(key=lambda c: (-c[2][c[1]], -(c[3].get("dollar_vol_15m") or 0.0)))        # best predicted net edge first, then deepest liquidity
        for s, d, edges, row, _ in ready:
            a = A[s]
            spread = costs.ASSUMED_SPREAD_PCT.get(s, 0.0)
            ctx = _ctx(pos, A, i, cash, day_start, dd, s)
            dec = engine.entry_decision(s, row, edges, a["o"][i + 1], spread, idx[i + 1], gate[s], ctx, cfg, policy, thr_of(s), blocked, skip_arming=True)
            if not dec["enter"] or dec["direction"] != d:
                why = dec["reason"] if not dec["enter"] else "direction_changed"
                rejects[why] = rejects.get(why, 0) + 1
                continue
            pos[s] = engine.open_position(s, dec, a["o"][i + 1], idx[i + 1], row, spread, cfg)
            gate[s]["armed"][d] = False
        if pos or i % 60 == 0:
            curve.append((ts, equity_now))
    last = len(idx) - 1                                                     # window end: flatten anything still open
    for s, p in list(pos.items()):
        tr = engine.close_position(p, "window_end", A[s]["c"][last], idx[last] + ONE_MIN, costs.ASSUMED_SPREAD_PCT.get(s, 0.0), cfg)
        cash += tr["net_pnl_usd"]
        trades.append(tr)
    curve.append((idx[last], cash))
    return {"trades": trades, "equity_curve": curve, "end_equity": cash, "rejects": rejects,
            "days": max((idx[-1] - idx[0]).total_seconds() / 86400, 1e-9) if len(idx) > 1 else 0.0}


def _ctx(pos: dict, A: dict, i: int, cash: float, day_start: float, drawdown_pct: float, symbol: str) -> dict:
    """The account state entry_decision needs, marked at candle i's close (shared by the signal and the post-confirmation decision)."""
    open_notional = sum(p.notional for p in pos.values())
    unreal = sum(engine.mark_to_market_pct(p, A[p.symbol]["c"][i]) / 100 * p.notional for p in pos.values())
    eq = cash + unreal
    return {"has_position": symbol in pos, "open_count": len(pos), "equity": eq, "free_cash": cash - open_notional,
            "day_pnl_pct": (eq - day_start) / day_start * 100 if day_start else 0.0, "open_notional": open_notional, "drawdown_pct": drawdown_pct}


# ---------------------------------------------------------------- metrics
def summarize(trades: list[dict], curve: list, start_equity: float, days: float) -> dict:
    n = len(trades)
    base = {"trades": n, "days": days, "trades_per_day": n / days if days else 0.0}
    if n == 0:
        return {**base, "win_rate": None, "net_pnl_usd": 0.0, "net_return_pct": 0.0, "profit_factor": None, "max_drawdown_pct": 0.0,
                "avg_win_usd": None, "avg_loss_usd": None, "avg_win_pct": None, "avg_loss_pct": None, "avg_duration_min": None,
                "fees_usd": 0.0, "slippage_usd": 0.0, "gross_pnl_usd": 0.0, "avg_mfe_pct": None, "avg_mae_pct": None,
                "avg_mfe_r": None, "avg_mae_r": None, "cost_casualties": 0}
    t = pd.DataFrame(trades)
    wins, losses = t[t["net_pnl_usd"] > 0], t[t["net_pnl_usd"] <= 0]
    eq = np.array([v for _, v in curve]) if curve else np.array([start_equity])
    dd = float(((np.maximum.accumulate(eq) - eq) / np.maximum.accumulate(eq)).max() * 100)
    return {**base,
            "win_rate": float((t["net_pnl_usd"] > 0).mean()),
            "net_pnl_usd": float(t["net_pnl_usd"].sum()), "net_return_pct": float(t["net_pnl_usd"].sum() / start_equity * 100),
            "profit_factor": float(wins["net_pnl_usd"].sum() / abs(losses["net_pnl_usd"].sum())) if len(losses) and losses["net_pnl_usd"].sum() else None,
            "max_drawdown_pct": dd,
            "avg_win_usd": float(wins["net_pnl_usd"].mean()) if len(wins) else None, "avg_loss_usd": float(losses["net_pnl_usd"].mean()) if len(losses) else None,
            "avg_win_pct": float(wins["net_pnl_pct"].mean()) if len(wins) else None, "avg_loss_pct": float(losses["net_pnl_pct"].mean()) if len(losses) else None,
            "avg_duration_min": float(t["duration_min"].mean()),
            "fees_usd": float(t["fees_usd"].sum()), "slippage_usd": float(t["slippage_usd"].sum()), "gross_pnl_usd": float(t["gross_pnl_usd"].sum()),
            "avg_mfe_pct": float(t["mfe_pct"].mean()), "avg_mae_pct": float(t["mae_pct"].mean()),
            "avg_mfe_r": float(t["mfe_r"].mean()), "avg_mae_r": float(t["mae_r"].mean()),
            "cost_casualties": int(((t["gross_pnl_usd"] > 0) & (t["net_pnl_usd"] <= 0)).sum())}


def breakdown(trades: list[dict], key: str, start_equity: float) -> dict:
    out = {}
    for k in sorted({t[key] for t in trades}):
        sub = [t for t in trades if t[key] == k]
        net = np.array([t["net_pnl_usd"] for t in sub])
        w, l = net[net > 0].sum(), abs(net[net <= 0].sum())
        out[k] = {"trades": len(sub), "win_rate": float((net > 0).mean()), "net_pnl_usd": float(net.sum()),
                  "profit_factor": float(w / l) if l else None, "avg_net_pct": float(np.mean([t["net_pnl_pct"] for t in sub])),
                  "avg_mfe_pct": float(np.mean([t["mfe_pct"] for t in sub])), "avg_mae_pct": float(np.mean([t["mae_pct"] for t in sub]))}
    return out


# ---------------------------------------------------------------- walk-forward
def walk_forward(frames: dict[str, pd.DataFrame], cfg: dict, train_days: float = 30, val_days: float = 6, test_days: float = 6,
                 learn_exits: bool = True, log=print, force_top_quantile: float | None = None) -> dict:
    """Expanding-window walk-forward. Block k: fit on everything before its validation window, choose the edge threshold (and
    optionally the exit policy) on the validation window, then replay the following test window. Equity carries across blocks."""
    dset = build_dataset(frames, cfg)
    if dset.empty:
        raise RuntimeError("no data")
    t_start, t_end = dset.index.min(), dset.index.max()
    base = {}
    feats = {s: compute_features(frames, s, base) for s in frames}
    embargo = pd.Timedelta(minutes=int(cfg["scalp_hold_bars"]) + 2)
    equity, all_trades, curve, rejects, folds = cfg["starting_balance"], [], [], {}, []
    v0 = t_start + pd.Timedelta(days=train_days)
    total_days = 0.0
    while v0 + pd.Timedelta(days=val_days + test_days * 0.5) <= t_end:
        v1 = v0 + pd.Timedelta(days=val_days)
        t1 = min(v1 + pd.Timedelta(days=test_days), t_end)
        train, val = dset[dset.index < v0 - embargo], dset[(dset.index >= v0) & (dset.index < v1 - embargo)]
        bundle = fit_bundle(train)
        vpred = predict_frame(bundle, val)
        picked = pick_threshold(vpred, cfg, cfg["scalp_allow_shorts"])
        thr = picked["thr"]
        if force_top_quantile is not None:                     # DIAGNOSTIC ONLY (never live): trade the model's top-q signals whether or not validation approved them
            thr = float(np.quantile(np.maximum(vpred["edge_long"], vpred["edge_short"]), 1 - force_top_quantile))
        pred = {}
        for s in frames:
            f = feats[s]
            f = f[(f.index >= v0 - pd.Timedelta(minutes=5)) & (f.index < t1 + ONE_MIN)].dropna(subset=["atr_1m_pct", "vol_ratio", "efficiency_30m"])
            el, es = (np.full(len(f), np.nan), np.full(len(f), np.nan)) if f.empty else predict_edges(bundle, f)
            p = f.copy()
            p["edge_long"], p["edge_short"] = el, es
            pred[s] = p
        policy = None
        pol_info = None
        if learn_exits and np.isfinite(thr):
            policy, pol_info = learn_policy(pred, thr, cfg, v0, v1)
        res = replay(frames, pred, thr, cfg, policy, start_equity=equity, t0=v1, t1=t1)
        folds.append({"val_window": [str(v0), str(v1)], "test_window": [str(v1), str(t1)], "thr": None if not np.isfinite(thr) else thr,
                      "val_pick": picked["chosen"], "policy": policy, "policy_info": pol_info, "test_trades": len(res["trades"]),
                      "train_rows": len(train)})
        log(f"[wf] test {v1:%m-%d}..{t1:%m-%d}  thr={'none (no positive-edge threshold)' if not np.isfinite(thr) else thr}  "
            f"trades={len(res['trades'])}  net=${res['end_equity'] - equity:+.2f}")
        equity = res["end_equity"]
        all_trades += res["trades"]
        curve += res["equity_curve"]
        total_days += res["days"]
        for k, v in res["rejects"].items():
            rejects[k] = rejects.get(k, 0) + v
        v0 = v0 + pd.Timedelta(days=test_days)
    start = cfg["starting_balance"]
    return {"summary": summarize(all_trades, curve, start, total_days), "by_symbol": breakdown(all_trades, "symbol", start),
            "by_regime": breakdown(all_trades, "regime", start), "by_setup": breakdown(all_trades, "setup", start),
            "by_direction": breakdown(all_trades, "direction", start), "by_exit": breakdown(all_trades, "exit_reason", start),
            "folds": folds, "rejects": rejects, "trades": all_trades, "equity_curve": curve, "start_equity": start,
            "end_equity": equity, "oos_days": total_days}
