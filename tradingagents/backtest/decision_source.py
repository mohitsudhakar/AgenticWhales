"""Pluggable sources of PortfolioDecision for the backtest harness.

The agent graph is one possible source (expensive — one LLM-driven run per
trade date), but to validate the executor + sizing pipeline cheaply we
also provide:

- ``FixedRatingDecisionSource``: always returns the same rating; useful to
  prove the harness wires correctly end-to-end.
- ``ReplayDecisionSource``: feeds a pre-computed mapping of (date, ticker)
  → rating, so historical decisions can be replayed deterministically.
- ``CallableDecisionSource``: wrap any function in the protocol surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Dict, Mapping, Optional, Protocol, Tuple, runtime_checkable

from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating


@runtime_checkable
class DecisionSource(Protocol):
    def decide(self, ticker: str, trade_date: date) -> Optional[PortfolioDecision]: ...


@dataclass
class FixedRatingDecisionSource:
    rating: PortfolioRating
    summary: str = "fixed-rating backtest source"

    def decide(self, ticker: str, trade_date: date) -> PortfolioDecision:
        return PortfolioDecision(
            rating=self.rating,
            executive_summary=self.summary,
            investment_thesis=f"{self.rating.value} forced for {ticker} on {trade_date}",
        )


class ReplayDecisionSource:
    """Feed historical decisions (date, ticker) -> rating into the harness."""

    def __init__(self, decisions: Mapping[Tuple[str, str], PortfolioRating]) -> None:
        # Normalise keys to (ISO date string, uppercase ticker)
        self._decisions: Dict[Tuple[str, str], PortfolioRating] = {
            (d, t.strip().upper()): r for (d, t), r in decisions.items()
        }

    def decide(self, ticker: str, trade_date: date) -> Optional[PortfolioDecision]:
        key = (trade_date.isoformat(), ticker.strip().upper())
        rating = self._decisions.get(key)
        if rating is None:
            return None
        return PortfolioDecision(
            rating=rating,
            executive_summary=f"replayed {rating.value} for {ticker} on {trade_date}",
            investment_thesis="replayed decision",
        )


@dataclass
class CallableDecisionSource:
    fn: Callable[[str, date], Optional[PortfolioDecision]]

    def decide(self, ticker: str, trade_date: date) -> Optional[PortfolioDecision]:
        return self.fn(ticker, trade_date)


class AgentGraphDecisionSource:
    """Drive the backtest from the real agent graph.

    Expensive — one full LLM-driven run per (ticker, date) — so the harness
    typically pairs this with a coarse ``rebalance_every_n_bars`` (weekly
    or longer). Use the cheap sources above for testing the pipeline first.
    """

    def __init__(self, graph) -> None:
        self.graph = graph

    def decide(self, ticker: str, trade_date: date) -> Optional[PortfolioDecision]:
        from tradingagents.execution.translation import decision_from_final_state

        final_state, _signal = self.graph.propagate(ticker, trade_date.isoformat())
        return decision_from_final_state(final_state)
