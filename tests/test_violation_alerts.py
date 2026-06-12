"""Violation alerts (Plus): opt-in, minimized, env-gated, never breaking audits."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import web.coach_api as coach_api
import web.server as server
from web import auth, email_service


@pytest.fixture
def client():
    return TestClient(server.app)


def _viol(kind="cooldown_after_loss"):
    return SimpleNamespace(rule_kind=kind, occurred_on="2026-06-01", dollars=-812.0)


def test_no_email_without_opt_in(monkeypatch):
    sent = []
    monkeypatch.setattr(email_service, "is_configured", lambda: True)
    monkeypatch.setattr(email_service, "send_email", lambda *a, **k: sent.append(a) or True)
    auth.upsert_coach_prefs("alert-u1", {"email_alerts": False, "digest_email": "a@b.co"})
    coach_api._send_violation_alert("alert-u1", [_viol()])
    assert sent == []


def test_no_email_when_resend_unconfigured(monkeypatch):
    sent = []
    monkeypatch.setattr(email_service, "is_configured", lambda: False)
    monkeypatch.setattr(email_service, "send_email", lambda *a, **k: sent.append(a) or True)
    auth.upsert_coach_prefs("alert-u2", {"email_alerts": True, "digest_email": "a@b.co",
                                         "unsubscribe_token": "tok"})
    coach_api._send_violation_alert("alert-u2", [_viol()])
    assert sent == []


def test_alert_email_is_minimized_with_unsubscribe(monkeypatch):
    from agenticwhales import pretrade
    captured = {}
    def fake_send(to, subject, html, **kw):
        captured.update(to=to, subject=subject, html=html, **kw)
        return True
    monkeypatch.setattr(email_service, "is_configured", lambda: True)
    monkeypatch.setattr(email_service, "send_email", fake_send)
    monkeypatch.setenv("AGENTICWHALES_PUBLIC_BASE_URL", "https://agenticwhales.com")
    auth.upsert_coach_prefs("alert-u3", {"email_alerts": True, "digest_email": "t@x.co",
                                         "unsubscribe_token": "tok123"})
    coach_api._send_violation_alert("alert-u3", [_viol(), _viol("size_cap_x_median")])
    assert captured["to"] == "t@x.co"
    assert "2 new rule violations" in captured["subject"]
    assert "$" not in captured["html"]                 # dollars stay in-app
    assert "-812" not in captured["html"]
    assert "cooldown after loss" in captured["html"]
    assert "token=tok123" in captured["unsubscribe_url"]
    assert "agenticwhales.com/coach#rules" in captured["html"]
    assert not pretrade.contains_directive(captured["html"])


def test_prefs_api_email_alerts_roundtrip(client):
    server.app.dependency_overrides[coach_api.optional_user_id] = lambda: "alert-u4"
    try:
        # Enabling alerts without an email on file is rejected.
        r = client.post("/api/coach/prefs", json={"email_alerts": True})
        assert r.status_code == 400
        r = client.post("/api/coach/prefs",
                        json={"email": "u4@x.co", "email_alerts": True}).json()
        assert r["email_alerts"] is True
        g = client.get("/api/coach/prefs").json()
        assert g["email_alerts"] is True
        r = client.post("/api/coach/prefs", json={"email_alerts": False}).json()
        assert r["email_alerts"] is False
    finally:
        server.app.dependency_overrides.clear()
