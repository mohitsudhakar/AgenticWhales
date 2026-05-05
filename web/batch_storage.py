"""JSON-file persistence for batch analysis runs."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOCK = threading.RLock()
_BASE = Path(os.path.expanduser("~/.tradingagents/web_batches"))


def _path(batch_id: str) -> Path:
    return _BASE / f"{batch_id}.json"


def ensure_dir() -> None:
    _BASE.mkdir(parents=True, exist_ok=True)


def save(batch: Dict[str, Any]) -> None:
    ensure_dir()
    bid = batch["id"]
    with _LOCK:
        tmp = _path(bid).with_suffix(".tmp")
        tmp.write_text(json.dumps(batch, default=str))
        tmp.replace(_path(bid))
    # Best-effort mirror to Supabase Postgres.
    try:
        from . import auth as _auth
        _auth.mirror_batch(batch)
    except Exception:
        pass


def load(batch_id: str) -> Optional[Dict[str, Any]]:
    p = _path(batch_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def list_all(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return all batches, optionally filtered to those owned by user_id."""
    ensure_dir()
    out: List[Dict[str, Any]] = []
    for p in _BASE.glob("*.json"):
        try:
            b = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if user_id is not None and b.get("user_id") != user_id:
            continue
        out.append(b)
    out.sort(key=lambda b: b.get("created_at", 0), reverse=True)
    return out


def delete(batch_id: str) -> bool:
    p = _path(batch_id)
    if p.exists():
        p.unlink()
        try:
            from . import auth as _auth
            _auth.delete_batch_row(batch_id)
        except Exception:
            pass
        return True
    return False
