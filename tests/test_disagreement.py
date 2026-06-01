"""Tests for `agenticwhales.disagreement` — Phase 2 deliverable #7.

The hashing-trick cosine + the lean inference + the auto-inject decision +
the storage round-trip + the ask.template_6 integration.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from agenticwhales import disagreement
from web import auth


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


# ---------------------------------------------------------------------------
# Cosine similarity
# ---------------------------------------------------------------------------

class TestCosine:
    def test_identical_texts_sim_one(self):
        text = "The market is overbought; momentum is fading."
        assert disagreement.cosine_similarity(text, text) == pytest.approx(1.0, rel=1e-6)

    def test_completely_different_texts_low_sim(self):
        a = "The market is overbought; momentum is fading."
        b = "Coffee prices reflect supply shocks in Brazil"
        sim = disagreement.cosine_similarity(a, b)
        assert sim < 0.30

    def test_partial_overlap(self):
        a = "Bull case rests on margin expansion and growth"
        b = "Bear case warns of margin compression and growth slowing"
        sim = disagreement.cosine_similarity(a, b)
        assert 0.20 < sim < 0.95

    def test_empty_text_yields_zero(self):
        assert disagreement.cosine_similarity("", "anything") == 0.0
        assert disagreement.cosine_similarity("text", "") == 0.0


# ---------------------------------------------------------------------------
# Lean inference + agreement
# ---------------------------------------------------------------------------

class TestLean:
    def test_infer_long(self):
        assert disagreement.infer_lean("Strong buy here. Margin expansion.") == "long"
        assert disagreement.infer_lean("After review I lean toward Overweight.") == "long"

    def test_infer_short(self):
        assert disagreement.infer_lean("Bearish — exit the position.") == "short"
        assert disagreement.infer_lean("Final view: Sell.") == "short"

    def test_infer_neutral(self):
        assert disagreement.infer_lean("Evidence is balanced. Hold.") == "neutral"

    def test_ratings_agree_long_long(self):
        bull = "Strong buy here, momentum"
        bear = "I actually agree on the long side"
        assert disagreement.ratings_agree(bull, bear) is True

    def test_ratings_disagree(self):
        bull = "Buy buy buy."
        bear = "Sell sell sell."
        assert disagreement.ratings_agree(bull, bear) is False


# ---------------------------------------------------------------------------
# Persistence + storage round-trip
# ---------------------------------------------------------------------------

class TestRecord:
    def test_round_trip(self):
        snap = disagreement.record_disagreement(
            user_id="u-1", session_id="sess-1",
            bull_history="Strong buy here, momentum is clear.",
            bear_history="I lean toward Sell here, risk-off.",
            bull_model="gpt-5.4", bear_model="claude-sonnet-4-6",
            recipe_id="rcp-1",
        )
        assert snap.session_id == "sess-1"
        assert snap.rating_agreement is False
        rows = disagreement.list_for_user("u-1")
        assert len(rows) == 1
        assert rows[0]["bull_model"] == "gpt-5.4"
        assert rows[0]["bear_model"] == "claude-sonnet-4-6"

    def test_dedup_by_session_id(self):
        for sim_text in ("first run", "second run"):
            disagreement.record_disagreement(
                user_id="u-1", session_id="sess-x",
                bull_history=sim_text, bear_history=sim_text,
            )
        rows = disagreement.list_for_user("u-1")
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Auto-inject decision
# ---------------------------------------------------------------------------

class TestAutoInject:
    def test_off_by_default(self):
        assert not disagreement.should_auto_inject({}, 0.9)
        assert not disagreement.should_auto_inject({"auto_inject_classical": False}, 0.9)

    def test_only_above_threshold(self):
        rec = {"auto_inject_classical": True}
        assert disagreement.should_auto_inject(rec, 0.6) is True
        assert disagreement.should_auto_inject(rec, 0.40) is False


# ---------------------------------------------------------------------------
# ask.template_6 integration
# ---------------------------------------------------------------------------

class TestAskIntegration:
    def test_template_6_returns_split_when_outcomes_joined(self):
        from agenticwhales import ask
        # Seed two sessions: one Bull/Bear agreed, one disagreed; each with
        # a corresponding paper_order + decision_outcome.
        _seed_session_with_outcome(
            user_id="u-1", session_id="s-agree",
            agree=True, realized=10.0,
        )
        _seed_session_with_outcome(
            user_id="u-1", session_id="s-disagree",
            agree=False, realized=-5.0,
        )
        result = ask.answer("u-1", 6)
        assert result.confidence == "ok"
        assert "agreed" in result.markdown.lower()
        assert "disagreed" in result.markdown.lower()
        assert result.data_points == 2


def _seed_session_with_outcome(*, user_id, session_id, agree, realized):
    """Insert a disagreement row + paper_order + decision_outcome triple
    so template_6's cross-join has data to work with."""
    # 1. Disagreement row.
    disagreement.record_disagreement(
        user_id=user_id, session_id=session_id,
        bull_history="Buy with confidence." if agree else "Strong buy here.",
        bear_history="Also Buy — agree" if agree else "I lean toward Sell.",
    )
    # 2. paper_order keyed to this session.
    oid = uuid.uuid4().hex
    auth.insert_paper_order({
        "id": oid, "user_id": user_id, "session_id": session_id, "recipe_id": None,
        "fire_id": f"fire-{oid[:8]}", "ticker": "AAPL", "side": "buy",
        "qty": 10.0, "fill_price": 100.0, "slippage_bps": 0, "gross_value": 1000.0,
        "pm_rating": "Buy", "conviction_score": 7,
        "expected_return_pct": 5.0, "expected_volatility_pct": 20.0,
        "prob_of_profit": 0.6, "expected_hold_days": 30,
        "kelly_fraction": 0.05, "status": "filled",
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    })
    # 3. Resolved outcome.
    auth._memstore[("decision_outcomes", oid)] = {
        "paper_order_id": oid,
        "user_id": user_id, "ticker": "AAPL",
        "predicted_return_pct": 5.0, "predicted_volatility_pct": 20.0,
        "predicted_prob_of_profit": 0.6, "predicted_hold_days": 30,
        "realized_return_pct": realized,
        "realized_at": datetime.now(tz=timezone.utc).isoformat(),
        "hit": realized > 0,
        "brier_component": (0.6 - (1.0 if realized > 0 else 0.0)) ** 2,
        "resolved_at": datetime.now(tz=timezone.utc).isoformat(),
    }
