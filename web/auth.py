"""Supabase auth + Postgres storage for sessions/batches.

Two responsibilities:
  1. Validate a Supabase JWT (access token) and return the user id (UUID)
     of the caller, via Supabase's /auth/v1/user endpoint. Saves the JWT
     secret config and a `pyjwt` dep at the cost of one HTTP round trip
     per request (cached for 60s in-process to soften that).
  2. Read/write session and batch rows in Supabase Postgres using the
     service_role key. Postgres is the source of truth — no JSON files
     on disk anymore. RLS still protects user data on the read path
     because every server endpoint validates the JWT and filters by uid.

If Supabase isn't configured at all, the storage helpers degrade to in-
memory (process-lifetime) state so local dev works without a database;
get_current_user_id falls back to a shared "anonymous" bucket.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from fastapi import Header, HTTPException, WebSocket

log = logging.getLogger(__name__)

ANONYMOUS_USER_ID = "anonymous"

# Admin email used to gate the /api/admin/* endpoints (usage dashboard).
# Compared case-insensitively against the Supabase user's email. Override
# via env if a different operator should hold the keys.
ADMIN_EMAIL = (os.getenv("AGENTICWHALES_ADMIN_EMAIL") or "mohit.sudhakar@gmail.com").strip().lower()

# Module-level requests session = HTTP keepalive across calls. Saves a
# fresh TCP+TLS handshake on every save during a multi-agent run.
_http = requests.Session()

# Token-validation cache — Supabase tokens last ~1h, this is purely to
# avoid hitting /auth/v1/user on every API call from the same client.
# Stores (expiry, uid, email) so the admin gate can check email without
# a second round trip.
_TOKEN_CACHE_TTL = 60.0
_token_cache: Dict[str, tuple[float, str, Optional[str]]] = {}

# In-memory fallback for when Supabase isn't configured. Keyed by
# (table, id) so sessions and batches don't collide. Process-local —
# wipes on restart.
_memstore: Dict[tuple[str, str], Dict[str, Any]] = {}


# ------------------------------------------------------------------
# env / config
# ------------------------------------------------------------------

def _supabase_url() -> Optional[str]:
    return os.getenv("AGENTICWHALES_SUPABASE_URL")


def _supabase_anon_key() -> Optional[str]:
    return os.getenv("AGENTICWHALES_SUPABASE_ANON_KEY")


def _supabase_service_key() -> Optional[str]:
    return os.getenv("AGENTICWHALES_SUPABASE_SERVICE_KEY")


def _supabase_configured() -> bool:
    return bool(_supabase_url() and _supabase_anon_key())


def _db_writable() -> bool:
    """Postgres CRUD requires the service-role key. Without it we fall back
    to the in-memory store so the rest of the app still works."""
    return bool(_supabase_url() and _supabase_service_key())


# ------------------------------------------------------------------
# JWT validation
# ------------------------------------------------------------------

def _fetch_user(token: str) -> Optional[tuple[str, Optional[str]]]:
    """Resolve a Supabase JWT to (uid, email). Cached for 60s. Returns None
    when Supabase isn't configured or the token is invalid/expired."""
    cached = _token_cache.get(token)
    if cached and cached[0] > time.time():
        return cached[1], cached[2]
    base = _supabase_url()
    anon = _supabase_anon_key()
    if not base or not anon:
        return None
    try:
        resp = _http.get(
            f"{base}/auth/v1/user",
            headers={"apikey": anon, "Authorization": f"Bearer {token}"},
            timeout=5,
        )
    except requests.RequestException as e:
        log.warning("auth: Supabase /auth/v1/user request failed: %s", e)
        return None
    if resp.status_code != 200:
        return None
    body = resp.json() or {}
    uid = body.get("id")
    if not uid:
        return None
    email = body.get("email")
    _token_cache[token] = (time.time() + _TOKEN_CACHE_TTL, uid, email)
    return uid, email


def _validate_token(token: str) -> Optional[str]:
    res = _fetch_user(token)
    return res[0] if res else None


def get_current_user_id(authorization: Optional[str] = Header(None)) -> str:
    """FastAPI dependency. Validates the Authorization: Bearer <jwt> header
    via Supabase and returns the user id. If Supabase isn't configured at
    all, falls back to the shared 'anonymous' user so local dev still works."""
    if not _supabase_configured():
        return ANONYMOUS_USER_ID
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing Authorization bearer token")
    token = authorization.split(None, 1)[1].strip()
    uid = _validate_token(token)
    if not uid:
        raise HTTPException(401, "Invalid or expired auth token")
    return uid


def require_admin(authorization: Optional[str] = Header(None)) -> str:
    """FastAPI dependency for admin-only endpoints. Returns the admin user's
    id. 401 if the token is missing/invalid; 403 if the authed user's email
    doesn't match ADMIN_EMAIL.

    Refuses to authorize when Supabase isn't configured — the admin gate
    relies on real auth and the 'anonymous' fallback has no identity."""
    if not _supabase_configured():
        raise HTTPException(403, "Admin dashboard requires Supabase auth")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing Authorization bearer token")
    token = authorization.split(None, 1)[1].strip()
    res = _fetch_user(token)
    if not res:
        raise HTTPException(401, "Invalid or expired auth token")
    uid, email = res
    if not email or email.strip().lower() != ADMIN_EMAIL:
        raise HTTPException(403, "Admin only")
    return uid


async def authenticate_websocket(ws: WebSocket, token: Optional[str]) -> Optional[str]:
    """Validate a WebSocket's ?token=... param. Closes the socket and returns
    None on failure. Returns 'anonymous' when Supabase isn't configured."""
    if not _supabase_configured():
        return ANONYMOUS_USER_ID
    if not token:
        await ws.close(code=4401)
        return None
    uid = _validate_token(token)
    if not uid:
        await ws.close(code=4401)
        return None
    return uid


# ------------------------------------------------------------------
# Postgres CRUD (service_role)
# ------------------------------------------------------------------

def _rest_url(table: str) -> str:
    return f"{_supabase_url()}/rest/v1/{table}"


def _service_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    key = _supabase_service_key()
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _ts_iso(epoch: Optional[float]) -> Optional[str]:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _upsert(table: str, row: Dict[str, Any]) -> None:
    if not _db_writable():
        return
    headers = _service_headers({"Prefer": "resolution=merge-duplicates,return=minimal"})
    try:
        resp = _http.post(_rest_url(table), headers=headers, json=[row], timeout=10)
        # PGRST204 = column in the payload isn't in the schema cache. This
        # happens whenever a Postgres migration was added in code but never
        # applied to the live database. Rather than dropping the whole row,
        # strip the missing column from the payload and retry once. Any
        # further drift will surface the next column and retry again, capped
        # by the number of columns in the row.
        retries = 0
        while resp.status_code == 400 and retries < 8:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if body.get("code") != "PGRST204":
                break
            col = _missing_column_from_pgrst204(body.get("message") or "")
            if not col or col not in row:
                break
            log.warning(
                "supabase upsert %s: column %r missing from schema cache; "
                "retrying without it (PGRST204)", table, col,
            )
            row = {k: v for k, v in row.items() if k != col}
            resp = _http.post(_rest_url(table), headers=headers, json=[row], timeout=10)
            retries += 1
        if resp.status_code >= 300:
            log.warning("supabase upsert %s -> %s: %s", table, resp.status_code, resp.text[:200])
    except requests.RequestException as e:
        log.warning("supabase upsert %s failed: %s", table, e)


_PGRST204_COL_RE = re.compile(r"Could not find the '([^']+)' column", re.I)


