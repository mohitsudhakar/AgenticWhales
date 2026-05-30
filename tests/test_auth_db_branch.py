"""Coverage for web/auth.py's Supabase/Postgres branches (`if _db_writable()`).

These paths are normally skipped in unit tests (conftest forces offline). Here
we re-enable them by setting URL + service-key env and swapping `auth._http`
for a small in-process fake that emulates the slice of PostgREST + the
`/auth/v1/user` token endpoint that auth.py actually calls. No network.
"""

from __future__ import annotations

import json as _json
from urllib.parse import urlparse, parse_qs

import pytest

from web import auth


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text or ""

    def json(self):
        return self._payload


def _table_of(url: str) -> str:
    path = urlparse(url).path
    # .../rest/v1/<table>  OR  .../auth/v1/user
    return path.rsplit("/", 1)[-1]


def _filters(url: str, params):
    """Merge eq.* filters from the URL query and the params kwarg."""
    out = {}
    q = parse_qs(urlparse(url).query)
    for k, vals in q.items():
        if k in ("select", "order", "limit", "on_conflict"):
            continue
        v = vals[0]
        if v.startswith("eq."):
            out[k] = v[3:]
        elif v == "is.null":
            out[k] = None
    for k, v in (params or {}).items():
        if k in ("select", "order", "limit", "on_conflict"):
            continue
        if isinstance(v, str) and v.startswith("eq."):
            out[k] = v[3:]
    return out


def _qparam(url: str, params, name):
    q = parse_qs(urlparse(url).query)
    if name in q:
        return q[name][0]
    if params and name in params:
        return params[name]
    return None


class FakePostgrest:
    """Minimal stateful PostgREST + auth-user endpoint."""

    def __init__(self):
        self.tables = {}            # table -> list[row]
        self.user = {"id": "uid-1", "email": "trader@example.com"}
        self.fail = False           # flip to simulate 500s
        self.calls = []

    # -- helpers --
    def _rows(self, table):
        return self.tables.setdefault(table, [])

    def _match(self, row, filt):
        for k, v in filt.items():
            if row.get(k) != v:
                return False
        return True

    # -- transport surface --
    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        table = _table_of(url)
        if table == "user":  # /auth/v1/user token validation
            if self.fail:
                return _Resp(401, None, "unauthorized")
            return _Resp(200, dict(self.user))
        if self.fail:
            return _Resp(500, None, "boom")
        rows = [r for r in self._rows(table) if self._match(r, _filters(url, params))]
        order = _qparam(url, params, "order")
        if order:
            col = order.split(".")[0]
            desc = order.endswith(".desc")
            rows = sorted(rows, key=lambda r: r.get(col) or "", reverse=desc)
        limit = _qparam(url, params, "limit")
        if limit is not None:
            rows = rows[: int(limit)]
        return _Resp(200, rows)

    def post(self, url, headers=None, json=None, params=None, timeout=None):
        self.calls.append(("POST", url, json))
        if self.fail:
            return _Resp(500, None, "boom")
        table = _table_of(url)
        rows = self._rows(table)
        # auth.py posts the body as a one-element list: json=[row].
        body = json[0] if isinstance(json, list) else json
        on_conflict = _qparam(url, params, "on_conflict")
        keys = on_conflict.split(",") if on_conflict else ["id"]
        for i, r in enumerate(rows):
            if all(r.get(k) == body.get(k) for k in keys):
                rows[i] = dict(body)
                return _Resp(201, [dict(body)])
        rows.append(dict(body))
        return _Resp(201, [dict(body)])

    def patch(self, url, headers=None, json=None, params=None, timeout=None):
        self.calls.append(("PATCH", url, json))
        if self.fail:
            return _Resp(500, None, "boom")
        table = _table_of(url)
        filt = _filters(url, params)
        patched = []
        for r in self._rows(table):
            if self._match(r, filt):
                r.update(json or {})
                patched.append(dict(r))
        return _Resp(200, patched)

    def delete(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("DELETE", url, params))
        if self.fail:
            return _Resp(500, None, "boom")
        table = _table_of(url)
        filt = _filters(url, params)
        before = self._rows(table)
        after = [r for r in before if not self._match(r, filt)]
        self.tables[table] = after
        return _Resp(204)


