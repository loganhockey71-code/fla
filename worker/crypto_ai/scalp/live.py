"""The live (paper) scalper: the thin database wrapper around engine.py. PAPER TRADING ONLY.

One pass = sync new 1m candles -> replay EVERY candle since each open trade last saw one (so a late or missed run still
honours stops and targets to the minute) -> check the live price for the still-forming minute -> look for new trades.
Safe to call as often as you like (every ~20 s in a burst, or once per tick); a Postgres advisory lock keeps two workers
from trading at once.

The loop the whole system implements:  analyse -> predict a short-term move -> WAIT FOR THE NEXT CANDLE TO CONFIRM -> trade ->
monitor -> exit -> learn.
  * A prediction that clears the model's edge threshold and every gate becomes a SIGNAL (`scalp_signals`, status 'pending').
  * When the next 1-minute candle has closed, `engine.confirm_signal` checks it agreed (closed the predicted way, followed
    through, did not reverse first, did not already run, and the model's fresh edge still holds). Confirmed -> the trade is
    opened at the live price; anything else -> NO TRADE, and the failure is recorded.
  * EVERY signal, traded or not, is graded afterwards against what the market actually did (`learn.grade_signals`), so wrong
    calls that never traded teach as much as the ones that did.

Robustness: managing OPEN trades (stops, targets, kill switch) is committed even if the entry side of the pass fails (a bad
model, a news query error): a failure to look for new trades must never leave an open trade unmanaged.

Kill switch: `autopilot_enabled` (strictly a boolean - see config.as_bool). Off means no new entries AND any open trade is
flattened at the live price, so turning it off actually stops the risk.
"""
import copy
import json
import sys
import time
import traceback
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import psycopg2.extras
from psycopg2.extras import Json

from . import data, engine, learn, model, news, patterns
from .features import compute_features
from .. import coinbase
from ..config import PRODUCTS, SYMBOLS, require_paper_only

LOCK = 778900
ONE_MIN = pd.Timedelta(minutes=1)
MAX_DATA_AGE = pd.Timedelta(minutes=4)      # entries need a candle at most this old (else the data feed is stale: don't trade blind)
PEAK_DAYS = 7                               # drawdown is measured from the highest equity of the last this-many UTC days

# gates that describe the PREDICTION or the MARKET (not the account's current state): when one of them refuses a prediction the signal is
# still recorded as 'blocked' (and graded later), so we learn whether the gate was right. Account-state gates (positions, cooldown, loss
# limits, exposure, spread) are transient and are not recorded as signals.
RECORDED_BLOCKS = {"setup_blocked", "news_conflict", "news_opposes", "illiquid", "liquidity_unknown", "cost_gate", "no_volatility_estimate", "size_too_small"}

_POS_FIELDS = {"symbol", "direction", "entry_ts", "entry_mid", "entry_fill", "qty", "notional", "stop_px", "initial_stop_px", "tp_px",
               "unit_pct", "atr_pct", "max_hold", "trail_act_r", "trail_mult", "best_px", "setup", "regime", "pred_edge_pct", "features",
               "bars_held", "mfe_pct", "mae_pct", "trail_active", "stop_moves", "pending_exit", "last_bar_ts", "spread_pct"}


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def position_from_row(r: dict) -> engine.Position:
    st = r["state"] or {}
    return engine.Position(
        symbol=r["symbol"], direction=1 if r["direction"] == "long" else -1, entry_ts=_ts(r["opened_at"]), entry_mid=r["entry_mid"],
        entry_fill=r["entry_price"], qty=r["qty"], notional=r["notional"], stop_px=r["stop_px"], initial_stop_px=r["initial_stop_px"],
        tp_px=r["take_profit_px"], unit_pct=r["unit_pct"], atr_pct=st.get("atr_pct", r["unit_pct"]), max_hold=int(st.get("max_hold", 15)),
        trail_act_r=st.get("trail_act_r", 0.8), trail_mult=st.get("trail_mult", 0.7), best_px=r["best_px"] or r["entry_mid"], setup=r["setup"],
        regime=r["regime"], pred_edge_pct=r["pred_edge_pct"] or 0.0, features=r["features"] or {}, bars_held=int(st.get("bars_held", 0)),
        mfe_pct=r["mfe_pct"] or 0.0, mae_pct=r["mae_pct"] or 0.0, trail_active=bool(r["trail_active"]), stop_moves=int(r["stop_moves"] or 0),
        pending_exit=st.get("pending_exit"), last_bar_ts=_ts(r["last_bar_ts"]) if r["last_bar_ts"] is not None else None,
        spread_pct=st.get("spread_pct", 0.0), slip_pct=st.get("slip_pct"))


