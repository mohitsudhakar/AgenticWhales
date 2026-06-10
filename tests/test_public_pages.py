"""Phase 3 public surface: coach-first landing, learn pages, sitemap, referral."""

import re

import pytest
from fastapi.testclient import TestClient

import web.server as server
from web import admin, auth
from web.learn_content import LEARN_PAGES

FORBIDDEN_PAGE_PHRASES = [
    "you should buy", "you should sell", "we recommend buying",
    "we recommend selling", "buy now", "sell now", "time to buy",
    "time to sell", "enter this trade", "take this trade",
    "guaranteed", "will make you", "beat the market",
]


@pytest.fixture
def client():
    return TestClient(server.app)


def _text(client, path):
    r = client.get(path)
    assert r.status_code == 200, path
    return re.sub(r"<[^>]+>", " ", r.text).lower()


def test_root_serves_coach_landing(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "discipline tax" in r.text.lower()
    # The waitlist hooks the old tests assert stay wired:
    assert "Join the waitlist" in r.text
    assert "data-waitlist" in r.text
    assert "/api/waitlist" in r.text
    assert "/signin" in r.text
    # The activation path:
    assert "/api/coach/upload_async" in r.text


def test_old_fund_landing_preserved(client):
    r = client.get("/fund-welcome")
    assert r.status_code == 200
    assert r.text != client.get("/").text


def test_learn_index_and_pages(client):
    idx = client.get("/learn")
    assert idx.status_code == 200
    for slug in LEARN_PAGES:
        assert f"/learn/{slug}" in idx.text
        page = client.get(f"/learn/{slug}")
        assert page.status_code == 200
        assert "Last reviewed" in page.text          # staleness disclosure
        assert "not investment advice" in page.text.lower()


def test_learn_unknown_slug_404(client):
    assert client.get("/learn/not-a-page").status_code == 404


def test_landing_and_learn_pages_carry_no_directives(client):
    paths = ["/", "/learn"] + [f"/learn/{s}" for s in LEARN_PAGES]
    for path in paths:
        text = _text(client, path)
        for phrase in FORBIDDEN_PAGE_PHRASES:
            assert phrase not in text, f"{path} contains {phrase!r}"


def test_sitemap_and_robots(client):
    sm = client.get("/sitemap.xml")
    assert sm.status_code == 200
    for slug in LEARN_PAGES:
        assert f"/learn/{slug}" in sm.text
    assert "/coach" in sm.text and "/methodology" in sm.text
    rb = client.get("/robots.txt")
    assert rb.status_code == 200 and "Sitemap:" in rb.text


# --------------------------------------------------------------------------- #
# Referral attribution (no rewards)
# --------------------------------------------------------------------------- #

def test_referral_claim_requires_auth(client):
    r = client.post("/api/referral/claim", json={"code": "creator1"})
    assert r.status_code == 401


def _signed_in(uid):
    server.app.dependency_overrides[auth.get_current_user_id] = lambda: uid


def test_referral_claim_first_touch_wins(client):
    _signed_in("ref-user-1")
    try:
        r1 = client.post("/api/referral/claim", json={"code": "creator1"})
        assert r1.status_code == 200 and r1.json()["recorded"] is True
        r2 = client.post("/api/referral/claim", json={"code": "creator2"})
        assert r2.status_code == 200 and r2.json()["recorded"] is False
        assert auth.admin_referral_stats().get("creator1", 0) >= 1
        assert auth._memstore[("referral_attributions", "ref-user-1")]["code"] == "creator1"
    finally:
        server.app.dependency_overrides.clear()


def test_referral_claim_validates_code(client):
    _signed_in("ref-user-2")
    try:
        r = client.post("/api/referral/claim", json={"code": "bad code!!"})
        assert r.status_code == 400
    finally:
        server.app.dependency_overrides.clear()


def test_dashboard_includes_referrals(client):
    d = admin.build_dashboard()
    assert "referrals" in d
