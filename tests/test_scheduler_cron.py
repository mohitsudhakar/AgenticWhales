"""Tests for the weekly maintenance cron — prompt-eval harness wiring.

We don't spin up real APScheduler timers here; we exercise the cron
*function* directly (`RecipeScheduler._run_prompt_evals`) so behaviour is
deterministic and fast. The registration test verifies the job lands in
the scheduler with the correct id + cron trigger.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from web import auth
from web.scheduler import RecipeScheduler


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


def _seed_outcomes(user_id, n=25, prob=0.85):
    """Seed N decision_outcomes rows so the prompt-eval harness has data."""
    for i in range(n):
        oid = uuid.uuid4().hex
        auth._memstore[("decision_outcomes", oid)] = {
            "paper_order_id": oid,
            "user_id": user_id,
            "ticker": "AAPL",
            "predicted_return_pct": 10.0,
            "predicted_volatility_pct": 20.0,
            "predicted_prob_of_profit": prob,
            "predicted_hold_days": 30,
            "realized_return_pct": 5.0 if i < n // 2 else -5.0,
            "realized_at": datetime.now(tz=timezone.utc).isoformat(),
            "hit": i < n // 2,
            "brier_component": (prob - (1.0 if i < n // 2 else 0.0)) ** 2,
            "resolved_at": datetime.now(tz=timezone.utc).isoformat(),
        }


class TestRegistration:
    def test_register_idempotent_when_scheduler_present(self):
        # Skip when apscheduler isn't installed (dev-only dep, not in
        # the default test environment).
        pytest.importorskip("apscheduler")
        sched = RecipeScheduler()
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        sched._scheduler = AsyncIOScheduler(timezone="UTC")
        sched._register_weekly_jobs()
        sched._register_weekly_jobs()   # idempotent (replace_existing=True)
        assert sched._scheduler.get_job("prompt_eval_weekly") is not None
        assert sched._scheduler.get_job("outcome_resolver_nightly") is not None

    def test_register_noop_without_scheduler(self):
        sched = RecipeScheduler()
        sched._scheduler = None
        # Must not raise even though no scheduler exists.
        sched._register_weekly_jobs()


class TestRunOutcomeResolver:
    def test_skips_when_not_leader(self):
        # Seed an unresolved paper order and run as non-leader — no resolver call.
        auth._memstore[("paper_orders", "po-1")] = {
            "id": "po-1", "user_id": "u-1", "ticker": "AAPL",
            "fill_price": 100.0, "qty": 10.0, "side": "buy",
        }
        sched = RecipeScheduler()
        sched._is_leader = False
        # Should be a no-op even with seeded orders.
        sched._run_outcome_resolver()
        outcomes = [v for (t, _), v in auth._memstore.items() if t == "decision_outcomes"]
        assert outcomes == []

    def test_walks_every_user_with_paper_orders(self, monkeypatch):
        # Seed paper orders for two users.
        for uid in ("u-a", "u-b"):
            auth._memstore[("paper_orders", f"po-{uid}")] = {
                "id": f"po-{uid}", "user_id": uid, "ticker": "AAPL",
                "fill_price": 100.0, "qty": 10.0, "side": "buy",
            }
        # Capture the resolver calls.
        called = []
        def fake_resolve(user_id, limit=None):
            called.append((user_id, limit))
            return 1
        from agenticwhales import outcomes as outcomes_mod
        monkeypatch.setattr(outcomes_mod, "resolve_outcomes_for_user", fake_resolve)
        sched = RecipeScheduler()
        sched._is_leader = True
        sched._run_outcome_resolver()
        assert {uid for uid, _ in called} == {"u-a", "u-b"}


class TestRunPromptEvals:
    def test_skips_when_not_leader(self):
        # Seed data + run without becoming leader. Should do nothing.
        _seed_outcomes("u-1", n=25, prob=0.85)
        sched = RecipeScheduler()
        sched._is_leader = False
        sched._run_prompt_evals()
        # No prompt_evals row should have been written.
        rows = [v for (t, _), v in auth._memstore.items() if t == "prompt_evals"]
        assert rows == []

    def test_runs_for_all_users_with_outcomes(self):
        _seed_outcomes("u-1", n=25, prob=0.85)
        _seed_outcomes("u-2", n=30, prob=0.85)
        # User with too few outcomes — should be silently skipped by the harness.
        _seed_outcomes("u-3", n=5, prob=0.85)

        sched = RecipeScheduler()
        sched._is_leader = True
        sched._run_prompt_evals()

        rows = [v for (t, _), v in auth._memstore.items() if t == "prompt_evals"]
        users_evaluated = {r["user_id"] for r in rows}
        assert users_evaluated == {"u-1", "u-2"}    # u-3 was below min_n

    def test_canary_flat_coin_promotes_against_overconfident_baseline(self):
        # Baseline = 0.85 prob, 50% hit rate → Brier ~0.37
        # Variant  = 0.5,  50% hit rate → Brier 0.25
        # Improvement = 0.12 (well above 0.02 promotion threshold)
        _seed_outcomes("u-1", n=25, prob=0.85)

        sched = RecipeScheduler()
        sched._is_leader = True
        sched._run_prompt_evals()

        rows = [v for (t, _), v in auth._memstore.items() if t == "prompt_evals"]
        assert rows
        assert rows[0]["variant"] == "canary-flat-coin"
        assert rows[0]["promoted"] is True
