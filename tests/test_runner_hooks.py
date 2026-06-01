"""Coverage for web/runner.py post-decision hooks + helpers that don't need
the LLM graph: _post_decision_hook policy branches, _auto_draft_journal,
_update_analyst_statuses, _maybe_apply_adaptive_depth skip paths,
_record_disagreement..., _latest_price_for.

Offline: auth memstore; recipe/paper boundaries monkeypatched. No LLM, no graph.
"""

from __future__ import annotations

import pytest

from agenticwhales.agents.schemas import OutputPolicy, PortfolioDecision, PortfolioRating
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


def _runner(user="u1", **session_over):
    form = {"ticker": "aapl", "analysis_date": "2024-01-02", "analysts": ["market"],
            "llm_provider": "google", "quick_think_llm": "q", "deep_think_llm": "d"}
    s = build_session(form)
    s["user_id"] = user
    s.update(session_over)
    r = SessionRunner(s, _FakeLoop())
    r.events = []
    r._broadcast = r.events.append
    return r


def _decision(rating=PortfolioRating.OVERWEIGHT, **kw):
    base = dict(rating=rating, executive_summary="Strong setup.",
                investment_thesis="Momentum + earnings.",
                expected_return_pct=8.0, expected_volatility_pct=15.0,
                prob_of_profit=0.62, expected_hold_days=10)
    base.update(kw)
    return PortfolioDecision(**base)


# ---------------------------------------------------------------------------
# _post_decision_hook — records conviction + auto-draft, then branches
# ---------------------------------------------------------------------------

def test_hook_notify_records_conviction_and_journal():
    r = _runner()
    r.session["pm_decision"] = _decision().model_dump(mode="json")
    # no recipe → default NOTIFY policy → conviction recorded, no order
    r._post_decision_hook()
    conv = auth.list_conviction_scores("u1")
    assert len(conv) == 1 and conv[0]["ticker"] == "AAPL"
    drafts = auth.list_journal_entries("u1", kind="auto_draft")
    assert len(drafts) == 1 and drafts[0]["is_draft"] is True


def test_hook_alert_conviction_broadcasts_when_over_threshold(monkeypatch):
    r = _runner(recipe_id="rec1")
    r.session["pm_decision"] = _decision().model_dump(mode="json")

    class _Recipe:
        output_policy = OutputPolicy.ALERT_CONVICTION
        conviction_threshold = 1   # low → fires

    monkeypatch.setattr(runner, "OutputPolicy", OutputPolicy)
    import agenticwhales.recipes as recipes_mod
    monkeypatch.setattr(recipes_mod, "load", lambda rid: _Recipe())
    r._post_decision_hook()
    assert any(e["type"] == "conviction_alert" for e in r.events)


def test_hook_alert_conviction_silent_when_under_threshold(monkeypatch):
    r = _runner(recipe_id="rec1")
    r.session["pm_decision"] = _decision(rating=PortfolioRating.HOLD).model_dump(mode="json")

    class _Recipe:
        output_policy = OutputPolicy.ALERT_CONVICTION
        conviction_threshold = 11  # impossible → never fires

    import agenticwhales.recipes as recipes_mod
    monkeypatch.setattr(recipes_mod, "load", lambda rid: _Recipe())
    r._post_decision_hook()
    assert not any(e["type"] == "conviction_alert" for e in r.events)


def test_hook_paper_trade_calls_do_paper_trade(monkeypatch):
    r = _runner(recipe_id="rec1")
    r.session["pm_decision"] = _decision().model_dump(mode="json")

    class _Recipe:
        output_policy = OutputPolicy.PAPER_TRADE
        conviction_threshold = 7

    import agenticwhales.recipes as recipes_mod
    monkeypatch.setattr(recipes_mod, "load", lambda rid: _Recipe())
    called = {}
    monkeypatch.setattr(r, "_do_paper_trade",
                        lambda **kw: called.update(ticker=kw["ticker"]))
    r._post_decision_hook()
    assert called.get("ticker") == "AAPL"


def test_hook_no_decision_returns():
    r = _runner()
    r.session.pop("pm_decision", None)
    r._post_decision_hook()  # no raise
    assert auth.list_conviction_scores("u1") == []


def test_hook_no_user_returns():
    r = _runner(user=None)
    r.session["pm_decision"] = _decision().model_dump(mode="json")
    r._post_decision_hook()  # returns at user guard


# ---------------------------------------------------------------------------
# _auto_draft_journal — dedupes per session
# ---------------------------------------------------------------------------

