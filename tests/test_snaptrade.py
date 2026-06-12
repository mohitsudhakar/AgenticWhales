"""Tests for the SnapTrade brokerage-connect integration (offline)."""

import pytest
from fastapi.testclient import TestClient

import web.server as server
import web.snaptrade_api as snaptrade_api
from web import auth
from agenticwhales.dataflows import snaptrade_client, snaptrade_normalize


@pytest.fixture
def client():
    return TestClient(server.app)


# --- signing (golden values; algorithm copied verbatim from the SnapTrade SDK) ---

def test_signature_golden_post_with_body():
    sig = snaptrade_client.compute_signature(
        "/snapTrade/registerUser?clientId=CID&timestamp=123", "SECRET", {"userId": "u1"})
    assert sig == "86PFzsDBE+ATjGzvtQ+2rv5TUT2DFLdZbn2ZVt6sbds="


def test_signature_golden_get_no_body():
    sig = snaptrade_client.compute_signature(
        "/accounts?clientId=CID&timestamp=123&userId=u1&userSecret=s", "SECRET", None)
    assert sig == "O0+KGC0GKfyqiR0hFGRIEd5BlmqVrIb5/goM+pw09ts="


def test_from_env_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("SNAPTRADE_CLIENT_ID", raising=False)
    monkeypatch.delenv("SNAPTRADE_CONSUMER_KEY", raising=False)
    assert snaptrade_client.from_env() is None


# --- normalization ---

def test_normalize_buy_and_sell_nested_symbol():
    acts = [
        {"type": "BUY", "units": 10, "price": 100, "amount": -1000,
         "trade_date": "2025-01-06T00:00:00Z", "symbol": {"symbol": {"symbol": "AAPL"}}},
        {"type": "SELL", "units": 10, "price": 120, "amount": 1200,
         "trade_date": "2025-02-01", "symbol": {"raw_symbol": "AAPL"}},
        {"type": "DIVIDEND", "units": 0, "price": 0, "amount": 5, "symbol": {"raw_symbol": "AAPL"}},
    ]
    txns = snaptrade_normalize.normalize_activities(acts)
    assert len(txns) == 2  # dividend skipped
    assert txns[0].type == "Buy" and txns[0].symbol == "AAPL" and txns[0].quantity == 10
    assert txns[1].type == "Sell" and txns[1].date == "2025-02-01"


def test_normalize_skips_incomplete():
    assert snaptrade_normalize.normalize_activity({"type": "BUY", "units": 0, "price": 0}) is None


# --- endpoints (config-gated) ---

def test_status_unconfigured(client, monkeypatch):
    monkeypatch.setattr(snaptrade_client, "from_env", lambda: None)
    j = client.get("/api/snaptrade/status").json()
    assert j["configured"] is False and j["connected"] is False


def test_connect_requires_signin(client, monkeypatch):
    # anonymous (tests have no Supabase) -> must sign in
    monkeypatch.setattr(snaptrade_client, "from_env", lambda: object())
    r = client.post("/api/snaptrade/connect", json={})
    assert r.status_code == 401


def test_connect_unconfigured_503(client, monkeypatch):
    from web.auth import get_current_user_id
    server.app.dependency_overrides[get_current_user_id] = lambda: "u-snap-1"
    monkeypatch.setattr(snaptrade_client, "from_env", lambda: None)
    try:
        r = client.post("/api/snaptrade/connect", json={})
        assert r.status_code == 503 and r.json()["configured"] is False
    finally:
        server.app.dependency_overrides.clear()


