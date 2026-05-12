"""Backtest harness for the trading-agents pipeline.

The harness replays historical bars through the same Executor + sizing
policy that powers paper and live trading. The only thing that swaps is
the BrokerClient (SimulatedBroker here) and the DecisionSource — the
agent graph itself can be plugged in when ready (expensive), but for
quick iteration there are cheaper decision sources (replay-from-CSV,
fixed rule, random) that exercise the executor pipeline end-to-end
without burning LLM calls.
"""

from .bars import load_history
from .decision_source import (
    AgentGraphDecisionSource,
    DecisionSource,
    FixedRatingDecisionSource,
    ReplayDecisionSource,
)
from .harness import BacktestHarness, BacktestResult
from .metrics import equity_metrics

__all__ = [
    "AgentGraphDecisionSource",
    "BacktestHarness",
    "BacktestResult",
    "DecisionSource",
    "FixedRatingDecisionSource",
    "ReplayDecisionSource",
    "equity_metrics",
    "load_history",
]
