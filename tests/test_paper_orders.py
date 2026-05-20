"""Tests for paper.place_order — buy/sell/short/cover + idempotency + clamp/block."""

from __future__ import annotations

import time

import pytest

from agenticwhales.agents.schemas import (
    GuardOutcome,
    ImpersonationToken,
    OrderSide,
    OrderStatus,
    PortfolioDecision,
    PortfolioRating,
)
from agenticwhales import paper
from web import auth


@pytest.fixture(autouse=True)
def _wipe_memstore():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


def _token(user_id: str = "u-1") -> ImpersonationToken:
    return ImpersonationToken(
        user_id=user_id, issued_at=time.time(),
        purpose="scheduler_fire", fire_id="fire-1",
    )


def _decision() -> PortfolioDecision:
    return PortfolioDecision(
        rating=PortfolioRating.BUY,
        executive_summary="x", investment_thesis="x",
        expected_return_pct=10.0, expected_volatility_pct=20.0,
        prob_of_profit=0.6, expected_hold_days=30,
    )


class TestBuyFlow:
    def test_buy_opens_long_position(self):
        result = paper.place_order(
            _token(), fire_id="fire-1", session_id="s1", recipe_id=None,
            ticker="AAPL", side=OrderSide.BUY, qty=10.0, market_price=100.0,
            slippage_bps=0, decision=_decision(), conviction=7, kelly_fraction=0.02,
            guard=GuardOutcome(allowed=True, allowed_qty=10.0),
        )
        assert result.status == OrderStatus.FILLED
        assert result.qty == 10.0
        pos = auth.load_paper_position("u-1", "AAPL")
        assert pos is not None
        assert pos["qty"] == 10.0
        assert pos["avg_cost"] == 100.0

    def test_buy_more_averages_cost(self):
        for px in (100.0, 200.0):
            paper.place_order(
                _token(), fire_id=f"fire-{px}", session_id="s1", recipe_id=None,
                ticker="AAPL", side=OrderSide.BUY, qty=10.0, market_price=px,
                slippage_bps=0, decision=_decision(), conviction=7, kelly_fraction=0.02,
                guard=GuardOutcome(allowed=True, allowed_qty=10.0),
            )
        pos = auth.load_paper_position("u-1", "AAPL")
        assert pos["qty"] == 20.0
        assert pos["avg_cost"] == pytest.approx(150.0)

    def test_buy_debits_cash(self):
        paper.place_order(
            _token(), fire_id="fire-1", session_id="s1", recipe_id=None,
            ticker="AAPL", side=OrderSide.BUY, qty=10.0, market_price=100.0,
            slippage_bps=0, decision=_decision(), conviction=7, kelly_fraction=0.02,
            guard=GuardOutcome(allowed=True, allowed_qty=10.0),
        )
        acct = auth.load_paper_account("u-1")
        # default starting cash 100_000 - 1000 = 99_000
        assert float(acct["cash"]) == pytest.approx(99_000.0)


class TestSellFlow:
    def _open_long(self, qty=20.0, px=100.0):
        paper.place_order(
            _token(), fire_id="open", session_id="s1", recipe_id=None,
            ticker="AAPL", side=OrderSide.BUY, qty=qty, market_price=px,
            slippage_bps=0, decision=_decision(), conviction=7, kelly_fraction=0.02,
            guard=GuardOutcome(allowed=True, allowed_qty=qty),
        )

    def test_partial_sell_realizes_pnl(self):
        self._open_long(qty=20.0, px=100.0)
        # Sell 10 @ 120 → realize $200 PnL.
        paper.place_order(
            _token(), fire_id="sell-1", session_id="s1", recipe_id=None,
            ticker="AAPL", side=OrderSide.SELL, qty=10.0, market_price=120.0,
            slippage_bps=0, decision=_decision(), conviction=7, kelly_fraction=0.02,
            guard=GuardOutcome(allowed=True, allowed_qty=10.0),
        )
        pos = auth.load_paper_position("u-1", "AAPL")
        assert pos["qty"] == 10.0
        acct = auth.load_paper_account("u-1")
        assert float(acct["realized_pnl"]) == pytest.approx(200.0)

    def test_full_sell_deletes_position(self):
        self._open_long(qty=20.0, px=100.0)
        paper.place_order(
            _token(), fire_id="sell-full", session_id="s1", recipe_id=None,
            ticker="AAPL", side=OrderSide.SELL, qty=20.0, market_price=110.0,
            slippage_bps=0, decision=_decision(), conviction=7, kelly_fraction=0.02,
            guard=GuardOutcome(allowed=True, allowed_qty=20.0),
        )
        assert auth.load_paper_position("u-1", "AAPL") is None


