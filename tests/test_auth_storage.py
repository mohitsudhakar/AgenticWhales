"""Coverage for web/auth.py storage helpers via the in-memory fallback path.

conftest's `_force_offline_supabase` strips Supabase env vars for unit tests,
so `_db_writable()` is False and every helper exercises its `_memstore` branch.
No network, no DB. Every function called here is verified to exist with the
signature used (see web/auth.py).
"""

from __future__ import annotations

import time

import pytest

from web import auth


@pytest.fixture(autouse=True)
def _wipe(monkeypatch):
    # Force the memstore path regardless of any .env creds a module-level
    # load_dotenv() may have repopulated mid-session.
    monkeypatch.setattr(auth, "_db_writable", lambda: False)
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

def _session(sid="s1", user="u1", **kw):
    base = {"id": sid, "user_id": user, "ticker": "AAPL",
            "analysis_date": "2024-01-02", "status": "completed",
            "created_at": 1_700_000_000.0}
    base.update(kw)
    return base


def test_save_load_session():
    auth.save_session(_session())
    assert auth.load_session("s1")["ticker"] == "AAPL"


def test_load_missing_session_is_none():
    assert auth.load_session("nope") is None


def test_list_sessions_scoped_to_user():
    auth.save_session(_session("s1", "u1"))
    auth.save_session(_session("s2", "u1"))
    auth.save_session(_session("s3", "u2"))
    ids = {s["id"] for s in auth.list_sessions("u1")}
    assert ids == {"s1", "s2"}


def test_delete_session():
    auth.save_session(_session())
    assert auth.delete_session("s1") is True
    assert auth.load_session("s1") is None


def test_find_cached_session_hit_and_miss():
    s = _session(completed_at=time.time())
    s["config"] = {"__sig": "sig-abc"}
    auth.save_session(s)
    assert auth.find_cached_session(
        user_id="u1", ticker="AAPL", analysis_date="2024-01-02",
        config_sig="sig-abc", ttl_minutes=60) is not None
    assert auth.find_cached_session(
        user_id="u1", ticker="AAPL", analysis_date="2024-01-02",
        config_sig="other", ttl_minutes=60) is None


def test_find_cached_session_expired():
    s = _session(completed_at=1.0)  # ancient epoch
    s["config"] = {"__sig": "sig"}
    auth.save_session(s)
    assert auth.find_cached_session(
        user_id="u1", ticker="AAPL", analysis_date="2024-01-02",
        config_sig="sig", ttl_minutes=1) is None


def test_find_cached_session_anonymous_is_none():
    assert auth.find_cached_session(
        user_id=auth.ANONYMOUS_USER_ID, ticker="AAPL",
        analysis_date="2024-01-02", config_sig="x") is None


def test_mark_session_failed():
    auth.save_session(_session(status="running"))
    assert auth.mark_session_failed("s1", failure_reason="boom") is True
    assert auth.load_session("s1")["status"] == "failed"


# ---------------------------------------------------------------------------
# batches
# ---------------------------------------------------------------------------

def test_batch_crud():
    auth.save_batch({"id": "b1", "user_id": "u1", "status": "done",
                     "items": [{}, {}], "created_at": 1.0})
    assert auth.load_batch("b1")["status"] == "done"
    assert {b["id"] for b in auth.list_batches("u1")} == {"b1"}
    assert auth.delete_batch("b1") is True
    assert auth.load_batch("b1") is None


# ---------------------------------------------------------------------------
# recipes
# ---------------------------------------------------------------------------

def _recipe(rid="r1", user="u1", status="active", **kw):
    base = {"id": rid, "user_id": user, "status": status,
            "created_at": "2024-01-01T00:00:00Z", "consecutive_failures": 0}
    base.update(kw)
    return base


def test_recipe_crud_and_status():
    auth.save_recipe(_recipe())
    assert auth.load_recipe("r1")["status"] == "active"
    auth.update_recipe_status("r1", "paused")
    assert auth.load_recipe("r1")["status"] == "paused"
    assert auth.delete_recipe("r1") is True
    assert auth.load_recipe("r1") is None


def test_list_recipes_and_all_active():
    auth.save_recipe(_recipe("r1", "u1", status="active"))
    auth.save_recipe(_recipe("r2", "u1", status="paused"))
    auth.save_recipe(_recipe("r3", "u2", status="active"))
    assert {r["id"] for r in auth.list_recipes("u1")} == {"r1", "r2"}
    assert {r["id"] for r in auth.list_recipes_all_active()} == {"r1", "r3"}


def test_recipe_failure_counters():
    auth.save_recipe(_recipe())
    assert auth.bump_recipe_failures("r1") == 1
    assert auth.bump_recipe_failures("r1") == 2
    auth.reset_recipe_failures("r1")
    assert auth.load_recipe("r1")["consecutive_failures"] == 0


