"""Standing briefs (Pro): config API + weekly cron firing brief sessions."""

import pytest
from fastapi.testclient import TestClient

import web.coach_api as coach_api
import web.scheduler as scheduler_mod
import web.server as server
from web import auth
from web.scheduler import RecipeScheduler


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture(autouse=True)
def _isolate_standing_briefs():
    """The cron walks EVERY active config in the store — drop rows between
    tests so one test's config can't fire in another's cron run."""
    for k in [k for k in list(auth._memstore) if k[0] == "coach_standing_briefs"]:
        auth._memstore.pop(k, None)
    yield


def _signed_in(uid):
    server.app.dependency_overrides[auth.get_current_user_id] = lambda: uid
    server.app.dependency_overrides[coach_api.optional_user_id] = lambda: uid


def test_standing_brief_config_roundtrip(client):
    _signed_in("sb-user-1")
    try:
        assert client.get("/api/coach/standing-brief").json()["configured"] is False
        r = client.post("/api/coach/standing-brief",
                        json={"tickers": ["aapl", "msft"], "active": True}).json()
        assert r["tickers"] == ["AAPL", "MSFT"] and r["active"] is True
        g = client.get("/api/coach/standing-brief").json()
        assert g["configured"] and g["cadence"] == "weekly"
        # Validation: empty active config, too many, junk ticker.
        assert client.post("/api/coach/standing-brief",
                           json={"tickers": [], "active": True}).status_code == 400
        assert client.post("/api/coach/standing-brief",
                           json={"tickers": list("ABCDEF"), "active": True}).status_code == 400
        assert client.post("/api/coach/standing-brief",
                           json={"tickers": ["not a ticker!"], "active": True}).status_code == 400
    finally:
        server.app.dependency_overrides.clear()


class _StubRunner:
    fired = []
    def __init__(self, session, loop=None):
        self.session = session
    def start(self):
        self.session["status"] = "completed"
        _StubRunner.fired.append(self.session)


def _prep_cron(monkeypatch, leader=True):
    sched = RecipeScheduler()
    sched._is_leader = leader
    import web.runner as runner_mod
    monkeypatch.setattr(runner_mod, "SessionRunner", _StubRunner)
    monkeypatch.setattr(server, "provider_configured", lambda p: True)
    _StubRunner.fired = []
    return sched


def test_cron_fires_brief_per_ticker(monkeypatch):
    sched = _prep_cron(monkeypatch)
    auth.upsert_standing_brief("sb-cron-1", {"tickers": ["AAPL", "NVDA"], "active": True})
    sched._run_standing_briefs()
    assert len(_StubRunner.fired) == 2
    s = _StubRunner.fired[0]
    assert s["session_type"] == "brief" and s["user_id"] == "sb-cron-1"
    assert s["origin"] == "standing_brief"
    # Idempotent within the day: a second fire is a no-op.
    sched._run_standing_briefs()
    assert len(_StubRunner.fired) == 2


def test_cron_skips_non_leader_and_inactive(monkeypatch):
    sched = _prep_cron(monkeypatch, leader=False)
    auth.upsert_standing_brief("sb-cron-2", {"tickers": ["AAPL"], "active": True})
    sched._run_standing_briefs()
    assert _StubRunner.fired == []
    sched._is_leader = True
    auth.upsert_standing_brief("sb-cron-2", {"active": False})
    sched._run_standing_briefs()
    assert _StubRunner.fired == []


def test_cron_respects_entitlement_when_enforced(monkeypatch):
    sched = _prep_cron(monkeypatch)
    monkeypatch.setenv("AGENTICWHALES_BILLING_ENFORCED", "1")
    auth.upsert_standing_brief("sb-cron-3", {"tickers": ["AAPL"], "active": True})
    auth._memstore[("profiles", "sb-cron-3")] = {"id": "sb-cron-3", "tier": "novice"}
    sched._run_standing_briefs()
    assert _StubRunner.fired == []          # Free plan: gated
    auth._memstore[("profiles", "sb-cron-3")] = {"id": "sb-cron-3", "tier": "master"}
    sched._run_standing_briefs()
    assert len(_StubRunner.fired) == 1      # Pro: fires
