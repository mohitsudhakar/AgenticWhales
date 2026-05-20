"""Tests for the impersonation context manager + audit log."""

from __future__ import annotations

import pytest

from agenticwhales.agents.schemas import ImpersonationToken
from agenticwhales.audit import audit, impersonate
from web import auth


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


class TestImpersonate:
    def test_yields_valid_token(self):
        with impersonate("u-1", "scheduler_fire", fire_id="fire-1") as tok:
            assert isinstance(tok, ImpersonationToken)
            assert tok.user_id == "u-1"
            assert tok.purpose == "scheduler_fire"
            assert tok.fire_id == "fire-1"
            assert tok.issued_at > 0

    def test_audit_logged_on_begin_and_end(self):
        with impersonate("u-1", "scheduler_fire"):
            pass
        # Memstore audit_log has at least the two rows.
        audit_rows = [
            v for (table, _), v in auth._memstore.items()
            if table == "audit_log"
        ]
        actions = {r["action"] for r in audit_rows}
        assert "impersonate.begin" in actions
        assert "impersonate.end" in actions

    def test_rejects_unknown_purpose(self):
        with pytest.raises(ValueError):
            with impersonate("u-1", "definitely-not-allowed"):
                pass

    def test_token_is_frozen(self):
        with impersonate("u-1", "scheduler_fire") as tok:
            with pytest.raises(Exception):
                tok.user_id = "different"  # type: ignore[misc]


class TestAudit:
    def test_writes_metadata(self):
        audit("system", "test.action", target_user_id="u-1", metadata={"k": "v"})
        rows = [v for (t, _), v in auth._memstore.items() if t == "audit_log"]
        assert any(r["action"] == "test.action" and r["metadata"] == {"k": "v"} for r in rows)