def test_touch_recipe_last_run():
    auth.save_recipe(_recipe())
    auth.touch_recipe_last_run("r1", 1_700_000_000.0)
    assert auth.load_recipe("r1")["last_run_at"] is not None


# ---------------------------------------------------------------------------
# paper account / positions / orders
# ---------------------------------------------------------------------------

def test_paper_account_roundtrip():
    auth.upsert_paper_account(user_id="u1", cash=50000.0, starting_cash=100000.0)
    acct = auth.load_paper_account("u1")
    assert acct["cash"] == 50000.0 and acct["user_id"] == "u1"


def test_paper_positions_upsert_and_list():
    auth.upsert_paper_position(user_id="u1", ticker="aapl", qty=10, avg_cost=150.0)
    auth.upsert_paper_position(user_id="u1", ticker="AAPL", qty=15, avg_cost=160.0)  # update
    auth.upsert_paper_position(user_id="u1", ticker="NVDA", qty=5, avg_cost=800.0)
    rows = auth.list_paper_positions("u1")
    assert len(rows) == 2  # AAPL deduped by upper-cased ticker
    aapl = next(r for r in rows if r["ticker"] == "AAPL")
    assert aapl["qty"] == 15


def test_paper_position_load_and_delete():
    auth.upsert_paper_position(user_id="u1", ticker="AAPL", qty=10, avg_cost=150.0)
    assert auth.load_paper_position("u1", "AAPL")["qty"] == 10
    assert auth.delete_paper_position("u1", "AAPL") is True
    assert auth.load_paper_position("u1", "AAPL") is None


def test_paper_orders_insert_list_limit():
    for i in range(5):
        auth.insert_paper_order({"id": f"o{i}", "user_id": "u1",
                                 "created_at": f"2024-01-0{i+1}T00:00:00Z"})
    rows = auth.list_paper_orders("u1", limit=3)
    assert len(rows) == 3
    assert rows[0]["id"] == "o4"  # newest first


# ---------------------------------------------------------------------------
# risk limits + events
# ---------------------------------------------------------------------------

def test_risk_limits_upsert_merges():
    auth.upsert_risk_limits("u1", max_position_pct=0.2)
    merged = auth.upsert_risk_limits("u1", kill_switch=True)
    assert merged["max_position_pct"] == 0.2
    assert merged["kill_switch"] is True
    assert auth.load_risk_limits("u1")["kill_switch"] is True


def test_risk_events_insert_and_list():
    auth.insert_risk_event({"user_id": "u1", "created_at": "2024-01-01T00:00:00Z",
                            "rule": "max_position"})
    auth.insert_risk_event({"user_id": "u1", "created_at": "2024-01-02T00:00:00Z",
                            "rule": "drawdown"})
    rows = auth.list_risk_events("u1")
    assert [r["created_at"] for r in rows] == [
        "2024-01-02T00:00:00Z", "2024-01-01T00:00:00Z"]  # newest first


# ---------------------------------------------------------------------------
# conviction
# ---------------------------------------------------------------------------

def test_conviction_insert_and_list_filtered():
    auth.insert_conviction_score({"id": "c1", "user_id": "u1", "ticker": "AAPL",
                                  "conviction_score": 8, "recorded_at": "2024-01-02T00:00:00Z"})
    auth.insert_conviction_score({"id": "c2", "user_id": "u1", "ticker": "NVDA",
                                  "conviction_score": 6, "recorded_at": "2024-01-03T00:00:00Z"})
    assert len(auth.list_conviction_scores("u1")) == 2
    only_aapl = auth.list_conviction_scores("u1", ticker="AAPL")
    assert len(only_aapl) == 1 and only_aapl[0]["ticker"] == "AAPL"


# ---------------------------------------------------------------------------
# recipe usage + user spend
# ---------------------------------------------------------------------------

def test_recipe_usage_accumulates():
    auth.add_recipe_usage(recipe_id="r1", user_id="u1", usage_date="2024-01-02",
                          input_tokens=100, output_tokens=20, reasoning_tokens=0,
                          token_cost_usd=0.5)
    auth.add_recipe_usage(recipe_id="r1", user_id="u1", usage_date="2024-01-02",
                          input_tokens=50, output_tokens=10, reasoning_tokens=0,
                          token_cost_usd=0.25)
    row = auth.load_recipe_usage("r1", "2024-01-02")
    assert row["input_tokens"] == 150
    assert row["run_count"] == 2
    assert row["token_cost_usd"] == pytest.approx(0.75)


def test_recipe_usage_failure_counter():
    auth.add_recipe_usage(recipe_id="r1", user_id="u1", usage_date="2024-01-02",
                          input_tokens=0, output_tokens=0, reasoning_tokens=0,
                          token_cost_usd=0.0, failure=True)
    assert auth.load_recipe_usage("r1", "2024-01-02")["failure_count"] == 1


