"""In-memory broker for backtest and unit tests.

Behaviour:

- Market orders fill immediately at the *current* reference price, plus a
  configurable slippage and per-share commission. The backtest harness is
  expected to call :meth:`set_reference_price` before placing orders so
  the fill price reflects the bar being replayed.
- Positions and cash live entirely in process. The harness can call
  :meth:`mark_to_market` to update unrealized P&L and equity using a
  dict of latest prices.
- ``client_order_id`` is treated as an idempotency key: a second order
  with the same key is rejected (mirrors real brokers).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Dict, List, Optional

from tradingagents.execution.broker import BrokerError
from tradingagents.execution.schemas import (
    Account,
    BrokerPosition,
    ExecutionMode,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Quote,
    TimeInForce,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SimulatedBroker:
    """Implements :class:`BrokerClient` against an in-memory ledger."""

    mode = ExecutionMode.BACKTEST

    def __init__(
        self,
        *,
        starting_cash: float = 100_000.0,
        slippage_bps: float = 0.0,
        commission_per_share: float = 0.0,
        commission_min: float = 0.0,
        allow_short: bool = False,
    ) -> None:
        self._cash = float(starting_cash)
        self._slippage_bps = float(slippage_bps)
        self._commission_per_share = float(commission_per_share)
        self._commission_min = float(commission_min)
        self._allow_short = allow_short

        self._positions: Dict[str, BrokerPosition] = {}
        self._orders: Dict[str, Order] = {}
        self._client_ids: Dict[str, str] = {}   # client_order_id → order id
        self._prices: Dict[str, float] = {}
        self._order_seq = itertools.count(1)

    # ----------------------------------------------------- harness control
    def set_reference_price(self, symbol: str, price: float) -> None:
        """Set the price at which subsequent market orders for ``symbol`` will fill."""
        self._prices[symbol.strip().upper()] = float(price)

    def set_reference_prices(self, prices: Dict[str, float]) -> None:
        for sym, px in prices.items():
            self.set_reference_price(sym, px)

    def mark_to_market(self, prices: Optional[Dict[str, float]] = None) -> float:
        """Recompute every position's market value + return total equity."""
        if prices:
            self.set_reference_prices(prices)
        for sym, pos in self._positions.items():
            px = self._prices.get(sym)
            if px is None or pos.qty == 0:
                continue
            pos.market_value = pos.qty * px
            if pos.avg_entry_price is not None:
                pos.unrealized_pl = (px - pos.avg_entry_price) * pos.qty
        return self.equity()

    def equity(self) -> float:
        positions_value = 0.0
        for sym, pos in self._positions.items():
            px = self._prices.get(sym, pos.avg_entry_price or 0.0)
            positions_value += pos.qty * (px or 0.0)
        return self._cash + positions_value

    # ----------------------------------------------------- BrokerClient API
    def get_account(self) -> Account:
        eq = self.equity()
        return Account(equity=eq, buying_power=max(self._cash, 0.0), cash=self._cash)

    def get_positions(self) -> List[BrokerPosition]:
        return [pos for pos in self._positions.values() if pos.qty != 0]

    def get_position(self, symbol: str) -> Optional[BrokerPosition]:
        pos = self._positions.get(symbol.strip().upper())
        if pos is None or pos.qty == 0:
            return None
        return pos

    def place_order(self, request: OrderRequest) -> Order:
        if request.order_type != OrderType.MARKET:
            raise BrokerError(
                f"SimulatedBroker only supports market orders (got {request.order_type.value})"
            )
        if request.client_order_id and request.client_order_id in self._client_ids:
            raise BrokerError(
                f"duplicate client_order_id {request.client_order_id!r}"
            )

        sym = request.symbol
        ref_price = self._prices.get(sym)
        if ref_price is None or ref_price <= 0:
            raise BrokerError(f"no reference price set for {sym}")

        # Apply slippage: pay more when buying, receive less when selling.
        slip = ref_price * (self._slippage_bps / 10_000.0)
        fill_price = ref_price + slip if request.side == OrderSide.BUY else ref_price - slip
        commission = max(self._commission_min, self._commission_per_share * request.qty)

        signed_qty = request.qty if request.side == OrderSide.BUY else -request.qty
        prev = self._positions.get(sym)
        prev_qty = prev.qty if prev else 0.0
        new_qty = prev_qty + signed_qty

        if not self._allow_short and new_qty < -1e-9:
            raise BrokerError(
                f"shorting disabled: cannot sell {request.qty} {sym} with only {prev_qty} held"
            )

        cash_delta = -(signed_qty * fill_price) - commission
        new_cash = self._cash + cash_delta
        if new_cash < -1e-6:
            raise BrokerError(
                f"insufficient cash: order needs ${-cash_delta:.2f}, have ${self._cash:.2f}"
            )

        # Recompute average entry price on adds; preserve on partial trims.
        new_avg = None
        if abs(new_qty) > 1e-9:
            if prev_qty == 0 or (prev_qty > 0) != (new_qty > 0):
                new_avg = fill_price
            elif abs(new_qty) > abs(prev_qty):
                # adding to position
                prev_cost_basis = (prev.avg_entry_price or fill_price) * prev_qty
                added_cost_basis = fill_price * signed_qty
                new_avg = (prev_cost_basis + added_cost_basis) / new_qty
            else:
                # trimming: keep avg cost unchanged
                new_avg = prev.avg_entry_price if prev else fill_price

        self._cash = new_cash
        self._prices[sym] = ref_price  # keep latest reference

        if abs(new_qty) < 1e-9:
            self._positions.pop(sym, None)
        else:
            self._positions[sym] = BrokerPosition(
                symbol=sym,
                qty=new_qty,
                avg_entry_price=new_avg,
                market_value=new_qty * ref_price,
            )

        order_id = f"sim-{next(self._order_seq):08d}"
        order = Order(
            id=order_id,
            client_order_id=request.client_order_id,
            symbol=sym,
            qty=request.qty,
            side=request.side,
            order_type=request.order_type,
            status=OrderStatus.FILLED,
            filled_qty=request.qty,
            filled_avg_price=fill_price,
            submitted_at=_now_iso(),
            filled_at=_now_iso(),
        )
        self._orders[order_id] = order
        if request.client_order_id:
            self._client_ids[request.client_order_id] = order_id
        return order

    def cancel_order(self, order_id: str) -> None:
        # All sim orders fill immediately, so cancel is a no-op except when the
        # order id is unknown.
        if order_id not in self._orders:
            raise BrokerError(f"unknown order {order_id}")

    def get_order(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise BrokerError(f"unknown order {order_id}")
        return order

    def get_latest_quote(self, symbol: str) -> Quote:
        sym = symbol.strip().upper()
        px = self._prices.get(sym)
        if px is None:
            raise BrokerError(f"no reference price set for {sym}")
        return Quote(symbol=sym, bid=px, ask=px, last=px, timestamp=_now_iso())

    def is_market_open(self) -> bool:
        return True  # backtest: always open
