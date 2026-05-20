"""Tests for `agenticwhales.calibration` — Platt-scaling head + opt-in flow.

Three layers of coverage:
  1. Pure math: `fit_platt`, `apply_platt`, `brier` behave correctly on
     trivial fixtures.
  2. Fit + storage: `fit_for_user` honors the unlock gate, persists, and
     opt-in flips a single flag.
  3. Integration: `paper.kelly_sizing` uses the calibrated probability when
     opted in, raw probability otherwise.
"""

from __future__ import annotations

import math
import time
import uuid
from datetime import datetime, timezone

import pytest

from agenticwhales import calibration
from agenticwhales.agents.schemas import PortfolioDecision, PortfolioRating
from agenticwhales import paper
from web import auth


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------

class TestPlattMath:
    def test_identity_when_empty(self):
        a, b = calibration.fit_platt([])
        assert (a, b) == (1.0, 0.0)

    def test_perfectly_calibrated_data_stays_identity_ish(self):
        # Generate (p, hit) pairs where the realized rate exactly matches p.
        pairs = []
        for p in (0.2, 0.4, 0.6, 0.8):
            for _ in range(50):
                pairs.append((p, True if (len(pairs) % 100) / 100 < p else False))
        a, b = calibration.fit_platt(pairs)
        # Should land near identity (a≈1, b≈0).
        assert 0.5 < a < 1.5
        assert abs(b) < 0.5

    def test_overconfident_data_pulls_slope_below_one(self):
        # User claims 0.8 → only hits 40% of the time. Calibrated `a` should
        # shrink toward 0 to deflate the over-stated probabilities.
        pairs = [(0.8, i < 4) for i in range(10)]   # 40% hit at p=0.8
        a, b = calibration.fit_platt(pairs)
        # Apply to the raw 0.8 and check it gets pulled down.
        calibrated = calibration.apply_platt(a, b, 0.8)
        assert calibrated < 0.8

    def test_apply_clips_extremes(self):
        # apply_platt internally clips logit → finite output even at 0 / 1.
        out = calibration.apply_platt(1.0, 0.0, 1.0)
        assert 0.0 < out < 1.0

    def test_brier_empty_returns_uninformative_baseline(self):
        assert calibration.brier([]) == 0.25

    def test_brier_lower_when_calibrated(self):
        pairs = [(0.8, True), (0.2, False), (0.7, True), (0.3, False)]
        raw_brier = calibration.brier(pairs)
        a, b = calibration.fit_platt(pairs)
        cal_pairs = [(calibration.apply_platt(a, b, p), h) for p, h in pairs]
        cal_brier = calibration.brier(cal_pairs)
        assert cal_brier <= raw_brier


# ---------------------------------------------------------------------------
# Fit + storage
# ---------------------------------------------------------------------------

