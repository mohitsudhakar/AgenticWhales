"""AlpacaBroker adapter: SDK call mapping (with the SDK mocked).

We never hit the real network in unit tests — the alpaca-py SDK is mocked
and we verify our adapter:
- Sends the right shape into ``submit_order``.
- Translates SDK enums/status back into our protocol's types.
- Treats Alpaca's "position does not exist" as ``None`` (not an error).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.execution.broker import BrokerError
from tradingagents.execution.schemas import (
    ExecutionMode,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)


def _patch_sdk():
    """Return contexts that swap alpaca's TradingClient + data client for mocks."""
    return (
        patch("alpaca.trading.client.TradingClient"),
        patch("alpaca.data.historical.StockHistoricalDataClient"),
    )


@pytest.fixture
def alpaca_broker(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    with patch("alpaca.trading.client.TradingClient") as TC, \
         patch("alpaca.data.historical.StockHistoricalDataClient") as DC:
        from tradingagents.execution.brokers.alpaca import AlpacaBroker

        broker = AlpacaBroker(mode=ExecutionMode.PAPER)
        # Swap the auto-created instances with simple Mocks we control.
        broker._trading = MagicMock()
        broker._data = MagicMock()
        yield broker


@pytest.mark.unit
def test_init_requires_credentials(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    from tradingagents.execution.brokers.alpaca import AlpacaBroker
    with pytest.raises(BrokerError, match="credentials missing"):
        AlpacaBroker(mode=ExecutionMode.PAPER)


@pytest.mark.unit
def test_init_rejects_backtest_mode(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    from tradingagents.execution.brokers.alpaca import AlpacaBroker
    with pytest.raises(ValueError, match="does not support backtest"):
        AlpacaBroker(mode=ExecutionMode.BACKTEST)


@pytest.mark.unit
def test_get_account_maps_fields(alpaca_broker):
    alpaca_broker._trading.get_account.return_value = SimpleNamespace(
        equity="50000.00", buying_power="100000.00", cash="50000.00", currency="USD",
    )
    acct = alpaca_broker.get_account()
    assert acct.equity == 50_000.0
    assert acct.buying_power == 100_000.0
    assert acct.cash == 50_000.0
    assert acct.currency == "USD"


@pytest.mark.unit
def test_get_position_returns_none_when_alpaca_says_no_position(alpaca_broker):
    alpaca_broker._trading.get_open_position.side_effect = Exception("position does not exist for AAPL")
    assert alpaca_broker.get_position("AAPL") is None


@pytest.mark.unit
def test_get_position_propagates_other_errors(alpaca_broker):
    alpaca_broker._trading.get_open_position.side_effect = Exception("rate limited")
    with pytest.raises(BrokerError, match="get_position"):
        alpaca_broker.get_position("AAPL")


@pytest.mark.unit
def test_place_order_translates_request_and_response(alpaca_broker):
    raw = SimpleNamespace(
        id="alp-001",
        client_order_id="ta-abc",
        symbol="AAPL",
        qty="10",
        side="OrderSide.BUY",
        order_type="OrderType.MARKET",
        limit_price=None,
        status="filled",
        filled_qty="10",
        filled_avg_price="150.25",
        submitted_at="2026-05-12T13:00:00Z",
        filled_at="2026-05-12T13:00:01Z",
    )
    alpaca_broker._trading.submit_order.return_value = raw

    order = alpaca_broker.place_order(OrderRequest(
        symbol="AAPL", qty=10, side=OrderSide.BUY,
        order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY,
        client_order_id="ta-abc",
    ))

    assert order.id == "alp-001"
    assert order.client_order_id == "ta-abc"
    assert order.symbol == "AAPL"
    assert order.qty == 10
    assert order.side == OrderSide.BUY
    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == 10
    assert order.filled_avg_price == 150.25
    alpaca_broker._trading.submit_order.assert_called_once()


@pytest.mark.unit
def test_place_order_rejects_limit_orders(alpaca_broker):
    with pytest.raises(BrokerError, match="Only market orders"):
        alpaca_broker.place_order(OrderRequest(
            symbol="AAPL", qty=1, side=OrderSide.BUY,
            order_type=OrderType.LIMIT, limit_price=100,
        ))


@pytest.mark.unit
def test_place_order_surfaces_duplicate_client_order_id(alpaca_broker):
    alpaca_broker._trading.submit_order.side_effect = Exception(
        "422: client_order_id must be unique"
    )
    with pytest.raises(BrokerError, match="order rejected"):
        alpaca_broker.place_order(OrderRequest(
            symbol="AAPL", qty=1, side=OrderSide.BUY,
            order_type=OrderType.MARKET, client_order_id="dup",
        ))


@pytest.mark.unit
def test_is_market_open_reads_clock(alpaca_broker):
    alpaca_broker._trading.get_clock.return_value = SimpleNamespace(is_open=True)
    assert alpaca_broker.is_market_open() is True
    alpaca_broker._trading.get_clock.return_value = SimpleNamespace(is_open=False)
    assert alpaca_broker.is_market_open() is False
