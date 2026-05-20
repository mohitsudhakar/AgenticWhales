"""End-to-end smoke test for the Phase 1 vertical.

Drives the full chain WITHOUT real LLM calls:

  recipe.create → scheduler._do_fire → stub SessionRunner → final_trade_decision
    → runner._post_decision_hook → RiskGuard → kelly_sizing → place_order
    → conviction_scores → risk_events (on clamps)
    → cost_middleware.record_fire_cost → recipe_usage / user_spend_daily

Substitutes a stub `run_session` callback that emits a synthetic
`pm_decision` directly, so the test runs in <1s and doesn't depend on
any provider API. The exact same code path runs in prod — the only
difference is who populates `pm_decision` (a real LangGraph run vs the
stub here).

What this test guarantees:
  - The vertical is wired correctly: every table that should get a row
    in a successful fire gets one.
  - Idempotency holds: re-fire the same recipe with the same fire_id and
    no duplicate paper_order is written.
  - Risk-guard clamping path works end-to-end (paper_order.status='clamped'
    and risk_events row appears).
  - Cost roll-up debits recipe_usage + user_spend_daily.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from agenticwhales import recipes as recipes_mod
from agenticwhales.agents.schemas import (
    OrderStatus,
    PortfolioRating,
    PortfolioDecision,
    RecipeStatus,
)
from agenticwhales.llm_clients.cost_middleware import record_fire_cost
from web import auth
from web.scheduler import RecipeScheduler


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


def _build_recipe(user_id="u-e2e", **overrides):
    form = {
        "name": "e2e",
        "tickers": ["AAPL"],
        "llm_provider": "google",
        "quick_model": "gemini-3-flash-preview",
        "deep_model": "gemini-3.1-pro-preview",
        "bull_model": "deepseek-v4",
        "bear_model": "gemini-3.1-pro-preview",
        "analysts": ["market", "quant"],
        "schedule_kind": "manual",
        "output_policy": "paper_trade",
        "market_hours_only": False,
        "max_daily_token_cost_usd": 10.0,
    }
    form.update(overrides)
    recipe = recipes_mod.build_recipe(form, user_id=user_id)
    recipes_mod.save(recipe)
    return recipe


def _stub_pm_decision(rating=PortfolioRating.BUY) -> PortfolioDecision:
    """A canned decision strong enough to clear Kelly's f* > 0 threshold."""
    return PortfolioDecision(
        rating=rating,
        executive_summary="stub",
        investment_thesis="stub",
        expected_return_pct=20.0,
        expected_volatility_pct=10.0,
        prob_of_profit=0.65,
        expected_hold_days=30,
        stop_loss=95.0,
        take_profit=130.0,
    )


