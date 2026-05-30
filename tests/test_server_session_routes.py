"""HTTP coverage for web/server.py session/batch lifecycle + signal routes.
Runners are replaced with light fakes so no LLM/graph runs; the background
stale-running sweep loop is driven directly with a patched sleep.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from fastapi.testclient import TestClient

from web import auth, server


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    server._runners.clear()
    server._batch_runners.clear()
    yield
    auth._reset_memstore_for_tests()
    server._runners.clear()
    server._batch_runners.clear()


@pytest.fixture
def uc():
    server.app.dependency_overrides[server.get_current_user_id] = lambda: "u-test"
    c = TestClient(server.app)
    c.follow_redirects = False
    yield c
    server.app.dependency_overrides.pop(server.get_current_user_id, None)


class _FakeRunner:
    def __init__(self, session=None, loop=None, **kw):
        self.session = session or {}
        self.started = False
        self._cancel_ok = True

    def start(self):
        self.started = True

    def cancel(self):
        return self._cancel_ok

    def snapshot(self):
        return {"snapshot": True, **self.session}


class _FakeBatchRunner(_FakeRunner):
    def __init__(self, batch=None, loop=None, **kw):
        super().__init__(batch, loop, **kw)
        self.batch = batch or {}


def _session_payload(**over):
    p = {"ticker": "AAPL", "analysis_date": "2024-01-02",
         "llm_provider": "google", "quick_think_llm": "q", "deep_think_llm": "d",
         "analysts": ["market"]}
    p.update(over)
    return p


def _batch_payload(**over):
    p = {"tickers": ["AAPL", "NVDA"], "analysis_date": "2024-01-02",
         "llm_provider": "google", "quick_think_llm": "q", "deep_think_llm": "d",
         "analysts": ["market"]}
    p.update(over)
    return p


# ===========================================================================
# create_session (+ cache branch)
# ===========================================================================

def test_create_session_starts_runner(uc, monkeypatch):
    monkeypatch.setattr(server, "SessionRunner", _FakeRunner)
    r = uc.post("/api/sessions", json=_session_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "AAPL" and body["id"]
    assert body["id"] in server._runners


def test_create_session_returns_cached(uc, monkeypatch):
    monkeypatch.setattr(server, "SessionRunner", _FakeRunner)
    monkeypatch.setattr(server, "CACHE_ENABLED", True)
    cached = {"id": "old", "ticker": "AAPL", "analysis_date": "2024-01-02",
              "status": "completed", "created_at": 1.0}
    monkeypatch.setattr(server.auth, "find_cached_session", lambda **k: cached)
    r = uc.post("/api/sessions", json=_session_payload())
    assert r.status_code == 200 and r.json()["cached"] is True


# ===========================================================================
# delete_session / cancel_session
# ===========================================================================

def test_delete_session_from_storage(uc):
    server.storage.save({"id": "s1", "user_id": "u-test", "ticker": "AAPL",
                         "analysis_date": "2024-01-02", "status": "completed",
                         "created_at": 1.0})
    assert uc.delete("/api/sessions/s1").json() == {"deleted": True}
    assert server.storage.load("s1") is None


def test_delete_session_running_conflicts(uc):
    server._runners["s2"] = _FakeRunner({"id": "s2", "user_id": "u-test",
                                         "status": "running"})
    assert uc.delete("/api/sessions/s2").status_code == 409


def test_delete_session_wrong_owner_404(uc):
    server.storage.save({"id": "s3", "user_id": "someone-else", "ticker": "X",
                         "analysis_date": "2024-01-02", "status": "completed",
                         "created_at": 1.0})
    assert uc.delete("/api/sessions/s3").status_code == 404


def test_cancel_session_no_runner_409(uc):
    server.storage.save({"id": "s4", "user_id": "u-test", "ticker": "X",
                         "analysis_date": "2024-01-02", "status": "completed",
                         "created_at": 1.0})
    assert uc.post("/api/sessions/s4/cancel").status_code == 409


def test_cancel_session_success(uc):
    server._runners["s5"] = _FakeRunner({"id": "s5", "user_id": "u-test",
                                        "status": "running"})
    r = uc.post("/api/sessions/s5/cancel")
    assert r.status_code == 200 and r.json()["snapshot"] is True


def test_cancel_session_not_cancellable_409(uc):
    runner = _FakeRunner({"id": "s6", "user_id": "u-test", "status": "running"})
    runner._cancel_ok = False
    server._runners["s6"] = runner
    assert uc.post("/api/sessions/s6/cancel").status_code == 409


# ===========================================================================
# create_batch / delete_batch / cancel_batch
# ===========================================================================

def test_create_batch_starts_runner(uc, monkeypatch):
    monkeypatch.setattr(server, "BatchRunner", _FakeBatchRunner)
    r = uc.post("/api/batches", json=_batch_payload())
    assert r.status_code == 200
    assert r.json()["ticker_count"] == 2
    assert r.json()["id"] in server._batch_runners


def test_delete_batch_running_conflicts(uc):
    server._batch_runners["b1"] = _FakeBatchRunner({"id": "b1", "user_id": "u-test",
                                                    "status": "running"})
    assert uc.delete("/api/batches/b1").status_code == 409


def test_delete_batch_from_storage(uc):
    server.batch_storage.save({"id": "b2", "user_id": "u-test",
                              "analysis_date": "2024-01-02", "status": "completed",
                              "created_at": 1.0, "items": []})
    assert uc.delete("/api/batches/b2").json() == {"deleted": True}


def test_cancel_batch_success(uc):
    server._batch_runners["b3"] = _FakeBatchRunner({"id": "b3", "user_id": "u-test",
                                                   "status": "running"})
    r = uc.post("/api/batches/b3/cancel")
    assert r.status_code == 200 and r.json()["snapshot"] is True


def test_cancel_batch_no_runner_409(uc):
    server.batch_storage.save({"id": "b4", "user_id": "u-test",
                              "analysis_date": "2024-01-02", "status": "completed",
                              "created_at": 1.0, "items": []})
    assert uc.post("/api/batches/b4/cancel").status_code == 409


# ===========================================================================
# websocket streams
# ===========================================================================

def test_stream_session_not_found_closes(uc):
    with pytest.raises(Exception):
        with uc.websocket_connect("/api/sessions/nope/stream") as ws:
            ws.receive_json()


def test_stream_session_from_storage(uc):
    # anonymous-owned (offline auth resolves websocket to ANONYMOUS_USER_ID)
    server.storage.save({"id": "s7", "user_id": auth.ANONYMOUS_USER_ID,
                         "ticker": "AAPL", "analysis_date": "2024-01-02",
                         "status": "completed", "created_at": 1.0})
    with uc.websocket_connect("/api/sessions/s7/stream") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "session" and msg["session"]["id"] == "s7"


def test_stream_batch_from_storage(uc):
    server.batch_storage.save({"id": "b7", "user_id": auth.ANONYMOUS_USER_ID,
                              "analysis_date": "2024-01-02", "status": "completed",
                              "created_at": 1.0, "items": []})
    with uc.websocket_connect("/api/batches/b7/stream") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "batch"


# ===========================================================================
# signals: x-recs / congress / transactions
# ===========================================================================

def test_post_x_recs_empty_tweets(uc, monkeypatch):
    import agenticwhales.dataflows.x_trades as xt
    monkeypatch.setattr(xt, "fetch_user_tweets", lambda h, max_results=30: [])
    r = uc.post("/api/signals/x-recs", json={"handle": "@trader"})
    assert r.status_code == 200 and r.json() == {"handle": "trader",
                                                 "tweets": [], "recommendations": []}


def test_post_x_recs_with_recs(uc, monkeypatch):
    import agenticwhales.dataflows.x_trades as xt
    monkeypatch.setattr(xt, "fetch_user_tweets", lambda h, max_results=30: ["t1"])
    monkeypatch.setattr(xt, "extract_trade_recs",
                        lambda h, tweets, provider, model: [{"ticker": "AAPL"}])
    r = uc.post("/api/signals/x-recs", json={"handle": "trader"})
    assert r.json()["recommendations"] == [{"ticker": "AAPL"}]


def test_post_x_recs_fetch_error_502(uc, monkeypatch):
    import agenticwhales.dataflows.x_trades as xt
    def _boom(h, max_results=30):
        raise xt.XTradesError("rate limited")
    monkeypatch.setattr(xt, "fetch_user_tweets", _boom)
    assert uc.post("/api/signals/x-recs", json={"handle": "x"}).status_code == 502


def test_post_congress_counts_buys_sells(uc, monkeypatch):
    import agenticwhales.dataflows.congress_trades as ct
    records = [{"transaction": "Purchase"}, {"transaction": "Sale (Full)"},
               {"transaction": "Buy"}]
    monkeypatch.setattr(ct, "fetch_congress_trades", lambda t, limit=50: records)
    r = uc.post("/api/signals/congress", json={"ticker": "aapl"})
    body = r.json()
    assert body["ticker"] == "AAPL" and body["buys"] == 2 and body["sells"] == 1


def test_post_congress_error_502(uc, monkeypatch):
    import agenticwhales.dataflows.congress_trades as ct
    def _boom(t, limit=50):
        raise ct.CongressTradesError("down")
    monkeypatch.setattr(ct, "fetch_congress_trades", _boom)
    assert uc.post("/api/signals/congress", json={"ticker": "AAPL"}).status_code == 502


def test_post_transactions_parses_and_persists(uc, monkeypatch):
    import agenticwhales.transactions as txmod
    from types import SimpleNamespace

    txns = [SimpleNamespace(date="2024-01-02", type="Buy", symbol="AAPL",
                            description="", quantity=10, price=100.0, amount=-1000.0)]
    monkeypatch.setattr(txmod, "parse_transactions_csv", lambda text: txns)

    class _Result:
        def model_dump(self):
            return {"metrics": {"trades": 1}}
    monkeypatch.setattr(txmod, "analyze_transactions",
                        lambda t, run_llm, provider, model: _Result())

    files = {"file": ("trades.csv", io.BytesIO(b"col\n1"), "text/csv")}
    r = uc.post("/api/signals/transactions", files=files, data={"run_llm": "false"})
    assert r.status_code == 200
    body = r.json()
    assert body["saved_count"] == 1 and body["persisted"] is True
    assert "batch_id" in body


def test_post_transactions_empty_400(uc, monkeypatch):
    import agenticwhales.transactions as txmod
    monkeypatch.setattr(txmod, "parse_transactions_csv", lambda text: [])
    files = {"file": ("empty.csv", io.BytesIO(b""), "text/csv")}
    assert uc.post("/api/signals/transactions", files=files).status_code == 400


# ===========================================================================
# background stale-running sweep loop + lifespan
# ===========================================================================

def test_stale_running_sweep_loop_boot_then_cancel(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(server.auth, "delete_stuck_running_sessions",
                        lambda **k: calls.__setitem__("n", calls["n"] + 1) or 1)

    sleeps = {"n": 0}

    async def _sleep(_secs):
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            raise asyncio.CancelledError
        return None
    monkeypatch.setattr(server.asyncio, "sleep", _sleep)
    asyncio.run(server._stale_running_sweep_loop())
    # boot sweep + one hourly sweep
    assert calls["n"] >= 2


def test_stale_running_sweep_loop_boot_error_swallowed(monkeypatch):
    def _boom(**k):
        raise RuntimeError("db down")
    monkeypatch.setattr(server.auth, "delete_stuck_running_sessions", _boom)

    async def _sleep(_secs):
        raise asyncio.CancelledError
    monkeypatch.setattr(server.asyncio, "sleep", _sleep)
    asyncio.run(server._stale_running_sweep_loop())  # no raise


def test_lifespan_runs_sweep(monkeypatch):
    monkeypatch.setattr(server.auth, "delete_stuck_running_sessions", lambda **k: 0)
    # entering the TestClient context manager triggers the lifespan handler
    with TestClient(server.app) as c:
        assert c.get("/healthz").status_code == 200
