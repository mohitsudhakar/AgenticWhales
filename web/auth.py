"""Supabase auth + Postgres mirror helpers.

Two responsibilities:
  1. Validate a Supabase JWT (access token) and return the user id (UUID)
     of the caller. We do this by calling Supabase's /auth/v1/user endpoint
     instead of verifying the JWT locally — saves a dependency and the
     JWT-secret env var. Adds one network round trip per request, which is
     fine for a personal app.
  2. Best-effort mirror of session/batch index rows to Supabase Postgres,
     using the service_role key. Failures are swallowed — the JSON files
     on disk remain the source of truth, so a Postgres outage doesn't
     break the user's flow.

If Supabase isn't configured (env vars missing), get_current_user_id falls
back to a single shared "anonymous" bucket so local development still works.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from fastapi import Header, HTTPException, Query, WebSocket

log = logging.getLogger(__name__)

ANONYMOUS_USER_ID = "anonymous"

# Tiny in-process cache for token → user_id to avoid hammering Supabase on
# every API call. Tokens themselves expire (~1h), so a 60-second TTL is
# plenty short.
_TOKEN_CACHE_TTL = 60.0
_token_cache: Dict[str, tuple[float, str]] = {}


def _supabase_url() -> Optional[str]:
    return os.getenv("AGENTICWHALES_SUPABASE_URL")


def _supabase_anon_key() -> Optional[str]:
    return os.getenv("AGENTICWHALES_SUPABASE_ANON_KEY")


def _supabase_service_key() -> Optional[str]:
    return os.getenv("AGENTICWHALES_SUPABASE_SERVICE_KEY")


def _supabase_configured() -> bool:
    return bool(_supabase_url() and _supabase_anon_key())


# ------------------------------------------------------------------
# JWT validation
# ------------------------------------------------------------------

def _validate_token(token: str) -> Optional[str]:
    """Hit Supabase /auth/v1/user with the bearer token; return user id on
    success, None on any failure (invalid token, network error, etc)."""
    cached = _token_cache.get(token)
    if cached and cached[0] > time.time():
        return cached[1]
    base = _supabase_url()
    anon = _supabase_anon_key()
    if not base or not anon:
        return None
    try:
        resp = requests.get(
            f"{base}/auth/v1/user",
            headers={"apikey": anon, "Authorization": f"Bearer {token}"},
            timeout=5,
        )
    except requests.RequestException as e:
        log.warning("auth: Supabase /auth/v1/user request failed: %s", e)
        return None
    if resp.status_code != 200:
        return None
    uid = (resp.json() or {}).get("id")
    if uid:
        _token_cache[token] = (time.time() + _TOKEN_CACHE_TTL, uid)
    return uid


def get_current_user_id(authorization: Optional[str] = Header(None)) -> str:
    """FastAPI dependency. Validates the Authorization: Bearer <jwt> header
    via Supabase and returns the user id. If Supabase isn't configured at
    all, falls back to a shared 'anonymous' user so local dev still works."""
    if not _supabase_configured():
        return ANONYMOUS_USER_ID
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing Authorization bearer token")
    token = authorization.split(None, 1)[1].strip()
    uid = _validate_token(token)
    if not uid:
        raise HTTPException(401, "Invalid or expired auth token")
    return uid


async def authenticate_websocket(ws: WebSocket, token: Optional[str]) -> Optional[str]:
    """Validate a WebSocket connection's ?token=... query param. On failure,
    closes the socket with an appropriate code and returns None."""
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
# Postgres mirror (best effort, service_role)
# ------------------------------------------------------------------

def _rest_url(table: str) -> Optional[str]:
    base = _supabase_url()
    return f"{base}/rest/v1/{table}" if base else None


def _service_headers() -> Optional[Dict[str, str]]:
    key = _supabase_service_key()
    if not key:
        return None
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _ts_iso(epoch: Optional[float]) -> Optional[str]:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _post(table: str, rows: List[Dict[str, Any]]) -> None:
    url = _rest_url(table)
    headers = _service_headers()
    if not url or not headers or url.endswith("YOUR-PROJECT-REF.supabase.co/rest/v1/" + table):
        return
    h = {**headers, "Prefer": "resolution=merge-duplicates,return=minimal"}
    try:
        resp = requests.post(url, headers=h, json=rows, timeout=5)
        if resp.status_code >= 300:
            log.debug("supabase mirror %s -> %s: %s", table, resp.status_code, resp.text[:200])
    except requests.RequestException as e:
        log.debug("supabase mirror %s failed: %s", table, e)


def mirror_session(session: Dict[str, Any]) -> None:
    """Upsert a session row into public.sessions. Skipped silently if the
    service_role key isn't configured."""
    user_id = session.get("user_id")
    if not user_id or user_id == ANONYMOUS_USER_ID:
        return
    _post("sessions", [{
        "id": session["id"],
        "user_id": user_id,
        "ticker": session.get("ticker"),
        "analysis_date": session.get("analysis_date"),
        "status": session.get("status"),
        "completed_at": _ts_iso(session.get("completed_at")),
        "data": session,
    }])


def mirror_batch(batch: Dict[str, Any]) -> None:
    user_id = batch.get("user_id")
    if not user_id or user_id == ANONYMOUS_USER_ID:
        return
    _post("batches", [{
        "id": batch["id"],
        "user_id": user_id,
        "analysis_date": batch.get("analysis_date"),
        "status": batch.get("status"),
        "ticker_count": len(batch.get("items", [])),
        "completed_at": _ts_iso(batch.get("completed_at")),
        "data": batch,
    }])


def delete_session_row(sid: str) -> None:
    url = _rest_url("sessions")
    headers = _service_headers()
    if not url or not headers:
        return
    try:
        requests.delete(f"{url}?id=eq.{sid}", headers=headers, timeout=5)
    except requests.RequestException:
        pass


def delete_batch_row(bid: str) -> None:
    url = _rest_url("batches")
    headers = _service_headers()
    if not url or not headers:
        return
    try:
        requests.delete(f"{url}?id=eq.{bid}", headers=headers, timeout=5)
    except requests.RequestException:
        pass
