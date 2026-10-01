"""XRP Ledger data. Free, no key (verified live 2026-09). Closes the on-chain coverage gap for XRP - the other
on-chain adapters (blockchain.info, Etherscan) only cover BTC and ETH.

Two independent free sources:
  * a public rippled node (s1.ripple.com), the same JSON-RPC API any XRPL validator/server exposes - `ledger` for
    per-ledger transaction counts and close time, `fee` for live network congestion (queue size, load level).
  * XRPScan's public REST API, for richer account-level lookups (not used for a specific account here since this
    project has no wallets of its own - left as a documented capability, see `fetch_account` below, for research
    that wants to track a specific known address, e.g. Ripple's escrow account for monthly unlock monitoring).
"""
from datetime import datetime

import pandas as pd

from .base import Adapter, http_get, http_post_json

RIPPLED = "https://s1.ripple.com:51234"
XRPSCAN_BASE = "https://api.xrpscan.com/api/v1"


def fetch_ledger_stats(now: datetime, symbols=("XRP",)) -> pd.DataFrame:
    j = http_post_json(RIPPLED, {"method": "ledger", "params": [{"ledger_index": "validated", "transactions": True, "expand": False}]})
    r = j.get("result", {})
    ledger = r.get("ledger", {})
    if not ledger:
        return pd.DataFrame()
    return pd.DataFrame([{"symbol": "XRP", "ledger_index": r.get("ledger_index"), "ts": pd.to_datetime(ledger.get("close_time_iso")),
                         "tx_count": len(ledger.get("transactions", [])), "collected_at": now}])


def fetch_fee_stats(now: datetime, symbols=("XRP",)) -> pd.DataFrame:
    j = http_post_json(RIPPLED, {"method": "fee", "params": [{}]})
    r = j.get("result", {})
    drops = r.get("drops", {})
    if not r:
        return pd.DataFrame()
    return pd.DataFrame([{"symbol": "XRP", "ts": now, "base_fee_drops": int(drops.get("base_fee", 0)), "median_fee_drops": int(drops.get("median_fee", 0)),
                         "open_ledger_fee_drops": int(drops.get("open_ledger_fee", 0)), "queue_size": int(r.get("current_queue_size", 0)),
                         "expected_ledger_size": int(r.get("expected_ledger_size", 0)), "collected_at": now}])


def fetch_account(now: datetime, address: str) -> pd.DataFrame:
    """Look up one known XRPL account (e.g. a labelled exchange or escrow wallet) via XRPScan - not called by
    `ADAPTERS` by default (no address is hardcoded here), but available for research that wants to track a
    specific address's balance over time."""
    j = http_get(f"{XRPSCAN_BASE}/account/{address}")
    if not j:
        return pd.DataFrame()
    return pd.DataFrame([{"symbol": "XRP", "address": address, "ts": now, "xrp_balance": float(j.get("xrpBalance", 0)),
                         "owner_count": j.get("ownerCount"), "sequence": j.get("sequence"), "collected_at": now}])


ADAPTERS = [
    Adapter("xrpl_ledger_stats", "onchain_metrics", "Public rippled node (JSON-RPC)", ["XRP"], fetch_ledger_stats, poll_every_s=60,
           notes="Per-validated-ledger transaction count and close time. XRP ledgers close roughly every 3-5 seconds."),
    Adapter("xrpl_fee_stats", "onchain_metrics", "Public rippled node (JSON-RPC)", ["XRP"], fetch_fee_stats, poll_every_s=60,
           notes="Live network congestion (fee levels, queue size) - XRPL's fee schedule is otherwise nearly fixed, so this is the useful signal."),
]
