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
