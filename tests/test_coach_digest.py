"""Weekly digest: builder arithmetic, idempotency, opt-in prefs, unsubscribe
(GET never mutates; POST flips), and the no-dollars-in-email rule."""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

import web.coach_api as coach_api
import web.server as server
from web import auth, digest, email_service


@pytest.fixture
def client():
    return TestClient(server.app)


def _signin(uid):
    server.app.dependency_overrides[coach_api.optional_user_id] = lambda: uid


def test_week_start_is_previous_monday():
    assert digest.week_start_for(dt.date(2026, 6, 10)) == dt.date(2026, 6, 1)   # Wed
    assert digest.week_start_for(dt.date(2026, 6, 8)) == dt.date(2026, 6, 1)    # Mon
    assert digest.week_start_for(dt.date(2026, 6, 14)) == dt.date(2026, 6, 1)   # Sun


def test_build_digest_counts_week_trades(client):
    _signin("digest-user-1")
    try:
        client.post("/api/coach/audit", json={"use_demo": True})
    finally:
        server.app.dependency_overrides.clear()
    p = digest.build_digest("digest-user-1", dt.date(2025, 1, 6))
    assert p is not None
    assert p["n_trades_closed"] >= 1            # demo AAPL closed 2025-01-08
    assert {"violations", "streak_weeks", "score", "week_start"} <= set(p)
    assert digest.build_digest("no-such-user", dt.date(2025, 1, 6)) is None


def test_send_weekly_digests_idempotent_and_inapp_without_email(client, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    _signin("digest-user-2")
    try:
        client.post("/api/coach/audit", json={"use_demo": True})
    finally:
        server.app.dependency_overrides.clear()
    r1 = digest.send_weekly_digests(today=dt.date(2026, 6, 10))
    assert r1["written"] >= 1 and r1["emailed"] == 0
    r2 = digest.send_weekly_digests(today=dt.date(2026, 6, 10))
    assert r2["written"] == 0                    # idempotent per user-week
    rows = auth.list_coach_digests("digest-user-2")
    assert rows and rows[0]["week_start"] == "2026-06-01"
    assert rows[0]["emailed"] is False


def test_digest_email_html_carries_no_dollars():
    p = digest.build_digest.__wrapped__ if hasattr(digest.build_digest, "__wrapped__") else None
    payload = {"week_start": "2026-06-01", "week_end": "2026-06-07",
               "n_trades_closed": 3, "violations": 1, "streak_weeks": 2,
               "score": 61, "score_delta": 4, "realized_pnl": 1234.56}
    html = digest._digest_email_html(payload, "https://example.com")
    assert "$" not in html and "1234" not in html      # dollars stay in-app
    assert "rule violation" in html.lower()


def test_email_service_unconfigured_noop(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("AGENTICWHALES_EMAIL_FROM", raising=False)
    assert email_service.is_configured() is False
    assert email_service.send_email("a@b.co", "s", "<p>x</p>") is False


def test_prefs_opt_in_requires_email(client):
    _signin("prefs-user-1")
    try:
        r = client.post("/api/coach/prefs", json={"email_digest": True})
        assert r.status_code == 400
        r = client.post("/api/coach/prefs",
                        json={"email_digest": True, "email": "not-an-email"})
        assert r.status_code == 400
        r = client.post("/api/coach/prefs",
                        json={"email_digest": True, "email": "t@example.com"})
        assert r.status_code == 200 and r.json()["email_digest"] is True
        prefs = auth.get_coach_prefs("prefs-user-1")
        assert len(prefs["unsubscribe_token"]) > 20    # minted on first opt-in
    finally:
        server.app.dependency_overrides.clear()


def test_unsubscribe_get_never_mutates_post_flips(client):
    _signin("prefs-user-2")
    try:
        client.post("/api/coach/prefs",
                    json={"email_digest": True, "email": "u@example.com"})
    finally:
        server.app.dependency_overrides.clear()
    token = auth.get_coach_prefs("prefs-user-2")["unsubscribe_token"]

    assert client.get("/api/coach/digest/unsubscribe?token=bogus").status_code == 404
    r = client.get(f"/api/coach/digest/unsubscribe?token={token}")
    assert r.status_code == 200 and "<form" in r.text
    assert auth.get_coach_prefs("prefs-user-2")["email_digest"] is True   # unchanged

    r = client.post(f"/api/coach/digest/unsubscribe?token={token}")
    assert r.status_code == 200
    assert auth.get_coach_prefs("prefs-user-2")["email_digest"] is False


def test_digests_endpoint_guest_empty(client):
    assert client.get("/api/coach/digests").json() == {"signed_in": False, "digests": []}