def test_user_spend_accumulates():
    auth.add_user_spend("u1", "2024-01-02", 1.5)
    auth.add_user_spend("u1", "2024-01-02", 2.0)
    assert auth.load_user_spend("u1", "2024-01-02") == pytest.approx(3.5)


def test_user_spend_missing_is_zero():
    assert auth.load_user_spend("nobody", "2024-01-02") == 0.0


# ---------------------------------------------------------------------------
# journal
# ---------------------------------------------------------------------------

def _entry(eid="j1", user="u1", **kw):
    base = {"id": eid, "user_id": user, "kind": "note", "body": "hello",
            "is_draft": False, "created_at": "2024-01-02T00:00:00Z"}
    base.update(kw)
    return base


def test_journal_save_load_delete():
    auth.save_journal_entry(_entry())
    assert auth.load_journal_entry("j1")["body"] == "hello"
    assert auth.delete_journal_entry("j1") is True
    assert auth.load_journal_entry("j1") is None


def test_journal_list_filters_drafts():
    auth.save_journal_entry(_entry("j1", is_draft=False))
    auth.save_journal_entry(_entry("j2", is_draft=True))
    assert len(auth.list_journal_entries("u1", include_drafts=True)) == 2
    assert len(auth.list_journal_entries("u1", include_drafts=False)) == 1


def test_journal_list_filter_by_kind():
    auth.save_journal_entry(_entry("j1", kind="note"))
    auth.save_journal_entry(_entry("j2", kind="reflection"))
    assert len(auth.list_journal_entries("u1", kind="reflection")) == 1


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

def test_audit_append_and_filtered_list():
    auth.append_audit(actor="system", action="streaming.fire",
                      target_user_id="u1", metadata={"symbol": "AAPL"})
    auth.append_audit(actor="system", action="other.thing",
                      target_user_id="u1", metadata={})
    fires = auth.list_audit(action="streaming.fire", target_user_id="u1")
    assert len(fires) == 1
    assert fires[0]["metadata"]["symbol"] == "AAPL"


# ---------------------------------------------------------------------------
# disagreement log (insert only — no list helper in auth)
# ---------------------------------------------------------------------------

def test_disagreement_insert_keyed_by_session():
    auth.insert_disagreement_log({"session_id": "s1", "user_id": "u1",
                                  "recipe_id": "r1", "similarity": 0.5})
    assert auth._memstore[("disagreement_log", "s1")]["recipe_id"] == "r1"


# ---------------------------------------------------------------------------
# compliance attestation
# ---------------------------------------------------------------------------

def test_active_version_falls_back_to_v1():
    assert auth.active_compliance_version() == "v1.0"


def test_attestation_save_load_and_latest():
    row = {"id": "a1", "user_id": "u1", "version": "v1.0",
           "ack_paper_only": True, "ack_not_advice": True, "ack_jurisdiction": True,
           "created_at": "2024-01-02T00:00:00Z", "revoked_at": None}
    auth.save_compliance_attestation(row)
    assert auth.load_compliance_attestation("a1")["version"] == "v1.0"
    assert auth.latest_active_attestation_for_user("u1")["id"] == "a1"


def test_latest_attestation_excludes_revoked_and_stale():
    auth.save_compliance_attestation({"id": "a1", "user_id": "u1", "version": "v1.0",
        "ack_paper_only": True, "ack_not_advice": True, "ack_jurisdiction": True,
        "created_at": "2024-01-02T00:00:00Z", "revoked_at": "2024-02-01T00:00:00Z"})
    auth.save_compliance_attestation({"id": "a2", "user_id": "u1", "version": "v0.9",
        "ack_paper_only": True, "ack_not_advice": True, "ack_jurisdiction": True,
        "created_at": "2024-01-03T00:00:00Z", "revoked_at": None})
    assert auth.latest_active_attestation_for_user("u1") is None


# ---------------------------------------------------------------------------
# transactions
# ---------------------------------------------------------------------------

def test_transactions_save_and_list():
    rows = [{"id": f"t{i}", "user_id": "u1", "batch_id": "b1", "symbol": "AAPL",
             "amount": -100.0, "created_at": f"2024-01-0{i+1}T00:00:00Z"}
            for i in range(3)]
    assert auth.save_transactions(rows) == 3
    assert len(auth.list_transactions("u1")) == 3
    assert len(auth.list_transactions("u1", batch_id="b1")) == 3
    assert len(auth.list_transactions("u1", batch_id="other")) == 0


# ---------------------------------------------------------------------------
# tier defaults
# ---------------------------------------------------------------------------

def test_tier_default_spend_caps_shape():
    caps = auth.tier_default_spend_caps("novice")
    assert "daily_spend_cap_usd" in caps and "monthly_spend_cap_usd" in caps
