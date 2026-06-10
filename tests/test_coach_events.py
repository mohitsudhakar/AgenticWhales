"""Phase 0 measurement layer: events endpoint, audit-origin tagging,
broker_connected-on-first-sync, admin funnel stats, /metrics route."""

import pytest
from fastapi.testclient import TestClient

import web.coach_api as coach_api
import web.server as server
import web.snaptrade_api as snaptrade_api
from agenticwhales.dataflows import snaptrade_client
from web import admin, auth


@pytest.fixture
def client():
    return TestClient(server.app)


def _signin(uid: str):
    server.app.dependency_overrides[coach_api.optional_user_id] = lambda: uid


def _events_for(uid: str, action: str):
    return [r for r in auth.list_audit(actor=uid, action=action, limit=100)]


# --------------------------------------------------------------------------- #
# /api/coach/events
# --------------------------------------------------------------------------- #

def test_unknown_event_rejected(client):
    r = client.post("/api/coach/events", json={"event": "totally_made_up"})
    assert r.status_code == 400


def test_guest_event_counts_but_writes_nothing_durable(client):
    before = len(auth.list_audit(action="share_card_exported", limit=1000))
    r = client.post("/api/coach/events", json={"event": "share_card_exported"})
    assert r.status_code == 200 and r.json()["ok"]
    after = len(auth.list_audit(action="share_card_exported", limit=1000))
    assert after == before  # anonymous -> metrics only, no audit_log growth


def test_signed_in_event_lands_in_audit_log(client):
    _signin("events-user-1")
    try:
        r = client.post("/api/coach/events",
                        json={"event": "share_card_exported",
                              "metadata": {"period": "Q1", "junk": "x" * 500}})
        assert r.status_code == 200
        rows = _events_for("events-user-1", "share_card_exported")
        assert len(rows) == 1
        meta = rows[0].get("metadata") or {}
        assert meta.get("period") == "Q1"
        assert "junk" not in meta  # oversized values dropped
    finally:
        server.app.dependency_overrides.clear()


def test_demo_fires_demo_viewed_for_signed_in_only(client):
    client.get("/api/coach/demo")  # guest: must not write durable rows
    _signin("events-user-2")
    try:
        client.get("/api/coach/demo")
        assert len(_events_for("events-user-2", "demo_viewed")) == 1
    finally:
        server.app.dependency_overrides.clear()
    assert not _events_for(auth.ANONYMOUS_USER_ID, "demo_viewed")


# --------------------------------------------------------------------------- #
# Audit origin tagging + broker_connected honesty
# --------------------------------------------------------------------------- #

def test_audit_origin_defaults_to_upload(client):
    _signin("origin-user-1")
    try:
        client.post("/api/coach/audit", json={"use_demo": True})
        row = auth.get_latest_coach_audit("origin-user-1")
        assert row and row.get("origin") == "upload"
    finally:
        server.app.dependency_overrides.clear()


class _FakeSnapClient:
    def get_activities(self, uid, secret, start=None, end=None):
        return [
            {"type": "BUY", "units": 50, "price": 180, "amount": -9000,
             "trade_date": "2025-01-06", "symbol": {"raw_symbol": "AAPL"}},
            {"type": "SELL", "units": 50, "price": 184, "amount": 9200,
             "trade_date": "2025-01-08", "symbol": {"raw_symbol": "AAPL"}},
        ]


def test_auto_sync_origin_and_broker_connected_fires_once(monkeypatch):
    import agenticwhales.prices as prices
    monkeypatch.setattr(snaptrade_client, "from_env", lambda: _FakeSnapClient())
    monkeypatch.setattr(prices, "fetch_ohlc", lambda *a, **k: None)
    uid = "origin-user-sync-1"
    auth.upsert_snaptrade_user(uid, uid, "secret")

    out1 = snaptrade_api.sync_user(uid)
    out2 = snaptrade_api.sync_user(uid)
    assert out1 and out2

    row = auth.get_latest_coach_audit(uid)
    assert row.get("origin") == "sync_auto"
    # broker_connected means data flowed, and only fires the FIRST time.
    assert len(_events_for(uid, "broker_connected")) == 1


# --------------------------------------------------------------------------- #
# Admin funnel + /metrics
# --------------------------------------------------------------------------- #

def test_dashboard_funnel_shape(client):
    d = admin.build_dashboard()
    f = d["funnel"]
    assert set(f["events"]) >= {"demo_viewed", "upload_started", "audit_viewed",
                                "share_card_exported", "broker_connected"}
    for key in ("activated_users", "share_rate", "median_minutes_to_first_card",
                "d30_retention", "d30_cohort_size"):
        assert key in f
    # Empty populations report None, never fake zeros.
    if f["activated_users"] == 0:
        assert f["share_rate"] is None


def test_metrics_endpoint_open_by_default(client, monkeypatch):
    monkeypatch.delenv("AGENTICWHALES_METRICS_TOKEN", raising=False)
    r = client.get("/metrics")
    assert r.status_code == 200


def test_metrics_endpoint_token_gated(client, monkeypatch):
    monkeypatch.setenv("AGENTICWHALES_METRICS_TOKEN", "sekrit")
    assert client.get("/metrics").status_code == 403
    assert client.get("/metrics?token=sekrit").status_code == 200
