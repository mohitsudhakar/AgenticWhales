"""Waitlist: storage (web.auth), the web.waitlist module (validation, idempotency,
optional Sheet mirror), and the HTTP routes (public signup, count, admin CSV).
All offline — Supabase forced off, so signups land in the in-memory store."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from web import auth, server, waitlist


@pytest.fixture(autouse=True)
def _wipe(monkeypatch):
    monkeypatch.setattr(auth, "_db_writable", lambda: False)
    monkeypatch.delenv("WAITLIST_SHEET_WEBHOOK_URL", raising=False)
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


@pytest.fixture
def client():
    return TestClient(server.app)


# ===========================================================================
# web.waitlist module
# ===========================================================================

def test_is_valid_email():
    assert waitlist.is_valid_email("a@b.com")
    assert not waitlist.is_valid_email("nope")
    assert not waitlist.is_valid_email("")
    assert not waitlist.is_valid_email("a@b")
    assert not waitlist.is_valid_email("x" * 250 + "@b.com")  # over length cap


def test_add_signup_persists_and_normalizes():
    row = waitlist.add_signup(email="  Trader@Firm.COM ", name="  Jo  ",
                              company="Millennium", note="keen", when=1_700_000_000.0)
    assert row["email"] == "trader@firm.com"   # lower + trimmed
    assert row["name"] == "Jo"
    assert row["company"] == "Millennium"
    assert row["source"] == "landing"
    stored = auth.get_waitlist_signup("trader@firm.com")
    assert stored is not None and stored["company"] == "Millennium"


def test_add_signup_rejects_bad_email():
    with pytest.raises(ValueError):
        waitlist.add_signup(email="not-an-email")


def test_add_signup_idempotent_preserves_created_at():
    first = waitlist.add_signup(email="a@b.com", company="One", when=1_700_000_000.0)
    second = waitlist.add_signup(email="A@B.com", company="Two", when=1_700_000_500.0)
    # same id + original created_at, but updated fields + updated_at
    assert second["id"] == first["id"]
    assert second["created_at"] == first["created_at"]
    assert second["company"] == "Two"
    assert second["updated_at"] != first["created_at"]
    assert auth.count_waitlist_signups() == 1   # not duplicated


def test_sheet_mirror_called_when_configured(monkeypatch):
    monkeypatch.setenv("WAITLIST_SHEET_WEBHOOK_URL", "https://script.example/exec")
    posted = {}
    import requests
    monkeypatch.setattr(requests, "post",
                        lambda url, **kw: posted.update(url=url, kw=kw) or None)
    waitlist.add_signup(email="a@b.com", name="Z")
    assert posted.get("url") == "https://script.example/exec"
    assert "a@b.com" in posted["kw"]["data"]


def test_sheet_mirror_failure_never_raises(monkeypatch):
    monkeypatch.setenv("WAITLIST_SHEET_WEBHOOK_URL", "https://script.example/exec")
    import requests
    def _boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(requests, "post", _boom)
    # signup still succeeds even though the mirror failed
    row = waitlist.add_signup(email="a@b.com")
    assert auth.get_waitlist_signup("a@b.com") is not None


def test_to_csv_roundtrip():
    waitlist.add_signup(email="a@b.com", name="Al", company="X")
    csv_text = waitlist.to_csv(auth.list_waitlist_signups())
    assert csv_text.splitlines()[0] == "created_at,email,name,company,note,source"
    assert "a@b.com" in csv_text and "Al" in csv_text


# ===========================================================================
# storage helpers
# ===========================================================================

def test_list_waitlist_signups_newest_first():
    waitlist.add_signup(email="old@b.com", when=1_700_000_000.0)
    waitlist.add_signup(email="new@b.com", when=1_700_001_000.0)
    rows = auth.list_waitlist_signups()
    assert [r["email"] for r in rows] == ["new@b.com", "old@b.com"]


# ===========================================================================
# HTTP routes
# ===========================================================================

def test_post_waitlist_ok(client):
    r = client.post("/api/waitlist", json={"email": "vip@jpm.com", "company": "JPM"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert auth.get_waitlist_signup("vip@jpm.com")["company"] == "JPM"


def test_post_waitlist_bad_email_400(client):
    r = client.post("/api/waitlist", json={"email": "garbage"})
    assert r.status_code == 400


def test_post_waitlist_missing_email_422(client):
    # pydantic min_length=3 → validation error before our handler
    assert client.post("/api/waitlist", json={}).status_code == 422


def test_waitlist_count_public(client):
    client.post("/api/waitlist", json={"email": "a@b.com"})
    client.post("/api/waitlist", json={"email": "c@d.com"})
    body = client.get("/api/waitlist/count").json()
    assert body["count"] == 2            # true figure
    assert body["display"] == 100        # vanity floor


@pytest.mark.parametrize("real,shown", [
    (0, 100),      # empty → floor
    (3, 100),      # small → floor (reads "100+")
    (49, 100),     # just below threshold → still floor
    (50, 100),     # at threshold: 50*2 = 100 (equals floor)
    (60, 120),     # past threshold → doubled
    (250, 500),    # doubled
])
def test_display_count_curve(real, shown):
    assert waitlist.display_count(real) == shown


def test_count_endpoint_reflects_doubling(client, monkeypatch):
    # 60 real signups → UI shows 120
    monkeypatch.setattr(auth, "count_waitlist_signups", lambda: 60)
    body = client.get("/api/waitlist/count").json()
    assert body["count"] == 60 and body["display"] == 120


def test_waitlist_export_requires_admin(client):
    # offline → require_admin refuses (Supabase not configured) → 403
    assert client.get("/api/waitlist/export.csv").status_code in (401, 403)


def test_waitlist_export_csv_for_admin(monkeypatch):
    # Force the admin gate open: configure Supabase + a fake token → admin email.
    monkeypatch.setattr(auth, "_supabase_configured", lambda: True)
    monkeypatch.setattr(auth, "_fetch_user", lambda tok: ("admin-uid", auth.ADMIN_EMAIL))
    c = TestClient(server.app)
    c.post("/api/waitlist", json={"email": "vip@millennium.com", "company": "Millennium"})
    r = c.get("/api/waitlist/export.csv", headers={"Authorization": "Bearer t"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "vip@millennium.com" in r.text
    assert "attachment" in r.headers.get("content-disposition", "")