def _missing_column_from_pgrst204(message: str) -> Optional[str]:
    m = _PGRST204_COL_RE.search(message or "")
    return m.group(1) if m else None


def _select_one(table: str, row_id: str) -> Optional[Dict[str, Any]]:
    if not _db_writable():
        return None
    try:
        resp = _http.get(
            f"{_rest_url(table)}?id=eq.{row_id}&select=data",
            headers=_service_headers(),
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning("supabase select %s -> %s", table, resp.status_code)
            return None
        rows = resp.json()
        if not rows:
            return None
        return rows[0].get("data")
    except requests.RequestException as e:
        log.warning("supabase select %s failed: %s", table, e)
        return None


def _select_for_user(table: str, user_id: str) -> List[Dict[str, Any]]:
    if not _db_writable():
        return []
    try:
        resp = _http.get(
            f"{_rest_url(table)}?user_id=eq.{user_id}&select=data&order=created_at.desc",
            headers=_service_headers(),
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning("supabase list %s -> %s: %s", table, resp.status_code, resp.text[:200])
            return []
        return [r["data"] for r in resp.json() if r.get("data")]
    except requests.RequestException as e:
        log.warning("supabase list %s failed: %s", table, e)
        return []


def _delete_one(table: str, row_id: str) -> bool:
    if not _db_writable():
        return False
    try:
        resp = _http.delete(
            f"{_rest_url(table)}?id=eq.{row_id}",
            headers=_service_headers(),
            timeout=10,
        )
        return resp.status_code < 300
    except requests.RequestException as e:
        log.warning("supabase delete %s failed: %s", table, e)
        return False


# ------------------------------------------------------------------
# Public storage interface — used by web/storage.py + web/batch_storage.py
# ------------------------------------------------------------------

def _stats_columns(stats: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Pull the denormalised counters out of a `stats` blob. Robust to missing
    keys / non-numeric values so a partial blob never breaks the upsert."""
    s = stats or {}
    def _i(k: str) -> int:
        try:
            return int(s.get(k) or 0)
        except (TypeError, ValueError):
            return 0
    return {
        "tokens_in":  _i("tokens_in"),
        "tokens_out": _i("tokens_out"),
        "llm_calls":  _i("llm_calls"),
        "tool_calls": _i("tool_calls"),
    }


def save_session(session: Dict[str, Any]) -> None:
    user_id = session.get("user_id")
    # Always write through to _memstore — when Supabase rejects the upsert
    # (schema drift, network blip), the session would otherwise vanish
    # mid-request. _memstore is the read path's fallback so this keeps the
    # process consistent even when the DB is out of sync.
    _memstore[("sessions", session["id"])] = session
    if not _db_writable() or not user_id or user_id == ANONYMOUS_USER_ID:
        return
    cfg = session.get("config") or {}
    row = {
        "id": session["id"],
        "user_id": user_id,
        "ticker": session.get("ticker"),
        "analysis_date": session.get("analysis_date"),
        "status": session.get("status"),
        "completed_at": _ts_iso(session.get("completed_at")),
        "quick_model": cfg.get("quick_think_llm"),
        "deep_model": cfg.get("deep_think_llm"),
        # PR-2: persist the compliance attestation id alongside the
        # session. None is the historical (pre-PR-2) value; new ad-hoc and
        # recipe-spawned sessions always carry an id once the migration
        # has run.
        "compliance_attestation_id": session.get("compliance_attestation_id"),
        "data": session,
    }
    row.update(_stats_columns(session.get("stats")))
    _upsert("sessions", row)


def load_session(session_id: str) -> Optional[Dict[str, Any]]:
    if _db_writable():
        row = _select_one("sessions", session_id)
        if row is not None:
            return row
    return _memstore.get(("sessions", session_id))


def list_sessions(user_id: str) -> List[Dict[str, Any]]:
    if _db_writable() and user_id and user_id != ANONYMOUS_USER_ID:
        return _select_for_user("sessions", user_id)
    return [
        s for (table, _), s in _memstore.items()
        if table == "sessions" and s.get("user_id") == user_id
    ]


def delete_session(session_id: str) -> bool:
    _memstore.pop(("sessions", session_id), None)
    if not _db_writable():
        return True
    return _delete_one("sessions", session_id)


def find_cached_session(
    user_id: str,
    ticker: str,
    analysis_date: str,
    config_sig: str,
    ttl_minutes: int = 30,
) -> Optional[Dict[str, Any]]:
    """Return the most recently completed session for this user matching
    (ticker, analysis_date, config_sig) within the last ttl_minutes, or
    None if there isn't one.

    config_sig is an opaque string that distinguishes meaningfully different
    runs (provider/models/depth/analysts/language). Different signatures
    don't match — running AAPL with deep=Gemini-Pro is not the same analysis
    as AAPL with deep=DeepSeek-V4.
    """
    if not user_id or user_id == ANONYMOUS_USER_ID:
        return None
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=ttl_minutes)
    cutoff_iso = cutoff.isoformat()

    if _db_writable():
        try:
            params = (
                f"user_id=eq.{user_id}"
                f"&ticker=eq.{ticker}"
                f"&analysis_date=eq.{analysis_date}"
                f"&status=eq.completed"
                f"&completed_at=gte.{cutoff_iso}"
                f"&order=completed_at.desc"
                f"&limit=5"
                f"&select=data"
            )
            resp = _http.get(
                f"{_rest_url('sessions')}?{params}",
                headers=_service_headers(),
                timeout=10,
            )
            if resp.status_code != 200:
                log.warning("supabase cache lookup -> %s: %s", resp.status_code, resp.text[:200])
                return None
            for row in resp.json():
                data = row.get("data") or {}
                if (data.get("config") or {}).get("__sig") == config_sig:
                    return data
            return None
        except requests.RequestException as e:
            log.warning("supabase cache lookup failed: %s", e)
            return None

    # In-memory fallback — scan _memstore.
    for (table, _), sess in _memstore.items():
        if table != "sessions":
            continue
        if sess.get("user_id") != user_id:
            continue
        if sess.get("ticker") != ticker or sess.get("analysis_date") != analysis_date:
            continue
        if sess.get("status") != "completed":
            continue
        completed_at = sess.get("completed_at")
        if not completed_at:
            continue
        completed_dt = datetime.fromtimestamp(completed_at, tz=timezone.utc)
        if completed_dt < cutoff:
            continue
        if (sess.get("config") or {}).get("__sig") == config_sig:
            return sess
    return None


def save_batch(batch: Dict[str, Any]) -> None:
    user_id = batch.get("user_id")
    if not _db_writable() or not user_id or user_id == ANONYMOUS_USER_ID:
        _memstore[("batches", batch["id"])] = batch
        return
    cfg = batch.get("config") or {}
    row = {
        "id": batch["id"],
        "user_id": user_id,
        "analysis_date": batch.get("analysis_date"),
        "status": batch.get("status"),
        "ticker_count": len(batch.get("items", [])),
        "completed_at": _ts_iso(batch.get("completed_at")),
        "quick_model": cfg.get("quick_think_llm"),
        "deep_model": cfg.get("deep_think_llm"),
        "data": batch,
    }
    # `totals` is the basket-wide aggregate that batch_runner maintains as
    # children complete; same shape as the per-session `stats`.
    row.update(_stats_columns(batch.get("totals")))
    _upsert("batches", row)


def load_batch(batch_id: str) -> Optional[Dict[str, Any]]:
    if _db_writable():
        row = _select_one("batches", batch_id)
        if row is not None:
            return row
    return _memstore.get(("batches", batch_id))


def list_batches(user_id: str) -> List[Dict[str, Any]]:
    if _db_writable() and user_id and user_id != ANONYMOUS_USER_ID:
        return _select_for_user("batches", user_id)
    return [
        b for (table, _), b in _memstore.items()
        if table == "batches" and b.get("user_id") == user_id
    ]


def delete_batch(batch_id: str) -> bool:
    _memstore.pop(("batches", batch_id), None)
    if not _db_writable():
        return True
    return _delete_one("batches", batch_id)


# ------------------------------------------------------------------
# Admin-scope reads — used by the usage dashboard only. Service-role
# bypasses RLS so we deliberately keep these behind require_admin.
# ------------------------------------------------------------------

# Postgres default row limit per PostgREST request. We pull up to 10k
# sessions / batches for the dashboard; beyond that the dashboard would
# need server-side aggregation (a SQL view or RPC).
_ADMIN_MAX_ROWS = 10000


def _admin_list_table(table: str, columns: str) -> List[Dict[str, Any]]:
    if not _db_writable():
        return []
    try:
        resp = _http.get(
            f"{_rest_url(table)}?select={columns}&order=created_at.desc&limit={_ADMIN_MAX_ROWS}",
            headers=_service_headers(),
            timeout=20,
        )
        if resp.status_code != 200:
            log.warning("admin list %s -> %s: %s", table, resp.status_code, resp.text[:200])
            return []
        return resp.json() or []
    except requests.RequestException as e:
        log.warning("admin list %s failed: %s", table, e)
        return []


def admin_list_sessions() -> List[Dict[str, Any]]:
    """Slim per-session rows across all users for the dashboard. When Supabase
    isn't configured, returns whatever the in-memory store has."""
    if _db_writable():
        return _admin_list_table(
            "sessions",
            "user_id,ticker,status,created_at,completed_at,tokens_in,tokens_out,llm_calls,tool_calls,quick_model,deep_model",
        )
    rows: List[Dict[str, Any]] = []
    for (table, _), sess in _memstore.items():
        if table != "sessions":
            continue
        stats = sess.get("stats") or {}
        cfg = sess.get("config") or {}
        rows.append({
            "user_id": sess.get("user_id"),
            "ticker": sess.get("ticker"),
            "status": sess.get("status"),
            "created_at": _ts_iso(sess.get("created_at")),
            "completed_at": _ts_iso(sess.get("completed_at")),
            "tokens_in": int(stats.get("tokens_in") or 0),
            "tokens_out": int(stats.get("tokens_out") or 0),
            "llm_calls": int(stats.get("llm_calls") or 0),
            "tool_calls": int(stats.get("tool_calls") or 0),
            "quick_model": cfg.get("quick_think_llm"),
            "deep_model": cfg.get("deep_think_llm"),
        })
    return rows


def admin_list_batches() -> List[Dict[str, Any]]:
    if _db_writable():
        return _admin_list_table(
            "batches",
            "user_id,status,created_at,completed_at,ticker_count,tokens_in,tokens_out,llm_calls,tool_calls,quick_model,deep_model",
        )
    rows: List[Dict[str, Any]] = []
    for (table, _), b in _memstore.items():
        if table != "batches":
            continue
        totals = b.get("totals") or {}
        cfg = b.get("config") or {}
        rows.append({
            "user_id": b.get("user_id"),
            "status": b.get("status"),
            "created_at": _ts_iso(b.get("created_at")),
            "completed_at": _ts_iso(b.get("completed_at")),
            "ticker_count": len(b.get("items") or []),
            "tokens_in": int(totals.get("tokens_in") or 0),
            "tokens_out": int(totals.get("tokens_out") or 0),
            "llm_calls": int(totals.get("llm_calls") or 0),
            "tool_calls": int(totals.get("tool_calls") or 0),
            "quick_model": cfg.get("quick_think_llm"),
            "deep_model": cfg.get("deep_think_llm"),
        })
    return rows


def admin_list_profiles() -> List[Dict[str, Any]]:
    if not _db_writable():
        return []
    try:
        resp = _http.get(
            f"{_rest_url('profiles')}?select=id,username,tier,created_at&limit={_ADMIN_MAX_ROWS}",
            headers=_service_headers(),
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning("admin list profiles -> %s: %s", resp.status_code, resp.text[:200])
            return []
        return resp.json() or []
    except requests.RequestException as e:
        log.warning("admin list profiles failed: %s", e)
        return []


def admin_list_users() -> List[Dict[str, Any]]:
    """Page through Supabase's GoTrue admin /users endpoint. Returns up to
    a few thousand users — well past anything this side project will see."""
    if not _db_writable():
        return []
    users: List[Dict[str, Any]] = []
    page = 1
    while page <= 100:  # hard cap so a bad response can't spin forever
        try:
            resp = _http.get(
                f"{_supabase_url()}/auth/v1/admin/users?page={page}&per_page=100",
                headers=_service_headers(),
                timeout=15,
            )
        except requests.RequestException as e:
            log.warning("admin list users failed: %s", e)
            break
        if resp.status_code != 200:
            log.warning("admin list users -> %s: %s", resp.status_code, resp.text[:200])
            break
        body = resp.json() or {}
        batch = body.get("users") if isinstance(body, dict) else body
        if not batch:
            break
        users.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return users


# =============================================================================
# Phase 1: recipes, paper trading, risk, cost, audit, pricing
# =============================================================================
#
# Storage helpers below follow the same dual-mode pattern as sessions/batches:
# Postgres when the service-role key is configured; an in-memory dict
# keyed by `(table, pk)` otherwise. The in-memory fallback is for local dev
# and CI; production must run with Supabase configured.
#
# Why one big file instead of three (`recipe_storage.py`, `paper_storage.py`,
# `risk_storage.py`)? The existing pattern already has all session + batch
# CRUD here; splitting Phase 1 CRUD across three new files would mean every
# read pulls helpers from a different module and the impersonation discipline
# fragments. DRY win > arbitrary module boundary.

# --- generic upsert helper (broader return shape than `_upsert` above) -----

def _upsert_columns(table: str, row: Dict[str, Any], on_conflict: Optional[str] = None) -> None:
    """Upsert a fully-columnar row (not the `{id, user_id, data}` shape).

    Used by tables like `paper_accounts`, `paper_positions`, `risk_limits`
    where columns are first-class (not buried in a `data` jsonb blob).
    `on_conflict` is the comma-separated PK column list — required for
    PostgREST to merge instead of insert-only.
    """
    if not _db_writable():
        return
    headers = _service_headers({"Prefer": "resolution=merge-duplicates,return=minimal"})
    url = _rest_url(table)
    if on_conflict:
        url = f"{url}?on_conflict={on_conflict}"
    try:
        resp = _http.post(url, headers=headers, json=[row], timeout=10)
        if resp.status_code >= 300:
            log.warning("supabase upsert %s -> %s: %s", table, resp.status_code, resp.text[:200])
    except requests.RequestException as e:
        log.warning("supabase upsert %s failed: %s", table, e)


def _select_columns(
    table: str,
    *,
    filters: Dict[str, Any],
    order: Optional[str] = None,
    limit: Optional[int] = None,
    select: str = "*",
) -> List[Dict[str, Any]]:
    """Generic select with arbitrary equality filters + optional order/limit."""
    if not _db_writable():
        return []
    parts: List[str] = [f"select={select}"]
    for col, val in filters.items():
        if val is None:
            parts.append(f"{col}=is.null")
        else:
            parts.append(f"{col}=eq.{val}")
    if order:
        parts.append(f"order={order}")
    if limit is not None:
        parts.append(f"limit={int(limit)}")
    url = f"{_rest_url(table)}?{'&'.join(parts)}"
    try:
        resp = _http.get(url, headers=_service_headers(), timeout=10)
        if resp.status_code != 200:
            log.warning("supabase select %s -> %s: %s", table, resp.status_code, resp.text[:200])
            return []
        return resp.json() or []
    except requests.RequestException as e:
        log.warning("supabase select %s failed: %s", table, e)
        return []


def _delete_where(table: str, filters: Dict[str, Any]) -> bool:
    if not _db_writable():
        return True
    parts = [f"{c}=eq.{v}" for c, v in filters.items()]
    url = f"{_rest_url(table)}?{'&'.join(parts)}"
    try:
        resp = _http.delete(url, headers=_service_headers(), timeout=10)
        return resp.status_code < 300
    except requests.RequestException as e:
        log.warning("supabase delete %s failed: %s", table, e)
        return False


# --- recipes ----------------------------------------------------------------

def save_recipe(recipe: Dict[str, Any]) -> None:
    """Insert-or-update a recipe row. Accepts the dict form of `Recipe`."""
    rid = recipe["id"]
    _memstore[("recipes", rid)] = recipe
    if not _db_writable():
        return
    row = _recipe_row(recipe)
    _upsert_columns("recipes", row, on_conflict="id")


def _recipe_row(recipe: Dict[str, Any]) -> Dict[str, Any]:
    """Project a Recipe dict to its columnar shape for `public.recipes`."""
    def _ts(val: Any) -> Optional[str]:
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return _ts_iso(float(val))
        return val  # assume already-formatted string
    return {
        "id": recipe["id"],
        "user_id": recipe["user_id"],
        "name": recipe["name"],
        "tickers": recipe["tickers"],
        "exchange_code": recipe.get("exchange_code", "XNYS"),
        "analysts": recipe.get("analysts") or [],
        "llm_provider": recipe["llm_provider"],
        "quick_model": recipe["quick_model"],
        "deep_model": recipe["deep_model"],
        "bull_model": recipe["bull_model"],
        "bear_model": recipe["bear_model"],
        "research_depth": recipe.get("research_depth", 1),
        "output_language": recipe.get("output_language", "English"),
        "schedule_kind": recipe.get("schedule_kind", "manual"),
        "schedule_expr": recipe.get("schedule_expr"),
        "misfire_grace_seconds": recipe.get("misfire_grace_seconds", 300),
        "market_hours_only": recipe.get("market_hours_only", True),
        "max_concurrent_tickers": recipe.get("max_concurrent_tickers", 5),
        "trigger_conditions": recipe.get("trigger_conditions"),
        "output_policy": recipe.get("output_policy", "notify"),
        "conviction_threshold": recipe.get("conviction_threshold", 7),
        "max_daily_token_cost_usd": recipe.get("max_daily_token_cost_usd", 5.0),
        "auto_inject_classical": recipe.get("auto_inject_classical", False),
        "consecutive_failures": recipe.get("consecutive_failures", 0),
        "status": recipe.get("status", "active"),
        "last_run_at": _ts(recipe.get("last_run_at")),
        "next_run_at": _ts(recipe.get("next_run_at")),
    }


def load_recipe(recipe_id: str) -> Optional[Dict[str, Any]]:
    if _db_writable():
        rows = _select_columns("recipes", filters={"id": recipe_id}, limit=1)
        if rows:
            return rows[0]
    return _memstore.get(("recipes", recipe_id))


def list_recipes(user_id: str) -> List[Dict[str, Any]]:
    if _db_writable() and user_id and user_id != ANONYMOUS_USER_ID:
        return _select_columns(
            "recipes", filters={"user_id": user_id}, order="created_at.desc",
        )
    return [
        r for (table, _), r in _memstore.items()
        if table == "recipes" and r.get("user_id") == user_id
    ]


def list_recipes_all_active() -> List[Dict[str, Any]]:
    """Scheduler bootstrap: every active recipe across users."""
    if _db_writable():
        return _select_columns(
            "recipes", filters={"status": "active"}, order="next_run_at.asc.nullsfirst",
        )
    return [
        r for (table, _), r in _memstore.items()
        if table == "recipes" and r.get("status") == "active"
    ]


def delete_recipe(recipe_id: str) -> bool:
    _memstore.pop(("recipes", recipe_id), None)
    if not _db_writable():
        return True
    return _delete_where("recipes", {"id": recipe_id})


def update_recipe_status(recipe_id: str, status: str) -> None:
    if r := _memstore.get(("recipes", recipe_id)):
        r["status"] = status
    if _db_writable():
        try:
            resp = _http.patch(
                f"{_rest_url('recipes')}?id=eq.{recipe_id}",
                headers=_service_headers({"Prefer": "return=minimal"}),
                json={"status": status, "updated_at": _ts_iso(time.time())},
                timeout=10,
            )
            if resp.status_code >= 300:
                log.warning("update_recipe_status -> %s: %s", resp.status_code, resp.text[:200])
        except requests.RequestException as e:
            log.warning("update_recipe_status failed: %s", e)


def touch_recipe_last_run(recipe_id: str, when: float) -> None:
    iso = _ts_iso(when)
    if r := _memstore.get(("recipes", recipe_id)):
        r["last_run_at"] = when
    if _db_writable():
        try:
            _http.patch(
                f"{_rest_url('recipes')}?id=eq.{recipe_id}",
                headers=_service_headers({"Prefer": "return=minimal"}),
                json={"last_run_at": iso, "updated_at": iso},
                timeout=10,
            )
        except requests.RequestException as e:
            log.warning("touch_recipe_last_run failed: %s", e)


def bump_recipe_failures(recipe_id: str) -> int:
    """Increment consecutive_failures; return new value. Best-effort in dev."""
    rec = _memstore.get(("recipes", recipe_id))
    if rec is not None:
        rec["consecutive_failures"] = int(rec.get("consecutive_failures", 0)) + 1
        new_val = rec["consecutive_failures"]
    else:
        new_val = 1
    if _db_writable():
        # Read-modify-write — not atomic across multi-worker, but the scheduler
        # already serializes per-recipe via the leader lock, so this is fine.
        row = load_recipe(recipe_id) or {}
        new_val = int(row.get("consecutive_failures", 0)) + 1
        try:
            _http.patch(
                f"{_rest_url('recipes')}?id=eq.{recipe_id}",
                headers=_service_headers({"Prefer": "return=minimal"}),
                json={"consecutive_failures": new_val, "updated_at": _ts_iso(time.time())},
                timeout=10,
            )
        except requests.RequestException as e:
            log.warning("bump_recipe_failures failed: %s", e)
    return new_val


def reset_recipe_failures(recipe_id: str) -> None:
    if r := _memstore.get(("recipes", recipe_id)):
        r["consecutive_failures"] = 0
    if _db_writable():
        try:
            _http.patch(
                f"{_rest_url('recipes')}?id=eq.{recipe_id}",
                headers=_service_headers({"Prefer": "return=minimal"}),
                json={"consecutive_failures": 0, "updated_at": _ts_iso(time.time())},
                timeout=10,
            )
        except requests.RequestException as e:
            log.warning("reset_recipe_failures failed: %s", e)


# --- paper account ----------------------------------------------------------

def load_paper_account(user_id: str) -> Optional[Dict[str, Any]]:
    if _db_writable():
        rows = _select_columns(
            "paper_accounts", filters={"user_id": user_id}, limit=1,
        )
        if rows:
            return rows[0]
    return _memstore.get(("paper_accounts", user_id))


def upsert_paper_account(
    *,
    user_id: str,
    cash: float,
    realized_pnl: float = 0.0,
    short_collateral_reserved: float = 0.0,
    starting_cash: Optional[float] = None,
    nav_open_today: Optional[float] = None,
    nav_open_today_date: Optional[str] = None,
) -> None:
    existing = _memstore.get(("paper_accounts", user_id)) or {}
    merged = {
        **existing,
        "user_id": user_id,
        "cash": float(cash),
        "realized_pnl": float(realized_pnl),
        "short_collateral_reserved": float(short_collateral_reserved),
        "starting_cash": float(starting_cash) if starting_cash is not None else float(existing.get("starting_cash", 100_000.0)),
        "updated_at": _ts_iso(time.time()),
    }
    if nav_open_today is not None:
        merged["nav_open_today"] = float(nav_open_today)
    if nav_open_today_date is not None:
        merged["nav_open_today_date"] = nav_open_today_date
    _memstore[("paper_accounts", user_id)] = merged
    if not _db_writable():
        return
    _upsert_columns("paper_accounts", merged, on_conflict="user_id")


# --- paper positions --------------------------------------------------------

def load_paper_position(user_id: str, ticker: str) -> Optional[Dict[str, Any]]:
    key = (user_id, ticker.upper())
    if _db_writable():
        rows = _select_columns(
            "paper_positions", filters={"user_id": user_id, "ticker": ticker.upper()}, limit=1,
        )
        if rows:
            return rows[0]
    return _memstore.get(("paper_positions", f"{key[0]}|{key[1]}"))


def list_paper_positions(user_id: str, *, ticker: Optional[str] = None) -> List[Dict[str, Any]]:
    if _db_writable():
        filters: Dict[str, Any] = {"user_id": user_id}
        if ticker:
            filters["ticker"] = ticker.upper()
        return _select_columns("paper_positions", filters=filters)
    out: List[Dict[str, Any]] = []
    for (table, key), row in _memstore.items():
        if table != "paper_positions":
            continue
        if not key.startswith(f"{user_id}|"):
            continue
        if ticker and row.get("ticker") != ticker.upper():
            continue
        out.append(row)
    return out


def upsert_paper_position(
    *,
    user_id: str,
    ticker: str,
    qty: float,
    avg_cost: float,
    last_price: Optional[float] = None,
) -> None:
    row = {
        "user_id": user_id,
        "ticker": ticker.upper(),
        "qty": float(qty),
        "avg_cost": float(avg_cost),
        "last_price": float(last_price) if last_price is not None else None,
        "last_price_at": _ts_iso(time.time()) if last_price is not None else None,
        "updated_at": _ts_iso(time.time()),
    }
    _memstore[("paper_positions", f"{user_id}|{ticker.upper()}")] = row
    if not _db_writable():
        return
    _upsert_columns("paper_positions", row, on_conflict="user_id,ticker")


def delete_paper_position(user_id: str, ticker: str) -> bool:
    _memstore.pop(("paper_positions", f"{user_id}|{ticker.upper()}"), None)
    if not _db_writable():
        return True
    return _delete_where(
        "paper_positions", {"user_id": user_id, "ticker": ticker.upper()},
    )


# --- paper orders -----------------------------------------------------------

def find_paper_order_idem(
    user_id: str, fire_id: str, ticker: str, side: str,
) -> Optional[Dict[str, Any]]:
    """Idempotency lookup. Returns the existing order if one matches the
    (user, fire, ticker, side) unique key. Used by `paper.place_order` to
    swallow duplicate scheduler retries."""
    if _db_writable():
        rows = _select_columns(
            "paper_orders",
            filters={"user_id": user_id, "fire_id": fire_id,
                     "ticker": ticker.upper(), "side": side},
            limit=1,
        )
        if rows:
            return rows[0]
    for (table, _), row in _memstore.items():
        if table != "paper_orders":
            continue
        if (row.get("user_id") == user_id and row.get("fire_id") == fire_id
                and row.get("ticker") == ticker.upper() and row.get("side") == side):
            return row
    return None


def insert_paper_order(row: Dict[str, Any]) -> None:
    _memstore[("paper_orders", row["id"])] = row
    if not _db_writable():
        return
    _upsert_columns("paper_orders", row, on_conflict="id")


def call_paper_place_order_rpc(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Invoke the Phase 1.5 Postgres RPC `paper_place_order(...)`.

    Returns the function's JSONB result on success (`{order_id, idempotent,
    ...}`), or None when (a) Supabase isn't configured, (b) the function
    isn't installed yet (404 from PostgREST), or (c) any transport error.
    Callers fall through to the Python implementation when None is returned.

    The function runs SECURITY DEFINER with a per-user advisory xact-lock so
    concurrent orders for the same user serialize cleanly. See migration in
    `docs/supabase-schema.sql`.
    """
    if not _db_writable():
        return None
    url = f"{_supabase_url()}/rest/v1/rpc/paper_place_order"
    try:
        resp = _http.post(
            url, headers=_service_headers(), json=payload, timeout=15,
        )
    except requests.RequestException as exc:
        log.warning("paper_place_order RPC transport failed: %s", exc)
        return None
    if resp.status_code == 404:
        # RPC not installed yet — surface once, then fall back silently.
        log.info("paper_place_order RPC missing; falling back to Python flow")
        return None
    if resp.status_code >= 300:
        log.warning("paper_place_order RPC -> %s: %s",
                    resp.status_code, resp.text[:200])
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def list_paper_orders(
    user_id: str, *, limit: int = 50, recipe_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if _db_writable():
        filters: Dict[str, Any] = {"user_id": user_id}
        if recipe_id:
            filters["recipe_id"] = recipe_id
        return _select_columns(
            "paper_orders", filters=filters, order="created_at.desc", limit=limit,
        )
    out = [
        row for (table, _), row in _memstore.items()
        if table == "paper_orders" and row.get("user_id") == user_id
        and (recipe_id is None or row.get("recipe_id") == recipe_id)
    ]
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out[:limit]


# --- conviction scores ------------------------------------------------------

def insert_conviction_score(row: Dict[str, Any]) -> None:
    pk = f"{row.get('recipe_id')}|{row.get('ticker')}|{row.get('recorded_at')}"
    _memstore[("conviction_scores", pk)] = row
    if not _db_writable():
        return
    _upsert_columns("conviction_scores", row)


def list_conviction_scores(
    user_id: str, *, ticker: Optional[str] = None, limit: int = 50,
) -> List[Dict[str, Any]]:
    if _db_writable():
        filters: Dict[str, Any] = {"user_id": user_id}
        if ticker:
            filters["ticker"] = ticker.upper()
        return _select_columns(
            "conviction_scores", filters=filters, order="recorded_at.desc", limit=limit,
        )
    out = [
        row for (table, _), row in _memstore.items()
        if table == "conviction_scores" and row.get("user_id") == user_id
        and (ticker is None or row.get("ticker") == ticker.upper())
    ]
    out.sort(key=lambda r: r.get("recorded_at") or "", reverse=True)
    return out[:limit]


# --- risk limits + events ---------------------------------------------------

def load_risk_limits(user_id: str) -> Optional[Dict[str, Any]]:
    if _db_writable():
        rows = _select_columns("risk_limits", filters={"user_id": user_id}, limit=1)
        if rows:
            return rows[0]
    return _memstore.get(("risk_limits", user_id))


def upsert_risk_limits(user_id: str, **fields: Any) -> Dict[str, Any]:
    existing = load_risk_limits(user_id) or _default_risk_limits_row(user_id)
    merged = {**existing, **{k: v for k, v in fields.items() if v is not None}}
    merged["user_id"] = user_id
    merged["updated_at"] = _ts_iso(time.time())
    _memstore[("risk_limits", user_id)] = merged
    if _db_writable():
        _upsert_columns("risk_limits", merged, on_conflict="user_id")
    return merged


def _default_risk_limits_row(user_id: str) -> Dict[str, Any]:
    # Default spend caps are tier-driven (PR-2, Sundar review #3). A brand-new
    # user lands on the `novice` row ($0.50/day, $10/month) so a stuck recipe
    # can't burn multi-figure bills before the user notices. Upgrading the
    # user's `profiles.tier` lifts the floor; users still tune the exact
    # numbers in the risk-limits UI.
    tier = load_profile_tier(user_id)
    tier_caps = tier_default_spend_caps(tier)
    return {
        "user_id": user_id,
        "max_position_pct": 0.10,
        "max_daily_drawdown_pct": 0.03,
        "max_slippage_bps": 10,
        "kelly_fraction_cap": 0.10,
        "adaptive_depth_variance_threshold": 0.30,
        "daily_spend_cap_usd": tier_caps["daily_spend_cap_usd"],
        "monthly_spend_cap_usd": tier_caps["monthly_spend_cap_usd"],
        "allow_shorts": False,
        "global_kill_switch": False,
        "behavioral_cooldown": False,
    }


def insert_risk_event(row: Dict[str, Any]) -> None:
    # Auto-generated id for in-memory storage; Postgres has a bigserial.
    pk = f"{row.get('user_id')}|{row.get('created_at')}|{row.get('rule')}"
    _memstore[("risk_events", pk)] = row
    if not _db_writable():
        return
    _upsert_columns("risk_events", row)


def insert_disagreement_log(row: Dict[str, Any]) -> None:
    """Append a row to `disagreement_log`. Used by:
      - Phase 2 #6 disagreement.score_and_log (bull vs bear LLMs)
      - Phase 3 #3 multi-TF fan-out (cross-timeframe spread)
    Idempotent on session_id; the unique key surfaces one row per fire."""
    row = dict(row)
    row.setdefault("recorded_at", _ts_iso(time.time()))
    pk = row.get("session_id") or f"{row.get('user_id')}|{row['recorded_at']}"
    _memstore[("disagreement_log", pk)] = row
    if _db_writable():
        try:
            _upsert_columns("disagreement_log", row)
        except Exception:
            pass


def list_risk_events(user_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
    if _db_writable():
        return _select_columns(
            "risk_events", filters={"user_id": user_id},
            order="created_at.desc", limit=limit,
        )
    out = [
        row for (table, _), row in _memstore.items()
        if table == "risk_events" and row.get("user_id") == user_id
    ]
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out[:limit]


# --- recipe usage + global spend -------------------------------------------

def add_recipe_usage(
    *,
    recipe_id: str,
    user_id: str,
    usage_date: str,
    input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
    token_cost_usd: float,
    failure: bool = False,
) -> None:
    pk = f"{recipe_id}|{usage_date}"
    existing = _memstore.get(("recipe_usage", pk)) or {
        "recipe_id": recipe_id, "user_id": user_id, "usage_date": usage_date,
        "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
        "token_cost_usd": 0.0, "run_count": 0, "failure_count": 0,
    }
    existing["input_tokens"] = int(existing.get("input_tokens", 0)) + int(input_tokens)
    existing["output_tokens"] = int(existing.get("output_tokens", 0)) + int(output_tokens)
    existing["reasoning_tokens"] = int(existing.get("reasoning_tokens", 0)) + int(reasoning_tokens)
    existing["token_cost_usd"] = float(existing.get("token_cost_usd", 0.0)) + float(token_cost_usd)
    if failure:
        existing["failure_count"] = int(existing.get("failure_count", 0)) + 1
    else:
        existing["run_count"] = int(existing.get("run_count", 0)) + 1
    _memstore[("recipe_usage", pk)] = existing
    if _db_writable():
        _upsert_columns("recipe_usage", existing, on_conflict="recipe_id,usage_date")


def load_recipe_usage(recipe_id: str, usage_date: str) -> Optional[Dict[str, Any]]:
    if _db_writable():
        rows = _select_columns(
            "recipe_usage",
            filters={"recipe_id": recipe_id, "usage_date": usage_date},
            limit=1,
        )
        if rows:
            return rows[0]
    return _memstore.get(("recipe_usage", f"{recipe_id}|{usage_date}"))


def add_user_spend(user_id: str, usage_date: str, cost_usd: float) -> None:
    pk = f"{user_id}|{usage_date}"
    existing = _memstore.get(("user_spend_daily", pk)) or {
        "user_id": user_id, "usage_date": usage_date, "total_cost_usd": 0.0,
    }
    existing["total_cost_usd"] = float(existing.get("total_cost_usd", 0.0)) + float(cost_usd)
    _memstore[("user_spend_daily", pk)] = existing
    if _db_writable():
        _upsert_columns("user_spend_daily", existing, on_conflict="user_id,usage_date")


def load_user_spend(user_id: str, usage_date: str) -> float:
    if _db_writable():
        rows = _select_columns(
            "user_spend_daily",
            filters={"user_id": user_id, "usage_date": usage_date},
            limit=1,
        )
        if rows:
            return float(rows[0].get("total_cost_usd") or 0.0)
    row = _memstore.get(("user_spend_daily", f"{user_id}|{usage_date}"))
    return float(row.get("total_cost_usd") or 0.0) if row else 0.0


# --- audit log + impersonation ---------------------------------------------

def append_audit(
    *,
    actor: str,
    action: str,
    target_user_id: Optional[str] = None,
    target_resource: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    row = {
        "actor": actor,
        "action": action,
        "target_user_id": target_user_id,
        "target_resource": target_resource,
        "metadata": metadata or {},
        "created_at": _ts_iso(time.time()),
    }
    pk = f"{actor}|{action}|{row['created_at']}|{target_user_id}"
    _memstore[("audit_log", pk)] = row
    if _db_writable():
        _upsert_columns("audit_log", row)


def list_audit(
    *,
    actor: Optional[str] = None,
    action: Optional[str] = None,
    target_user_id: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Read audit_log entries, optionally filtered. Most recent first.

    Used by the /fund streaming-status panel (Phase 3) and any future
    surface that needs to display historical system actions."""
    if _db_writable():
        try:
            filters: Dict[str, Any] = {}
            if actor is not None:
                filters["actor"] = actor
            if action is not None:
                filters["action"] = action
            if target_user_id is not None:
                filters["target_user_id"] = target_user_id
            rows = _select_columns(
                "audit_log",
                filters=filters,
                select="*",
                order="created_at.desc",
                limit=limit,
            )
            return rows or []
        except Exception:
            pass
    # Memstore fallback — pull all audit rows, filter, sort.
    rows = [
        dict(v) for (table, _), v in _memstore.items()
        if table == "audit_log"
    ]
    if actor is not None:
        rows = [r for r in rows if r.get("actor") == actor]
    if action is not None:
        rows = [r for r in rows if r.get("action") == action]
    if target_user_id is not None:
        rows = [r for r in rows if r.get("target_user_id") == target_user_id]
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return rows[:limit]


# --- pricing ---------------------------------------------------------------

def fetch_price_row(provider: str, model: str, at: Optional[datetime] = None):
    """Return the most-recent llm_pricing row whose `effective_at` ≤ `at`.

    Imported lazily by `agenticwhales.llm_clients.pricing.cost_for`.
    Returns the `PriceRow` dataclass when found, otherwise None.
    """
    if not _db_writable():
        return None
    from decimal import Decimal
    from agenticwhales.llm_clients.pricing import PriceRow

    target_iso = (at or datetime.now(tz=timezone.utc)).isoformat()
    parts = [
        "select=*",
        f"provider=eq.{provider.lower()}",
        f"model=eq.{model}",
        f"effective_at=lte.{target_iso}",
        "order=effective_at.desc",
        "limit=1",
    ]
    try:
        resp = _http.get(
            f"{_rest_url('llm_pricing')}?{'&'.join(parts)}",
            headers=_service_headers(),
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        rows = resp.json() or []
        if not rows:
            return None
        r = rows[0]
        return PriceRow(
            provider=r["provider"],
            model=r["model"],
            input_per_1m=Decimal(str(r["input_per_1m_usd"])),
            output_per_1m=Decimal(str(r["output_per_1m_usd"])),
            cache_read_per_1m=Decimal(str(r["cache_read_per_1m_usd"])) if r.get("cache_read_per_1m_usd") is not None else None,
            reasoning_per_1m=Decimal(str(r["reasoning_per_1m_usd"])) if r.get("reasoning_per_1m_usd") is not None else None,
            effective_at=datetime.fromisoformat(r["effective_at"].replace("Z", "+00:00")),
            source_url=r.get("source_url"),
        )
    except requests.RequestException as e:
        log.warning("fetch_price_row failed: %s", e)
        return None


def list_priced_models() -> List[tuple]:
    """Return `(provider, model)` pairs available in the pricing table."""
    if not _db_writable():
        return []
    try:
        resp = _http.get(
            f"{_rest_url('llm_pricing')}?select=provider,model",
            headers=_service_headers(),
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        seen = {(r["provider"], r["model"]) for r in resp.json() or []}
        return sorted(seen)
    except requests.RequestException:
        return []


# --- journal entries (Phase 2) ---------------------------------------------

def save_journal_entry(row: Dict[str, Any]) -> None:
    """Insert-or-update a journal entry. Caller supplies the full dict."""
    entry_id = row["id"]
    _memstore[("journal_entries", entry_id)] = row
    if not _db_writable():
        return
    _upsert_columns("journal_entries", row, on_conflict="id")


def load_journal_entry(entry_id: str) -> Optional[Dict[str, Any]]:
    if _db_writable():
        rows = _select_columns("journal_entries", filters={"id": entry_id}, limit=1)
        if rows:
            return rows[0]
    return _memstore.get(("journal_entries", entry_id))


def list_journal_entries(
    user_id: str,
    *,
    session_id: Optional[str] = None,
    paper_order_id: Optional[str] = None,
    thesis_id: Optional[str] = None,
    kind: Optional[str] = None,
    include_drafts: bool = True,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Return entries for a user, newest first. Filters are optional and
    combinable. Drafts are included by default — the UI hides them in the
    Journal timeline but the auto-draft endpoint needs to see them."""
    if _db_writable():
        filters: Dict[str, Any] = {"user_id": user_id}
        if session_id:     filters["session_id"] = session_id
        if paper_order_id: filters["paper_order_id"] = paper_order_id
        if thesis_id:      filters["thesis_id"] = thesis_id
        if kind:           filters["kind"] = kind
        if not include_drafts: filters["is_draft"] = "false"
        return _select_columns(
            "journal_entries", filters=filters,
            order="created_at.desc", limit=limit,
        )
    out = [
        r for (t, _), r in _memstore.items()
        if t == "journal_entries" and r.get("user_id") == user_id
        and (not session_id or r.get("session_id") == session_id)
        and (not paper_order_id or r.get("paper_order_id") == paper_order_id)
        and (not thesis_id or r.get("thesis_id") == thesis_id)
        and (not kind or r.get("kind") == kind)
        and (include_drafts or not r.get("is_draft"))
    ]
    out.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return out[:limit]


def delete_journal_entry(entry_id: str) -> bool:
    _memstore.pop(("journal_entries", entry_id), None)
    if not _db_writable():
        return True
    return _delete_where("journal_entries", {"id": entry_id})


# ---------------------------------------------------------------------------
# PR-3: session-state helpers for the stuck-run reaper + concurrent-fire gate
# ---------------------------------------------------------------------------


def has_running_session_for_recipe(recipe_id: str) -> bool:
    """True when this recipe has a session in `status='running'`.

    Used by the recipe-fire path as a DB-backed alternative to the
    in-process `threading.Lock`. Surviving leadership handoff is the
    point — a session marked running on a dead pod stays as a guard
    against concurrent fires until the stuck-run reaper resets it.
    """
    if _db_writable():
        rows = _select_columns(
            "sessions",
            filters={"recipe_id": recipe_id, "status": "running"},
            limit=1,
            select="id",
        )
        return bool(rows)
    return any(
        r.get("recipe_id") == recipe_id and r.get("status") == "running"
        for (t, _), r in _memstore.items() if t == "sessions"
    )


def list_stuck_running_sessions(
    *, older_than_seconds: int = 30 * 60, limit: int = 500,
) -> List[Dict[str, Any]]:
    """Return sessions stuck at `status='running'` past the cutoff.

    Cutoff is computed against `created_at` (we never updated `updated_at`
    historically; `created_at` is the most reliable signal we have without
    a migration). The reaper uses this to flip stuck rows to `failed` so
    the next recipe fire can proceed.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=older_than_seconds)
    cutoff_iso = cutoff.isoformat()
    if _db_writable():
        # Supabase REST filter: `created_at=lt.<cutoff>` — we issue the
        # constraint manually since _select_columns only supports eq.
        url = _rest_url("sessions")
        params = {
            "status": "eq.running",
            "created_at": f"lt.{cutoff_iso}",
            "select": "id,user_id,recipe_id,fire_id,created_at,status",
            "order": "created_at.asc",
            "limit": str(limit),
        }
        try:
            resp = _http.get(url, params=params, headers=_service_headers(), timeout=10)
            if resp.status_code == 200:
                return resp.json() or []
            log.warning("supabase list stuck sessions -> %s: %s",
                        resp.status_code, resp.text)
            return []
        except Exception as exc:
            log.warning("supabase list stuck sessions failed: %s", exc)
            return []
    # _memstore fallback
    out = []
    for (t, _), r in _memstore.items():
        if t != "sessions" or r.get("status") != "running":
            continue
        created = r.get("created_at")
        try:
            if isinstance(created, (int, float)):
                created_dt = datetime.fromtimestamp(created, tz=timezone.utc)
            else:
                created_dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if created_dt < cutoff:
            out.append(r)
    out.sort(key=lambda r: r.get("created_at") or "")
    return out[:limit]


def delete_stuck_running_sessions(
    *, older_than_seconds: int = 24 * 60 * 60, limit: int = 500,
) -> int:
    """Hard-delete sessions that are still flagged `running` or `pending` past
    the cutoff (default 24h).

    The 30-min stuck-run reaper at `scheduler._run_stuck_run_reaper` flips
    these rows to `status='failed'` first, but rows where the reaper never
    ran (autonomy disabled, non-leader process, deploy that pre-dates the
    reaper) still linger as RUNNING. After a full day no legitimate
    in-flight session exists — every real run completes in under 5 minutes —
    so we purge them so they stop polluting the user's Analyses + Recent
    Activity tables.

    Returns the count of deleted rows. Idempotent.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=older_than_seconds)
    cutoff_iso = cutoff.isoformat()
    deleted = 0

    if _db_writable():
        # Pull the IDs first so we can drop them from _memstore too and emit
        # an accurate count. PostgREST supports the `in.(...)` filter for a
        # batched DELETE but we keep one-at-a-time for the audit-friendliness.
        url = _rest_url("sessions")
        params = {
            "status": "in.(running,pending,composing_report)",
            "created_at": f"lt.{cutoff_iso}",
            "select": "id,user_id,created_at,status",
            "order": "created_at.asc",
            "limit": str(limit),
        }
        rows: List[Dict[str, Any]] = []
        try:
            resp = _http.get(url, params=params, headers=_service_headers(), timeout=10)
            if resp.status_code == 200:
                rows = resp.json() or []
            else:
                log.warning("delete_stuck_running_sessions: list -> %s: %s",
                            resp.status_code, resp.text[:200])
        except Exception as exc:
            log.warning("delete_stuck_running_sessions: list failed: %s", exc)

        for r in rows:
            sid = r.get("id")
            if not sid:
                continue
            try:
                if _delete_one("sessions", sid):
                    deleted += 1
            except Exception as exc:
                log.warning("delete_stuck_running_sessions: delete %s failed: %s", sid, exc)
            _memstore.pop(("sessions", sid), None)

    # _memstore sweep (covers dev mode + any rows the DB delete missed).
    stale_keys = []
    for key, r in list(_memstore.items()):
        table, _sid = key
        if table != "sessions":
            continue
        status = r.get("status")
        if status not in ("running", "pending", "composing_report"):
            continue
        created = r.get("created_at")
        try:
            if isinstance(created, (int, float)):
                created_dt = datetime.fromtimestamp(created, tz=timezone.utc)
            else:
                created_dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if created_dt < cutoff:
            stale_keys.append(key)
    for key in stale_keys:
        if _memstore.pop(key, None) is not None:
            # Already counted via DB path? Add if memstore-only.
            if not _db_writable():
                deleted += 1
    return deleted


def mark_session_failed(
    session_id: str, *, failure_reason: str, when: Optional[float] = None,
) -> bool:
    """Flip a session row to `status='failed'` with a recorded reason.

    Returns True if a row was updated; False if no row matched.

    Both stores receive the write so callers can read back consistently:
      - memstore: the session dict is mutated in place
      - Postgres: a partial update touches only status, completed_at, data
    """
    now = _ts_iso(when if when is not None else time.time())
    updated = False

    # _memstore first so dev mode is fast and visible.
    memrow = _memstore.get(("sessions", session_id))
    if memrow is not None:
        memrow["status"] = "failed"
        memrow["completed_at"] = now
        memrow.setdefault("data", memrow)["failure_reason"] = failure_reason
        updated = True

    if _db_writable():
        # Read the row's data jsonb so we can stamp failure_reason inside it.
        row = _select_one("sessions", session_id) or {}
        data = row.get("data") or {}
        data["failure_reason"] = failure_reason
        patch = {
            "id": session_id,
            "status": "failed",
            "completed_at": now,
            "data": data,
        }
        try:
            _upsert("sessions", patch)
            updated = True
        except Exception as exc:
            log.warning("mark_session_failed Supabase upsert failed: %s", exc)
    return updated


# ---------------------------------------------------------------------------
# Compliance attestation (PR-2, Sundar review #2)
# ---------------------------------------------------------------------------
#
# Server-side enforcement of the paper-only / not-advice / jurisdiction
# acknowledgement. The previous design recorded an audit row but didn't
# block downstream actions; the version below ties session creation and
# paper-order placement to a non-revoked attestation row whose `version`
# matches `compliance_active_version.version`.
#
# Storage: real Postgres when available, _memstore fallback otherwise.

# Hard-coded fallback when the DB is unreachable. The migration seeds the
# same value, so production reads through to Postgres; this constant only
# matters in tests / guest mode.
_FALLBACK_ACTIVE_COMPLIANCE_VERSION = "v1.0"


def active_compliance_version() -> str:
    """Return the currently-enforced disclaimer version."""
    if _db_writable():
        rows = _select_columns(
            "compliance_active_version", filters={"id": 1}, limit=1,
            select="version",
        )
        if rows:
            return rows[0]["version"]
    return _memstore.get(
        ("compliance_active_version", "1"),
        {"version": _FALLBACK_ACTIVE_COMPLIANCE_VERSION},
    )["version"]


def save_compliance_attestation(row: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a fresh attestation row. ID is assumed to be set by caller."""
    _memstore[("compliance_attestations", row["id"])] = row
    if _db_writable():
        _upsert_columns("compliance_attestations", row, on_conflict="id")
    return row


def load_compliance_attestation(att_id: str) -> Optional[Dict[str, Any]]:
    if _db_writable():
        rows = _select_columns(
            "compliance_attestations", filters={"id": att_id}, limit=1,
        )
        if rows:
            return rows[0]
    return _memstore.get(("compliance_attestations", att_id))


def latest_active_attestation_for_user(user_id: str) -> Optional[Dict[str, Any]]:
    """Return the most recent non-revoked attestation matching the active
    version, or None if the user has no qualifying attestation.

    Always falls through to _memstore if Postgres yields nothing — covers the
    case where Supabase is configured but the `compliance_attestations` table
    is missing from the schema cache (PGRST205), which would otherwise leave
    the user stuck in an accept-loop: every accept writes successfully to
    _memstore via `save_compliance_attestation`, but reads only checked Postgres.
    """
    version = active_compliance_version()
    if _db_writable():
        rows = _select_columns(
            "compliance_attestations",
            filters={"user_id": user_id, "version": version},
            order="created_at.desc",
            limit=1,
        )
        for r in rows:
            if not r.get("revoked_at") and r.get("ack_paper_only") \
                    and r.get("ack_not_advice") and r.get("ack_jurisdiction"):
                return r
        # fall through to _memstore — see docstring
    candidates = [
        r for (t, _), r in _memstore.items()
        if t == "compliance_attestations"
        and r.get("user_id") == user_id
        and r.get("version") == version
        and not r.get("revoked_at")
        and r.get("ack_paper_only") and r.get("ack_not_advice")
        and r.get("ack_jurisdiction")
    ]
    candidates.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Profile tier lookup (PR-2, Sundar review #3 — tier-driven spend caps)
# ---------------------------------------------------------------------------

# Tier-keyed defaults for the spend caps. Risk limits inherit from these
# the first time a user touches them; subsequent edits in /api/risk/limits
# take precedence so a user can voluntarily lower caps but not silently
# raise them above their tier.
_TIER_DEFAULTS = {
    "novice":       {"daily_spend_cap_usd":   0.50, "monthly_spend_cap_usd":   10.0},
    "intermediate": {"daily_spend_cap_usd":   5.00, "monthly_spend_cap_usd":  100.0},
    "master":       {"daily_spend_cap_usd":  50.00, "monthly_spend_cap_usd": 1000.0},
}


def load_profile_tier(user_id: str) -> str:
    """Return the user's tier or 'novice' if unknown.

    Used by the risk-limits defaults so a brand-new user lands on a low
    spend cap until they upgrade their tier (and pay for it).
    """
    if _db_writable():
        rows = _select_columns(
            "profiles", filters={"id": user_id}, limit=1, select="tier",
        )
        if rows and rows[0].get("tier"):
            return rows[0]["tier"]
    row = _memstore.get(("profiles", user_id))
    if row and row.get("tier"):
        return row["tier"]
    return "novice"


def tier_default_spend_caps(tier: str) -> Dict[str, float]:
    """Return the daily/monthly cap pair for a tier (falls back to novice)."""
    return dict(_TIER_DEFAULTS.get(tier, _TIER_DEFAULTS["novice"]))


# --- test helper -----------------------------------------------------------

def _reset_memstore_for_tests() -> None:
    """Drop all in-memory storage. Pytest fixture helper."""
    _memstore.clear()
    _token_cache.clear()
