"""Data-source adapter framework: the shared Adapter.run() contract, the Parquet data lake (write/dedupe/read), and
each concrete adapter's parsing. All HTTP is mocked - no live network calls in this test file (the endpoints were
verified live by hand; see DATA_SOURCES.md)."""
import shutil
import tempfile
from datetime import datetime, timezone

import pandas as pd
import pytest

from crypto_ai.datasources import base, datalake, defillama, deribit, okx, onchain, registry

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def lake(tmp_path):
    return str(tmp_path / "lake")


# ---------------------------------------------------------------- Adapter contract
def test_adapter_run_writes_to_the_lake_and_reports_rows(lake):
    a = base.Adapter("test_source", "test_category", "Test", ["BTC"], lambda now: pd.DataFrame({"ts": [now], "x": [1]}))
    r = a.run(NOW, lake_root=lake)
    assert r["ok"] and r["rows"] == 1
    back = datalake.read("test_category", "test_source", NOW, NOW, root=lake)
    assert len(back) == 1 and back["x"].iloc[0] == 1


def test_adapter_run_never_raises_on_a_broken_fetch(lake):
    def boom(now):
        raise ConnectionError("down")
    a = base.Adapter("broken", "cat", "Test", ["BTC"], boom)
    r = a.run(NOW, lake_root=lake)
    assert r["ok"] is False and "down" in r["error"]


def test_adapter_run_handles_an_empty_frame(lake):
    a = base.Adapter("empty", "cat", "Test", ["BTC"], lambda now: pd.DataFrame())
    r = a.run(NOW, lake_root=lake)
    assert r["ok"] and r["rows"] == 0


def test_due_respects_poll_interval():
    a = base.Adapter("x", "cat", "Test", ["BTC"], lambda now: pd.DataFrame(), poll_every_s=100)
    assert a.due(NOW)
    a.last_run["at"] = NOW
    assert not a.due(NOW.replace(minute=1))
    from datetime import timedelta
    assert a.due(NOW + timedelta(seconds=200))


# ---------------------------------------------------------------- data lake: dedupe + partitioning
def test_write_dedupes_on_rerun_and_appends_new_rows(lake):
    df1 = pd.DataFrame({"ts": [NOW], "symbol": ["BTC"], "value": [1.0]})
    datalake.write(df1, "cat", "src", NOW, root=lake)
    datalake.write(df1, "cat", "src", NOW, root=lake)   # identical re-run: must not duplicate
    df2 = pd.DataFrame({"ts": [NOW], "symbol": ["ETH"], "value": [2.0]})
    datalake.write(df2, "cat", "src", NOW, root=lake)
    back = datalake.read("cat", "src", NOW, NOW, root=lake)
    assert len(back) == 2 and set(back["symbol"]) == {"BTC", "ETH"}


def test_read_spans_multiple_day_partitions(lake):
    from datetime import timedelta
    d1, d2 = NOW, NOW + timedelta(days=2)
    datalake.write(pd.DataFrame({"ts": [d1], "v": [1]}), "cat", "src", d1, root=lake)
    datalake.write(pd.DataFrame({"ts": [d2], "v": [2]}), "cat", "src", d2, root=lake)
    assert len(datalake.read("cat", "src", d1, d2, root=lake)) == 2
    assert len(datalake.read("cat", "src", d1, d1, root=lake)) == 1


def test_write_tolerates_unhashable_columns(lake):
    """A list/dict column (e.g. OKX's raw bids/asks) can't be de-duped on directly - write() falls back to the
    other (hashable) columns instead of crashing. Here that's just `ts`, so an identical re-run still collapses."""
    df = pd.DataFrame({"ts": [NOW], "bids": [[["1", "2"]]]})
    datalake.write(df, "cat", "src", NOW, root=lake)
    datalake.write(df, "cat", "src", NOW, root=lake)
    back = datalake.read("cat", "src", NOW, NOW, root=lake)
    assert len(back) == 1
    df2 = pd.DataFrame({"ts": [NOW + pd.Timedelta(seconds=1)], "bids": [[["3", "4"]]]})
    datalake.write(df2, "cat", "src", NOW, root=lake)
    assert len(datalake.read("cat", "src", NOW, NOW, root=lake)) == 2   # a genuinely different row is still kept


# ---------------------------------------------------------------- OKX adapter
OKX_FUNDING = {"data": [{"instId": "BTC-USDT-SWAP", "fundingRate": "0.0001", "fundingTime": "1735732800000"}]}
OKX_OI = {"data": [{"instId": "BTC-USDT-SWAP", "oi": "100", "oiCcy": "1000", "oiUsd": "5000000", "ts": "1735732800000"}]}
OKX_LIQ = {"data": [{"instId": "BTC-USDT-SWAP", "details": [{"ts": "1735732800000", "side": "buy", "posSide": "short", "bkPx": "50000", "sz": "1.5"}]}]}
OKX_BOOK = {"data": [{"ts": "1735732800000", "bids": [["49999", "1"]], "asks": [["50001", "1"]]}]}
OKX_TRADES = {"data": [{"tradeId": "1", "ts": "1735732800000", "px": "50000", "sz": "0.1", "side": "buy"}]}


