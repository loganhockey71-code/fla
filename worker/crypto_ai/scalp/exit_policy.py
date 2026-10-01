"""Learns the stop / target / trailing multipliers from NET P&L and hands them to the live engine.

This replaces the old report-only `optimize-exits`: its result is a policy dict ({symbol or 'symbol|regime': multipliers}) that
`live.py` loads from the database on every pass and `engine.policy_for` applies to every new trade.

Method (walk-forward, never a shuffled split): take the entries the current model would have made in a window, replay each
one bar by bar through the real engine (`step_position` / `close_position`, so trailing, reversal and intrabar rules are
included) under every multiplier combo, and split the window in time into two halves. A combo is scored by its WORST half's
net P&L after costs, and it only replaces what is in force if that worst-half score beats the incumbent's by a margin, is
positive, and has enough trades. Bounds are enforced (engine.clamp_policy) and the stop is never widened after entry.
"""
import itertools

import numpy as np
import pandas as pd

from . import costs, engine

GRID_STOP = [0.7, 1.0, 1.4]
GRID_TP = [1.0, 1.6, 2.4]
GRID_TRAIL = [0.5, 0.7, 1.0]
MIN_TRADES = 30               # per symbol (per regime the bar is higher: MIN_TRADES_REGIME)
MIN_TRADES_REGIME = 40
MARGIN_PCT = 0.5              # worst-half total net % the challenger must beat the incumbent by
MAX_ENTRIES = 500


def select_entries(sym_df: pd.DataFrame, thr: float, cfg: dict, t0, t1) -> list[tuple]:
    """(row timestamp, direction, edge) for the entries the rule 'edge >= thr' produces in [t0, t1), one trade at a time
    (the next entry is allowed only after the default-exit hold window). With confirmation on (the live default) a prediction at
    row i only counts if row i+1 confirms it (engine.confirm_signal), and the entry is then that confirmation row - exactly the
    entries live trading would have made, so the exit multipliers are learned on the trades that actually happen."""
    w = sym_df[(sym_df.index >= pd.Timestamp(t0)) & (sym_df.index < pd.Timestamp(t1))]
    if w.empty or not np.isfinite(thr):
        return []
    el, es = w["edge_long"].to_numpy(), w["edge_short"].to_numpy()
    if not cfg["scalp_allow_shorts"]:
        es = np.full_like(es, -np.inf)
    eff = max(cfg["scalp_min_edge_pct"], thr)
    best = np.maximum(el, es)
    cand = np.flatnonzero(best >= eff)
    o, h, l, c, atr = (w[k].to_numpy(float) for k in ("open", "high", "low", "close", "atr_1m_pct"))
    confirm = bool(cfg["scalp_confirm_required"])
    out, k = [], 0
    hold = int(cfg["scalp_hold_bars"]) + int(cfg["scalp_cooldown_min"])
    while k < len(cand):
        i = int(cand[k])
        d = 1 if el[i] >= es[i] else -1
        if confirm:
            j = i + 1
            if j >= len(w) or not engine.confirm_signal(d, c[i], atr[i], {"open": o[j], "high": h[j], "low": l[j], "close": c[j]},
                                                         (el if d > 0 else es)[j], eff, cfg)["ok"]:
                k += 1
                continue
            out.append((w.index[j], d, float((el if d > 0 else es)[j])))
            k = np.searchsorted(cand, j + hold)
        else:
            out.append((w.index[i], d, float(best[i])))
            k = np.searchsorted(cand, i + hold)
    return out[-MAX_ENTRIES:]


def simulate_entry(a: dict, i: int, d: int, edge: float, row: dict, cfg: dict, pol: dict, spread: float) -> dict | None:
    """One entry replayed under multipliers `pol`. `a` = numpy arrays {o,h,l,c,ts}. None if the cost gate rejects it."""
    n = len(a["c"])
    if i + 2 >= n:
        return None
    mid = a["o"][i + 1]
    plan = engine.plan_exits(mid, d, row.get("atr_1m_pct"), cfg, pol)
    if plan is None or not engine.cost_gate(plan, cfg, spread)[0]:
        return None
    dec = {"direction": d, "plan": plan, "usd": 1000.0, "setup": engine.setup_of(row, d),
           "regime": engine.regime_of(row.get("vol_ratio"), row.get("efficiency_30m")), "edge": edge}
    pos = engine.open_position(row.get("_sym", "X"), dec, mid, a["ts"][i + 1], row, spread, cfg)
    nrev = int(cfg["scalp_reversal_bars"])
    for j in range(i + 1, min(i + 1 + int(cfg["scalp_hold_bars"]) + 3, n)):
        bar = {"ts": a["ts"][j], "open": a["o"][j], "high": a["h"][j], "low": a["l"][j], "close": a["c"][j]}
        ex = engine.step_position(pos, bar, pd.Series(a["c"][max(0, j - nrev):j + 1]), cfg)
        if ex:
            return engine.close_position(pos, ex["reason"], ex["mid"], a["ts"][j], spread, cfg)
    return engine.close_position(pos, "window_end", a["c"][min(i + int(cfg["scalp_hold_bars"]) + 2, n - 1)], a["ts"][-1], spread, cfg)


