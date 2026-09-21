"""Shared HTTP helper for research sources: secrets stay server-side, are redacted from every error/log line,
and GET responses are cached on disk-free memory for a TTL to respect rate limits."""
import os
import re
import time

import requests

_S = requests.Session()
_S.headers["User-Agent"] = "crypto-ai-paper-lab/1.0 (research; read-only)"
_CACHE: dict[str, tuple[float, object]] = {}
SECRET_ENV = ("FRED_API_KEY", "CONGRESS_API_KEY", "DATABASE_URL")


def redact(text: str) -> str:
    text = str(text)
    for name in SECRET_ENV:
        v = os.environ.get(name)
        if v:
            text = text.replace(v, f"<{name}>")
    return re.sub(r"(api_key|apikey|key)=[^&\s]+", r"\1=<redacted>", text, flags=re.I)


class SourceError(RuntimeError):
    """An error message that is guaranteed free of secrets."""


def get_json(url: str, params: dict | None = None, ttl: int = 0, timeout: int = 20, retries: int = 3):
    ck = url + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()) if "key" not in k.lower())
    hit = _CACHE.get(ck)
    if ttl and hit and time.time() - hit[0] < ttl:
        return hit[1]
    last = None
    for i in range(retries):
        try:
            r = _S.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(2 * (i + 1))
                last = "rate limited (429)"
                continue
            r.raise_for_status()
            data = r.json()
            if ttl:
                _CACHE[ck] = (time.time(), data)
            return data
        except requests.HTTPError as e:
            raise SourceError(f"HTTP {e.response.status_code} from {url.split('?')[0]}") from None
        except (requests.RequestException, ValueError) as e:
            last = redact(type(e).__name__)
            time.sleep(1 + i)
    raise SourceError(f"{last or 'failed'} from {url.split('?')[0]}")


def get_text(url: str, timeout: int = 20) -> bytes:
    try:
        r = _S.get(url, timeout=timeout)
        r.raise_for_status()
        return r.content
    except requests.HTTPError as e:
        raise SourceError(f"HTTP {e.response.status_code} from {url}") from None
    except requests.RequestException as e:
        raise SourceError(f"{type(e).__name__} from {url}") from None


def get_conditional(url: str, etag: str | None, last_modified: str | None, timeout: int = 20):
    """Conditional GET so unchanged feeds cost the publisher (and our quota) almost nothing.
    Returns (status, body|None, etag, last_modified). status 304 => nothing new."""
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        r = _S.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise SourceError(f"{type(e).__name__} from {url}") from None
    if r.status_code == 304:
        return 304, None, etag, last_modified
    if r.status_code >= 400:
        raise SourceError(f"HTTP {r.status_code} from {url}")
    return r.status_code, r.content, r.headers.get("ETag"), r.headers.get("Last-Modified")
