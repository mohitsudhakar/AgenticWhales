"""Backtest harness: deterministic price series + fixed-rating decisions."""

from __future__ import annotations

import pytest

from tradingagents.agents.schemas import PortfolioRating
from tradingagents.backtest.bars import synthetic_bars
from tradingagents.backtest.decision_source import (
    FixedRatingDecisionSource,
    ReplayDecisionSource,
)
from tradingagents.backtest.harness import BacktestHarness
from tradingagents.execution.sizing import SizingPolicy


@pytest.mark.unit
def test_hold_only_strategy_leaves_cash_untouched():
    bars = {"AAPL": synthetic_bars([100, 101, 102, 103, 104])}
    harness = BacktestHarness(
        bars=bars,
        decision_source=FixedRatingDecisionSource(PortfolioRating.HOLD),
        starting_cash=10_000,
    )
    result = harness.run()
    # No trades, equity stays at 10,000 the whole time
    assert len(result.equity_curve) == 5
    assert all(p.equity == 10_000 for p in result.equity_curve)
    assert sum(1 for t in result.trades if t.order) == 0
    assert result.metrics["total_return"] == 0.0


@pytest.mark.unit
def test_buy_rating_opens_position_and_tracks_price_up():
    # Price climbs from 100 to 120 — a long position should profit
    prices = [100, 100, 100, 110, 120]
    bars = {"AAPL": synthetic_bars(prices)}
    harness = BacktestHarness(
        bars=bars,
        decision_source=FixedRatingDecisionSource(PortfolioRating.BUY),
        sizing=SizingPolicy(target_weights={PortfolioRating.BUY: 0.10}),
        starting_cash=10_000,
    )
    result = harness.run()
    # First bar: no rebalance (i==0). Subsequent bars: BUY decision targets 10% = ~10 shares.
    # Once filled, position is worth more as price rises.
    assert result.metrics["final_equity"] > 10_000
    # At least one BUY trade must have been placed
    placed = [t for t in result.trades if t.order is not None]
    assert any(t.action == "BUY" for t in placed)


@pytest.mark.unit
def test_buy_then_hold_does_not_keep_buying():
    """Once we hit target weight, subsequent BUY decisions are no-ops (SKIP)."""
    bars = {"AAPL": synthetic_bars([100] * 10)}  # flat price
    harness = BacktestHarness(
        bars=bars,
        decision_source=FixedRatingDecisionSource(PortfolioRating.BUY),
        starting_cash=10_000,
    )
    result = harness.run()
    # One real BUY, the rest SKIP because we're already at target
    real_buys = [t for t in result.trades if t.action == "BUY" and t.order is not None]
    skips = [t for t in result.trades if t.action == "SKIP"]
    assert len(real_buys) == 1
    assert len(skips) >= 1


@pytest.mark.unit
def test_replay_decision_source_drives_dated_trades():
    bars = {"AAPL": synthetic_bars([100, 100, 100, 100, 100], start="2025-01-06")}
    # 2025-01-06 is Monday; bars are business days
    timeline = list(bars["AAPL"].index)
    decisions = {
        (timeline[1].date().isoformat(), "AAPL"): PortfolioRating.BUY,
        (timeline[3].date().isoformat(), "AAPL"): PortfolioRating.SELL,
    }
    harness = BacktestHarness(
        bars=bars,
        decision_source=ReplayDecisionSource(decisions),
        starting_cash=10_000,
    )
    result = harness.run()
    actions = [t.action for t in result.trades if t.order is not None]
    assert actions == ["BUY", "SELL"]


@pytest.mark.unit
def test_metrics_computed_for_winning_curve():
    bars = {"AAPL": synthetic_bars([100, 110, 120, 130, 140])}
    harness = BacktestHarness(
        bars=bars,
        decision_source=FixedRatingDecisionSource(PortfolioRating.BUY),
        starting_cash=10_000,
    )
    result = harness.run()
    assert result.metrics["total_return"] > 0
    assert result.metrics["max_drawdown"] <= 0  # always non-positive
    assert "sharpe" in result.metrics
    assert "cagr" in result.metrics


@pytest.mark.unit
def test_slippage_costs_money():
    bars = {"AAPL": synthetic_bars([100] * 6)}
    no_slip = BacktestHarness(
        bars=bars,
        decision_source=FixedRatingDecisionSource(PortfolioRating.BUY),
        starting_cash=10_000,
        slippage_bps=0,
    ).run()
    with_slip = BacktestHarness(
        bars=bars,
        decision_source=FixedRatingDecisionSource(PortfolioRating.BUY),
        starting_cash=10_000,
        slippage_bps=50,  # 50 bps round-trip eats into PnL
    ).run()
    assert with_slip.metrics["final_equity"] < no_slip.metrics["final_equity"]
