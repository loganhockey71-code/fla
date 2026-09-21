"""Congress.gov API: crypto-relevant bills, actions, amendments, committees, hearings and House votes.
Key from CONGRESS_API_KEY (server-side only). A bill produces an event only when its state CHANGES."""
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone

from ..http import SourceError, get_json
from .common import record_event

BASE = "https://api.congress.gov/v3"
CONGRESS = 119

CRYPTO_TERMS = (r"crypto|digital asset|digital commodit|stablecoin|blockchain|bitcoin|ethereum|\bxrp\b|virtual currenc|distributed ledger|"
                r"central bank digital currency|\bcbdc\b|tokeniz|market structure|financial innovation|payment stablecoin|genius act|digital asset market clarity|clarity act for digital")
REGULATOR_TERMS = r"securities and exchange commission|commodity futures trading commission|\bcftc\b|\bsec\b|exchange act|securities exchange"
FRIENDLY = r"clarity|innovation|framework|market structure|genius|payment stablecoin|anti-cbdc|guidance|certainty|safe harbor|tax (relief|clarity)"
HOSTILE = r"\bban\b|prohibit|restrict|moratorium|penalt|crack ?down|illicit"

STAGE_PROB = {"introduced": 0.03, "committee": 0.08, "passed_one_chamber": 0.35, "passed_both": 0.75, "to_president": 0.92, "law": 1.0, "failed": 0.0}
STAGE_BUMP = {"introduced": 0, "committee": 6, "passed_one_chamber": 22, "passed_both": 30, "to_president": 34, "law": 40, "failed": 12}


def _key() -> str:
    k = os.environ.get("CONGRESS_API_KEY")
    if not k:
        raise SourceError("CONGRESS_API_KEY is not set")
    return k


def api(path: str, ttl: int = 240, **params):
    return get_json(f"{BASE}/{path}", {"api_key": _key(), "format": "json", **params}, ttl=ttl)


def relevance(text: str) -> int:
    t = text.lower()
    if re.search(CRYPTO_TERMS, t):
        return 90
    if re.search(REGULATOR_TERMS, t):
        return 50
    return 0


def stage_from_actions(actions: list[dict]) -> str:
    texts = " | ".join(a.get("text", "") for a in actions).lower()
    if "became public law" in texts or "signed by president" in texts:
        return "law"
    if re.search(r"vetoed|failed of passage|motion to reconsider.*failed", texts):
        return "failed"
    if "presented to president" in texts:
        return "to_president"
    house = bool(re.search(r"passed house|passed/agreed to in house|agreed to in house|house passed", texts))
    senate = bool(re.search(r"passed senate|passed/agreed to in senate|agreed to in senate|senate passed", texts))
    if house and senate:
        return "passed_both"
    if house or senate:
        return "passed_one_chamber"
    if re.search(r"referred to|committee|hearings? held|markup|ordered to be reported|reported by", texts):
        return "committee"
    return "introduced"


def _state_hash(latest_date, latest_text, n_actions, n_amend, committees, votes) -> str:
    blob = json.dumps([str(latest_date), latest_text, n_actions, n_amend, sorted(committees), sorted(v["id"] for v in votes)])
    return hashlib.sha1(blob.encode()).hexdigest()


def _bill_event(item: dict, stage: str, kind: str, headline: str) -> dict:
    title = item["title"]
    sentiment = "neutral"
    advancing = stage in ("passed_one_chamber", "passed_both", "to_president", "law")
    if re.search(FRIENDLY, title.lower()) and not re.search(HOSTILE, title.lower()):
        sentiment = "positive" if advancing or kind == "vote_passed" else "neutral"
    elif re.search(HOSTILE, title.lower()):
        sentiment = "negative" if advancing or kind == "vote_passed" else "neutral"
    if stage == "failed" or kind == "vote_failed":
        sentiment = "negative" if sentiment == "positive" else "neutral"
    imp = min(100, 45 + STAGE_BUMP.get(stage, 0) + (10 if item["crypto_relevance"] >= 90 else 0) + (6 if kind.startswith("vote") else 0))
    return {"category": "legislation", "coins": ["BTC", "ETH", "XRP"], "named": [], "sentiment": sentiment, "importance": imp,
            "event_probability": STAGE_PROB.get(stage, 0.05), "event_probability_source": "stage_estimate", "headline": headline}


