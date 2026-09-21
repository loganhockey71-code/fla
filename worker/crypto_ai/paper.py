"""Paper-trading engine. FAKE MONEY ONLY: spot, long-only, no leverage, BTC/ETH/XRP only.

The pure functions (fills, sizing, cash accounting) have no I/O so they can be unit tested.
There is deliberately no exchange/broker order code anywhere in this project.
"""
from datetime import datetime, timedelta, timezone

from .config import SYMBOLS


def fill_price(mid: float, side: str, spread_pct: float | None, slippage_pct: float, use_spread: bool) -> float:
    """Simulated execution: cross half the real spread, then add slippage against us."""
    half = (spread_pct or 0.0) / 2 / 100 if use_spread else 0.0
    slip = slippage_pct / 100
    return mid * (1 + half + slip) if side == "buy" else mid * (1 - half - slip)


def position_budget(total_value: float, cash: float, confidence: float, cfg: dict) -> float:
    """10% of portfolio for a normal signal, 20% for a high-confidence one; never more than cash (no leverage)."""
    pct = cfg["high_conf_position_pct"] if confidence * 100 >= cfg["high_confidence_threshold"] else cfg["normal_position_pct"]
    pct = min(pct, cfg["high_conf_position_pct"])
    return max(0.0, min(total_value * pct / 100, cash))


def plan_buy(mid: float, spread_pct: float | None, budget: float, cfg: dict) -> dict | None:
    if budget <= 1.0:
        return None
    fee_rate = cfg["trading_fee_pct"] / 100
    exec_price = fill_price(mid, "buy", spread_pct, cfg["slippage_pct"], cfg["use_real_spread"])
    notional = budget / (1 + fee_rate)          # budget covers notional + fee
    return {
        "market_price": mid, "exec_price": exec_price, "amount_invested": notional,
        "quantity": notional / exec_price, "fee_entry": notional * fee_rate,
        "slippage_entry_pct": cfg["slippage_pct"], "spread_entry_pct": spread_pct or 0.0,
    }


def plan_sell(trade: dict, mid: float, spread_pct: float | None, cfg: dict) -> dict:
    fee_rate = cfg["trading_fee_pct"] / 100
    exit_price = fill_price(mid, "sell", spread_pct, cfg["slippage_pct"], cfg["use_real_spread"])
    gross = trade["quantity"] * exit_price
    fee_exit = gross * fee_rate
    cost = trade["amount_invested"] + trade["fee_entry"]
    pnl = gross - fee_exit - cost
    return {"exit_market_price": mid, "exit_price": exit_price, "fee_exit": fee_exit,
            "pnl_usd": pnl, "pnl_pct": pnl / cost * 100}


def cash_balance(starting: float, trades: list[dict]) -> float:
    cash = starting
    for t in trades:
        if t["status"] in ("open", "closed"):
            cash -= t["amount_invested"] + t["fee_entry"]
        if t["status"] == "closed":
            cash += t["quantity"] * t["exit_price"] - t["fee_exit"]
    return cash


def positions_value(trades: list[dict], prices: dict[str, float]) -> float:
    """Open positions marked at the current mid (before exit costs)."""
    return sum(t["quantity"] * prices[t["symbol"]] for t in trades if t["status"] == "open" and t["symbol"] in prices)


# ---------------------------------------------------------------- database-facing job code
def portfolio_state(db, cfg: dict, prices: dict[str, float]) -> dict:
    trades = db.all("select * from paper_trades where status in ('open','closed') and account='ai'")
    cash = cash_balance(cfg["starting_balance"], trades)
    pos = positions_value(trades, prices)
    return {"cash": cash, "positions_value": pos, "total_value": cash + pos, "trades": trades}


def snapshot_portfolio(db, cfg, prices, note=None) -> dict:
    st = portfolio_state(db, cfg, prices)
    db.insert("portfolio", {"ts": datetime.now(timezone.utc), "cash": st["cash"],
                            "positions_value": st["positions_value"], "total_value": st["total_value"],
                            "note": note})
    return st


def _skip(db, pred, reason, now):
    db.insert("paper_trades", {"prediction_id": pred["id"], "symbol": pred["symbol"], "horizon_h": pred["horizon_h"],
                               "signal": pred["signal"], "status": "skipped", "skip_reason": reason, "opened_at": now})


def act_on_prediction(db, pred: dict, snap: dict, prices: dict[str, float], cfg: dict) -> None:
    """Simulate acting on one freshly-logged BUY/SELL signal at the real current price."""
    if pred["signal"] == "HOLD" or pred["symbol"] not in SYMBOLS:
        return
    now = datetime.now(timezone.utc)
    sym, h = pred["symbol"], pred["horizon_h"]
    st = portfolio_state(db, cfg, prices)
    if pred["signal"] == "BUY":
        if h not in cfg["trade_horizons"]:
            return
        if any(t["status"] == "open" and t["symbol"] == sym and t["horizon_h"] == h for t in st["trades"]):
            return _skip(db, pred, f"a {h}h {sym} position is already open", now)
        if snap.get("spread_pct") and snap["spread_pct"] > cfg["max_spread_pct_to_trade"]:
            return _skip(db, pred, f"spread {snap['spread_pct']:.2f}% too wide", now)
        plan = plan_buy(snap["price"], snap.get("spread_pct"),
                        position_budget(st["total_value"], st["cash"], pred["confidence"], cfg), cfg)
        if plan is None:
            return _skip(db, pred, "not enough cash (no leverage allowed)", now)
        after = st["total_value"] - plan["fee_entry"]
        db.insert("paper_trades", {"prediction_id": pred["id"], "symbol": sym, "horizon_h": h, "signal": "BUY",
                                   "status": "open", "opened_at": now, "planned_exit_at": now + timedelta(hours=h),
                                   "balance_after_open": after, **plan})
    else:  # SELL: spot only, so it can only close longs that already exist
        open_longs = [t for t in st["trades"] if t["status"] == "open" and t["symbol"] == sym]
        if not open_longs:
            return _skip(db, pred, "SELL signal but no open long (spot only, no shorting)", now)
        for t in open_longs:
            close_trade(db, t, snap["price"], snap.get("spread_pct"), cfg, prices, "sell_signal", now)


def close_trade(db, trade: dict, mid: float, spread_pct, cfg: dict, prices: dict, reason: str, when: datetime) -> None:
    res = plan_sell(trade, mid, spread_pct, cfg)
    st = portfolio_state(db, cfg, prices)
    # balance after = cash + this trade's proceeds + remaining open positions (excluding this one)
    proceeds = trade["quantity"] * res["exit_price"] - res["fee_exit"]
    others = sum(t["quantity"] * prices.get(t["symbol"], 0) for t in st["trades"]
                 if t["status"] == "open" and t["id"] != trade["id"])
    db.update("paper_trades", trade["id"], {
        "status": "closed", "closed_at": when, "exit_reason": reason,
        "balance_after": st["cash"] + proceeds + others, **res})


def close_due_positions(db, cfg: dict, prices: dict, spreads: dict, price_lookup) -> int:
    """Close positions whose horizon has ended, at the real price AT the planned exit time."""
    now = datetime.now(timezone.utc)
    n = 0
    for t in db.all("select * from paper_trades where status='open' and account='ai' and planned_exit_at <= %s", [now]):
        mid = price_lookup(t["symbol"], t["planned_exit_at"]) or prices.get(t["symbol"])
        if mid is None:
            continue
        close_trade(db, t, mid, spreads.get(t["symbol"]), cfg, prices, "horizon", t["planned_exit_at"])
        n += 1
    return n
