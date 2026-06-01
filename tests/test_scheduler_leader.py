"""Coverage for web/scheduler.py leader-election, lifecycle, weekly cron jobs,
bootstrap, and job CRUD against a fake APScheduler + fake Supabase transport.
No real scheduler threads, no network. Async paths driven via asyncio.run.
"""

from __future__ import annotations

import asyncio

import pytest

from agenticwhales.agents.schemas import Recipe, RecipeStatus, ScheduleKind
from web import scheduler as sched_mod
from web.scheduler import RecipeScheduler


@pytest.fixture(autouse=True)
def _wipe(monkeypatch):
    from web import auth
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


def _recipe(rid="r1", user="u1", **kw):
    base = dict(
        id=rid, user_id=user, name="R", tickers=["AAPL"],
        llm_provider="google", quick_model="q", deep_model="d",
        bull_model="x", bear_model="y",
        status=RecipeStatus.ACTIVE, schedule_kind=ScheduleKind.MANUAL,
        market_hours_only=False, max_daily_token_cost_usd=5.0,
        consecutive_failures=0,
    )
    base.update(kw)
    return Recipe(**base)


class _FakeJob:
    def __init__(self, jid):
        self.id = jid


class _FakeAPS:
    """Records add/remove/pause/resume/shutdown; add_job returns a job."""

    def __init__(self, raise_on=None):
        self.jobs = []
        self.removed = []
        self.paused = []
        self.resumed = []
        self.shut = False
        self.raise_on = raise_on or set()

    def add_job(self, fn, trigger=None, **kw):
        jid = kw.get("id", "job")
        self.jobs.append((jid, kw))
        return _FakeJob(jid)

    def remove_job(self, jid):
        if "remove" in self.raise_on:
            raise RuntimeError("no job")
        self.removed.append(jid)

    def pause_job(self, jid):
        if "pause" in self.raise_on:
            raise RuntimeError("no job")
        self.paused.append(jid)

    def resume_job(self, jid):
        if "resume" in self.raise_on:
            raise RuntimeError("no job")
        self.resumed.append(jid)

    def shutdown(self, wait=False):
        self.shut = True


# ===========================================================================
# job CRUD with a (fake) scheduler present
# ===========================================================================

def test_add_cron_recipe_registers_job():
    s = RecipeScheduler()
    s._scheduler = _FakeAPS()
    jid = s.add(_recipe(schedule_kind=ScheduleKind.CRON, schedule_expr="0 9 * * *"))
    assert jid == "r1" and s._scheduler.jobs[0][0] == "r1"


def test_add_interval_recipe_registers_job():
    s = RecipeScheduler()
    s._scheduler = _FakeAPS()
    jid = s.add(_recipe(schedule_kind=ScheduleKind.INTERVAL, schedule_expr="15m"))
    assert jid == "r1"


def test_remove_pause_resume_with_scheduler():
    s = RecipeScheduler()
    s._scheduler = _FakeAPS()
    s.remove("r1"); s.pause("r1"); s.resume("r1")
    assert s._scheduler.removed == ["r1"]
    assert s._scheduler.paused == ["r1"]
    assert s._scheduler.resumed == ["r1"]


def test_remove_pause_swallow_errors():
    s = RecipeScheduler()
    s._scheduler = _FakeAPS(raise_on={"remove", "pause"})
    s.remove("r1")  # no raise
    s.pause("r1")   # no raise


def test_resume_missing_job_readds(monkeypatch):
    s = RecipeScheduler()
    s._scheduler = _FakeAPS(raise_on={"resume"})
    monkeypatch.setattr(sched_mod.recipes_mod, "load",
                        lambda rid: _recipe(schedule_kind=ScheduleKind.CRON,
                                            schedule_expr="0 9 * * *"))
    s.resume("r1")
    # fell into except → re-added via self.add
    assert any(j[0] == "r1" for j in s._scheduler.jobs)


# ===========================================================================
# weekly job registration
# ===========================================================================

def test_register_weekly_jobs_adds_four():
    s = RecipeScheduler()
    s._scheduler = _FakeAPS()
    s._register_weekly_jobs()
    ids = {j[0] for j in s._scheduler.jobs}
    assert {"prompt_eval_weekly", "outcome_resolver_nightly",
            "stuck_run_reaper", "stale_running_cleanup"} <= ids


def test_register_weekly_jobs_noop_without_scheduler():
    s = RecipeScheduler()
    s._register_weekly_jobs()  # no scheduler → returns, no raise


# ===========================================================================
# cron job bodies (leader-gated)
# ===========================================================================

def test_stale_running_cleanup_non_leader_returns(monkeypatch):
    s = RecipeScheduler()
    s._is_leader = False
    called = {"n": 0}
    monkeypatch.setattr("web.auth.delete_stuck_running_sessions",
                        lambda **k: called.__setitem__("n", called["n"] + 1))
    s._run_stale_running_cleanup()
    assert called["n"] == 0


