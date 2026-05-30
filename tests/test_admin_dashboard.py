"""Coverage for web/admin.py — the usage-dashboard aggregation. The four
auth.admin_list_* sources are monkeypatched with fixtures so the per-user +
daily roll-up logic runs with no DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from web import admin


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def test_parse_iso_variants():
    assert admin._parse_iso(None) is None
    assert admin._parse_iso("garbage") is None
    dt = admin._parse_iso("2024-01-02T03:04:05Z")
    assert dt is not None and dt.year == 2024


def test_day_key():
    assert admin._day_key(None) is None
    assert admin._day_key("2024-01-02T23:00:00+00:00") == "2024-01-02"


def test_i_coercion():
    assert admin._i(None) == 0
    assert admin._i("5") == 5
    assert admin._i("nope") == 0
    assert admin._i(3.9) == 3


def test_empty_user_bucket_shape():
    b = admin._empty_user_bucket("u1")
    assert b["user_id"] == "u1" and b["tier"] == "novice"
    assert b["tokens_total"] == 0


# ---------------------------------------------------------------------------
# build_dashboard
# ---------------------------------------------------------------------------

@pytest.fixture
def patched(monkeypatch):
    today = datetime.now(timezone.utc).date().isoformat()
    sessions = [
        {"user_id": "u1", "created_at": f"{today}T10:00:00+00:00",
         "tokens_in": 100, "tokens_out": 50, "llm_calls": 3, "tool_calls": 1},
        {"user_id": "u1", "created_at": f"{today}T11:00:00+00:00",
         "tokens_in": 200, "tokens_out": 80, "llm_calls": 2, "tool_calls": 0},
        {"user_id": "u2", "created_at": f"{today}T09:00:00+00:00",
         "tokens_in": 10, "tokens_out": 5, "llm_calls": 1, "tool_calls": 0},
        {"user_id": None, "created_at": None},  # anonymous, no day → tolerated
    ]
    batches = [
        {"user_id": "u1", "created_at": f"{today}T12:00:00+00:00",
         "tokens_in": 500, "tokens_out": 100, "llm_calls": 10, "tool_calls": 4},
    ]
    profiles = [
        {"id": "u1", "username": "alice", "tier": "master", "created_at": "2023-01-01T00:00:00Z"},
        {"id": "u3", "username": "carol"},  # profile-only user (no sessions)
    ]
    users = [
        {"id": "u1", "email": "alice@x.com", "created_at": "2023-01-01T00:00:00Z",
         "user_metadata": {"full_name": "Alice A"}},
        {"id": "u2", "email": "bob@x.com"},
        {"id": None},  # skipped
    ]
    monkeypatch.setattr(admin.auth, "admin_list_sessions", lambda: sessions)
    monkeypatch.setattr(admin.auth, "admin_list_batches", lambda: batches)
    monkeypatch.setattr(admin.auth, "admin_list_profiles", lambda: profiles)
    monkeypatch.setattr(admin.auth, "admin_list_users", lambda: users)
    return today


def test_build_dashboard_overall(patched):
    dash = admin.build_dashboard()
    o = dash["overall"]
    assert o["total_users"] == 3          # total_users = len(users), raw count
    assert o["total_analyses"] == 4       # 4 session rows
    assert o["total_batches"] == 1
    assert o["total_tokens_in"] == 100 + 200 + 10 + 500
    assert o["total_tokens_out"] == 50 + 80 + 5 + 100
    assert o["dau_today"] == 2            # u1 + u2 active today
    # only the 3 dated sessions land in a day bucket (the None-dated anon row
    # counts toward total_analyses but not the daily series)
    assert o["analyses_today"] == 3


def test_build_dashboard_per_user(patched):
    dash = admin.build_dashboard()
    by_id = {u["user_id"]: u for u in dash["per_user"]}
    # u1 has 2 analyses + 1 batch, profile name/tier applied
    assert by_id["u1"]["username"] == "alice"
    assert by_id["u1"]["tier"] == "master"
    assert by_id["u1"]["analyses"] == 2 and by_id["u1"]["batches"] == 1
    assert by_id["u1"]["tokens_total"] == (100 + 200 + 500) + (50 + 80 + 100)
    # u2 from the users list, no profile → falls back
    assert by_id["u2"]["email"] == "bob@x.com"
    # u3 is profile-only with zero activity
    assert by_id["u3"]["analyses"] == 0
    # per_user is sorted by tokens_total desc → u1 first
    assert dash["per_user"][0]["user_id"] == "u1"


def test_build_dashboard_daily_series_window(patched):
    dash = admin.build_dashboard()
    assert len(dash["daily"]) == admin.DAILY_WINDOW_DAYS
    last = dash["daily"][-1]
    assert last["date"] == patched          # window ends today
    # 3 dated sessions today (None-dated anon row excluded); u1 + u2 active
    assert last["analyses"] == 3 and last["active_users"] == 2
    # a middle day with no activity is zero-filled
    assert dash["daily"][0]["analyses"] == 0


def test_build_dashboard_metadata(patched):
    dash = admin.build_dashboard()
    assert "generated_at" in dash
    assert dash["admin_email"] == admin.auth.ADMIN_EMAIL
    assert isinstance(dash["supabase_configured"], bool)


def test_build_dashboard_empty_sources(monkeypatch):
    for fn in ("admin_list_sessions", "admin_list_batches",
               "admin_list_profiles", "admin_list_users"):
        monkeypatch.setattr(admin.auth, fn, lambda: [])
    dash = admin.build_dashboard()
    assert dash["per_user"] == []
    assert dash["overall"]["total_users"] == 0
    assert len(dash["daily"]) == admin.DAILY_WINDOW_DAYS