@pytest.fixture
def db(monkeypatch):
    # conftest's autouse _force_offline_supabase deletes the Supabase env vars
    # (and may run after this fixture), so we can't rely on setenv. Instead
    # override the config getters + _db_writable directly, and swap the
    # transport for the in-process fake.
    monkeypatch.setattr(auth, "_supabase_url", lambda: "https://fake.supabase.co")
    monkeypatch.setattr(auth, "_supabase_anon_key", lambda: "anon-key")
    monkeypatch.setattr(auth, "_supabase_service_key", lambda: "service-key")
    monkeypatch.setattr(auth, "_supabase_configured", lambda: True)
    monkeypatch.setattr(auth, "_db_writable", lambda: True)
    fake = FakePostgrest()
    monkeypatch.setattr(auth, "_http", fake)
    auth._reset_memstore_for_tests()
    auth._token_cache.clear()
    assert auth._db_writable() is True
    yield fake
    auth._token_cache.clear()
    auth._reset_memstore_for_tests()


# ---------------------------------------------------------------------------
# low-level query helpers
# ---------------------------------------------------------------------------

def test_select_columns_roundtrip(db):
    db.tables["recipes"] = [
        {"id": "r1", "user_id": "u1", "status": "active", "created_at": "2024-01-02"},
        {"id": "r2", "user_id": "u1", "status": "paused", "created_at": "2024-01-03"},
        {"id": "r3", "user_id": "u2", "status": "active", "created_at": "2024-01-01"},
    ]
    rows = auth._select_columns("recipes", filters={"user_id": "u1"}, order="created_at.desc")
    assert [r["id"] for r in rows] == ["r2", "r1"]
    limited = auth._select_columns("recipes", filters={"user_id": "u1"}, limit=1)
    assert len(limited) == 1


def test_select_columns_error_returns_empty(db):
    db.fail = True
    assert auth._select_columns("recipes", filters={"user_id": "u1"}) == []


def test_select_one_hit_and_miss(db):
    # _select_one reads the `data` envelope (sessions/batches shape).
    db.tables["sessions"] = [{"id": "s1", "data": {"id": "s1", "user_id": "u1"}}]
    assert auth._select_one("sessions", "s1")["user_id"] == "u1"
    assert auth._select_one("sessions", "nope") is None


def test_select_for_user(db):
    db.tables["sessions"] = [
        {"id": "s1", "user_id": "u1", "created_at": "2024-01-01",
         "data": {"id": "s1", "user_id": "u1"}},
        {"id": "s2", "user_id": "u2", "created_at": "2024-01-02",
         "data": {"id": "s2", "user_id": "u2"}},
    ]
    rows = auth._select_for_user("sessions", "u1")
    assert [r["id"] for r in rows] == ["s1"]


def test_upsert_and_delete_one(db):
    # _upsert wraps via the `data` envelope; _select_one unwraps it.
    auth._upsert("sessions", {"id": "s1", "user_id": "u1",
                              "data": {"id": "s1", "status": "running"}})
    assert auth._select_one("sessions", "s1")["status"] == "running"
    auth._upsert("sessions", {"id": "s1", "user_id": "u1",
                              "data": {"id": "s1", "status": "done"}})
    assert len(db.tables["sessions"]) == 1
    assert auth._delete_one("sessions", "s1") is True
    assert auth._select_one("sessions", "s1") is None


def test_delete_where(db):
    db.tables["journal_entries"] = [{"id": "j1", "user_id": "u1"},
                                    {"id": "j2", "user_id": "u1"}]
    assert auth._delete_where("journal_entries", {"id": "j1"}) is True
    assert len(db.tables["journal_entries"]) == 1


