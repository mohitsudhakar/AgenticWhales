"""HTTP coverage for the many small/stub web/server.py routes.

Runs offline (conftest strips Supabase env), so get_current_user_id resolves
to the shared anonymous user. Owner-scoped write paths are exercised by
overriding the get_current_user_id dependency with a concrete user id.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from web import auth, server


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


@pytest.fixture
def client():
    c = TestClient(server.app)
    c.follow_redirects = False
    return c


@pytest.fixture
def user_client():
    """A client whose requests resolve to a concrete signed-in user."""
    server.app.dependency_overrides[server.get_current_user_id] = lambda: "u-test"
    c = TestClient(server.app)
    c.follow_redirects = False
    yield c
    server.app.dependency_overrides.pop(server.get_current_user_id, None)


# ---------------------------------------------------------------------------
# static pages + health
# ---------------------------------------------------------------------------

def test_root_fund_analyze_usage_pages(client):
    for path in ("/", "/fund", "/analyze", "/usage"):
        assert client.get(path).status_code == 200


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_readyz(client):
    r = client.get("/readyz")
    assert r.status_code in (200, 503)
    assert "ready" in r.json()


def test_config_endpoint(client):
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert "providers" in body and "analysts" in body and "defaults" in body


# ---------------------------------------------------------------------------
# portfolio
# ---------------------------------------------------------------------------

def test_portfolio_get_and_put(client, monkeypatch):
    saved = {}
    monkeypatch.setattr(server.portfolio, "load_all", lambda: saved)
    monkeypatch.setattr(server.portfolio, "save_all", lambda p: saved.update(p))
    assert client.get("/api/portfolio").json() == {"positions": {}}
    r = client.put("/api/portfolio", json={"positions": {"AAPL": {"qty": 10}}})
    assert r.status_code == 200
    assert r.json()["positions"]["AAPL"]["qty"] == 10


# ---------------------------------------------------------------------------
# sessions — 404 paths (anonymous owns nothing persisted)
# ---------------------------------------------------------------------------

def test_get_missing_session_404(client):
    assert client.get("/api/sessions/nope").status_code == 404


def test_delete_missing_session_404(client):
    assert client.delete("/api/sessions/nope").status_code == 404


def test_cancel_missing_session_404(client):
    assert client.post("/api/sessions/nope/cancel").status_code == 404


def test_session_ablation_404(client):
    assert client.get("/api/sessions/nope/ablation").status_code == 404


def test_list_sessions_empty(client):
    assert client.get("/api/sessions").json() == []


# ---------------------------------------------------------------------------
# batches
# ---------------------------------------------------------------------------

def test_list_batches_empty(client):
    assert client.get("/api/batches").json() == []


def test_get_missing_batch_404(client):
    assert client.get("/api/batches/nope").status_code == 404


def test_delete_missing_batch_404(client):
    assert client.delete("/api/batches/nope").status_code == 404


def test_cancel_missing_batch_404(client):
    assert client.post("/api/batches/nope/cancel").status_code == 404


# ---------------------------------------------------------------------------
# paper account / positions / orders / calibration / conviction
# ---------------------------------------------------------------------------

def test_paper_account_default(client):
    r = client.get("/api/paper/account")
    assert r.status_code == 200
    assert r.json()["nav"] == 100000.0


def test_paper_positions_orders_empty(client):
    assert client.get("/api/paper/positions").json() == []
    assert client.get("/api/paper/orders").json() == []


def test_paper_calibration_stub(client):
    assert client.get("/api/paper/calibration").json() == {"brier": None, "samples": 0}


def test_paper_conviction_empty(client):
    assert client.get("/api/paper/conviction").json() == []


def test_conviction_timeseries_empty(client):
    assert client.get("/api/paper/conviction/timeseries").json()["points"] == []


def test_resolve_outcomes_stub(client):
    assert client.post("/api/paper/outcomes/resolve").json() == {"resolved": 0}


# ---------------------------------------------------------------------------
# risk
# ---------------------------------------------------------------------------

def test_risk_events_empty(client):
    assert client.get("/api/risk/events").json() == []


def test_risk_limits_get(client):
    r = client.get("/api/risk/limits")
    assert r.status_code == 200


def test_risk_limits_put_requires_user(client):
    # anonymous → 401
    assert client.put("/api/risk/limits", json={"max_position_pct": 0.2}).status_code == 401


def test_risk_limits_put_as_user(user_client):
    r = user_client.put("/api/risk/limits", json={"max_position_pct": 0.25})
    assert r.status_code == 200 and r.json()["max_position_pct"] == 0.25


def test_kill_switch_as_user(user_client):
    r = user_client.post("/api/risk/kill-switch", json={"enabled": True})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# recipes — list + sub-routes
# ---------------------------------------------------------------------------

def test_recipes_list_empty(client):
    assert client.get("/api/recipes").json() == []


def test_recipe_subroutes_404_when_missing(user_client):
    assert user_client.delete("/api/recipes/nope").status_code == 404
    assert user_client.post("/api/recipes/nope/trigger-now").status_code == 404
    assert user_client.post("/api/recipes/nope/pause").status_code == 404
    assert user_client.post("/api/recipes/nope/resume").status_code == 404
    assert user_client.post("/api/recipes/nope/kill").status_code == 404


def test_recipe_lifecycle_as_user(user_client):
    payload = {
        "name": "R", "tickers": ["AAPL"], "analysts": ["market"],
        "llm_provider": "google", "quick_model": "gemini-3-flash-preview",
        "deep_model": "gemini-3.1-pro-preview",
        "bull_model": "gemini-3.1-pro-preview", "bear_model": "deepseek-v4",
        "schedule_kind": "manual", "output_policy": "notify",
    }
    rid = user_client.post("/api/recipes", json=payload).json()["id"]
    assert user_client.post(f"/api/recipes/{rid}/pause").json()["status"] == "paused"
    assert user_client.post(f"/api/recipes/{rid}/resume").json()["status"] == "active"
    assert user_client.post(f"/api/recipes/{rid}/kill").json()["status"] == "killed"
    assert user_client.get(f"/api/recipes/{rid}/usage").status_code == 200
    assert user_client.get(f"/api/recipes/{rid}/sessions").status_code == 200
    assert user_client.delete(f"/api/recipes/{rid}").json()["deleted"] is True


# ---------------------------------------------------------------------------
# journal
# ---------------------------------------------------------------------------

def test_journal_list_empty_anon(client):
    assert client.get("/api/journal/entries").json() == []


def test_journal_post_requires_user(client):
    assert client.post("/api/journal/entries", json={"body": "hi"}).status_code == 401


def test_journal_crud_as_user(user_client):
    created = user_client.post("/api/journal/entries", json={"body": "first", "kind": "note"})
    assert created.status_code == 200
    eid = created.json()["id"]
    listed = user_client.get("/api/journal/entries").json()
    assert any(e["id"] == eid for e in listed)
    upd = user_client.put(f"/api/journal/entries/{eid}", json={"body": "edited", "kind": "reflection"})
    assert upd.json()["body"] == "edited"
    assert user_client.delete(f"/api/journal/entries/{eid}").json()["deleted"] is True


def test_journal_put_delete_missing_404(user_client):
    assert user_client.put("/api/journal/entries/nope", json={"body": "x"}).status_code == 404
    assert user_client.delete("/api/journal/entries/nope").status_code == 404


# ---------------------------------------------------------------------------
# ask / behavioral / disagreement / prompt-evals / calibration stubs
# ---------------------------------------------------------------------------

def test_ask_templates(client):
    tpls = client.get("/api/journal/ask/templates").json()
    assert isinstance(tpls, list) and tpls[0]["template_id"] == "best_call"


def test_ask_answer_and_404(client):
    ok = client.post("/api/journal/ask", json={"template_id": "best_call"})
    assert ok.status_code == 200 and ok.json()["confidence"] == "low"
    assert client.post("/api/journal/ask", json={"template_id": "nope"}).status_code == 404


def test_behavioral_stubs(client):
    assert client.get("/api/behavioral/findings").json() == []
    upd = client.post("/api/behavioral/findings/update",
                      json={"pattern": "tilt", "created_at": "2024-01-01", "action": "dismiss"})
    assert upd.json() == {"ok": True, "action": "dismiss"}
    assert client.post("/api/behavioral/scan").json() == {"new_findings": 0}


def test_disagreement_and_prompt_evals_empty(client):
    assert client.get("/api/disagreement").json() == []
    assert client.get("/api/prompt-evals").json() == []


def test_calibration_stubs(client):
    assert client.get("/api/calibration").json()["available"] is False
    assert client.post("/api/calibration/opt-in", json={"apply": True})\
        .json() == {"applied": True, "regime": "all"}
    assert client.post("/api/calibration/fit").json()["fitted"] is False


# ---------------------------------------------------------------------------
# usage admin gate (anonymous / non-admin → 403)
# ---------------------------------------------------------------------------

def test_usage_me_forbidden_offline(client):
    # require_admin refuses when Supabase isn't configured.
    assert client.get("/api/usage/me").status_code == 403


def test_usage_dashboard_forbidden_offline(client):
    assert client.get("/api/usage/dashboard").status_code == 403