def _arrays(df: pd.DataFrame) -> dict:
    return {"o": df["open"].to_numpy(), "h": df["high"].to_numpy(), "l": df["low"].to_numpy(), "c": df["close"].to_numpy(), "ts": df.index}


def _score(nets_by_half: list[list[float]]) -> tuple[float, int]:
    totals = [sum(h) for h in nets_by_half]
    return min(totals), sum(len(h) for h in nets_by_half)


def _evaluate_combo(entries, a, df, cfg, pol, spread, sym) -> tuple[list[list[float]], list[dict]]:
    half = len(entries) // 2
    halves, trades = [[], []], []
    pos_of = df.index.get_indexer([e[0] for e in entries])
    for n, ((ts, d, edge), i) in enumerate(zip(entries, pos_of)):
        row = df.iloc[i].to_dict()
        row["_sym"] = sym
        t = simulate_entry(a, int(i), d, edge, row, cfg, pol, spread)
        if t is not None:
            halves[0 if n < half else 1].append(t["net_pnl_pct"])
            trades.append(t)
    return halves, trades


def optimize_group(entries, df, cfg, sym, incumbent: dict, min_trades: int) -> dict:
    """Best multipliers for this group of entries, or the incumbent (with the evidence) if nothing beats it."""
    a = _arrays(df)
    spread = costs.ASSUMED_SPREAD_PCT.get(sym, 0.0)
    inc_halves, _ = _evaluate_combo(entries, a, df, cfg, incumbent, spread, sym)
    inc_score, inc_n = _score(inc_halves)
    best, best_score, best_n = None, inc_score, inc_n
    tried = 0
    if len(entries) < 2 * 4:
        return {"policy": None, "reason": f"only {len(entries)} entries", "incumbent_score": inc_score, "tried": 0}
    for s, t, tr in itertools.product(GRID_STOP, GRID_TP, GRID_TRAIL):
        pol = engine.clamp_policy({"stop_mult": s, "tp_mult": t, "trail_mult": tr})
        halves, _ = _evaluate_combo(entries, a, df, cfg, pol, spread, sym)
        score, n = _score(halves)
        tried += 1
        if n >= min_trades and score > best_score:
            best, best_score, best_n = pol, score, n
    if best is not None and best_score > max(0.0, inc_score + MARGIN_PCT):
        return {"policy": best, "score": best_score, "n": best_n, "incumbent_score": inc_score, "tried": tried,
                "reason": f"worst-half net {best_score:+.2f}% vs incumbent {inc_score:+.2f}% over {best_n} trades"}
    return {"policy": None, "score": best_score, "n": best_n, "incumbent_score": inc_score, "tried": tried,
            "reason": "no combo beat the incumbent by the required margin"}


def learn_policy(pred: dict[str, pd.DataFrame], thr, cfg: dict, t0, t1, current: dict | None = None) -> tuple[dict, dict]:
    """Returns (policy, info). `policy` = the current policy with any group whose exits improved out-of-sample replaced;
    `info` explains every group so the dashboard and logs can show why nothing (or something) changed."""
    policy = dict(current or {})
    info = {}
    for sym, df in pred.items():
        t = thr[sym] if isinstance(thr, dict) else thr
        entries = select_entries(df, t, cfg, t0, t1)
        incumbent = engine.policy_for(policy, sym, "", cfg)
        res = optimize_group(entries, df, cfg, sym, incumbent, MIN_TRADES)
        info[sym] = {k: v for k, v in res.items() if k != "policy"} | {"entries": len(entries)}
        if res["policy"]:
            policy[sym] = res["policy"]
        # regime-specific multipliers only when a regime has enough of its own trades AND beats this coin's policy
        regimes = pd.Series([engine.regime_of(df["vol_ratio"].get(e[0]), df["efficiency_30m"].get(e[0])) for e in entries])
        for reg in regimes.unique():
            sub = [e for e, r in zip(entries, regimes) if r == reg]
            if len(sub) < MIN_TRADES_REGIME:
                continue
            inc = engine.policy_for(policy, sym, reg, cfg)
            r = optimize_group(sub, df, cfg, sym, inc, MIN_TRADES_REGIME)
            info[f"{sym}|{reg}"] = {k: v for k, v in r.items() if k != "policy"} | {"entries": len(sub)}
            if r["policy"]:
                policy[f"{sym}|{reg}"] = r["policy"]
    return policy, info
