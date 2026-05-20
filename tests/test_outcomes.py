"""Tests for the decision-outcome resolver."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agenticwhales import outcomes as outcomes_mod
from agenticwhales.agents.schemas import (
    GuardOutcome,
    ImpersonationToken,
    OrderSide,
    PortfolioDecision,
    PortfolioRating,
)
from agenticwhales import paper
from web import auth


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


def _token():
    return ImpersonationToken(
        user_id="u-1", issued_at=time.time(),
        purpose="scheduler_fire", fire_id="fire-1",
    )


def _decision(rating=PortfolioRating.BUY, *, er=10.0, p=0.65, hold=30):
    return PortfolioDecision(
        rating=rating, executive_summary="x", investment_thesis="x",
        expected_return_pct=er, expected_volatility_pct=20.0,
        prob_of_profit=p, expected_hold_days=hold,
    )


def _place_buy(fill_price=100.0, fire_id="fire-1"):
    return paper.place_order(
        _token(), fire_id=fire_id, session_id="s-1", recipe_id="r-1",
        ticker="AAPL", side=OrderSide.BUY, qty=10.0, market_price=fill_price,
        slippage_bps=0, decision=_decision(), conviction=7,
        kelly_fraction=0.02,
        guard=GuardOutcome(allowed=True, allowed_qty=10.0),
    )


def _backdate_order(order_id: str, created_days_ago: int) -> None:
    """Rewrite a paper_order's `created_at` so the outcome resolver sees it as due."""
    row = auth._memstore[("paper_orders", order_id)]
    row["created_at"] = (
        datetime.now(tz=timezone.utc) - timedelta(days=created_days_ago)
    ).isoformat()


def _snapshot(price: float) -> str:
    return f"Latest close for AAPL: ${price:.2f}"


class TestResolve:
    def test_winning_long_records_positive_return_and_hit(self):
        result = _place_buy(fill_price=100.0)
        _backdate_order(result.order_id, 31)
        with patch.object(outcomes_mod, "fetch_snapshot_block",
                          return_value=_snapshot(110.0)):
            written = outcomes_mod.resolve_outcomes_for_user("u-1")
        assert len(written) == 1
        row = written[0]
        assert row.hit is True
        assert row.realized_return_pct == pytest.approx(10.0, rel=1e-3)
        # Brier component: (predicted 0.65 - actual 1.0)^2 = 0.1225
        assert row.brier_component == pytest.approx(0.1225, rel=1e-3)

    def test_losing_long_records_negative_return_and_miss(self):
        result = _place_buy(fill_price=100.0)
        _backdate_order(result.order_id, 31)
        with patch.object(outcomes_mod, "fetch_snapshot_block",
                          return_value=_snapshot(90.0)):
            written = outcomes_mod.resolve_outcomes_for_user("u-1")
        assert written[0].hit is False
        assert written[0].realized_return_pct == pytest.approx(-10.0, rel=1e-3)

    def test_pre_hold_orders_skipped(self):
        result = _place_buy(fill_price=100.0)
        # Created just now — predicted_hold_days=30. Not due.
        with patch.object(outcomes_mod, "fetch_snapshot_block",
                          return_value=_snapshot(110.0)):
            written = outcomes_mod.resolve_outcomes_for_user("u-1")
        assert written == []

    def test_idempotent_on_rerun(self):
        result = _place_buy(fill_price=100.0)
        _backdate_order(result.order_id, 31)
        with patch.object(outcomes_mod, "fetch_snapshot_block",
                          return_value=_snapshot(110.0)):
            first = outcomes_mod.resolve_outcomes_for_user("u-1")
            second = outcomes_mod.resolve_outcomes_for_user("u-1")
        assert len(first) == 1
        assert len(second) == 0

    def test_brier_score_aggregate(self):
        # Two trades, one hit one miss; each predicted p=0.65.
        for fire_id, fp, final in (("f-1", 100.0, 110.0), ("f-2", 100.0, 90.0)):
            r = paper.place_order(
                _token(), fire_id=fire_id, session_id="s-1", recipe_id="r-1",
                ticker="AAPL", side=OrderSide.BUY, qty=10.0, market_price=fp,
                slippage_bps=0, decision=_decision(), conviction=7,
                kelly_fraction=0.02,
                guard=GuardOutcome(allowed=True, allowed_qty=10.0),
            )
            _backdate_order(r.order_id, 31)
            with patch.object(outcomes_mod, "fetch_snapshot_block",
                              return_value=_snapshot(final)):
                outcomes_mod.resolve_outcomes_for_user("u-1")

        score = outcomes_mod.brier_score("u-1")
        # (0.65-1)^2 + (0.65-0)^2 = 0.1225 + 0.4225 = 0.545 → mean 0.2725
        assert score == pytest.approx(0.2725, rel=1e-3)

    def test_short_realized_return_sign_flipped(self):
        # Open a short by placing a SHORT order directly.
        auth.upsert_risk_limits("u-1", allow_shorts=True)
        paper.place_order(
            _token(), fire_id="fire-short", session_id="s-1", recipe_id="r-1",
            ticker="AAPL", side=OrderSide.SHORT, qty=5.0, market_price=100.0,
            slippage_bps=0,
            decision=_decision(rating=PortfolioRating.SELL, er=-10.0),
            conviction=7, kelly_fraction=0.02,
            guard=GuardOutcome(allowed=True, allowed_qty=5.0),
        )
        # Find the order id we just inserted. (Memstore key tuple is
        # `(table_name, row_id)`; we want the row_id half.)
        order_id = [
            key[1]
            for key, v in auth._memstore.items()
            if key[0] == "paper_orders" and v["fire_id"] == "fire-short"
        ][0]
        _backdate_order(order_id, 31)
        # Price dropped — short profits → realized_return_pct > 0 and hit=True.
        with patch.object(outcomes_mod, "fetch_snapshot_block",
                          return_value=_snapshot(90.0)):
            written = outcomes_mod.resolve_outcomes_for_user("u-1")
        assert written[0].hit is True
        assert written[0].realized_return_pct == pytest.approx(10.0, rel=1e-3)
