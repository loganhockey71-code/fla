// Static mirror of worker/crypto_ai/datasources/registry.py's adapter list, for the dashboard. The dashboard has no
// access to the Python worker process or its .env, so "key required" here is a fact about the adapter, not a live
// check of whether the key is actually set - only the worker knows that (see worker/DATA_SOURCES.md).
// Keep this in sync by hand when adapters are added/removed in the Python registry.
export type SourceMeta = { name: string; category: string; source: string; symbols: string[]; keyRequired: string | null };

export const DATA_SOURCES: SourceMeta[] = [
  { name: "coinbase_candles_1m", category: "ohlcv", source: "Coinbase Exchange public API", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "coinbase_ticker", category: "bid_ask", source: "Coinbase Exchange public API", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "coinbase_order_book_imbalance", category: "order_books_l2", source: "Coinbase Exchange public API", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "okx_funding_rate", category: "funding_rates", source: "OKX public API", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "okx_open_interest", category: "open_interest", source: "OKX public API", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "okx_liquidations", category: "liquidations", source: "OKX public API", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "okx_order_book", category: "order_books_l2", source: "OKX public API", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "okx_trades", category: "tick_trades", source: "OKX public API", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "kraken_ohlc", category: "ohlcv", source: "Kraken public API", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "kraken_trades", category: "tick_trades", source: "Kraken public API", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "kraken_spread", category: "bid_ask", source: "Kraken public API", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "kraken_depth", category: "order_books_l2", source: "Kraken public API", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "binance_candles_1m", category: "ohlcv", source: "Binance public API (geo-restricted in some regions)", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "binance_trades", category: "tick_trades", source: "Binance public API (geo-restricted in some regions)", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "binance_agg_trades", category: "tick_trades", source: "Binance public API (geo-restricted in some regions)", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "binance_funding_rate", category: "funding_rates", source: "Binance public API (geo-restricted in some regions)", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "binance_open_interest", category: "open_interest", source: "Binance public API (geo-restricted in some regions)", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "coingecko_markets", category: "market_data", source: "CoinGecko public API", symbols: ["BTC", "ETH", "XRP"], keyRequired: "COINGECKO_API_KEY (optional)" },
  { name: "coingecko_trending", category: "market_data", source: "CoinGecko public API", symbols: ["BTC", "ETH", "XRP"], keyRequired: "COINGECKO_API_KEY (optional)" },
  { name: "coingecko_market_chart", category: "ohlcv", source: "CoinGecko public API", symbols: ["BTC", "ETH", "XRP"], keyRequired: "COINGECKO_API_KEY (optional)" },
  { name: "coinmarketcap_quotes", category: "market_data", source: "CoinMarketCap API (free tier)", symbols: ["BTC", "ETH", "XRP"], keyRequired: "COINMARKETCAP_API_KEY" },
  { name: "dexscreener_pairs", category: "dex_data", source: "DexScreener public API", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "geckoterminal_trending_pools", category: "dex_data", source: "GeckoTerminal public API", symbols: ["ETH"], keyRequired: null },
  { name: "deribit_funding_index", category: "futures_basis", source: "Deribit public API", symbols: ["BTC", "ETH"], keyRequired: null },
  { name: "defillama_stablecoins", category: "stablecoin_flows", source: "DefiLlama public API", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "defillama_tvl", category: "defi_tvl", source: "DefiLlama public API", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "blockchain_info_btc_charts", category: "onchain_metrics", source: "blockchain.info Charts API", symbols: ["BTC"], keyRequired: null },
  { name: "etherscan_eth_supply", category: "onchain_metrics", source: "Etherscan API (free tier)", symbols: ["ETH"], keyRequired: "ETHERSCAN_API_KEY" },
  { name: "xrpl_ledger_stats", category: "onchain_metrics", source: "Public rippled node (JSON-RPC)", symbols: ["XRP"], keyRequired: null },
  { name: "xrpl_fee_stats", category: "onchain_metrics", source: "Public rippled node (JSON-RPC)", symbols: ["XRP"], keyRequired: null },
  { name: "gdelt_articles", category: "news", source: "GDELT Project Doc API", symbols: ["BTC", "ETH", "XRP"], keyRequired: null },
  { name: "reddit_hot_posts", category: "social_sentiment", source: "Reddit official API", symbols: ["BTC", "ETH", "XRP"], keyRequired: "REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET" },
];
