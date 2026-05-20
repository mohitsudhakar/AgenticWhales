"""Tests for RecipeScheduler gate logic (in-memory fallback).

Cover the fail-fast gate sequence: status → market-hours → budget →
user-spend → consecutive-failures → all-clear. No real APScheduler needed —
we drive `_do_fire` directly.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agenticwhales import recipes as recipes_mod
from agenticwhales.agents.schemas import RecipeStatus
from web import auth
from web.scheduler import RecipeScheduler


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


def _make_recipe(user_id="u-1", **overrides):
    form = {
        "name": "test", "tickers": ["AAPL"], "llm_provider": "google",
        "quick_model": "gemini-3-flash-preview",
        "deep_model": "gemini-3.1-pro-preview",
        "bull_model": "deepseek-v4",
        "bear_model": "gemini-3.1-pro-preview",
        "analysts": ["market"],
        "schedule_kind": "manual",
        "market_hours_only": False,  # default off in tests
    }
    form.update(overrides)
    r = recipes_mod.build_recipe(form, user_id=user_id)
    recipes_mod.save(r)
    return r


class TestStatusGate:
    def test_paused_recipe_dropped(self):
        sched = RecipeScheduler()
        r = _make_recipe()
        recipes_mod.update_status(r.id, RecipeStatus.PAUSED)
        # Override the in-memory recipe so _do_fire sees PAUSED.
        loaded = recipes_mod.load(r.id)
        called = {"count": 0}

        def runner(*_args, **_kwargs):
            called["count"] += 1

        sched._run_session = runner
        sched._do_fire(loaded, "fire-1", skip_market_hours=True)
        assert called["count"] == 0


class TestMarketHoursGate:
    def test_market_closed_skips(self):
        sched = RecipeScheduler()
        r = _make_recipe(market_hours_only=True, exchange_code="XNYS")
        called = {"count": 0}

        def runner(*_args, **_kwargs):
            called["count"] += 1

        sched._run_session = runner
        # Patch is_market_open to return False.
        with patch("web.scheduler.is_market_open", return_value=False):
            sched._do_fire(recipes_mod.load(r.id), "fire-1", skip_market_hours=False)
        assert called["count"] == 0

    def test_skip_market_hours_param_overrides(self):
        sched = RecipeScheduler()
        r = _make_recipe(market_hours_only=True)
        called = {"count": 0}

        def runner(*_args, **_kwargs):
            called["count"] += 1

        sched._run_session = runner
        with patch("web.scheduler.is_market_open", return_value=False):
            sched._do_fire(recipes_mod.load(r.id), "fire-1", skip_market_hours=True)
        assert called["count"] == 1


class TestBudgetGate:
    def test_budget_exhausted_skips(self):
        sched = RecipeScheduler()
        r = _make_recipe(max_daily_token_cost_usd=1.0)
        # Pre-seed usage at the cap.
        from datetime import datetime, timezone
        today = datetime.now(tz=timezone.utc).date().isoformat()
        auth.add_recipe_usage(
            recipe_id=r.id, user_id="u-1", usage_date=today,
            input_tokens=1000, output_tokens=500, reasoning_tokens=0,
            token_cost_usd=1.5,
        )
        called = {"count": 0}

        def runner(*_args, **_kwargs):
            called["count"] += 1

        sched._run_session = runner
        sched._do_fire(recipes_mod.load(r.id), "fire-1", skip_market_hours=True)
        assert called["count"] == 0

        # And a risk_event row was written.
        events = auth.list_risk_events("u-1")
        assert any(e["rule"] == "budget" for e in events)


class TestUserSpendGate:
    def test_user_spend_cap_skips(self):
        sched = RecipeScheduler()
        r = _make_recipe()
        from datetime import datetime, timezone
        today = datetime.now(tz=timezone.utc).date().isoformat()
        auth.upsert_risk_limits("u-1", daily_spend_cap_usd=0.10)
        auth.add_user_spend("u-1", today, 0.20)
        called = {"count": 0}

        def runner(*_args, **_kwargs):
            called["count"] += 1

        sched._run_session = runner
        sched._do_fire(recipes_mod.load(r.id), "fire-1", skip_market_hours=True)
        assert called["count"] == 0
        events = auth.list_risk_events("u-1")
        assert any(e["rule"] == "user_spend_cap" for e in events)


class TestFailureGate:
    def test_consecutive_failures_auto_pause(self):
        sched = RecipeScheduler()
        r = _make_recipe()
        for _ in range(5):
            recipes_mod.bump_failures(r.id)
        called = {"count": 0}

        def runner(*_args, **_kwargs):
            called["count"] += 1

        sched._run_session = runner
        loaded = recipes_mod.load(r.id)
        sched._do_fire(loaded, "fire-1", skip_market_hours=True)
        assert called["count"] == 0
        # Status flipped to failed.
        assert recipes_mod.load(r.id).status == RecipeStatus.FAILED
        events = auth.list_risk_events("u-1")
        assert any(e["rule"] == "failures" for e in events)


class TestAllClearPath:
    def test_runner_called_when_all_gates_pass(self):
        sched = RecipeScheduler()
        r = _make_recipe()
        called = {"count": 0, "recipe_id": None, "fire_id": None}

        def runner(recipe, fire_id):
            called["count"] += 1
            called["recipe_id"] = recipe.id
            called["fire_id"] = fire_id

        sched._run_session = runner
        sched._do_fire(recipes_mod.load(r.id), "fire-1", skip_market_hours=True)
        assert called["count"] == 1
        assert called["recipe_id"] == r.id
        assert called["fire_id"] == "fire-1"

    def test_runner_exception_bumps_failures(self):
        sched = RecipeScheduler()
        r = _make_recipe()

        def runner(*_args, **_kwargs):
            raise RuntimeError("simulated")

        sched._run_session = runner
        sched._do_fire(recipes_mod.load(r.id), "fire-1", skip_market_hours=True)
        loaded = recipes_mod.load(r.id)
        assert loaded.consecutive_failures == 1


class TestIntervalParser:
    @pytest.mark.parametrize("expr,expected", [
        ("30",  30),
        ("30s", 30),
        ("15m", 900),
        ("6h",  21_600),
        ("1d",  86_400),
    ])
    def test_parse(self, expr, expected):
        from web.scheduler import _parse_interval_seconds
        assert _parse_interval_seconds(expr) == expected

    def test_invalid_raises(self):
        from web.scheduler import _parse_interval_seconds
        with pytest.raises(ValueError):
            _parse_interval_seconds("garbage")
        with pytest.raises(ValueError):
            _parse_interval_seconds("")
