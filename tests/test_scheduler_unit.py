"""Unit coverage for web/scheduler.py — interval parsing, trigger construction,
job-CRUD no-op guards, and the _do_fire gate ladder. No real APScheduler jobs
run; the fire path is driven directly with mocked recipes/auth/calendar.
"""

from __future__ import annotations

import pytest

from agenticwhales.agents.schemas import Recipe, RecipeStatus, ScheduleKind
from web import scheduler as sched_mod
from web.scheduler import RecipeScheduler, _parse_interval_seconds


@pytest.fixture(autouse=True)
def _wipe():
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


# ---- _parse_interval_seconds ----

@pytest.mark.parametrize("expr,secs", [
    ("30s", 30), ("15m", 900), ("6h", 21600), ("1d", 86400), ("45", 45),
])
def test_parse_interval_ok(expr, secs):
    assert _parse_interval_seconds(expr) == secs


@pytest.mark.parametrize("bad", ["", "  ", "abc", "10x", "m", "1.5h"])
def test_parse_interval_bad(bad):
    with pytest.raises(ValueError):
        _parse_interval_seconds(bad)


# ---- job CRUD: _scheduler is None → no-op / None ----

def test_add_returns_none_without_scheduler():
    s = RecipeScheduler()
    assert s._scheduler is None
    assert s.add(_recipe(schedule_kind=ScheduleKind.CRON, schedule_expr="0 9 * * *")) is None


def test_add_manual_recipe_is_none():
    s = RecipeScheduler()
    s._scheduler = object()  # truthy; manual short-circuits before use
    assert s.add(_recipe(schedule_kind=ScheduleKind.MANUAL)) is None


def test_remove_pause_resume_noop_without_scheduler():
    s = RecipeScheduler()
    s.remove("r1")
    s.pause("r1")
    s.resume("r1")  # no raise


# ---- _trigger_for ----

def test_trigger_for_cron_and_missing_expr():
    s = RecipeScheduler()
    assert s._trigger_for(_recipe(schedule_kind=ScheduleKind.CRON,
                                  schedule_expr="0 9 * * *")) is not None
    assert s._trigger_for(_recipe(schedule_kind=ScheduleKind.CRON,
                                  schedule_expr=None)) is None


def test_trigger_for_interval_and_bad_expr():
    s = RecipeScheduler()
    assert s._trigger_for(_recipe(schedule_kind=ScheduleKind.INTERVAL,
                                  schedule_expr="15m")) is not None
    assert s._trigger_for(_recipe(schedule_kind=ScheduleKind.INTERVAL,
                                  schedule_expr="garbage")) is None


def test_trigger_for_manual_is_none():
    s = RecipeScheduler()
    assert s._trigger_for(_recipe(schedule_kind=ScheduleKind.MANUAL)) is None


# ---- trigger_now ----

def test_trigger_now_missing_recipe_raises(monkeypatch):
    s = RecipeScheduler()
    monkeypatch.setattr(sched_mod.recipes_mod, "load", lambda rid: None)
    with pytest.raises(ValueError):
        s.trigger_now("nope")


def test_trigger_now_fires(monkeypatch):
    s = RecipeScheduler()
    rec = _recipe()
    monkeypatch.setattr(sched_mod.recipes_mod, "load", lambda rid: rec)
    fired = {}
    monkeypatch.setattr(s, "_do_fire",
                        lambda r, fid, **kw: fired.update(rid=r.id, skip=kw.get("skip_market_hours")))
    fid = s.trigger_now("r1")
    assert fired["rid"] == "r1" and fired["skip"] is True
    assert isinstance(fid, str)


# ---- _do_fire gate ladder ----

@pytest.fixture
def fire_env(monkeypatch):
    calls = {"reset": 0, "touch": 0, "bump": 0, "status": [], "metrics": [], "risk": []}
    monkeypatch.setattr(sched_mod.recipes_mod, "reset_failures",
                        lambda rid: calls.__setitem__("reset", calls["reset"] + 1))
    monkeypatch.setattr(sched_mod.recipes_mod, "touch_last_run",
                        lambda rid: calls.__setitem__("touch", calls["touch"] + 1))
    monkeypatch.setattr(sched_mod.recipes_mod, "update_status",
                        lambda rid, st: calls["status"].append(st))
    monkeypatch.setattr(sched_mod.recipes_mod, "bump_failures",
                        lambda rid: (calls.__setitem__("bump", calls["bump"] + 1), 99)[1])
    monkeypatch.setattr(sched_mod, "is_market_open", lambda code, when: True)
    s = RecipeScheduler()
    monkeypatch.setattr(s, "_record_metric", lambda st: calls["metrics"].append(st))
    monkeypatch.setattr(s, "_emit_risk_event", lambda rec, rule, det: calls["risk"].append(rule))
    return s, calls


