"""Autopilot: lets the AI trade the user's MANUAL paper account while they are away. FAKE MONEY ONLY.

Same rules as the rest of the project: spot, long-only, no leverage, BTC/ETH/XRP, real prices with fees, spread and slippage.
There is deliberately no exchange/broker order code anywhere. `decide` and the sizing helpers are pure (unit tested);
`run` is the thin database wrapper. It follows the SAME current signal the dashboard shows (a live sudden-event signal, else
the standing model read), and acts on each signal at most once, so a signal that stays up for hours cannot repeat itself.

Guard rails (each one is a test): 10% of the portfolio per buy (20% when high-confidence), never more than 40% of the
portfolio in one coin, never more than the cash, a $5 minimum, no selling in the first 2 hours of a position on an ordinary
(non-sudden) signal so fees can't eat it alive, and a switch (`autopilot_enabled`) that turns it off.
"""
import json
from datetime import datetime, timedelta, timezone

from .config import SYMBOLS
from .paper import fill_price, position_budget

MIN_ORDER = 5.0
MAX_COIN_SHARE = 0.40           # same cap as the "what should I do with the cash" planner
MIN_HOLD_H = 2.0                # ordinary (non-sudden) SELL/REDUCE ignored while the position is younger than this
MANUAL_LOCK = 778899            # the same advisory lock the website takes, so the two can never double-spend
SOURCE = "autopilot"


def _cents(x: float) -> float:
    return int(x * 100 + 1e-9) / 100


def decide(coins: dict, cash: float, total: float, cfg: dict, now: datetime) -> list[dict]:
    """coins[sym] = {signal: {id, action, urgency, trigger_kind, bull, created_at} | None, held_value, youngest_lot_at, last_auto_at}.
    Returns orders: {side:'buy', coin, usd, why, signal_id} or {side:'sell', coin, fraction, why, signal_id}. Cash is spent in order."""
    orders, cash_left = [], cash
    for sym in SYMBOLS:
        c = coins.get(sym) or {}
        sig = c.get("signal")
        if not sig:
            continue
        if c.get("last_auto_at") and c["last_auto_at"] >= sig["created_at"]:
            continue                                                       # this signal was already acted on
        sudden = sig["trigger_kind"] != "scheduled"
        held = c.get("held_value") or 0.0
        act = sig["action"]
        if act == "BUY":
            bull = sig.get("bull")
            conf = max(bull, 1 - bull) if bull is not None else 0.5
            budget = min(position_budget(total, cash_left, conf, cfg), MAX_COIN_SHARE * total - held, cash_left)
            budget = _cents(max(0.0, budget))
            if budget < MIN_ORDER:
                continue
            cash_left -= budget
            orders.append({"side": "buy", "coin": sym, "usd": budget, "signal_id": sig["id"],
                           "why": f"{'Sudden-event' if sudden else 'Standing'} BUY signal ({conf:.0%} confidence)"})
        elif act in ("REDUCE", "SELL") and held >= 1.0:
            lot_at = c.get("youngest_lot_at")
            if not sudden and lot_at and now - lot_at < timedelta(hours=MIN_HOLD_H):
                continue                                                   # too fresh: an ordinary signal is not worth a round trip of fees
            f = 1.0 if act == "SELL" else 0.5
            if held * (1 - f) < MIN_ORDER:
                f = 1.0                                                    # don't leave dust behind
            orders.append({"side": "sell", "coin": sym, "fraction": f, "signal_id": sig["id"],
                           "why": f"{'Sudden-event' if sudden else 'Standing'} {act} signal"})
    return orders


# ---------------------------------------------------------------- database wrapper
def _manual_rows(cur):
    cur.execute("select * from paper_trades where account='manual' and status in ('open','closed') order by id")
    return cur.fetchall()


def _cash(cfg, rows) -> float:
    cash = cfg["starting_balance"]
    for t in rows:
        cash -= t["amount_invested"] + t["fee_entry"]
        if t["status"] == "closed":
            cash += t["quantity"] * t["exit_price"] - t["fee_exit"]
    return cash


