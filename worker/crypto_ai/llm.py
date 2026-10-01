"""Optional AI analysis layer via OpenRouter. ANALYSIS ONLY - nothing here places, sizes, or times a trade; every
trading decision stays in the deterministic/ML code in crypto_ai.scalp. This module is used (if at all) to write
human-readable commentary onto news items and post-mortems, and for ad-hoc research questions from the CLI.

Safety rules, all enforced in code, not just by convention:
  * the API key comes ONLY from the OPENROUTER_API_KEY environment variable - never hardcoded, never logged, never
    put in a prompt or a stored row.
  * only $0 models are ever called. `free_models()` asks OpenRouter for its model list and keeps only entries whose
    prompt AND completion price are exactly "0" - `chat()` refuses (raises) to call anything else, so a config
    typo can never route to a paid model.
  * every call is best-effort: timeout, capped retries with backoff on 429/5xx, and any failure (no key, no free
    models available, network down, malformed response) returns None instead of raising - a caller in the learning
    or news pipeline must never crash or block on this.
"""
import os
import time

import requests

API_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT = 20
MAX_RETRIES = 3
RETRY_STATUSES = {429, 500, 502, 503, 504}
_FREE_MODELS_CACHE: dict = {"at": 0.0, "models": None}
FREE_MODELS_TTL_S = 3600


def api_key() -> str | None:
    return os.environ.get("OPENROUTER_API_KEY") or None


def enabled() -> bool:
    return api_key() is not None


def _headers() -> dict:
    return {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/crypto-ai-lab", "X-Title": "Crypto AI Lab (research, paper trading only)"}


def free_models(force: bool = False) -> list[str]:
    """Model ids whose prompt AND completion price are both 0, per OpenRouter's own model list. Cached for
    `FREE_MODELS_TTL_S` so normal use doesn't re-fetch the list on every call. Empty list (not an exception) if the
    key is missing or the request fails."""
    if not enabled():
        return []
    if not force and _FREE_MODELS_CACHE["models"] is not None and time.time() - _FREE_MODELS_CACHE["at"] < FREE_MODELS_TTL_S:
        return _FREE_MODELS_CACHE["models"]
    try:
        r = requests.get(f"{API_URL}/models", headers=_headers(), timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        models = []
        for m in r.json().get("data", []):
            p = m.get("pricing") or {}
            try:
                if float(p.get("prompt", 1)) == 0.0 and float(p.get("completion", 1)) == 0.0:
                    models.append(m["id"])
            except (TypeError, ValueError):
                continue
        _FREE_MODELS_CACHE.update(at=time.time(), models=models)
        return models
    except Exception:
        return _FREE_MODELS_CACHE["models"] or []


def default_free_model() -> str | None:
    """`OPENROUTER_MODEL` if set AND currently free, else the first free model OpenRouter reports, else None."""
    free = free_models()
    if not free:
        return None
    wanted = os.environ.get("OPENROUTER_MODEL")
    return wanted if wanted in free else free[0]


def chat(messages: list[dict], model: str | None = None, max_tokens: int = 400, temperature: float = 0.2) -> str | None:
    """One best-effort chat completion. Returns the reply text, or None on any failure/unavailability. `model`, if
    given, MUST be in `free_models()` - this never silently falls back to a paid one."""
    if not enabled():
        return None
    free = free_models()
    if not free:
        return None
    if model is None:
        model = default_free_model()
    if model not in free:
        return None
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    wait = 1.0
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(f"{API_URL}/chat/completions", headers=_headers(), json=body, timeout=DEFAULT_TIMEOUT)
            if r.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                time.sleep(wait)
                wait *= 2
                continue
            r.raise_for_status()
            choices = r.json().get("choices") or []
            return choices[0]["message"]["content"].strip() if choices else None
        except requests.RequestException:
            if attempt < MAX_RETRIES - 1:
                time.sleep(wait)
                wait *= 2
                continue
            return None
        except (KeyError, IndexError, ValueError, TypeError):
            return None
    return None


# ---------------------------------------------------------------- analysis helpers (never touch trading decisions)
def summarize_news(items: list[dict]) -> str | None:
    """One short paragraph on what a batch of research_events headlines means for BTC/ETH/XRP right now. Informational
    only - never stored as a signal, never read by realtime.py or the scalper."""
    if not items:
        return None
    lines = "\n".join(f"- [{i.get('source', '?')}] {i.get('title', '')}" for i in items[:20])
    return chat([
        {"role": "system", "content": "You are a terse crypto markets analyst. Summarize in at most 4 sentences. No trading advice, no price targets, no certainty you don't have."},
        {"role": "user", "content": f"Recent headlines potentially affecting BTC, ETH, or XRP:\n{lines}\n\nWhat's the takeaway?"},
    ])


def explain_trade(trade: dict, deterministic_lesson: str) -> str | None:
    """A second, LLM-written opinion on why a completed trade worked or failed, ADDED ALONGSIDE (never replacing)
    the deterministic post-mortem from crypto_ai.scalp.learn.post_mortem. Purely for a human reading the dashboard."""
    facts = (f"{trade.get('symbol')} {trade.get('direction')}: entry {trade.get('entry_price')}, exit {trade.get('exit_price')}, "
             f"net {trade.get('net_pnl_pct')}%, exit reason {trade.get('exit_reason')}, setup {trade.get('setup')}, regime {trade.get('regime')}. "
             f"Deterministic analysis already says: \"{deterministic_lesson}\"")
    return chat([
        {"role": "system", "content": "You add one extra sentence of colour to an already-computed trade post-mortem. Do not repeat the given analysis, do not suggest a rule change, do not give financial advice."},
        {"role": "user", "content": facts},
    ], max_tokens=120)


def research_question(question: str, context: str = "") -> str | None:
    """Ad-hoc research assistant for the CLI (`crypto_ai.cli ask`). Never called from the trading or learning
    pipeline - a human-initiated, one-off query only."""
    msgs = [{"role": "system", "content": "You are a crypto markets research assistant for a paper-trading research project. Be concise and say when you're unsure. Never give financial advice or price predictions presented as fact."}]
    if context:
        msgs.append({"role": "user", "content": f"Context:\n{context}"})
    msgs.append({"role": "user", "content": question})
    return chat(msgs, max_tokens=600)
