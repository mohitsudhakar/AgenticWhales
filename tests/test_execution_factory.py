"""build_broker(): env-driven broker selection."""

from __future__ import annotations

import pytest

from tradingagents.execution.broker import BrokerError
from tradingagents.execution.brokers import SimulatedBroker
from tradingagents.execution.factory import build_broker
from tradingagents.execution.schemas import ExecutionMode


@pytest.mark.unit
def test_backtest_mode_returns_simulated_broker():
    broker = build_broker("backtest")
    assert isinstance(broker, SimulatedBroker)
    assert broker.mode == ExecutionMode.BACKTEST


@pytest.mark.unit
def test_unknown_mode_raises():
    with pytest.raises(BrokerError, match="unknown BROKERAGE_MODE"):
        build_broker("hyperdrive")


@pytest.mark.unit
def test_paper_mode_requires_credentials(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(BrokerError, match="credentials missing"):
        build_broker("paper")


@pytest.mark.unit
def test_live_mode_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.delenv("BROKERAGE_ALLOW_LIVE", raising=False)
    with pytest.raises(BrokerError, match="BROKERAGE_ALLOW_LIVE=1"):
        build_broker("live")


@pytest.mark.unit
def test_default_mode_is_paper(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.delenv("BROKERAGE_MODE", raising=False)
    with pytest.raises(BrokerError, match="credentials missing"):
        # default is paper, which needs creds → confirms default path
        build_broker()