def _allow_budget(monkeypatch):
    monkeypatch.setattr("web.auth.load_recipe_usage", lambda rid, day: None)
    monkeypatch.setattr("web.auth.load_risk_limits", lambda uid: {"daily_spend_cap_usd": 100.0})
    monkeypatch.setattr("web.auth.load_user_spend", lambda uid, day: 0.0)


def test_do_fire_skips_inactive(fire_env):
    s, calls = fire_env
    s._do_fire(_recipe(status=RecipeStatus.PAUSED), "f1", skip_market_hours=True)
    assert calls["metrics"] == ["skipped"]


def test_do_fire_skips_market_closed(fire_env, monkeypatch):
    s, calls = fire_env
    monkeypatch.setattr(sched_mod, "is_market_open", lambda code, when: False)
    s._do_fire(_recipe(market_hours_only=True), "f1", skip_market_hours=False)
    assert calls["metrics"] == ["skipped"]


def test_do_fire_budget_gate(fire_env, monkeypatch):
    s, calls = fire_env
    monkeypatch.setattr("web.auth.load_recipe_usage", lambda rid, day: {"token_cost_usd": 99.0})
    s._do_fire(_recipe(max_daily_token_cost_usd=5.0), "f1", skip_market_hours=True)
    assert calls["metrics"] == ["budget"] and calls["risk"] == ["budget"]


def test_do_fire_user_spend_cap(fire_env, monkeypatch):
    s, calls = fire_env
    monkeypatch.setattr("web.auth.load_recipe_usage", lambda rid, day: None)
    monkeypatch.setattr("web.auth.load_risk_limits", lambda uid: {"daily_spend_cap_usd": 1.0})
    monkeypatch.setattr("web.auth.load_user_spend", lambda uid, day: 5.0)
    s._do_fire(_recipe(), "f1", skip_market_hours=True)
    assert calls["risk"] == ["user_spend_cap"]


def test_do_fire_consecutive_failure_autopause(fire_env, monkeypatch):
    s, calls = fire_env
    _allow_budget(monkeypatch)
    s._do_fire(_recipe(consecutive_failures=5), "f1", skip_market_hours=True)
    assert RecipeStatus.FAILED in calls["status"]
    assert "failures" in calls["risk"]


def test_do_fire_dry_run_no_callback(fire_env, monkeypatch):
    s, calls = fire_env
    _allow_budget(monkeypatch)
    s._run_session = None
    s._do_fire(_recipe(), "f1", skip_market_hours=True)
    assert calls["metrics"] == ["ok"]
    assert calls["reset"] == 1 and calls["touch"] == 1


def test_do_fire_runs_session_callback(fire_env, monkeypatch):
    s, calls = fire_env
    _allow_budget(monkeypatch)
    ran = {}
    s._run_session = lambda rec, fid: ran.update(rid=rec.id, fid=fid)
    s._do_fire(_recipe(), "f1", skip_market_hours=True)
    assert ran == {"rid": "r1", "fid": "f1"}
    assert calls["metrics"] == ["ok"] and calls["reset"] == 1


def test_do_fire_callback_failure_bumps(fire_env, monkeypatch):
    s, calls = fire_env
    _allow_budget(monkeypatch)
    def _boom(rec, fid):
        raise RuntimeError("fire failed")
    s._run_session = _boom
    s._do_fire(_recipe(), "f1", skip_market_hours=True)
    assert calls["bump"] == 1 and calls["metrics"] == ["failed"]
    assert RecipeStatus.FAILED in calls["status"]  # bump stub returns 99 ≥ MAX


# ---- _wrap_fire concurrency guard ----

def test_wrap_fire_missing_recipe(monkeypatch):
    s = RecipeScheduler()
    monkeypatch.setattr(sched_mod.recipes_mod, "load", lambda rid: None)
    s._wrap_fire("r1")
    assert "r1" in s._inflight


def test_wrap_fire_fires_when_lock_free(monkeypatch):
    s = RecipeScheduler()
    rec = _recipe()
    monkeypatch.setattr(sched_mod.recipes_mod, "load", lambda rid: rec)
    fired = {}
    monkeypatch.setattr(s, "_do_fire", lambda r, fid, **kw: fired.update(rid=r.id))
    s._wrap_fire("r1")
    assert fired["rid"] == "r1"


def test_wrap_fire_skips_when_locked(monkeypatch):
    import threading
    s = RecipeScheduler()
    monkeypatch.setattr(sched_mod.recipes_mod, "load", lambda rid: _recipe())
    held = threading.Lock()
    held.acquire()
    s._inflight["r1"] = held
    called = {"n": 0}
    monkeypatch.setattr(s, "_do_fire", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    s._wrap_fire("r1")
    assert called["n"] == 0


# ---- _emit_risk_event ----

def test_emit_risk_event_inserts_row():
    from web import auth
    s = RecipeScheduler()
    s._emit_risk_event(_recipe(), "budget", {"cap_usd": 5.0})
    rows = auth.list_risk_events("u1")
    assert len(rows) == 1 and rows[0]["rule"] == "budget"