def _pos_state(p: engine.Position) -> dict:
    return {"bars_held": p.bars_held, "pending_exit": p.pending_exit, "atr_pct": p.atr_pct, "max_hold": p.max_hold, "trail_act_r": p.trail_act_r,
            "trail_mult": p.trail_mult, "spread_pct": p.spread_pct, "slip_pct": p.slip_pct}


def _save_open(cur, trade_id: int, p: engine.Position) -> None:
    cur.execute("""update scalp_trades set stop_px=%s, best_px=%s, trail_active=%s, stop_moves=%s, mfe_pct=%s, mae_pct=%s, last_bar_ts=%s, state=%s
                   where id=%s""",
                (p.stop_px, p.best_px, p.trail_active, p.stop_moves, p.mfe_pct, p.mae_pct, p.last_bar_ts, Json(_pos_state(p)), trade_id))


def _book_close(cur, trade_id: int, tr: dict) -> None:
    cur.execute("""update scalp_trades set status='closed', closed_at=%s, duration_min=%s, exit_mid=%s, exit_price=%s, stop_px=%s, trail_active=%s,
                   stop_moves=%s, mfe_pct=%s, mae_pct=%s, mfe_r=%s, mae_r=%s, fees_usd=%s, slippage_usd=%s, gross_pnl_usd=%s, net_pnl_usd=%s,
                   net_pnl_pct=%s, exit_reason=%s, profitable=%s, state='{}' where id=%s""",
                (tr["exit_ts"].to_pydatetime(), tr["duration_min"], tr["exit_mid"], tr["exit_price"], tr["final_stop_px"], tr["trail_active"], tr["stop_moves"],
                 tr["mfe_pct"], tr["mae_pct"], tr["mfe_r"], tr["mae_r"], tr["fees_usd"], tr["slippage_usd"], tr["gross_pnl_usd"], tr["net_pnl_usd"],
                 tr["net_pnl_pct"], tr["exit_reason"], tr["profitable"], trade_id))
    learn.record_lesson(cur, {**tr, "id": trade_id})


def _live_check(p: engine.Position, mid: float) -> dict | None:
    """The still-forming minute has no closed candle yet: use the live price so a stop or target touched since the last
    candle is not missed. Filled at the live price if it is already beyond the level (never better than the stop)."""
    d = p.direction
    if (mid <= p.stop_px) if d > 0 else (mid >= p.stop_px):
        return {"reason": "trailing_stop" if p.trail_active else "stop_loss", "mid": mid}
    if (mid >= p.tp_px) if d > 0 else (mid <= p.tp_px):
        return {"reason": "take_profit", "mid": p.tp_px}
    return None


def _state(cur, key, default=None):
    cur.execute("select value from scalp_state where key=%s", (key,))
    r = cur.fetchone()
    return r["value"] if r else default


def _put_state(cur, key, value) -> None:
    cur.execute("""insert into scalp_state (key, value, updated_at) values (%s, %s::jsonb, now())
                   on conflict (key) do update set value = excluded.value, updated_at = now()""", (key, json.dumps(value, default=str)))


def live_prices(symbols=SYMBOLS) -> dict:
    out = {}
    for s in symbols:
        try:
            out[s] = coinbase.ticker(PRODUCTS[s])
        except Exception:
            pass
    return out


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def _snapshot(row: dict, extra: dict | None = None) -> dict:
    """The entry conditions worth keeping with a signal / trade: model-feature values (finite floats) + anything in `extra`."""
    out = {k: _num(v) for k, v in row.items() if k in engine._ENTRY_FEATURES}
    if row.get("dollar_vol_15m") is not None:
        out["dollar_vol_15m"] = _num(row.get("dollar_vol_15m"))
    out.update(extra or {})
    return out


