"""THE decision engine. Pure functions (no I/O): the live worker, the backtest and the exit optimiser all call exactly
these, so what was measured is what trades. PAPER ONLY.

Entry:   model predicts the NET edge (after fees/slippage/spread) of a long and of a short entered now; `entry_decision`
         picks the better one and rejects it unless every gate passes (costs, arming, cooldown, exposure, daily loss,
         drawdown, spread, liquidity, news, setup blocklist). No quota: if only 8 setups clear the gates, 8 trades happen.
Confirm: a prediction made at a candle's close is only a SIGNAL. It trades only if the NEXT candle confirms it
         (`confirm_signal`: closed the predicted way, followed through, did not reverse first, did not already run, and the
         model's fresh edge is still there). If confirmation fails there is no trade. Live and the backtest both use it.
Exits:   stop and target are sized from current volatility (unit = ATR% x sqrt(hold bars)) times a learned per-coin
         multiplier. `step_position` walks ONE 1-minute candle at a time using its high/low, so a stop touched between
         two live polls is still honoured. A stop only ever moves in the trade's favour (never wider). Winners are
         protected by a trailing stop once ~0.8R in profit; losers are cut by the stop, a momentum reversal, or no
         follow-through.
Intrabar: if a candle touches both stop and target the STOP is taken (conservative). A gap through the stop fills at the
         open. Reversal / no-follow-through exits fill at the NEXT candle's open (a decision made at a close can't fill at it).
"""
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from . import costs
from .features import regime_of, setup_of
from .labels import MIN_UNIT_PCT

STOP_BOUNDS, TP_BOUNDS, TRAIL_BOUNDS = (0.5, 2.5), (0.6, 4.0), (0.3, 1.5)
REARM_FRACTION = 0.5     # after trading a signal, its edge must fall below this x the threshold before the same direction may re-enter


@dataclass
class Position:
    symbol: str
    direction: int                     # +1 long, -1 short
    entry_ts: pd.Timestamp             # open time of the candle the fill happened on
    entry_mid: float
    entry_fill: float
    qty: float
    notional: float                    # qty * entry_fill
    stop_px: float
    initial_stop_px: float
    tp_px: float
    unit_pct: float
    atr_pct: float
    max_hold: int
    trail_act_r: float
    trail_mult: float
    best_px: float
    setup: str = "model_other"
    regime: str = "normal_chop"
    pred_edge_pct: float = 0.0
    features: dict = field(default_factory=dict)
    bars_held: int = 0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    trail_active: bool = False
    stop_moves: int = 0
    pending_exit: str | None = None
    last_bar_ts: pd.Timestamp | None = None
    spread_pct: float = 0.0
    slip_pct: float | None = None      # slippage per side actually charged at entry (flat setting + size impact); reused at exit

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- policy + planning
def clamp_policy(p: dict) -> dict:
    return {"stop_mult": float(np.clip(p["stop_mult"], *STOP_BOUNDS)), "tp_mult": float(np.clip(p["tp_mult"], *TP_BOUNDS)),
            "trail_mult": float(np.clip(p["trail_mult"], *TRAIL_BOUNDS))}


def policy_for(policy: dict | None, symbol: str, regime: str, cfg: dict) -> dict:
    """Exit multipliers: the learned (symbol, regime) entry if there is one, else the learned symbol entry, else the defaults."""
    base = {"stop_mult": cfg["scalp_stop_mult"], "tp_mult": cfg["scalp_tp_mult"], "trail_mult": cfg["scalp_trail_mult"]}
    for key in (f"{symbol}|{regime}", symbol):
        if policy and key in policy:
            return clamp_policy({**base, **{k: v for k, v in policy[key].items() if k in base}})
    return base


def plan_exits(entry_mid: float, direction: int, atr_pct: float | None, cfg: dict, pol: dict) -> dict | None:
    """Stop / target / trailing distances for a trade entered at `entry_mid`. None when volatility is unknown."""
    if atr_pct is None or not np.isfinite(atr_pct) or atr_pct <= 0:
        return None
    unit = max(atr_pct * np.sqrt(cfg["scalp_hold_bars"]), MIN_UNIT_PCT)
    stop_pct, tp_pct = pol["stop_mult"] * unit, pol["tp_mult"] * unit
    return {"stop_px": entry_mid * (1 - direction * stop_pct / 100), "tp_px": entry_mid * (1 + direction * tp_pct / 100),
            "stop_dist_pct": stop_pct, "tp_dist_pct": tp_pct, "unit_pct": unit, "trail_mult": pol["trail_mult"]}


