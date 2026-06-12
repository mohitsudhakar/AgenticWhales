"""Plan ladder + entitlements: mapping, beta non-enforcement, plan API."""

import pytest
from fastapi.testclient import TestClient

import web.server as server
import web.coach_api as coach_api
from web import auth, entitlements


@pytest.fixture
def client():
    return TestClient(server.app)


def test_tier_maps_to_plan(monkeypatch):
    monkeypatch.setattr(auth, "get_user_tier", lambda uid: "intermediate")
    assert entitlements.plan_for_user("u1") == "plus"
    monkeypatch.setattr(auth, "get_user_tier", lambda uid: "master")
    assert entitlements.plan_for_user("u1") == "pro"
    monkeypatch.setattr(auth, "get_user_tier", lambda uid: "novice")
    assert entitlements.plan_for_user("u1") == "free"
    assert entitlements.plan_for_user(auth.ANONYMOUS_USER_ID) == "free"


def test_every_plan_defines_every_key():
    keys = set(entitlements.ENTITLEMENTS["free"])
    for plan, ents in entitlements.ENTITLEMENTS.items():
        assert set(ents) == keys, f"{plan} matrix drifted"
    # Every gated feature names a real plan that actually includes it.
    for key, plan in entitlements.FEATURE_MIN_PLAN.items():
        assert entitlements.ENTITLEMENTS[plan][key] is True


def test_beta_default_never_blocks(monkeypatch):
    monkeypatch.delenv("AGENTICWHALES_BILLING_ENFORCED", raising=False)
    monkeypatch.setattr(auth, "get_user_tier", lambda uid: "novice")
    assert entitlements.check("u1", "eval_mode") is True          # gated on paper
    assert entitlements.check("u1", "max_active_rules", used=99) is True


def test_enforced_mode_gates(monkeypatch):
    monkeypatch.setenv("AGENTICWHALES_BILLING_ENFORCED", "1")
    monkeypatch.setattr(auth, "get_user_tier", lambda uid: "novice")
    assert entitlements.check("u1", "eval_mode") is False
    assert entitlements.check("u1", "max_active_rules", used=2) is False
    assert entitlements.check("u1", "max_active_rules", used=1) is True
    monkeypatch.setattr(auth, "get_user_tier", lambda uid: "master")
    assert entitlements.check("u1", "eval_mode") is True
    assert entitlements.check("u1", "briefs_per_day", used=10_000) is True  # unlimited


def test_plan_api_guest_and_signed_in(client):
    j = client.get("/api/account/plan").json()
    assert j["plan"] == "free" and j["beta"] is True
    server.app.dependency_overrides[coach_api.optional_user_id] = lambda: "plan-user-1"
    try:
        auth._memstore[("profiles", "plan-user-1")] = {"id": "plan-user-1", "tier": "master"}
        j = client.get("/api/account/plan").json()
        assert j["plan"] == "pro" and j["label"] == "Pro"
        assert j["entitlements"]["standing_briefs"] is True
    finally:
        server.app.dependency_overrides.clear()
