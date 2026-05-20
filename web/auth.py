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
        if resp.status_code >= 300:
            log.warning("supabase upsert %s -> %s: %s", table, resp.status_code, resp.text[:200])
    except requests.RequestException as e:
        log.warning("supabase upsert %s failed: %s", table, e)


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
    if not _db_writable() or not user_id or user_id == ANONYMOUS_USER_ID:
        # In-memory fallback so local dev without Supabase still works.
        _memstore[("sessions", session["id"])] = session
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