def test_okx_funding_and_oi_and_liq_and_book_and_trades_parse_correctly(monkeypatch):
    def fake_get(url, params=None, **kw):
        if "funding-rate" in url:
            return OKX_FUNDING
        if "open-interest" in url:
            return OKX_OI
        if "liquidation-orders" in url:
            return OKX_LIQ
        if "books" in url:
            return OKX_BOOK
        if "trades" in url:
            return OKX_TRADES
        raise AssertionError(url)
    monkeypatch.setattr(okx, "http_get", fake_get)
    f = okx.fetch_funding_rate(NOW, ["BTC"])
    assert f.iloc[0]["funding_rate"] == 0.0001 and f.iloc[0]["symbol"] == "BTC"
    oi = okx.fetch_open_interest(NOW, ["BTC"])
    assert oi.iloc[0]["oi_usd"] == 5_000_000.0
    liq = okx.fetch_liquidations(NOW, ["BTC"])
    assert liq.iloc[0]["size"] == 1.5 and liq.iloc[0]["side"] == "buy"
    book = okx.fetch_order_book(NOW, ["BTC"])
    assert book.iloc[0]["bids"] == [["49999", "1"]]
    trades = okx.fetch_trades(NOW, ["BTC"])
    assert trades.iloc[0]["aggressor"] == "buy" and trades.iloc[0]["price"] == 50000.0


def test_okx_adapter_skips_a_symbol_that_errors_without_failing_the_whole_fetch(monkeypatch):
    def fake_get(url, params=None, **kw):
        if params.get("instId") == "ETH-USDT-SWAP" or params.get("instFamily") == "ETH-USDT":
            raise ConnectionError("down")
        return OKX_FUNDING
    monkeypatch.setattr(okx, "http_get", fake_get)
    f = okx.fetch_funding_rate(NOW, ["BTC", "ETH", "XRP"])
    assert set(f["symbol"]) == {"BTC", "XRP"}


# ---------------------------------------------------------------- Deribit adapter
def test_deribit_only_covers_btc_eth_and_pairs_funding_with_index(monkeypatch):
    def fake_get(url, params=None, **kw):
        if "funding_rate_value" in url:
            return {"result": 0.0003}
        if "index_price" in url:
            return {"result": {"index_price": 51000.0}}
        raise AssertionError(url)
    monkeypatch.setattr(deribit, "http_get", fake_get)
    df = deribit.fetch_funding_and_index(NOW, ["BTC", "ETH", "XRP"])
    assert set(df["symbol"]) == {"BTC", "ETH"}   # XRP silently skipped: no XRP derivatives on Deribit
    assert df[df.symbol == "BTC"]["index_price"].iloc[0] == 51000.0


# ---------------------------------------------------------------- DefiLlama adapter
def test_defillama_stablecoins_and_tvl_parse_the_nested_payload(monkeypatch):
    def fake_get(url, params=None, **kw):
        if "stablecoincharts" in url:
            return [{"date": "1735732800", "totalCirculatingUSD": {"peggedUSD": 123.0}}]
        if "historicalChainTvl" in url:
            return [{"date": 1735732800, "tvl": 456.0}]
        raise AssertionError(url)
    monkeypatch.setattr(defillama, "http_get", fake_get)
    sc = defillama.fetch_stablecoins(NOW)
    assert sc.iloc[0]["total_circulating_usd"] == 123.0
    tvl = defillama.fetch_tvl(NOW)
    assert tvl.iloc[0]["total_tvl_usd"] == 456.0


# ---------------------------------------------------------------- on-chain adapters
def test_blockchain_info_btc_charts_parses_every_metric(monkeypatch):
    monkeypatch.setattr(onchain, "http_get", lambda url, params=None, **kw: {"values": [{"x": 1735732800, "y": 1.0}]})
    df = onchain.fetch_btc_charts(NOW)
    assert set(df["metric"]) == set(onchain.CHARTS.keys())


def test_etherscan_adapter_is_skipped_not_broken_without_a_key(monkeypatch):
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
    assert onchain.fetch_eth_supply(NOW).empty


def test_etherscan_adapter_parses_supply_when_key_present(monkeypatch):
    monkeypatch.setenv("ETHERSCAN_API_KEY", "free-tier-key")
    monkeypatch.setattr(onchain, "http_get", lambda url, params=None, **kw: {"status": "1", "result": {"EthSupply": str(120_000_000 * 10**18)}})
    df = onchain.fetch_eth_supply(NOW)
    assert df.iloc[0]["eth_supply"] == pytest.approx(120_000_000)


# ---------------------------------------------------------------- registry
def test_registry_lists_every_adapter_with_a_unique_name_and_valid_category():
    names = [a.name for a in registry.ALL]
    assert len(names) == len(set(names)) and len(names) >= 8
    for a in registry.ALL:
        assert a.symbols and a.category and a.source