def test_stale_running_cleanup_deletes(monkeypatch):
    s = RecipeScheduler()
    s._is_leader = True
    monkeypatch.setattr("web.auth.delete_stuck_running_sessions", lambda **k: 3)
    s._run_stale_running_cleanup()  # no raise; logs deletion


def test_stale_running_cleanup_swallows_error(monkeypatch):
    s = RecipeScheduler()
    s._is_leader = True
    def _boom(**k):
        raise RuntimeError("db down")
    monkeypatch.setattr("web.auth.delete_stuck_running_sessions", _boom)
    s._run_stale_running_cleanup()  # exception caught


def test_outcome_resolver_scans_memstore(monkeypatch):
    from web import auth
    s = RecipeScheduler()
    s._is_leader = True
    auth._memstore[("paper_orders", "o1")] = {"user_id": "u1"}
    seen = []
    monkeypatch.setattr("agenticwhales.outcomes.resolve_outcomes_for_user",
                        lambda uid, limit=200: seen.append(uid) or 2)
    s._run_outcome_resolver()
    assert seen == ["u1"]


def test_outcome_resolver_non_leader(monkeypatch):
    s = RecipeScheduler()
    s._is_leader = False
    s._run_outcome_resolver()  # returns immediately


def test_prompt_evals_scans_memstore(monkeypatch):
    from web import auth
    s = RecipeScheduler()
    s._is_leader = True
    auth._memstore[("decision_outcomes", "d1")] = {"user_id": "u1"}

    class _Res:
        promoted = True
    monkeypatch.setattr("agenticwhales.adaptive.evaluate_prompt_variant",
                        lambda uid, variant, scorer: _Res())
    s._run_prompt_evals()  # evaluated + promoted counters exercised


def test_prompt_evals_handles_none_result(monkeypatch):
    from web import auth
    s = RecipeScheduler()
    s._is_leader = True
    auth._memstore[("decision_outcomes", "d1")] = {"user_id": "u1"}
    monkeypatch.setattr("agenticwhales.adaptive.evaluate_prompt_variant",
                        lambda uid, variant, scorer: None)
    s._run_prompt_evals()


# ===========================================================================
# leader election with fake transport
# ===========================================================================

class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []

    def json(self):
        return self._payload


@pytest.fixture
def leader_db(monkeypatch):
    from web import auth
    monkeypatch.setattr(auth, "_db_writable", lambda: True)
    monkeypatch.setattr(auth, "_supabase_url", lambda: "https://fake.supabase.co")
    monkeypatch.setattr(auth, "_supabase_service_key", lambda: "svc")
    state = {"rows": [], "get": None, "post": None}

    class _HTTP:
        def get(self, url, headers=None, params=None, timeout=None):
            return state["get"] or _Resp(200, state["rows"])

        def post(self, url, headers=None, json=None, params=None, timeout=None):
            if state["post"] is not None:
                return state["post"]
            state["rows"] = [dict(json[0])]
            return _Resp(201, [dict(json[0])])

        def delete(self, url, headers=None, params=None, timeout=None):
            return _Resp(204)

    monkeypatch.setattr(auth, "_http", _HTTP())
    return state


def test_try_acquire_leader_dev_mode_true(monkeypatch):
    from web import auth
    monkeypatch.setattr(auth, "_db_writable", lambda: False)
    assert RecipeScheduler()._try_acquire_leader() is True


def test_try_acquire_leader_claims_empty_row(leader_db):
    # no existing row → upsert claims it and we win
    assert RecipeScheduler()._try_acquire_leader() is True


def test_try_acquire_leader_refreshes_own_row(leader_db):
    leader_db["rows"] = [{"id": 1, "worker_id": sched_mod._WORKER_ID,
                          "heartbeat_at": "2024-01-01T00:00:00+00:00"}]
    assert RecipeScheduler()._try_acquire_leader() is True


def test_try_acquire_leader_yields_to_fresh_other(leader_db):
    from datetime import datetime, timezone
    fresh = datetime.now(tz=timezone.utc).isoformat()
    leader_db["rows"] = [{"id": 1, "worker_id": "other", "heartbeat_at": fresh}]
    assert RecipeScheduler()._try_acquire_leader() is False


def test_try_acquire_leader_takes_stale_other(leader_db):
    leader_db["rows"] = [{"id": 1, "worker_id": "other",
                          "heartbeat_at": "2000-01-01T00:00:00+00:00"}]
    assert RecipeScheduler()._try_acquire_leader() is True


def test_try_acquire_leader_non_200_false(leader_db):
    leader_db["get"] = _Resp(500, None)
    assert RecipeScheduler()._try_acquire_leader() is False


