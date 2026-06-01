"""Tests for the Recipe CRUD path (in-memory fallback)."""

from __future__ import annotations

import pytest

from agenticwhales import recipes as recipes_mod
from agenticwhales.agents.schemas import RecipeStatus
from web import auth


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


def _form(**overrides):
    base = {
        "name": "Daily AAPL",
        "tickers": ["AAPL"],
        "llm_provider": "google",
        "quick_model": "gemini-3-flash-preview",
        "deep_model": "gemini-3.1-pro-preview",
        "bull_model": "deepseek-v4",
        "bear_model": "gemini-3.1-pro-preview",
        "analysts": ["market", "quant"],
        "schedule_kind": "interval",
        "schedule_expr": "60s",
    }
    base.update(overrides)
    return base


class TestCRUD:
    def test_save_and_load(self):
        recipe = recipes_mod.build_recipe(_form(), user_id="u-1")
        recipes_mod.save(recipe)
        loaded = recipes_mod.load(recipe.id)
        assert loaded is not None
        assert loaded.name == "Daily AAPL"
        assert loaded.bull_model == "deepseek-v4"

    def test_list_for_user(self):
        for name in ("a", "b", "c"):
            r = recipes_mod.build_recipe(_form(name=name), user_id="u-1")
            recipes_mod.save(r)
        # Another user's recipe should not leak.
        other = recipes_mod.build_recipe(_form(name="x"), user_id="u-2")
        recipes_mod.save(other)

        rs = recipes_mod.list_for_user("u-1")
        assert {r.name for r in rs} == {"a", "b", "c"}

    def test_list_all_active(self):
        r1 = recipes_mod.build_recipe(_form(name="a"), user_id="u-1")
        r2 = recipes_mod.build_recipe(_form(name="b"), user_id="u-2")
        recipes_mod.save(r1)
        recipes_mod.save(r2)
        recipes_mod.update_status(r2.id, RecipeStatus.PAUSED)

        active = recipes_mod.list_all_active()
        assert {r.id for r in active} == {r1.id}

    def test_delete(self):
        r = recipes_mod.build_recipe(_form(), user_id="u-1")
        recipes_mod.save(r)
        assert recipes_mod.load(r.id) is not None
        assert recipes_mod.delete(r.id) is True
        assert recipes_mod.load(r.id) is None

    def test_bump_and_reset_failures(self):
        r = recipes_mod.build_recipe(_form(), user_id="u-1")
        recipes_mod.save(r)
        assert recipes_mod.bump_failures(r.id) == 1
        assert recipes_mod.bump_failures(r.id) == 2
        recipes_mod.reset_failures(r.id)
        loaded = recipes_mod.load(r.id)
        assert loaded.consecutive_failures == 0

    def test_touch_last_run(self):
        r = recipes_mod.build_recipe(_form(), user_id="u-1")
        recipes_mod.save(r)
        recipes_mod.touch_last_run(r.id, when=1_700_000_000.0)
        loaded = recipes_mod.load(r.id)
        # In dev fallback, last_run_at stays as the epoch we passed in.
        assert loaded.last_run_at is not None
