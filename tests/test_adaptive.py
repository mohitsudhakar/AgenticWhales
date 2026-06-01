"""Tests for `agenticwhales.adaptive` — Phase 2 deliverable #9.

Adaptive depth + prompt-eval harness. Both functions are pure given their
callable inputs, so we can fully test them without a live LLM.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from agenticwhales import adaptive
from web import auth


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


# ---------------------------------------------------------------------------
# Adaptive depth
# ---------------------------------------------------------------------------

class TestShouldEscalate:
    def test_identical_samples_do_not_escalate(self):
        same = ["buy strong momentum"] * 3
        assert adaptive.should_escalate(same) is False

    def test_divergent_samples_escalate(self):
        diverse = [
            "buy strong momentum technicals",
            "sell — bearish risk-off rotation",
            "hold — evidence balanced both sides",
        ]
        assert adaptive.should_escalate(diverse) is True

    def test_threshold_respected(self):
        # Mostly identical with a single different token in each sample
        # (~0.40 pairwise Jaccard distance). Should not escalate at 0.50
        # threshold but should at the default 0.30.
        slightly_diverse = [
            "buy strong momentum technicals support",
            "buy strong momentum technicals resistance",
            "buy strong momentum technicals breakout",
        ]
        assert adaptive.should_escalate(slightly_diverse, threshold=0.50) is False
        assert adaptive.should_escalate(slightly_diverse, threshold=0.10) is True

    def test_empty_or_single_returns_false(self):
        assert adaptive.should_escalate([]) is False
        assert adaptive.should_escalate(["only one"]) is False


# ---------------------------------------------------------------------------
# Prompt-eval harness
# ---------------------------------------------------------------------------

def _seed_outcomes(user_id="u-1", n=25, prob_baseline=0.65):
    """Seed N outcomes where the baseline prob is `prob_baseline` and
    realized hits are 50%. Setup mimics an over-confident PM where Brier
    is high; a variant that picks better probs should win."""
    for i in range(n):
        oid = uuid.uuid4().hex
        auth._memstore[("decision_outcomes", oid)] = {
            "paper_order_id": oid,
            "user_id": user_id,
            "ticker": "AAPL",
            "predicted_return_pct": 10.0,
            "predicted_volatility_pct": 20.0,
            "predicted_prob_of_profit": prob_baseline,
            "predicted_hold_days": 30,
            "realized_return_pct": 5.0 if i < n // 2 else -5.0,
            "realized_at": datetime.now(tz=timezone.utc).isoformat(),
            "hit": i < n // 2,
            "brier_component": (prob_baseline - (1.0 if i < n // 2 else 0.0)) ** 2,
            "resolved_at": datetime.now(tz=timezone.utc).isoformat(),
        }


class TestPromptEval:
    def test_below_n_returns_none(self):
        _seed_outcomes(n=5)
        result = adaptive.evaluate_prompt_variant(
            "u-1", variant="v1", scorer=lambda r: 0.5,
        )
        assert result is None

    def test_better_variant_gets_promoted(self):
        _seed_outcomes(n=25, prob_baseline=0.85)
        # Variant that emits a calibrated 0.5 on every sample. Baseline
        # Brier = (0.85-1)^2*0.5 + (0.85-0)^2*0.5 = 0.0225/2 + 0.7225/2 = 0.3725
        # Variant Brier = (0.5-1)^2*0.5 + (0.5-0)^2*0.5 = 0.25
        # Improvement ~0.12 → well above 0.02 threshold → promoted.
        result = adaptive.evaluate_prompt_variant(
            "u-1", variant="calibrated-v1", scorer=lambda r: 0.5,
        )
        assert result is not None
        assert result.promoted is True
        assert result.improvement > 0.02
        assert result.n_samples == 25

    def test_no_improvement_not_promoted(self):
        _seed_outcomes(n=25, prob_baseline=0.5)
        # Variant matches baseline → no improvement → not promoted.
        result = adaptive.evaluate_prompt_variant(
            "u-1", variant="same-as-baseline",
            scorer=lambda r: r.get("predicted_prob_of_profit"),
        )
        assert result is not None
        assert result.promoted is False
        assert abs(result.improvement) < 1e-6

    def test_persists_for_listing(self):
        _seed_outcomes(n=25, prob_baseline=0.85)
        adaptive.evaluate_prompt_variant(
            "u-1", variant="v-promoted", scorer=lambda r: 0.5,
        )
        adaptive.evaluate_prompt_variant(
            "u-1", variant="v-noop",
            scorer=lambda r: r.get("predicted_prob_of_profit"),
        )
        rows = adaptive.list_recent_evals("u-1")
        assert len(rows) == 2
        assert {r["variant"] for r in rows} == {"v-promoted", "v-noop"}

    def test_skip_rows_with_none_probs(self):
        _seed_outcomes(n=25, prob_baseline=0.85)
        # Scorer returns None for half the rows → still works on the
        # remaining N (>= min_n by default we set).
        def scorer(r):
            return 0.5 if int(r["paper_order_id"], 16) % 2 == 0 else None
        result = adaptive.evaluate_prompt_variant(
            "u-1", variant="partial", scorer=scorer, min_n=5,
        )
        # The implementation requires N >= min_n outcomes *with* a hit; the
        # scorer's None means "skip this sample" inside the loop. We
        # ensured min_n is small enough that the kept samples meet the bar.
        assert result is not None
        assert 0 < result.n_samples < 25