# ---------------------------------------------------------------------------
# public storage funcs through the DB branch
# ---------------------------------------------------------------------------

def test_save_session_writes_db(db):
    auth.save_session({"id": "s1", "user_id": "u1", "ticker": "AAPL",
                       "analysis_date": "2024-01-02", "status": "completed",
                       "created_at": 1_700_000_000.0})
    # Row landed in the DB table (not just memstore).
    assert any(r["id"] == "s1" for r in db.tables.get("sessions", []))
    assert auth.load_session("s1")["ticker"] == "AAPL"


def test_save_session_skips_db_for_anonymous(db):
    auth.save_session({"id": "s9", "user_id": auth.ANONYMOUS_USER_ID,
                       "ticker": "AAPL", "status": "completed"})
    assert db.tables.get("sessions", []) == []  # anon never hits the DB


def test_recipe_db_roundtrip(db):
    # _recipe_row projects the full columnar shape, so supply every required key.
    auth.save_recipe({"id": "r1", "user_id": "u1", "name": "R1", "status": "active",
                      "tickers": ["AAPL"], "analysts": ["market"],
                      "llm_provider": "google", "quick_model": "q", "deep_model": "d",
                      "bull_model": "x", "bear_model": "y", "created_at": "2024-01-01"})
    assert auth.load_recipe("r1")["status"] == "active"
    auth.update_recipe_status("r1", "killed")
    assert auth.load_recipe("r1")["status"] == "killed"
    assert auth.list_recipes("u1")[0]["id"] == "r1"


def test_paper_account_and_positions_db(db):
    auth.upsert_paper_account(user_id="u1", cash=100000.0, starting_cash=100000.0)
    assert auth.load_paper_account("u1")["cash"] == 100000.0
    auth.upsert_paper_position(user_id="u1", ticker="AAPL", qty=10, avg_cost=150.0)
    assert auth.list_paper_positions("u1")[0]["ticker"] == "AAPL"


def test_risk_limits_db_merge(db):
    auth.upsert_risk_limits("u1", max_position_pct=0.2)
    merged = auth.upsert_risk_limits("u1", kill_switch=True)
    assert merged["max_position_pct"] == 0.2 and merged["kill_switch"] is True
    assert auth.load_risk_limits("u1")["kill_switch"] is True


def test_journal_db_roundtrip(db):
    auth.save_journal_entry({"id": "j1", "user_id": "u1", "kind": "note",
                             "body": "hi", "is_draft": False,
                             "created_at": "2024-01-02"})
    assert auth.load_journal_entry("j1")["body"] == "hi"
    assert len(auth.list_journal_entries("u1")) == 1
    assert auth.delete_journal_entry("j1") is True


def test_transactions_db_roundtrip(db):
    rows = [{"id": "t1", "user_id": "u1", "batch_id": "b1", "symbol": "AAPL",
             "amount": -100.0, "created_at": "2024-01-02"}]
    assert auth.save_transactions(rows) == 1
    assert len(auth.list_transactions("u1")) == 1


def test_audit_db_roundtrip(db):
    auth.append_audit(actor="system", action="streaming.fire",
                      target_user_id="u1", metadata={"symbol": "AAPL"})
    rows = auth.list_audit(action="streaming.fire", target_user_id="u1")
    assert len(rows) == 1 and rows[0]["metadata"]["symbol"] == "AAPL"


# ---------------------------------------------------------------------------
# token validation / auth dependencies
# ---------------------------------------------------------------------------

def test_fetch_user_success_and_cache(db):
    res = auth._fetch_user("tok-1")
    assert res == ("uid-1", "trader@example.com")
    # Second call served from cache even if transport now fails.
    db.fail = True
    assert auth._fetch_user("tok-1") == ("uid-1", "trader@example.com")


def test_fetch_user_unauthorized(db):
    db.fail = True
    assert auth._fetch_user("bad") is None


