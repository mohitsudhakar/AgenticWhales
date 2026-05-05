"""Persistence for analysis sessions — thin wrapper around web.auth's
Postgres CRUD. Postgres is the source of truth; if Supabase isn't
configured, web.auth falls back to a process-local in-memory store."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import auth


def ensure_dir() -> None:
    """No-op kept for backwards-compat with the old disk-backed API. The
    server still calls this on startup."""


def save(session: Dict[str, Any]) -> None:
    auth.save_session(session)


def load(session_id: str) -> Optional[Dict[str, Any]]:
    return auth.load_session(session_id)


def list_all(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    if user_id is None:
        # Old call site without filtering — Postgres listing is keyed on
        # user_id, so an unfiltered list isn't supported anymore. Callers
        # in server.py always pass a user_id; this branch only exists to
        # avoid a hard error if something stale slips through.
        return []
    return auth.list_sessions(user_id)


def delete(session_id: str) -> bool:
    return auth.delete_session(session_id)
