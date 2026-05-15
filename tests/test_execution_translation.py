"""Parse PortfolioDecision out of the Portfolio Manager's rendered markdown."""

from __future__ import annotations

import pytest

from tradingagents.agents.schemas import PortfolioRating, render_pm_decision, PortfolioDecision
from tradingagents.execution.translation import decision_from_final_state, decision_from_markdown


@pytest.mark.unit
def test_parse_canonical_render():
    """Round-trip: a PortfolioDecision rendered by the PM should parse back."""
    original = PortfolioDecision(
        rating=PortfolioRating.OVERWEIGHT,
        executive_summary="Trim long by 30% on next bounce; cut below 180.",
        investment_thesis="Earnings beat but guidance flat.",
        price_target=215.0,
        time_horizon="3-6 months",
    )
    md = render_pm_decision(original)
    parsed = decision_from_markdown(md)
    assert parsed.rating == PortfolioRating.OVERWEIGHT
    assert parsed.price_target == 215.0
    assert "3-6 months" in (parsed.time_horizon or "")


@pytest.mark.unit
def test_missing_rating_defaults_to_hold():
    parsed = decision_from_markdown("This text has no rating at all.")
    assert parsed.rating == PortfolioRating.HOLD


@pytest.mark.unit
def test_empty_input_does_not_raise():
    parsed = decision_from_markdown("")
    assert parsed.rating == PortfolioRating.HOLD
    assert parsed.executive_summary  # has fallback text


@pytest.mark.unit
def test_from_final_state_pulls_final_trade_decision_key():
    md = "**Rating**: Sell\n\n**Executive Summary**: exit fully"
    state = {"final_trade_decision": md, "other_key": "ignored"}
    parsed = decision_from_final_state(state)
    assert parsed.rating == PortfolioRating.SELL


@pytest.mark.unit
def test_from_final_state_handles_missing_key():
    parsed = decision_from_final_state({})
    assert parsed.rating == PortfolioRating.HOLD


@pytest.mark.unit
def test_price_target_parses_with_dollar_sign():
    md = "**Rating**: Buy\n\n**Price Target**: $250.50"
    parsed = decision_from_markdown(md)
    assert parsed.price_target == 250.50
