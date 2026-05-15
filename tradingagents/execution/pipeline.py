"""High-level orchestration: agent graph -> executor -> portfolio mirror.

This is the seam the CLI and the web SessionRunner both call. It does
exactly three things:

1. Run the agent graph for one (ticker, date).
2. Translate the Portfolio Manager's final markdown decision into a typed
   PortfolioDecision and hand it to the Executor.
3. Sync the local ``portfolio.json`` mirror from broker truth so the next
   agent run sees the updated position.

The pipeline is deliberately thin — it composes existing modules rather
than introducing new state. The agent graph remains the single source of
investment decisions; the broker remains the single source of position
truth; ``portfolio.json`` becomes a read-only mirror for the prompts.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Tuple

from .broker import BrokerClient
from .executor import Executor
from .portfolio_mirror import PortfolioMirror
from .schemas import ExecutionResult
from .translation import decision_from_final_state

logger = logging.getLogger(__name__)


class LivePipeline:
    """Run the agent graph and trade on its decision via any BrokerClient."""

    def __init__(
        self,
        graph,
        broker: BrokerClient,
        executor: Optional[Executor] = None,
        *,
        mirror: bool = True,
        dry_run: bool = False,
    ) -> None:
        self.graph = graph
        self.broker = broker
        self.executor = executor or Executor(broker)
        self.mirror = PortfolioMirror(broker) if mirror else None
        self.dry_run = dry_run

    def run(self, ticker: str, trade_date: str) -> Tuple[Mapping[str, Any], ExecutionResult]:
        final_state, _signal = self.graph.propagate(ticker, trade_date)
        decision = decision_from_final_state(final_state)
        logger.info(
            "Pipeline decision for %s on %s: rating=%s",
            ticker, trade_date, decision.rating.value,
        )

        result = self.executor.execute(
            ticker, decision,
            trade_date=str(trade_date),
            dry_run=self.dry_run,
        )

        if self.mirror is not None and result.action in {"BUY", "SELL"} and result.order is not None:
            self.mirror.sync()

        return final_state, result
