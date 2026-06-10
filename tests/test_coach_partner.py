"""Accountability partner: double-opt-in, compliance-only view (forbidden-keys
test), revocation kills the token, rate limits."""

import json

import pytest
from fastapi.testclient import TestClient

import web.coach_api as coach_api
import web.server as server
from web import auth

# Keys that must NEVER appear anywhere in a partner view payload.
FORBIDDEN_KEYS = {"pnl", "dollars", "symbol", "symbols", "total_pnl", "price",
                  "qty", "quantity", "notional", "trades", "transactions",
                  "discipline_score", "evidence"}


@pytest.fixture
def client():
    return TestClient(server.app)


def _signin(uid):
    server.app.dependency_overrides[coach_api.optional_user_id] = lambda: uid


def _invite(client, uid, email="pal@example.com"):
    _signin(uid)
    try:
        r = client.post("/api/coach/partner", json={"email": email})
        assert r.status_code == 200
        return r.json()
    finally:
        server.app.dependency_overrides.clear()


def _token_for(uid):
    return auth.list_coach_partners(uid)[0]["view_token"]


def test_invite_requires_signin_and_valid_email(client):
    assert client.post("/api/coach/partner",
                       json={"email": "x@y.co"}).status_code == 401
    _signin("partner-owner-0")
    try:
        assert client.post("/api/coach/partner",
                           json={"email": "nope"}).status_code == 400
    finally:
        server.app.dependency_overrides.clear()


def test_double_opt_in_required_before_view(client):
    j = _invite(client, "partner-owner-1")
    assert j["confirm_url"]            # email dark -> shareable link returned
    token = _token_for("partner-owner-1")
    # Not confirmed yet: view must 404.
    assert client.get(f"/api/coach/partner/view?token={token}").status_code == 404
    # GET confirm renders a form and does NOT activate.
    r = client.get(f"/api/coach/partner/confirm?token={token}")
    assert r.status_code == 200 and "<form" in r.text
    assert auth.list_coach_partners("partner-owner-1")[0]["status"] == "invited"
    # POST activates.
    assert client.post(f"/api/coach/partner/confirm?token={token}").status_code == 200
    assert auth.list_coach_partners("partner-owner-1")[0]["status"] == "active"
    assert client.get(f"/api/coach/partner/view?token={token}").status_code == 200


def test_view_payload_contains_nothing_financial(client):
    _invite(client, "partner-owner-2")
    token = _token_for("partner-owner-2")
    client.post(f"/api/coach/partner/confirm?token={token}")
    # Give the owner real coach data so a leak would be visible if it existed.
    _signin("partner-owner-2")
    try:
        client.post("/api/coach/audit", json={"use_demo": True})
        client.get("/api/coach/rules")
    finally:
        server.app.dependency_overrides.clear()
    v = client.get(f"/api/coach/partner/view?token={token}").json()
    flat = json.dumps(v).lower()
    for key in FORBIDDEN_KEYS:
        assert f'"{key}"' not in flat, f"partner view leaked {key!r}"
    assert "rules" in v and "streak_weeks" in v


def test_one_active_partner_and_revocation_kills_token(client):
    _invite(client, "partner-owner-3")
    _signin("partner-owner-3")
    try:
        # Second invite while one is pending -> 409.
        assert client.post("/api/coach/partner",
                           json={"email": "b@example.com"}).status_code == 409
        pid = auth.list_coach_partners("partner-owner-3")[0]["id"]
        token = auth.list_coach_partners("partner-owner-3")[0]["view_token"]
        client.post(f"/api/coach/partner/confirm?token={token}")
        assert client.delete(f"/api/coach/partner/{pid}").status_code == 200
    finally:
        server.app.dependency_overrides.clear()
    assert client.get(f"/api/coach/partner/view?token={token}").status_code == 404


def test_revoke_idor_guard(client):
    _invite(client, "partner-owner-4")
    pid = auth.list_coach_partners("partner-owner-4")[0]["id"]
    _signin("partner-attacker")
    try:
        assert client.delete(f"/api/coach/partner/{pid}").status_code == 404
    finally:
        server.app.dependency_overrides.clear()


def test_partner_can_unsubscribe_unilaterally(client):
    _invite(client, "partner-owner-5")
    token = _token_for("partner-owner-5")
    assert client.post(f"/api/coach/partner/unsubscribe?token={token}").status_code == 200
    assert auth.list_coach_partners("partner-owner-5")[0]["status"] == "revoked"


def test_partner_page_serves(client):
    r = client.get("/partner?token=whatever")
    assert r.status_code == 200 and "Accountability partner view" in r.text