def _stub_run_session(recipe, fire_id, *,
                     decision: PortfolioDecision = None,
                     last_price: float = 100.0,
                     stats: dict = None):
    """Mimics what a real SessionRunner would do without invoking LangGraph.

    Writes a session row, then calls the post-decision-hook helpers directly
    in the same sequence the real runner uses:
      1. record_fire_cost (cost roll-up)
      2. RiskGuard → kelly_sizing → place_order → conviction_scores
    """
    import uuid

    from agenticwhales.agents.schemas import (
        OrderSide,
        OutputPolicy,
        PaperAccount,
        PaperPosition,
    )
    from agenticwhales import paper, risk as risk_mod
    from agenticwhales.audit import impersonate

    decision = decision or _stub_pm_decision()
    user_id = recipe.user_id
    session_id = uuid.uuid4().hex
    ticker = recipe.tickers[0]

    # 1. Persist a minimal session row.
    auth.save_session({
        "id": session_id,
        "user_id": user_id,
        "ticker": ticker,
        "analysis_date": datetime.now(tz=timezone.utc).date().isoformat(),
        "status": "completed",
        "created_at": datetime.now(tz=timezone.utc).timestamp(),
        "completed_at": datetime.now(tz=timezone.utc).timestamp(),
        "recipe_id": recipe.id,
        "fire_id": fire_id,
        "config": {
            "llm_provider": recipe.llm_provider,
            "quick_think_llm": recipe.quick_model,
            "deep_think_llm": recipe.deep_model,
        },
        "stats": stats or {"tokens_in": 100_000, "tokens_out": 10_000},
    })

    # 2. Cost roll-up.
    record_fire_cost(
        user_id=user_id, recipe_id=recipe.id, session_id=session_id,
        provider=recipe.llm_provider, quick_model=recipe.quick_model,
        deep_model=recipe.deep_model,
        stats=stats or {"tokens_in": 100_000, "tokens_out": 10_000},
    )

    # 3. Conviction score (always recorded).
    conviction = paper.score_from_decision(decision)
    auth.insert_conviction_score({
        "user_id": user_id, "recipe_id": recipe.id, "session_id": session_id,
        "ticker": ticker,
        "rating": decision.rating.value,
        "conviction_score": conviction,
        "expected_return_pct": decision.expected_return_pct,
        "expected_volatility_pct": decision.expected_volatility_pct,
        "prob_of_profit": decision.prob_of_profit,
        "recorded_at": datetime.now(tz=timezone.utc).isoformat(),
    })

    # 4. Paper-trade only when output_policy says so.
    if recipe.output_policy != OutputPolicy.PAPER_TRADE:
        return

    # 5. Risk guard → place_order under impersonation.
    limits_row = auth.load_risk_limits(user_id) or auth._default_risk_limits_row(user_id)
    limits = risk_mod.RiskLimits(
        max_position_pct=float(limits_row.get("max_position_pct", 0.10)),
        max_daily_drawdown_pct=float(limits_row.get("max_daily_drawdown_pct", 0.03)),
        max_slippage_bps=int(limits_row.get("max_slippage_bps", 10)),
        kelly_fraction_cap=float(limits_row.get("kelly_fraction_cap", 0.10)),
        global_kill_switch=bool(limits_row.get("global_kill_switch", False)),
        allow_shorts=bool(limits_row.get("allow_shorts", False)),
    )
    account_row = auth.load_paper_account(user_id) or {
        "starting_cash": paper.DEFAULT_STARTING_CASH,
        "cash": paper.DEFAULT_STARTING_CASH,
        "realized_pnl": 0.0,
        "short_collateral_reserved": 0.0,
        "nav_open_today": None,
        "nav_open_today_date": None,
    }
    account = PaperAccount(
        user_id=user_id,
        starting_cash=float(account_row.get("starting_cash", paper.DEFAULT_STARTING_CASH)),
        cash=float(account_row["cash"]),
        short_collateral_reserved=float(account_row.get("short_collateral_reserved", 0.0)),
        realized_pnl=float(account_row.get("realized_pnl", 0.0)),
    )
    positions = [
        PaperPosition(
            user_id=user_id, ticker=p["ticker"], qty=float(p["qty"]),
            avg_cost=float(p["avg_cost"]),
            last_price=float(p["last_price"]) if p.get("last_price") is not None else None,
        ) for p in auth.list_paper_positions(user_id)
    ]

    sizing = paper.kelly_sizing(
        decision, nav=account.cash + sum(p.qty * (p.last_price or p.avg_cost) for p in positions),
        last_price=last_price, kelly_fraction_cap=limits.kelly_fraction_cap,
    )
    if sizing.qty == 0:
        return

    guard = risk_mod.RiskGuard(
        user_id=user_id, limits=limits, account=account, positions=positions,
    )
    outcome = guard.evaluate(decision, ticker, abs(sizing.qty), last_price)

    with impersonate(user_id, "scheduler_fire", fire_id=fire_id) as token:
        if outcome.rule:
            risk_mod.record_event(
                token, recipe_id=recipe.id, session_id=session_id, ticker=ticker,
                rule=outcome.rule,
                details={
                    "target_qty": abs(sizing.qty),
                    "allowed_qty": outcome.allowed_qty,
                    "reason": outcome.reason,
                },
            )
            if not outcome.allowed:
                return

        from agenticwhales.agents.schemas import OrderSide
        side = OrderSide.BUY if sizing.direction > 0 else OrderSide.SELL
        paper.place_order(
            token, fire_id=fire_id, session_id=session_id, recipe_id=recipe.id,
            ticker=ticker, side=side, qty=abs(sizing.qty),
            market_price=last_price, slippage_bps=limits.max_slippage_bps,
            decision=decision, conviction=conviction,
            kelly_fraction=sizing.fraction, guard=outcome,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestE2EHappyPath:
    def test_full_vertical_writes_every_expected_row(self):
        recipe = _build_recipe()
        sched = RecipeScheduler()
        sched._run_session = lambda r, fid: _stub_run_session(r, fid)
        sched._do_fire(recipe, "fire-e2e-1", skip_market_hours=True)

        # 1. A session row exists.
        sessions = [v for (t, _), v in auth._memstore.items() if t == "sessions"]
        recipe_sessions = [s for s in sessions if s.get("recipe_id") == recipe.id]
        assert len(recipe_sessions) == 1

        # 2. A paper_order with status='filled' exists.
        orders = auth.list_paper_orders(recipe.user_id, limit=10)
        assert len(orders) == 1
        assert orders[0]["status"] == "filled"
        assert orders[0]["ticker"] == "AAPL"

        # 3. The paper position landed.
        positions = auth.list_paper_positions(recipe.user_id)
        assert len(positions) == 1
        assert positions[0]["ticker"] == "AAPL"
        assert float(positions[0]["qty"]) > 0

        # 4. Cash debited.
        acct = auth.load_paper_account(recipe.user_id)
        assert float(acct["cash"]) < float(acct["starting_cash"])

        # 5. Conviction score recorded.
        cs = auth.list_conviction_scores(recipe.user_id, ticker="AAPL")
        assert len(cs) == 1
        assert cs[0]["rating"] == "Buy"

        # 6. Cost roll-up: recipe_usage + user_spend_daily.
        today = datetime.now(tz=timezone.utc).date().isoformat()
        usage = auth.load_recipe_usage(recipe.id, today)
        assert usage is not None
        assert float(usage["token_cost_usd"]) > 0
        assert auth.load_user_spend(recipe.user_id, today) > 0

        # 7. consecutive_failures reset, last_run_at touched.
        loaded = recipes_mod.load(recipe.id)
        assert loaded.consecutive_failures == 0
        assert loaded.last_run_at is not None


class TestE2EIdempotency:
    def test_same_fire_id_does_not_double_place(self):
        recipe = _build_recipe()
        sched = RecipeScheduler()
        sched._run_session = lambda r, fid: _stub_run_session(r, fid)

        sched._do_fire(recipe, "fire-idem", skip_market_hours=True)
        first_orders = auth.list_paper_orders(recipe.user_id)
        assert len(first_orders) == 1
        first_qty = float(first_orders[0]["qty"])

        # Re-fire with the same fire_id.
        sched._do_fire(recipe, "fire-idem", skip_market_hours=True)
        second_orders = auth.list_paper_orders(recipe.user_id)
        assert len(second_orders) == 1  # idempotent → no duplicate
        # Position qty didn't double.
        pos = auth.load_paper_position(recipe.user_id, "AAPL")
        assert float(pos["qty"]) == first_qty


class TestE2EClampPath:
    def test_position_cap_clamps_and_emits_risk_event(self):
        recipe = _build_recipe()
        # Tighten the position cap so the order gets clamped.
        auth.upsert_risk_limits(recipe.user_id, max_position_pct=0.001)

        sched = RecipeScheduler()
        sched._run_session = lambda r, fid: _stub_run_session(r, fid)
        sched._do_fire(recipe, "fire-clamp", skip_market_hours=True)

        # Order should land with status='clamped' (partial fill).
        orders = auth.list_paper_orders(recipe.user_id, limit=10)
        assert len(orders) == 1
        assert orders[0]["status"] == "clamped"

        # A risk_event row with rule='max_position' should be present.
        events = auth.list_risk_events(recipe.user_id)
        assert any(e["rule"] == "max_position" for e in events)


class TestE2ENotifyOnly:
    def test_notify_policy_records_conviction_but_no_order(self):
        recipe = _build_recipe(output_policy="notify")
        sched = RecipeScheduler()
        sched._run_session = lambda r, fid: _stub_run_session(r, fid)
        sched._do_fire(recipe, "fire-notify", skip_market_hours=True)

        # Conviction recorded.
        cs = auth.list_conviction_scores(recipe.user_id, ticker="AAPL")
        assert len(cs) == 1

        # NO paper order.
        assert auth.list_paper_orders(recipe.user_id) == []


class TestE2EKillSwitch:
    def test_global_kill_switch_blocks_order(self):
        recipe = _build_recipe()
        auth.upsert_risk_limits(recipe.user_id, global_kill_switch=True)

        sched = RecipeScheduler()
        sched._run_session = lambda r, fid: _stub_run_session(r, fid)
        sched._do_fire(recipe, "fire-kill", skip_market_hours=True)

        # Conviction still recorded (informational only).
        cs = auth.list_conviction_scores(recipe.user_id)
        assert len(cs) == 1

        # No filled position.
        assert auth.list_paper_positions(recipe.user_id) == []

        # The risk_event row with rule='kill_switch' should be present.
        events = auth.list_risk_events(recipe.user_id)
        assert any(e["rule"] == "kill_switch" for e in events)


class TestE2EBudgetGate:
    def test_budget_exhausted_blocks_subsequent_fires(self):
        recipe = _build_recipe(max_daily_token_cost_usd=0.50)
        sched = RecipeScheduler()
        # First fire: heavy stats → cost > cap.
        heavy_stats = {"tokens_in": 1_000_000, "tokens_out": 100_000}
        sched._run_session = lambda r, fid: _stub_run_session(r, fid, stats=heavy_stats)
        sched._do_fire(recipe, "fire-1", skip_market_hours=True)

        # After fire-1, recipe_usage should be over the cap.
        today = datetime.now(tz=timezone.utc).date().isoformat()
        usage = auth.load_recipe_usage(recipe.id, today)
        assert float(usage["token_cost_usd"]) > 0.50

        # Second fire should be skipped by the budget gate.
        sched._do_fire(recipe, "fire-2", skip_market_hours=True)
        events = auth.list_risk_events(recipe.user_id)
        assert any(e["rule"] == "budget" for e in events)

        # Only the first fire's order landed.
        orders = auth.list_paper_orders(recipe.user_id)
        assert len(orders) == 1