def cost_gate(plan: dict, cfg: dict, spread_pct: float) -> tuple[bool, str | None]:
    """A trade that is only profitable BEFORE costs is rejected: the target must beat round-trip costs by a margin,
    and hitting the stop (which also pays costs) must not be a wildly worse bet than hitting the target is a good one."""
    cost = costs.round_trip_cost_pct(cfg, spread_pct)
    if plan["tp_dist_pct"] - cost < cfg["scalp_min_net_target_pct"]:
        return False, f"target {plan['tp_dist_pct']:.2f}% does not clear round-trip costs {cost:.2f}%"
    return True, None


def size_position(equity: float, free_cash: float, stop_dist_pct: float, cfg: dict, spread_pct: float = 0.0, *, conf_mult: float = 1.0,
                  liq_usd: float | None = None, exposure_room: float | None = None, unit_pct: float = 0.0) -> float:
    """Notional USD, the smallest of:
      * the per-position % of equity (scaled by `conf_mult`, which news may only raise a little or cut),
      * free cash (no leverage, ever),
      * what keeps a full stop-out, INCLUDING fees, spread, slippage and this order's own market impact, within scalp_risk_pct of equity,
      * scalp_max_participation_pct of the last 15 minutes' dollar volume (the market has to be able to absorb the order),
      * what is left of the total-exposure budget (`exposure_room`)."""
    if not (equity > 0):
        return 0.0
    cap = min(equity * cfg["scalp_position_pct"] / 100 * max(conf_mult, 0.0), free_cash)
    if liq_usd is not None and np.isfinite(liq_usd):
        cap = min(cap, max(liq_usd, 0.0) * cfg["scalp_max_participation_pct"] / 100)
    if exposure_room is not None:
        cap = min(cap, max(exposure_room, 0.0))
    cap = max(cap, 0.0)
    flat = stop_dist_pct + costs.round_trip_cost_pct(cfg, spread_pct)
    by_risk = equity * cfg["scalp_risk_pct"] / 100 / (flat / 100)
    usd = min(cap, by_risk)
    if liq_usd is not None and unit_pct > 0 and usd > 0:                      # the order's own impact is part of what a stop-out costs
        imp = costs.impact_pct(usd, liq_usd, unit_pct, cfg)
        usd = min(usd, equity * cfg["scalp_risk_pct"] / 100 / ((flat + 2 * imp) / 100))
    return max(0.0, usd)


# ---------------------------------------------------------------- entry decision
def new_gate_state() -> dict:
    return {"last_exit_ts": None, "armed": {1: True, -1: True}}


def update_arming(state: dict, edges: dict, thr: float) -> None:
    """Call once per decision bar. A direction that just traded stays disarmed until its predicted edge relaxes, so one
    lingering signal cannot open the same trade again and again."""
    for d in (1, -1):
        if edges[d] < REARM_FRACTION * thr:
            state["armed"][d] = True


