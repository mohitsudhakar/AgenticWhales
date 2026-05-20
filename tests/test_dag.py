"""Tests for the multi-TF DAG fan-out helper."""

from __future__ import annotations

import pytest

from agenticwhales.agents.schemas import PortfolioDecision, PortfolioRating
from agenticwhales.dag import (
    CANONICAL_TIMEFRAMES,
    _disagreement_index,
    merge_decisions,
    tf_weights,
)


def _make(rating, *, er=5.0, vol=15.0, p=0.55, hold=5, stop=95.0):
    return PortfolioDecision(
        rating=rating,
        stop_loss=stop,
        expected_return_pct=er, expected_volatility_pct=vol,
        prob_of_profit=p, expected_hold_days=hold,
        executive_summary="x", investment_thesis="y",
    )


class TestTFWeights:
    def test_empty(self):
        assert tf_weights([], expected_hold_days=5) == {}

    def test_sums_to_one(self):
        w = tf_weights(["1m", "1h", "1d"], expected_hold_days=5)
        assert abs(sum(w.values()) - 1.0) < 1e-9

    def test_long_hold_favors_long_tf(self):
        # 30-day hold should weight 1d much more than 1m.
        w = tf_weights(["1m", "1h", "1d"], expected_hold_days=30)
        assert w["1d"] > w["1h"] > w["1m"]

    def test_short_hold_favors_short_tf(self):
        # ~0.5-day hold should weight 1h more than 1d.
        w = tf_weights(["1m", "1h", "1d"], expected_hold_days=0.5)
        assert w["1h"] > w["1d"]

    def test_unknown_tf_dropped(self):
        w = tf_weights(["1m", "fortnight", "1d"], expected_hold_days=5)
        assert "fortnight" not in w
        assert set(w.keys()) == {"1m", "1d"}


class TestMergeDecisions:
    def test_returns_none_on_empty(self):
        assert merge_decisions({}) is None

    def test_unanimous_agreement_passes_through(self):
        d = _make(PortfolioRating.OVERWEIGHT)
        merged = merge_decisions({"1h": d, "1d": d})
        assert merged is not None
        assert merged.rating == PortfolioRating.OVERWEIGHT

    def test_weighted_average_picks_majority(self):
        decisions = {
            "1m": _make(PortfolioRating.SELL),
            "1h": _make(PortfolioRating.BUY),
            "1d": _make(PortfolioRating.BUY),
        }
        merged = merge_decisions(decisions)
        # 1d weighted higher under default 5-day hold; 2 buys dominate the sell.
        assert merged is not None
        assert merged.rating in (PortfolioRating.OVERWEIGHT, PortfolioRating.BUY)

    def test_explicit_weights_dominate(self):
        decisions = {
            "1d": _make(PortfolioRating.SELL),
            "1h": _make(PortfolioRating.BUY),
        }
        merged = merge_decisions(decisions, weights={"1d": 0.95, "1h": 0.05})
        assert merged.rating in (PortfolioRating.UNDERWEIGHT, PortfolioRating.SELL)

    def test_stop_loss_uses_median(self):
        d1 = _make(PortfolioRating.OVERWEIGHT, stop=95.0)
        d2 = _make(PortfolioRating.OVERWEIGHT, stop=90.0)
        d3 = _make(PortfolioRating.OVERWEIGHT, stop=10.0)  # outlier
        merged = merge_decisions({"1m": d1, "1h": d2, "1d": d3})
        # Median of [10, 90, 95] = 90 — outlier ignored
        assert merged.stop_loss == 90.0

    def test_thesis_contains_disagreement(self):
        decisions = {
            "1m": _make(PortfolioRating.SELL),
            "1d": _make(PortfolioRating.BUY),
        }
        merged = merge_decisions(decisions)
        assert merged is not None
        assert "disagree" in merged.investment_thesis

    def test_prob_clamped_to_unit(self):
        decisions = {"1d": _make(PortfolioRating.BUY, p=0.99)}
        merged = merge_decisions(decisions)
        assert 0.0 <= merged.prob_of_profit <= 1.0


class TestDisagreement:
    def test_zero_when_unanimous(self):
        d = _make(PortfolioRating.OVERWEIGHT)
        assert _disagreement_index({"a": d, "b": d}) == 0.0

    def test_high_when_opposite_extremes(self):
        a = _make(PortfolioRating.SELL)
        b = _make(PortfolioRating.BUY)
        assert _disagreement_index({"a": a, "b": b}) >= 0.99


class TestCanonical:
    def test_canonical_set_unchanged(self):
        assert "1m" in CANONICAL_TIMEFRAMES
        assert "1d" in CANONICAL_TIMEFRAMES
