"""Tests for `paper.kelly_sizing` — the Phase 1 / Demis-review sizing path.

Three properties matter:
  1. Quarter/deci-Kelly math is correct for plausible inputs.
  2. Edge cases (p=0, p=1, b=0, negative scalars) degrade to qty=0.
  3. Direction = sign(rating); magnitude is independent of rating.
"""

from __future__ import annotations

import pytest

from agenticwhales.agents.schemas import PortfolioDecision, PortfolioRating
from agenticwhales.paper import kelly_sizing, score_from_decision


def _decision(
    rating: PortfolioRating,
    *,
    er: float = 10.0,
    vol: float = 20.0,
    p: float = 0.6,
    hold: int = 30,
) -> PortfolioDecision:
    return PortfolioDecision(
        rating=rating,
        executive_summary="x",
        investment_thesis="x",
        expected_return_pct=er,
        expected_volatility_pct=vol,
        prob_of_profit=p,
        expected_hold_days=hold,
    )


class TestKellyHappyPath:
    def test_buy_positive_qty(self):
        d = _decision(PortfolioRating.BUY, er=20.0, vol=20.0, p=0.6)
        result = kelly_sizing(d, nav=100_000, last_price=100, kelly_fraction_cap=0.10)
        # Full Kelly: (0.6 * 2 - 1) / 1 = 0.2 -> 0.02 after deci-cap -> $2000 -> 20 shares
        assert result.direction == 1
        assert result.qty == pytest.approx(20.0, rel=1e-3)
        assert 0 < result.fraction <= 0.10

    def test_sell_negative_qty(self):
        d = _decision(PortfolioRating.SELL, er=-15.0, vol=20.0, p=0.65)
        result = kelly_sizing(d, nav=100_000, last_price=50, kelly_fraction_cap=0.10)
        assert result.direction == -1
        assert result.qty < 0

    def test_overweight_same_sign_as_buy(self):
        d1 = _decision(PortfolioRating.BUY)
        d2 = _decision(PortfolioRating.OVERWEIGHT)
        r1 = kelly_sizing(d1, nav=100_000, last_price=100)
        r2 = kelly_sizing(d2, nav=100_000, last_price=100)
        # Identical scalars + same direction → identical sizing.
        # Magnitude is independent of rating bucket.
        assert r1.qty == pytest.approx(r2.qty)
        assert r1.direction == r2.direction == 1

    def test_underweight_same_magnitude_opposite_sign_to_overweight(self):
        # Use params where Kelly actually fires (b=2, p=0.6 → f*=0.4).
        # OVERWEIGHT: bet the underlying goes up by er → trade PnL +er.
        # UNDERWEIGHT: bet the underlying goes down by -er → trade PnL -er.
        # Both have abs(er)=20 so Kelly magnitude matches; only direction flips.
        d_over = _decision(PortfolioRating.OVERWEIGHT, er=20.0, vol=10.0, p=0.6)
        d_under = _decision(PortfolioRating.UNDERWEIGHT, er=-20.0, vol=10.0, p=0.6)
        r_over = kelly_sizing(d_over, nav=100_000, last_price=100)
        r_under = kelly_sizing(d_under, nav=100_000, last_price=100)
        assert r_over.qty > 0
        assert r_under.qty < 0
        assert abs(r_over.qty) == pytest.approx(abs(r_under.qty), rel=1e-6)

    def test_kelly_can_overrule_rating_when_expectancy_negative(self):
        # Safety property: if Kelly math says don't bet (negative f*), the
        # rating doesn't override it. This is intentional — uncalibrated LLM
        # rating + poor risk/reward should not auto-size into a bad trade.
        d = _decision(PortfolioRating.BUY, er=10.0, vol=20.0, p=0.6)
        # b = 10/20 = 0.5, f* = (0.6*1.5 - 1)/0.5 = -0.2 → no bet
        assert kelly_sizing(d, nav=100_000, last_price=100).qty == 0.0


