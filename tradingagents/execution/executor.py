"""The Executor: turn a PortfolioDecision into broker orders.

This is the single seam where the agent graph hands off to the brokerage.
The same code runs against SimulatedBroker (backtest), AlpacaBroker paper
endpoint (paper), or AlpacaBroker live endpoint (live); only the
BrokerClient implementation swaps.

Design notes:

- The broker is the source of truth for positions, not the local
  ``portfolio.json``. We always read current qty from the broker before
  deciding what to trade. The local mirror is updated *after* the fact
  via :class:`tradingagents.execution.portfolio_mirror.PortfolioMirror`,
  not used to decide.
- Orders carry an idempotency key derived from (ticker, trade_date,
  rating). A re-run of the same decision on the same day cannot
  double-fire — the broker rejects the duplicate ``client_order_id``.
- ``dry_run=True`` skips the order placement but still returns the full
  delta + target so callers can show "what would happen" in the UI.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating

from .broker import BrokerClient, BrokerError
from .schemas import (
    ExecutionMode,
    ExecutionResult,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from .sizing import SizingPolicy

logger = logging.getLogger(__name__)


def _idempotency_key(ticker: str, trade_date: str, rating: PortfolioRating) -> str:
    payload = f"{ticker}|{trade_date}|{rating.value}"
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"ta-{digest}"


class Executor:
    """Execute a Portfolio Manager decision against any BrokerClient."""

    def __init__(
        self,
        broker: BrokerClient,
        sizing: Optional[SizingPolicy] = None,
        *,
        wait_for_fill: bool = True,
        fill_timeout_seconds: float = 30.0,
        fill_poll_seconds: float = 0.5,
    ) -> None:
        self.broker = broker
        self.sizing = sizing or SizingPolicy()
        self.wait_for_fill = wait_for_fill
        self.fill_timeout_seconds = fill_timeout_seconds
        self.fill_poll_seconds = fill_poll_seconds

    # ---------------------------------------------------------------- core
    def execute(
        self,
        ticker: str,
        decision: PortfolioDecision,
        *,
        trade_date: Optional[str] = None,
        reference_price: Optional[float] = None,
        dry_run: bool = False,
    ) -> ExecutionResult:
        """Translate ``decision`` into an order against the broker.

        ``reference_price`` is the price the sizing math should use. In
        backtest the harness passes the bar's open; in paper/live we fall
        back to ``broker.get_latest_quote()`` if not supplied.
        """
        ticker = ticker.strip().upper()
        rating = decision.rating
        trade_date = trade_date or datetime.now(timezone.utc).date().isoformat()

        target_weight = self.sizing.target_weight_for(rating)
        current_pos = self.broker.get_position(ticker)
        current_qty = current_pos.qty if current_pos else 0.0

        if target_weight is None:
            # HOLD — leave the position alone.
            return ExecutionResult(
                ticker=ticker,
                rating=rating.value,
                action="HOLD",
                prev_qty=current_qty,
                target_qty=current_qty,
                delta_qty=0.0,
                target_weight=None,
                reason="rating is Hold",
                mode=self.broker.mode,
            )

        price = reference_price
        if price is None:
            try:
                quote = self.broker.get_latest_quote(ticker)
                price = quote.last or quote.ask or quote.bid
            except BrokerError as exc:
                return ExecutionResult(
                    ticker=ticker,
                    rating=rating.value,
                    action="ERROR",
                    prev_qty=current_qty,
                    reason=f"quote failed: {exc}",
                    target_weight=target_weight,
                    mode=self.broker.mode,
                )
        if not price or price <= 0:
            return ExecutionResult(
                ticker=ticker,
                rating=rating.value,
                action="ERROR",
                prev_qty=current_qty,
                reason="no usable price for sizing",
                target_weight=target_weight,
                mode=self.broker.mode,
            )

        account = self.broker.get_account()
        target_qty = self.sizing.target_qty(target_weight, account.equity, price)
        delta = target_qty - current_qty
        delta_value = abs(delta) * price

        if delta == 0 or delta_value < self.sizing.min_order_value:
            return ExecutionResult(
                ticker=ticker,
                rating=rating.value,
                action="SKIP",
                prev_qty=current_qty,
                target_qty=target_qty,
                delta_qty=delta,
                reference_price=price,
                target_weight=target_weight,
                reason=(
                    "already at target" if delta == 0
                    else f"delta value ${delta_value:.2f} below min ${self.sizing.min_order_value:.2f}"
                ),
                mode=self.broker.mode,
            )

        side = OrderSide.BUY if delta > 0 else OrderSide.SELL
        qty = abs(delta)

        if dry_run:
            return ExecutionResult(
                ticker=ticker,
                rating=rating.value,
                action="DRY_RUN",
                prev_qty=current_qty,
                target_qty=target_qty,
                delta_qty=delta,
                reference_price=price,
                target_weight=target_weight,
                reason=f"would {side.value} {qty} @ ~{price:.2f}",
                mode=self.broker.mode,
            )

        request = OrderRequest(
            symbol=ticker,
            qty=qty,
            side=side,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            client_order_id=_idempotency_key(ticker, trade_date, rating),
        )

        try:
            order = self.broker.place_order(request)
        except BrokerError as exc:
            return ExecutionResult(
                ticker=ticker,
                rating=rating.value,
                action="ERROR",
                prev_qty=current_qty,
                target_qty=target_qty,
                delta_qty=delta,
                reference_price=price,
                target_weight=target_weight,
                reason=f"place_order failed: {exc}",
                mode=self.broker.mode,
            )

        if self.wait_for_fill and order.status not in (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELED):
            order = self._await_fill(order)

        return ExecutionResult(
            ticker=ticker,
            rating=rating.value,
            action=side.value.upper(),
            prev_qty=current_qty,
            target_qty=target_qty,
            delta_qty=delta,
            reference_price=price,
            target_weight=target_weight,
            order=order,
            mode=self.broker.mode,
        )

    # ----------------------------------------------------------- internal
    def _await_fill(self, order: Order) -> Order:
        deadline = time.monotonic() + self.fill_timeout_seconds
        current = order
        while time.monotonic() < deadline:
            try:
                current = self.broker.get_order(current.id)
            except BrokerError as exc:
                logger.warning("get_order(%s) failed during fill wait: %s", current.id, exc)
                break
            if current.status in (
                OrderStatus.FILLED,
                OrderStatus.REJECTED,
                OrderStatus.CANCELED,
                OrderStatus.EXPIRED,
            ):
                return current
            time.sleep(self.fill_poll_seconds)
        return current
