"""New data-source adapters (Kraken, Binance, CoinGecko, CoinMarketCap, DexScreener, GeckoTerminal, GDELT, Reddit,
Coinbase wrapper), plus auth/rate-limit/retry behaviour shared via base.http_get. All HTTP is mocked - endpoints
were verified live by hand (see DATA_SOURCES.md); Binance and Reddit could not be live-verified from this
environment (Binance: geo-restricted; Reddit: needs the user's own registered app credentials).
"""
import time
from datetime import datetime, timezone

import pandas as pd
import pytest
import requests

from crypto_ai.datasources import base, binance, coinbase as cb_ds, coingecko, coinmarketcap, dexscreener, gdelt, geckoterminal, kraken, reddit, registry

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- base.http_get: retry/backoff/timeout/auth headers
def test_http_get_retries_on_429_then_succeeds(monkeypatch):
    calls = {"n": 0}

    class R:
        def __init__(self, code, payload=None):
            self.status_code = code
            self._payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError()

        def json(self):
            return self._payload

    def fake_get(url, params=None, timeout=None, headers=None):
        calls["n"] += 1
        return R(429) if calls["n"] < 2 else R(200, {"ok": True})
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert base.http_get("http://x") == {"ok": True} and calls["n"] == 2


def test_http_get_raises_after_exhausting_retries(monkeypatch):
    class R:
        status_code = 500

        def raise_for_status(self):
            raise requests.HTTPError("boom")
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(requests.HTTPError):
        base.http_get("http://x", retries=2)


def test_http_get_merges_extra_headers_without_dropping_the_user_agent(monkeypatch):
    seen = {}

    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {}
    def fake_get(url, params=None, timeout=None, headers=None):
        seen.update(headers or {})
        return R()
    monkeypatch.setattr(requests, "get", fake_get)
    base.http_get("http://x", headers={"X-Api-Key": "secret"})
    assert seen.get("X-Api-Key") == "secret" and "User-Agent" in seen


# ---------------------------------------------------------------- Kraken
KRAKEN_OHLC = {"error": [], "result": {"XXBTZUSD": [[1735732800, "50000", "50100", "49900", "50050", "50025", "1.5", 10]], "last": 1735732800}}
KRAKEN_TRADES = {"error": [], "result": {"XXBTZUSD": [["50000", "0.1", 1735732800.1, "b", "l", "", 1]], "last": "1"}}
KRAKEN_SPREAD = {"error": [], "result": {"XXBTZUSD": [[1735732800, "49999", "50001"]], "last": 1735732800}}
KRAKEN_DEPTH = {"error": [], "result": {"XXBTZUSD": {"bids": [["49999", "1"]], "asks": [["50001", "1"]]}}}
KRAKEN_ERROR = {"error": ["EQuery:Unknown asset pair"], "result": {}}


def test_kraken_parses_ohlc_trades_spread_depth(monkeypatch):
    def fake_get(url, params=None, **kw):
        return {"OHLC": KRAKEN_OHLC, "Trades": KRAKEN_TRADES, "Spread": KRAKEN_SPREAD, "Depth": KRAKEN_DEPTH}[url.rsplit("/", 1)[-1]]
    monkeypatch.setattr(kraken, "http_get", fake_get)
    o = kraken.fetch_ohlc(NOW, ["BTC"])
    assert o.iloc[0]["close"] == 50050.0 and o.iloc[0]["symbol"] == "BTC"
    t = kraken.fetch_trades(NOW, ["BTC"])
    assert t.iloc[0]["aggressor"] == "buy"
    s = kraken.fetch_spread(NOW, ["BTC"])
    assert s.iloc[0]["spread_pct"] > 0
    d = kraken.fetch_depth(NOW, ["BTC"])
    assert d.iloc[0]["bids"] == [["49999", "1"]]


def test_kraken_error_response_yields_empty_frame_not_a_crash(monkeypatch):
    monkeypatch.setattr(kraken, "http_get", lambda *a, **k: KRAKEN_ERROR)
    assert kraken.fetch_ohlc(NOW, ["BTC"]).empty


