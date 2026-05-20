"""Tests for the /fund streaming-fires panel: `auth.list_audit` filter behavior
and the CLI `stream test` exit codes when creds are missing."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from web import auth


@pytest.fixture(autouse=True)
def clear_memstore():
    # Sandbox audit_log rows; ensure each test starts clean.
    keys = [k for k in list(auth._memstore.keys()) if k[0] == "audit_log"]
    for k in keys:
        auth._memstore.pop(k, None)
    yield
    keys = [k for k in list(auth._memstore.keys()) if k[0] == "audit_log"]
    for k in keys:
        auth._memstore.pop(k, None)


def _seed(actor: str, action: str, target_user_id: str | None = None,
          metadata: dict | None = None) -> None:
    auth.append_audit(
        actor=actor, action=action,
        target_user_id=target_user_id,
        metadata=metadata,
    )


class TestListAudit:
    def test_filter_by_action(self):
        _seed("system", "streaming.fire", "u-1", {"recipe_id": "r1", "symbol": "AAPL"})
        _seed("system", "scheduler.leader.acquire")
        rows = auth.list_audit(action="streaming.fire")
        assert len(rows) == 1
        assert rows[0]["action"] == "streaming.fire"

    def test_filter_by_target_user(self):
        _seed("system", "streaming.fire", "u-1", {"symbol": "AAPL"})
        _seed("system", "streaming.fire", "u-2", {"symbol": "MSFT"})
        rows = auth.list_audit(action="streaming.fire", target_user_id="u-1")
        assert len(rows) == 1
        assert rows[0]["target_user_id"] == "u-1"

    def test_most_recent_first(self):
        import time as _t
        _seed("system", "streaming.fire", "u-1", {"symbol": "AAPL"})
        _t.sleep(0.01)
        _seed("system", "streaming.fire", "u-1", {"symbol": "MSFT"})
        rows = auth.list_audit(action="streaming.fire", target_user_id="u-1")
        assert len(rows) == 2
        assert rows[0]["metadata"]["symbol"] == "MSFT"
        assert rows[1]["metadata"]["symbol"] == "AAPL"

    def test_limit(self):
        for i in range(5):
            _seed("system", "streaming.fire", "u-1", {"symbol": f"T{i}"})
        rows = auth.list_audit(action="streaming.fire", target_user_id="u-1", limit=3)
        assert len(rows) == 3

    def test_empty_returns_empty_list(self):
        assert auth.list_audit(action="streaming.fire") == []


class TestStreamCLI:
    def test_missing_creds_exits_two(self, monkeypatch):
        monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
        monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
        # Single-command Typer apps auto-collapse so the subcommand name is
        # implicit — invoke with just the options.
        from cli.stream import app as stream_app
        runner = CliRunner()
        result = runner.invoke(stream_app, ["--ticker", "AAPL", "--seconds", "1"])
        assert result.exit_code == 2
        assert "not set in env" in result.output
