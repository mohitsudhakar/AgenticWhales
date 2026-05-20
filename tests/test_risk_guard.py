"""Tests for the pre-trade RiskGuard."""

from __future__ import annotations

import pytest

from agenticwhales.agents.schemas import (
    PaperAccount,
    PaperPosition,
    PortfolioDecision,
    PortfolioRating,
)
from agenticwhales.risk import RiskGuard, RiskLimits


def _decision(rating: PortfolioRating = PortfolioRating.BUY) -> PortfolioDecision:
    return PortfolioDecision(
        rating=rating,
        executive_summary="x",
        investment_thesis="x",
        expected_return_pct=10.0,
        expected_volatility_pct=20.0,
        prob_of_profit=0.6,
        expected_hold_days=30,
    )


def _account(cash: float = 100_000.0, nav_open: float | None = None) -> PaperAccount:
    return PaperAccount(
        user_id="u-1",
        starting_cash=cash,
        cash=cash,
        nav_open_today=nav_open,
        nav_open_today_date="2026-05-18" if nav_open else None,
    )


class TestKillSwitch:
    def test_global_kill_blocks(self):
        guard = RiskGuard(
            user_id="u-1",
            limits=RiskLimits(global_kill_switch=True),
            account=_account(),
            positions=[],
        )
        out = guard.evaluate(_decision(), "AAPL", 10.0, 200.0)
        assert out.allowed is False
        assert out.allowed_qty == 0.0
        assert out.rule == "kill_switch"

    def test_recipe_kill_blocks(self):
        guard = RiskGuard(
            user_id="u-1",
            limits=RiskLimits(),
            account=_account(),
            positions=[],
            recipe_killed=True,
        )
        out = guard.evaluate(_decision(), "AAPL", 10.0, 200.0)
        assert out.allowed is False
        assert out.rule == "kill_switch"


class TestDailyDrawdown:
    def test_under_cap_passes(self):
        # NAV today = cash 100_000 (no positions); nav_open_today = 101_000.
        # Drawdown = -0.99% — under the 3% default cap.
        guard = RiskGuard(
            user_id="u-1",
            limits=RiskLimits(max_daily_drawdown_pct=0.03),
            account=_account(cash=100_000, nav_open=101_000),
            positions=[],
        )
        out = guard.evaluate(_decision(), "AAPL", 1.0, 100.0)
        assert out.allowed is True

    def test_at_or_below_cap_blocks(self):
        # nav_open = 110_000, current = 100_000 → -9.09% drawdown.
        guard = RiskGuard(
            user_id="u-1",
            limits=RiskLimits(max_daily_drawdown_pct=0.03),
            account=_account(cash=100_000, nav_open=110_000),
            positions=[],
        )
        out = guard.evaluate(_decision(), "AAPL", 1.0, 100.0)
        assert out.allowed is False
        assert out.rule == "daily_drawdown"

    def test_no_nav_open_skips_check(self):
        # Brand-new day, no baseline → don't false-positive.
        guard = RiskGuard(
            user_id="u-1",
            limits=RiskLimits(),
            account=_account(),
            positions=[],
        )
        out = guard.evaluate(_decision(), "AAPL", 1.0, 100.0)
        assert out.allowed is True


class TestPositionCap:
    def test_within_cap_passes(self):
        # NAV ≈ 100_000; 5% cap = $5000; 10 shares × $100 = $1000 → fine.
        guard = RiskGuard(
            user_id="u-1",
            limits=RiskLimits(max_position_pct=0.05),
            account=_account(cash=100_000),
            positions=[],
        )
        out = guard.evaluate(_decision(), "AAPL", 10.0, 100.0)
        assert out.allowed is True
        assert out.allowed_qty == 10.0

    def test_clamps_over_cap(self):
        # NAV = 100_000; 5% cap = $5000; target = 100 × $100 = $10_000 → clamp to 50 shares.
        guard = RiskGuard(
            user_id="u-1",
            limits=RiskLimits(max_position_pct=0.05),
            account=_account(cash=100_000),
            positions=[],
        )
        out = guard.evaluate(_decision(), "AAPL", 100.0, 100.0)
        assert out.allowed is True
        assert out.allowed_qty == pytest.approx(50.0)
        assert out.blocked_qty == pytest.approx(50.0)
        assert out.rule == "max_position"

    def test_existing_position_eats_into_cap(self):
        # 5% cap = $5000; already hold 30 × $100 = $3000. Room = $2000 → 20 shares.
        positions = [PaperPosition(user_id="u-1", ticker="AAPL", qty=30, avg_cost=100.0)]
        guard = RiskGuard(
            user_id="u-1",
            limits=RiskLimits(max_position_pct=0.05),
            account=_account(cash=100_000),
            positions=positions,
        )
        out = guard.evaluate(_decision(), "AAPL", 100.0, 100.0)
        assert out.allowed is True
        assert out.allowed_qty == pytest.approx(20.0)

    def test_existing_at_cap_blocks(self):
        # Cap dollars = $5000; existing = $5000 → no room → block.
        positions = [PaperPosition(user_id="u-1", ticker="AAPL", qty=50, avg_cost=100.0)]
        guard = RiskGuard(
            user_id="u-1",
            limits=RiskLimits(max_position_pct=0.05),
            account=_account(cash=100_000),
            positions=positions,
        )
        out = guard.evaluate(_decision(), "AAPL", 100.0, 100.0)
        assert out.allowed is False
        assert out.allowed_qty == 0.0
        assert out.rule == "max_position"

    def test_zero_target_qty_trivially_passes(self):
        guard = RiskGuard(
            user_id="u-1", limits=RiskLimits(), account=_account(), positions=[],
        )
        out = guard.evaluate(_decision(PortfolioRating.HOLD), "AAPL", 0.0, 100.0)
        assert out.allowed is True
        assert out.allowed_qty == 0.0
