"""Self-learning for the scalper. From 05:00 through 23:59 LOCAL time it keeps analysing completed trades and the market,
but it never lets one trade change the model:

  * grade_signals   EVERY prediction (traded, rejected by the next candle, or blocked by a gate) is graded against what the market
                    really did afterwards: right / wrong / flat, and whether it would have paid after costs. Wrong calls that never
                    traded are studied exactly like the ones that did (`scalp_signals`). Grading only reads candles AFTER the decision.
  * post_mortem     every closed trade gets a rule-based verdict + plain-English lesson (append-only, `scalp_lessons`)
  * group_stats     rolling expectancy after costs per coin / direction / setup / regime (last N days)
  * blocked_setups  a setup is blocked only after >= N trades AND mean + z*stderr < 0 (confidently negative); the rolling
                    window makes a blocked setup age out and get a fresh probation sample instead of staying dead forever
  * insights        the lessons in aggregate ("XRP shorts work when volume is up and price breaks below VWAP", ...)
  * exit policy     walk-forward search of stop/target/trail multipliers on NET P&L, applied only if better out-of-sample
  * retrain         challenger model vs champion on an unseen holdout; promoted only if it nets more with a paired bootstrap

The pure functions here are unit tested; the `run_*` functions are the thin database wrappers `live.py` calls.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from psycopg2.extras import Json

from . import costs, data, exit_policy, model
from .features import compute_features
from ..config import SYMBOLS


# ---------------------------------------------------------------- learning window (local time)
def local_tz(cfg: dict):
    name = (cfg.get("local_timezone") or os.environ.get("LOCAL_TZ") or "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def in_learning_window(now_utc: datetime, cfg: dict) -> bool:
    """05:00 <= local time < 24:00 (i.e. through 23:59). Trading itself is not gated by this window."""
    h = now_utc.astimezone(local_tz(cfg)).hour
    return cfg["learn_window_start_h"] <= h < cfg["learn_window_end_h"]


# ---------------------------------------------------------------- post-mortem (pure)
def post_mortem(t: dict) -> dict:
    """Why did this trade work or fail? `t` = a closed trade row/dict. Returns {verdict, tags, lesson, details}."""
    sym, dirn = t["symbol"], t["direction"]
    net, gross = t["net_pnl_usd"], t["gross_pnl_usd"]
    dur = t.get("duration_min") or 0.0
    mfe_r, mae_r = t.get("mfe_r") or 0.0, t.get("mae_r") or 0.0
    reason = t.get("exit_reason")
    f = t.get("features") or {}
    if isinstance(f, str):
        f = json.loads(f)
    vol, vw = f.get("vol_rel_5m"), f.get("dist_vwap_15m")
    tags, cost = [], (t.get("fees_usd") or 0.0) + (t.get("slippage_usd") or 0.0)
    who = f"{sym} {dirn}"
    if net > 0:
        verdict = "win"
        if reason == "take_profit":
            tags.append("target_hit")
            text = f"{who} worked: hit its target in {dur:.0f} min (net {t['net_pnl_pct']:+.2f}%)."
        elif reason == "trailing_stop":
            tags.append("trailed_win")
            text = f"{who} worked: peaked at +{t['mfe_pct']:.2f}% and the trailing stop banked {t['net_pnl_pct']:+.2f}% net."
        else:
            tags.append("time_win")
            text = f"{who} worked: drifted the right way for {dur:.0f} min ({t['net_pnl_pct']:+.2f}% net) without needing the target."
        if vol is not None and vol >= 1.5 and vw is not None and ((dirn == "long" and vw > 0) or (dirn == "short" and vw < 0)):
            tags.append("volume_vwap_confirm")
            text += (f" It worked when volume was {vol:.1f}x normal and price was {'above' if dirn == 'long' else 'below'} VWAP.")
    elif gross > 0:
        verdict = "cost_casualty"
        tags.append("cost_casualty")
        text = (f"{who} was right but lost: it made {gross:+.2f} USD before costs and paid {cost:.2f} USD in fees/slippage "
                f"(net {net:+.2f}). The move was too small for these costs.")
    else:
        verdict = "loss"
        if reason == "momentum_reversal":
            tags.append("momentum_reversal")
            text = f"{who} failed because momentum reversed within {dur:.0f} min."
        elif reason == "no_follow_through":
            tags.append("no_follow_through")
            text = f"{who} failed: no follow-through (peak only {mfe_r:.2f}R) so it was cut after {dur:.0f} min."
        elif reason in ("stop_loss", "trailing_stop"):
            if mfe_r >= 0.5:
                tags.append("gave_back_winner")
                text = f"{who} failed after being {mfe_r:.1f}R in profit: it reversed into the stop (the trail should have armed sooner)."
            elif dur <= 2:
                tags.append("immediate_adverse")
                text = f"{who} was stopped within {dur:.0f} min: the entry met an immediate adverse move."
            else:
                tags.append("stopped_out")
                text = f"{who} was stopped out after {dur:.0f} min (max adverse {mae_r:.1f}R, best {mfe_r:.2f}R)."
        elif reason == "time_stop":
            tags.append("time_stop_loss")
            text = f"{who} went nowhere for {dur:.0f} min and was closed at a loss (best {mfe_r:.2f}R)."
        else:
            text = f"{who} closed at a loss ({reason})."
        if (t.get("regime") or "").startswith("high_vol"):
            tags.append("high_vol")
            text += " It was in a high-volatility regime."
    na, cm = f.get("news_alignment"), f.get("confirm_move_atr")
    if cm is not None:
        text += f" The next candle had confirmed it ({cm:+.2f} ATR of follow-through)."
        tags.append("confirmed_entry")
    if na == "supports":
        tags.append("news_supported")
        text += " News agreed with the trade" + (f" ({f.get('news_confirmations')} independent source group(s))." if f.get("news_confirmations") else ".")
    elif na == "opposes":
        tags.append("news_opposed")
        text += " News was leaning against it (size was cut)."
    elif na == "unclear":
        tags.append("news_unclear")
    for pat in (f.get("patterns") or []):
        tags.append(f"pattern:{pat}")
    tags += [f"setup:{t.get('setup')}", f"regime:{t.get('regime')}"]
    return {"verdict": verdict, "tags": tags, "lesson": text,
            "details": {"mfe_r": mfe_r, "mae_r": mae_r, "duration_min": dur, "cost_usd": cost, "setup": t.get("setup"), "regime": t.get("regime"),
                        "vol_rel_5m": vol, "dist_vwap_15m": vw}}


# ---------------------------------------------------------------- rolling statistics + blocking (pure)
def group_stats(trades: list[dict], keys: tuple[str, ...]) -> dict:
    """{group key tuple: stats} over closed trades. Expectancy = mean net % per trade AFTER fees and slippage."""
    groups: dict[tuple, list[dict]] = {}
    for t in trades:
        groups.setdefault(tuple(t[k] for k in keys), []).append(t)
    out = {}
    for g, ts in groups.items():
        x = np.array([t["net_pnl_pct"] for t in ts])
        usd = np.array([t["net_pnl_usd"] for t in ts])
        wins, losses = usd[usd > 0].sum(), abs(usd[usd <= 0].sum())
        out[g] = {"n": len(ts), "win_rate": float((usd > 0).mean()), "mean_net_pct": float(x.mean()),
                  "stderr": float(x.std(ddof=1) / np.sqrt(len(x))) if len(x) > 1 else None, "net_usd": float(usd.sum()),
                  "profit_factor": float(wins / losses) if losses else None,
                  "avg_mfe_r": float(np.mean([t.get("mfe_r") or 0 for t in ts])), "avg_mae_r": float(np.mean([t.get("mae_r") or 0 for t in ts])),
                  "stop_rate": float(np.mean([t["exit_reason"] in ("stop_loss", "trailing_stop") for t in ts]))}
    return out


def halves(trades: list[dict]) -> tuple[list[dict], list[dict]]:
    """Chronological split (first half of the trades / second half): a pattern only counts if it holds in BOTH."""
    ts = sorted(trades, key=lambda t: t.get("closed_at") or t.get("exit_ts") or 0)
    mid = len(ts) // 2
    return ts[:mid], ts[mid:]


def stable_sign(trades: list[dict], min_half: int) -> int:
    """+1 / -1 if the mean net % has that sign in BOTH chronological halves (each with >= min_half trades), else 0."""
    a, b = halves(trades)
    if len(a) < min_half or len(b) < min_half:
        return 0
    ma, mb = np.mean([t["net_pnl_pct"] for t in a]), np.mean([t["net_pnl_pct"] for t in b])
    return 1 if ma > 0 and mb > 0 else -1 if ma < 0 and mb < 0 else 0


def blocked_setups(trades: list[dict], cfg: dict) -> list[tuple]:
    """Setups with proven negative expectancy after costs: (symbol, direction(+1/-1), setup|regime) whose mean net % plus
    z stderrs is still below zero with at least `scalp_setup_min_trades` trades AND that is negative in both chronological
    halves (walk-forward stability). Never blocks on a handful of trades or on one bad stretch."""
    out = []
    for dim in ("setup", "regime"):
        groups: dict[tuple, list[dict]] = {}
        for t in trades:
            groups.setdefault((t["symbol"], t["direction"], t[dim]), []).append(t)
        for (sym, dirn, tag), ts in groups.items():
            s = group_stats(ts, ("symbol",))[(sym,)]
            if (s["n"] >= cfg["scalp_setup_min_trades"] and s["stderr"] is not None and s["mean_net_pct"] + cfg["scalp_setup_block_z"] * s["stderr"] < 0
                    and stable_sign(ts, cfg["scalp_setup_min_trades"] // 3) < 0):
                out.append((sym, 1 if dirn == "long" else -1, tag, s["mean_net_pct"], s["n"]))
    return out


# ---------------------------------------------------------------- what actually predicted profit (from entry conditions + exit behaviour)
def condition_tags(t: dict) -> list[str]:
    """Human-readable entry conditions derived from the features stored with the trade, in the trade's own direction."""
    f = t.get("features") or {}
    if isinstance(f, str):
        f = json.loads(f)
    d = 1 if t["direction"] == "long" else -1
    tags = []
    g = lambda k: f.get(k) if f.get(k) is not None else None
    if g("ret_1m") is not None and abs(g("ret_1m")) >= 0.5:
        tags.append("entered after a >0.5% 1-minute move")
    if g("vol_rel_5m") is not None and g("vol_rel_5m") >= 2:
        tags.append("volume spike (5m volume >= 2x normal)")
    if g("dist_vwap_15m") is not None and abs(g("dist_vwap_15m")) >= 0.05:
        tags.append("price " + ("above" if g("dist_vwap_15m") > 0 else "below") + " VWAP" + (" (with the trade)" if d * g("dist_vwap_15m") > 0 else " (against the trade)"))
    if g("btc_ret_5m") is not None and abs(g("btc_ret_5m")) >= 0.1 and t["symbol"] != "BTC":
        tags.append("BTC moving " + ("with" if d * g("btc_ret_5m") > 0 else "against") + " the trade")
    if g("rsi_1m") is not None and (g("rsi_1m") >= 75 or g("rsi_1m") <= 25):
        tags.append("RSI extreme " + ("(overbought)" if g("rsi_1m") >= 75 else "(oversold)"))
    if (t.get("regime") or "").startswith("high_vol"):
        tags.append("high-volatility regime")
    for pat in (f.get("patterns") or []):
        tags.append(f"candle pattern: {pat.replace('_', ' ')}")
    na = f.get("news_alignment")
    if na in ("supports", "opposes", "unclear"):
        tags.append({"supports": "news agreed with the trade", "opposes": "news leaned against the trade", "unclear": "important news with unclear direction"}[na])
    if f.get("news_confirmations") and f.get("news_confirmations") >= 2:
        tags.append("2+ independent news source groups")
    cm = f.get("confirm_move_atr")
    if cm is not None:
        tags.append("strong confirmation candle (>= 1 ATR)" if cm >= 1.0 else "weak confirmation candle (< 0.5 ATR)" if cm < 0.5 else "moderate confirmation candle")
    tags.append(f"setup {t.get('setup')}")
    return tags


