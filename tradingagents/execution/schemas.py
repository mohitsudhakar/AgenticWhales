"""Brokerage-agnostic data types used across the execution layer.

Every broker adapter (SimulatedBroker, AlpacaBroker, …) emits and consumes
these same shapes, so the Executor and the backtest harness don't need to
know which broker they're talking to.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ExecutionMode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class OrderStatus(str, Enum):
    PENDING = "pending"          # accepted by broker, not yet filled
    FILLED = "filled"            # fully filled
    PARTIALLY_FILLED = "partially_filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class Account(BaseModel):
    equity: float
    buying_power: float
    cash: float
    currency: str = "USD"


class BrokerPosition(BaseModel):
    symbol: str
    qty: float
    avg_entry_price: Optional[float] = None
    market_value: Optional[float] = None
    unrealized_pl: Optional[float] = None

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class Quote(BaseModel):
    symbol: str
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    timestamp: Optional[str] = None


class OrderRequest(BaseModel):
    symbol: str
    qty: float = Field(gt=0, description="Always positive; direction is in `side`.")
    side: OrderSide
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    time_in_force: TimeInForce = TimeInForce.DAY
    client_order_id: Optional[str] = Field(
        default=None,
        description=(
            "Idempotency key. Brokers reject a second order with the same key, "
            "so re-running an executor cannot double-fire."
        ),
    )

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class Order(BaseModel):
    id: str
    client_order_id: Optional[str] = None
    symbol: str
    qty: float
    side: OrderSide
    order_type: OrderType
    limit_price: Optional[float] = None
    status: OrderStatus
    filled_qty: float = 0.0
    filled_avg_price: Optional[float] = None
    submitted_at: Optional[str] = None
    filled_at: Optional[str] = None

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class ExecutionResult(BaseModel):
    """Outcome of one Executor.execute() call.

    Reported regardless of mode (backtest/paper/live) so that the calling
    layer can log, persist, and surface the same shape.
    """

    ticker: str
    rating: str                            # PortfolioRating.value
    action: str                            # "BUY" | "SELL" | "HOLD" | "SKIP" | "DRY_RUN" | "ERROR"
    prev_qty: float = 0.0
    target_qty: float = 0.0
    delta_qty: float = 0.0
    reference_price: Optional[float] = None
    target_weight: Optional[float] = None
    order: Optional[Order] = None
    reason: Optional[str] = None           # populated for HOLD / SKIP / ERROR
    mode: ExecutionMode = ExecutionMode.BACKTEST
