"""Brokerage-agnostic client interface.

Every broker adapter implements this Protocol. The Executor and backtest
harness only ever depend on this surface — never on the concrete adapter —
so SimulatedBroker / AlpacaBroker / future-IBKR-adapter are drop-in swaps.
"""

from __future__ import annotations

from typing import List, Optional, Protocol, runtime_checkable

from .schemas import Account, BrokerPosition, ExecutionMode, Order, OrderRequest, Quote


class BrokerError(RuntimeError):
    """Raised by brokers for any non-recoverable execution failure.

    Recoverable conditions (e.g. retryable network blips) should be retried
    inside the adapter; this exception means the caller should stop.
    """


@runtime_checkable
class BrokerClient(Protocol):
    """Minimal brokerage surface needed for the Executor + reconciliation loop."""

    mode: ExecutionMode

    def get_account(self) -> Account: ...

    def get_positions(self) -> List[BrokerPosition]: ...

    def get_position(self, symbol: str) -> Optional[BrokerPosition]: ...

    def place_order(self, request: OrderRequest) -> Order: ...

    def cancel_order(self, order_id: str) -> None: ...

    def get_order(self, order_id: str) -> Order: ...

    def get_latest_quote(self, symbol: str) -> Quote: ...

    def is_market_open(self) -> bool: ...