def test_upsert_leader_row_failure(leader_db):
    leader_db["post"] = _Resp(409, None)
    from datetime import datetime, timezone
    assert RecipeScheduler()._upsert_leader_row(datetime.now(tz=timezone.utc)) is False


def test_release_leader_noop_when_not_leader(leader_db):
    s = RecipeScheduler()
    s._is_leader = False
    s._release_leader()  # early return, no raise


def test_release_leader_deletes_when_leader(leader_db):
    s = RecipeScheduler()
    s._is_leader = True
    s._release_leader()  # issues delete, no raise


# ===========================================================================
# bootstrap
# ===========================================================================

def test_bootstrap_registers_active_recipes(monkeypatch):
    s = RecipeScheduler()
    s._scheduler = _FakeAPS()
    monkeypatch.setattr(sched_mod.recipes_mod, "list_all_active",
                        lambda: [_recipe(rid="r1", schedule_kind=ScheduleKind.CRON,
                                         schedule_expr="0 9 * * *")])
    asyncio.run(s._bootstrap())
    assert s._bootstrapped is True
    assert any(j[0] == "r1" for j in s._scheduler.jobs)


def test_bootstrap_idempotent(monkeypatch):
    s = RecipeScheduler()
    s._bootstrapped = True
    monkeypatch.setattr(sched_mod.recipes_mod, "list_all_active",
                        lambda: (_ for _ in ()).throw(AssertionError("should not scan")))
    asyncio.run(s._bootstrap())  # early return


# ===========================================================================
# streaming worker + fire callback
# ===========================================================================

def test_start_streaming_worker_creates(monkeypatch):
    s = RecipeScheduler()

    class _Worker:
        def __init__(self, fire_recipe, is_leader_fn):
            self.started = None

        async def start(self, active):
            self.started = active

    monkeypatch.setattr("web.streaming_worker.StreamingWorker", _Worker)
    monkeypatch.setattr(sched_mod.recipes_mod, "list_all_active", lambda: [])
    asyncio.run(s._start_streaming_worker())
    assert s._streaming_worker is not None


def test_start_streaming_worker_noop_when_present():
    s = RecipeScheduler()
    sentinel = object()
    s._streaming_worker = sentinel
    asyncio.run(s._start_streaming_worker())
    assert s._streaming_worker is sentinel


def test_fire_from_streaming_dispatches(monkeypatch):
    s = RecipeScheduler()
    fired = {}
    monkeypatch.setattr(s, "_do_fire",
                        lambda recipe, fire_id, skip_market_hours: fired.update(
                            id=recipe.id, fid=fire_id))
    monkeypatch.setattr(sched_mod, "audit", lambda *a, **k: None)
    asyncio.run(s._fire_from_streaming(_recipe(), "AAPL", "vol spike"))
    assert fired["id"] == "r1" and fired["fid"]


def test_fire_from_streaming_swallows_error(monkeypatch):
    s = RecipeScheduler()
    def _boom(**k):
        raise RuntimeError("fire failed")
    monkeypatch.setattr(s, "_do_fire", lambda *a, **k: _boom())
    monkeypatch.setattr(sched_mod, "audit", lambda *a, **k: None)
    asyncio.run(s._fire_from_streaming(_recipe(), "AAPL", "x"))  # no raise


# ===========================================================================
# leader loop (single iteration, then cancelled)
# ===========================================================================

def test_leader_loop_becomes_leader(monkeypatch):
    s = RecipeScheduler()
    s._streaming_worker = object()  # so _start_streaming_worker early-returns
    monkeypatch.setattr(s, "_try_acquire_leader", lambda: True)

    async def _no_bootstrap():
        return None
    monkeypatch.setattr(s, "_bootstrap", _no_bootstrap)
    monkeypatch.setattr(s, "_register_weekly_jobs", lambda: None)
    monkeypatch.setattr(sched_mod, "audit", lambda *a, **k: None)

    async def _cancel(*a, **k):
        raise asyncio.CancelledError
    monkeypatch.setattr(sched_mod.asyncio, "sleep", _cancel)

    asyncio.run(s._leader_loop())
    assert s._is_leader is True


def test_leader_loop_loses_leadership(monkeypatch):
    s = RecipeScheduler()
    s._is_leader = True

    class _Worker:
        def __init__(self):
            self.stopped = False

        async def stop(self):
            self.stopped = True

    worker = _Worker()
    s._streaming_worker = worker
    monkeypatch.setattr(s, "_try_acquire_leader", lambda: False)
    monkeypatch.setattr(sched_mod, "audit", lambda *a, **k: None)

    async def _cancel(*a, **k):
        raise asyncio.CancelledError
    monkeypatch.setattr(sched_mod.asyncio, "sleep", _cancel)

    asyncio.run(s._leader_loop())
    assert s._is_leader is False and worker.stopped is True