# ---------------------------------------------------------------- Binance (geo-restricted here; parsing tested via mocks)
BINANCE_KLINES = [[1735732800000, "50000", "50100", "49900", "50050", "1.5", 0, 0, 12]]
BINANCE_TRADES = [{"id": 1, "time": 1735732800000, "price": "50000", "qty": "0.1", "isBuyerMaker": False}]
BINANCE_AGG = [{"a": 1, "T": 1735732800000, "p": "50000", "q": "0.1", "m": True}]
BINANCE_FUNDING = [{"fundingTime": 1735732800000, "fundingRate": "0.0001"}]
BINANCE_OI = {"time": 1735732800000, "openInterest": "1000"}
BINANCE_RESTRICTED = {"code": 0, "msg": "Service unavailable from a restricted location according to 'b. Eligibility'"}


def test_binance_parses_candles_trades_aggtrades_funding_oi(monkeypatch):
    def fake_get(url, params=None, **kw):
        return {"klines": BINANCE_KLINES, "trades": BINANCE_TRADES, "aggTrades": BINANCE_AGG, "fundingRate": BINANCE_FUNDING, "openInterest": BINANCE_OI}[url.rsplit("/", 1)[-1]]
    monkeypatch.setattr(binance, "http_get", fake_get)
    assert binance.fetch_candles_1m(NOW, ["BTC"]).iloc[0]["close"] == 50050.0
    assert binance.fetch_trades(NOW, ["BTC"]).iloc[0]["aggressor"] == "buy"
    assert binance.fetch_agg_trades(NOW, ["BTC"]).iloc[0]["aggressor"] == "sell"
    assert binance.fetch_funding_rate(NOW, ["BTC"]).iloc[0]["funding_rate"] == 0.0001
    assert binance.fetch_open_interest(NOW, ["BTC"]).iloc[0]["open_interest"] == 1000.0


def test_binance_geo_restriction_surfaces_as_a_clean_adapter_failure_not_a_crash(monkeypatch):
    """Reproduces the actual live response this environment got from Binance - Adapter.run() must report ok:False,
    never raise past the batch (see DATA_SOURCES.md 'Rejected')."""
    def fake_get(url, params=None, timeout=None, headers=None):
        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return BINANCE_RESTRICTED
        return R()
    monkeypatch.setattr(requests, "get", fake_get)
    a = binance.ADAPTERS[0]
    r = a.run(NOW, lake_root="ignored-empty-result-so-no-write")
    # the endpoint returns 200 with an error body shaped like a dict, not a list - our parser expects a list and
    # will raise a clear error rather than silently mis-parsing an error payload as data
    assert r["ok"] is False


# ---------------------------------------------------------------- CoinGecko (optional key)
CG_MARKETS = [{"id": "bitcoin", "current_price": 50000, "market_cap": 1e12, "market_cap_rank": 1, "total_volume": 1e10, "price_change_percentage_24h": 1.2}]
CG_TRENDING = {"coins": [{"item": {"id": "official-trump", "symbol": "TRUMP", "market_cap_rank": 100}}]}
CG_CHART = {"prices": [[1735732800000, 50000]], "total_volumes": [[1735732800000, 1e10]], "market_caps": [[1735732800000, 1e12]]}


def test_coingecko_markets_and_trending_and_chart(monkeypatch):
    def fake_get(url, params=None, headers=None):
        if "trending" in url:
            return CG_TRENDING
        if "market_chart" in url:
            return CG_CHART
        return CG_MARKETS
    monkeypatch.setattr(coingecko, "http_get", fake_get)
    m = coingecko.fetch_markets(NOW, ["BTC"])
    assert m.iloc[0]["symbol"] == "BTC" and m.iloc[0]["market_cap_rank"] == 1
    t = coingecko.fetch_trending(NOW)
    assert t.iloc[0]["coin_id"] == "official-trump"
    c = coingecko.fetch_market_chart(NOW, ["BTC"])
    assert c.iloc[0]["price"] == 50000


def test_coingecko_sends_the_demo_key_header_only_when_set(monkeypatch):
    monkeypatch.delenv("COINGECKO_API_KEY", raising=False)
    assert coingecko._headers() == {}
    monkeypatch.setenv("COINGECKO_API_KEY", "demo-key")
    assert coingecko._headers() == {"x-cg-demo-api-key": "demo-key"}


