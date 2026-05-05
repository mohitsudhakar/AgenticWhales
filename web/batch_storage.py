"""Persistence for basket (batch) runs — thin wrapper around web.auth's
Postgres CRUD."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import auth


def ensure_dir() -> None:
    """No-op kept for backwards-compat with the old disk-backed API."""


def save(batch: Dict[str, Any]) -> None:
    auth.save_batch(batch)


def load(batch_id: str) -> Optional[Dict[str, Any]]:
    return auth.load_batch(batch_id)


def list_all(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    if user_id is None:
        return []
    return auth.list_batches(user_id)


def delete(batch_id: str) -> bool:
    return auth.delete_batch(batch_id)
