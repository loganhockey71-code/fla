"""cli.py's own logic, not the database-backed commands (those are covered by test_e2e.py)."""
from crypto_ai import cli


def test_reconnect_retries_with_backoff_instead_of_raising(monkeypatch):
    """A DNS/network blip while reconnecting must never kill an unattended loop/watch process."""
    calls, sleeps = [], []
    attempts = iter([ConnectionError("dns blip"), ConnectionError("dns blip"), "connected"])

    def fake_db():
        r = next(attempts)
        calls.append(r)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(cli, "DB", fake_db)
    monkeypatch.setattr(cli.time, "sleep", lambda s: sleeps.append(s))
    assert cli._reconnect() == "connected"
    assert len(calls) == 3
    assert sleeps == [5, 10]                              # backoff doubles each failed attempt


def test_reconnect_backoff_caps_at_max_wait(monkeypatch):
    calls = {"n": 0}

    def fake_db():
        calls["n"] += 1
        if calls["n"] < 6:
            raise ConnectionError()
        return "connected"

    sleeps = []
    monkeypatch.setattr(cli, "DB", fake_db)
    monkeypatch.setattr(cli.time, "sleep", lambda s: sleeps.append(s))
    assert cli._reconnect(max_wait=20) == "connected"
    assert max(sleeps) <= 20
