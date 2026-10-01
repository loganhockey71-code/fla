"""cli.py's own logic, not the database-backed commands (those are covered by test_e2e.py)."""
import pytest

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


def test_local_scalp_loop_polls_news_collects_prices_and_runs_the_scalper_and_survives_failures(monkeypatch):
    """`cli scalp` is the 'guaranteed cadence' runner. It used to run scalper passes ONLY: the news layer starved and the dashboard prices went stale."""
    from crypto_ai import cli

    calls = []

    class FakeDB:
        def settings(self):
            from crypto_ai.config import DEFAULT_SETTINGS
            return dict(DEFAULT_SETTINGS)

    def news_poll(db, **k):
        calls.append("news")
        if calls.count("news") == 1:
            raise RuntimeError("feed down")                                  # the first poll fails; the scalper must carry on regardless

    monkeypatch.setattr(cli.registry, "run_due", news_poll)
    monkeypatch.setattr(cli, "collect", lambda db: calls.append("collect"))
    monkeypatch.setattr(cli, "scalp_cycle", lambda db, cfg: calls.append("scalp") or {"opened": [], "closed": [], "error": None})
    monkeypatch.setattr(cli.scalp_learn, "run_learning", lambda db, cfg: calls.append("learn"))
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    with pytest.raises(KeyboardInterrupt):
        cli.scalp_forever(FakeDB(), every=20, collect_every_s=10_000)
    assert calls.count("scalp") == 3 and calls.count("learn") == 3          # a failing news poll in loop 1 did not stop the scalper pass
    assert calls.count("news") == 3 and calls.count("collect") == 1          # prices are refreshed once per collect interval, not every pass
    assert sleeps == [20, 20, 20]
