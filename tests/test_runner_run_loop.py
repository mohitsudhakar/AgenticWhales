"""Coverage for web/runner.py's _run streaming loop driven by a fake graph.
The AgenticWhalesGraph, snapshot fetch, cost middleware, and post-completion
hooks are all faked — no LLM, no graph compile, no network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from web import auth, runner
from web.runner import SessionRunner, build_session


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(auth, "_db_writable", lambda: False)
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


class _FakeLoop:
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


def _runner(user="u1", **over):
    form = {"ticker": "aapl", "analysis_date": "2024-01-02", "analysts": ["market"],
            "llm_provider": "google", "quick_think_llm": "q", "deep_think_llm": "d"}
    s = build_session(form)
    s["user_id"] = user
    s.update(over)
    r = SessionRunner(s, _FakeLoop())
    r.events = []
    r._broadcast = r.events.append
    return r


def _install_fake_graph(monkeypatch, chunks):
    class _FakeGraph:
        def __init__(self, analysts, config=None, debug=False, callbacks=None):
            self.user_id = None
            self.propagator = SimpleNamespace(
                create_initial_state=lambda *a, **k: {"company_of_interest": "AAPL"},
                get_graph_args=lambda callbacks=None: {},
            )
            self.graph = SimpleNamespace(stream=lambda init, **kw: iter(chunks))

    monkeypatch.setattr(runner, "AgenticWhalesGraph", _FakeGraph)
    monkeypatch.setattr(runner, "fetch_snapshot_block",
                        lambda t, d: "Latest close: $100.0")


def _patch_tail(monkeypatch, r):
    """Stub the post-completion side effects so _run stays isolated."""
    import agenticwhales.llm_clients.cost_middleware as cm
    import agenticwhales.behavioral as bh
    monkeypatch.setattr(cm, "record_fire_cost", lambda **k: None)
    monkeypatch.setattr(bh, "scan_user", lambda uid: None)
    hooks = {"post": 0, "disagree": 0}
    monkeypatch.setattr(r, "_post_decision_hook",
                        lambda: hooks.__setitem__("post", hooks["post"] + 1))
    monkeypatch.setattr(r, "_record_disagreement_and_maybe_inject_classical",
                        lambda: hooks.__setitem__("disagree", hooks["disagree"] + 1))
    return hooks


def _full_chunks():
    return [
        {"messages": [AIMessage(content="market thoughts", id="m1",
                                tool_calls=[{"name": "get_news", "args": {"q": "x"},
                                             "id": "t1"}])],
         "market_report": "the market read"},
        {"investment_debate_state": {"bull_history": "Bull: buy",
                                     "bear_history": "Bear: sell",
                                     "judge_decision": "Manager: hold", "count": 4}},
        {"trader_investment_plan": "trader plan"},
        {"risk_debate_state": {"aggressive_history": "agg", "conservative_history": "con",
                               "neutral_history": "neu", "judge_decision": "risk verdict",
                               "count": 6}},
        {"final_trade_decision": "FINAL: BUY",
         "pm_decision": {"rating": "overweight", "executive_summary": "go"}},
    ]


# ===========================================================================
# full stream
# ===========================================================================

def test_run_streams_full_pipeline(monkeypatch):
    r = _runner()
    _install_fake_graph(monkeypatch, _full_chunks())
    hooks = _patch_tail(monkeypatch, r)
    r._run()

    assert r.session["status"] == "completed"
    rs = r.session["report_sections"]
    assert rs["bull_history"] == "Bull: buy"
    assert rs["bear_history"] == "Bear: sell"
    assert rs["investment_plan"] == "Manager: hold"
    assert rs["trader_investment_plan"] == "trader plan"
    assert rs["aggressive_history"] == "agg"
    assert rs["neutral_history"] == "neu"
    assert rs["final_trade_decision"] == "FINAL: BUY"
    # PM decision captured for the post-decision hook
    assert r.session["pm_decision"] == {"rating": "overweight", "executive_summary": "go"}
    # message + tool-call entries appended
    types = {m["type"] for m in r.session["messages"]}
    assert "tool_call" in types
    # post-completion hooks ran
    assert hooks["post"] == 1 and hooks["disagree"] == 1
    # all agents marked completed at the end
    assert all(v == "completed" for v in r.session["agent_status"].values())


def test_run_skips_post_decision_when_flagged(monkeypatch):
    r = _runner(skip_post_decision=True)
    _install_fake_graph(monkeypatch, _full_chunks())
    hooks = _patch_tail(monkeypatch, r)
    r._run()
    assert r.session["status"] == "completed"
    assert hooks["post"] == 0  # skip_post_decision short-circuits before the hook


def test_run_cancelled_before_stream(monkeypatch):
    r = _runner()
    _install_fake_graph(monkeypatch, _full_chunks())
    _patch_tail(monkeypatch, r)
    r._cancel_requested.set()
    r._run()
    assert r.session["status"] == "cancelled"


def test_run_dedupes_message_ids(monkeypatch):
    r = _runner()
    dup = AIMessage(content="same", id="dup")
    chunks = [{"messages": [dup]}, {"messages": [dup]},
              {"final_trade_decision": "BUY"}]
    _install_fake_graph(monkeypatch, chunks)
    _patch_tail(monkeypatch, r)
    r._run()
    # the duplicate id is only appended once
    agent_msgs = [m for m in r.session["messages"] if m["content"] == "same"]
    assert len(agent_msgs) == 1
