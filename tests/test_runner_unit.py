"""Unit coverage for web/runner.py — the synchronous, deterministic surface.

We avoid the LLM graph entirely: tests target the module-level helpers,
SessionRunner's state mutators (driven with a fake event loop that runs
callbacks inline), the subscribe/snapshot API, lifecycle (cancel), the
order-side / abstain helpers, and the early-return branches of the
post-decision hook. No network, no threads spawned.
"""

from __future__ import annotations

import pytest

from agenticwhales.agents.schemas import OrderSide, PaperPosition, PortfolioRating
from web import auth, runner
from web.runner import (
    AGENT_TO_TEAM,
    ANALYST_AGENT_NAMES,
    SECTION_AGENT,
    SessionRunner,
    build_session,
    config_signature,
    _classify_message,
    _compact_args,
    _deep_copy,
    _stringify,
)


@pytest.fixture(autouse=True)
def _offline():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


class _FakeLoop:
    """Stand-in for an asyncio loop: run scheduled callbacks immediately."""

    def __init__(self):
        self.calls = 0

    def call_soon_threadsafe(self, fn, *args):
        self.calls += 1
        fn(*args)


def _form(**over):
    f = {"ticker": "aapl", "analysis_date": "2024-01-02",
         "analysts": ["market", "quant"],
         "llm_provider": "google", "quick_think_llm": "q", "deep_think_llm": "d"}
    f.update(over)
    return f


def _runner(**form_over):
    session = build_session(_form(**form_over))
    r = SessionRunner(session, _FakeLoop())
    # Capture broadcasts in a plain list — avoids cross-test asyncio.Queue
    # state-sharing when there's no running event loop.
    r.events = []
    r._broadcast = r.events.append  # type: ignore[assignment]
    return r


# ---------------------------------------------------------------------------
# module-level helpers
# ---------------------------------------------------------------------------

def test_config_signature_stable_and_selective():
    a = _form(research_depth=1, output_language="English")
    b = dict(a, ticker="NVDA", analysis_date="2024-02-02")  # only ticker/date differ
    assert config_signature(a) == config_signature(b)  # ticker/date excluded
    c = dict(a, research_depth=5)
    assert config_signature(a) != config_signature(c)
    # signature is a pipe-joined string of the meaningful knobs
    assert "google" in config_signature(a)


def test_build_session_shape():
    s = build_session(_form(analysts=["market", "news"]))
    assert s["ticker"] == "AAPL"  # upper-cased
    assert s["status"] == "pending"
    assert s["report_sections"] == {} and s["messages"] == []
    assert s["agent_status"]["Market Analyst"] == "pending"
    assert s["agent_status"]["News Analyst"] == "pending"
    assert s["agent_status"]["Portfolio Manager"] == "pending"
    assert "Quant Analyst" not in s["agent_status"]  # not selected
    assert len(s["id"]) == 32


def test_build_session_defaults_all_analysts():
    s = build_session(_form(analysts=None))  # None → all analysts
    for name in ANALYST_AGENT_NAMES.values():
        assert s["agent_status"][name] == "pending"


def test_classify_message_variants():
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
    assert _classify_message(HumanMessage(content="hi")) == ("user", "hi")
    assert _classify_message(AIMessage(content="thinking")) == ("agent", "thinking")
    assert _classify_message(ToolMessage(content="data", tool_call_id="t1")) == ("tool", "data")
    # SystemMessage is none of the three branches → falls through to ("system", text)
    assert _classify_message(SystemMessage(content="sys")) == ("system", "sys")


def test_stringify_str_list_and_none():
    assert _stringify("hello") == "hello"
    # only dicts with type=="text" (and bare strings) are kept
    assert _stringify([{"type": "text", "text": "a"}, {"foo": "b"}, "c"]) == "a\nc"
    assert _stringify("   ") is None      # blank → None
    assert _stringify(None) is None
    assert _stringify(123) == "123"


