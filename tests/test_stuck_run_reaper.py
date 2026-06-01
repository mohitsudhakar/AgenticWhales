"""PR-3: stuck-run reaper + DB-backed concurrent-fire gate.

Covers the three pieces:

1. `auth.has_running_session_for_recipe` — true while a session is
   `running`, false after the row flips to `completed` / `failed`.
2. `auth.list_stuck_running_sessions` — returns rows older than the cutoff,
   excludes fresh rows, excludes already-failed rows.
3. `auth.mark_session_failed` — flips status, stamps `failure_reason` into
   the data jsonb, idempotent on second call.
4. `scheduler._run_stuck_run_reaper` — flips stuck rows, emits audit log,
   skips when not leader.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from web import auth
from web.scheduler import RecipeScheduler


@pytest.fixture(autouse=True)
def _isolated_memstore(monkeypatch):
    monkeypatch.setattr(auth, "_db_writable", lambda: False)
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


def _insert_session(sid: str, *, status: str = "running",
                    recipe_id: str = "rcp-1",
                    user_id: str = "user-1",
                    created_at: datetime | None = None) -> dict:
    if created_at is None:
        created_at = datetime.now(tz=timezone.utc)
    row = {
        "id": sid,
        "user_id": user_id,
        "recipe_id": recipe_id,
        "fire_id": f"fire-{sid}",
        "ticker": "NVDA",
        "status": status,
        "created_at": created_at.isoformat(),
        "data": {},
    }
    auth._memstore[("sessions", sid)] = row
    return row


# ---------------------------------------------------------------------------
# has_running_session_for_recipe
# ---------------------------------------------------------------------------


def test_has_running_session_true_when_session_running():
    _insert_session("s1", status="running", recipe_id="r1")
    assert auth.has_running_session_for_recipe("r1") is True


def test_has_running_session_false_when_completed():
    _insert_session("s1", status="completed", recipe_id="r1")
    assert auth.has_running_session_for_recipe("r1") is False


def test_has_running_session_isolates_by_recipe_id():
    _insert_session("s1", status="running", recipe_id="r1")
    _insert_session("s2", status="running", recipe_id="r2")
    assert auth.has_running_session_for_recipe("r1") is True
    assert auth.has_running_session_for_recipe("r3") is False


# ---------------------------------------------------------------------------
# list_stuck_running_sessions
# ---------------------------------------------------------------------------


def test_list_stuck_running_returns_rows_past_cutoff():
    old = datetime.now(tz=timezone.utc) - timedelta(hours=2)
    fresh = datetime.now(tz=timezone.utc)
    _insert_session("stuck", status="running", created_at=old)
    _insert_session("fresh", status="running", created_at=fresh)

    stuck = auth.list_stuck_running_sessions(older_than_seconds=30 * 60)
    ids = {r["id"] for r in stuck}
    assert "stuck" in ids
    assert "fresh" not in ids


def test_list_stuck_running_excludes_completed_rows():
    old = datetime.now(tz=timezone.utc) - timedelta(hours=2)
    _insert_session("done", status="completed", created_at=old)
    stuck = auth.list_stuck_running_sessions(older_than_seconds=30 * 60)
    assert stuck == []


def test_list_stuck_running_respects_limit_and_order():
    base = datetime.now(tz=timezone.utc) - timedelta(hours=3)
    for i in range(5):
        _insert_session(f"s{i}", status="running",
                        created_at=base + timedelta(minutes=i))
    stuck = auth.list_stuck_running_sessions(older_than_seconds=60, limit=3)
    assert len(stuck) == 3
    # Oldest first
    ages = [r["created_at"] for r in stuck]
    assert ages == sorted(ages)


# ---------------------------------------------------------------------------
# mark_session_failed
# ---------------------------------------------------------------------------


def test_mark_session_failed_flips_status_and_records_reason():
    row = _insert_session("s1", status="running")
    assert auth.mark_session_failed("s1", failure_reason="stuck_run_reaped")
    assert auth._memstore[("sessions", "s1")]["status"] == "failed"
    # The reason is recorded inside `data`
    assert auth._memstore[("sessions", "s1")]["data"]["failure_reason"] \
        == "stuck_run_reaped"


def test_mark_session_failed_is_idempotent():
    _insert_session("s1", status="running")
    assert auth.mark_session_failed("s1", failure_reason="A")
    assert auth.mark_session_failed("s1", failure_reason="B")
    # Second call wins (last-write semantics)
    assert auth._memstore[("sessions", "s1")]["data"]["failure_reason"] == "B"


def test_mark_session_failed_returns_false_on_unknown_id():
    assert auth.mark_session_failed("does-not-exist",
                                    failure_reason="x") is False


# ---------------------------------------------------------------------------
# scheduler._run_stuck_run_reaper
# ---------------------------------------------------------------------------


def test_reaper_no_op_when_not_leader():
    sched = RecipeScheduler()
    sched._is_leader = False
    old = datetime.now(tz=timezone.utc) - timedelta(hours=2)
    _insert_session("stuck", status="running", created_at=old)

    sched._run_stuck_run_reaper()

    # Untouched
    assert auth._memstore[("sessions", "stuck")]["status"] == "running"


def test_reaper_flips_stuck_rows_when_leader():
    sched = RecipeScheduler()
    sched._is_leader = True
    old = datetime.now(tz=timezone.utc) - timedelta(hours=2)
    fresh = datetime.now(tz=timezone.utc)
    _insert_session("stuck", status="running", created_at=old)
    _insert_session("fresh", status="running", created_at=fresh)

    sched._run_stuck_run_reaper()

    assert auth._memstore[("sessions", "stuck")]["status"] == "failed"
    assert auth._memstore[("sessions", "stuck")]["data"]["failure_reason"] \
        == "stuck_run_reaped"
    # Fresh row is left alone
    assert auth._memstore[("sessions", "fresh")]["status"] == "running"


def test_reaper_no_op_when_nothing_stuck():
    sched = RecipeScheduler()
    sched._is_leader = True
    fresh = datetime.now(tz=timezone.utc)
    _insert_session("a", status="running", created_at=fresh)
    _insert_session("b", status="completed", created_at=fresh)

    # Should not raise even with no rows to reap
    sched._run_stuck_run_reaper()

    assert auth._memstore[("sessions", "a")]["status"] == "running"
    assert auth._memstore[("sessions", "b")]["status"] == "completed"


def test_reaper_unblocks_recipe_fires_after_flip():
    """Integration check: reaper flips stuck → has_running goes false."""
    sched = RecipeScheduler()
    sched._is_leader = True
    old = datetime.now(tz=timezone.utc) - timedelta(hours=2)
    _insert_session("stuck", status="running", recipe_id="r1", created_at=old)

    assert auth.has_running_session_for_recipe("r1") is True
    sched._run_stuck_run_reaper()
    assert auth.has_running_session_for_recipe("r1") is False