class TestSlippage:
    def test_buy_pays_up(self):
        result = paper.place_order(
            _token(), fire_id="fire-1", session_id="s1", recipe_id=None,
            ticker="AAPL", side=OrderSide.BUY, qty=1.0, market_price=100.0,
            slippage_bps=50,  # 0.5%
            decision=_decision(), conviction=7, kelly_fraction=0.02,
            guard=GuardOutcome(allowed=True, allowed_qty=1.0),
        )
        assert result.fill_price == pytest.approx(100.5)

    def test_sell_receives_down(self):
        # First open a position.
        paper.place_order(
            _token(), fire_id="open", session_id="s1", recipe_id=None,
            ticker="AAPL", side=OrderSide.BUY, qty=10.0, market_price=100.0,
            slippage_bps=0, decision=_decision(), conviction=7, kelly_fraction=0.02,
            guard=GuardOutcome(allowed=True, allowed_qty=10.0),
        )
        result = paper.place_order(
            _token(), fire_id="sell", session_id="s1", recipe_id=None,
            ticker="AAPL", side=OrderSide.SELL, qty=5.0, market_price=100.0,
            slippage_bps=50,
            decision=_decision(), conviction=7, kelly_fraction=0.02,
            guard=GuardOutcome(allowed=True, allowed_qty=5.0),
        )
        assert result.fill_price == pytest.approx(99.5)


class TestIdempotency:
    def test_same_fire_returns_existing_order(self):
        first = paper.place_order(
            _token(), fire_id="fire-x", session_id="s1", recipe_id=None,
            ticker="AAPL", side=OrderSide.BUY, qty=10.0, market_price=100.0,
            slippage_bps=0, decision=_decision(), conviction=7, kelly_fraction=0.02,
            guard=GuardOutcome(allowed=True, allowed_qty=10.0),
        )
        # Same fire_id + ticker + side → idempotent return; books unchanged.
        second = paper.place_order(
            _token(), fire_id="fire-x", session_id="s1", recipe_id=None,
            ticker="AAPL", side=OrderSide.BUY, qty=10.0, market_price=100.0,
            slippage_bps=0, decision=_decision(), conviction=7, kelly_fraction=0.02,
            guard=GuardOutcome(allowed=True, allowed_qty=10.0),
        )
        assert second.idempotent is True
        assert second.order_id == first.order_id
        pos = auth.load_paper_position("u-1", "AAPL")
        assert pos["qty"] == 10.0  # not 20

    def test_different_fire_does_not_dedupe(self):
        for fid in ("fire-a", "fire-b"):
            paper.place_order(
                _token(), fire_id=fid, session_id="s1", recipe_id=None,
                ticker="AAPL", side=OrderSide.BUY, qty=5.0, market_price=100.0,
                slippage_bps=0, decision=_decision(), conviction=7, kelly_fraction=0.02,
                guard=GuardOutcome(allowed=True, allowed_qty=5.0),
            )
        pos = auth.load_paper_position("u-1", "AAPL")
        assert pos["qty"] == 10.0


class TestBlockedAndClamped:
    def test_blocked_records_order_without_position_change(self):
        result = paper.place_order(
            _token(), fire_id="fire-blocked", session_id="s1", recipe_id=None,
            ticker="AAPL", side=OrderSide.BUY, qty=10.0, market_price=100.0,
            slippage_bps=0, decision=_decision(), conviction=7, kelly_fraction=0.02,
            guard=GuardOutcome(allowed=False, allowed_qty=0.0, blocked_qty=10.0, rule="kill_switch"),
        )
        assert result.status == OrderStatus.BLOCKED
        assert auth.load_paper_position("u-1", "AAPL") is None
        # No cash debit on a blocked order.
        acct = auth.load_paper_account("u-1")
        assert acct is None or float(acct.get("cash", 100_000.0)) == 100_000.0

    def test_clamped_writes_partial_fill(self):
        result = paper.place_order(
            _token(), fire_id="fire-clamp", session_id="s1", recipe_id=None,
            ticker="AAPL", side=OrderSide.BUY, qty=100.0, market_price=100.0,
            slippage_bps=0, decision=_decision(), conviction=7, kelly_fraction=0.02,
            guard=GuardOutcome(allowed=True, allowed_qty=20.0, blocked_qty=80.0, rule="max_position"),
        )
        assert result.status == OrderStatus.CLAMPED
        assert result.qty == 20.0
        pos = auth.load_paper_position("u-1", "AAPL")
        assert pos["qty"] == 20.0  # the clamped quantity, not 100