def test_sync_normalizes_and_audits(client, monkeypatch):
    from web.auth import get_current_user_id
    import agenticwhales.prices as prices

    class FakeClient:
        def get_activities(self, uid, secret, start=None, end=None):
            return [
                {"type": "BUY", "units": 50, "price": 180, "amount": -9000,
                 "trade_date": "2025-01-06", "symbol": {"raw_symbol": "AAPL"}},
                {"type": "SELL", "units": 50, "price": 184, "amount": 9200,
                 "trade_date": "2025-01-08", "symbol": {"raw_symbol": "AAPL"}},
            ]

    monkeypatch.setattr(snaptrade_client, "from_env", lambda: FakeClient())
    monkeypatch.setattr(prices, "fetch_ohlc", lambda *a, **k: None)  # no network
    auth.upsert_snaptrade_user("u-snap-2", "u-snap-2", "secret")
    server.app.dependency_overrides[get_current_user_id] = lambda: "u-snap-2"
    try:
        r = client.post("/api/snaptrade/sync").json()
        assert r["n_trades"] == 1 and r["source"] == "snaptrade"
    finally:
        server.app.dependency_overrides.clear()


class _PagedClient(snaptrade_client.SnapTradeClient):
    """Stubs _request to emulate the per-account, paginated activities API."""

    def __init__(self):
        super().__init__("cid", "ck")
        self.calls = []

    def _request(self, method, path, *, query=None, body=None):
        self.calls.append((method, path, dict(query or {})))
        if path == "/accounts":
            return [{"id": "acct-1"}, {"id": "acct-2"}, {"name": "no-id"}]
        if path == "/accounts/acct-1/activities":
            offset = int(query.get("offset", 0))
            pages = {0: [{"type": "BUY", "n": 1}, {"type": "SELL", "n": 2}],
                     2: [{"type": "BUY", "n": 3}]}
            return {"data": pages.get(offset, []),
                    "pagination": {"offset": offset, "limit": 2, "total": 3}}
        if path == "/accounts/acct-2/activities":
            return [{"type": "BUY", "n": 4}]      # bare-list shape
        raise AssertionError(f"unexpected path {path}")


def test_get_activities_fans_out_per_account_with_pagination():
    """The global GET /activities endpoint is GONE (410) — activities must come
    from /accounts/{id}/activities, paged, across every account."""
    c = _PagedClient()
    acts = c.get_activities("u", "s", start="2023-06-12")
    assert [a["n"] for a in acts] == [1, 2, 3, 4]
    paths = [p for _, p, _ in c.calls]
    assert "/activities" not in paths                       # retired endpoint unused
    assert paths.count("/accounts/acct-1/activities") == 2  # two pages
    # Date filter propagates to the per-account calls.
    assert all(q.get("startDate") == "2023-06-12"
               for _, p, q in c.calls if "activities" in p)


def test_get_activities_tolerates_one_dead_account():
    class _Flaky(_PagedClient):
        def _request(self, method, path, *, query=None, body=None):
            if path == "/accounts/acct-1/activities":
                raise RuntimeError("this account's brokerage is down")
            return super()._request(method, path, query=query, body=body)

    acts = _Flaky().get_activities("u", "s")
    assert [a["n"] for a in acts] == [4]          # the healthy account still syncs


def test_get_activities_raises_when_all_accounts_fail():
    class _Dead(_PagedClient):
        def _request(self, method, path, *, query=None, body=None):
            if "activities" in path:
                raise RuntimeError("410 Client Error: Gone")
            return super()._request(method, path, query=query, body=body)

    with pytest.raises(RuntimeError, match="Gone"):
        _Dead().get_activities("u", "s")


