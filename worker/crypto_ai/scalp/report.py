"""Plain-text report for a walk-forward backtest (everything the task asked to see, out-of-sample only)."""


def _f(x, spec="{:.2f}", none="n/a"):
    return none if x is None else spec.format(x)


def _table(title: str, rows: dict) -> list[str]:
    out = [f"\n{title}", f"  {'':22s} {'trades':>6s} {'win%':>6s} {'net $':>9s} {'PF':>6s} {'avg net%':>9s} {'MFE%':>7s} {'MAE%':>7s}"]
    for k, v in rows.items():
        out.append(f"  {str(k):22s} {v['trades']:6d} {v['win_rate'] * 100:6.1f} {v['net_pnl_usd']:9.2f} {_f(v['profit_factor']):>6s} "
                   f"{v['avg_net_pct']:9.3f} {v['avg_mfe_pct']:7.3f} {v['avg_mae_pct']:7.3f}")
    if not rows:
        out.append("  (no trades)")
    return out


def render(res: dict, cfg: dict) -> str:
    s = res["summary"]
    lines = [
        "=" * 78,
        "SCALPER WALK-FORWARD BACKTEST - out-of-sample test windows only (nothing here was used to fit or choose anything)",
        f"costs per side: fee {cfg['trading_fee_pct']}% + slippage {cfg['slippage_pct']}% (+ assumed spread)  => round trip ~{2 * (cfg['trading_fee_pct'] + cfg['slippage_pct']):.2f}%",
        f"start equity ${res['start_equity']:,.2f}  ->  end equity ${res['end_equity']:,.2f}   test days: {res['oos_days']:.1f}",
        "=" * 78,
        f"total trades          {s['trades']}",
        f"trades / day          {s['trades_per_day']:.2f}",
        f"win rate              {_f(None if s['win_rate'] is None else s['win_rate'] * 100, '{:.1f}%')}",
        f"net P&L               ${s['net_pnl_usd']:+,.2f}  ({s['net_return_pct']:+.2f}% of starting equity)",
        f"profit factor         {_f(s['profit_factor'])}",
        f"max drawdown          {s['max_drawdown_pct']:.2f}%",
        f"average win           {_f(s['avg_win_usd'], '${:+.2f}')}  ({_f(s['avg_win_pct'], '{:+.3f}%')})",
        f"average loss          {_f(s['avg_loss_usd'], '${:+.2f}')}  ({_f(s['avg_loss_pct'], '{:+.3f}%')})",
        f"average duration      {_f(s['avg_duration_min'], '{:.1f} min')}",
        f"fees                  ${s['fees_usd']:.2f}",
        f"slippage+spread       ${s['slippage_usd']:.2f}",
        f"gross P&L (mid-mid)   ${s['gross_pnl_usd']:+,.2f}   (net = gross - fees - slippage)",
        f"avg MFE / MAE         {_f(s['avg_mfe_pct'], '{:+.3f}%')} / {_f(s['avg_mae_pct'], '{:+.3f}%')}   "
        f"({_f(s['avg_mfe_r'], '{:.2f}R')} / {_f(s['avg_mae_r'], '{:.2f}R')})",
        f"cost casualties       {s['cost_casualties']} trades were profitable before costs and lost after them",
    ]
    lines += _table("By coin", res["by_symbol"])
    lines += _table("By market regime", res["by_regime"])
    lines += _table("By direction", res["by_direction"])
    lines += _table("By setup", res["by_setup"])
    lines += _table("By exit reason", res["by_exit"])
    lines.append("\nEntry gate rejections (signals that cleared the model threshold but were stopped by a gate): " + (str(res["rejects"]) or "none"))
    lines.append("\nWalk-forward folds")
    for f in res["folds"]:
        pick = f["val_pick"]
        lines.append(f"  test {f['test_window'][0][:10]}..{f['test_window'][1][:10]}  threshold={_f(f['thr'], '{:.2f}%', 'none')}  "
                     f"val pick={'-' if not pick else f'n={pick['n']} mean net {pick['mean_net_pct']:+.3f}%'}  test trades={f['test_trades']}")
    return "\n".join(lines)


def to_jsonable(res: dict) -> dict:
    return {k: v for k, v in res.items() if k not in ("equity_curve",)} | {"equity_curve": [(str(t), v) for t, v in res["equity_curve"][::30]]}
