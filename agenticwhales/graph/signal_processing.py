"""Extract the 5-tier portfolio rating from the Portfolio Manager's decision.

Preferred source is the structured ``PortfolioDecision.rating`` field that the
Portfolio Manager emits via structured output and stashes on agent state as
``pm_decision``. The rendered markdown still carries a ``**Rating**: X``
header (see :func:`agenticwhales.agents.schemas.render_pm_decision`) and the
deterministic heuristic in :mod:`agenticwhales.agents.utils.rating` is the
fallback when only free text is available — but trusting prose makes the
system vulnerable to sycophantic markdown that contradicts the structured
field, so we use the typed value when we have it.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from agenticwhales.agents.schemas import PortfolioDecision, PortfolioRating
from agenticwhales.agents.utils.rating import RATINGS_5_TIER, parse_rating

logger = logging.getLogger(__name__)


def _rating_from_pm_decision(pm_decision: Any) -> str | None:
    """Pull a canonical 5-tier rating out of a PortfolioDecision-shaped object.

    Accepts the Pydantic model, the ``model_dump`` dict that the graph stashes
    on agent state, or ``None`` (free-text fallback fired). Returns ``None``
    when no usable rating is present so the caller can fall back to regex.
    """
    if pm_decision is None:
        return None

    if isinstance(pm_decision, PortfolioDecision):
        return pm_decision.rating.value

    if isinstance(pm_decision, Mapping):
        raw = pm_decision.get("rating")
        if isinstance(raw, PortfolioRating):
            return raw.value
        if isinstance(raw, str) and raw in RATINGS_5_TIER:
            return raw

    return None


class SignalProcessor:
    """Read the 5-tier rating out of a Portfolio Manager decision."""

    def __init__(self, quick_thinking_llm: Any = None):
        # The LLM argument is accepted for backwards compatibility but no
        # longer used: the structured PortfolioDecision is authoritative and
        # the markdown regex is only the free-text fallback path.
        self.quick_thinking_llm = quick_thinking_llm

    def process_signal(
        self,
        full_signal: str,
        pm_decision: Any = None,
    ) -> str:
        """Return one of Buy / Overweight / Hold / Underweight / Sell.

        Prefers the structured ``pm_decision.rating`` field when supplied;
        otherwise heuristically parses ``full_signal`` markdown. Falling back
        to text is logged at INFO so the rate is observable in metrics.
        """
        structured = _rating_from_pm_decision(pm_decision)
        if structured is not None:
            return structured

        logger.info(
            "signal_processing.regex_fallback",
            extra={"reason": "no_structured_pm_decision"},
        )
        return parse_rating(full_signal)