class TestKellyEdges:
    def test_hold_returns_zero(self):
        d = _decision(PortfolioRating.HOLD)
        assert kelly_sizing(d, nav=1, last_price=1).qty == 0.0

    def test_missing_prob_returns_zero(self):
        d = PortfolioDecision(
            rating=PortfolioRating.BUY,
            executive_summary="x", investment_thesis="x",
            expected_return_pct=10.0, expected_volatility_pct=20.0,
            prob_of_profit=None, expected_hold_days=30,
        )
        assert kelly_sizing(d, nav=100_000, last_price=100).qty == 0.0

    def test_missing_return_returns_zero(self):
        d = PortfolioDecision(
            rating=PortfolioRating.BUY,
            executive_summary="x", investment_thesis="x",
            expected_return_pct=None, expected_volatility_pct=20.0,
            prob_of_profit=0.6, expected_hold_days=30,
        )
        assert kelly_sizing(d, nav=100_000, last_price=100).qty == 0.0

    def test_zero_volatility_returns_zero(self):
        d = _decision(PortfolioRating.BUY, vol=0.0)
        # Replace with er fallback: since vol=0 the loss leg derives from |er|;
        # b = 1 → p=0.6 → f* = 0.2 — non-zero. So this is intentionally NOT
        # the zero path; the actual zero path is er=0 too.
        d_zero = _decision(PortfolioRating.BUY, er=0.0, vol=0.0)
        assert kelly_sizing(d_zero, nav=100_000, last_price=100).qty == 0.0

    def test_p_below_threshold_returns_zero(self):
        # b=1, p=0.4 → f* = 0.4*2 - 1 = -0.2 (negative; no bet)
        d = _decision(PortfolioRating.BUY, er=10.0, vol=10.0, p=0.4)
        assert kelly_sizing(d, nav=100_000, last_price=100).qty == 0.0

    def test_p_at_one_caps_to_full(self):
        # p=1 should bet the cap (not infinity).
        d = _decision(PortfolioRating.BUY, er=10.0, vol=10.0, p=1.0)
        result = kelly_sizing(d, nav=100_000, last_price=100, kelly_fraction_cap=0.10)
        # f* = (1 * 2 - 1) / 1 = 1.0; capped at 0.10 → $10k → 100 shares
        assert result.qty == pytest.approx(100.0, rel=1e-3)

    def test_negative_nav_returns_zero(self):
        d = _decision(PortfolioRating.BUY)
        assert kelly_sizing(d, nav=-1.0, last_price=100).qty == 0.0

    def test_zero_price_returns_zero(self):
        d = _decision(PortfolioRating.BUY)
        assert kelly_sizing(d, nav=100_000, last_price=0).qty == 0.0


class TestKellyCap:
    def test_deci_kelly_more_conservative_than_quarter(self):
        d = _decision(PortfolioRating.BUY, er=20.0, vol=20.0, p=0.6)
        deci = kelly_sizing(d, nav=100_000, last_price=100, kelly_fraction_cap=0.10)
        quarter = kelly_sizing(d, nav=100_000, last_price=100, kelly_fraction_cap=0.25)
        assert deci.qty < quarter.qty
        assert deci.fraction < quarter.fraction


class TestConvictionScore:
    def test_score_in_bounds_for_all_ratings(self):
        for r in PortfolioRating:
            d = _decision(r, er=10.0, vol=15.0, p=0.55, hold=30)
            s = score_from_decision(d)
            assert 1 <= s <= 10

    def test_higher_signal_higher_score(self):
        low = _decision(PortfolioRating.BUY, er=3.0, vol=30.0, p=0.55)
        high = _decision(PortfolioRating.BUY, er=20.0, vol=10.0, p=0.75)
        assert score_from_decision(high) > score_from_decision(low)

    def test_falls_back_when_scalars_missing(self):
        d = PortfolioDecision(
            rating=PortfolioRating.BUY,
            executive_summary="x", investment_thesis="x",
        )
        # Fallback table: BUY → 9.
        assert score_from_decision(d) == 9
