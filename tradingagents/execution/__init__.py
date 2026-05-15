"""Brokerage execution layer.

Translates the Portfolio Manager's PortfolioDecision (a 5-tier rating with a
free-text plan) into concrete share-level orders against a pluggable broker
backend. The same Executor + sizing logic is used in backtest, paper, and
live modes — only the BrokerClient implementation changes.
"""

from .schemas import (
    Account,
    BrokerPosition,
    ExecutionMode,
    ExecutionResult,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Quote,
    TimeInForce,
)
from .broker import BrokerClient, BrokerError
from .sizing import SizingPolicy
from .executor import Executor
from .pipeline import LivePipeline
from .portfolio_mirror import PortfolioMirror
from .translation import decision_from_final_state, decision_from_markdown
from .factory import build_broker

__all__ = [
    "Account",
    "BrokerClient",
    "BrokerError",
    "BrokerPosition",
    "ExecutionMode",
    "ExecutionResult",
    "Executor",
    "LivePipeline",
    "Order",
    "OrderRequest",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "PortfolioMirror",
    "Quote",
    "SizingPolicy",
    "TimeInForce",
    "build_broker",
    "decision_from_final_state",
    "decision_from_markdown",
]