def _emit(db, src, item, stage, kind, headline, ext_suffix, published):
    ev = _bill_event(item, stage, kind, headline)
    return record_event(db, src, {
        "external_id": f"{item['external_id']}:{ext_suffix}", "title": f"{headline}: {item['title'][:200]}", "url": item["url"],
        "published_at": published, "summary": f"{item['title']} ({item['bill_type'].upper()} {item['number']}, {CONGRESS}th Congress). "
                                                 f"Stage: {stage.replace('_', ' ')}. Latest action: {item['latest_action_text'] or 'n/a'}",
        "raw": f"congress.gov {item['external_id']} hash={item['state_hash'][:10]}", "kind": "legislation", "no_dedupe": True,
        "preclassified": ev, "event_probability": ev["event_probability"], "event_probability_source": ev["event_probability_source"],
        "details": {"bill": item["external_id"], "stage": stage, "change": kind}})


def _fetch_bill(bt: str, num: str) -> dict:
    p = f"bill/{CONGRESS}/{bt}/{num}"
    d = api(p)["bill"]
    actions = api(f"{p}/actions", limit=250).get("actions", [])
    committees = [c.get("name", "") for c in api(f"{p}/committees").get("committees", [])]
    amends = api(f"{p}/amendments", limit=50).get("amendments", [])
    return {"detail": d, "actions": actions, "committees": committees, "amendments": amends}


def _date(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).date() if s else None


def track_bill(db, src, row: dict, since_new: bool, now) -> str | None:
    bt, num = row["type"].lower(), str(row["number"])
    ext = f"{CONGRESS}-{bt}-{num}"
    existing = db.one("select * from legislative_items where external_id=%s", [ext])
    upd = row.get("updateDate")
    upd_dt = datetime.fromisoformat(upd.replace("Z", "+00:00")) if upd and "T" in upd else (datetime.fromisoformat(upd + "T00:00:00+00:00") if upd else None)
    if existing and upd_dt and existing["source_updated_at"] and existing["source_updated_at"] >= upd_dt:
        db.run("update legislative_items set last_checked_at=%s where id=%s", [now, existing["id"]])
        return None
    b = _fetch_bill(bt, num)
    d, actions = b["detail"], b["actions"]
    latest = d.get("latestAction") or {}
    votes = existing["house_votes"] if existing else []
    stage = stage_from_actions(actions)
    item = {
        "item_type": "bill", "external_id": ext, "congress": CONGRESS, "bill_type": bt, "number": num, "title": d.get("title", "")[:600],
        "sponsor": ((d.get("sponsors") or [{}])[0].get("fullName")), "origin_chamber": d.get("originChamber"),
        "introduced_date": d.get("introducedDate"), "policy_area": (d.get("policyArea") or {}).get("name"),
        "latest_action_date": latest.get("actionDate"), "latest_action_text": latest.get("text"), "status_stage": stage,
        "committees": json.dumps(b["committees"]), "actions": json.dumps([{"date": a.get("actionDate"), "text": a.get("text", "")[:300]} for a in actions[:40]]),
        "amendments": json.dumps([{"n": a.get("number"), "type": a.get("type"), "desc": (a.get("description") or "")[:200]} for a in b["amendments"][:30]]),
        "house_votes": json.dumps(votes), "crypto_relevance": relevance(d.get("title", "")),
        "url": f"https://www.congress.gov/bill/{CONGRESS}th-congress/{'house' if bt.startswith('h') else 'senate'}-bill/{num}",
        "source_updated_at": upd_dt,
    }
    item["state_hash"] = _state_hash(latest.get("actionDate"), latest.get("text"), len(actions), len(b["amendments"]), b["committees"], votes)
    cols = list(item)
    if existing is None:
        db.run(f"insert into legislative_items ({','.join(cols)}) values ({','.join(['%s'] * len(cols))})", [item[c] for c in cols])
        if since_new:
            return _emit(db, src, item, stage, "new", "New tracked bill", item["state_hash"][:8], now)
        return None
    if existing["state_hash"] == item["state_hash"]:                # updateDate moved but nothing we track changed
        db.run("update legislative_items set source_updated_at=%s, last_checked_at=%s where id=%s", [upd_dt, now, existing["id"]])
        return None
    sets = ",".join(f"{c}=%s" for c in cols)
    db.run(f"update legislative_items set {sets}, last_changed_at=%s, last_checked_at=%s where id=%s", [item[c] for c in cols] + [now, now, existing["id"]])
    what = "Bill advanced" if STAGE_BUMP[stage] > STAGE_BUMP.get(existing["status_stage"], 0) else "Bill updated"
    return _emit(db, src, item, stage, "change", what, item["state_hash"][:8], now)


