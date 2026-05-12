"""SimulatedBroker: ledger correctness for fills, shorts, idempotency."""

from __future__ import annotations

import pytest

from tradingagents.execution.broker import BrokerError
from tradingagents.execution.brokers import SimulatedBroker
from tradingagents.execution.schemas import OrderRequest, OrderSide, OrderStatus, OrderType


def _buy(broker: SimulatedBroker, sym: str, qty: float, *, cid: str | None = None):
    return broker.place_order(OrderRequest(
        symbol=sym, qty=qty, side=OrderSide.BUY, order_type=OrderType.MARKET, client_order_id=cid,
    ))


def _sell(broker: SimulatedBroker, sym: str, qty: float, *, cid: str | None = None):
    return broker.place_order(OrderRequest(
        symbol=sym, qty=qty, side=OrderSide.SELL, order_type=OrderType.MARKET, client_order_id=cid,
    ))


@pytest.mark.unit
def test_market_buy_decreases_cash_and_creates_position():
    broker = SimulatedBroker(starting_cash=10_000)
    broker.set_reference_price("AAPL", 100)
    order = _buy(broker, "AAPL", 10)
    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == 10
    assert order.filled_avg_price == 100
    pos = broker.get_position("AAPL")
    assert pos.qty == 10
    assert pos.avg_entry_price == 100
    assert broker.get_account().cash == 9_000


@pytest.mark.unit
def test_market_sell_trims_position_and_returns_cash():
    broker = SimulatedBroker(starting_cash=10_000)
    broker.set_reference_price("AAPL", 100)
    _buy(broker, "AAPL", 10)
    _sell(broker, "AAPL", 4)
    pos = broker.get_position("AAPL")
    assert pos.qty == 6
    assert pos.avg_entry_price == 100  # avg cost preserved on trim
    assert broker.get_account().cash == 9_400


@pytest.mark.unit
def test_selling_to_zero_clears_position():
    broker = SimulatedBroker(starting_cash=10_000)
    broker.set_reference_price("AAPL", 100)
    _buy(broker, "AAPL", 5)
    _sell(broker, "AAPL", 5)
    assert broker.get_position("AAPL") is None
    assert broker.get_account().cash == 10_000


@pytest.mark.unit
def test_shorting_blocked_by_default():
    broker = SimulatedBroker(starting_cash=10_000, allow_short=False)
    broker.set_reference_price("AAPL", 100)
    with pytest.raises(BrokerError, match="shorting disabled"):
        _sell(broker, "AAPL", 1)


@pytest.mark.unit
def test_shorting_works_when_enabled():
    broker = SimulatedBroker(starting_cash=10_000, allow_short=True)
    broker.set_reference_price("AAPL", 100)
    _sell(broker, "AAPL", 3)
    pos = broker.get_position("AAPL")
    assert pos.qty == -3
    assert pos.avg_entry_price == 100
    assert broker.get_account().cash == 10_300


@pytest.mark.unit
def test_duplicate_client_order_id_rejected():
    broker = SimulatedBroker(starting_cash=10_000)
    broker.set_reference_price("AAPL", 100)
    _buy(broker, "AAPL", 1, cid="ta-abc")
    with pytest.raises(BrokerError, match="duplicate client_order_id"):
        _buy(broker, "AAPL", 1, cid="ta-abc")


@pytest.mark.unit
def test_slippage_applied_to_fill_price():
    broker = SimulatedBroker(starting_cash=10_000, slippage_bps=10)  # 10 bps = 0.10%
    broker.set_reference_price("AAPL", 100)
    buy = _buy(broker, "AAPL", 1)
    assert buy.filled_avg_price == pytest.approx(100.10)  # paid more
    sell = _sell(broker, "AAPL", 1)
    assert sell.filled_avg_price == pytest.approx(99.90)  # received less


@pytest.mark.unit
def test_commission_charged_on_each_fill():
    broker = SimulatedBroker(starting_cash=10_000, commission_per_share=0.05, commission_min=1.0)
    broker.set_reference_price("AAPL", 100)
    _buy(broker, "AAPL", 10)  # commission = max(1.0, 0.05*10) = 1.0
    assert broker.get_account().cash == 8_999.0


@pytest.mark.unit
def test_insufficient_cash_rejected():
    broker = SimulatedBroker(starting_cash=100)
    broker.set_reference_price("AAPL", 100)
    with pytest.raises(BrokerError, match="insufficient cash"):
        _buy(broker, "AAPL", 5)


@pytest.mark.unit
def test_equity_tracks_cash_plus_position_value():
    broker = SimulatedBroker(starting_cash=10_000)
    broker.set_reference_price("AAPL", 100)
    _buy(broker, "AAPL", 10)
    assert broker.equity() == 10_000  # bought at 100, still worth 100
    broker.set_reference_price("AAPL", 120)
    broker.mark_to_market()
    assert broker.equity() == 10_200


@pytest.mark.unit
def test_market_order_requires_reference_price():
    broker = SimulatedBroker()
    with pytest.raises(BrokerError, match="no reference price"):
        _buy(broker, "AAPL", 1)


@pytest.mark.unit
def test_avg_cost_blends_when_adding_to_long():
    broker = SimulatedBroker(starting_cash=100_000)
    broker.set_reference_price("AAPL", 100)
    _buy(broker, "AAPL", 10)
    broker.set_reference_price("AAPL", 200)
    _buy(broker, "AAPL", 10)
    # weighted avg of (10*100 + 10*200) / 20 = 150
    assert broker.get_position("AAPL").avg_entry_price == pytest.approx(150)