def entry_decision(symbol: str, row: dict, edges: dict, mid: float, spread_pct: float, now: pd.Timestamp, state: dict,
                   ctx: dict, cfg: dict, policy: dict | None, model_thr: float, blocked: set | frozenset = frozenset(),
                   skip_arming: bool = False) -> dict:
    """Decide whether to open a trade on `symbol` right now. `edges` = predicted net edge % {+1: long, -1: short}.
    ctx = {open_count, equity, free_cash, day_pnl_pct, has_position} plus optional {open_notional, drawdown_pct, news}, where
    `news` = {+1: effect, -1: effect} from news.effect() (allow / size_mult / reason / alignment per direction).
    `skip_arming` is for a CONFIRMED signal: the arming latch was spent when the signal was raised.
    Returns {'enter': bool, 'reason': str, ...}."""
    thr = max(cfg["scalp_min_edge_pct"], model_thr)
    if ctx["has_position"]:
        return {"enter": False, "reason": "have_position"}
    if ctx["open_count"] >= cfg["scalp_max_positions"]:
        return {"enter": False, "reason": "max_positions"}
    if ctx["day_pnl_pct"] <= -cfg["scalp_daily_loss_limit_pct"]:
        return {"enter": False, "reason": "daily_loss_limit"}
    if ctx.get("drawdown_pct", 0.0) >= cfg["scalp_max_drawdown_pct"]:
        return {"enter": False, "reason": "drawdown_halt"}
    if spread_pct is not None and spread_pct > cfg["scalp_max_spread_pct"]:
        return {"enter": False, "reason": "spread_too_wide"}
    last = state.get("last_exit_ts")
    if last is not None and now - last < pd.Timedelta(minutes=cfg["scalp_cooldown_min"]):
        return {"enter": False, "reason": "cooldown"}
    dirs = [1, -1] if cfg["scalp_allow_shorts"] else [1]
    d = max(dirs, key=lambda k: edges[k])
    edge = edges[d]
    if not np.isfinite(edge) or edge < thr:
        return {"enter": False, "reason": "no_edge"}
    if not skip_arming and not state["armed"][d]:
        return {"enter": False, "reason": "not_rearmed"}
    regime = regime_of(row.get("vol_ratio"), row.get("efficiency_30m"))
    setup = setup_of(row, d)
    if (symbol, d, setup) in blocked or (symbol, d, regime) in blocked or (symbol, d, setup, regime) in blocked:
        return {"enter": False, "reason": "setup_blocked", "setup": setup, "regime": regime}
    news = (ctx.get("news") or {}).get(d)
    if news is not None and not news["allow"]:
        return {"enter": False, "reason": news["reason"], "setup": setup, "regime": regime, "news": news}
    liq = None
    if "dollar_vol_15m" in row:                                                   # liquidity gate (whenever the feed supplies volume)
        liq = row["dollar_vol_15m"]
        if liq is None or not np.isfinite(liq):
            return {"enter": False, "reason": "liquidity_unknown", "setup": setup, "regime": regime}
        if liq < cfg["scalp_min_liquidity_usd"]:
            return {"enter": False, "reason": "illiquid", "setup": setup, "regime": regime, "detail": f"{liq:,.0f} USD traded in 15 min"}
    pol = policy_for(policy, symbol, regime, cfg)
    plan = plan_exits(mid, d, row.get("atr_1m_pct"), cfg, pol)
    if plan is None:
        return {"enter": False, "reason": "no_volatility_estimate"}
    ok, why = cost_gate(plan, cfg, spread_pct or 0.0)
    if not ok:
        return {"enter": False, "reason": "cost_gate", "detail": why}
    room = ctx["equity"] * cfg["scalp_max_exposure_pct"] / 100 - ctx.get("open_notional", 0.0)
    if room < 5.0:
        return {"enter": False, "reason": "exposure_limit"}
    mult = news["size_mult"] if news is not None else 1.0
    usd = size_position(ctx["equity"], ctx["free_cash"], plan["stop_dist_pct"], cfg, spread_pct or 0.0, conf_mult=mult, liq_usd=liq,
                        exposure_room=room, unit_pct=plan["unit_pct"])
    if usd < 5.0:
        return {"enter": False, "reason": "size_too_small"}
    return {"enter": True, "reason": "ok", "direction": d, "edge": float(edge), "plan": plan, "usd": usd, "setup": setup, "regime": regime,
            "impact_pct": costs.impact_pct(usd, liq, plan["unit_pct"], cfg) if liq is not None else 0.0, "news": news,
            "liquidity_usd": None if liq is None else float(liq)}


# ---------------------------------------------------------------- next-candle confirmation
def confirm_signal(direction: int, ref_price: float, atr_pct: float | None, bar: dict, edge_now: float, thr: float, cfg: dict) -> dict:
    """Did the candle AFTER the prediction agree with it? `ref_price` = close of the candle the prediction was made from,
    `bar` = the next candle {open, high, low, close} (fully closed), `edge_now` = the model's predicted net edge in the SAME direction
    computed from that confirmation candle, `thr` = the entry threshold. Moves are measured in 1-minute ATRs so BTC and XRP are comparable.
    Returns {'ok': bool, 'reason': str, 'move_atr': float, 'adverse_atr': float}. The first failing test names the reason."""
    d = direction
    if atr_pct is None or not np.isfinite(atr_pct) or atr_pct <= 0 or not np.isfinite(ref_price) or ref_price <= 0:
        return {"ok": False, "reason": "no_volatility_estimate", "move_atr": float("nan"), "adverse_atr": float("nan")}
    move = d * (bar["close"] / ref_price - 1) * 100
    against = d * ((bar["low"] if d > 0 else bar["high"]) / ref_price - 1) * 100
    move_atr, adverse_atr = move / atr_pct, max(-against, 0.0) / atr_pct
    out = {"move_atr": float(move_atr), "adverse_atr": float(adverse_atr)}
    if d * (bar["close"] - bar["open"]) <= 0:
        return {"ok": False, "reason": "candle_against", **out}
    if move_atr < cfg["scalp_confirm_min_move_atr"]:
        return {"ok": False, "reason": "no_follow_through", **out}
    if adverse_atr > cfg["scalp_confirm_max_adverse_atr"]:
        return {"ok": False, "reason": "reversal", **out}
    if move_atr > cfg["scalp_confirm_max_chase_atr"]:
        return {"ok": False, "reason": "chased", **out}
    if not np.isfinite(edge_now) or edge_now < cfg["scalp_confirm_edge_keep"] * thr:
        return {"ok": False, "reason": "edge_gone", **out}
    return {"ok": True, "reason": "confirmed", **out}


