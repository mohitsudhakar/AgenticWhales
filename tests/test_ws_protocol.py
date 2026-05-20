"""WebSocket protocol tests for /api/sessions/{sid}/stream.

The /fund hero opens this WS to render the live multi-agent debate. We need
to know the event types it emits and the order, so the renderer logic stays
in sync with the runner. We feed canned events directly into the runner's
broadcast queue and assert the WS hands them out in order.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from web import auth, server
from web.runner import SessionRunner


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


def _attach_runner_for_user(session_id: str, user_id: str) -> SessionRunner:
    """Build a minimal session dict + register a runner in web/server._runners
    so the WS endpoint will hand us a live stream."""
    session = {
        "id": session_id,
        "user_id": user_id,
        "ticker": "AAPL",
        "analysis_date": "2024-06-01",
        "status": "running",
        "agent_status": {},
        "report_sections": {},
        "messages": [],
        "config": {
            "llm_provider": "google",
            "quick_think_llm": "gemini-3-flash-preview",
            "deep_think_llm": "gemini-3.1-pro-preview",
        },
        "created_at": time.time(),
    }
    loop = asyncio.new_event_loop()
    runner = SessionRunner(session, loop)
    server._runners[session_id] = runner
    return runner


class TestSessionStream:
    def test_emits_initial_session_event(self, client):
        sid = "ws-test-1"
        _attach_runner_for_user(sid, "anonymous")

        with client.websocket_connect(f"/api/sessions/{sid}/stream") as ws:
            event = ws.receive_json()
            assert event["type"] == "session"
            assert event["session"]["id"] == sid
            assert event["session"]["ticker"] == "AAPL"

    def test_unknown_session_closes(self, client):
        # No runner registered, no row in storage → 4404 close.
        with pytest.raises(Exception):
            with client.websocket_connect("/api/sessions/does-not-exist/stream") as ws:
                ws.receive_json()


class TestStreamingEventTypes:
    """The hero / decision panel relies on a fixed set of WS event types.
    This locks the protocol shape so a runner refactor that drops one of
    them breaks here instead of silently breaking the UI."""

    def test_runner_broadcast_emits_documented_types(self):
        """Lock the protocol shape: the runner only emits event types that
        appear in this allowlist. Adding a new emission requires updating
        both the allowlist AND the hero renderer in fund.js
        (`handleHeroEvent`) — those must stay in lockstep so the well-lit
        path keeps rendering everything the runner produces."""
        documented = {
            "session", "agent_status", "report", "message",
            "stats", "team_timing", "risk_event", "tool_call",
            "paper_order", "conviction_alert", "disagreement",
            "classical_voice", "adaptive_depth_escalation",
        }
        # Mine the runner source for `"type": <literal>` payload constructions.
        import re
        from pathlib import Path
        src = Path("web/runner.py").read_text()
        found = set(re.findall(r'"type"\s*:\s*"([a-z_]+)"', src))
        unknown = found - documented
        assert unknown == set(), (
            f"Runner emits undocumented WS event types: {unknown}. "
            f"Add them to this test's allowlist AND wire a renderer in "
            f"fund.js handleHeroEvent() at the same time."
        )