# ===========================================================================
# start / shutdown lifecycle
# ===========================================================================

def test_start_disabled_without_aps(monkeypatch):
    monkeypatch.setattr(sched_mod, "_HAS_APS", False)
    s = RecipeScheduler()
    s.start()
    assert s._scheduler is None


def test_start_idempotent_when_already_started():
    s = RecipeScheduler()
    s._scheduler = _FakeAPS()
    s.start()  # _scheduler not None → returns immediately
    assert isinstance(s._scheduler, _FakeAPS)


def test_outcome_resolver_db_branch(monkeypatch):
    s = RecipeScheduler()
    s._is_leader = True
    monkeypatch.setattr("web.auth._db_writable", lambda: True)
    monkeypatch.setattr("web.auth._select_columns",
                        lambda *a, **k: [{"user_id": "u1"}, {"user_id": "u2"}])
    seen = []
    monkeypatch.setattr("agenticwhales.outcomes.resolve_outcomes_for_user",
                        lambda uid, limit=200: seen.append(uid) or 1)
    s._run_outcome_resolver()
    assert set(seen) == {"u1", "u2"}


def test_outcome_resolver_db_scan_error(monkeypatch):
    s = RecipeScheduler()
    s._is_leader = True
    monkeypatch.setattr("web.auth._db_writable", lambda: True)
    def _boom(*a, **k):
        raise RuntimeError("scan failed")
    monkeypatch.setattr("web.auth._select_columns", _boom)
    monkeypatch.setattr("agenticwhales.outcomes.resolve_outcomes_for_user",
                        lambda uid, limit=200: 0)
    s._run_outcome_resolver()  # scan error caught, no users → no raise


def test_prompt_evals_db_branch(monkeypatch):
    s = RecipeScheduler()
    s._is_leader = True
    monkeypatch.setattr("web.auth._db_writable", lambda: True)
    monkeypatch.setattr("web.auth._select_columns",
                        lambda *a, **k: [{"user_id": "u1"}])

    class _Res:
        promoted = False
    monkeypatch.setattr("agenticwhales.adaptive.evaluate_prompt_variant",
                        lambda uid, variant, scorer: _Res())
    s._run_prompt_evals()


def test_start_streaming_worker_swallows_start_error(monkeypatch):
    s = RecipeScheduler()

    class _Worker:
        def __init__(self, fire_recipe, is_leader_fn):
            pass

        async def start(self, active):
            raise RuntimeError("alpaca down")

    monkeypatch.setattr("web.streaming_worker.StreamingWorker", _Worker)
    monkeypatch.setattr(sched_mod.recipes_mod, "list_all_active", lambda: [])
    asyncio.run(s._start_streaming_worker())
    assert s._streaming_worker is None  # start failed → not retained


def test_start_streaming_worker_list_active_error(monkeypatch):
    s = RecipeScheduler()

    class _Worker:
        def __init__(self, fire_recipe, is_leader_fn):
            self.started = None

        async def start(self, active):
            self.started = active

    monkeypatch.setattr("web.streaming_worker.StreamingWorker", _Worker)
    monkeypatch.setattr(sched_mod.recipes_mod, "list_all_active",
                        lambda: (_ for _ in ()).throw(RuntimeError("db")))
    asyncio.run(s._start_streaming_worker())
    assert s._streaming_worker is not None  # started with empty bindings


def test_leader_loop_already_leader_noop(monkeypatch):
    s = RecipeScheduler()
    s._is_leader = True  # already leader → elif acquired: pass branch
    monkeypatch.setattr(s, "_try_acquire_leader", lambda: True)
    monkeypatch.setattr(sched_mod, "audit", lambda *a, **k: None)

    async def _cancel(*a, **k):
        raise asyncio.CancelledError
    monkeypatch.setattr(sched_mod.asyncio, "sleep", _cancel)
    asyncio.run(s._leader_loop())
    assert s._is_leader is True


def test_start_creates_scheduler(monkeypatch):
    s = RecipeScheduler()

    async def _dummy():
        return None
    monkeypatch.setattr(s, "_leader_loop", _dummy)

    async def go():
        s.start()
        assert s._scheduler is not None
        await s.shutdown()

    asyncio.run(go())


def test_shutdown_stops_worker_and_scheduler(monkeypatch):
    s = RecipeScheduler()

    class _Task:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    class _Worker:
        def __init__(self):
            self.stopped = False

        async def stop(self):
            self.stopped = True

    task = _Task()
    worker = _Worker()
    fake_sched = _FakeAPS()
    s._heartbeat_task = task
    s._streaming_worker = worker
    s._scheduler = fake_sched
    asyncio.run(s.shutdown())
    assert task.cancelled and worker.stopped and fake_sched.shut
    assert s._streaming_worker is None