def poll(db, src: dict) -> dict:
    now = datetime.now(timezone.utc)
    first = src["last_success_at"] is None
    since = (src["last_success_at"] or now - timedelta(days=30)) - timedelta(hours=1)
    frm = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    new = changed = 0

    # 1. recently updated bills -> crypto-relevant ones (plus anything we already track)
    tracked = {r["external_id"] for r in db.all("select external_id from legislative_items where item_type='bill'")}
    cands = []
    for offset in range(0, 1000, 250):
        page = api(f"bill/{CONGRESS}", fromDateTime=frm, limit=250, offset=offset, sort="updateDate desc").get("bills", [])
        for r in page:
            ext = f"{CONGRESS}-{r['type'].lower()}-{r['number']}"
            if ext in tracked or relevance(r.get("title", "")) > 0:
                cands.append(r)
        if len(page) < 250:
            break
    for r in cands:
        res = track_bill(db, src, r, since_new=True, now=now)
        if res == "new":
            new += 1
    # 2. House roll-call votes on tracked bills
    votes = api(f"house-vote/{CONGRESS}", limit=100, fromDateTime=frm).get("houseRollCallVotes", [])
    for v in votes:
        if not v.get("legislationType") or not v.get("legislationNumber"):
            continue
        ext = f"{CONGRESS}-{v['legislationType'].lower()}-{v['legislationNumber']}"
        row = db.one("select * from legislative_items where external_id=%s", [ext])
        if not row:
            continue
        vid = str(v["identifier"])
        vs = row["house_votes"] or []
        if any(x["id"] == vid for x in vs):
            continue
        vs.append({"id": vid, "roll": v.get("rollCallNumber"), "result": v.get("result"), "date": v.get("startDate")})
        passed = "pass" in (v.get("result") or "").lower() or "agreed" in (v.get("result") or "").lower()
        item = {**row, "state_hash": _state_hash(row["latest_action_date"], row["latest_action_text"], 0, 0, row["committees"], vs)}
        db.run("update legislative_items set house_votes=%s, state_hash=%s, last_changed_at=%s where id=%s",
               [json.dumps(vs), item["state_hash"], now, row["id"]])
        if _emit(db, src, item, "passed_one_chamber" if passed else "failed", "vote_passed" if passed else "vote_failed",
                 f"House vote: {v.get('result')}", f"vote{vid}", now) == "new":
            new += 1
    # 3. hearings
    for h in api(f"hearing/{CONGRESS}", limit=60, fromDateTime=frm).get("hearings", []):
        ext = f"hearing-{h['congress']}-{h['chamber'].lower()}-{h['jacketNumber']}"
        if ext in tracked or db.one("select 1 x from legislative_items where external_id=%s", [ext]):
            continue
        d = api(f"hearing/{h['congress']}/{h['chamber'].lower()}/{h['jacketNumber']}").get("hearing", {})
        title = d.get("title", "") or ""
        rel = relevance(title)
        db.run("""insert into legislative_items (item_type, external_id, congress, title, origin_chamber, latest_action_date, state_hash,
                  crypto_relevance, committees, url) values ('hearing',%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
               [ext, h["congress"], title[:600], h["chamber"], (d.get("dates") or [{}])[0].get("date"), hashlib.sha1(title.encode()).hexdigest(), rel,
                json.dumps([c.get("name") for c in d.get("committees", [])]), f"https://www.congress.gov/event/hearing/{h['jacketNumber']}"])
        if rel:
            hi = {"external_id": ext, "title": title, "url": f"https://www.congress.gov/", "crypto_relevance": rel, "bill_type": "hearing",
                  "number": str(h["jacketNumber"]), "latest_action_text": "Congressional hearing", "state_hash": ext}
            ev = {"category": "legislation", "coins": ["BTC", "ETH", "XRP"], "named": [], "sentiment": "neutral", "importance": 52 if rel >= 90 else 40,
                  "event_probability": 1.0, "event_probability_source": "published_fact"}
            if record_event(db, src, {"external_id": ext, "title": f"Congressional hearing: {title[:200]}", "url": hi["url"], "published_at": now,
                                      "summary": title, "raw": f"congress.gov hearing {ext}", "kind": "legislation", "no_dedupe": True,
                                      "preclassified": ev, "event_probability": 1.0, "event_probability_source": "published_fact"}) == "new":
                new += 1
    return {"status": "ok", "new": new, "candidates": len(cands), "first_run": first}