def _buy(cur, cfg, o, mid, spread, rows, prices, advice) -> dict:
    fee = cfg["trading_fee_pct"] / 100
    cash = _cash(cfg, rows)
    amount = min(o["usd"], cash)
    if amount < MIN_ORDER:
        return {"ok": False, "why": "not enough cash"}
    exec_px = fill_price(mid, "buy", spread, cfg["slippage_pct"], cfg["use_real_spread"])
    notional = amount / (1 + fee)
    qty, fee_entry = notional / exec_px, notional * fee
    open_value = sum(r["quantity"] * prices[r["symbol"]] for r in rows if r["status"] == "open")
    after = cash - amount + open_value + qty * mid
    cur.execute("""insert into paper_trades (prediction_id, symbol, horizon_h, signal, status, opened_at, market_price, exec_price, amount_invested, quantity,
                     fee_entry, slippage_entry_pct, spread_entry_pct, planned_exit_at, balance_after_open, account, ai_advice)
                   values (null,%s,0,'BUY','open',now(),%s,%s,%s,%s,%s,%s,%s,null,%s,'manual',%s)""",
                (o["coin"], mid, exec_px, notional, qty, fee_entry, cfg["slippage_pct"], (spread or 0) if cfg["use_real_spread"] else 0, after, json.dumps(advice)))
    return {"ok": True, "text": f"bought ${amount:.2f} of {o['coin']} at {exec_px:,.2f}"}


def _sell(cur, cfg, o, mid, spread, rows, prices, advice) -> dict:
    fee = cfg["trading_fee_pct"] / 100
    lots = [r for r in rows if r["status"] == "open" and r["symbol"] == o["coin"]]
    if not lots:
        return {"ok": False, "why": "nothing held"}
    f = o["fraction"]
    full = f > 0.9999
    exit_px = fill_price(mid, "sell", spread, cfg["slippage_pct"], cfg["use_real_spread"])
    cash = _cash(cfg, rows)
    parts, proceeds, pnl_total = [], 0.0, 0.0
    for lot in lots:
        part = 1.0 if full else f
        q = lot["quantity"] * part
        cost = (lot["amount_invested"] + lot["fee_entry"]) * part
        gross = q * exit_px
        fee_exit = gross * fee
        pnl = gross - fee_exit - cost
        parts.append((lot, part, fee_exit, pnl, cost))
        proceeds += gross - fee_exit
        pnl_total += pnl
    touched = {lot["id"] for lot in lots}
    others = sum(r["quantity"] * prices[r["symbol"]] for r in rows if r["status"] == "open" and r["id"] not in touched)
    remaining = 0.0 if full else sum(lot["quantity"] * (1 - f) * mid for lot in lots)
    after = cash + proceeds + others + remaining
    for lot, part, fee_exit, pnl, cost in parts:
        if full:
            cur.execute("""update paper_trades set status='closed', closed_at=now(), exit_market_price=%s, exit_price=%s, fee_exit=%s, exit_reason='autopilot_sell',
                           pnl_usd=%s, pnl_pct=%s, balance_after=%s where id=%s""", (mid, exit_px, fee_exit, pnl, pnl / cost * 100, after, lot["id"]))
        else:                                                              # split: a closed slice plus a smaller open lot (same shape as the website)
            cur.execute("""insert into paper_trades (prediction_id, symbol, horizon_h, signal, status, opened_at, market_price, exec_price, amount_invested, quantity, fee_entry,
                             slippage_entry_pct, spread_entry_pct, closed_at, exit_market_price, exit_price, fee_exit, exit_reason, pnl_usd, pnl_pct, balance_after, account, ai_advice)
                           values (null,%s,0,'BUY','closed',%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s,%s,'autopilot_sell',%s,%s,%s,'manual',%s)""",
                        (lot["symbol"], lot["opened_at"], lot["market_price"], lot["exec_price"], lot["amount_invested"] * f, lot["quantity"] * f, lot["fee_entry"] * f,
                         lot["slippage_entry_pct"], lot["spread_entry_pct"], mid, exit_px, fee_exit, pnl, pnl / cost * 100, after,
                         json.dumps(lot["ai_advice"]) if lot["ai_advice"] is not None else None))
            cur.execute("update paper_trades set quantity=%s, amount_invested=%s, fee_entry=%s, balance_after_open=%s where id=%s",
                        (lot["quantity"] * (1 - f), lot["amount_invested"] * (1 - f), lot["fee_entry"] * (1 - f), after, lot["id"]))
    return {"ok": True, "text": f"sold {'all' if full else f'{f:.0%}'} of {o['coin']} at {exit_px:,.2f} ({'profit' if pnl_total >= 0 else 'loss'} ${abs(pnl_total):.2f})"}