def test_auto_draft_dedupes():
    r = _runner()
    d = _decision()
    r._auto_draft_journal(user_id="u1", session_id=r.session["id"], recipe_id=None,
                          decision=d, ticker="AAPL", conviction=8)
    r._auto_draft_journal(user_id="u1", session_id=r.session["id"], recipe_id=None,
                          decision=d, ticker="AAPL", conviction=8)
    assert len(auth.list_journal_entries("u1", kind="auto_draft")) == 1


def test_auto_draft_body_handles_missing_scalars():
    r = _runner()
    d = _decision(expected_return_pct=None, expected_volatility_pct=None,
                  prob_of_profit=None, expected_hold_days=None)
    r._auto_draft_journal(user_id="u1", session_id="sess-x", recipe_id="rec",
                          decision=d, ticker="AAPL", conviction=5)
    body = auth.list_journal_entries("u1", kind="auto_draft")[0]["body"]
    assert "not provided" in body and "AAPL" in body


# ---------------------------------------------------------------------------
# _update_analyst_statuses
# ---------------------------------------------------------------------------

def test_update_analyst_statuses_sets_report_and_completes():
    r = _runner()  # analysts=["market"]
    r._update_analyst_statuses({"market_report": "the market read"}, ["market"])
    assert r.session["report_sections"]["market_report"] == "the market read"
    assert r.session["agent_status"]["Market Analyst"] == "completed"


def test_update_analyst_statuses_marks_first_pending_in_progress():
    r = _runner()
    # no report yet → market becomes the active (in_progress) one
    r._update_analyst_statuses({}, ["market"])
    assert r.session["agent_status"]["Market Analyst"] == "in_progress"


# ---------------------------------------------------------------------------
# _maybe_apply_adaptive_depth — skip paths
# ---------------------------------------------------------------------------

def test_adaptive_depth_skips_without_user():
    r = _runner(user=None)
    cfg = {"llm_provider": "google", "quick_think_llm": "q", "deep_think_llm": "d"}
    r._maybe_apply_adaptive_depth(cfg)  # returns at user guard, cfg unchanged
    assert cfg["quick_think_llm"] == "q"


def test_adaptive_depth_skips_when_threshold_zero(monkeypatch):
    r = _runner()
    monkeypatch.setattr("web.auth.load_risk_limits",
                        lambda uid: {"adaptive_depth_variance_threshold": 0})
    cfg = {"llm_provider": "google", "quick_think_llm": "q", "deep_think_llm": "d"}
    r._maybe_apply_adaptive_depth(cfg)
    assert cfg["quick_think_llm"] == "q"  # no escalation


def test_adaptive_depth_skips_on_client_error(monkeypatch):
    r = _runner()
    monkeypatch.setattr("web.auth.load_risk_limits",
                        lambda uid: {"adaptive_depth_variance_threshold": 0.3})
    def _boom(**kw):
        raise RuntimeError("no key")
    monkeypatch.setattr("agenticwhales.llm_clients.factory.create_llm_client", _boom)
    cfg = {"llm_provider": "google", "quick_think_llm": "q", "deep_think_llm": "d"}
    r._maybe_apply_adaptive_depth(cfg)
    assert cfg["quick_think_llm"] == "q"  # client error → keep original


# ---------------------------------------------------------------------------
# _record_disagreement_and_maybe_inject_classical — skip paths
# ---------------------------------------------------------------------------

def test_disagreement_skips_without_user():
    r = _runner(user=None)
    r._record_disagreement_and_maybe_inject_classical()  # user guard


def test_disagreement_skips_without_debate():
    r = _runner()
    r.session["report_sections"] = {}  # no bull/bear history
    r._record_disagreement_and_maybe_inject_classical()
    assert not any(e["type"] == "disagreement" for e in r.events)


def test_disagreement_records_and_broadcasts(monkeypatch):
    r = _runner()
    r.session["report_sections"] = {"bull_history": "Bull is very bullish.",
                                    "bear_history": "Bear sees downside risk."}
    r._record_disagreement_and_maybe_inject_classical()
    assert any(e["type"] == "disagreement" for e in r.events)


# ---------------------------------------------------------------------------
# _latest_price_for
# ---------------------------------------------------------------------------

def test_latest_price_for_parses_snapshot():
    r = _runner()
    r.session["market_snapshot"] = "Header\nLatest close: $123.45 on 2024-01-02\nmore"
    assert r._latest_price_for("AAPL") == pytest.approx(123.45)


def test_latest_price_for_falls_back_to_zero():
    r = _runner()
    r.session["market_snapshot"] = "no price line here"
    assert r._latest_price_for("AAPL") == 0.0
