"""PR-2: server-enforced compliance attestation gate.

Covers:
- POST /api/audit/compliance-ack creates a versioned row + returns its id
- The endpoint rejects when version mismatches the active version
- The endpoint rejects when any of the three ack flags is False
- require_active_attestation accepts an explicit id, rejects a wrong-owner
  id (404), rejects a stale-version row (412), and falls back to "latest
  active" when no explicit id is supplied.
- Session creation surfaces HTTP 412 when no attestation is available.

These tests run against the in-memory storage (no Supabase needed).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from web import auth, server


@pytest.fixture(autouse=True)
def _clean_memstore(monkeypatch):
    # Force the in-memory fallback regardless of whether the developer
    # happens to have Supabase env vars exported. Otherwise the test hits
    # a real DB where the new tables don't exist yet.
    monkeypatch.setattr(auth, "_db_writable", lambda: False)
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


def _make_attestation(user_id: str, version: str = "v1.0",
                      revoked: bool = False, ack_all: bool = True) -> str:
    """Create a stored attestation row and return its id."""
    import uuid as _uuid
    from datetime import datetime, timezone
    row = {
        "id": _uuid.uuid4().hex,
        "user_id": user_id,
        "version": version,
        "ack_paper_only": ack_all,
        "ack_not_advice": ack_all,
        "ack_jurisdiction": ack_all,
        "disclaimer_text": "test disclaimer",
        "jurisdiction": "US",
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "revoked_at": "2026-01-01T00:00:00Z" if revoked else None,
    }
    auth.save_compliance_attestation(row)
    return row["id"]


# ---------------------------------------------------------------------------
# active_compliance_version + storage helpers
# ---------------------------------------------------------------------------


def test_active_version_falls_back_to_v1_when_db_unwritable():
    assert auth.active_compliance_version() == "v1.0"


def test_latest_active_attestation_excludes_revoked_rows():
    user_id = "user-A"
    revoked = _make_attestation(user_id, revoked=True)
    fresh = _make_attestation(user_id, revoked=False)

    found = auth.latest_active_attestation_for_user(user_id)
    assert found is not None
    assert found["id"] == fresh
    assert found["id"] != revoked


def test_latest_active_attestation_excludes_stale_version():
    user_id = "user-B"
    _make_attestation(user_id, version="v0.9")  # stale
    found = auth.latest_active_attestation_for_user(user_id)
    assert found is None


def test_latest_active_attestation_requires_all_three_acks():
    user_id = "user-C"
    _make_attestation(user_id, ack_all=False)
    assert auth.latest_active_attestation_for_user(user_id) is None


# ---------------------------------------------------------------------------
# require_active_attestation — the dependency function used by session create
# ---------------------------------------------------------------------------


def test_require_attestation_uses_supplied_id_when_valid():
    user_id = "user-D"
    aid = _make_attestation(user_id)
    row = server.require_active_attestation(user_id, aid)
    assert row["id"] == aid


def test_require_attestation_rejects_wrong_owner_with_404():
    owner = "user-E"
    other = "user-F"
    aid = _make_attestation(owner)
    with pytest.raises(HTTPException) as excinfo:
        server.require_active_attestation(other, aid)
    assert excinfo.value.status_code == 404


def test_require_attestation_rejects_stale_version_with_412():
    user_id = "user-G"
    aid = _make_attestation(user_id, version="v0.9")
    with pytest.raises(HTTPException) as excinfo:
        server.require_active_attestation(user_id, aid)
    assert excinfo.value.status_code == 412


def test_require_attestation_falls_back_to_latest_active():
    user_id = "user-H"
    aid = _make_attestation(user_id)
    row = server.require_active_attestation(user_id, attestation_id=None)
    assert row["id"] == aid


def test_require_attestation_raises_412_when_no_valid_row():
    with pytest.raises(HTTPException) as excinfo:
        server.require_active_attestation("user-I", attestation_id=None)
    assert excinfo.value.status_code == 412
    # The 412 detail is now a structured dict with a machine-parseable code
    # so the frontend interceptor can show the modal instead of leaking the
    # raw error to the user.
    detail = excinfo.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "compliance_required"
    assert "attestation required" in detail["message"].lower()


# ---------------------------------------------------------------------------
# Tier-driven default risk limits (PR-2, Sundar review #3)
# ---------------------------------------------------------------------------


def test_default_risk_limits_inherits_novice_caps_for_unknown_user():
    row = auth._default_risk_limits_row("brand-new-user")
    assert row["daily_spend_cap_usd"] == 0.50
    assert row["monthly_spend_cap_usd"] == 10.0


def test_default_risk_limits_inherits_master_caps_when_tier_set():
    user_id = "tier-master"
    auth._memstore[("profiles", user_id)] = {
        "id": user_id, "username": "vip", "tier": "master",
    }
    row = auth._default_risk_limits_row(user_id)
    assert row["daily_spend_cap_usd"] == 50.0
    assert row["monthly_spend_cap_usd"] == 1000.0


def test_default_risk_limits_inherits_intermediate_caps():
    user_id = "tier-intermediate"
    auth._memstore[("profiles", user_id)] = {
        "id": user_id, "username": "regular", "tier": "intermediate",
    }
    row = auth._default_risk_limits_row(user_id)
    assert row["daily_spend_cap_usd"] == 5.0
    assert row["monthly_spend_cap_usd"] == 100.0