def pattern_findings(trades: list[dict], min_n: int = 20) -> list[dict]:
    """Per (symbol, direction, condition) and (direction, condition): win rate and expectancy AFTER costs, kept only if the sign of the
    expectancy is the same in both chronological halves. This is the walk-forward filter: a pattern that only worked in one stretch
    is not reported as a lesson."""
    groups: dict[tuple, list[dict]] = {}
    for t in trades:
        for tag in condition_tags(t):
            groups.setdefault((t["symbol"], t["direction"], tag), []).append(t)
            groups.setdefault(("ALL", t["direction"], tag), []).append(t)
    out = []
    for (sym, dirn, tag), ts in groups.items():
        if len(ts) < min_n:
            continue
        sg = stable_sign(ts, min_n // 2)
        if sg == 0:
            continue
        usd = np.array([t["net_pnl_usd"] for t in ts])
        out.append({"symbol": sym, "direction": dirn, "condition": tag, "n": len(ts), "win_rate": float((usd > 0).mean()),
                    "mean_net_pct": float(np.mean([t["net_pnl_pct"] for t in ts])), "sign": sg})
    return sorted(out, key=lambda r: -abs(r["mean_net_pct"]) * np.sqrt(r["n"]))


def exit_behaviour(trades: list[dict], min_n: int = 15) -> list[str]:
    """What the exit rules did to profitability (stable across halves only)."""
    out = []
    by = group_stats(trades, ("exit_reason",))
    for (reason,), s in by.items():
        if s["n"] < min_n:
            continue
        ts = [t for t in trades if t["exit_reason"] == reason]
        if stable_sign(ts, min_n // 2) == 0:
            continue
        out.append(f"{reason.replace('_', ' ')} exits: {s['n']} trades, {s['win_rate']:.0%} profitable, {s['mean_net_pct']:+.3f}% net per trade after costs, "
                   f"avg best-excursion {s['avg_mfe_r']:.2f}R / worst {s['avg_mae_r']:.2f}R.")
    return out


def insights(trades: list[dict], lessons: list[dict], cfg: dict, signals: list[dict] | None = None) -> list[str]:
    """The lessons in aggregate, as sentences a human can act on. `signals` = graded scalp_signals rows (every prediction, traded or not)."""
    out = signal_insights(signals or [], cfg)
    if not trades:
        return (out + ["No completed trades yet: nothing more to learn from trades."])[:20]
    net = sum(t["net_pnl_usd"] for t in trades)
    gross = sum(t["gross_pnl_usd"] for t in trades)
    cost = sum((t["fees_usd"] or 0) + (t["slippage_usd"] or 0) for t in trades)
    out.append(f"{len(trades)} trades in the window: gross {gross:+.2f} USD, costs {cost:.2f} USD, net {net:+.2f} USD"
               + (f" (costs ate {cost / gross:.0%} of the gross edge)." if gross > 0 else "."))
    for (sym, dirn, setup), s in sorted(group_stats(trades, ("symbol", "direction", "setup")).items(), key=lambda kv: -kv[1]["net_usd"]):
        if s["n"] >= 8 and s["mean_net_pct"] > 0:
            out.append(f"{sym} {dirn} '{setup}' works: {s['n']} trades, {s['win_rate']:.0%} win rate, {s['mean_net_pct']:+.3f}% net per trade after costs.")
    for sym, dirn, tag, mean, n in blocked_setups(trades, cfg):
        out.append(f"{sym} {'long' if dirn > 0 else 'short'} '{tag}' has negative expectancy after fees ({mean:+.3f}% net over {n} trades): blocked until it ages out of the window.")
    for (sym, regime), s in group_stats(trades, ("symbol", "regime")).items():
        if s["n"] >= 10 and s["stop_rate"] >= 0.7 and s["mean_net_pct"] < 0:
            out.append(f"{sym} trades in the {regime} regime keep getting stopped ({s['stop_rate']:.0%} of {s['n']}): they likely need a wider stop or fewer entries.")
    for r in pattern_findings(trades)[:6]:
        who = r["symbol"] if r["symbol"] != "ALL" else "all coins"
        verb = "worked" if r["sign"] > 0 else "lost money"
        out.append(f"{who} {r['direction']}, {r['condition']}: {verb} in both halves of the window ({r['win_rate']:.0%} win rate, {r['mean_net_pct']:+.3f}% net per trade over {r['n']} trades).")
    out += exit_behaviour(trades)[:4]
    tag_n: dict[str, int] = {}
    for l in lessons:
        for tg in l["tags"]:
            if not tg.startswith(("setup:", "regime:")):
                tag_n[tg] = tag_n.get(tg, 0) + 1
    common = sorted(tag_n.items(), key=lambda kv: -kv[1])[:3]
    if common:
        out.append("Most common outcomes: " + ", ".join(f"{k} x{v}" for k, v in common) + ".")
    return out[:24]


# ---------------------------------------------------------------- every prediction is graded (pure)
def grade_signal(sig: dict, candles: pd.DataFrame, cfg: dict) -> dict | None:
    """What the market did AFTER this prediction. `sig` needs decision_ts and direction; `candles` = closed 1m candles (index = open
    time) that include the whole hold window. Returns None until every one of the hold-window candles has closed.
    Convention (the model's own): enter at the open of the candle after the decision, exit at the close `scalp_hold_bars` later."""
    H = int(cfg["scalp_hold_bars"])
    t = pd.Timestamp(sig["decision_ts"])
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    w = candles[(candles.index > t) & (candles.index <= t + pd.Timedelta(minutes=H))]
    if len(w) < H:
        return None
    d = 1 if sig["direction"] == "long" else -1
    entry, exit_ = float(w["open"].iloc[0]), float(w["close"].iloc[-1])
    gross = d * (exit_ / entry - 1) * 100
    mfe = (float(w["high"].max()) / entry - 1) * 100 if d > 0 else (1 - float(w["low"].min()) / entry) * 100
    mae = (float(w["low"].min()) / entry - 1) * 100 if d > 0 else (1 - float(w["high"].max()) / entry) * 100
    net = gross - costs.round_trip_cost_pct(cfg, costs.ASSUMED_SPREAD_PCT.get(sig["symbol"], 0.0))
    outcome = "right" if gross > 1e-9 else "wrong" if gross < -1e-9 else "flat"
    return {"fwd_gross_pct": gross, "fwd_net_pct": net, "fwd_mfe_pct": max(mfe, 0.0), "fwd_mae_pct": min(mae, 0.0), "outcome": outcome, "profitable": bool(net > 0)}


def signal_lesson(sig: dict, g: dict) -> tuple[str, list[str]]:
    """Plain-English reason a prediction was right or wrong, and what the next-candle confirmation / news gate did about it."""
    who = f"{sig['symbol']} {sig['direction']} prediction ({sig['pred_edge_pct']:+.2f}% predicted net edge)"
    status, reason = sig["status"], sig.get("reason") or ""
    mv = f"{g['fwd_gross_pct']:+.2f}% in the {int(round(g.get('hold', 15)))} min that followed"
    tags = [g["outcome"], f"status:{status}"] + ([f"reason:{reason}"] if reason and status in ("failed", "blocked", "expired") else [])
    traded = status == "confirmed"
    if g["outcome"] == "right":
        base = f"{who} was RIGHT: price moved {mv}"
        if g["profitable"]:
            if traded:
                text = base + ", enough to pay after costs. It was confirmed by the next candle and traded."
            else:
                text = base + f", enough to pay after costs, but it did NOT trade ({status}: {reason.replace('_', ' ')}). That gate cost a winner."
                tags.append("gate_missed_winner")
        else:
            text = base + ", but that is smaller than the round-trip costs: right direction, still a loser (cost casualty)."
            tags.append("cost_casualty")
    elif g["outcome"] == "wrong":
        base = f"{who} was WRONG: price moved {mv} (worst point {g['fwd_mae_pct']:+.2f}%)"
        if traded:
            text = base + ". It was confirmed by the next candle anyway, so confirmation did not protect this one."
            tags.append("confirmed_but_wrong")
        elif status == "failed":
            text = base + f". The next candle did not confirm it ({reason.replace('_', ' ')}), so the filter correctly kept us out."
            tags.append("gate_saved_loss")
        elif status == "blocked":
            text = base + f". It was blocked ({reason.replace('_', ' ')}), which kept us out of a loser."
            tags.append("gate_saved_loss")
        else:
            text = base + "."
    else:
        text = f"{who} went nowhere ({mv})."
    feats = sig.get("features") or {}
    if isinstance(feats, str):
        feats = json.loads(feats)
    news = sig.get("news") or {}
    if isinstance(news, str):
        news = json.loads(news)
    bias = news.get("bias")
    if bias in ("bullish", "bearish"):
        d = 1 if sig["direction"] == "long" else -1
        aligned = (bias == "bullish") == (d > 0)
        tags.append("news_supported" if aligned else "news_opposed")
        text += f" News was {'with' if aligned else 'against'} it ({bias}, score {news.get('net', 0):+.0f})" + (
            f" and {news.get('confirmations')} independent source group(s) agreed." if news.get("confirmations", 0) >= 2 else ".")
    elif bias == "conflict":
        tags.append("news_conflict")
        text += " Sources were conflicting."
    for pat in (feats.get("patterns") or []):
        tags.append(f"pattern:{pat}")
    tags += [f"setup:{sig.get('setup')}", f"regime:{sig.get('regime')}"]
    return text, tags


def signals_as_trades(signals: list[dict]) -> list[dict]:
    """Graded signals in the shape the trade-statistics helpers expect (net % of the model's fixed-horizon trade, after costs)."""
    out = []
    for g in signals:
        f = g.get("features") or {}
        if isinstance(f, str):
            f = json.loads(f)
        news = g.get("news") or {}
        if isinstance(news, str):
            news = json.loads(news)
        f = {**f, "news_alignment": ("supports" if (news.get("bias") == "bullish") == (g["direction"] == "long") else "opposes") if news.get("bias") in ("bullish", "bearish") else
             ("unclear" if news.get("bias") == "unclear" else "none"), "news_confirmations": news.get("confirmations")}
        out.append({"symbol": g["symbol"], "direction": g["direction"], "net_pnl_pct": g["fwd_net_pct"], "net_pnl_usd": g["fwd_net_pct"], "gross_pnl_usd": g["fwd_gross_pct"],
                    "features": f, "regime": g.get("regime"), "setup": g.get("setup"), "exit_reason": f"signal_{g['status']}", "closed_at": g["decision_ts"],
                    "mfe_r": None, "mae_r": None, "fees_usd": 0.0, "slippage_usd": 0.0})
    return out


def _rate(xs: list[dict]) -> tuple[float, float]:
    return (sum(1 for x in xs if x["outcome"] == "right") / len(xs), sum(1 for x in xs if x["profitable"]) / len(xs))


def signal_insights(signals: list[dict], cfg: dict) -> list[str]:
    """What the graded predictions say about the model, the next-candle confirmation, the news gate and the patterns. Needs real counts."""
    g = [s for s in signals if s.get("outcome")]
    if not g:
        return []
    out = []
    right, paid = _rate(g)
    out.append(f"{len(g)} graded predictions: {right:.0%} moved the predicted way, {paid:.0%} would have paid after fees and slippage.")
    conf = [s for s in g if s["status"] == "confirmed"]
    fail = [s for s in g if s["status"] == "failed"]
    if len(conf) >= 10 and len(fail) >= 10:
        rc, pc = _rate(conf)
        rf, pf = _rate(fail)
        mc, mf = np.mean([s["fwd_net_pct"] for s in conf]), np.mean([s["fwd_net_pct"] for s in fail])
        verdict = ("Next-candle confirmation is helping" if mc > mf and rc > rf else
                   "Next-candle confirmation is NOT separating winners from losers yet" if abs(mc - mf) < 0.01 else "Next-candle confirmation is rejecting predictions that did better than the ones it let through")
        out.append(f"{verdict}: confirmed predictions were right {rc:.0%} of the time ({mc:+.3f}% net, n={len(conf)}) vs {rf:.0%} for the ones it rejected ({mf:+.3f}% net, n={len(fail)}).")
    for label, st in (("blocked by a gate", "blocked"),):
        sub = [s for s in g if s["status"] == st]
        if len(sub) >= 10:
            r, pd_ = _rate(sub)
            out.append(f"Predictions {label}: right {r:.0%}, paid {pd_:.0%}, mean {np.mean([s['fwd_net_pct'] for s in sub]):+.3f}% net (n={len(sub)}) - " +
                       ("the gates are mostly keeping us out of losers." if pd_ < 0.4 else "the gates are costing winners; review them."))
    news_g = {"supports": [], "opposes": []}
    for s in signals_as_trades(g):
        a = s["features"].get("news_alignment")
        if a in news_g:
            news_g[a].append(s)
    if len(news_g["supports"]) >= 8 and len(news_g["opposes"]) >= 8:
        ms, mo = np.mean([s["net_pnl_pct"] for s in news_g["supports"]]), np.mean([s["net_pnl_pct"] for s in news_g["opposes"]])
        out.append(f"News alignment: predictions with news behind them averaged {ms:+.3f}% net (n={len(news_g['supports'])}) vs {mo:+.3f}% when news leaned against (n={len(news_g['opposes'])}).")
    for r in pattern_findings(signals_as_trades(g), min_n=20)[:4]:
        who = r["symbol"] if r["symbol"] != "ALL" else "all coins"
        out.append(f"Predictions on {who} ({r['direction']}), {r['condition']}: {'worked' if r['sign'] > 0 else 'lost money'} in both halves of the window ({r['win_rate']:.0%} paid, {r['mean_net_pct']:+.3f}% net over {r['n']}).")
    return out


# ---------------------------------------------------------------- database wrappers
def _state_get(db, key, default=None):
    r = db.one("select value from scalp_state where key=%s", [key])
    return r["value"] if r else default


def _state_put(db, key, value):
    db.run("""insert into scalp_state (key, value, updated_at) values (%s, %s::jsonb, now())
              on conflict (key) do update set value = excluded.value, updated_at = now()""", [key, json.dumps(value, default=str)])


def record_lesson(cur_or_db, trade: dict) -> None:
    """Write the post-mortem for a just-closed trade. `trade` must carry its id (as trade_id). Deterministic only -
    called from inside live.py's locked trading transaction, so it must never make a network call (see
    `enrich_lessons_with_llm` for the optional, decoupled OpenRouter commentary pass)."""
    pm = post_mortem(trade)
    sql = """insert into scalp_lessons (trade_id, symbol, verdict, tags, lesson, details) values (%s,%s,%s,%s,%s,%s::jsonb)
             on conflict (trade_id) do nothing"""
    params = [trade["id"], trade["symbol"], pm["verdict"], pm["tags"], pm["lesson"], json.dumps(pm["details"], default=str)]
    (cur_or_db.execute if hasattr(cur_or_db, "execute") else cur_or_db.run)(sql, params)


def enrich_lessons_with_llm(db, cfg: dict, limit: int = 20) -> int:
    """Optional, best-effort: backfill an `llm_note` (one extra sentence of colour, ADDITIVE only - never changes
    `verdict`/`tags`/the deterministic `lesson`) onto recent lessons that don't have one yet. Off unless both
    `llm_enabled` is true and OPENROUTER_API_KEY is set; runs OUTSIDE any trading transaction/advisory lock, since
    it makes network calls. Never raises - a failed or unavailable OpenRouter call just leaves rows unenriched."""
    if not cfg.get("llm_enabled"):
        return 0
    from .. import llm
    if not llm.enabled():
        return 0
    rows = db.all("""select l.id, l.lesson, t.* from scalp_lessons l join scalp_trades t on t.id = l.trade_id
                     where l.details->>'llm_note' is null order by l.id desc limit %s""", [limit])
    n = 0
    for r in rows:
        try:
            note = llm.explain_trade(dict(r), r["lesson"])
        except Exception:
            note = None
        if note:
            db.run("update scalp_lessons set details = details || jsonb_build_object('llm_note', %s::text) where id = %s", [note, r["id"]])
            n += 1
    return n


def closed_trades(db, days: int, now: datetime | None = None) -> list[dict]:
    since = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    return db.all("select * from scalp_trades where status='closed' and closed_at > %s order by closed_at", [since])


def graded_signals(db, days: int, now: datetime | None = None) -> list[dict]:
    since = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    return db.all("select * from scalp_signals where graded_at is not null and decision_ts > %s order by decision_ts", [since])


def grade_signals(db, cfg: dict, now: datetime | None = None, limit: int = 300) -> int:
    """Grade every prediction whose hold window has fully elapsed (traded or not). Bookkeeping only: it records facts about the past and
    changes nothing about future trading except through the aggregate studies in run_stats. Safe to call every pass."""
    now = now or datetime.now(timezone.utc)
    H = int(cfg["scalp_hold_bars"])
    rows = db.all("""select * from scalp_signals where graded_at is null and status <> 'pending' and decision_ts <= %s order by decision_ts limit %s""",
                  [now - timedelta(minutes=H + 2), limit])
    n = 0
    for r in rows:
        c = db.all("select ts, open, high, low, close, volume from candles where symbol=%s and granularity=60 and ts > %s and ts <= %s order by ts",
                   [r["symbol"], r["decision_ts"], r["decision_ts"] + timedelta(minutes=H)])
        if not c:
            if now - r["decision_ts"] > timedelta(hours=6):          # the candles were never stored (a gap): do not retry forever
                db.run("update scalp_signals set graded_at=now(), lesson=%s, tags=%s where id=%s", ["No candles stored for this window, so it could not be graded.", ["ungradable"], r["id"]])
            continue
        df = pd.DataFrame(c).set_index("ts")
        df.index = pd.DatetimeIndex(df.index)
        g = grade_signal(r, data.fill_gaps(df.astype(float)), cfg)
        if g is None:
            continue
        text, tags = signal_lesson(r, {**g, "hold": H})
        db.run("""update scalp_signals set graded_at=now(), fwd_gross_pct=%s, fwd_net_pct=%s, fwd_mfe_pct=%s, fwd_mae_pct=%s, outcome=%s, profitable=%s,
                  lesson=%s, tags=%s where id=%s""",
               [g["fwd_gross_pct"], g["fwd_net_pct"], g["fwd_mfe_pct"], g["fwd_mae_pct"], g["outcome"], g["profitable"], text, tags, r["id"]])
        n += 1
    return n


def run_bookkeeping(db, cfg: dict, now: datetime | None = None) -> dict:
    """Cheap, window-independent upkeep that must run after every scalper pass: grade finished predictions."""
    return {"graded": grade_signals(db, cfg, now)}


def run_stats(db, cfg: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    trades = closed_trades(db, cfg["scalp_setup_window_days"], now)
    sigs = graded_signals(db, cfg["scalp_setup_window_days"], now)
    lessons = db.all("select l.tags from scalp_lessons l join scalp_trades t on t.id = l.trade_id where t.closed_at > %s",
                     [now - timedelta(days=cfg["scalp_setup_window_days"])])
    blocked = blocked_setups(trades, cfg)
    _state_put(db, "blocked_setups", [list(b) for b in blocked])
    _state_put(db, "insights", insights(trades, lessons, cfg, sigs))
    by = {"|".join(map(str, k)): v for k, v in group_stats(trades, ("symbol", "direction", "setup", "regime")).items()}
    _state_put(db, "setup_stats", by)
    return {"trades": len(trades), "signals": len(sigs), "blocked": len(blocked)}


def load_blocked(db) -> frozenset:
    """Set of (symbol, direction, tag) the engine must not trade right now."""
    rows = _state_get(db, "blocked_setups", []) or []
    return frozenset((r[0], int(r[1]), r[2]) for r in rows)


def load_active_model(db) -> dict | None:
    m = db.one("select id, bundle, threshold, meta from scalp_models where is_active")
    if not m:
        return None
    return {"id": m["id"], "bundle": m["bundle"], "thr": float("inf") if m["threshold"] is None else float(m["threshold"]), "meta": m["meta"]}


def save_model(db, cand: dict, meta: dict, activate: bool) -> int:
    if activate:
        db.run("update scalp_models set is_active=false where is_active")
    return db.insert("scalp_models", {"bundle": Json(cand["bundle"]), "threshold": None if not np.isfinite(cand["thr"]) else cand["thr"],
                                      "meta": Json(meta, dumps=lambda o: json.dumps(o, default=str)), "is_active": activate})


def maybe_retrain(db, cfg: dict, now: datetime, force: bool = False, frames: dict | None = None) -> dict:
    """Challenger vs champion on an unseen holdout. Attempted at most every `scalp_retrain_min_hours`."""
    log = _state_get(db, "retrain_log", []) or []
    if log and not force:
        last = datetime.fromisoformat(log[-1]["at"])
        if now - last < timedelta(hours=cfg["scalp_retrain_min_hours"]):
            return {"decision": "not_due"}
    frames = frames or data.load_db_history(db, cfg["scalp_train_days"], now)
    if len(frames) < len(SYMBOLS):
        return {"decision": "waiting", "reason": "candle history for every coin is not stored yet"}
    d = model.build_dataset(frames, cfg)
    entry = {"at": now.isoformat()}
    if len(d) < cfg["scalp_min_train_rows"]:
        entry.update(decision="waiting", reason=f"only {len(d)} training rows (< {cfg['scalp_min_train_rows']})")
    else:
        cut = d.index.max() - pd.Timedelta(days=cfg["scalp_holdout_days"])
        embargo = pd.Timedelta(minutes=int(cfg["scalp_hold_bars"]) + 2)
        cand = model.train_with_threshold(d[d.index < cut - embargo], cfg, val_days=min(4.0, cfg["scalp_holdout_days"]))
        champ = load_active_model(db)
        cmp = model.compare_on_holdout(d[d.index >= cut], champ, cand, cfg)
        meta = {"trained_at": now.isoformat(), "holdout_start": str(cut), "train_rows": cand["train_rows"], "val_rows": cand["val_rows"],
                "train_end": cand["train_end"], "val_start": cand["val_start"], "threshold_table": cand["picked"]["table"],
                "chosen": cand["picked"]["chosen"], "holdout": cmp, "costs": {"fee_pct": cfg["trading_fee_pct"], "slippage_pct": cfg["slippage_pct"]}}
        if cmp["promote"]:
            mid = save_model(db, cand, meta, activate=True)
            entry.update(decision="promoted", model_id=mid, reason=cmp["reason"])
        else:
            entry.update(decision="kept_champion" if champ else "no_edge", reason=cmp["reason"])
        entry["holdout"] = {"challenger": cmp["challenger"], "champion": cmp["champion"], "p": cmp["p"]}
    _state_put(db, "retrain_log", (log + [entry])[-30:])
    return entry


def maybe_learn_exits(db, cfg: dict, now: datetime, force: bool = False, frames: dict | None = None) -> dict:
    """Walk-forward exit-multiplier search on the active model's out-of-sample entries; the result is what live trades use."""
    st = _state_get(db, "exit_policy", {}) or {}
    if st.get("at") and not force and now - datetime.fromisoformat(st["at"]) < timedelta(hours=6):
        return {"decision": "not_due"}
    champ = load_active_model(db)
    if not champ or not np.isfinite(champ["thr"]):
        return {"decision": "waiting", "reason": "no active model with a positive-edge threshold"}
    t0 = pd.Timestamp(champ["meta"].get("holdout_start") or now - timedelta(days=5))
    t1 = pd.Timestamp(now)
    if t1 - t0 < pd.Timedelta(days=2):
        return {"decision": "waiting", "reason": "less than 2 days of out-of-sample history for this model"}
    frames = frames or data.load_db_history(db, cfg["scalp_train_days"], now)
    pred = {}
    for s, df in frames.items():
        f = compute_features(frames, s).dropna(subset=["atr_1m_pct", "vol_ratio", "efficiency_30m"])
        pred[s] = model.predict_frame(champ["bundle"], f)
    policy, info = exit_policy.learn_policy(pred, champ["thr"], cfg, t0, t1, st.get("policy"))
    changed = policy != (st.get("policy") or {})
    _state_put(db, "exit_policy", {"at": now.isoformat(), "policy": policy, "info": info, "window": [str(t0), str(t1)]})
    return {"decision": "updated" if changed else "unchanged", "policy": policy}


def load_policy(db) -> dict:
    return (_state_get(db, "exit_policy", {}) or {}).get("policy") or {}


def run_learning(db, cfg: dict, now: datetime | None = None, force: bool = False) -> dict:
    """The periodic analysis pass. Does nothing outside the local 05:00-23:59 window (unless forced)."""
    now = now or datetime.now(timezone.utc)
    if not force and not in_learning_window(now, cfg):
        return {"active": False, "note": "outside the 05:00-23:59 local learning window"}
    last = _state_get(db, "learn_last", {}) or {}
    if last.get("at") and not force and now - datetime.fromisoformat(last["at"]) < timedelta(minutes=5):
        return {"active": True, "note": "analysed under 5 minutes ago"}
    out = {"active": True}
    out["graded"] = grade_signals(db, cfg, now)
    out["stats"] = run_stats(db, cfg, now)
    out["llm_enriched"] = enrich_lessons_with_llm(db, cfg)
    out["exits"] = maybe_learn_exits(db, cfg, now, force)
    out["retrain"] = maybe_retrain(db, cfg, now, force)
    _state_put(db, "learn_last", {"at": now.isoformat(), "summary": {k: (v if not isinstance(v, dict) else v.get("decision", v)) for k, v in out.items()}})
    return out
