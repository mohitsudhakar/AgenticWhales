"""Coverage for web/auth.py's in-memory fallback branches — the `else` side of
`if _db_writable()` that the DB-branch tests skip. conftest forces offline, so
these run against the _memstore directly with no transport.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from web import auth


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


def _now_ts():
    return time.time()


# ===========================================================================
# admin_list_* from memstore
# ===========================================================================

def test_admin_list_sessions_from_memstore():
    auth._memstore[("sessions", "s1")] = {
        "user_id": "u1", "ticker": "AAPL", "status": "completed",
        "created_at": _now_ts(), "completed_at": _now_ts(),
        "stats": {"tokens_in": 100, "tokens_out": 50, "llm_calls": 3, "tool_calls": 1},
        "config": {"quick_think_llm": "q", "deep_think_llm": "d"},
    }
    rows = auth.admin_list_sessions()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAPL" and rows[0]["tokens_in"] == 100
    assert rows[0]["quick_model"] == "q"


def test_admin_list_batches_from_memstore():
    auth._memstore[("batches", "b1")] = {
        "user_id": "u1", "status": "completed", "created_at": _now_ts(),
        "items": [{"ticker": "AAPL"}, {"ticker": "NVDA"}],
        "totals": {"tokens_in": 500, "tokens_out": 100, "llm_calls": 10, "tool_calls": 4},
        "config": {"quick_think_llm": "q", "deep_think_llm": "d"},
    }
    rows = auth.admin_list_batches()
    assert len(rows) == 1 and rows[0]["ticker_count"] == 2
    assert rows[0]["tokens_in"] == 500


def test_admin_list_profiles_and_users_empty_offline():
    # not _db_writable() → both return []
    assert auth.admin_list_profiles() == []
    assert auth.admin_list_users() == []


# ===========================================================================
# find_cached_session — memstore scan
# ===========================================================================

def _cached_session(**over):
    s = {"id": "s1", "user_id": "u1", "ticker": "AAPL", "analysis_date": "2024-01-02",
         "status": "completed", "completed_at": _now_ts(),
         "config": {"__sig": "sigA"}}
    s.update(over)
    return s


def test_find_cached_session_memstore_hit():
    auth._memstore[("sessions", "s1")] = _cached_session()
    hit = auth.find_cached_session("u1", "AAPL", "2024-01-02", "sigA")
    assert hit is not None and hit["id"] == "s1"


def test_find_cached_session_memstore_sig_miss():
    auth._memstore[("sessions", "s1")] = _cached_session()
    assert auth.find_cached_session("u1", "AAPL", "2024-01-02", "OTHER") is None


def test_find_cached_session_memstore_expired():
    old = time.time() - 60 * 60  # 1h ago, outside default 30m ttl
    auth._memstore[("sessions", "s1")] = _cached_session(completed_at=old)
    assert auth.find_cached_session("u1", "AAPL", "2024-01-02", "sigA") is None


def test_find_cached_session_memstore_not_completed():
    auth._memstore[("sessions", "s1")] = _cached_session(status="running")
    assert auth.find_cached_session("u1", "AAPL", "2024-01-02", "sigA") is None


def test_find_cached_session_anonymous_short_circuits():
    assert auth.find_cached_session(auth.ANONYMOUS_USER_ID, "AAPL", "2024-01-02", "x") is None


# ===========================================================================
# stuck-session reaper — memstore branch
# ===========================================================================

def test_list_stuck_running_sessions_memstore():
    old_iso = (datetime.now(tz=timezone.utc) - timedelta(hours=2)).isoformat()
    recent_iso = datetime.now(tz=timezone.utc).isoformat()
    auth._memstore[("sessions", "old")] = {"id": "old", "status": "running",
                                           "created_at": old_iso}
    auth._memstore[("sessions", "fresh")] = {"id": "fresh", "status": "running",
                                             "created_at": recent_iso}
    auth._memstore[("sessions", "done")] = {"id": "done", "status": "completed",
                                            "created_at": old_iso}
    auth._memstore[("sessions", "bad")] = {"id": "bad", "status": "running",
                                           "created_at": "not-a-date"}
    stuck = auth.list_stuck_running_sessions(older_than_seconds=30 * 60)
    ids = [r["id"] for r in stuck]
    assert ids == ["old"]  # only the old running one; bad-date + fresh + done excluded


def test_delete_stuck_running_sessions_memstore():
    old_ts = time.time() - 48 * 60 * 60  # 2 days
    auth._memstore[("sessions", "s1")] = {"id": "s1", "status": "running",
                                          "created_at": old_ts}
    auth._memstore[("sessions", "s2")] = {"id": "s2", "status": "pending",
                                          "created_at": old_ts}
    auth._memstore[("sessions", "s3")] = {"id": "s3", "status": "completed",
                                          "created_at": old_ts}
    n = auth.delete_stuck_running_sessions(older_than_seconds=24 * 60 * 60)
    assert n == 2
    assert ("sessions", "s1") not in auth._memstore
    assert ("sessions", "s3") in auth._memstore  # completed left alone


# ===========================================================================
# low-level helpers return empty/false when not writable
# ===========================================================================

def test_select_helpers_empty_offline():
    assert auth._select_columns("recipes", filters={"user_id": "u1"}) == []
    assert auth._select_for_user("sessions", "u1") == []


def test_fetch_user_unconfigured_returns_none():
    # offline: _supabase_url()/_supabase_anon_key() are empty → None
    assert auth._fetch_user("tok") is None


def test_validate_token_offline_none():
    assert auth._validate_token("tok") is None
