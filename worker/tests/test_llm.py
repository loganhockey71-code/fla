"""OpenRouter integration: free-model enforcement, retry/timeout/graceful-failure, and that trading code never
depends on it. All HTTP is mocked - no network calls in this test file."""
import requests
import pytest

from crypto_ai import llm


class FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


MODELS_PAYLOAD = {"data": [
    {"id": "free/model-a", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "paid/model-b", "pricing": {"prompt": "0.000002", "completion": "0.000004"}},
    {"id": "free/model-c", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "broken/model-d", "pricing": {}},
]}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    llm._FREE_MODELS_CACHE.update(at=0.0, models=None)
    yield
    llm._FREE_MODELS_CACHE.update(at=0.0, models=None)


def test_disabled_without_an_api_key_and_never_calls_the_network(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(requests, "get", lambda *a, **k: called.append(1) or FakeResp())
    monkeypatch.setattr(requests, "post", lambda *a, **k: called.append(1) or FakeResp())
    assert llm.enabled() is False
    assert llm.free_models() == []
    assert llm.chat([{"role": "user", "content": "hi"}]) is None
    assert not called


def test_only_zero_priced_models_are_ever_considered_free(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, MODELS_PAYLOAD))
    free = llm.free_models()
    assert free == ["free/model-a", "free/model-c"]
    assert "paid/model-b" not in free and "broken/model-d" not in free


def test_chat_refuses_a_model_not_on_the_free_list(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, MODELS_PAYLOAD))
    posted = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: posted.append(1) or FakeResp(200, {"choices": [{"message": {"content": "x"}}]}))
    assert llm.chat([{"role": "user", "content": "hi"}], model="paid/model-b") is None
    assert not posted


def test_chat_succeeds_on_a_free_model_and_strips_the_reply(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, MODELS_PAYLOAD))
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(200, {"choices": [{"message": {"content": "  hello there  "}}]}))
    out = llm.chat([{"role": "user", "content": "hi"}], model="free/model-a")
    assert out == "hello there"


def test_retries_on_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, MODELS_PAYLOAD))
    calls = {"n": 0}

    def post(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            return FakeResp(503)
        return FakeResp(200, {"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    assert llm.chat([{"role": "user", "content": "hi"}], model="free/model-a") == "ok"
    assert calls["n"] == 3


def test_exhausted_retries_return_none_never_raise(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, MODELS_PAYLOAD))
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(503))
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    assert llm.chat([{"role": "user", "content": "hi"}], model="free/model-a") is None


def test_network_exception_is_swallowed(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, MODELS_PAYLOAD))

    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(requests, "post", boom)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    assert llm.chat([{"role": "user", "content": "hi"}], model="free/model-a") is None


def test_malformed_response_returns_none(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, MODELS_PAYLOAD))
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(200, {"unexpected": True}))
    assert llm.chat([{"role": "user", "content": "hi"}], model="free/model-a") is None


def test_default_free_model_prefers_the_configured_one_when_it_is_actually_free(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, MODELS_PAYLOAD))
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    assert llm.default_free_model() == "free/model-a"
    monkeypatch.setenv("OPENROUTER_MODEL", "free/model-c")
    assert llm.default_free_model() == "free/model-c"
    monkeypatch.setenv("OPENROUTER_MODEL", "paid/model-b")   # configured but not free: falls back, never uses the paid one
    assert llm.default_free_model() == "free/model-a"


def test_summarize_news_and_explain_trade_are_best_effort_and_never_required(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert llm.summarize_news([{"title": "x", "source": "y"}]) is None
    assert llm.explain_trade({"symbol": "BTC", "direction": "long"}, "deterministic lesson") is None
    assert llm.summarize_news([]) is None


def test_api_key_is_never_placed_in_a_prompt_or_returned_value(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, MODELS_PAYLOAD))
    seen = {}

    def post(url, headers=None, json=None, timeout=None):
        seen["headers"], seen["json"] = headers, json
        return FakeResp(200, {"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr(requests, "post", post)
    llm.chat([{"role": "user", "content": "hi"}], model="free/model-a")
    assert "test-key-not-real" not in str(seen["json"])
    assert seen["headers"]["Authorization"] == "Bearer test-key-not-real"   # key goes ONLY in the auth header