def test_compact_args_truncates():
    assert _compact_args({"a": 1}) == "a=1"        # k=v form, not JSON
    assert _compact_args(None) == ""
    long = _compact_args({"k": "x" * 500}, limit=50)
    assert long.endswith("…") and len(long) == 50


def test_deep_copy_is_independent():
    src = {"a": {"b": [1, 2]}}
    cp = _deep_copy(src)
    cp["a"]["b"].append(3)
    assert src["a"]["b"] == [1, 2]


def test_agent_to_team_and_section_maps():
    assert AGENT_TO_TEAM["Market Analyst"] == "Analyst Team"
    assert AGENT_TO_TEAM["Portfolio Manager"] == "Portfolio Management"
    assert AGENT_TO_TEAM["Bull Researcher"] == "Research Team"
    assert SECTION_AGENT["final_trade_decision"] == "Portfolio Manager"


# ---------------------------------------------------------------------------
# subscription + snapshot
# ---------------------------------------------------------------------------

def test_subscribe_unsubscribe():
    r = _runner()
    q = r.subscribe()
    assert q in r.subscribers
    r.unsubscribe(q)
    assert q not in r.subscribers
    r.unsubscribe(q)  # idempotent — no error


def test_snapshot_is_a_copy():
    r = _runner()
    snap = r.snapshot()
    snap["ticker"] = "CHANGED"
    assert r.session["ticker"] == "AAPL"


# ---------------------------------------------------------------------------
# state mutators (broadcast through the fake loop)
# ---------------------------------------------------------------------------

def test_set_status_broadcasts_and_persists():
    r = _runner()
    r._set_status("Market Analyst", "in_progress")
    assert r.session["agent_status"]["Market Analyst"] == "in_progress"
    assert any(e["type"] == "agent_status" and e["agent"] == "Market Analyst"
               for e in r.events)


def test_set_status_unknown_agent_noop():
    r = _runner()
    r._set_status("Nonexistent Agent", "in_progress")
    assert r.events == []  # nothing broadcast


def test_set_status_same_status_noop():
    r = _runner()
    r._set_status("Market Analyst", "in_progress")
    r.events.clear()
    r._set_status("Market Analyst", "in_progress")  # already in_progress
    assert r.events == []


def test_team_timing_start_and_complete():
    r = _runner(analysts=["market"])  # Analyst Team = just Market Analyst here
    r._set_status("Market Analyst", "in_progress")
    r._set_status("Market Analyst", "completed")
    timings = r.session["team_timings"]["Analyst Team"]
    assert timings["started_at"] is not None
    assert timings["completed_at"] is not None
    assert timings["duration_s"] is not None
    assert any(e["type"] == "team_timing" for e in r.events)


def test_set_report_broadcasts():
    r = _runner()
    r._set_report("market_report", "the report body")
    assert r.session["report_sections"]["market_report"] == "the report body"
    rep = next(e for e in r.events if e["type"] == "report")
    assert rep["agent"] == "Market Analyst" and rep["content"] == "the report body"


def test_append_message_caps_at_500():
    r = _runner()
    for i in range(520):
        r._append_message({"i": i})
    assert len(r.session["messages"]) == 500
    assert r.session["messages"][-1]["i"] == 519  # newest kept


def test_set_session_updates_fields():
    r = _runner()
    r._set_session(status="running", pm_decision={"rating": "Buy"})
    assert r.session["status"] == "running"
    assert any(e["type"] == "session" for e in r.events)


def test_set_stats_dedupes():
    r = _runner()
    r._set_stats({"tokens_in": 10})
    r._set_stats({"tokens_in": 10})  # identical → no second broadcast
    assert sum(1 for e in r.events if e["type"] == "stats") == 1


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def test_cancel_only_when_active():
    r = _runner()
    r.session["status"] = "running"
    assert r.cancel() is True
    assert r.is_cancelled() is True
    r2 = _runner()
    r2.session["status"] = "completed"
    assert r2.cancel() is False


