"""Tests for the multi-TF recipe-fire orchestrator (Phase 3 #3).

We stub SessionRunner so the test doesn't burn LLM tokens — each "run" just
stamps a canned PortfolioDecision on the session and flips status to
'completed'. The orchestrator then merges the per-TF decisions and writes
the merged decision back to the lead session.

What we verify:
  1. A single-TF recipe (default) uses the existing fire-and-forget path
     (one SessionRunner) — orchestrator is NOT invoked.
  2. A multi-TF recipe with N timeframes spawns N session runners.
  3. The per-TF sessions carry `session["timeframe"]` and
     `session["skip_post_decision"]=True`.
  4. The merged decision lands on the lead session as `pm_decision` plus the
     raw per-TF map under `multitf_decisions`.
  5. A `disagreement_log` row is written.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from agenticwhales.agents.schemas import (
    PortfolioDecision,
    PortfolioRating,
    Recipe,
    RecipeStatus,
)
from web import auth


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


def _make_recipe(timeframes: List[str]) -> Recipe:
    return Recipe(
        id="r-multitf",
        user_id="u-1",
        name="multitf test",
        tickers=["AAPL"],
        analysts=["market"],
        llm_provider="google",
        quick_model="gemini-3-flash-preview",
        deep_model="gemini-3.1-pro-preview",
        bull_model="deepseek-v4",
        bear_model="gemini-3.1-pro-preview",
        timeframes=timeframes,
        status=RecipeStatus.ACTIVE,
    )


def _stub_pm_decision(rating: PortfolioRating, **overrides) -> Dict[str, Any]:
    """Build a valid PortfolioDecision payload as a dict."""
    base = dict(
        rating=rating,
        executive_summary="stub",
        investment_thesis="stub",
        stop_loss=95.0,
        expected_return_pct=overrides.get("expected_return_pct", 5.0),
        expected_volatility_pct=overrides.get("expected_volatility_pct", 15.0),
        prob_of_profit=overrides.get("prob_of_profit", 0.6),
        expected_hold_days=overrides.get("expected_hold_days", 5),
    )
    return PortfolioDecision(**base).model_dump(mode="json")


class _StubRunner:
    """Mimics web.runner.SessionRunner just enough for the orchestrator.

    Patched in via monkeypatch. The orchestrator calls
    SessionRunner(session, loop).start() then joins the thread; we run the
    canned 'graph' synchronously inside start() so join() returns immediately.
    """

    # Class-level registry mapping (recipe_id, timeframe) -> PortfolioDecision
    canned: Dict[str, Dict[str, Any]] = {}

    def __init__(self, session, loop):
        self.session = session
        self.loop = loop
        self._thread = None

    def start(self):
        tf = self.session.get("timeframe", "1d")
        decision = self.canned.get(tf)
        if decision:
            self.session["pm_decision"] = decision
        self.session["status"] = "completed"

        class _NoopThread:
            def join(self):
                return None

        self._thread = _NoopThread()

    def _post_decision_hook(self):
        # Recorded on the lead session for the orchestrator's final step.
        self.session["_post_decision_ran"] = True


def _patch_runner_and_attestation(monkeypatch):
    monkeypatch.setattr("web.server.SessionRunner", _StubRunner)
    # Bypass the attestation check used in _run_recipe_session.
    monkeypatch.setattr(
        "web.auth.latest_active_attestation_for_user",
        lambda uid: {"id": "a-1", "version": "v1"},
    )
    monkeypatch.setattr(
        "web.auth.has_running_session_for_recipe", lambda rid: False,
    )


class TestSingleTFFallsThroughExistingPath:
    def test_no_multitf_orchestrator_when_one_timeframe(self, monkeypatch):
        from web import server
        _patch_runner_and_attestation(monkeypatch)

        recipe = _make_recipe(timeframes=["1d"])
        _StubRunner.canned = {"1d": _stub_pm_decision(PortfolioRating.BUY)}

        called = {"count": 0}

        def fake_multitf(*args, **kwargs):
            called["count"] += 1

        monkeypatch.setattr(server, "_run_recipe_multitf", fake_multitf)
        server._run_recipe_session(recipe, "fire-1")

        assert called["count"] == 0


class TestMultiTFOrchestrator:
    def test_runs_one_session_per_timeframe(self, monkeypatch):
        from web import server, runner as runner_mod
        _patch_runner_and_attestation(monkeypatch)

        recipe = _make_recipe(timeframes=["1h", "1d"])
        _StubRunner.canned = {
            "1h": _stub_pm_decision(PortfolioRating.OVERWEIGHT,
                                     expected_return_pct=6.0, prob_of_profit=0.6),
            "1d": _stub_pm_decision(PortfolioRating.BUY,
                                     expected_return_pct=10.0, prob_of_profit=0.65),
        }

        server._run_recipe_session(recipe, "fire-multitf-1")

        sessions = [v for (t, _), v in auth._memstore.items() if t == "sessions"]
        per_tf = [s for s in sessions if s.get("recipe_id") == "r-multitf"]
        timeframes_seen = sorted(s.get("timeframe") for s in per_tf if s.get("timeframe"))
        assert timeframes_seen == ["1d", "1h"]

    def test_lead_session_carries_merged_decision(self, monkeypatch):
        from web import server
        _patch_runner_and_attestation(monkeypatch)

        recipe = _make_recipe(timeframes=["1h", "1d"])
        _StubRunner.canned = {
            "1h": _stub_pm_decision(PortfolioRating.OVERWEIGHT,
                                     expected_return_pct=6.0, prob_of_profit=0.6),
            "1d": _stub_pm_decision(PortfolioRating.BUY,
                                     expected_return_pct=10.0, prob_of_profit=0.65),
        }

        server._run_recipe_session(recipe, "fire-multitf-2")

        sessions = [v for (t, _), v in auth._memstore.items() if t == "sessions"]
        per_tf = [s for s in sessions if s.get("recipe_id") == "r-multitf"]
        # The lead session is whichever one carries the multitf_decisions map.
        lead = next((s for s in per_tf if s.get("multitf_decisions")), None)
        assert lead is not None
        assert set(lead["multitf_decisions"].keys()) == {"1d", "1h"}
        # Merged decision is stamped as pm_decision and is a real PortfolioDecision.
        assert lead["pm_decision"]["rating"] in {"Buy", "Overweight"}

    def test_writes_disagreement_log_row(self, monkeypatch):
        from web import server
        _patch_runner_and_attestation(monkeypatch)

        recipe = _make_recipe(timeframes=["1h", "1d"])
        # Same decisions → disagreement = 0, similarity = 1.0
        same = _stub_pm_decision(PortfolioRating.OVERWEIGHT)
        _StubRunner.canned = {"1h": same, "1d": same}

        server._run_recipe_session(recipe, "fire-multitf-3")

        rows = [v for (t, _), v in auth._memstore.items() if t == "disagreement_log"]
        assert len(rows) == 1
        assert rows[0]["recipe_id"] == "r-multitf"
        assert rows[0]["rating_agreement"] is True
        assert float(rows[0]["similarity"]) == pytest.approx(1.0)

    def test_per_tf_sessions_skip_post_decision_hook(self, monkeypatch):
        from web import server
        _patch_runner_and_attestation(monkeypatch)

        recipe = _make_recipe(timeframes=["1h", "1d"])
        decision = _stub_pm_decision(PortfolioRating.HOLD)
        _StubRunner.canned = {"1h": decision, "1d": decision}

        server._run_recipe_session(recipe, "fire-multitf-4")

        # Every per-TF session should have been built with skip_post_decision=True.
        # The orchestrator strips that flag from the LEAD session before
        # running the merged hook.
        sessions = [v for (t, _), v in auth._memstore.items() if t == "sessions"]
        per_tf = [s for s in sessions if s.get("recipe_id") == "r-multitf"]
        lead = next((s for s in per_tf if s.get("multitf_decisions")), None)
        non_lead = [s for s in per_tf if s is not lead]
        for s in non_lead:
            assert s.get("skip_post_decision") is True
        # Lead has the flag stripped (so the hook runs against the merged decision).
        assert "skip_post_decision" not in lead
