"""FRED (St. Louis Fed) macro series. Key comes from FRED_API_KEY, server-side only, never logged."""
import os
from datetime import date, datetime, timedelta, timezone

from ..http import SourceError, get_json
from .common import record_event

API = "https://api.stlouisfed.org/fred/series/observations"
HISTORY_START = "2019-01-01"

# id -> (label, release lag in days: when the market could first have known the value)
SERIES = {
    "DFF": ("Federal Funds Rate", 1),
    "DGS2": ("2-Year Treasury yield", 1),
    "DGS10": ("10-Year Treasury yield", 1),
    "CPIAUCSL": ("CPI (all items)", 45),
    "CPILFESL": ("Core CPI", 45),
    "UNRATE": ("Unemployment rate", 37),
    "PAYEMS": ("Nonfarm payrolls", 37),
    "ICSA": ("Initial jobless claims", 5),
}


def _key() -> str:
    k = os.environ.get("FRED_API_KEY")
    if not k:
        raise SourceError("FRED_API_KEY is not set")
    return k


def fetch(series_id: str, start: str, limit: int | None = None) -> list[tuple[date, float]]:
    params = {"series_id": series_id, "api_key": _key(), "file_type": "json", "observation_start": start}
    if limit:
        params.update(sort_order="desc", limit=limit)
    j = get_json(API, params, ttl=600)
    out = []
    for o in j.get("observations", []):
        if o["value"] not in (".", "", None):
            out.append((date.fromisoformat(o["date"]), float(o["value"])))
    return sorted(out)


def available_at(obs: date, lag_days: int) -> datetime:
    return datetime(obs.year, obs.month, obs.day, tzinfo=timezone.utc) + timedelta(days=lag_days)


def macro_event(sid: str, label: str, obs: date, val: float, hist: list[tuple[date, float]]) -> dict | None:
    """Turn a fresh release into a research event, or None if it's not notable."""
    prev = [v for d, v in hist if d < obs]
    if not prev:
        return None
    last = prev[-1]
    cat, sentiment, imp, text = "macro", "neutral", 0, ""
    if sid == "DFF":
        chg = val - (prev[-5] if len(prev) >= 5 else last)
        if abs(chg) < 0.10:
            return None
        cat, sentiment, imp = "interest_rates", ("negative" if chg > 0 else "positive"), 70
        text = f"Effective federal funds rate {'up' if chg > 0 else 'down'} {abs(chg) * 100:.0f} bps to {val:.2f}%"
    elif sid in ("DGS2", "DGS10"):
        chg = val - last
        if abs(chg) < 0.10:
            return None
        cat, sentiment, imp = "interest_rates", ("negative" if chg > 0 else "positive"), 50
        text = f"{label} {'jumped' if chg > 0 else 'fell'} {abs(chg) * 100:.0f} bps to {val:.2f}%"
    elif sid in ("CPIAUCSL", "CPILFESL"):
        allv = [v for _, v in hist]
        if len(allv) < 14:
            return None
        yoy, prev_yoy = (val / allv[-13] - 1) * 100, (last / allv[-14] - 1) * 100
        d = yoy - prev_yoy
        cat, imp = "inflation", 60 + min(int(abs(d) * 40), 25)
        sentiment = "negative" if d > 0.05 else "positive" if d < -0.05 else "neutral"
        text = f"{label} {yoy:.1f}% year over year for {obs:%B %Y}, {'up' if d > 0 else 'down'} from {prev_yoy:.1f}%"
    elif sid == "UNRATE":
        d = val - last
        cat, imp = "macro", 50 + min(int(abs(d) * 50), 20)
        sentiment = "negative" if d >= 0.2 else "neutral"
        text = f"US unemployment rate {val:.1f}% for {obs:%B %Y} (prior {last:.1f}%)"
    elif sid == "PAYEMS":
        d = val - last
        cat, imp = "macro", 50
        text = f"Nonfarm payrolls {'added' if d >= 0 else 'lost'} {abs(d):.0f}k jobs in {obs:%B %Y}"
    elif sid == "ICSA":
        if last <= 0 or val / last - 1 < 0.10:
            return None
        cat, sentiment, imp = "macro", "negative", 40
        text = f"Initial jobless claims jumped {(val / last - 1) * 100:.0f}% to {val:,.0f}"
    else:
        return None
    return {"category": cat, "coins": ["BTC", "ETH", "XRP"], "named": [], "sentiment": sentiment, "importance": imp, "text": text}


def poll(db, src: dict) -> dict:
    stored = new_rows = events = 0
    now = datetime.now(timezone.utc)
    for sid, (label, lag) in SERIES.items():
        have = db.one("select max(obs_date) d from macro_data where series_id=%s", [sid])["d"]
        start = HISTORY_START if have is None else (have - timedelta(days=60)).isoformat()   # re-read 60d to catch revisions
        obs = fetch(sid, start)
        known = {r["obs_date"] for r in db.all("select obs_date from macro_data where series_id=%s and obs_date >= %s", [sid, start])}
        db.bulk("insert into macro_data (series_id, obs_date, value, available_at) values %s "
                "on conflict (series_id, obs_date) do update set value=excluded.value, fetched_at=now()",
                [(sid, d, v, available_at(d, lag)) for d, v in obs])
        stored += len(obs)
        fresh = [(d, v) for d, v in obs if d not in known]
        new_rows += len(fresh)
        if have is None:
            continue                                     # first backfill: history only, no synthetic "events"
        full = [(r["obs_date"], r["value"]) for r in db.all(
            "select obs_date, value from macro_data where series_id=%s order by obs_date", [sid])]
        for d, v in fresh:
            if (now.date() - d).days > 75:
                continue
            ev = macro_event(sid, label, d, v, [x for x in full if x[0] < d])
            if ev:
                res = record_event(db, src, {
                    "external_id": f"{sid}:{d}", "title": ev["text"], "url": f"https://fred.stlouisfed.org/series/{sid}",
                    "published_at": available_at(d, lag), "summary": ev["text"], "raw": f"FRED {sid} {d} = {v}", "kind": "macro",
                    "no_dedupe": True, "preclassified": ev, "details": {"series": sid, "value": v, "obs_date": str(d)}})
                events += res == "new"
    return {"status": "ok", "new": events, "rows": new_rows}
