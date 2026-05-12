"""Alpaca broker adapter (paper + live).

Alpaca was chosen as the first integration because:

- It is the only major US-equity brokerage with an official, documented REST
  + websocket API maintained by the vendor.
- The paper endpoint is byte-identical to live, so the same code runs in
  both modes — only the credentials differ.
- The Python SDK (``alpaca-py``) maps cleanly onto our BrokerClient
  protocol.

This adapter does not wrap every Alpaca capability — only what the Executor
+ reconciliation loop need. Anything more exotic (bracket orders, multi-leg
options, crypto) should be added as a separate adapter rather than bolted
onto this one.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

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

logger = logging.getLogger(__name__)


# Alpaca paper endpoint. Live is https://api.alpaca.markets — never default
# to live; require an explicit ``mode=live`` to switch.
_PAPER_BASE_URL = "https://paper-api.alpaca.markets"
_LIVE_BASE_URL = "https://api.alpaca.markets"


def _alpaca_status_to_ours(status) -> OrderStatus:
    s = str(status).lower().replace("orderstatus.", "").strip()
    mapping = {
        "new": OrderStatus.PENDING,
        "accepted": OrderStatus.PENDING,
        "pending_new": OrderStatus.PENDING,
        "accepted_for_bidding": OrderStatus.PENDING,
        "held": OrderStatus.PENDING,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
        "filled": OrderStatus.FILLED,
        "done_for_day": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELED,
        "cancelled": OrderStatus.CANCELED,
        "expired": OrderStatus.EXPIRED,
        "replaced": OrderStatus.CANCELED,
        "pending_cancel": OrderStatus.PENDING,
        "pending_replace": OrderStatus.PENDING,
        "rejected": OrderStatus.REJECTED,
        "suspended": OrderStatus.PENDING,
        "calculated": OrderStatus.PENDING,
    }
    return mapping.get(s, OrderStatus.PENDING)


def _side_to_alpaca(side: OrderSide):
    from alpaca.trading.enums import OrderSide as AlpacaSide
    return AlpacaSide.BUY if side == OrderSide.BUY else AlpacaSide.SELL


def _tif_to_alpaca(tif: TimeInForce):
    from alpaca.trading.enums import TimeInForce as AlpacaTIF
    mapping = {
        TimeInForce.DAY: AlpacaTIF.DAY,
        TimeInForce.GTC: AlpacaTIF.GTC,
        TimeInForce.IOC: AlpacaTIF.IOC,
        TimeInForce.FOK: AlpacaTIF.FOK,
    }
    return mapping[tif]


class AlpacaBroker:
    """Implements :class:`BrokerClient` against alpaca-py."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        mode: ExecutionMode = ExecutionMode.PAPER,
    ) -> None:
        if mode == ExecutionMode.BACKTEST:
            raise ValueError("AlpacaBroker does not support backtest mode; use SimulatedBroker.")

        self.mode = mode
        api_key = api_key or os.environ.get("ALPACA_API_KEY")
        secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise BrokerError(
                "Alpaca credentials missing: set ALPACA_API_KEY and ALPACA_SECRET_KEY"
            )

        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient

        paper = mode == ExecutionMode.PAPER
        self._trading = TradingClient(api_key, secret_key, paper=paper)
        self._data = StockHistoricalDataClient(api_key, secret_key)
        logger.info("AlpacaBroker initialised in %s mode", mode.value)

    # ------------------------------------------------------------- accounts
    def get_account(self) -> Account:
        try:
            acct = self._trading.get_account()
        except Exception as exc:  # alpaca raises various subclasses
            raise BrokerError(f"get_account failed: {exc}") from exc
        return Account(
            equity=float(acct.equity),
            buying_power=float(acct.buying_power),
            cash=float(acct.cash),
            currency=getattr(acct, "currency", "USD") or "USD",
        )

    # ------------------------------------------------------------ positions
    def get_positions(self) -> List[BrokerPosition]:
        try:
            positions = self._trading.get_all_positions()
        except Exception as exc:
            raise BrokerError(f"get_positions failed: {exc}") from exc
        out: List[BrokerPosition] = []
        for p in positions:
            out.append(BrokerPosition(
                symbol=p.symbol,
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price) if p.avg_entry_price else None,
                market_value=float(p.market_value) if p.market_value else None,
                unrealized_pl=float(p.unrealized_pl) if p.unrealized_pl else None,
            ))
        return out

    def get_position(self, symbol: str) -> Optional[BrokerPosition]:
        sym = symbol.strip().upper()
        try:
            p = self._trading.get_open_position(sym)
        except Exception as exc:
            # Alpaca returns 404 for "no position" — treat as None instead of error.
            if "position does not exist" in str(exc).lower() or "404" in str(exc):
                return None
            raise BrokerError(f"get_position({sym}) failed: {exc}") from exc
        return BrokerPosition(
            symbol=p.symbol,
            qty=float(p.qty),
            avg_entry_price=float(p.avg_entry_price) if p.avg_entry_price else None,
            market_value=float(p.market_value) if p.market_value else None,
            unrealized_pl=float(p.unrealized_pl) if p.unrealized_pl else None,
        )

    # --------------------------------------------------------------- orders
    def place_order(self, request: OrderRequest) -> Order:
        if request.order_type != OrderType.MARKET:
            raise BrokerError(
                "Only market orders are supported by AlpacaBroker today "
                f"(got {request.order_type.value})"
            )
        from alpaca.trading.requests import MarketOrderRequest

        order_req = MarketOrderRequest(
            symbol=request.symbol,
            qty=request.qty,
            side=_side_to_alpaca(request.side),
            time_in_force=_tif_to_alpaca(request.time_in_force),
            client_order_id=request.client_order_id,
        )
        try:
            raw = self._trading.submit_order(order_req)
        except Exception as exc:
            msg = str(exc)
            # Alpaca rejects duplicate client_order_id with a 422 — surface
            # cleanly so the Executor's caller can detect it.
            if "client_order_id" in msg or "422" in msg:
                raise BrokerError(f"order rejected: {msg}") from exc
            raise BrokerError(f"submit_order failed: {msg}") from exc
        return self._to_order(raw)

    def cancel_order(self, order_id: str) -> None:
        try:
            self._trading.cancel_order_by_id(order_id)
        except Exception as exc:
            raise BrokerError(f"cancel_order({order_id}) failed: {exc}") from exc

    def get_order(self, order_id: str) -> Order:
        try:
            raw = self._trading.get_order_by_id(order_id)
        except Exception as exc:
            raise BrokerError(f"get_order({order_id}) failed: {exc}") from exc
        return self._to_order(raw)

    # --------------------------------------------------------------- quotes
    def get_latest_quote(self, symbol: str) -> Quote:
        from alpaca.data.requests import StockLatestQuoteRequest

        sym = symbol.strip().upper()
        try:
            resp = self._data.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=sym)
            )
        except Exception as exc:
            raise BrokerError(f"get_latest_quote({sym}) failed: {exc}") from exc
        q = resp.get(sym) if isinstance(resp, dict) else None
        if q is None:
            raise BrokerError(f"no quote returned for {sym}")
        bid = float(q.bid_price) if getattr(q, "bid_price", None) else None
        ask = float(q.ask_price) if getattr(q, "ask_price", None) else None
        last = None
        if bid and ask:
            last = (bid + ask) / 2.0
        elif bid:
            last = bid
        elif ask:
            last = ask
        ts = getattr(q, "timestamp", None)
        return Quote(symbol=sym, bid=bid, ask=ask, last=last, timestamp=str(ts) if ts else None)

    def is_market_open(self) -> bool:
        try:
            clock = self._trading.get_clock()
            return bool(clock.is_open)
        except Exception as exc:
            raise BrokerError(f"get_clock failed: {exc}") from exc

    # -------------------------------------------------------------- helpers
    def _to_order(self, raw) -> Order:
        side = OrderSide.BUY if str(raw.side).lower().endswith("buy") else OrderSide.SELL
        otype = OrderType.MARKET if str(raw.order_type).lower().endswith("market") else OrderType.LIMIT
        return Order(
            id=str(raw.id),
            client_order_id=raw.client_order_id,
            symbol=raw.symbol,
            qty=float(raw.qty),
            side=side,
            order_type=otype,
            limit_price=float(raw.limit_price) if getattr(raw, "limit_price", None) else None,
            status=_alpaca_status_to_ours(raw.status),
            filled_qty=float(raw.filled_qty) if raw.filled_qty else 0.0,
            filled_avg_price=float(raw.filled_avg_price) if raw.filled_avg_price else None,
            submitted_at=str(raw.submitted_at) if raw.submitted_at else None,
            filled_at=str(raw.filled_at) if raw.filled_at else None,
        )
