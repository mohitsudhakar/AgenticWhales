"""JSON-file persistence for web analysis sessions."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOCK = threading.RLock()
_BASE = Path(os.path.expanduser("~/.tradingagents/web_sessions"))


def _path(session_id: str) -> Path:
    return _BASE / f"{session_id}.json"


def ensure_dir() -> None:
    _BASE.mkdir(parents=True, exist_ok=True)


def save(session: Dict[str, Any]) -> None:
    ensure_dir()
    sid = session["id"]
    with _LOCK:
        tmp = _path(sid).with_suffix(".tmp")
        tmp.write_text(json.dumps(session, default=str))
        tmp.replace(_path(sid))
    # Best-effort mirror to Supabase Postgres so the sidebar can list across
    # devices / restarts. Lazy import keeps the storage module usable without
    # the auth helper (and avoids circular imports).
    try:
        from . import auth as _auth
        _auth.mirror_session(session)
    except Exception:
        pass


def load(session_id: str) -> Optional[Dict[str, Any]]:
    p = _path(session_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def list_all(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return all sessions, optionally filtered to those owned by user_id.
    Sessions saved before per-user scoping landed have no user_id and will
    therefore be filtered out (orphaned)."""
    ensure_dir()
    out: List[Dict[str, Any]] = []
    for p in _BASE.glob("*.json"):
        try:
            s = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if user_id is not None and s.get("user_id") != user_id:
            continue
        out.append(s)
    out.sort(key=lambda s: s.get("created_at", 0), reverse=True)
    return out


def delete(session_id: str) -> bool:
    p = _path(session_id)
    if p.exists():
        p.unlink()
        try:
            from . import auth as _auth
            _auth.delete_session_row(session_id)
        except Exception:
            pass
        return True
    return False