# ---------------------------------------------------------------- opening / managing / closing
def open_position(symbol: str, decision: dict, entry_mid: float, entry_ts: pd.Timestamp, row: dict, spread_pct: float,
                  cfg: dict) -> Position:
    d, plan = decision["direction"], decision["plan"]
    slip = cfg["slippage_pct"] + float(decision.get("impact_pct") or 0.0)         # flat setting + this order's own market impact
    ef = costs.fill(entry_mid, d, True, spread_pct, slip, cfg.get("use_real_spread", True))
    # re-anchor the stop/target on the price we were actually handed so their distances are exactly as planned
    stop = entry_mid * (1 - d * plan["stop_dist_pct"] / 100)
    tp = entry_mid * (1 + d * plan["tp_dist_pct"] / 100)
    qty = decision["usd"] / ef
    return Position(symbol=symbol, direction=d, entry_ts=entry_ts, entry_mid=entry_mid, entry_fill=ef, qty=qty, notional=qty * ef,
                    stop_px=stop, initial_stop_px=stop, tp_px=tp, unit_pct=plan["unit_pct"], atr_pct=float(row["atr_1m_pct"]),
                    max_hold=int(cfg["scalp_hold_bars"]), trail_act_r=cfg["scalp_trail_act_r"], trail_mult=plan["trail_mult"],
                    best_px=entry_mid, setup=decision["setup"], regime=decision["regime"], pred_edge_pct=decision["edge"],
                    features={k: (None if v is None or not np.isfinite(v) else float(v)) for k, v in row.items()
                              if k in _ENTRY_FEATURES}, spread_pct=spread_pct or 0.0, last_bar_ts=entry_ts - pd.Timedelta(minutes=1), slip_pct=slip)


_ENTRY_FEATURES = {"ret_1m", "ret_3m", "ret_5m", "ret_15m", "ret_30m", "ret_60m", "atr_1m_pct", "sigma_30m", "vol_ratio", "vol_rel_1m",
                   "vol_rel_5m", "rsi_1m", "rsi_5m", "macd_hist_5m", "trend_15m", "dist_vwap_15m", "dist_vwap_60m", "dist_high_30m",
                   "dist_low_30m", "range_pos_60m", "efficiency_30m", "btc_ret_5m", "rel_ret_5m", "close_loc", "up_bar_share_10m"}


def tighten_stop(pos: Position, candidate: float) -> bool:
    """Move the stop toward price only. Never widens: 'never increase risk after entering'."""
    better = candidate > pos.stop_px if pos.direction > 0 else candidate < pos.stop_px
    if better:
        pos.stop_px = candidate
        pos.stop_moves += 1
    return better


def _exit(reason: str, mid: float) -> dict:
    return {"reason": reason, "mid": float(mid)}


