"""Tests for the Classical (non-LLM) Analyst — Phase 2 deliverable #6.

We test the signal helpers + aggregation + ClassicalResult contract with
synthetic OHLCV. No live yfinance calls — `load_ohlcv` is monkeypatched.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import math
import numpy as np
import pandas as pd
import pytest

from agenticwhales import classical
from agenticwhales.agents.schemas import PortfolioRating


# ---------------------------------------------------------------------------
# Synthetic-OHLCV helpers
# ---------------------------------------------------------------------------

def _ohlcv_uptrend(n: int = 500, start: float = 100.0, drift: float = 0.003) -> pd.DataFrame:
    """Roughly-monotonic uptrend OHLCV. Used to verify a buy-side response.
    Default drift is set high enough that signal aggregation clearly clears
    the +0.20 Overweight cutoff even after vol-regime dampening."""
    rng = np.random.RandomState(0)
    closes = start * np.cumprod(1 + drift + rng.normal(0, 0.005, n))
    highs = closes * (1 + rng.uniform(0, 0.01, n))
    lows  = closes * (1 - rng.uniform(0, 0.01, n))
    opens = closes * (1 + rng.uniform(-0.005, 0.005, n))
    return pd.DataFrame({
        "Date": pd.date_range("2023-01-01", periods=n, freq="B"),
        "Open": opens, "High": highs, "Low": lows, "Close": closes,
        "Volume": rng.uniform(1e6, 5e6, n),
    })


def _ohlcv_downtrend(n: int = 500, start: float = 1000.0, drift: float = -0.003) -> pd.DataFrame:
    return _ohlcv_uptrend(n=n, start=start, drift=drift)


def _ohlcv_flat(n: int = 500, start: float = 100.0) -> pd.DataFrame:
    rng = np.random.RandomState(1)
    closes = start + rng.normal(0, 0.05, n)
    highs = closes + rng.uniform(0, 0.1, n)
    lows  = closes - rng.uniform(0, 0.1, n)
    return pd.DataFrame({
        "Date": pd.date_range("2023-01-01", periods=n, freq="B"),
        "Open": closes, "High": highs, "Low": lows, "Close": closes,
        "Volume": np.ones(n) * 1e6,
    })


# ---------------------------------------------------------------------------
# Signal helpers
# ---------------------------------------------------------------------------

class TestSignals:
    def test_momentum_long_on_uptrend(self):
        df = _ohlcv_uptrend()
        sig = classical.momentum_signal(df["Close"])
        assert sig.direction == 1
        assert sig.strength > 0
        assert "12-1 return" in sig.notes

    def test_momentum_short_on_downtrend(self):
        df = _ohlcv_downtrend()
        sig = classical.momentum_signal(df["Close"])
        assert sig.direction == -1

    def test_momentum_neutral_on_short_history(self):
        sig = classical.momentum_signal(pd.Series([100.0, 101.0, 102.0]))
        assert sig.direction == 0
        assert sig.strength == 0

    def test_bollinger_short_above_band(self):
        # Recent close >> the prior 20-day mean.
        close = pd.Series([100.0] * 19 + [200.0])
        sig = classical.bollinger_signal(close)
        assert sig.direction == -1
        assert "above upper" in sig.notes

    def test_bollinger_long_below_band(self):
        close = pd.Series([100.0] * 19 + [50.0])
        sig = classical.bollinger_signal(close)
        assert sig.direction == 1
        assert "below lower" in sig.notes

    def test_bollinger_neutral_inside_band(self):
        rng = np.random.RandomState(2)
        close = pd.Series(100 + rng.normal(0, 1, 30))
        sig = classical.bollinger_signal(close)
        assert sig.direction == 0

    def test_trend_long_on_golden_cross(self):
        df = _ohlcv_uptrend()
        sig = classical.trend_signal(df["Close"])
        assert sig.direction == 1
        assert sig.strength > 0

    def test_trend_short_on_death_cross(self):
        df = _ohlcv_downtrend()
        sig = classical.trend_signal(df["Close"])
        assert sig.direction == -1

    def test_atr_positive_on_real_data(self):
        df = _ohlcv_uptrend()
        atr = classical.atr_value(df["High"], df["Low"], df["Close"])
        assert atr > 0

    def test_vol_multiplier_in_range(self):
        df = _ohlcv_uptrend()
        mult, note = classical.vol_regime_multiplier(df["High"], df["Low"], df["Close"])
        assert 0.5 <= mult <= 1.0
        assert "ATR percentile" in note


# ---------------------------------------------------------------------------
# Aggregation → PortfolioDecision
# ---------------------------------------------------------------------------

class TestAggregation:
    def test_uptrend_yields_long_rating(self):
        df = _ohlcv_uptrend()
        with patch("agenticwhales.dataflows.stockstats_utils.load_ohlcv", return_value=df):
            result = classical.analyze_classical("FAKE", "2024-12-31")
        assert result is not None
        assert result.decision.rating in (PortfolioRating.BUY, PortfolioRating.OVERWEIGHT)
        assert result.aggregate_score > 0
        # Scalars populated.
        assert result.decision.expected_return_pct is not None
        assert result.decision.prob_of_profit is not None
        assert 0.30 <= result.decision.prob_of_profit <= 0.70
        assert result.decision.stop_loss is not None
        assert result.decision.stop_loss < result.last_price
        assert result.decision.take_profit > result.last_price

    def test_downtrend_yields_short_rating(self):
        df = _ohlcv_downtrend()
        with patch("agenticwhales.dataflows.stockstats_utils.load_ohlcv", return_value=df):
            result = classical.analyze_classical("FAKE", "2024-12-31")
        assert result is not None
        assert result.decision.rating in (PortfolioRating.SELL, PortfolioRating.UNDERWEIGHT)
        assert result.aggregate_score < 0
        # Short: stop above, target below.
        assert result.decision.stop_loss > result.last_price
        assert result.decision.take_profit < result.last_price

    def test_flat_yields_hold(self):
        df = _ohlcv_flat()
        with patch("agenticwhales.dataflows.stockstats_utils.load_ohlcv", return_value=df):
            result = classical.analyze_classical("FAKE", "2024-12-31")
        assert result is not None
        # Tiny price noise should leave us close to neutral.
        assert abs(result.aggregate_score) < 0.5

    def test_insufficient_history_returns_none(self):
        # Only 100 bars — below the 200 SMA requirement.
        df = _ohlcv_uptrend(n=100)
        with patch("agenticwhales.dataflows.stockstats_utils.load_ohlcv", return_value=df):
            result = classical.analyze_classical("FAKE", "2024-12-31")
        assert result is None

    def test_load_failure_returns_none(self):
        with patch("agenticwhales.dataflows.stockstats_utils.load_ohlcv",
                   side_effect=Exception("boom")):
            result = classical.analyze_classical("FAKE", "2024-12-31")
        assert result is None


# ---------------------------------------------------------------------------
# QuantRadar (Option D) — deterministic 6-dim radar
# ---------------------------------------------------------------------------

class TestRadar:
    def test_radar_fields_in_bounds(self):
        df = _ohlcv_uptrend()
        with patch("agenticwhales.dataflows.stockstats_utils.load_ohlcv", return_value=df):
            result = classical.analyze_classical("FAKE", "2024-12-31")
        r = result.radar
        for v in (r.volatility_risk, r.sr_strength, r.breakout_likelihood,
                  r.momentum_strength, r.pattern_reliability, r.trend_certainty):
            assert 1 <= v <= 10
        assert r.direction in ("long", "short", "neutral")

    def test_radar_direction_matches_score(self):
        df = _ohlcv_uptrend()
        with patch("agenticwhales.dataflows.stockstats_utils.load_ohlcv", return_value=df):
            result = classical.analyze_classical("FAKE", "2024-12-31")
        assert result.radar.direction == "long"

    def test_radar_pattern_reliability_is_stub(self):
        """Phase 2 doesn't implement chart-pattern detection; the radar
        score for that axis is always 1 until Phase 3."""
        df = _ohlcv_uptrend()
        with patch("agenticwhales.dataflows.stockstats_utils.load_ohlcv", return_value=df):
            result = classical.analyze_classical("FAKE", "2024-12-31")
        assert result.radar.pattern_reliability == 1


# ---------------------------------------------------------------------------
# Output contract — Bull/Bear should never see same-family heterogeneity
# clash when the Classical Analyst is the third voice
# ---------------------------------------------------------------------------

class TestHeterogeneityProperty:
    def test_classical_voice_is_not_an_llm_family(self):
        """The Classical Analyst is the ground-truth non-LLM voice. We
        validate by simple type: its output is a deterministic function of
        the price series — same input → same output."""
        df = _ohlcv_uptrend()
        with patch("agenticwhales.dataflows.stockstats_utils.load_ohlcv", return_value=df):
            r1 = classical.analyze_classical("FAKE", "2024-12-31")
            r2 = classical.analyze_classical("FAKE", "2024-12-31")
        assert r1 is not None and r2 is not None
        assert r1.decision.rating == r2.decision.rating
        assert r1.aggregate_score == r2.aggregate_score
