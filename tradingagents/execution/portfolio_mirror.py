"""Bridge between the broker's positions (source of truth) and the local
``~/.tradingagents/portfolio.json`` file the agent prompts read.

The agent graph injects the current position into prompt context via
``tradingagents.portfolio.format_for_prompt(symbol)``, which reads the
JSON file. Once we have a real broker, that file becomes a *mirror* of
broker state — we sync after every fill (and periodically in live mode)
so the next agent run sees the correct position.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable

from tradingagents import portfolio as portfolio_file
from .broker import BrokerClient, BrokerError
from .schemas import BrokerPosition

logger = logging.getLogger(__name__)


class PortfolioMirror:
    """Sync ``portfolio.json`` from a BrokerClient.

    The mirror is intentionally one-way (broker → file): user edits to
    the file are not pushed to the broker. This keeps the executor's
    invariant that broker truth is authoritative.
    """

    def __init__(self, broker: BrokerClient) -> None:
        self.broker = broker

    def sync(self) -> Dict[str, Dict[str, float]]:
        """Pull positions from the broker and overwrite portfolio.json.

        Returns the new positions dict for logging / display.
        """
        try:
            positions: Iterable[BrokerPosition] = self.broker.get_positions()
        except BrokerError as exc:
            logger.error("Mirror sync failed: %s", exc)
            return {}

        snapshot: Dict[str, Dict[str, float]] = {}
        for pos in positions:
            if pos.qty == 0:
                continue
            entry: Dict[str, float] = {"qty": float(pos.qty)}
            if pos.avg_entry_price is not None:
                entry["avg_cost"] = float(pos.avg_entry_price)
            snapshot[pos.symbol] = entry

        portfolio_file.save_all(snapshot)
        return snapshot