def test_validate_token(db):
    assert auth._validate_token("tok-1") == "uid-1"


def test_get_current_user_id_with_valid_bearer(db):
    assert auth.get_current_user_id("Bearer tok-1") == "uid-1"


def test_get_current_user_id_missing_header_401(db):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        auth.get_current_user_id(None)
    assert e.value.status_code == 401


def test_get_current_user_id_bad_token_401(db):
    from fastapi import HTTPException
    db.fail = True
    with pytest.raises(HTTPException) as e:
        auth.get_current_user_id("Bearer nope")
    assert e.value.status_code == 401


def test_require_admin_allows_admin_email(db, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_EMAIL", "trader@example.com")
    assert auth.require_admin("Bearer tok-1") == "uid-1"


def test_require_admin_rejects_non_admin(db, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_EMAIL", "someone-else@example.com")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        auth.require_admin("Bearer tok-1")
    assert e.value.status_code == 403


def test_require_admin_missing_header_401(db):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        auth.require_admin(None)
    assert e.value.status_code == 401


# ---------------------------------------------------------------------------
# authenticate_websocket (async)
# ---------------------------------------------------------------------------

class _FakeWS:
    def __init__(self):
        self.closed = None

    async def close(self, code=None):
        self.closed = code


def test_authenticate_websocket_valid(db):
    import asyncio
    ws = _FakeWS()
    assert asyncio.run(auth.authenticate_websocket(ws, "tok-1")) == "uid-1"
    assert ws.closed is None


def test_authenticate_websocket_missing_token_closes(db):
    import asyncio
    ws = _FakeWS()
    assert asyncio.run(auth.authenticate_websocket(ws, None)) is None
    assert ws.closed == 4401


def test_authenticate_websocket_bad_token_closes(db):
    import asyncio
    db.fail = True
    ws = _FakeWS()
    assert asyncio.run(auth.authenticate_websocket(ws, "nope")) is None
    assert ws.closed == 4401


# ---------------------------------------------------------------------------
# find_cached_session
# ---------------------------------------------------------------------------

def _recent_iso(minutes_ago=1):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(tz=timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def test_find_cached_session_hit_and_sig_miss(db):
    db.tables["sessions"] = [
        {"id": "s1", "user_id": "u1", "ticker": "AAPL", "analysis_date": "2024-01-02",
         "status": "completed", "completed_at": _recent_iso(),
         "data": {"id": "s1", "config": {"__sig": "sigA"}, "ticker": "AAPL"}},
    ]
    hit = auth.find_cached_session("u1", "AAPL", "2024-01-02", "sigA")
    assert hit is not None and hit["id"] == "s1"
    # different signature → no match
    assert auth.find_cached_session("u1", "AAPL", "2024-01-02", "sigB") is None


def test_find_cached_session_anonymous_returns_none(db):
    assert auth.find_cached_session(auth.ANONYMOUS_USER_ID, "AAPL", "2024-01-02", "x") is None
    assert auth.find_cached_session("", "AAPL", "2024-01-02", "x") is None


def test_find_cached_session_error_returns_none(db):
    db.fail = True
    assert auth.find_cached_session("u1", "AAPL", "2024-01-02", "x") is None


# ---------------------------------------------------------------------------
# save_batch DB branch
# ---------------------------------------------------------------------------

def test_save_batch_writes_db(db):
    auth.save_batch({"id": "b1", "user_id": "u1", "analysis_date": "2024-01-02",
                     "status": "completed", "items": [{"ticker": "AAPL"}],
                     "completed_at": 1_700_000_000.0,
                     "config": {"quick_think_llm": "q", "deep_think_llm": "d"},
                     "totals": {"tokens_in": 5}})
    assert any(r["id"] == "b1" for r in db.tables.get("batches", []))
    assert auth.load_batch("b1")["status"] == "completed"
    assert auth.list_batches("u1")[0]["id"] == "b1"


def test_save_batch_anonymous_memstore_only(db):
    auth.save_batch({"id": "b9", "user_id": auth.ANONYMOUS_USER_ID, "items": []})
    assert db.tables.get("batches", []) == []


# ---------------------------------------------------------------------------
# admin_list_* (service-role reads)
# ---------------------------------------------------------------------------

def test_admin_list_sessions_and_batches(db):
    db.tables["sessions"] = [{"user_id": "u1", "ticker": "AAPL", "status": "completed",
                              "created_at": "2024-01-02"}]
    db.tables["batches"] = [{"user_id": "u1", "status": "completed",
                             "created_at": "2024-01-02"}]
    assert auth.admin_list_sessions()[0]["ticker"] == "AAPL"
    assert auth.admin_list_batches()[0]["status"] == "completed"


def test_admin_list_profiles(db):
    db.tables["profiles"] = [{"id": "u1", "username": "alice", "tier": "master",
                              "created_at": "2023-01-01"}]
    assert auth.admin_list_profiles()[0]["username"] == "alice"


def test_admin_list_table_error_returns_empty(db):
    db.fail = True
    assert auth.admin_list_sessions() == []
    assert auth.admin_list_profiles() == []


def test_admin_list_users_paginates(db):
    db.tables["users"] = [{"id": "u1", "email": "a@x.com"},
                          {"id": "u2", "email": "b@x.com"}]
    users = auth.admin_list_users()
    assert {u["id"] for u in users} == {"u1", "u2"}


def test_admin_list_users_error_breaks(db):
    db.fail = True
    assert auth.admin_list_users() == []


# ---------------------------------------------------------------------------
# pricing reads
# ---------------------------------------------------------------------------

def test_fetch_price_row_hit(db):
    db.tables["llm_pricing"] = [{
        "provider": "google", "model": "gemini", "input_per_1m_usd": "1.25",
        "output_per_1m_usd": "5.0", "cache_read_per_1m_usd": None,
        "reasoning_per_1m_usd": None, "effective_at": "2024-01-01T00:00:00+00:00",
        "source_url": "http://x",
    }]
    row = auth.fetch_price_row("Google", "gemini")
    assert row is not None and str(row.input_per_1m) == "1.25"


def test_fetch_price_row_no_rows(db):
    assert auth.fetch_price_row("google", "missing") is None


def test_list_priced_models(db):
    db.tables["llm_pricing"] = [
        {"provider": "google", "model": "g1"},
        {"provider": "openai", "model": "o1"},
        {"provider": "google", "model": "g1"},  # dup collapses
    ]
    assert auth.list_priced_models() == [("google", "g1"), ("openai", "o1")]


# ---------------------------------------------------------------------------
# paper_place_order RPC
# ---------------------------------------------------------------------------

def test_call_paper_place_order_rpc_success(db):
    out = auth.call_paper_place_order_rpc({"p_user": "u1", "p_ticker": "AAPL"})
    assert out is not None  # fake echoes the body back


def test_call_paper_place_order_rpc_transport_error(db):
    db.fail = True
    assert auth.call_paper_place_order_rpc({"p_user": "u1"}) is None


def test_call_paper_place_order_rpc_404(db, monkeypatch):
    monkeypatch.setattr(auth._http, "post",
                        lambda *a, **k: _Resp(404, None, "missing"))
    assert auth.call_paper_place_order_rpc({"p_user": "u1"}) is None


# ---------------------------------------------------------------------------
# recipe failure counters
# ---------------------------------------------------------------------------

def test_touch_recipe_last_run_patches(db):
    db.tables["recipes"] = [{"id": "r1", "user_id": "u1", "last_run_at": None}]
    auth.touch_recipe_last_run("r1", 1_700_000_000.0)
    assert db.tables["recipes"][0]["last_run_at"] is not None


def test_bump_and_reset_recipe_failures(db):
    db.tables["recipes"] = [{"id": "r1", "user_id": "u1", "name": "R", "status": "active",
                             "consecutive_failures": 0, "tickers": ["AAPL"],
                             "analysts": ["market"], "llm_provider": "google",
                             "quick_model": "q", "deep_model": "d", "bull_model": "x",
                             "bear_model": "y", "created_at": "2024-01-01"}]
    assert auth.bump_recipe_failures("r1") == 1
    assert db.tables["recipes"][0]["consecutive_failures"] == 1
    auth.reset_recipe_failures("r1")
    assert db.tables["recipes"][0]["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# paper orders / conviction scores / disagreement DB reads
# ---------------------------------------------------------------------------

def test_list_paper_orders_db(db):
    db.tables["paper_orders"] = [
        {"id": "o1", "user_id": "u1", "recipe_id": "rec1", "created_at": "2024-01-02"},
        {"id": "o2", "user_id": "u1", "recipe_id": None, "created_at": "2024-01-03"},
    ]
    assert len(auth.list_paper_orders("u1")) == 2
    assert [o["id"] for o in auth.list_paper_orders("u1", recipe_id="rec1")] == ["o1"]


def test_list_conviction_scores_db(db):
    db.tables["conviction_scores"] = [
        {"id": "c1", "user_id": "u1", "ticker": "AAPL", "recorded_at": "2024-01-02"},
    ]
    assert auth.list_conviction_scores("u1", ticker="aapl")[0]["ticker"] == "AAPL"


def test_insert_disagreement_log_db(db):
    auth.insert_disagreement_log({"session_id": "s1", "user_id": "u1", "spread": 0.4})
    assert any(r.get("session_id") == "s1"
               for r in db.tables.get("disagreement_log", []))


# ---------------------------------------------------------------------------
# compliance version + attestation DB reads
# ---------------------------------------------------------------------------

def test_active_compliance_version_db(db):
    db.tables["compliance_active_version"] = [{"id": "1", "version": "2026-01"}]
    assert auth.active_compliance_version() == "2026-01"


def test_latest_active_attestation_db(db):
    db.tables["compliance_active_version"] = [{"id": "1", "version": "2026-01"}]
    db.tables["compliance_attestations"] = [{
        "id": "a1", "user_id": "u1", "version": "2026-01", "revoked_at": None,
        "ack_paper_only": True, "ack_not_advice": True, "ack_jurisdiction": True,
        "created_at": "2024-01-02",
    }]
    att = auth.latest_active_attestation_for_user("u1")
    assert att is not None and att["id"] == "a1"


def test_latest_active_attestation_none_when_revoked(db):
    db.tables["compliance_active_version"] = [{"id": "1", "version": "2026-01"}]
    db.tables["compliance_attestations"] = [{
        "id": "a1", "user_id": "u1", "version": "2026-01", "revoked_at": "2024-02-01",
        "ack_paper_only": True, "ack_not_advice": True, "ack_jurisdiction": True,
        "created_at": "2024-01-02",
    }]
    assert auth.latest_active_attestation_for_user("u1") is None


# ---------------------------------------------------------------------------
# stuck-session reaper / mark_session_failed
# ---------------------------------------------------------------------------

def test_list_stuck_running_sessions_db(db):
    db.tables["sessions"] = [
        {"id": "s1", "user_id": "u1", "status": "running", "created_at": "2020-01-01"},
        {"id": "s2", "user_id": "u1", "status": "completed", "created_at": "2020-01-01"},
    ]
    stuck = auth.list_stuck_running_sessions()
    assert [r["id"] for r in stuck] == ["s1"]


def test_list_stuck_running_sessions_error(db):
    db.fail = True
    assert auth.list_stuck_running_sessions() == []


def test_delete_stuck_running_sessions_db(db):
    db.tables["sessions"] = [
        {"id": "s1", "user_id": "u1", "status": "running", "created_at": "2020-01-01"},
        {"id": "s2", "user_id": "u1", "status": "pending", "created_at": "2020-01-01"},
    ]
    n = auth.delete_stuck_running_sessions()
    assert n == 2
    assert db.tables["sessions"] == []


def test_mark_session_failed_db(db):
    db.tables["sessions"] = [{"id": "s1", "user_id": "u1", "status": "running",
                              "data": {"id": "s1"}}]
    assert auth.mark_session_failed("s1", failure_reason="timeout") is True


# ---------------------------------------------------------------------------
# _upsert PGRST204 retry (column missing from schema cache → strip + retry)
# ---------------------------------------------------------------------------

def test_upsert_strips_missing_column_on_pgrst204(db, monkeypatch):
    calls = {"n": 0}

    def _post(url, headers=None, json=None, params=None, timeout=None):
        calls["n"] += 1
        body = json[0]
        if "ghost_col" in body:
            return _Resp(400, {"code": "PGRST204",
                               "message": "Could not find the 'ghost_col' column"},
                         "schema cache miss")
        db._rows("widgets").append(dict(body))
        return _Resp(201, [dict(body)])

    monkeypatch.setattr(db, "post", _post)
    auth._upsert("widgets", {"id": "w1", "name": "ok", "ghost_col": "drop me"})
    # retried without the ghost column and succeeded
    assert calls["n"] == 2
    assert db.tables["widgets"][0] == {"id": "w1", "name": "ok"}


# ---------------------------------------------------------------------------
# _fetch_user edge cases
# ---------------------------------------------------------------------------

def test_fetch_user_request_exception_returns_none(db, monkeypatch):
    import requests as _rq

    def _boom(*a, **k):
        raise _rq.RequestException("network down")
    monkeypatch.setattr(db, "get", _boom)
    auth._token_cache.clear()
    assert auth._fetch_user("tok-x") is None


def test_fetch_user_no_uid_returns_none(db, monkeypatch):
    monkeypatch.setattr(db, "get",
                        lambda *a, **k: _Resp(200, {"email": "x@y.com"}))  # no id
    auth._token_cache.clear()
    assert auth._fetch_user("tok-y") is None


# ---------------------------------------------------------------------------
# low-level helper error paths (transport 500)
# ---------------------------------------------------------------------------

def test_select_one_error_returns_none(db):
    db.fail = True
    assert auth._select_one("sessions", "s1") is None


def test_select_for_user_error_returns_empty(db):
    db.fail = True
    assert auth._select_for_user("sessions", "u1") == []


def test_delete_one_error_returns_false(db):
    db.fail = True
    assert auth._delete_one("sessions", "s1") is False


def test_delete_where_error_returns_false(db):
    db.fail = True
    assert auth._delete_where("journal_entries", {"id": "j1"}) is False


def test_admin_list_users_non_200_breaks(db, monkeypatch):
    monkeypatch.setattr(db, "get", lambda *a, **k: _Resp(403, None))
    assert auth.admin_list_users() == []


def test_list_stuck_running_sessions_db_non_200(db, monkeypatch):
    monkeypatch.setattr(db, "get", lambda *a, **k: _Resp(500, None, "boom"))
    assert auth.list_stuck_running_sessions() == []


def test_delete_stuck_running_sessions_db_list_error(db, monkeypatch):
    monkeypatch.setattr(db, "get", lambda *a, **k: _Resp(500, None, "boom"))
    # list fails → no rows pulled; memstore sweep also empty → 0 deleted
    assert auth.delete_stuck_running_sessions() == 0


def test_fetch_price_row_non_200(db, monkeypatch):
    monkeypatch.setattr(db, "get", lambda *a, **k: _Resp(404, None))
    assert auth.fetch_price_row("google", "gemini") is None


def test_find_cached_session_request_exception(db, monkeypatch):
    import requests as _rq

    def _boom(*a, **k):
        raise _rq.RequestException("net")
    monkeypatch.setattr(db, "get", _boom)
    assert auth.find_cached_session("u1", "AAPL", "2024-01-02", "sig") is None
