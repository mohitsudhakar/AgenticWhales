"""Sizing policy: rating -> target weight -> share count."""

from __future__ import annotations

import math

import pytest

from tradingagents.agents.schemas import PortfolioRating
from tradingagents.execution.sizing import SizingPolicy


@pytest.mark.unit
def test_buy_rating_yields_positive_target_weight():
    sizing = SizingPolicy()
    assert sizing.target_weight_for(PortfolioRating.BUY) == 0.10
    assert sizing.target_weight_for(PortfolioRating.OVERWEIGHT) == 0.05


@pytest.mark.unit
def test_hold_returns_none_meaning_no_change():
    sizing = SizingPolicy()
    assert sizing.target_weight_for(PortfolioRating.HOLD) is None


@pytest.mark.unit
def test_short_disabled_collapses_bearish_to_zero():
    sizing = SizingPolicy(allow_short=False)
    assert sizing.target_weight_for(PortfolioRating.UNDERWEIGHT) == 0.0
    assert sizing.target_weight_for(PortfolioRating.SELL) == 0.0


@pytest.mark.unit
def test_short_enabled_passes_negative_weight():
    sizing = SizingPolicy(allow_short=True)
    assert sizing.target_weight_for(PortfolioRating.UNDERWEIGHT) == -0.025
    assert sizing.target_weight_for(PortfolioRating.SELL) == -0.05


@pytest.mark.unit
def test_max_position_weight_caps_target():
    sizing = SizingPolicy(
        target_weights={PortfolioRating.BUY: 0.50},
        max_position_weight=0.20,
    )
    assert sizing.target_weight_for(PortfolioRating.BUY) == 0.20


@pytest.mark.unit
def test_target_qty_truncates_toward_zero_for_integers():
    sizing = SizingPolicy()
    # 10% of $10,000 = $1,000; at $150 = 6.66 shares -> 6
    assert sizing.target_qty(0.10, 10_000, 150) == 6.0


@pytest.mark.unit
def test_target_qty_supports_fractional_shares_when_enabled():
    sizing = SizingPolicy(fractional=True)
    qty = sizing.target_qty(0.10, 10_000, 150)
    assert math.isclose(qty, 6.6667, rel_tol=1e-3)


@pytest.mark.unit
def test_zero_equity_yields_zero_qty():
    sizing = SizingPolicy()
    assert sizing.target_qty(0.10, 0, 150) == 0.0


@pytest.mark.unit
def test_negative_weight_target_qty_is_negative_when_short_enabled():
    sizing = SizingPolicy(allow_short=True)
    # -5% of $10,000 = -$500; at $100 = -5
    assert sizing.target_qty(-0.05, 10_000, 100) == -5.0
