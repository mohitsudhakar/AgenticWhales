"""Phase 1 trust surface: /security + /methodology pages.

Page-level compliance uses an explicit forbidden-phrases list rather than
pretrade.contains_directive — disclaimer copy legitimately contains the words
"buy" and "sell" ("never says buy or sell"), which the strict tripwire would
flag. The tripwire stays scoped to short generated strings.
"""

import re

import pytest
from fastapi.testclient import TestClient

import web.server as server

# Second-person / imperative advice constructions that must never appear in
# page copy regardless of context.
FORBIDDEN_PAGE_PHRASES = [
    "you should buy", "you should sell", "we recommend buying",
    "we recommend selling", "buy now", "sell now", "time to buy",
    "time to sell", "enter this trade", "take this trade",
    "guaranteed", "will make you", "beat the market",
]


@pytest.fixture
def client():
    return TestClient(server.app)


def _page_text(client, path: str) -> str:
    r = client.get(path)
    assert r.status_code == 200, path
    # crude tag strip is fine for phrase scanning
    return re.sub(r"<[^>]+>", " ", r.text).lower()


@pytest.mark.parametrize("path", ["/security", "/methodology"])
def test_trust_pages_serve_and_carry_no_directives(client, path):
    text = _page_text(client, path)
    for phrase in FORBIDDEN_PAGE_PHRASES:
        assert phrase not in text, f"{path} contains forbidden phrase {phrase!r}"
    assert "not investment advice" in text


def test_security_page_claims_match_reality(client):
    text = _page_text(client, "/security")
    # The honest claims the compliance review required:
    assert "never place orders" in text
    assert "row-level security" in text
    assert "snaptrade" in text and "supabase" in text and "resend" in text  # named processors
    assert "delete all my data" in text
    # The overclaim the review banned must NOT appear:
    assert "credentials never touch our server" not in text


def test_methodology_page_states_the_law_and_limits(client):
    text = _page_text(client, "/methodology")
    assert "deterministic" in text
    assert "not detected forward" in text          # honest forward-validation term
    assert "barber" in text                        # research cited qualitatively
    assert "limitations" in text
    # No invented-percentile language:
    assert "modeled percentile" not in text


def test_coach_page_links_to_trust_pages(client):
    r = client.get("/coach")
    assert r.status_code == 200
    assert 'href="/methodology"' in r.text and 'href="/security"' in r.text
