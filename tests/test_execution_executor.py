"""Executor end-to-end against the SimulatedBroker."""

from __future__ import annotations

import pytest

from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating
from tradingagents.execution.broker import BrokerError
from tradingagents.execution.brokers import SimulatedBroker
from tradingagents.execution.executor import Executor
from tradingagents.execution.schemas import OrderSide
from tradingagents.execution.sizing import SizingPolicy


def _decision(rating: PortfolioRating) -> PortfolioDecision:
    return PortfolioDecision(
        rating=rating,
        executive_summary="test plan",
        investment_thesis="test thesis",
    )


@pytest.mark.unit
def test_buy_from_flat_opens_long_position():
    broker = SimulatedBroker(starting_cash=10_000)
    broker.set_reference_price("AAPL", 100)
    executor = Executor(broker)
    result = executor.execute("AAPL", _decision(PortfolioRating.BUY), trade_date="2026-05-12")
    # 10% of $10,000 = $1,000 / $100 = 10 shares
    assert result.action == "BUY"
    assert result.target_qty == 10
    assert result.delta_qty == 10
    assert result.order is not None
    assert result.order.side == OrderSide.BUY
    assert broker.get_position("AAPL").qty == 10


@pytest.mark.unit
def test_hold_does_not_place_any_order():
    broker = SimulatedBroker(starting_cash=10_000)
    broker.set_reference_price("AAPL", 100)
    executor = Executor(broker)
    result = executor.execute("AAPL", _decision(PortfolioRating.HOLD))
    assert result.action == "HOLD"
    assert result.order is None
    assert broker.get_position("AAPL") is None


@pytest.mark.unit
def test_sell_with_short_disabled_exits_to_flat():
    broker = SimulatedBroker(starting_cash=10_000)
    broker.set_reference_price("AAPL", 100)
    executor = Executor(broker, SizingPolicy(allow_short=False))
    # First open a long position via BUY decision
    executor.execute("AAPL", _decision(PortfolioRating.BUY), trade_date="d1")
    # Now SELL: target weight 0 (short disabled), so exit fully
    result = executor.execute("AAPL", _decision(PortfolioRating.SELL), trade_date="d2")
    assert result.action == "SELL"
    assert result.target_qty == 0
    assert broker.get_position("AAPL") is None


@pytest.mark.unit
def test_sell_with_short_enabled_opens_short():
    broker = SimulatedBroker(starting_cash=10_000, allow_short=True)
    broker.set_reference_price("AAPL", 100)
    executor = Executor(broker, SizingPolicy(allow_short=True))
    result = executor.execute("AAPL", _decision(PortfolioRating.SELL), trade_date="d1")
    # -5% of $10,000 = -$500 / $100 = -5 shares
    assert result.action == "SELL"
    assert result.target_qty == -5
    assert broker.get_position("AAPL").qty == -5


@pytest.mark.unit
def test_overweight_from_existing_buy_position_does_not_double_up():
    """Rating goes BUY -> OVERWEIGHT: target shrinks from 10% to 5%, so we trim."""
    broker = SimulatedBroker(starting_cash=10_000)
    broker.set_reference_price("AAPL", 100)
    executor = Executor(broker)
    executor.execute("AAPL", _decision(PortfolioRating.BUY), trade_date="d1")  # qty=10
    result = executor.execute("AAPL", _decision(PortfolioRating.OVERWEIGHT), trade_date="d2")
    # 5% of equity (still ~$10,000) at $100 = 5 shares; delta = 5 - 10 = -5
    assert result.action == "SELL"
    assert result.target_qty == 5
    assert result.delta_qty == -5
    assert broker.get_position("AAPL").qty == 5


@pytest.mark.unit
def test_re_running_same_decision_is_idempotent():
    """Two layers of protection: (1) reading broker state shows delta=0 → SKIP;
    (2) if a race did slip through, the broker would reject the duplicate
    client_order_id (covered in test_duplicate_client_order_id_rejected).
    """
    broker = SimulatedBroker(starting_cash=10_000)
    broker.set_reference_price("AAPL", 100)
    executor = Executor(broker)
    r1 = executor.execute("AAPL", _decision(PortfolioRating.BUY), trade_date="2026-05-12")
    assert r1.action == "BUY"
    r2 = executor.execute("AAPL", _decision(PortfolioRating.BUY), trade_date="2026-05-12")
    assert r2.action == "SKIP"
    assert r2.delta_qty == 0
    # Position unchanged from first run
    assert broker.get_position("AAPL").qty == 10


@pytest.mark.unit
def test_skip_when_delta_value_below_min_order_value():
    broker = SimulatedBroker(starting_cash=10_000)
    broker.set_reference_price("AAPL", 100)
    # Give it 10 shares, then ask for 10% target which is also 10 shares → delta 0
    executor = Executor(broker, SizingPolicy(min_order_value=1_000_000))
    executor.execute("AAPL", _decision(PortfolioRating.BUY), trade_date="d1")
    result = executor.execute("AAPL", _decision(PortfolioRating.OVERWEIGHT), trade_date="d2")
    # Even though delta is non-zero, min_order_value is huge → SKIP
    assert result.action == "SKIP"


@pytest.mark.unit
def test_dry_run_does_not_place_order_but_reports_intent():
    broker = SimulatedBroker(starting_cash=10_000)
    broker.set_reference_price("AAPL", 100)
    executor = Executor(broker)
    result = executor.execute(
        "AAPL", _decision(PortfolioRating.BUY), trade_date="d1", dry_run=True,
    )
    assert result.action == "DRY_RUN"
    assert result.target_qty == 10
    assert result.delta_qty == 10
    assert result.order is None
    assert broker.get_position("AAPL") is None  # nothing placed


@pytest.mark.unit
def test_executor_uses_reference_price_when_passed():
    """The backtest harness passes the bar's open price; executor should use it."""
    broker = SimulatedBroker(starting_cash=10_000)
    broker.set_reference_price("AAPL", 50)  # broker has stale price
    executor = Executor(broker)
    result = executor.execute(
        "AAPL", _decision(PortfolioRating.BUY), trade_date="d1",
        reference_price=100,  # but harness says 100
    )
    # sizing uses 100, fill happens at broker's 50 (sim broker is in control)
    assert result.reference_price == 100
    assert result.target_qty == 10
