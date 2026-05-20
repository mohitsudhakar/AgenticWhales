"""Tests for the backtest replay loop.

Synthetic OHLCV fixtures keep the tests deterministic and offline.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Optional

import pandas as pd
import pytest

from agenticwhales.agents.schemas import PortfolioDecision, PortfolioRating
from agenticwhales.asof import LookAheadViolation, current_as_of
from agenticwhales.backtest import (
    BacktestResult,
    momentum_stub_generator,
    run_backtest,
)


def _build_history(start: dt.date, days: int, *, drift: float = 0.0,
                   start_price: float = 100.0, seed: int = 42) -> pd.DataFrame:
    """Geometric price walk with controllable drift. Skips weekends."""
    import random
    rng = random.Random(seed)
    rows = []
    price = start_price
    day = start
    while len(rows) < days:
        if day.weekday() < 5:
            ret = drift + rng.gauss(0.0, 0.01)
            open_ = price
            close = price * (1.0 + ret)
            high = max(open_, close) * (1.0 + abs(rng.gauss(0.0, 0.005)))
            low = min(open_, close) * (1.0 - abs(rng.gauss(0.0, 0.005)))
            rows.append({
                "Date": pd.Timestamp(day),
                "Open": round(open_, 2),
                "High": round(high, 2),
                "Low": round(low, 2),
                "Close": round(close, 2),
                "Volume": 1_000_000,
            })
            price = close
        day += dt.timedelta(days=1)
    df = pd.DataFrame(rows).set_index("Date")
    return df


class TestMomentumStub:
    def test_returns_none_for_short_history(self):
        df = _build_history(dt.date(2024, 1, 1), 30)
        assert momentum_stub_generator("AAPL", dt.date(2024, 2, 1), df) is None

    def test_uptrending_returns_overweight(self):
        df = _build_history(dt.date(2024, 1, 1), 80, drift=0.004)
        d = momentum_stub_generator("AAPL", dt.date(2024, 4, 15), df)
        assert d is not None
        assert d.rating == PortfolioRating.OVERWEIGHT
        assert d.expected_return_pct > 0

    def test_downtrending_returns_underweight(self):
        df = _build_history(dt.date(2024, 1, 1), 80, drift=-0.004)
        d = momentum_stub_generator("AAPL", dt.date(2024, 4, 15), df)
        assert d is not None
        assert d.rating == PortfolioRating.UNDERWEIGHT
        assert d.expected_return_pct < 0

    def test_flat_returns_hold(self):
        df = _build_history(dt.date(2024, 1, 1), 80, drift=0.0, seed=1)
        d = momentum_stub_generator("AAPL", dt.date(2024, 4, 15), df)
        assert d is not None
        assert d.rating == PortfolioRating.HOLD


class TestRunBacktest:
    def test_basic_uptrend_run(self):
        df = _build_history(dt.date(2023, 10, 1), 200, drift=0.003)
        # Slice the from-date so warmup history is available
        result = run_backtest(
            "AAPL", dt.date(2024, 1, 1), dt.date(2024, 5, 1),
            history=df, starting_cash=100_000.0,
        )
        assert isinstance(result, BacktestResult)
        assert result.symbol == "AAPL"
        assert result.total_decisions > 0
        assert len(result.equity_curve) > 0
        # Each equity-curve row has date + nav
        assert all("date" in r and "nav" in r for r in result.equity_curve)

    def test_window_validation(self):
        df = _build_history(dt.date(2024, 1, 1), 100)
        with pytest.raises(ValueError, match="to_date must be"):
            run_backtest("AAPL", dt.date(2024, 5, 1), dt.date(2024, 4, 1), history=df)

    def test_empty_window(self):
        df = _build_history(dt.date(2024, 1, 1), 10)
        with pytest.raises(ValueError, match="no trading days"):
            run_backtest("AAPL", dt.date(2025, 1, 1), dt.date(2025, 2, 1), history=df)

    def test_hold_decisions_produce_no_trades(self):
        df = _build_history(dt.date(2023, 10, 1), 200, drift=0.0, seed=7)

        def always_hold(symbol, as_of, history):
            if len(history) < 50:
                return None
            return PortfolioDecision(
                rating=PortfolioRating.HOLD,
                expected_return_pct=0.0,
                expected_volatility_pct=15.0,
                prob_of_profit=0.5,
                expected_hold_days=10,
                executive_summary="Hold — fixture-only.",
                investment_thesis="Hold — fixture-only.",
            )

        result = run_backtest(
            "AAPL", dt.date(2024, 1, 1), dt.date(2024, 3, 1),
            history=df, decision_fn=always_hold,
        )
        assert result.closed_trades == 0
        assert result.final_nav == result.starting_cash

    def test_stop_loss_triggers_exit(self):
        # Build a history that gaps down so the stop fires.
        df = _build_history(dt.date(2023, 10, 1), 80, drift=0.003)
        # Inject a crash on a known date that will produce a low under any stop
        crash_idx = df.index[60]
        df.loc[crash_idx, "Low"] = df.loc[crash_idx, "Close"] * 0.5
        df.loc[crash_idx, "Close"] = df.loc[crash_idx, "Close"] * 0.5

        result = run_backtest(
            "AAPL", df.index[55].date(), df.index[75].date(),
            history=df,
        )
        # At least one trade should have closed with reason='stop'.
        assert any(t["reason"] == "stop" for t in result.trades), result.trades

    def test_brier_in_unit_interval(self):
        df = _build_history(dt.date(2023, 10, 1), 200, drift=0.003)
        result = run_backtest(
            "AAPL", dt.date(2024, 1, 1), dt.date(2024, 5, 1),
            history=df,
        )
        assert 0.0 <= result.brier <= 1.0
        assert 0.0 <= result.hit_rate <= 1.0


class TestAsOfEnforcement:
    def test_decision_fn_sees_as_of_set(self):
        df = _build_history(dt.date(2023, 10, 1), 100, drift=0.002)
        seen_dates = []

        def spy(symbol, as_of, history):
            seen_dates.append((as_of, current_as_of()))
            return None

        run_backtest(
            "AAPL", dt.date(2024, 1, 1), dt.date(2024, 1, 31),
            history=df, decision_fn=spy,
        )
        # Inside the decision callback, current_as_of() should match the
        # backtest day, never None.
        assert seen_dates
        for as_of, bound in seen_dates:
            assert bound == as_of
        # After the call returns, the binding is unset.
        assert current_as_of() is None

    def test_decision_fn_history_never_past_as_of(self):
        df = _build_history(dt.date(2023, 10, 1), 200, drift=0.002)

        def strict_check(symbol, as_of, history):
            assert history.index.max().date() <= as_of, \
                f"history leaked past as_of={as_of}"
            return None

        run_backtest(
            "AAPL", dt.date(2024, 1, 1), dt.date(2024, 3, 1),
            history=df, decision_fn=strict_check,
        )