def test_finalize_cancelled():
    r = _runner()
    r._finalize_cancelled()
    assert r.session["status"] == "cancelled"
    assert r.session["completed_at"] is not None


def test_run_safe_marks_failed_on_exception(monkeypatch):
    r = _runner()
    def _boom():
        raise RuntimeError("kaboom")
    monkeypatch.setattr(r, "_run", _boom)
    r._run_safe()
    assert r.session["status"] == "failed"
    assert "kaboom" in r.session["error"]


def test_run_safe_treats_post_cancel_exception_as_cancelled(monkeypatch):
    r = _runner()
    r._cancel_requested.set()
    def _boom():
        raise RuntimeError("torn down")
    monkeypatch.setattr(r, "_run", _boom)
    r._run_safe()
    assert r.session["status"] == "cancelled"


# ---------------------------------------------------------------------------
# order-side + abstain helpers
# ---------------------------------------------------------------------------

def _pos(ticker, qty):
    return PaperPosition(user_id="u1", ticker=ticker, qty=qty, avg_cost=100.0)


def test_side_for_long_direction():
    # direction is numeric: >0 long, <0 short, 0 reaches default BUY.
    r = _runner()
    assert r._side_for(1, [], "AAPL") == OrderSide.BUY            # flat → buy
    assert r._side_for(1, [_pos("AAPL", -5)], "AAPL") == OrderSide.COVER  # short → cover


def test_side_for_short_direction():
    r = _runner()
    assert r._side_for(-1, [_pos("AAPL", 10)], "AAPL") == OrderSide.SELL  # long → sell
    assert r._side_for(-1, [], "AAPL") == OrderSide.SHORT          # flat → short


def test_side_for_zero_direction_defaults_buy():
    r = _runner()
    assert r._side_for(0, [], "AAPL") == OrderSide.BUY


def test_abstain_reason_paths():
    from agenticwhales.agents.schemas import PortfolioDecision

    r = _runner()
    hold = PortfolioDecision(rating=PortfolioRating.HOLD, executive_summary="s",
                             investment_thesis="t")
    assert "Hold" in r._abstain_reason(hold, None, 100.0)

    # Decision lacking prob_of_profit / expected_return_pct.
    bare = PortfolioDecision(rating=PortfolioRating.OVERWEIGHT, executive_summary="s",
                             investment_thesis="t")
    assert "price" in r._abstain_reason(bare, None, None).lower()       # no/0 price
    assert "prob_of_profit" in r._abstain_reason(bare, None, 100.0)     # missing p

    have_p = PortfolioDecision(rating=PortfolioRating.OVERWEIGHT, executive_summary="s",
                               investment_thesis="t", prob_of_profit=0.6)
    assert "expected_return_pct" in r._abstain_reason(have_p, None, 100.0)

    full = PortfolioDecision(rating=PortfolioRating.OVERWEIGHT, executive_summary="s",
                             investment_thesis="t", prob_of_profit=0.6,
                             expected_return_pct=8.0)

    class _SzZero:
        fraction = 0
    assert "no bet" in r._abstain_reason(full, _SzZero(), 100.0)        # fractional Kelly 0

    class _SzPos:
        fraction = 0.1
    assert "rounding floor" in r._abstain_reason(full, _SzPos(), 100.0)  # qty rounded to 0


# ---------------------------------------------------------------------------
# post-decision hook early returns
# ---------------------------------------------------------------------------

def test_post_decision_hook_no_decision_returns():
    r = _runner()
    r.session.pop("pm_decision", None)
    r._post_decision_hook()  # no raise, no-op


def test_post_decision_hook_no_user_returns():
    r = _runner()
    r.session["pm_decision"] = {"rating": "Buy", "executive_summary": "s",
                               "investment_thesis": "t"}
    r.session.pop("user_id", None)
    r._post_decision_hook()  # returns at the user_id guard, no raise
