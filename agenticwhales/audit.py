"""Audit logging + impersonation safety.

Two responsibilities:

1. `audit(actor, action, ...)` writes a row to `public.audit_log`. Append-only;
   nothing in this module ever updates or deletes.

2. `impersonate(user_id, purpose, fire_id=)` yields an `ImpersonationToken`
   that capability-types every server-side action on behalf of a user. Storage
   helpers that write user-scoped rows accept either `ImpersonationToken` or
   `UserJWTContext` — never a bare `user_id` string. This makes it impossible
   for a refactor to accidentally bypass the audit trail.

Audit-log writes are best-effort: if Supabase is down or unconfigured, we
log to the local logger and continue. Audit is for forensics, not for
preventing actions — the right defense for the latter is RLS + scoped tokens.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from .agents.schemas import ImpersonationToken

log = logging.getLogger(__name__)


def audit(
    actor: str,
    action: str,
    *,
    target_user_id: Optional[str] = None,
    target_resource: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Append-only write to `audit_log`. Failures log but don't raise."""
    try:
        from web import auth  # lazy to avoid import cycle
        auth.append_audit(
            actor=actor,
            action=action,
            target_user_id=target_user_id,
            target_resource=target_resource,
            metadata=metadata or {},
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("audit write failed (%s/%s): %s", actor, action, exc)


@contextmanager
def impersonate(
    user_id: str,
    purpose: str = "scheduler_fire",
    fire_id: Optional[str] = None,
) -> Iterator[ImpersonationToken]:
    """Issue a typed capability token for server-side actions on behalf of a user.

    Audit-logged on entry and exit. Token is not persisted; lives only for the
    duration of the `with` block. Storage helpers MUST accept this token
    (or a JWT user context) — bare-string user_ids should fail mypy.

    Usage:
        with impersonate(recipe.user_id, "scheduler_fire", fire_id=fid) as tok:
            paper.place_order(tok, ticker="AAPL", side="buy", ...)
    """
    if purpose not in ("scheduler_fire", "admin_export", "support_view", "outcome_resolver"):
        raise ValueError(f"unknown impersonation purpose: {purpose}")

    token = ImpersonationToken(
        user_id=user_id,
        issued_at=time.time(),
        purpose=purpose,  # type: ignore[arg-type]
        fire_id=fire_id,
    )
    audit(
        "system",
        "impersonate.begin",
        target_user_id=user_id,
        metadata={"purpose": purpose, "fire_id": fire_id},
    )
    try:
        yield token
    finally:
        audit(
            "system",
            "impersonate.end",
            target_user_id=user_id,
            metadata={"purpose": purpose, "fire_id": fire_id},
        )
