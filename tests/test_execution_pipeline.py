"""LivePipeline: graph → executor → mirror end-to-end (using SimulatedBroker)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tradingagents.agents.schemas import PortfolioRating, render_pm_decision, PortfolioDecision
from tradingagents.execution.brokers import SimulatedBroker
from tradingagents.execution.executor import Executor
from tradingagents.execution.pipeline import LivePipeline


def _fake_graph(rating: PortfolioRating, summary: str = "test"):
    """Build a graph stub that returns a canned PM decision."""
    decision_md = render_pm_decision(PortfolioDecision(
        rating=rating,
        executive_summary=summary,
        investment_thesis=summary,
    ))
    graph = MagicMock()
    graph.propagate.return_value = (
        {"final_trade_decision": decision_md},
        rating.value,
    )
    return graph


@pytest.mark.unit
def test_pipeline_runs_graph_and_executes_trade(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # isolate portfolio.json

    broker = SimulatedBroker(starting_cash=10_000)
    broker.set_reference_price("AAPL", 100)
    graph = _fake_graph(PortfolioRating.BUY)
    pipeline = LivePipeline(graph, broker, Executor(broker))

    final_state, result = pipeline.run("AAPL", "2026-05-12")

    graph.propagate.assert_called_once_with("AAPL", "2026-05-12")
    assert result.action == "BUY"
    assert result.order is not None
    assert broker.get_position("AAPL").qty == 10


@pytest.mark.unit
def test_pipeline_skips_mirror_on_hold(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    broker = SimulatedBroker(starting_cash=10_000)
    broker.set_reference_price("AAPL", 100)
    graph = _fake_graph(PortfolioRating.HOLD)
    pipeline = LivePipeline(graph, broker, Executor(broker))

    _state, result = pipeline.run("AAPL", "2026-05-12")
    assert result.action == "HOLD"
    assert result.order is None
    assert broker.get_position("AAPL") is None


@pytest.mark.unit
def test_pipeline_dry_run_does_not_place_orders(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    broker = SimulatedBroker(starting_cash=10_000)
    broker.set_reference_price("AAPL", 100)
    graph = _fake_graph(PortfolioRating.BUY)
    pipeline = LivePipeline(graph, broker, Executor(broker), dry_run=True)

    _state, result = pipeline.run("AAPL", "2026-05-12")
    assert result.action == "DRY_RUN"
    assert result.target_qty == 10
    assert broker.get_position("AAPL") is None
