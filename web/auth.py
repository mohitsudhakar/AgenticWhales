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
# Server-side analysis quota (Analyst Desk). The novice/intermediate/
# master daily limits were historically enforced only in the browser
# (supabase-client.js) — trivially bypassable with a raw POST. These
# helpers make POST /api/sessions and /api/batches the enforcement
# point; the client check remains as UX.
# ------------------------------------------------------------------

ANALYSIS_TIER_QUOTAS: Dict[str, Optional[int]] = {
    "novice": 3,
    "intermediate": 50,
    "master": None,        # unlimited
}


def get_user_tier(user_id: str) -> str:
    """The user's tier from `profiles` (service-role read); 'novice' default."""
    if _db_writable():
        rows = _select_columns("profiles", filters={"id": user_id},
                               select="tier", limit=1)
        if rows:
            return (rows[0].get("tier") or "novice").lower()
    row = _memstore.get(("profiles", user_id))
    return ((row or {}).get("tier") or "novice").lower()


def _epoch_of(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    ts = _parse_iso_ts(v)
    return ts.timestamp() if ts else 0.0


def count_user_analyses_today(user_id: str) -> int:
    """Units consumed today (UTC): one per interactive session (recipe-fired
    sessions are excluded — recipes have their own budget gates) plus one per
    basket ticker (the same accounting the desk UI shows the user)."""
    now = datetime.now(tz=timezone.utc)
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp()
    units = 0
    for s in list_sessions(user_id):
        if _epoch_of(s.get("created_at")) >= midnight and not s.get("recipe_id"):
            units += 1
    for b in list_batches(user_id):
        if _epoch_of(b.get("created_at")) >= midnight:
            units += len(b.get("tickers") or b.get("items") or []) or 1
    return units


def check_analysis_quota(user_id: str, units: int = 1) -> tuple:
    """(allowed, info) for creating `units` more analyses today.

    The anonymous user (Supabase unconfigured — local dev) is exempt; when
    Supabase IS configured, anonymous requests never reach the endpoints
    (get_current_user_id 401s first), so sign-in + quota are both enforced
    exactly when running with real auth."""
    if not user_id or user_id == ANONYMOUS_USER_ID:
        return True, {"tier": "anonymous", "cap": None, "used": 0}
    tier = get_user_tier(user_id)
    cap = ANALYSIS_TIER_QUOTAS.get(tier, ANALYSIS_TIER_QUOTAS["novice"])
    if cap is None:
        return True, {"tier": tier, "cap": None, "used": 0}
    used = count_user_analyses_today(user_id)
    return (used + units <= cap), {"tier": tier, "cap": cap, "used": used}


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


def insert_coach_audit(row: Dict[str, Any]) -> None:
    """Persist a behavioral-coach audit summary for the current user.
    Dual-path: memstore first, then Supabase (columnar)."""
    pk = row.get("id") or f"{row.get('user_id')}|{row.get('created_at')}"
    _memstore[("coach_audits", pk)] = row
    if not _db_writable():
        return
    _upsert_columns("coach_audits", row, on_conflict="id")


_AUDIT_SUMMARY_COLS = "id,created_at,discipline_score,total_pnl,disciplined_pnl,n_trades"


def list_coach_audits(user_id: str, *, limit: int = 60) -> list:
    """A user's audit *summaries*, newest first (for the discipline-over-time chart).
    Deliberately excludes the heavy `transactions`/`leak_summary` payloads."""
    if _db_writable():
        return _select_columns(
            "coach_audits", filters={"user_id": user_id},
            order="created_at.desc", limit=limit, select=_AUDIT_SUMMARY_COLS,
        )
    out = [
        {k: r.get(k) for k in ("id", "created_at", "discipline_score",
                               "total_pnl", "disciplined_pnl", "n_trades")}
        for (t, _), r in _memstore.items()
        if t == "coach_audits" and r.get("user_id") == user_id
    ]
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out[:limit]


def get_latest_coach_audit(user_id: str) -> Optional[Dict[str, Any]]:
    """The user's most recent audit row INCLUDING its raw `transactions`,
    so the dashboard can recompute the full report without a re-upload."""
    if _db_writable():
        rows = _select_columns(
            "coach_audits", filters={"user_id": user_id},
            order="created_at.desc", limit=1,
        )
        return rows[0] if rows else None
    mine = [r for (t, _), r in _memstore.items()
            if t == "coach_audits" and r.get("user_id") == user_id]
    mine.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return mine[0] if mine else None


def save_coach_trades(user_id: str, transactions: list) -> None:
    """Store the user's full deduped trade history (the continuous-timeline source)."""
    row = {"user_id": user_id, "transactions": transactions, "updated_at": _ts_iso(time.time())}
    _memstore[("coach_trades", user_id)] = row
    if not _db_writable():
        return
    _upsert_columns("coach_trades", row, on_conflict="user_id")


def get_coach_trades(user_id: str) -> list:
    if _db_writable():
        rows = _select_columns("coach_trades", filters={"user_id": user_id}, limit=1)
        if rows:
            return rows[0].get("transactions") or []
    return (_memstore.get(("coach_trades", user_id)) or {}).get("transactions") or []


def insert_coach_finding(row: Dict[str, Any]) -> None:
    """Persist one emitted coach finding (a flagged leak + its prescribed rule).
    The forward-validation seam: rows are later resolved against the trades
    that happened AFTER the finding was shown (D1 in the 2026-06-08 critique)."""
    pk = row.get("id") or f"{row.get('user_id')}|{row.get('leak_key')}|{row.get('created_at')}"
    row.setdefault("id", pk)
    _memstore[("coach_findings", pk)] = row
    if not _db_writable():
        return
    _upsert_columns("coach_findings", row, on_conflict="id")


def list_coach_findings(user_id: str, *, unresolved_only: bool = False,
                        limit: int = 200) -> list:
    """A user's persisted findings, newest first."""
    if _db_writable():
        filters: Dict[str, Any] = {"user_id": user_id}
        if unresolved_only:
            filters["resolved_at"] = None
        return _select_columns("coach_findings", filters=filters,
                               order="created_at.desc", limit=limit)
    out = [r for (t, _), r in _memstore.items()
           if t == "coach_findings" and r.get("user_id") == user_id
           and (not unresolved_only or not r.get("resolved_at"))]
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out[:limit]


def update_coach_finding(finding_id: str, fields: Dict[str, Any]) -> None:
    """Merge resolution fields into a finding (memstore + Supabase upsert)."""
    row = _memstore.get(("coach_findings", finding_id))
    if row is not None:
        row.update(fields)
    if not _db_writable():
        return
    db_rows = _select_columns("coach_findings", filters={"id": finding_id}, limit=1)
    if db_rows:
        merged = {**db_rows[0], **fields}
        _upsert_columns("coach_findings", merged, on_conflict="id")
    elif row is not None:
        _upsert_columns("coach_findings", row, on_conflict="id")


def insert_coach_rule(row: Dict[str, Any]) -> None:
    """Persist one rule-book row (suggested/active/paused). Dual-path."""
    pk = row.get("id")
    _memstore[("coach_rules", pk)] = row
    if not _db_writable():
        return
    _upsert_columns("coach_rules", row, on_conflict="id")


def list_coach_rules(user_id: str, *, status: Optional[str] = None,
                     limit: int = 100) -> list:
    if _db_writable():
        filters: Dict[str, Any] = {"user_id": user_id}
        if status:
            filters["status"] = status
        return _select_columns("coach_rules", filters=filters,
                               order="created_at.desc", limit=limit)
    out = [r for (t, _), r in _memstore.items()
           if t == "coach_rules" and r.get("user_id") == user_id
           and (status is None or r.get("status") == status)]
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out[:limit]


def get_coach_rule(rule_id: str) -> Optional[Dict[str, Any]]:
    row = _memstore.get(("coach_rules", rule_id))
    if row is not None:
        return row
    if _db_writable():
        rows = _select_columns("coach_rules", filters={"id": rule_id}, limit=1)
        return rows[0] if rows else None
    return None


def update_coach_rule(rule_id: str, fields: Dict[str, Any]) -> None:
    row = _memstore.get(("coach_rules", rule_id))
    if row is not None:
        row.update(fields)
    if not _db_writable():
        return
    db_rows = _select_columns("coach_rules", filters={"id": rule_id}, limit=1)
    if db_rows:
        _upsert_columns("coach_rules", {**db_rows[0], **fields}, on_conflict="id")
    elif row is not None:
        _upsert_columns("coach_rules", row, on_conflict="id")


def insert_coach_rule_event(row: Dict[str, Any]) -> None:
    """Persist one rule violation event. The id is deterministic (hash of
    user|rule|trade identity), so re-audits of merged history are no-ops."""
    pk = row.get("id")
    _memstore[("coach_rule_events", pk)] = row
    if not _db_writable():
        return
    _upsert_columns("coach_rule_events", row, on_conflict="id")


def list_coach_rule_events(user_id: str, *, limit: int = 500) -> list:
    if _db_writable():
        return _select_columns("coach_rule_events", filters={"user_id": user_id},
                               order="occurred_on.desc", limit=limit)
    out = [r for (t, _), r in _memstore.items()
           if t == "coach_rule_events" and r.get("user_id") == user_id]
    out.sort(key=lambda r: r.get("occurred_on") or "", reverse=True)
    return out[:limit]


def get_coach_prefs(user_id: str) -> Dict[str, Any]:
    row = _memstore.get(("coach_prefs", user_id))
    if row is None and _db_writable():
        rows = _select_columns("coach_prefs", filters={"user_id": user_id}, limit=1)
        row = rows[0] if rows else None
        if row is not None:
            _memstore[("coach_prefs", user_id)] = row
    return row or {"user_id": user_id, "email_digest": False,
                   "digest_email": "", "unsubscribe_token": ""}


def upsert_coach_prefs(user_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    row = get_coach_prefs(user_id)
    row.update(fields)
    row["user_id"] = user_id
    row["updated_at"] = _ts_iso(time.time())
    _memstore[("coach_prefs", user_id)] = row
    if _db_writable():
        _upsert_columns("coach_prefs", row, on_conflict="user_id")
    return row


def find_coach_prefs_by_token(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    for (t, _), r in _memstore.items():
        if t == "coach_prefs" and r.get("unsubscribe_token") == token:
            return r
    if _db_writable():
        rows = _select_columns("coach_prefs", filters={"unsubscribe_token": token},
                               limit=1)
        return rows[0] if rows else None
    return None


def insert_coach_digest(row: Dict[str, Any]) -> bool:
    """Idempotent by pk `user_id|week_start`: returns False if that week's
    digest already exists."""
    pk = row.get("id") or f"{row.get('user_id')}|{row.get('week_start')}"
    row["id"] = pk
    if _memstore.get(("coach_digests", pk)) is not None:
        return False
    if _db_writable():
        existing = _select_columns("coach_digests", filters={"id": pk}, limit=1)
        if existing:
            _memstore[("coach_digests", pk)] = existing[0]
            return False
    _memstore[("coach_digests", pk)] = row
    if _db_writable():
        _upsert_columns("coach_digests", row, on_conflict="id")
    return True


def list_coach_digests(user_id: str, *, limit: int = 12) -> list:
    if _db_writable():
        return _select_columns("coach_digests", filters={"user_id": user_id},
                               order="week_start.desc", limit=limit)
    out = [r for (t, _), r in _memstore.items()
           if t == "coach_digests" and r.get("user_id") == user_id]
    out.sort(key=lambda r: r.get("week_start") or "", reverse=True)
    return out[:limit]


def insert_coach_partner(row: Dict[str, Any]) -> None:
    _memstore[("coach_partners", row["id"])] = row
    if _db_writable():
        _upsert_columns("coach_partners", row, on_conflict="id")


def list_coach_partners(user_id: str) -> list:
    if _db_writable():
        return _select_columns("coach_partners", filters={"user_id": user_id},
                               order="invited_at.desc", limit=10)
    out = [r for (t, _), r in _memstore.items()
           if t == "coach_partners" and r.get("user_id") == user_id]
    out.sort(key=lambda r: r.get("invited_at") or "", reverse=True)
    return out


def get_coach_partner(partner_id: str) -> Optional[Dict[str, Any]]:
    row = _memstore.get(("coach_partners", partner_id))
    if row is None and _db_writable():
        rows = _select_columns("coach_partners", filters={"id": partner_id}, limit=1)
        row = rows[0] if rows else None
    return row


def find_coach_partner_by_token(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    for (t, _), r in _memstore.items():
        if t == "coach_partners" and r.get("view_token") == token:
            return r
    if _db_writable():
        rows = _select_columns("coach_partners", filters={"view_token": token}, limit=1)
        return rows[0] if rows else None
    return None


def update_coach_partner(partner_id: str, fields: Dict[str, Any]) -> None:
    row = _memstore.get(("coach_partners", partner_id))
    if row is not None:
        row.update(fields)
    if not _db_writable():
        return
    db_rows = _select_columns("coach_partners", filters={"id": partner_id}, limit=1)
    if db_rows:
        _upsert_columns("coach_partners", {**db_rows[0], **fields}, on_conflict="id")
    elif row is not None:
        _upsert_columns("coach_partners", row, on_conflict="id")


# Every coach-owned table must appear here — tests assert this list covers all
# coach_*/referral migrations, so "delete all my data" can never silently miss
# a new table.
COACH_DATA_TABLES = ("coach_trades", "coach_audits", "coach_findings",
                     "coach_rules", "coach_rule_events", "coach_prefs",
                     "coach_digests", "coach_partners", "referral_attributions")


def delete_coach_data(user_id: str) -> Dict[str, int]:
    """Delete ALL of a user's coach data (trades, audits, findings, rules,
    violations, prefs, digests, partner links, referral attribution)."""
    n_trades = len(get_coach_trades(user_id))
    n_audits = len(list_coach_audits(user_id, limit=1000))
    n_findings = len(list_coach_findings(user_id, limit=10000))
    n_rules = len(list_coach_rules(user_id, limit=1000))
    _memstore.pop(("coach_trades", user_id), None)
    _memstore.pop(("coach_prefs", user_id), None)
    _memstore.pop(("referral_attributions", user_id), None)
    for table in ("coach_audits", "coach_findings", "coach_rules",
                  "coach_rule_events", "coach_digests", "coach_partners"):
        for key in [k for k in list(_memstore)
                    if k[0] == table and _memstore[k].get("user_id") == user_id]:
            _memstore.pop(key, None)
    if _db_writable():
        for table in COACH_DATA_TABLES:
            _delete_where(table, {"user_id": user_id})
    return {"trades": n_trades, "audits": n_audits, "findings": n_findings,
            "rules": n_rules}


def list_cohort_scores() -> List[float]:
    """Latest discipline score per signed-in user — the benchmark cohort.
    Heavy uploaders count once (latest row wins); guests are excluded."""
    if _db_writable():
        rows = _select_columns("coach_audits", filters={},
                               select="user_id,discipline_score,created_at",
                               limit=10000)
    else:
        rows = [r for (t, _), r in _memstore.items() if t == "coach_audits"]
    latest: Dict[str, tuple] = {}
    for r in rows:
        uid = r.get("user_id")
        if not uid or uid == ANONYMOUS_USER_ID:
            continue
        ts = r.get("created_at") or ""
        if uid not in latest or ts > latest[uid][0]:
            latest[uid] = (ts, r.get("discipline_score"))
    return [float(s) for _, s in latest.values() if s is not None]


def admin_coach_stats() -> Dict[str, int]:
    """Coach activation + findings funnel for the admin dashboard.
    Activation = a user with at least one persisted audit (their first
    quantified leak card; the `coach_activation` audit event marks the moment)."""
    if _db_writable():
        audits = _select_columns("coach_audits", filters={},
                                 select="user_id", limit=10000)
        findings = _select_columns("coach_findings", filters={},
                                   select="user_id,resolved_at,persisted", limit=10000)
    else:
        audits = [r for (t, _), r in _memstore.items() if t == "coach_audits"]
        findings = [r for (t, _), r in _memstore.items() if t == "coach_findings"]
    return {
        "activated_users": len({r.get("user_id") for r in audits if r.get("user_id")}),
        "total_audits": len(audits),
        "open_findings": sum(1 for f in findings if not f.get("resolved_at")),
        "resolved_findings": sum(1 for f in findings if f.get("resolved_at")),
        "leaks_fixed": sum(1 for f in findings
                           if f.get("resolved_at") and f.get("persisted") is False),
    }


def save_referral_attribution(user_id: str, code: str, source: str = "") -> bool:
    """First-touch referral attribution: record the code that brought this
    user, once. Returns True if newly recorded, False if one already exists."""
    if _memstore.get(("referral_attributions", user_id)) is not None:
        return False
    if _db_writable():
        existing = _select_columns("referral_attributions",
                                   filters={"user_id": user_id}, limit=1)
        if existing:
            _memstore[("referral_attributions", user_id)] = existing[0]
            return False
    row = {"user_id": user_id, "code": code, "source": source,
           "created_at": _ts_iso(time.time())}
    _memstore[("referral_attributions", user_id)] = row
    if _db_writable():
        _upsert_columns("referral_attributions", row, on_conflict="user_id")
    return True


def admin_referral_stats(*, limit: int = 10000) -> Dict[str, int]:
    """Signups attributed per referral code (attribution only — no rewards)."""
    if _db_writable():
        rows = _select_columns("referral_attributions", filters={},
                               select="code", limit=limit)
    else:
        rows = [r for (t, _), r in _memstore.items() if t == "referral_attributions"]
    counts: Dict[str, int] = {}
    for r in rows:
        code = (r.get("code") or "").strip()
        if code:
            counts[code] = counts.get(code, 0) + 1
    return counts


def _parse_iso_ts(v: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def admin_funnel_stats() -> Dict[str, Any]:
    """Coach funnel + health metrics from durable audit_log events.

    Honesty rules baked in: activation = first persisted audit; D30 retention
    counts USER-INITIATED audits only (origin != 'sync_auto' — the nightly cron
    writing audits is scheduler uptime, not retention); metrics are None when
    the underlying population is empty rather than fake zeros."""
    counts: Dict[str, int] = {}
    actors: Dict[str, set] = {}
    activation_rows: List[Dict[str, Any]] = []
    for action in ("demo_viewed", "upload_started", "audit_viewed",
                   "share_card_exported", "broker_connected", "coach_activation"):
        rows = list_audit(action=action, limit=10000)
        if len(rows) >= 10000:
            log.warning("admin_funnel_stats: %s hit the 10k page limit — counts are "
                        "truncated; move to a SQL count", action)
        counts[action] = len(rows)
        actors[action] = {r.get("actor") for r in rows if r.get("actor")}
        if action == "coach_activation":
            activation_rows = rows
    activated = len(actors["coach_activation"])
    share_rate = (round(len(actors["share_card_exported"]) / activated, 3)
                  if activated else None)

    # Median minutes from account creation to the first personal leak card.
    median_minutes_to_first_card = None
    user_created = {u.get("id"): u.get("created_at")
                    for u in admin_list_users() if u.get("id")}
    deltas = []
    for r in activation_rows:
        t0 = _parse_iso_ts(user_created.get(r.get("actor")))
        t1 = _parse_iso_ts(r.get("created_at"))
        if t0 and t1 and t1 >= t0:
            deltas.append((t1 - t0).total_seconds() / 60.0)
    if deltas:
        deltas.sort()
        median_minutes_to_first_card = round(deltas[len(deltas) // 2], 1)

    # D30 retention over user-initiated signals only.
    if _db_writable():
        audit_rows = _select_columns("coach_audits", filters={},
                                     select="user_id,created_at,origin", limit=10000)
    else:
        audit_rows = [r for (t, _), r in _memstore.items() if t == "coach_audits"]
    now = datetime.now(tz=timezone.utc)
    cohort = set()
    retained = set()
    activated_at = {}
    for r in activation_rows:
        ts = _parse_iso_ts(r.get("created_at"))
        if r.get("actor") and ts:
            activated_at[r["actor"]] = ts
    for uid, ts in activated_at.items():
        if (now - ts).days >= 30:
            cohort.add(uid)
    for r in audit_rows:
        uid = r.get("user_id")
        ts = _parse_iso_ts(r.get("created_at"))
        if (uid in cohort and ts and (now - ts).days <= 30
                and (r.get("origin") or "upload") != "sync_auto"):
            retained.add(uid)
    d30_retention = round(len(retained) / len(cohort), 3) if cohort else None

    return {
        "events": counts,
        "activated_users": activated,
        "share_rate": share_rate,
        "median_minutes_to_first_card": median_minutes_to_first_card,
        "d30_retention": d30_retention,
        "d30_cohort_size": len(cohort),
    }


def upsert_snaptrade_user(user_id: str, st_user_id: str, st_user_secret: str) -> None:
    """Store a user's SnapTrade userSecret (the long-lived read credential)."""
    row = {"user_id": user_id, "st_user_id": st_user_id,
           "st_user_secret": st_user_secret, "updated_at": _ts_iso(time.time())}
    _memstore[("snaptrade_users", user_id)] = row
    if not _db_writable():
        return
    _upsert_columns("snaptrade_users", row, on_conflict="user_id")


def get_snaptrade_user(user_id: str) -> Optional[Dict[str, Any]]:
    if _db_writable():
        rows = _select_columns("snaptrade_users", filters={"user_id": user_id}, limit=1)
        if rows:
            return rows[0]
    return _memstore.get(("snaptrade_users", user_id))


def list_all_snaptrade_users() -> list:
    """Every connected user's id — drives the nightly auto-sync (service-role only)."""
    if _db_writable():
        return _select_columns("snaptrade_users", filters={}, select="user_id", limit=10_000)
    return [{"user_id": r.get("user_id")}
            for (t, _), r in _memstore.items() if t == "snaptrade_users"]


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

def save_transactions(rows: List[Dict[str, Any]]) -> int:
    """Bulk-persist uploaded brokerage transactions. Each row must carry
    id + user_id. Mirrors the journal storage pattern: memstore always,
    Supabase when writable. Returns the number stored."""
    n = 0
    for row in rows:
        _memstore[("transactions", row["id"])] = row
        n += 1
    if _db_writable():
        for row in rows:
            try:
                _upsert_columns("transactions", row, on_conflict="id")
            except Exception as exc:  # noqa: BLE001
                log.warning("transactions db write failed: %s", exc)
    return n


def list_transactions(
    user_id: str,
    *,
    limit: int = 2000,
    batch_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return a user's saved transactions, most recent first."""
    if _db_writable():
        filters: Dict[str, Any] = {"user_id": user_id}
        if batch_id:
            filters["batch_id"] = batch_id
        return _select_columns(
            "transactions", filters=filters,
            order="created_at.desc", limit=limit,
        )
    out = [
        r for (t, _), r in _memstore.items()
        if t == "transactions" and r.get("user_id") == user_id
        and (not batch_id or r.get("batch_id") == batch_id)
    ]
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out[:limit]


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


# --- waitlist ---------------------------------------------------------------

def save_waitlist_signup(row: Dict[str, Any]) -> None:
    """Persist a waitlist signup. Dual-mode like the rest of storage: Postgres
    when configured, in-memory otherwise. Keyed on the (lower-cased) email so a
    repeat signup updates the existing row rather than duplicating.

    Caller supplies the full dict (id, email, name, company, note, source,
    created_at). The DB table is `waitlist_signups` with a unique `email`."""
    _memstore[("waitlist_signups", row["email"])] = row
    if not _db_writable():
        return
    _upsert_columns("waitlist_signups", row, on_conflict="email")


def get_waitlist_signup(email: str) -> Optional[Dict[str, Any]]:
    key = (email or "").strip().lower()
    if _db_writable():
        rows = _select_columns("waitlist_signups", filters={"email": key}, limit=1)
        if rows:
            return rows[0]
    return _memstore.get(("waitlist_signups", key))


def list_waitlist_signups(*, limit: int = 5000) -> List[Dict[str, Any]]:
    """All signups, newest first. Admin-only surface (export)."""
    if _db_writable():
        rows = _select_columns(
            "waitlist_signups", filters={}, order="created_at.desc", limit=limit,
        )
        if rows:
            return rows
    rows = [
        v for (t, _), v in _memstore.items() if t == "waitlist_signups"
    ]
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows[:limit]


def count_waitlist_signups() -> int:
    return len(list_waitlist_signups(limit=100000))


# --- test helper -----------------------------------------------------------

def _reset_memstore_for_tests() -> None:
    """Drop all in-memory storage. Pytest fixture helper."""
    _memstore.clear()
    _token_cache.clear()
