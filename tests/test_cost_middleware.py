"""Tests for the cost middleware (post-fire roll-up + pre-call budget check)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agenticwhales.llm_clients.cost_middleware import (
    BudgetExceeded,
    check_user_budget,
    record_fire_cost,
)
from web import auth


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


class TestRecordFireCost:
    def test_writes_recipe_usage_and_user_spend(self):
        cost = record_fire_cost(
            user_id="u-1",
            recipe_id="r-1",
            session_id="s-1",
            provider="google",
            quick_model="gemini-3-flash-preview",
            deep_model="gemini-3.1-pro-preview",
            stats={"tokens_in": 1_000_000, "tokens_out": 100_000},
        )
        # Pricing seed: gemini-3.1-pro-preview is 1.25 in + 10.00 out per 1M.
        # 1M @ 1.25 + 100k @ 10.00 = 1.25 + 1.00 = 2.25
        assert float(cost) == pytest.approx(2.25, rel=1e-6)

        today = datetime.now(tz=timezone.utc).date().isoformat()
        usage = auth.load_recipe_usage("r-1", today)
        assert usage is not None
        assert float(usage["token_cost_usd"]) == pytest.approx(2.25, rel=1e-6)
        assert int(usage["input_tokens"]) == 1_000_000
        assert int(usage["output_tokens"]) == 100_000
        assert int(usage["run_count"]) == 1

        spent = auth.load_user_spend("u-1", today)
        assert float(spent) == pytest.approx(2.25, rel=1e-6)

    def test_unknown_model_records_zero_cost_but_does_not_raise(self):
        # Unknown provider/model → cost_for raises ValueError; we catch and
        # bill $0 rather than crashing the session.
        cost = record_fire_cost(
            user_id="u-1",
            recipe_id="r-1",
            session_id="s-1",
            provider="unknown",
            quick_model="unknown-mini",
            deep_model="unknown-pro",
            stats={"tokens_in": 1000, "tokens_out": 500},
        )
        assert float(cost) == 0.0

    def test_per_model_attribution_when_breakdown_present(self):
        """Phase 1.5 cleanup: when StatsCallbackHandler hands us a per-model
        breakdown, each model bills at its own rate.

        gemini-3-flash-preview: $0.075/1M in, $0.30/1M out
        gemini-3.1-pro-preview: $1.25/1M in, $10.00/1M out

        Quick: 1M in + 100k out = $0.075 + $0.030 = $0.105
        Deep:  200k in + 50k out = $0.25 + $0.50 = $0.75
        Total                                      = $0.855
        """
        cost = record_fire_cost(
            user_id="u-1", recipe_id="r-1", session_id="s-1",
            provider="google",
            quick_model="gemini-3-flash-preview",
            deep_model="gemini-3.1-pro-preview",
            stats={
                "tokens_in": 1_200_000, "tokens_out": 150_000,
                "model_usage": {
                    "gemini-3-flash-preview":
                        {"tokens_in": 1_000_000, "tokens_out": 100_000, "llm_calls": 5},
                    "gemini-3.1-pro-preview":
                        {"tokens_in": 200_000,   "tokens_out": 50_000,  "llm_calls": 1},
                },
            },
        )
        assert float(cost) == pytest.approx(0.855, rel=1e-3)

    def test_unknown_model_in_breakdown_falls_back_to_deep_rate(self):
        cost = record_fire_cost(
            user_id="u-1", recipe_id="r-1", session_id="s-1",
            provider="google",
            quick_model="gemini-3-flash-preview",
            deep_model="gemini-3.1-pro-preview",
            stats={
                "tokens_in": 1_000_000, "tokens_out": 0,
                "model_usage": {
                    "mystery-model-not-in-pricing":
                        {"tokens_in": 1_000_000, "tokens_out": 0, "llm_calls": 1},
                },
            },
        )
        # Should fall back to deep_model rate: $1.25 for 1M input tokens.
        assert float(cost) == pytest.approx(1.25, rel=1e-3)

    def test_writes_llm_call_log_row(self):
        record_fire_cost(
            user_id="u-1", recipe_id="r-1", session_id="s-2",
            provider="google", quick_model="gemini-3-flash-preview",
            deep_model="gemini-3.1-pro-preview",
            stats={"tokens_in": 5_000, "tokens_out": 1_000},
        )
        logs = [v for (t, _), v in auth._memstore.items() if t == "llm_call_log"]
        assert any(l.get("session_id") == "s-2" for l in logs)


class TestCheckUserBudget:
    def test_under_cap_passes(self):
        # Explicit cap so the test doesn't depend on tier defaults — a novice
        # user has $0.50/day which would trip on the $1 spend below.
        auth.upsert_risk_limits("u-1", daily_spend_cap_usd=25.0)
        record_fire_cost(
            user_id="u-1", recipe_id="r-1", session_id="s-1",
            provider="google", quick_model="gemini-3-flash-preview",
            deep_model="gemini-3.1-pro-preview",
            stats={"tokens_in": 800_000, "tokens_out": 0},  # 800k * $1.25/1M = $1
        )
        # Should not raise.
        check_user_budget("u-1")

    def test_over_cap_raises(self):
        auth.upsert_risk_limits("u-1", daily_spend_cap_usd=0.10)
        record_fire_cost(
            user_id="u-1", recipe_id="r-1", session_id="s-1",
            provider="google", quick_model="gemini-3-flash-preview",
            deep_model="gemini-3.1-pro-preview",
            stats={"tokens_in": 200_000, "tokens_out": 0},  # 200k * $1.25/1M = $0.25
        )
        with pytest.raises(BudgetExceeded) as exc:
            check_user_budget("u-1")
        assert exc.value.spent_usd >= exc.value.cap_usd
