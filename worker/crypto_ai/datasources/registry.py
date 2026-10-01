"""All adapters, discovered from each source module's `ADAPTERS` list. Adding a source = adding a module here and
nothing else changes (`cli.py collect-datasources`, `datalake.run_due`, the dashboard, tests all just iterate `ALL`)."""
from . import binance, coinbase, coingecko, coinmarketcap, defillama, deribit, dexscreener, gdelt, geckoterminal, kraken, okx, onchain, reddit, xrpl

ALL = [
    *coinbase.ADAPTERS, *okx.ADAPTERS, *kraken.ADAPTERS, *binance.ADAPTERS,
    *coingecko.ADAPTERS, *coinmarketcap.ADAPTERS,
    *dexscreener.ADAPTERS, *geckoterminal.ADAPTERS,
    *deribit.ADAPTERS, *defillama.ADAPTERS, *onchain.ADAPTERS, *xrpl.ADAPTERS,
    *gdelt.ADAPTERS, *reddit.ADAPTERS,
]

# Keys (env var name -> what it unlocks), for the .env.example / README / dashboard "key required" column.
OPTIONAL_KEYS = {
    "COINGECKO_API_KEY": ["coingecko_markets", "coingecko_trending", "coingecko_market_chart"],
    "COINMARKETCAP_API_KEY": ["coinmarketcap_quotes"],
    "ETHERSCAN_API_KEY": ["etherscan_eth_supply"],
    "REDDIT_CLIENT_ID": ["reddit_hot_posts"], "REDDIT_CLIENT_SECRET": ["reddit_hot_posts"],
}