def test_status_reports_sync_window_and_last_sync(client, monkeypatch):
    """Ingestion transparency: connected users see when the last sync ran,
    what date range is on file, and how many rows we hold."""
    from web.auth import get_current_user_id
    import agenticwhales.prices as prices
    uid = "u-snap-status"
    monkeypatch.setattr(snaptrade_client, "from_env", lambda: _PagedClientForSync())
    monkeypatch.setattr(prices, "fetch_ohlc", lambda *a, **k: None)
    auth.upsert_snaptrade_user(uid, uid, "secret")
    server.app.dependency_overrides[get_current_user_id] = lambda: uid
    server.app.dependency_overrides[snaptrade_api.optional_user_id] = lambda: uid
    try:
        # First sync: everything is new.
        r1 = client.post("/api/snaptrade/sync").json()
        assert r1["new_transactions"] == 2 and r1["n_transactions"] == 2
        # Second sync of identical history: pulled again, but 0 genuinely new.
        r2 = client.post("/api/snaptrade/sync").json()
        assert r2["new_transactions"] == 0 and r2["pulled_transactions"] == 2

        s = client.get("/api/snaptrade/status").json()
        assert s["connected"] is True
        sync = s["sync"]
        assert sync["n_transactions"] == 2
        assert sync["history_start"] == "2025-01-06"
        assert sync["history_end"] == "2025-01-08"
        assert sync["last_synced_at"]            # origin-tagged audit found
    finally:
        server.app.dependency_overrides.clear()


class _PagedClientForSync:
    def get_activities(self, uid, secret, start=None, end=None):
        return [
            {"type": "BUY", "units": 50, "price": 180, "amount": -9000,
             "trade_date": "2025-01-06", "symbol": {"raw_symbol": "AAPL"}},
            {"type": "SELL", "units": 50, "price": 184, "amount": 9200,
             "trade_date": "2025-01-08", "symbol": {"raw_symbol": "AAPL"}},
        ]


def test_status_has_no_sync_block_when_disconnected(client, monkeypatch):
    monkeypatch.setattr(snaptrade_client, "from_env", lambda: object())
    j = client.get("/api/snaptrade/status").json()
    assert "sync" not in j


def test_list_all_snaptrade_users_memstore():
    auth.upsert_snaptrade_user("cron-u1", "cron-u1", "s1")
    ids = {r["user_id"] for r in auth.list_all_snaptrade_users()}
    assert "cron-u1" in ids


def test_sync_user_core_audits_and_persists(monkeypatch):
    import agenticwhales.prices as prices

    class FakeClient:
        def get_activities(self, uid, secret, start=None, end=None):
            return [
                {"type": "BUY", "units": 50, "price": 180, "amount": -9000,
                 "trade_date": "2025-01-06", "symbol": {"raw_symbol": "AAPL"}},
                {"type": "SELL", "units": 50, "price": 184, "amount": 9200,
                 "trade_date": "2025-01-08", "symbol": {"raw_symbol": "AAPL"}},
            ]

    monkeypatch.setattr(snaptrade_client, "from_env", lambda: FakeClient())
    monkeypatch.setattr(prices, "fetch_ohlc", lambda *a, **k: None)
    auth.upsert_snaptrade_user("cron-u2", "cron-u2", "secret")
    out = snaptrade_api.sync_user("cron-u2")
    assert out and out["n_trades"] == 1 and out["source"] == "snaptrade"


def test_sync_user_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(snaptrade_client, "from_env", lambda: None)
    assert snaptrade_api.sync_user("whoever") is None


def test_cron_syncs_connected_users(monkeypatch):
    from web.scheduler import RecipeScheduler
    sched = RecipeScheduler()
    sched._is_leader = True
    monkeypatch.setattr(snaptrade_client, "from_env", lambda: object())  # "configured"
    monkeypatch.setattr(auth, "list_all_snaptrade_users",
                        lambda: [{"user_id": "a"}, {"user_id": "b"}])
    called = []
    monkeypatch.setattr(snaptrade_api, "sync_user", lambda uid: called.append(uid) or {"ok": 1})
    sched._run_snaptrade_sync()
    assert set(called) == {"a", "b"}


def test_cron_skips_when_not_leader(monkeypatch):
    from web.scheduler import RecipeScheduler
    sched = RecipeScheduler()
    sched._is_leader = False
    called = []
    monkeypatch.setattr(snaptrade_api, "sync_user", lambda uid: called.append(uid))
    sched._run_snaptrade_sync()
    assert called == []