def _write_status(cur, payload: dict) -> None:
    cur.execute("""insert into settings (key, value, updated_at) values ('autopilot_status', %s::jsonb, now())
                   on conflict (key) do update set value = excluded.value, updated_at = now()""", (json.dumps(payload),))


def run(db, cfg: dict, snaps: dict) -> dict:
    """One autopilot pass. Called from `tick` and the watch loop. Safe to call as often as you like."""
    import psycopg2.extras
    now = datetime.now(timezone.utc)
    prices = {s: v["price"] for s, v in snaps.items() if v.get("price")}
    conn = db.conn
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("select pg_advisory_xact_lock(%s)", (MANUAL_LOCK,))
            if not cfg.get("autopilot_enabled", True):
                _write_status(cur, {"at": now.isoformat(), "enabled": False, "made": 0, "note": "Autopilot is off."})
                conn.commit()
                return {"enabled": False, "made": 0}
            if len(prices) != len(SYMBOLS):
                _write_status(cur, {"at": now.isoformat(), "enabled": True, "made": 0, "note": "Skipped: a live price was missing."})
                conn.commit()
                return {"enabled": True, "made": 0}
            rows = _manual_rows(cur)
            cash = _cash(cfg, rows)
            held = {s: sum(r["quantity"] * prices[s] for r in rows if r["status"] == "open" and r["symbol"] == s) for s in SYMBOLS}
            total = cash + sum(held.values())
            cur.execute("""select distinct on (symbol) id, symbol, action, urgency, trigger_kind, model_bull_prob, created_at from live_signals
                           where expires_at > now() order by symbol, created_at desc""")
            sigs = {r["symbol"]: {"id": r["id"], "action": r["action"], "urgency": r["urgency"], "trigger_kind": r["trigger_kind"],
                                  "bull": r["model_bull_prob"], "created_at": r["created_at"]} for r in cur.fetchall()}
            coins = {}
            for s in SYMBOLS:
                mine = [r for r in rows if r["symbol"] == s]
                auto = [r["opened_at"] for r in mine if (r["ai_advice"] or {}).get("source") == SOURCE] + \
                       [r["closed_at"] for r in mine if r["exit_reason"] == "autopilot_sell"]
                lots = [r["opened_at"] for r in mine if r["status"] == "open"]
                coins[s] = {"signal": sigs.get(s), "held_value": held[s], "youngest_lot_at": max(lots) if lots else None, "last_auto_at": max(auto) if auto else None}
            done = []
            for o in decide(coins, cash, total, cfg, now):
                snap = snaps[o["coin"]]
                if snap.get("spread_pct") and snap["spread_pct"] > cfg["max_spread_pct_to_trade"]:
                    continue                                               # a wide spread means a bad fill: try again next pass
                advice = {"source": SOURCE, "why": o["why"], "signal_id": o["signal_id"], "predictions": []}
                res = (_buy if o["side"] == "buy" else _sell)(cur, cfg, o, snap["price"], snap.get("spread_pct"), rows, prices, advice)
                if res["ok"]:
                    done.append(f"{o['why']}: {res['text']}")
                    cur.execute("select * from paper_trades where account='manual' and status in ('open','closed') order by id")
                    rows = cur.fetchall()                                  # fresh state for the next coin
            note = "; ".join(done) if done else "Checked. No trade needed."
            _write_status(cur, {"at": now.isoformat(), "enabled": True, "made": len(done), "note": note[:600]})
        conn.commit()
        return {"enabled": True, "made": len(done), "trades": done}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True