def _seed_outcome(user_id, prob, hit):
    """Insert a synthetic decision_outcomes row directly."""
    oid = uuid.uuid4().hex
    auth._memstore[("decision_outcomes", oid)] = {
        "paper_order_id": oid,
        "user_id": user_id,
        "ticker": "AAPL",
        "predicted_return_pct": 10.0,
        "predicted_volatility_pct": 20.0,
        "predicted_prob_of_profit": prob,
        "predicted_hold_days": 30,
        "realized_return_pct": 5.0 if hit else -5.0,
        "realized_at": datetime.now(tz=timezone.utc).isoformat(),
        "hit": hit,
        "brier_component": (prob - (1.0 if hit else 0.0)) ** 2,
        "resolved_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    return oid


class TestFitForUser:
    def test_below_gate_returns_none(self):
        for _ in range(5):    # well below UNLOCK_N=30
            _seed_outcome("u-1", 0.6, True)
        assert calibration.fit_for_user("u-1") is None

    def test_at_or_above_gate_fits_and_persists(self):
        for i in range(40):
            _seed_outcome("u-1", 0.7, hit=(i < 20))   # 50% hit at p=0.7 → over-confident
        fit = calibration.fit_for_user("u-1")
        assert fit is not None
        assert fit.n_samples == 40
        assert fit.brier_after <= fit.brier_before
        # Persisted into memstore.
        stored = calibration.current_fit("u-1")
        assert stored is not None
        assert stored.a == fit.a and stored.b == fit.b

    def test_applied_flag_starts_false(self):
        for i in range(40):
            _seed_outcome("u-1", 0.7, hit=(i < 20))
        calibration.fit_for_user("u-1")
        assert not calibration.is_opted_in("u-1")


# ---------------------------------------------------------------------------
# Opt-in flow
# ---------------------------------------------------------------------------

class TestOptIn:
    def test_opt_in_without_fit_returns_false(self):
        assert calibration.opt_in("u-1", apply=True) is False

    def test_opt_in_flips_flag(self):
        for i in range(40):
            _seed_outcome("u-1", 0.7, hit=(i < 20))
        calibration.fit_for_user("u-1")
        assert not calibration.is_opted_in("u-1")
        assert calibration.opt_in("u-1", apply=True) is True
        assert calibration.is_opted_in("u-1") is True

    def test_revoke(self):
        for i in range(40):
            _seed_outcome("u-1", 0.7, hit=(i < 20))
        calibration.fit_for_user("u-1")
        calibration.opt_in("u-1", apply=True)
        calibration.opt_in("u-1", apply=False)
        assert calibration.is_opted_in("u-1") is False


# ---------------------------------------------------------------------------
# Suggestion shape — drives the Overview card
# ---------------------------------------------------------------------------

class TestSuggestion:
    def test_no_fit_status_below_gate(self):
        s = calibration.suggestion("u-1")
        assert s["status"] == "no_fit"
        assert s["n"] == 0

    def test_available_status_after_fit(self):
        # Seed strongly over-confident data so the Platt fit clearly helps.
        for i in range(40):
            _seed_outcome("u-1", 0.85, hit=(i < 20))   # claimed 85%, real 50%
        s = calibration.suggestion("u-1")
        assert s["status"] == "available"
        assert s["n"] >= 30
        assert s["improvement"] > 0

    def test_applied_status_after_opt_in(self):
        for i in range(40):
            _seed_outcome("u-1", 0.85, hit=(i < 20))
        calibration.suggestion("u-1")   # auto-fits
        calibration.opt_in("u-1", apply=True)
        s = calibration.suggestion("u-1")
        assert s["status"] == "applied"


# ---------------------------------------------------------------------------
# apply_if_opted_in passthrough behavior
# ---------------------------------------------------------------------------

class TestApplyHook:
    def test_passthrough_without_opt_in(self):
        # No fit, no opt-in → returns input untouched.
        assert calibration.apply_if_opted_in("u-1", 0.7) == 0.7

    def test_calibrated_when_opted_in(self):
        for i in range(40):
            _seed_outcome("u-1", 0.85, hit=(i < 20))   # over-confident
        calibration.fit_for_user("u-1")
        calibration.opt_in("u-1", apply=True)
        calibrated = calibration.apply_if_opted_in("u-1", 0.85)
        # Should be deflated below the raw 0.85 since the user is over-confident.
        assert calibrated is not None
        assert calibrated < 0.85

    def test_handles_none_probability(self):
        assert calibration.apply_if_opted_in("u-1", None) is None


# ---------------------------------------------------------------------------
# Integration: kelly_sizing uses calibrated p when opted in
# ---------------------------------------------------------------------------

class TestKellyIntegration:
    def test_kelly_uses_raw_when_not_opted_in(self):
        # Same decision, two calls — one with user_id, one without. Without
        # opt-in, the two should match.
        d = _decision(prob=0.65)
        r1 = paper.kelly_sizing(d, nav=100_000, last_price=100, user_id=None)
        r2 = paper.kelly_sizing(d, nav=100_000, last_price=100, user_id="u-1")
        assert r1.fraction == pytest.approx(r2.fraction)

    def test_kelly_shrinks_when_calibration_says_overconfident(self):
        # Train an over-confident calibration head on u-1.
        for i in range(40):
            _seed_outcome("u-1", 0.85, hit=(i < 20))
        calibration.fit_for_user("u-1")
        calibration.opt_in("u-1", apply=True)

        d = _decision(prob=0.85)
        # With opt-in: calibrated p should be lower → smaller Kelly fraction.
        with_user = paper.kelly_sizing(d, nav=100_000, last_price=100, user_id="u-1")
        without_user = paper.kelly_sizing(d, nav=100_000, last_price=100, user_id=None)
        assert with_user.fraction < without_user.fraction


def _decision(prob: float) -> PortfolioDecision:
    return PortfolioDecision(
        rating=PortfolioRating.BUY,
        executive_summary="x", investment_thesis="x",
        expected_return_pct=20.0, expected_volatility_pct=10.0,
        prob_of_profit=prob, expected_hold_days=30,
    )