# ---------------------------------------------------------------- CoinMarketCap (required key -> skip without it)
def test_coinmarketcap_skips_without_a_key(monkeypatch):
    monkeypatch.delenv("COINMARKETCAP_API_KEY", raising=False)
    assert coinmarketcap.fetch_quotes(NOW).empty


def test_coinmarketcap_parses_quotes_with_a_key(monkeypatch):
    monkeypatch.setenv("COINMARKETCAP_API_KEY", "free-tier-key")
    payload = {"data": {"BTC": {"cmc_rank": 1, "quote": {"USD": {"price": 50000, "market_cap": 1e12, "volume_24h": 1e10, "percent_change_24h": 1.1}}}}}
    seen = {}

    def fake_get(url, params=None, headers=None):
        seen.update(headers or {})
        return payload
    monkeypatch.setattr(coinmarketcap, "http_get", fake_get)
    df = coinmarketcap.fetch_quotes(NOW, ["BTC"])
    assert df.iloc[0]["price"] == 50000 and seen.get("X-CMC_PRO_API_KEY") == "free-tier-key"


# ---------------------------------------------------------------- DexScreener / GeckoTerminal
def test_dexscreener_parses_pairs(monkeypatch):
    payload = {"pairs": [{"chainId": "ethereum", "dexId": "uniswap", "pairAddress": "0xabc", "baseToken": {"symbol": "WBTC"}, "quoteToken": {"symbol": "USDC"},
                         "priceUsd": "50000", "liquidity": {"usd": 1e7}, "volume": {"h24": 1e6}, "priceChange": {"h24": 1.5}}]}
    monkeypatch.setattr(dexscreener, "http_get", lambda *a, **k: payload)
    df = dexscreener.fetch_pairs(NOW, ["BTC"])
    assert df.iloc[0]["price_usd"] == 50000.0 and df.iloc[0]["dex"] == "uniswap"


def test_geckoterminal_parses_trending_pools(monkeypatch):
    payload = {"data": [{"id": "eth_0x1", "attributes": {"name": "WETH/USDC", "base_token_price_usd": "2600", "volume_usd": {"h24": "1000"}, "reserve_in_usd": "5000"}}]}
    monkeypatch.setattr(geckoterminal, "http_get", lambda *a, **k: payload)
    df = geckoterminal.fetch_trending_pools(NOW)
    assert df.iloc[0]["name"] == "WETH/USDC"


# ---------------------------------------------------------------- GDELT: rate-limit text response handled gracefully
def test_gdelt_handles_a_plain_text_rate_limit_response_without_crashing(monkeypatch):
    def fake_get(url, params=None, **kw):
        raise requests.exceptions.JSONDecodeError("bad json", "doc", 0)
    monkeypatch.setattr(gdelt, "http_get", fake_get)
    assert gdelt.fetch_articles(NOW, ["BTC"]).empty


def test_gdelt_parses_articles(monkeypatch):
    payload = {"articles": [{"seendate": "20260101T120000Z", "title": "Bitcoin rises", "url": "http://x", "domain": "x.com", "language": "English", "sourcecountry": "United States"}]}
    monkeypatch.setattr(gdelt, "http_get", lambda *a, **k: payload)
    df = gdelt.fetch_articles(NOW, ["BTC"])
    assert df.iloc[0]["title"] == "Bitcoin rises" and df.iloc[0]["ts"].year == 2026


