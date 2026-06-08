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