def step_position(pos: Position, bar: dict, recent_closes: pd.Series | None, cfg: dict) -> dict | None:
    """Advance `pos` by ONE closed 1m candle {ts, open, high, low, close}. Mutates `pos`. Returns {'reason','mid'} if the
    trade exits on this candle (mid = the pre-slippage reference price), else None. `recent_closes` = closes up to and
    including this bar (used for the momentum-reversal check)."""
    d, o, h, l, c = pos.direction, bar["open"], bar["high"], bar["low"], bar["close"]
    pos.last_bar_ts = bar["ts"]
    if pos.pending_exit:                                                   # decided at last close: fills at this open
        return _exit(pos.pending_exit, o)
    stop_hit = l <= pos.stop_px if d > 0 else h >= pos.stop_px
    tp_hit = h >= pos.tp_px if d > 0 else l <= pos.tp_px
    if stop_hit:                                                           # stop beats target when one candle touches both
        gap = min(pos.stop_px, o) if d > 0 else max(pos.stop_px, o)
        return _exit("trailing_stop" if pos.trail_active else "stop_loss", gap)
    if tp_hit:
        return _exit("take_profit", pos.tp_px)
    pos.bars_held += 1
    fav, adv = (h, l) if d > 0 else (l, h)
    pos.best_px = max(pos.best_px, fav) if d > 0 else min(pos.best_px, fav)
    pos.mfe_pct = max(pos.mfe_pct, d * (fav / pos.entry_mid - 1) * 100)
    pos.mae_pct = min(pos.mae_pct, d * (adv / pos.entry_mid - 1) * 100)
    # trailing stop: arms at trail_act_r x the ORIGINAL risk in profit; then trails trail_mult x unit behind the best price
    init_risk = abs(pos.entry_mid - pos.initial_stop_px)
    if init_risk > 0 and d * (pos.best_px - pos.entry_mid) >= pos.trail_act_r * init_risk:
        pos.trail_active = True
        tighten_stop(pos, pos.best_px * (1 - d * pos.trail_mult * pos.unit_pct / 100))
    if pos.bars_held >= pos.max_hold:
        return _exit("time_stop", c)
    mark_pct = d * (c / pos.entry_mid - 1) * 100
    n = int(cfg["scalp_reversal_bars"])
    if recent_closes is not None and len(recent_closes) > n and not pos.trail_active:
        move = (recent_closes.iloc[-1] / recent_closes.iloc[-1 - n] - 1) * 100 * d
        if move <= -cfg["scalp_reversal_mult"] * pos.atr_pct * np.sqrt(n) and mark_pct < 0:
            pos.pending_exit = "momentum_reversal"
            return None
    stop_dist_pct = abs(pos.entry_mid - pos.initial_stop_px) / pos.entry_mid * 100
    if (pos.bars_held >= cfg["scalp_nofollow_bars"] and pos.mfe_pct < cfg["scalp_nofollow_mfe_r"] * stop_dist_pct and mark_pct < 0):
        pos.pending_exit = "no_follow_through"
    return None


def close_position(pos: Position, exit_reason: str, exit_mid: float, exit_ts: pd.Timestamp, spread_pct: float, cfg: dict) -> dict:
    """Book the trade: every field the learning loop records for a completed trade."""
    d = pos.direction
    xf = costs.fill(exit_mid, d, False, spread_pct, cfg["slippage_pct"] if pos.slip_pct is None else pos.slip_pct, cfg.get("use_real_spread", True))
    net = costs.net_pct(pos.entry_fill, xf, d, cfg["trading_fee_pct"])
    net_usd = pos.qty * pos.entry_fill * net / 100
    gross_usd = d * (exit_mid - pos.entry_mid) * pos.qty
    fees = cfg["trading_fee_pct"] / 100 * pos.qty * (pos.entry_fill + xf)
    slip = gross_usd - d * (xf - pos.entry_fill) * pos.qty
    mfe = max(pos.mfe_pct, d * (exit_mid / pos.entry_mid - 1) * 100)
    mae = min(pos.mae_pct, d * (exit_mid / pos.entry_mid - 1) * 100)
    stop_dist_pct = abs(pos.entry_mid - pos.initial_stop_px) / pos.entry_mid * 100
    return {
        "symbol": pos.symbol, "direction": "long" if d > 0 else "short", "entry_ts": pos.entry_ts, "exit_ts": exit_ts,
        "duration_min": max((exit_ts - pos.entry_ts).total_seconds() / 60, 0.0), "entry_mid": pos.entry_mid, "entry_price": pos.entry_fill,
        "exit_mid": exit_mid, "exit_price": xf, "qty": pos.qty, "notional": pos.notional,
        "initial_stop_px": pos.initial_stop_px, "final_stop_px": pos.stop_px, "take_profit_px": pos.tp_px,
        "stop_dist_pct": stop_dist_pct, "tp_dist_pct": abs(pos.tp_px / pos.entry_mid - 1) * 100,
        "trail_active": pos.trail_active, "stop_moves": pos.stop_moves, "mfe_pct": mfe, "mae_pct": mae,
        "mfe_r": mfe / stop_dist_pct if stop_dist_pct else None, "mae_r": mae / stop_dist_pct if stop_dist_pct else None,
        "fees_usd": fees, "slippage_usd": slip, "gross_pnl_usd": gross_usd, "net_pnl_usd": net_usd, "net_pnl_pct": net,
        "exit_reason": exit_reason, "profitable": bool(net_usd > 0), "setup": pos.setup, "regime": pos.regime,
        "pred_edge_pct": pos.pred_edge_pct, "features": pos.features, "spread_pct": pos.spread_pct,
    }


def mark_to_market_pct(pos: Position, price: float) -> float:
    return pos.direction * (price / pos.entry_mid - 1) * 100
