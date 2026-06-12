"""Market-validation tranche: /pricing smoke test + segment capture.

The pricing page is a measurement instrument, not billing: it must carry the
honest beta framing, reserve through the existing waitlist store, and pass the
same forbidden-phrases sweep as every other public page.
"""

import re

import pytest
from fastapi.testclient import TestClient

import web.server as server
from web import auth
from web.coach_api import COACH_EVENTS

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


def test_pricing_serves_and_carries_no_directives(client):
    text = _text(client, "/pricing")
    for phrase in FORBIDDEN_PAGE_PHRASES:
        assert phrase not in text, f"/pricing contains forbidden phrase {phrase!r}"
    assert "not investment advice" in text


def test_pricing_is_honest_about_beta_and_reservations(client):
    text = _text(client, "/pricing")
    # The smoke-test honesty rules from docs/marketing/icp-and-pricing.md:
    assert "free during the beta" in text or "free while we're in beta" in text
    assert "non-binding" in text
    assert "founding price" in text
    # Fee-to-fee comparison only — never a results promise:
    assert "not a promise about results" in text


def test_pricing_reserves_through_existing_waitlist(client):
    r = client.get("/pricing")
    assert "/api/waitlist" in r.text
    # Plan lands in `source` so the admin CSV export segments naturally:
    assert "'pricing-' + plan" in r.text
    # Segment rides along in `note`:
    assert "seg=" in r.text


def test_pricing_funnel_events_are_allowlisted(client):
    assert {"pricing_viewed", "founding_reserved"} <= COACH_EVENTS
    r = client.post("/api/coach/events", json={"event": "pricing_viewed"})
    assert r.status_code == 200 and r.json().get("ok") is True
    r = client.post("/api/coach/events",
                    json={"event": "founding_reserved",
                          "metadata": {"plan": "plus", "seg": "prop"}})
    assert r.status_code == 200 and r.json().get("ok") is True


def test_reservation_lands_in_waitlist_store(client):
    email = "founding-reserver@example.com"
    r = client.post("/api/waitlist", json={
        "email": email, "source": "pricing-plus", "note": "seg=prop"})
    assert r.status_code == 200
    row = auth.get_waitlist_signup(email)
    assert row and row["source"] == "pricing-plus" and row["note"] == "seg=prop"


def test_landing_links_pricing_and_captures_segment(client):
    r = client.get("/")
    assert 'href="/pricing"' in r.text
    assert 'id="wlSegment"' in r.text
    # The beachhead segment is presented on the landing page:
    assert "prop-firm" in r.text.lower()


def test_sitemap_includes_pricing(client):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert "/pricing</loc>" in r.text
