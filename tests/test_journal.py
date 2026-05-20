"""Tests for the Phase 2 Journal — CRUD + post-decision auto-draft.

The auto-draft path is the user-felt change that defines Phase 2 (user-felt
change #1: "The fund auto-drafted a journal entry after the debate; I edited
and saved.") — covered here with a synthetic PortfolioDecision so we don't
need a live LLM.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import pytest

from agenticwhales.agents.schemas import (
    JournalEntry,
    JournalKind,
    PortfolioDecision,
    PortfolioRating,
)
from web import auth


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


def _decision() -> PortfolioDecision:
    return PortfolioDecision(
        rating=PortfolioRating.BUY,
        executive_summary="Tactical entry on momentum + Quant Radar trend strength",
        investment_thesis="Underlying broke above 50/200 SMA with expanding RSI",
        expected_return_pct=8.5,
        expected_volatility_pct=18.0,
        prob_of_profit=0.62,
        expected_hold_days=30,
        stop_loss=180.0,
        take_profit=210.0,
    )


# ---------------------------------------------------------------------------
# Pydantic schema
# ---------------------------------------------------------------------------

class TestJournalSchema:
    def test_required_fields(self):
        e = JournalEntry(
            id=uuid.uuid4().hex, user_id="u-1",
            body="Quick note", kind=JournalKind.NOTE,
        )
        assert e.kind == JournalKind.NOTE
        assert not e.is_draft
        assert e.sentiment_score is None

    def test_body_must_be_nonempty(self):
        with pytest.raises(Exception):
            JournalEntry(id="x", user_id="u-1", body="")

    def test_sentiment_range(self):
        with pytest.raises(Exception):
            JournalEntry(id="x", user_id="u-1", body="ok", sentiment_score=150)
        with pytest.raises(Exception):
            JournalEntry(id="x", user_id="u-1", body="ok", sentiment_score=-150)


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

class TestStorageRoundTrip:
    def test_save_then_load(self):
        row = _row("u-1", session_id="s-1", body="hello")
        auth.save_journal_entry(row)
        loaded = auth.load_journal_entry(row["id"])
        assert loaded is not None
        assert loaded["body"] == "hello"
        assert loaded["session_id"] == "s-1"

    def test_list_filters(self):
        # Three users, multiple sessions, mix of drafts.
        auth.save_journal_entry(_row("u-1", session_id="s-1", kind="auto_draft", is_draft=True))
        auth.save_journal_entry(_row("u-1", session_id="s-1", kind="note"))
        auth.save_journal_entry(_row("u-1", session_id="s-2", kind="reflection"))
        auth.save_journal_entry(_row("u-2", session_id="s-1", kind="note"))

        all_u1 = auth.list_journal_entries("u-1")
        assert len(all_u1) == 3

        no_drafts = auth.list_journal_entries("u-1", include_drafts=False)
        assert len(no_drafts) == 2

        s1_only = auth.list_journal_entries("u-1", session_id="s-1")
        assert len(s1_only) == 2

        only_reflections = auth.list_journal_entries("u-1", kind="reflection")
        assert len(only_reflections) == 1

    def test_delete(self):
        row = _row("u-1", body="kill me")
        auth.save_journal_entry(row)
        assert auth.delete_journal_entry(row["id"]) is True
        assert auth.load_journal_entry(row["id"]) is None


def _row(user_id, **overrides):
    now = datetime.now(tz=timezone.utc).isoformat()
    r = {
        "id": uuid.uuid4().hex,
        "user_id": user_id,
        "session_id": None,
        "paper_order_id": None,
        "thesis_id": None,
        "kind": "note",
        "body": "test body",
        "sentiment_score": None,
        "is_draft": False,
        "created_at": now,
        "updated_at": now,
    }
    r.update(overrides)
    return r


# ---------------------------------------------------------------------------
# Auto-draft from the runner
# ---------------------------------------------------------------------------

class TestAutoDraft:
    def _runner(self, user_id="u-1", session_id="sess-1", recipe_id="r-1"):
        """Build a minimal SessionRunner-like object that exposes only the
        method we need. We intentionally don't instantiate the real
        SessionRunner (it pulls in LangGraph + threading); the auto-draft
        method is self-contained."""
        from web.runner import SessionRunner

        # Hand-craft a session dict with the fields auto-draft reads.
        session = {
            "id": session_id,
            "user_id": user_id,
            "ticker": "AAPL",
            "analysis_date": "2026-05-19",
            "config": {},
            "agent_status": {},
            "report_sections": {},
            "messages": [],
            "stats": {},
            "team_timings": {},
            "recipe_id": recipe_id,
        }
        # Build the runner with a no-op event loop sentinel; we won't call .start().
        import asyncio
        try:
            loop = asyncio.new_event_loop()
        except Exception:
            loop = None
        return SessionRunner(session, loop)

    def test_auto_draft_writes_one_entry(self):
        runner = self._runner()
        runner._auto_draft_journal(
            user_id="u-1", session_id="sess-1", recipe_id="r-1",
            decision=_decision(), ticker="AAPL", conviction=8,
        )
        entries = auth.list_journal_entries("u-1", kind="auto_draft")
        assert len(entries) == 1
        e = entries[0]
        assert e["session_id"] == "sess-1"
        assert e["thesis_id"] == "r-1"
        assert e["is_draft"] is True
        assert "AAPL" in e["body"]
        assert "Buy" in e["body"]
        assert "conviction 8/10" in e["body"]
        # Body should mention the scalars the PM produced.
        assert "8.5%" in e["body"]
        assert "62%" in e["body"]

    def test_auto_draft_dedupes_per_session(self):
        runner = self._runner()
        for _ in range(3):
            runner._auto_draft_journal(
                user_id="u-1", session_id="sess-1", recipe_id="r-1",
                decision=_decision(), ticker="AAPL", conviction=8,
            )
        # Three calls → still only one auto_draft per session.
        entries = auth.list_journal_entries("u-1", kind="auto_draft")
        assert len(entries) == 1

    def test_auto_draft_handles_missing_scalars(self):
        runner = self._runner()
        bare = PortfolioDecision(
            rating=PortfolioRating.HOLD,
            executive_summary="Evidence is balanced; no edge.",
            investment_thesis="Mixed signals, low conviction.",
        )
        runner._auto_draft_journal(
            user_id="u-1", session_id="sess-1", recipe_id="r-1",
            decision=bare, ticker="MSFT", conviction=3,
        )
        e = auth.list_journal_entries("u-1", kind="auto_draft")[0]
        # The placeholder strings appear when the PM omitted scalars.
        assert "not provided" in e["body"]


# ---------------------------------------------------------------------------
# RPC fallback for paper_place_order (Phase 1.5)
# ---------------------------------------------------------------------------

class TestPlaceOrderRPCFallback:
    """Verify the RPC wrapper returns None in dev (no Supabase) so the Python
    fallback fires. The actual RPC behavior requires a live Postgres + the
    Phase-1.5 migration applied; that's covered manually in the verification
    section of the plan."""

    def test_rpc_returns_none_without_supabase(self):
        # _db_writable() returns False when no service key is set.
        result = auth.call_paper_place_order_rpc({
            "p_user_id": "u-1", "p_fire_id": "f-1", "p_recipe_id": None,
            "p_session_id": "s-1", "p_ticker": "AAPL", "p_side": "buy",
            "p_qty": 1.0, "p_fill_price": 100.0, "p_slippage_bps": 0,
            "p_pm_rating": "Buy", "p_conviction": 7,
            "p_expected_return_pct": 5.0, "p_expected_volatility_pct": 10.0,
            "p_prob_of_profit": 0.6, "p_expected_hold_days": 30,
            "p_kelly_fraction": 0.02, "p_status": "filled",
        })
        assert result is None