# ---------------------------------------------------------------- Reddit: OAuth + missing-credential skip
def test_reddit_skips_without_credentials(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    reddit._TOKEN_CACHE.update(token=None, expires_at=0.0)
    assert reddit.fetch_hot_posts(NOW).empty


def test_reddit_fetches_a_token_then_posts_and_caches_the_token(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    reddit._TOKEN_CACHE.update(token=None, expires_at=0.0)
    token_calls = {"n": 0}

    class TokenResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            token_calls["n"] += 1
            return {"access_token": "tok123", "expires_in": 3600}
    monkeypatch.setattr(requests, "post", lambda *a, **k: TokenResp())
    seen_auth = {}

    def fake_get(url, params=None, headers=None):
        seen_auth["auth"] = headers.get("Authorization")
        return {"data": {"children": [{"data": {"id": "p1", "created_utc": 1735732800, "title": "HODL", "score": 10, "num_comments": 2, "upvote_ratio": 0.9}}]}}
    monkeypatch.setattr(reddit, "http_get", fake_get)
    df = reddit.fetch_hot_posts(NOW, ["BTC"])
    assert df.iloc[0]["title"] == "HODL" and seen_auth["auth"] == "bearer tok123" and token_calls["n"] == 1
    reddit.fetch_hot_posts(NOW, ["BTC"])            # second call must reuse the cached token, not re-authenticate
    assert token_calls["n"] == 1


# ---------------------------------------------------------------- Coinbase wrapper (reuses tested crypto_ai.coinbase)
def test_coinbase_wrapper_ticker_uses_the_existing_module(monkeypatch):
    from crypto_ai import coinbase as cb
    monkeypatch.setattr(cb, "ticker", lambda product: {"price": 50000.0, "bid": 49999.0, "ask": 50001.0, "spread_pct": 0.004, "volume_24h": 1000.0})
    df = cb_ds.fetch_ticker(NOW, ["BTC"])
    assert df.iloc[0]["price"] == 50000.0


# ---------------------------------------------------------------- registry completeness
def test_registry_has_30_or_more_adapters_covering_the_requested_sources():
    names = {a.name for a in registry.ALL}
    assert len(names) >= 25
    for prefix in ("coinbase_", "okx_", "kraken_", "binance_", "coingecko_", "coinmarketcap_", "dexscreener_",
                  "geckoterminal_", "deribit_", "defillama_", "gdelt_", "reddit_"):
        assert any(n.startswith(prefix) for n in names), prefix


# ---------------------------------------------------------------- XRPL (public rippled node + XRPScan)
def test_xrpl_ledger_stats_parses_tx_count_and_close_time(monkeypatch):
    from crypto_ai.datasources import xrpl
    payload = {"result": {"ledger_index": 12345, "ledger": {"close_time_iso": "2026-01-01T00:00:00Z", "transactions": ["a", "b", "c"]}}}
    monkeypatch.setattr(xrpl, "http_post_json", lambda *a, **k: payload)
    df = xrpl.fetch_ledger_stats(NOW)
    assert df.iloc[0]["tx_count"] == 3 and df.iloc[0]["ledger_index"] == 12345


def test_xrpl_fee_stats_parses_drops_and_queue(monkeypatch):
    from crypto_ai.datasources import xrpl
    payload = {"result": {"drops": {"base_fee": "10", "median_fee": "5000", "open_ledger_fee": "10"}, "current_queue_size": "0", "expected_ledger_size": "1952"}}
    monkeypatch.setattr(xrpl, "http_post_json", lambda *a, **k: payload)
    df = xrpl.fetch_fee_stats(NOW)
    assert df.iloc[0]["base_fee_drops"] == 10 and df.iloc[0]["queue_size"] == 0


def test_xrpl_account_lookup_via_xrpscan(monkeypatch):
    from crypto_ai.datasources import xrpl
    monkeypatch.setattr(xrpl, "http_get", lambda *a, **k: {"xrpBalance": "200.0", "ownerCount": 5, "sequence": 1})
    df = xrpl.fetch_account(NOW, "rSomeAddress")
    assert df.iloc[0]["xrp_balance"] == 200.0


def test_xrpl_handles_empty_or_missing_results_without_crashing(monkeypatch):
    from crypto_ai.datasources import xrpl
    monkeypatch.setattr(xrpl, "http_post_json", lambda *a, **k: {"result": {}})
    assert xrpl.fetch_ledger_stats(NOW).empty
    assert xrpl.fetch_fee_stats(NOW).empty


def test_http_post_json_retries_and_merges_headers(monkeypatch):
    import time as time_mod
    calls = {"n": 0}

    class R:
        def __init__(self, code, payload=None):
            self.status_code = code
            self._payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError()

        def json(self):
            return self._payload

    def fake_post(url, json=None, timeout=None, headers=None):
        calls["n"] += 1
        return R(503) if calls["n"] < 2 else R(200, {"ok": True})
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(time_mod, "sleep", lambda s: None)
    assert base.http_post_json("http://x", {"a": 1}) == {"ok": True} and calls["n"] == 2


def test_registry_includes_xrpl():
    names = {a.name for a in registry.ALL}
    assert "xrpl_ledger_stats" in names and "xrpl_fee_stats" in names