# ---------------------------------------------------------------- signals (every prediction is recorded)
def _insert_signal(cur, s: str, d: int, decision_ts, ref_price: float, edge: float, atr: float, setup: str, regime: str, features: dict,
                   news_ctx: dict, model_id, status: str, reason: str | None = None) -> int | None:
    cur.execute("""insert into scalp_signals (symbol, direction, decision_ts, ref_price, pred_edge_pct, atr_pct, setup, regime, features, news, model_id,
                       status, reason, resolved_at)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict (symbol, decision_ts) do nothing returning id""",
                (s, "long" if d > 0 else "short", decision_ts.to_pydatetime(), ref_price, edge, atr, setup, regime, Json(features), Json(news_ctx), model_id,
                 status, reason, None if status == "pending" else datetime.now(timezone.utc)))
    r = cur.fetchone()
    return r["id"] if r else None


def _resolve_signal(cur, sig_id: int, status: str, reason: str | None, **extra) -> None:
    sets = ["status=%s", "reason=%s", "resolved_at=now()"] + [f"{k}=%s" for k in extra]
    cur.execute(f"update scalp_signals set {', '.join(sets)} where id=%s and status='pending'", [status, reason, *extra.values(), sig_id])


def _drawdown(cur, equity: float, now: datetime) -> float:
    """% below the highest equity of the last PEAK_DAYS UTC days. Updates the stored daily peaks."""
    peaks = _state(cur, "equity_peaks", {}) or {}
    today = now.astimezone(timezone.utc).date().isoformat()
    peaks[today] = max(float(peaks.get(today, 0.0)), equity)
    keep = sorted(peaks)[-PEAK_DAYS:]
    peaks = {k: peaks[k] for k in keep}
    _put_state(cur, "equity_peaks", peaks)
    top = max(peaks.values())
    return max(0.0, (top - equity) / top * 100) if top > 0 else 0.0


