"""CoinGecko free API: backup / reference price only."""
import requests

from .config import COINGECKO_IDS


def reference_prices() -> dict[str, float]:
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": ",".join(COINGECKO_IDS.values()), "vs_currencies": "usd"},
                         timeout=15, headers={"User-Agent": "crypto-ai-paper-lab/1.0"})
        r.raise_for_status()
        j = r.json()
        return {s: float(j[cid]["usd"]) for s, cid in COINGECKO_IDS.items() if cid in j}
    except Exception:
        return {}
