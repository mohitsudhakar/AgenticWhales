"""Factory for picking the right BrokerClient based on environment / config.

This is the only place that knows about the concrete adapter classes, so
callers can write::

    broker = build_broker()  # mode + creds resolved from env

without importing any specific broker module. Keeps alpaca-py optional.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from .broker import BrokerClient, BrokerError
from .schemas import ExecutionMode

logger = logging.getLogger(__name__)


def _resolve_mode(mode: Optional[str]) -> ExecutionMode:
    raw = (mode or os.environ.get("BROKERAGE_MODE") or "paper").strip().lower()
    try:
        return ExecutionMode(raw)
    except ValueError as exc:
        raise BrokerError(f"unknown BROKERAGE_MODE {raw!r}") from exc


def build_broker(
    mode: Optional[str] = None,
    *,
    starting_cash: float = 100_000.0,
) -> BrokerClient:
    """Instantiate the broker for the requested mode.

    - ``backtest`` -> SimulatedBroker (no creds needed)
    - ``paper``    -> AlpacaBroker against paper endpoint (needs creds)
    - ``live``     -> AlpacaBroker against live endpoint (needs creds + explicit opt-in)
    """
    exec_mode = _resolve_mode(mode)

    if exec_mode == ExecutionMode.BACKTEST:
        from .brokers import SimulatedBroker
        return SimulatedBroker(starting_cash=starting_cash)

    # paper or live
    if exec_mode == ExecutionMode.LIVE and os.environ.get("BROKERAGE_ALLOW_LIVE") != "1":
        raise BrokerError(
            "Live mode requires BROKERAGE_ALLOW_LIVE=1 in the environment "
            "as an explicit opt-in safety check."
        )

    from .brokers.alpaca import AlpacaBroker
    return AlpacaBroker(mode=exec_mode)
