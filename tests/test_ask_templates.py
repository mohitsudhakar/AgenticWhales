"""Tests for `agenticwhales.ask` — the 10 templated questions.

Each template is exercised with:
  1. The empty / no-data path → graceful `confidence='no_data'` shape.
  2. A seeded happy path → deterministic findings + table rows.

We don't test the LLM synthesis path because v1 doesn't have one (intentional
design choice; see ask.py docstring). The templates are pure functions over
the user's data — testable in isolation.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from agenticwhales import ask
from agenticwhales.agents.schemas import OrderSide, OrderStatus
from web import auth


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


# ---------------------------------------------------------------------------
# Helpers — seed paper_orders + decision_outcomes + journal_entries directly
# into the memstore. We bypass `paper.place_order` so tests don't depend on
# the Kelly + RiskGuard layers.
# ---------------------------------------------------------------------------

def _seed_order(
    user_id="u-1",
    *,
    ticker="AAPL",
    side="buy",
    qty=10.0,
    fill_price=100.0,
    pm_rating="Buy",
    conviction=8,
    expected_return_pct=10.0,
    prob_of_profit=0.65,
    expected_hold_days=30,
    recipe_id=None,
    session_id=None,
    created_at=None,
):
    oid = uuid.uuid4().hex
    sid = session_id or f"sess-{oid[:8]}"
    rid = recipe_id or f"rcp-{oid[:8]}"
    ts = created_at or datetime.now(tz=timezone.utc)
    row = {
        "id": oid,
        "user_id": user_id,
        "session_id": sid,
        "recipe_id": rid,
        "fire_id": f"fire-{oid[:8]}",
        "ticker": ticker,
        "side": side,
        "qty": qty,
        "fill_price": fill_price,
        "slippage_bps": 0,
        "gross_value": qty * fill_price,
        "pm_rating": pm_rating,
        "conviction_score": conviction,
        "expected_return_pct": expected_return_pct,
        "expected_volatility_pct": 20.0,
        "prob_of_profit": prob_of_profit,
        "expected_hold_days": expected_hold_days,
        "kelly_fraction": 0.05,
        "status": "filled",
        "created_at": ts.isoformat() if isinstance(ts, datetime) else ts,
    }
    auth.insert_paper_order(row)
    return oid, sid, rid


def _seed_outcome(oid, *, user_id="u-1", realized_return_pct=8.0, hit=True,
                  prob=0.65, predicted_return=10.0):
    auth._memstore[("decision_outcomes", oid)] = {
        "paper_order_id": oid,
        "user_id": user_id,
        "ticker": "AAPL",
        "predicted_return_pct": predicted_return,
        "predicted_volatility_pct": 20.0,
        "predicted_prob_of_profit": prob,
        "predicted_hold_days": 30,
        "realized_return_pct": realized_return_pct,
        "realized_at": datetime.now(tz=timezone.utc).isoformat(),
        "hit": hit,
        "brier_component": (prob - (1.0 if hit else 0.0)) ** 2,
        "resolved_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def _seed_recipe(user_id="u-1", *, name="Test thesis", deep_model="gemini-3.1-pro-preview"):
    rid = uuid.uuid4().hex
    auth.save_recipe({
        "id": rid,
        "user_id": user_id,
        "name": name,
        "tickers": ["AAPL"],
        "analysts": ["market"],
        "llm_provider": "google",
        "quick_model": "gemini-3-flash-preview",
        "deep_model": deep_model,
        "bull_model": "deepseek-v4-pro",
        "bear_model": "gemini-3.1-pro-preview",
        "research_depth": 1,
        "schedule_kind": "manual",
        "schedule_expr": None,
        "exchange_code": "XNYS",
        "market_hours_only": True,
        "max_concurrent_tickers": 5,
        "trigger_conditions": None,
        "output_policy": "paper_trade",
        "conviction_threshold": 7,
        "max_daily_token_cost_usd": 5.0,
        "consecutive_failures": 0,
        "status": "active",
        "last_run_at": None,
        "next_run_at": None,
    })
    return rid


def _seed_journal(user_id="u-1", *, body="note", session_id=None, kind="note",
                  created_at=None):
    eid = uuid.uuid4().hex
    ts = (created_at or datetime.now(tz=timezone.utc))
    auth.save_journal_entry({
        "id": eid,
        "user_id": user_id,
        "session_id": session_id,
        "paper_order_id": None,
        "thesis_id": None,
        "kind": kind,
        "body": body,
        "sentiment_score": None,
        "is_draft": False,
        "created_at": ts.isoformat() if isinstance(ts, datetime) else ts,
        "updated_at": ts.isoformat() if isinstance(ts, datetime) else ts,
    })
    return eid


# ---------------------------------------------------------------------------
# Registry / dispatch
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_ten_templates_registered(self):
        templates = ask.list_templates()
        assert len(templates) == 10
        ids = {t["template_id"] for t in templates}
        assert ids == set(range(1, 11))

    def test_unknown_template_raises(self):
        with pytest.raises(KeyError):
            ask.answer("u-1", 999)


# ---------------------------------------------------------------------------
# Empty / no-data paths — every template must degrade gracefully
# ---------------------------------------------------------------------------

class TestNoDataPaths:
    @pytest.mark.parametrize("tid", list(range(1, 11)))
    def test_empty_state_does_not_crash(self, tid):
        # Template 9 has a heuristic fallback that still returns a real
        # answer when there are no orders — confidence='low' but not no_data.
        # Templates 6 and 9 have their own deferred behavior.
        result = ask.answer("u-1", tid)
        assert result.template_id == tid
        assert isinstance(result.markdown, str) and len(result.markdown) > 0
        # template 9 with no orders returns "no obvious tilt" not no_data.
        if tid not in (9,):
            assert result.confidence in ("no_data", "low", "ok")


# ---------------------------------------------------------------------------
# Template 1: DOW
# ---------------------------------------------------------------------------

class TestTemplate1:
    def test_aggregates_by_dow(self):
        # 2026-05-18 is a Monday; 2026-05-19 is a Tuesday.
        mon = datetime(2026, 5, 18, 14, 0, tzinfo=timezone.utc)
        tue = datetime(2026, 5, 19, 14, 0, tzinfo=timezone.utc)
        oid1, _, _ = _seed_order(created_at=mon)
        oid2, _, _ = _seed_order(created_at=tue)
        _seed_outcome(oid1, realized_return_pct=-5.0, hit=False)
        _seed_outcome(oid2, realized_return_pct=10.0, hit=True)
        result = ask.answer("u-1", 1)
        assert result.confidence == "ok"
        assert result.data_points == 2
        assert result.table and len(result.table) == 2
        days = {r["day"] for r in result.table}
        assert days == {"Mon", "Tue"}


# ---------------------------------------------------------------------------
# Template 2: Thesis PnL
# ---------------------------------------------------------------------------

class TestTemplate2:
    def test_ranks_by_avg_return(self):
        good = _seed_recipe(name="Good thesis")
        bad = _seed_recipe(name="Bad thesis")
        for _ in range(3):
            oid, _, _ = _seed_order(recipe_id=good)
            _seed_outcome(oid, realized_return_pct=8.0, hit=True)
        for _ in range(3):
            oid, _, _ = _seed_order(recipe_id=bad)
            _seed_outcome(oid, realized_return_pct=-5.0, hit=False)
        result = ask.answer("u-1", 2)
        assert result.confidence == "ok"
        assert len(result.table) == 2
        # First row should be the better one.
        assert result.table[0]["name"] == "Good thesis"
        assert "Good thesis" in result.markdown
        assert "Bad thesis" in result.markdown


# ---------------------------------------------------------------------------
# Template 3: Calibration
# ---------------------------------------------------------------------------

class TestTemplate3:
    def test_brier_scored_and_interpreted(self):
        # Two outcomes: one hit at p=0.8, one miss at p=0.2. Brier = (0.8-1)^2/2 + (0.2-0)^2/2
        # = 0.04/2 + 0.04/2 = 0.04 → "well-calibrated"
        oid1, _, _ = _seed_order()
        oid2, _, _ = _seed_order()
        _seed_outcome(oid1, realized_return_pct=10.0, hit=True, prob=0.8)
        _seed_outcome(oid2, realized_return_pct=-5.0, hit=False, prob=0.2)
        result = ask.answer("u-1", 3)
        assert result.confidence == "ok"
        assert result.data_points == 2
        assert "Brier score" in result.markdown
        assert "well-calibrated" in result.markdown or "0.0400" in result.markdown


# ---------------------------------------------------------------------------
# Template 4: Model accuracy
# ---------------------------------------------------------------------------

class TestTemplate4:
    def test_ranks_models_by_hit_rate(self):
        good_model_rid = _seed_recipe(deep_model="claude-opus-4-6")
        bad_model_rid = _seed_recipe(deep_model="gemini-3-flash-preview")
        for _ in range(4):
            oid, _, _ = _seed_order(recipe_id=good_model_rid)
            _seed_outcome(oid, hit=True)
        for _ in range(4):
            oid, _, _ = _seed_order(recipe_id=bad_model_rid)
            _seed_outcome(oid, hit=False)
        result = ask.answer("u-1", 4)
        assert result.confidence == "ok"
        assert result.table[0]["model"] == "claude-opus-4-6"
        assert result.table[0]["hit_rate"] == 1.0
        assert result.table[-1]["model"] == "gemini-3-flash-preview"


# ---------------------------------------------------------------------------
# Template 5: Holding period
# ---------------------------------------------------------------------------

class TestTemplate5:
    def test_buckets_by_horizon(self):
        oid1, _, _ = _seed_order(expected_hold_days=5)
        oid2, _, _ = _seed_order(expected_hold_days=60)
        _seed_outcome(oid1, realized_return_pct=2.0)
        _seed_outcome(oid2, realized_return_pct=15.0)
        result = ask.answer("u-1", 5)
        assert result.confidence == "ok"
        labels = {r["horizon"] for r in result.table}
        assert labels == {"≤7d", "31-90d"}


# ---------------------------------------------------------------------------
# Template 6: Bull/Bear agreement — explicitly deferred
# ---------------------------------------------------------------------------

class TestTemplate6:
    def test_returns_no_data_without_disagreement_rows(self):
        # Orders + outcomes seeded but NO disagreement_log entries — the
        # template needs a Bull/Bear comparison to compute the split.
        # (Deliverable #7 populates disagreement_log on every recipe fire;
        # tests that haven't seeded those rows hit this no_data path.)
        oid, _, _ = _seed_order()
        _seed_outcome(oid)
        result = ask.answer("u-1", 6)
        assert result.confidence == "no_data"
        assert "disagreement" in result.markdown.lower()


# ---------------------------------------------------------------------------
# Template 7: Worst decisions + journal context
# ---------------------------------------------------------------------------

class TestTemplate7:
    def test_pulls_worst_decisions_and_journal_context(self):
        # Three trades — one big loser.
        oid1, sid1, _ = _seed_order(ticker="TSLA")
        oid2, sid2, _ = _seed_order(ticker="NVDA")
        oid3, sid3, _ = _seed_order(ticker="AAPL")
        _seed_outcome(oid1, realized_return_pct=-25.0, hit=False)
        _seed_outcome(oid2, realized_return_pct=5.0, hit=True)
        _seed_outcome(oid3, realized_return_pct=-3.0, hit=False)
        _seed_journal(session_id=sid1, body="Loaded up on TSLA before earnings — too much conviction.")

        result = ask.answer("u-1", 7)
        assert result.confidence == "ok"
        assert result.table[0]["ticker"] == "TSLA"
        assert "TSLA" in result.markdown
        assert "Loaded up" in result.table[0]["note"]


# ---------------------------------------------------------------------------
# Template 8: Writings before losses
# ---------------------------------------------------------------------------

class TestTemplate8:
    def test_finds_journal_within_24h_before_loss(self):
        # Loss order at T; journal entry at T - 6h.
        trade_ts = datetime(2026, 5, 18, 18, 0, tzinfo=timezone.utc)
        journal_ts = trade_ts - timedelta(hours=6)
        oid, sid, _ = _seed_order(ticker="TSLA", created_at=trade_ts)
        _seed_outcome(oid, realized_return_pct=-15.0, hit=False)
        _seed_journal(body="Feeling FOMO; can't miss this run.", created_at=journal_ts)
        result = ask.answer("u-1", 8)
        assert result.confidence == "ok"
        assert "FOMO" in result.markdown

    def test_no_finding_when_journal_outside_window(self):
        trade_ts = datetime(2026, 5, 18, 18, 0, tzinfo=timezone.utc)
        far_journal = trade_ts - timedelta(days=5)  # outside 24h
        oid, _, _ = _seed_order(ticker="TSLA", created_at=trade_ts)
        _seed_outcome(oid, realized_return_pct=-15.0, hit=False)
        _seed_journal(body="Random.", created_at=far_journal)
        result = ask.answer("u-1", 8)
        assert result.confidence == "no_data"


# ---------------------------------------------------------------------------
# Template 9: Tilting — heuristic fallback when behavioral_findings empty
# ---------------------------------------------------------------------------

class TestTemplate9:
    def test_heuristic_flags_rapid_reentries(self):
        now = datetime.now(tz=timezone.utc)
        # Two NVDA trades within 30 min — should flag.
        _seed_order(ticker="NVDA", created_at=now - timedelta(minutes=20))
        _seed_order(ticker="NVDA", created_at=now)
        result = ask.answer("u-1", 9)
        assert result.confidence == "low"
        assert "NVDA" in result.markdown

    def test_no_tilt_when_separated(self):
        now = datetime.now(tz=timezone.utc)
        _seed_order(ticker="NVDA", created_at=now - timedelta(hours=3))
        _seed_order(ticker="NVDA", created_at=now)
        result = ask.answer("u-1", 9)
        assert "No obvious tilt patterns" in result.markdown


# ---------------------------------------------------------------------------
# Template 10: My edge
# ---------------------------------------------------------------------------

class TestTemplate10:
    def test_identifies_best_rating(self):
        # Seed enough Buys that hit and Sells that miss.
        for _ in range(5):
            oid, _, _ = _seed_order(pm_rating="Buy")
            _seed_outcome(oid, hit=True)
        for _ in range(5):
            oid, _, _ = _seed_order(pm_rating="Sell")
            _seed_outcome(oid, hit=False)
        result = ask.answer("u-1", 10)
        assert result.confidence == "ok"
        # Best should be Buy with 100% hit.
        assert result.table[0]["rating"] == "Buy"
        assert result.table[0]["hit_rate"] == 1.0


# ---------------------------------------------------------------------------
# Confidence + audit hook contract
# ---------------------------------------------------------------------------

class TestResultContract:
    def test_to_dict_serializable(self):
        oid, _, _ = _seed_order()
        _seed_outcome(oid)
        result = ask.answer("u-1", 1)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "markdown" in d and "template_id" in d
        assert "confidence" in d
