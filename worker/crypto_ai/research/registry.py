"""Source registry + scheduler. Each source has its own polling interval; `run_due` only polls what is due,
so it is safe to call every minute or every 30 minutes. Errors are stored redacted of any API key."""
from datetime import datetime, timedelta, timezone

from ..http import redact
from .common import TIERS

M = 60
# key, name, kind, tier, url, interval_seconds
SOURCES = [
    ("sec_press", "SEC press releases", "rss", 1, "https://www.sec.gov/news/pressreleases.rss", 5 * M),
    ("sec_statements", "SEC speeches & statements", "rss", 1, "https://www.sec.gov/news/speeches-statements.rss", 5 * M),
    ("fed_press", "Federal Reserve press releases", "rss", 1, "https://www.federalreserve.gov/feeds/press_all.xml", 5 * M),
    ("fed_speeches", "Federal Reserve speeches", "rss", 1, "https://www.federalreserve.gov/feeds/speeches.xml", 5 * M),
    ("cftc_press", "CFTC press releases", "rss", 1, "https://www.cftc.gov/RSS/RSSGP/rssgp.xml", 5 * M),
    ("cftc_enforcement", "CFTC enforcement", "rss", 1, "https://www.cftc.gov/RSS/RSSENF/rssenf.xml", 5 * M),
    ("congress_api", "Congress.gov API", "api", 1, "https://api.congress.gov/v3", 12 * M),
    ("fred_api", "FRED (St. Louis Fed)", "api", 1, "https://api.stlouisfed.org/fred", 45 * M),
    ("xrpl_rippled", "XRP Ledger (rippled releases)", "rss", 2, "https://github.com/XRPLF/rippled/releases.atom", 5 * M),
    ("eth_foundation", "Ethereum Foundation blog", "rss", 2, "https://blog.ethereum.org/en/feed.xml", 5 * M),
    ("eth_geth", "go-ethereum releases", "rss", 2, "https://github.com/ethereum/go-ethereum/releases.atom", 5 * M),
    ("cnbc_finance", "CNBC Finance", "rss", 3, "https://www.cnbc.com/id/10000664/device/rss/rss.html", 10 * M),
    ("marketwatch", "MarketWatch top stories", "rss", 3, "https://feeds.content.dowjones.io/public/rss/mw_topstories", 10 * M),
    ("coindesk", "CoinDesk", "rss", 4, "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml", 5 * M),
    ("cointelegraph", "Cointelegraph", "rss", 4, "https://cointelegraph.com/rss", 5 * M),
    ("decrypt", "Decrypt", "rss", 4, "https://decrypt.co/feed", 5 * M),
    ("polymarket", "Polymarket (public, read-only)", "api", 5, "https://gamma-api.polymarket.com", 5 * M),
    ("kalshi", "Kalshi (public, read-only)", "api", 5, "https://api.elections.kalshi.com/trade-api/v2", 5 * M),
]


def ensure_registry(db) -> None:
    for key, name, kind, tier, url, interval in SOURCES:
        label, cred = TIERS[tier]
        db.run("""insert into source_registry (key,name,kind,tier,tier_label,credibility_score,url,poll_interval_s)
                  values (%s,%s,%s,%s,%s,%s,%s,%s)
                  on conflict (key) do update set name=excluded.name, kind=excluded.kind, tier=excluded.tier,
                    tier_label=excluded.tier_label, credibility_score=excluded.credibility_score, url=excluded.url,
                    poll_interval_s=excluded.poll_interval_s""", [key, name, kind, tier, label, cred, url, interval])


def _pollers():
    from . import congress, feeds, fred, markets
    return {"congress_api": congress.poll, "fred_api": fred.poll, "polymarket": markets.poll_polymarket, "kalshi": markets.poll_kalshi}, feeds.poll_rss


def run_due(db, only: list[str] | None = None, force: bool = False, interval_s: int | None = None) -> dict:
    ensure_registry(db)
    special, rss = _pollers()
    now = datetime.now(timezone.utc)
    report = {}
    for src in db.all("select * from source_registry where enabled order by tier, key"):
        if only and src["key"] not in only:
            continue
        due = force or src["last_polled_at"] is None or now - src["last_polled_at"] >= timedelta(seconds=interval_s or src["poll_interval_s"])
        if not due:
            continue
        fn = special.get(src["key"], rss)
        try:
            res = fn(db, src) or {}
            db.run("update source_registry set last_polled_at=%s, last_success_at=%s, last_status=%s, last_error=null, items_last_run=%s where key=%s",
                   [now, now, res.get("status", "ok"), res.get("new", 0), src["key"]])
            report[src["key"]] = res
        except Exception as e:                     # one bad source never stops the others
            msg = redact(f"{type(e).__name__}: {e}")[:300]
            db.run("update source_registry set last_polled_at=%s, last_status='error', last_error=%s where key=%s", [now, msg, src["key"]])
            report[src["key"]] = {"status": "error", "error": msg}
    return report