def _open_trade(cur, s: str, p: engine.Position, dec: dict, now: datetime, model_id: int, policy: dict | None, cfg: dict, signal_id: int | None) -> int:
    cur.execute("""insert into scalp_trades (symbol, direction, status, opened_at, entry_mid, entry_price, qty, notional, initial_stop_px, stop_px,
                   take_profit_px, stop_dist_pct, tp_dist_pct, unit_pct, best_px, setup, regime, pred_edge_pct, features, model_id, policy, last_bar_ts, state,
                   signal_id)
                   values (%s,%s,'open',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
                (s, "long" if p.direction > 0 else "short", now, p.entry_mid, p.entry_fill, p.qty, p.notional, p.initial_stop_px, p.stop_px,
                 p.tp_px, dec["plan"]["stop_dist_pct"], dec["plan"]["tp_dist_pct"], p.unit_pct, p.best_px, p.setup, p.regime, p.pred_edge_pct,
                 Json(p.features), model_id, Json(engine.policy_for(policy, s, p.regime, cfg)), p.last_bar_ts, Json(_pos_state(p)), signal_id))
    return cur.fetchone()["id"]


# ---------------------------------------------------------------- the pass
def run_pass(db, cfg: dict, now: datetime | None = None, prices: dict | None = None) -> dict:
    require_paper_only()
    now = now or datetime.now(timezone.utc)
    enabled = bool(cfg["autopilot_enabled"])
    frames = {s: data.sync_live(db, s, now) for s in SYMBOLS}
    prices = prices if prices is not None else live_prices()
    model_row = learn.load_active_model(db)
    policy = learn.load_policy(db)
    blocked = learn.load_blocked(db)
    fresh = all(len(frames[s]) > 200 and _ts(now) - (frames[s].index[-1] + ONE_MIN) <= MAX_DATA_AGE for s in SYMBOLS)
    feats = {}
    if fresh:
        try:
            base = {}
            feats = {s: compute_features(frames, s, base) for s in SYMBOLS}
        except Exception:
            traceback.print_exc(file=sys.stderr)
            fresh, feats = False, {}

    conn = db.conn
    conn.autocommit = False
    done, opened, rejected, signals_note = [], [], {}, {"raised": 0, "confirmed": 0, "failed": {}, "expired": 0, "blocked": {}}
    entry_error = None
    reads: dict = {}
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("select pg_advisory_xact_lock(%s)", (LOCK,))
            cur.execute("select * from scalp_trades where status='open' order by id")
            open_rows = cur.fetchall()
            gates = _state(cur, "gate", {}) or {}

            # ---- 1. manage open trades: every candle since last seen, then the live price. ALWAYS committed (see module docstring).
            still_open = {}
            for r in open_rows:
                s = r["symbol"]
                p = position_from_row(r)
                mid = prices.get(s, {}).get("price")
                spread = (prices.get(s) or {}).get("spread_pct") or p.spread_pct
                ex, ex_ts = None, None
                if not enabled:
                    ex, ex_ts = ({"reason": "kill_switch", "mid": mid}, _ts(now)) if mid else (None, None)
                else:
                    df = frames[s]
                    new = df[df.index > p.last_bar_ts] if p.last_bar_ts is not None else df.iloc[0:0]
                    closes = df["close"]
                    for ts, b in new.iterrows():
                        upto = closes.loc[:ts].iloc[-(int(cfg["scalp_reversal_bars"]) + 1):]
                        ex = engine.step_position(p, {"ts": ts, "open": b.open, "high": b.high, "low": b.low, "close": b.close}, upto, cfg)
                        if ex:
                            ex_ts = ts if (p.pending_exit and ex["reason"] == p.pending_exit) else ts + ONE_MIN
                            break
                    if ex is None and mid:
                        ex = _live_check(p, mid)
                        if ex is None and p.pending_exit:                    # decided at the last close: execute at the live price now
                            ex = {"reason": p.pending_exit, "mid": mid}
                        ex_ts = _ts(now) if ex else None
                if ex and ex["mid"]:
                    tr = engine.close_position(p, ex["reason"], ex["mid"], ex_ts, spread or 0.0, cfg)
                    _book_close(cur, r["id"], tr)
                    gates.setdefault(s, engine.new_gate_state())
                    gates[s]["last_exit_ts"] = str(ex_ts)
                    done.append(f"{s} {tr['direction']} closed ({tr['exit_reason']}) net {tr['net_pnl_usd']:+.2f} USD")
                else:
                    _save_open(cur, r["id"], p)
                    still_open[s] = p

            # ---- 2. new entries. Isolated in a savepoint: if it fails, step 1 still commits and the error is reported.
            cur.execute("select coalesce(sum(net_pnl_usd),0) x from scalp_trades where status='closed'")
            realized = cur.fetchone()["x"]
            day0 = _ts(now).floor("1D").to_pydatetime()
            cur.execute("select coalesce(sum(net_pnl_usd),0) x from scalp_trades where status='closed' and closed_at >= %s", (day0,))
            day_pnl = cur.fetchone()["x"]
            cash = cfg["starting_balance"] + realized
            unreal = sum(engine.mark_to_market_pct(p, prices[s]["price"]) / 100 * p.notional for s, p in still_open.items() if s in prices)
            day_start = cash - day_pnl
            cur.execute("savepoint entry_stage")
            gates_before, open_before = copy.deepcopy(gates), dict(still_open)       # the stage mutates these in memory: undo that too if it fails
            try:
                reads = _entry_stage(cur, cfg, now, enabled, fresh, frames, feats, prices, model_row, policy, blocked, gates, still_open, cash, unreal,
                                     day_pnl, day_start, opened, rejected, signals_note)
            except Exception as e:                                          # never let the entry side take the exit side down with it
                traceback.print_exc(file=sys.stderr)
                cur.execute("rollback to savepoint entry_stage")
                entry_error = f"{type(e).__name__}: {str(e)[:160]}"
                opened.clear()
                gates.clear()
                gates.update(gates_before)          # the database rolled the new signals/trades back, so the arming latches must go back with them
                still_open.clear()
                still_open.update(open_before)
            _put_state(cur, "gate", gates)
            thr = None if not model_row or not np.isfinite(model_row["thr"]) else model_row["thr"]
            note = ("Autopilot is OFF: no new trades." if not enabled else
                    f"Entry side failed ({entry_error}); open trades are still being managed." if entry_error else
                    "Waiting for fresh 1-minute candles." if not fresh else
                    "No trained model yet: not trading." if not model_row else
                    "Model found no threshold with a positive net edge after costs: not trading." if thr is None else "Running.")
            status = {"at": now.isoformat(), "enabled": enabled, "fresh_data": fresh, "model": model_row["id"] if model_row else None, "threshold": thr,
                      "opened": opened, "closed": done, "rejected": rejected, "signals": signals_note, "reads": reads, "open": len(still_open),
                      "confirmation_required": bool(cfg["scalp_confirm_required"]), "error": entry_error, "note": note}
            _put_state(cur, "status", status)
            _put_state(cur, "heartbeat", {"at": now.isoformat()})
        conn.commit()
        return {"enabled": enabled, "opened": opened, "closed": done, "rejected": rejected, "note": note, "signals": signals_note, "error": entry_error}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True


def _entry_stage(cur, cfg, now, enabled, fresh, frames, feats, prices, model_row, policy, blocked, gates, still_open, cash, unreal, day_pnl, day_start,
                 opened, rejected, sig_note) -> dict:
    """Resolve pending signals, raise new ones, and open confirmed trades. Mutates `still_open`, `gates`, `opened`, `rejected`, `sig_note`.
    Returns the per-coin 'live read' the dashboard shows."""
    nowts = _ts(now)
    delay = pd.Timedelta(seconds=float(cfg["scalp_confirm_max_delay_s"]))
    cur.execute("select * from scalp_signals where status='pending' order by id")
    pend = {r["symbol"]: r for r in cur.fetchall()}
    reads: dict = {}

    # a pending signal that can no longer be confirmed in time is closed out, whatever else is going on
    for s, r in list(pend.items()):
        conf_close = _ts(r["decision_ts"]) + 2 * ONE_MIN                      # the confirmation candle closes two minutes after the decision candle opened
        if not enabled:
            _resolve_signal(cur, r["id"], "expired", "autopilot_off")
            sig_note["expired"] += 1
            del pend[s]
        elif nowts - conf_close > delay:
            _resolve_signal(cur, r["id"], "expired", "confirmation_too_late")
            sig_note["expired"] += 1
            del pend[s]
    if not (enabled and fresh and model_row and np.isfinite(model_row["thr"])):
        return reads

    equity = cash + unreal
    dd = _drawdown(cur, equity, now)
    open_notional = sum(p.notional for p in still_open.values())
    eff = max(cfg["scalp_min_edge_pct"], model_row["thr"])
    ready: list[dict] = []                                                    # confirmed (or unconfirmed-mode) candidates, opened best-first below

    for s in SYMBOLS:
        tick = prices.get(s)
        if not tick:
            continue
        f_all = feats[s]
        last_ts = f_all.index[-1]
        f = f_all.iloc[[-1]]
        if f[["atr_1m_pct", "vol_ratio", "efficiency_30m"]].isna().any(axis=None):
            continue
        el, es = (float(x[0]) for x in model.predict_edges(model_row["bundle"], f))
        g = gates.setdefault(s, engine.new_gate_state())
        g["armed"] = {int(k): v for k, v in g["armed"].items()}
        lx = g.get("last_exit_ts")
        g["last_exit_ts"] = _ts(lx) if lx else None
        spread = tick.get("spread_pct") or 0.0
        row = f.iloc[0].to_dict()
        try:
            nctx = news.context(news.load_events(cur_db(cur), s, now, cfg["scalp_news_window_min"]), now, cfg) if cfg["scalp_news_enabled"] else dict(news.NEUTRAL_CTX)
        except Exception:                                                     # news is context, never a reason to stop managing the book
            nctx = dict(news.NEUTRAL_CTX)
        reads[s] = {"long": round(el, 4), "short": round(es, 4), "threshold": round(eff, 4), "news": nctx["bias"], "news_net": nctx["net"],
                    "pending": s in pend, "price": tick["price"], "at": now.isoformat()}
        ctx = {"has_position": s in still_open, "open_count": len(still_open), "equity": equity, "free_cash": cash - open_notional,
               "day_pnl_pct": (day_pnl + unreal) / day_start * 100 if day_start else 0.0, "open_notional": open_notional, "drawdown_pct": dd,
               "news": news.effects(nctx, cfg)}
        pat = patterns.candle_patterns(frames[s])
        snap = _snapshot(row, {"patterns": pat, "news_bias": nctx["bias"], "news_net": nctx["net"], "news_confirmations": nctx["confirmations"]})

        sig = pend.get(s)
        if sig is not None:                                                     # ---- a prediction is waiting for its confirmation candle
            d = 1 if sig["direction"] == "long" else -1
            conf_ts = _ts(sig["decision_ts"]) + ONE_MIN
            if conf_ts not in frames[s].index or conf_ts not in f_all.index:
                continue                                                      # the confirmation candle has not closed yet: keep waiting
            crow = f_all.loc[[conf_ts]]
            if crow[["atr_1m_pct", "vol_ratio", "efficiency_30m"]].isna().any(axis=None):
                _resolve_signal(cur, sig["id"], "expired", "no_features_on_confirmation")
                sig_note["expired"] += 1
                continue
            cel, ces = (float(x[0]) for x in model.predict_edges(model_row["bundle"], crow))
            reads[s]["pending"] = False                                           # resolved this pass (confirmed, refused or expired): no longer waiting
            b = frames[s].loc[conf_ts]
            res = engine.confirm_signal(d, sig["ref_price"], sig["atr_pct"], {"open": b.open, "high": b.high, "low": b.low, "close": b.close},
                                        cel if d > 0 else ces, eff, cfg)
            if not res["ok"]:
                _resolve_signal(cur, sig["id"], "failed", res["reason"], confirm_move_atr=_num(res["move_atr"]), confirm_edge_pct=_num(cel if d > 0 else ces))
                sig_note["failed"][res["reason"]] = sig_note["failed"].get(res["reason"], 0) + 1
                continue
            ready.append({"s": s, "edges": {1: cel, -1: ces}, "row": crow.iloc[0].to_dict(), "tick": tick, "spread": spread, "g": g, "ctx": ctx, "sig": sig,
                          "confirm": res, "snap": snap, "dir": d})
            continue

        # ---- no waiting signal: is there a new prediction?
        update_edges = {1: el, -1: es}
        engine.update_arming(g, update_edges, eff)
        dec = engine.entry_decision(s, row, update_edges, tick["price"], spread, nowts, g, ctx, cfg, policy, model_row["thr"], blocked)
        best = max(el, es if cfg["scalp_allow_shorts"] else -np.inf)
        if not dec["enter"]:
            if best >= eff:
                rejected[dec["reason"]] = rejected.get(dec["reason"], 0) + 1
            if best >= eff and dec["reason"] in RECORDED_BLOCKS:               # a real prediction a gate refused: record it, grade it, learn from it
                d = 1 if (not cfg["scalp_allow_shorts"] or el >= es) else -1
                if g["armed"][d]:
                    _insert_signal(cur, s, d, last_ts, float(row["close"]), update_edges[d], float(row["atr_1m_pct"]),
                                   dec.get("setup") or engine.setup_of(row, d), dec.get("regime") or "", snap, nctx, model_row["id"], "blocked", dec["reason"])
                    g["armed"][d] = False
                    sig_note["blocked"][dec["reason"]] = sig_note["blocked"].get(dec["reason"], 0) + 1
            continue
        d = dec["direction"]
        sid = _insert_signal(cur, s, d, last_ts, float(row["close"]), dec["edge"], float(row["atr_1m_pct"]), dec["setup"], dec["regime"], snap, nctx,
                             model_row["id"], "pending" if cfg["scalp_confirm_required"] else "confirmed", None if cfg["scalp_confirm_required"] else "unconfirmed_mode")
        g["armed"][d] = False                                                  # one lingering prediction = one signal
        sig_note["raised"] += 1
        if cfg["scalp_confirm_required"]:
            reads[s]["pending"] = True
            continue                                                           # wait for the next candle: no trade yet
        ready.append({"s": s, "edges": update_edges, "row": row, "tick": tick, "spread": spread, "g": g, "ctx": ctx, "sig": {"id": sid, "decision_ts": last_ts},
                      "confirm": None, "snap": snap, "dir": d, "skip_arming": True})

    # ---- open the best candidates first (highest predicted NET edge in %, then deepest liquidity): never "the cheapest coin" or the first alphabetically
    ready.sort(key=lambda c: (-(c["edges"][c["dir"]]), -(c["row"].get("dollar_vol_15m") or 0.0)))
    for c in ready:
        s, g, sig = c["s"], c["g"], c["sig"]
        ctx = {**c["ctx"], "has_position": s in still_open, "open_count": len(still_open), "equity": equity, "free_cash": cash - open_notional,
               "open_notional": open_notional}
        dec = engine.entry_decision(s, c["row"], c["edges"], c["tick"]["price"], c["spread"], nowts, g, ctx, cfg, policy, model_row["thr"], blocked, skip_arming=True)
        if not dec["enter"] or dec["direction"] != c["dir"]:
            why = dec["reason"] if not dec["enter"] else "direction_changed"
            if sig and sig.get("id") and c["confirm"] is not None:
                _resolve_signal(cur, sig["id"], "failed", f"blocked_after_confirmation:{why}", confirm_move_atr=_num(c["confirm"]["move_atr"]))
                sig_note["failed"][f"blocked:{why}"] = sig_note["failed"].get(f"blocked:{why}", 0) + 1
            rejected[why] = rejected.get(why, 0) + 1
            continue
        minute = nowts.floor("1min")
        extra = {"signal_id": sig["id"] if sig else None, **c["snap"]}
        if c["confirm"] is not None:
            extra.update({"confirm_move_atr": _num(c["confirm"]["move_atr"]), "confirm_adverse_atr": _num(c["confirm"]["adverse_atr"])})
        extra.update({"news_alignment": (dec.get("news") or {}).get("alignment", "none"), "impact_pct": _num(dec.get("impact_pct")),
                      "liquidity_usd": _num(dec.get("liquidity_usd"))})
        p = engine.open_position(s, dec, c["tick"]["price"], minute, c["row"], c["spread"], cfg)
        p.last_bar_ts = minute              # the forming candle already traded before we got in: only later candles apply
        p.features = {**p.features, **{k: v for k, v in extra.items() if k not in p.features}}
        tid = _open_trade(cur, s, p, dec, now, model_row["id"], policy, cfg, sig["id"] if sig else None)
        if sig and sig.get("id") and c["confirm"] is not None:
            _resolve_signal(cur, sig["id"], "confirmed", "confirmed", confirm_move_atr=_num(c["confirm"]["move_atr"]), confirm_edge_pct=_num(c["edges"][c["dir"]]), trade_id=tid)
        elif sig and sig.get("id"):
            cur.execute("update scalp_signals set trade_id=%s where id=%s", (tid, sig["id"]))
        sig_note["confirmed"] += 1
        g["armed"][dec["direction"]] = False
        still_open[s] = p
        open_notional += p.notional
        g["last_exit_ts"] = str(g["last_exit_ts"]) if g["last_exit_ts"] is not None else None
        opened.append(f"{s} {'long' if p.direction > 0 else 'short'} at {p.entry_fill:,.4f} ({p.setup}, edge {p.pred_edge_pct:+.2f}%"
                      + (f", confirmed by the next candle: {c['confirm']['move_atr']:+.2f} ATR" if c["confirm"] else "") + ")")
    for s in SYMBOLS:                                                           # JSON-safe gate state (timestamps as strings)
        g = gates.get(s)
        if g is not None and g.get("last_exit_ts") is not None and not isinstance(g["last_exit_ts"], str):
            g["last_exit_ts"] = str(g["last_exit_ts"])
    return reads


class _CursorDB:
    """Lets the pure-ish news loader run its read through the pass's own cursor (inside the same transaction)."""

    def __init__(self, cur):
        self.cur = cur

    def all(self, sql, params=None):
        self.cur.execute(sql, params)
        return [dict(r) for r in self.cur.fetchall()] if self.cur.description else []


def cur_db(cur) -> _CursorDB:
    return _CursorDB(cur)


def burst(db_factory, cfg_loader, seconds: int, every: int, on_line=print) -> None:
    """Run passes every `every` seconds for `seconds` (a GitHub Actions run covers ~4 of its 5 minutes this way)."""
    end = time.time() + seconds
    db = db_factory()
    while True:
        cfg = cfg_loader(db)
        r = run_pass(db, cfg)
        for line in r["opened"] + r["closed"]:
            on_line(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] SCALP {line}")
        if time.time() + every > end:
            return
        time.sleep(every)
