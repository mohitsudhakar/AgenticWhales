"""Tests for the shared rating heuristic and the SignalProcessor adapter.

The Portfolio Manager produces a typed PortfolioDecision via structured
output and renders it to markdown that always contains a ``**Rating**: X``
header.  The deterministic heuristic in ``agenticwhales.agents.utils.rating``
is therefore sufficient to extract the rating downstream — no second LLM
call is needed — and SignalProcessor is now a thin adapter that delegates
to it.
"""

import pytest

from agenticwhales.agents.utils.rating import RATINGS_5_TIER, parse_rating
from agenticwhales.graph.signal_processing import SignalProcessor


# ---------------------------------------------------------------------------
# Heuristic parser
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseRating:
    def test_explicit_label_buy(self):
        assert parse_rating("Rating: Buy\nReasoning here.") == "Buy"

    def test_explicit_label_overweight(self):
        assert parse_rating("Rating: Overweight\nDetails.") == "Overweight"

    def test_explicit_label_with_markdown_bold_value(self):
        # Regression: Rating: **Sell** — markdown around the value.
        assert parse_rating("Rating: **Sell**\nExit immediately.") == "Sell"

    def test_explicit_label_with_markdown_bold_label(self):
        assert parse_rating("**Rating**: Underweight\nTrim exposure.") == "Underweight"

    def test_rendered_pm_markdown_shape(self):
        # The exact shape produced by render_pm_decision must always parse.
        text = (
            "**Rating**: Buy\n\n"
            "**Executive Summary**: Enter at $189-192, 6% portfolio cap.\n\n"
            "**Investment Thesis**: AI capex cycle intact; institutional flows constructive."
        )
        assert parse_rating(text) == "Buy"

    def test_explicit_label_wins_over_prose_with_markdown(self):
        text = (
            "The buy thesis is weakened by guidance.\n"
            "Rating: **Sell**\n"
            "Exit before earnings."
        )
        assert parse_rating(text) == "Sell"

    def test_no_rating_returns_default(self):
        assert parse_rating("No clear directional signal at this time.") == "Hold"

    def test_no_rating_custom_default(self):
        assert parse_rating("Plain prose.", default="Underweight") == "Underweight"

    def test_all_five_tiers_recognised(self):
        for r in RATINGS_5_TIER:
            assert parse_rating(f"Rating: {r}") == r


# ---------------------------------------------------------------------------
# SignalProcessor: thin adapter over the heuristic
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSignalProcessor:
    def test_returns_rating_from_pm_markdown(self):
        sp = SignalProcessor()
        md = "**Rating**: Overweight\n\n**Executive Summary**: Build gradually."
        assert sp.process_signal(md) == "Overweight"

    def test_makes_no_llm_calls(self):
        """SignalProcessor must not invoke the LLM it was constructed with —
        the rating is parseable from the rendered PM markdown directly."""
        from unittest.mock import MagicMock

        llm = MagicMock()
        sp = SignalProcessor(llm)
        sp.process_signal("Rating: Buy\nDetails.")
        llm.invoke.assert_not_called()
        llm.with_structured_output.assert_not_called()

    def test_default_when_no_rating_present(self):
        sp = SignalProcessor()
        assert sp.process_signal("Plain prose without a recommendation.") == "Hold"


# ---------------------------------------------------------------------------
# Sycophancy guard: structured PortfolioDecision overrides contradicting prose
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSycophancyGuard:
    """The PortfolioDecision.rating field is authoritative; prose isn't.

    Reward-hacking surface: an LLM that learns to write "Strong Buy" markdown
    while the structured field says HOLD would silently move the downstream
    Brier score. The signal path must trust the typed field when present.
    """

    def _hold_decision_dict(self):
        from agenticwhales.agents.schemas import PortfolioDecision

        return PortfolioDecision(
            rating="Hold",
            executive_summary="Evidence is balanced; sit out.",
            investment_thesis="Neither side carried the debate.",
        ).model_dump(mode="json")

    def test_structured_overrides_buy_in_markdown(self):
        sp = SignalProcessor()
        markdown_says_buy = (
            "**Rating**: Buy\n\n"
            "**Executive Summary**: Strong Buy — load up here.\n"
        )
        assert sp.process_signal(
            markdown_says_buy,
            self._hold_decision_dict(),
        ) == "Hold"

    def test_structured_accepts_pydantic_model(self):
        from agenticwhales.agents.schemas import PortfolioDecision

        sp = SignalProcessor()
        model = PortfolioDecision(
            rating="Sell",
            executive_summary="Exit now.",
            investment_thesis="Guidance cut.",
        )
        assert sp.process_signal("Rating: Buy\nWhatever the prose says.", model) == "Sell"

    def test_falls_back_when_structured_is_none(self):
        sp = SignalProcessor()
        # Free-text fallback fired in the PM node → pm_decision is None.
        assert sp.process_signal("Rating: Underweight\nTrim.", None) == "Underweight"

    def test_invalid_structured_rating_falls_back_to_regex(self):
        sp = SignalProcessor()
        # If something corrupted the dict, don't trust it — fall back.
        bogus = {"rating": "Maybe", "executive_summary": "x", "investment_thesis": "y"}
        assert sp.process_signal("Rating: Overweight\nDetails.", bogus) == "Overweight"
