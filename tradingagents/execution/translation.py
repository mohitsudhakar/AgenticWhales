"""Translate the agent graph's final state into a PortfolioDecision.

The graph's downstream consumers all read ``final_state["final_trade_decision"]``
as markdown. The executor wants a typed PortfolioDecision. We parse the
rating with the same deterministic heuristic the rest of the system uses
(``tradingagents.agents.utils.rating.parse_rating``) and carry the rest of
the markdown through as the executive summary so the broker logs have an
audit trail.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating
from tradingagents.agents.utils.rating import parse_rating


_PRICE_TARGET_RE = re.compile(r"price\s*target.*?[:\-]\s*\$?([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_TIME_HORIZON_RE = re.compile(r"time\s*horizon.*?[:\-]\s*(.+?)(?:\n|$)", re.IGNORECASE)


def decision_from_markdown(text: str) -> PortfolioDecision:
    """Parse a PortfolioDecision out of the rendered markdown.

    Always succeeds: missing fields default to safe values, and the rating
    falls back to Hold when nothing parseable is found.
    """
    rating_str = parse_rating(text or "", default="Hold")
    rating = PortfolioRating(rating_str)

    price_target = None
    m = _PRICE_TARGET_RE.search(text or "")
    if m:
        try:
            price_target = float(m.group(1))
        except ValueError:
            pass

    time_horizon = None
    m = _TIME_HORIZON_RE.search(text or "")
    if m:
        time_horizon = m.group(1).strip().strip("*").strip()

    return PortfolioDecision(
        rating=rating,
        executive_summary=(text or "").strip()[:2000] or "decision text unavailable",
        investment_thesis=(text or "").strip()[:4000] or "decision text unavailable",
        price_target=price_target,
        time_horizon=time_horizon,
    )


def decision_from_final_state(final_state: Mapping[str, Any]) -> PortfolioDecision:
    """Pull the Portfolio Manager's decision out of the graph's final state."""
    text = ""
    if isinstance(final_state, Mapping):
        text = final_state.get("final_trade_decision") or ""
    return decision_from_markdown(text)
