"""Analyst Desk reposition: briefs end at research synthesis (no rating, no
sizing, no trading-tail nodes), the API masks decision artifacts, and the
daily tier quota is enforced server-side."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from web import auth, server


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    server._runners.clear()
    yield
    auth._reset_memstore_for_tests()
    server._runners.clear()


# --------------------------------------------------------------------------- #
# Graph structure: brief mode has no trading tail AT ALL
# --------------------------------------------------------------------------- #

def _graph_setup():
    from agenticwhales.graph.setup import GraphSetup
    logic = MagicMock()
    return GraphSetup(
        quick_thinking_llm=MagicMock(),
        deep_thinking_llm=MagicMock(),
        tool_nodes={k: MagicMock() for k in
                    ("market", "quant", "social", "news", "fundamentals")},
        conditional_logic=logic,
    )


def test_brief_graph_contains_no_trading_nodes():
    wf = _graph_setup().setup_graph(["market"], stop_after_research=True)
    nodes = set(wf.nodes)
    assert "Research Manager" in nodes
    assert "Bull Researcher" in nodes and "Bear Researcher" in nodes
    for forbidden in ("Trader", "Aggressive Analyst", "Conservative Analyst",
                      "Neutral Analyst", "Portfolio Manager"):
        assert forbidden not in nodes, f"brief graph must not contain {forbidden}"
    # Research Manager is terminal.
    assert ("Research Manager", "__end__") in set(wf.edges)
    wf.compile()  # structurally valid


def test_full_graph_unchanged():
    wf = _graph_setup().setup_graph(["market"], stop_after_research=False)
    nodes = set(wf.nodes)
    for required in ("Trader", "Portfolio Manager", "Aggressive Analyst"):
        assert required in nodes
    assert ("Portfolio Manager", "__end__") in set(wf.edges)
    wf.compile()


# --------------------------------------------------------------------------- #
# Research Manager brief node: synthesis, no rating machinery
# --------------------------------------------------------------------------- #

class _FakeLLM:
    def __init__(self, reply="Synthesis: the evidence converges on ..."):
        self.reply = reply
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        out = MagicMock()
        out.content = self.reply
        return out


def _debate_state():
    return {
        "company_of_interest": "AAPL",
        "market_snapshot": "",
        "quant_radar": "",
        "current_position": "",
        "investment_debate_state": {
            "history": "Bull said X. Bear said Y.",
            "bull_history": "X", "bear_history": "Y", "count": 2,
        },
    }


def test_brief_research_manager_prompt_and_output():
    from agenticwhales.agents.managers.research_manager import create_research_manager
    llm = _FakeLLM()
    node = create_research_manager(llm, brief=True)
    out = node(_debate_state())
    assert out["investment_plan"].startswith("Synthesis")
    prompt = llm.prompts[0]
    # The brief prompt forbids decisions and carries no rating scale.
    assert "must not issue one" in prompt
    assert "no rating, no recommendation" in prompt
    assert "Rating Scale" not in prompt
    assert "**Buy**" not in prompt and "**Sell**" not in prompt


def test_full_research_manager_still_structured():
    from agenticwhales.agents.managers import research_manager as rm
    node = rm.create_research_manager(MagicMock(), brief=False)
    assert node.__name__ == "research_manager_node"
    brief_node = rm.create_research_manager(MagicMock(), brief=True)
    assert brief_node.__name__ == "research_manager_brief_node"


# --------------------------------------------------------------------------- #
# API masking: briefs never expose pm_decision / final_trade_decision
# --------------------------------------------------------------------------- #

def _session(**over):
    s = {"id": "s1", "ticker": "AAPL", "analysis_date": "2024-01-02",
         "status": "completed", "created_at": time.time(),
         "session_type": "brief",
         "pm_decision": {"rating": "BUY", "expected_return_pct": 5.0},
         "report_sections": {"investment_plan": "synthesis...",
                             "final_trade_decision": "BUY..."}}
    s.update(over)
    return s


def test_summary_masks_brief_pm_decision():
    out = server._summary(_session())
    assert out["session_type"] == "brief"
    assert out["pm_decision"] is None
    # Full (recipe/lab/legacy) sessions keep the verdict.
    full = server._summary(_session(session_type="full"))
    assert full["pm_decision"]["rating"] == "BUY"
    legacy = server._summary({k: v for k, v in _session().items()
                              if k != "session_type"})
    assert legacy["session_type"] == "full"


def test_detail_masks_brief_artifacts():
    masked = server._mask_brief(_session())
    assert "pm_decision" not in masked
    assert "final_trade_decision" not in masked["report_sections"]
    assert masked["report_sections"]["investment_plan"]  # synthesis kept
    full = server._mask_brief(_session(session_type="full"))
    assert full["pm_decision"]["rating"] == "BUY"


# --------------------------------------------------------------------------- #
# Server-side quota enforcement
# --------------------------------------------------------------------------- #

def _seed_session(uid, *, recipe_id=None, age_seconds=0.0):
    auth.save_session({"id": f"q{time.time_ns()}", "user_id": uid,
                       "ticker": "AAPL", "analysis_date": "2024-01-02",
                       "status": "completed",
                       "created_at": time.time() - age_seconds,
                       "recipe_id": recipe_id})


def test_quota_novice_cap_and_recipe_exclusion():
    uid = "quota-user-1"
    for _ in range(3):
        _seed_session(uid)
    _seed_session(uid, recipe_id="r1")          # recipe fires don't count
    _seed_session(uid, age_seconds=2 * 86400)   # yesterday doesn't count
    assert auth.count_user_analyses_today(uid) == 3
    allowed, info = auth.check_analysis_quota(uid)
    assert not allowed and info["cap"] == 3 and info["tier"] == "novice"


def test_quota_master_unlimited_and_anonymous_exempt():
    uid = "quota-user-2"
    auth._memstore[("profiles", uid)] = {"tier": "master"}
    for _ in range(10):
        _seed_session(uid)
    allowed, info = auth.check_analysis_quota(uid)
    assert allowed and info["cap"] is None
    assert auth.check_analysis_quota(auth.ANONYMOUS_USER_ID)[0] is True


def test_create_session_429_over_quota(monkeypatch):
    class _FakeRunner:
        def __init__(self, session=None, loop=None, **kw):
            self.session = session or {}

        def start(self):
            pass

    monkeypatch.setattr(server, "SessionRunner", _FakeRunner)
    server.app.dependency_overrides[server.get_current_user_id] = lambda: "quota-user-3"
    try:
        c = TestClient(server.app)
        payload = {"ticker": "AAPL", "analysis_date": "2024-01-02",
                   "llm_provider": "google", "quick_think_llm": "g",
                   "deep_think_llm": "g", "analysts": ["market"]}
        for i in range(3):
            payload["ticker"] = f"TK{i}"        # distinct tickers: no cache hits
            assert c.post("/api/sessions", json=payload).status_code == 200
        payload["ticker"] = "TK99"
        r = c.post("/api/sessions", json=payload)
        assert r.status_code == 429
        assert "3/3" in r.json()["detail"]
    finally:
        server.app.dependency_overrides.pop(server.get_current_user_id, None)


def test_config_flags_provider_key_presence(monkeypatch):
    c = TestClient(server.app)
    # conftest sets placeholder keys, so google reads as configured…
    provs = {p["key"]: p["configured"] for p in c.get("/api/config").json()["providers"]}
    assert provs["google"] is True
    # …and dropping the keys flips the flag (a fresh clone with no .env).
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provs = {p["key"]: p["configured"] for p in c.get("/api/config").json()["providers"]}
    assert provs["google"] is False


def test_unconfigured_provider_fails_fast_with_clear_400(monkeypatch):
    """A missing server key must be a pre-flight 400 — never a mid-run pydantic
    error after the quota slot is spent (the bug a fresh clone exposed)."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    c = TestClient(server.app)
    payload = {"ticker": "AAPL", "analysis_date": "2024-01-02",
               "llm_provider": "google", "quick_think_llm": "g",
               "deep_think_llm": "g", "analysts": ["market"]}
    r = c.post("/api/sessions", json=payload)
    assert r.status_code == 400
    assert "GOOGLE_API_KEY" in r.json()["detail"]
    # No session was created — the quota slot survives.
    assert auth.count_user_analyses_today(auth.ANONYMOUS_USER_ID) == 0
    # Batches get the same gate.
    r = c.post("/api/batches", json={**{k: v for k, v in payload.items() if k != "ticker"},
                                     "tickers": ["A", "B"]})
    assert r.status_code == 400


def test_batch_quota_counts_tickers(monkeypatch):
    uid = "quota-user-4"
    server.app.dependency_overrides[server.get_current_user_id] = lambda: uid
    try:
        c = TestClient(server.app)
        # A 4-ticker basket exceeds the novice cap of 3 outright.
        r = c.post("/api/batches", json={
            "tickers": ["A", "B", "C", "D"], "analysis_date": "2024-01-02",
            "llm_provider": "google", "quick_think_llm": "g",
            "deep_think_llm": "g", "analysts": ["market"]})
        assert r.status_code == 429
    finally:
        server.app.dependency_overrides.pop(server.get_current_user_id, None)
