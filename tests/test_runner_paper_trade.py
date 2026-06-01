"""Coverage for web/runner.py's _do_paper_trade branches, adaptive-depth
escalation, and disagreement auto-inject. The risk guard, Kelly sizing, paper
fill, LLM client, and classical voice are all faked — no broker, no LLM.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agenticwhales import behavioral, paper, risk as risk_mod
from agenticwhales.agents.schemas import (
    OrderSide, PortfolioDecision, PortfolioRating,
)
from agenticwhales.audit import impersonate
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
    s["market_snapshot"] = "Latest close: $100.0 on 2024-01-02"
    s.update(over)
    r = SessionRunner(s, _FakeLoop())
    r.events = []
    r._broadcast = r.events.append
    return r


def _decision(rating=PortfolioRating.OVERWEIGHT, **kw):
    base = dict(rating=rating, executive_summary="Strong.", investment_thesis="Momentum.",
                expected_return_pct=8.0, expected_volatility_pct=15.0,
                prob_of_profit=0.62, expected_hold_days=10)
    base.update(kw)
    return PortfolioDecision(**base)


def _call(r, decision):
    with impersonate("u1") as tok:
        r._do_paper_trade(tok, decision, "AAPL", 8, None, "fire1", r.session["id"])


# ===========================================================================
# _do_paper_trade — cooldown circuit-breaker
# ===========================================================================

def test_paper_trade_blocked_by_cooldown(monkeypatch):
    r = _runner()
    monkeypatch.setattr(behavioral, "cooldown_in_effect",
                        lambda uid: {"pattern": "revenge", "severity": "high",
                                     "evidence": {"summary": "rapid re-entry"}})
    monkeypatch.setattr(risk_mod, "record_event", lambda *a, **k: None)
    _call(r, _decision())
    assert any(e["type"] == "risk_event" and e["rule"] == "tilt_cooldown"
               for e in r.events)
    # no order placed
    assert not any(e["type"] == "paper_order" for e in r.events)


# ===========================================================================
# _do_paper_trade — abstain (Kelly returns zero)
# ===========================================================================

def test_paper_trade_abstains_when_zero_qty(monkeypatch):
    r = _runner()
    monkeypatch.setattr(behavioral, "cooldown_in_effect", lambda uid: None)
    monkeypatch.setattr(paper, "kelly_sizing",
                        lambda decision, **k: SimpleNamespace(qty=0, fraction=0.0, direction=0))
    monkeypatch.setattr(risk_mod, "record_event", lambda *a, **k: None)
    _call(r, _decision(rating=PortfolioRating.HOLD))
    assert any(e["type"] == "risk_event" and e["rule"] == "abstain" for e in r.events)


# ===========================================================================
# _do_paper_trade — guard hard-block
# ===========================================================================

def test_paper_trade_guard_blocks(monkeypatch):
    r = _runner()
    monkeypatch.setattr(behavioral, "cooldown_in_effect", lambda uid: None)
    monkeypatch.setattr(paper, "kelly_sizing",
                        lambda decision, **k: SimpleNamespace(qty=10, fraction=0.05, direction=1))
    monkeypatch.setattr(risk_mod, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(risk_mod, "RiskGuard", lambda **k: SimpleNamespace(
        evaluate=lambda **kk: SimpleNamespace(rule="max_position", allowed=False,
                                              allowed_qty=0, reason="too big")))
    placed = {"n": 0}
    monkeypatch.setattr(paper, "place_order",
                        lambda token, **k: placed.__setitem__("n", placed["n"] + 1))
    _call(r, _decision())
    assert any(e["type"] == "risk_event" and e["rule"] == "max_position" for e in r.events)
    assert placed["n"] == 0  # blocked → no order


# ===========================================================================
# _do_paper_trade — order placed
# ===========================================================================

def test_paper_trade_places_order(monkeypatch):
    r = _runner()
    monkeypatch.setattr(behavioral, "cooldown_in_effect", lambda uid: None)
    monkeypatch.setattr(paper, "kelly_sizing",
                        lambda decision, **k: SimpleNamespace(qty=10, fraction=0.05, direction=1))
    monkeypatch.setattr(risk_mod, "RiskGuard", lambda **k: SimpleNamespace(
        evaluate=lambda **kk: SimpleNamespace(rule=None, allowed=True,
                                              allowed_qty=10, reason=None)))
    monkeypatch.setattr(paper, "place_order", lambda token, **k: SimpleNamespace(
        order_id="o1", status=SimpleNamespace(value="filled"),
        qty=10, fill_price=100.0, idempotent=False))
    _call(r, _decision())
    order_evts = [e for e in r.events if e["type"] == "paper_order"]
    assert order_evts and order_evts[0]["order_id"] == "o1"
    assert order_evts[0]["status"] == "filled"


# ===========================================================================
# _maybe_apply_adaptive_depth — escalation + sample-failure paths
# ===========================================================================

def _adaptive_setup(monkeypatch, threshold=0.3, invoke=None, escalate=True):
    monkeypatch.setattr("web.auth.load_risk_limits",
                        lambda uid: {"adaptive_depth_variance_threshold": threshold})
    quick = SimpleNamespace(invoke=invoke or (lambda p: SimpleNamespace(content="long: momentum")))
    client = SimpleNamespace(get_quick_thinking_llm=lambda: quick)
    monkeypatch.setattr("agenticwhales.llm_clients.factory.create_llm_client",
                        lambda **k: client)
    monkeypatch.setattr("agenticwhales.adaptive.should_escalate",
                        lambda samples, threshold: escalate)


def test_adaptive_depth_escalates(monkeypatch):
    r = _runner()
    _adaptive_setup(monkeypatch, escalate=True)
    cfg = {"llm_provider": "google", "quick_think_llm": "q", "deep_think_llm": "d",
           "max_debate_rounds": 1}
    r._maybe_apply_adaptive_depth(cfg)
    assert cfg["quick_think_llm"] == "d"  # escalated to deep
    assert cfg["max_debate_rounds"] == 2
    assert any(e["type"] == "adaptive_depth_escalation" for e in r.events)


def test_adaptive_depth_no_escalation(monkeypatch):
    r = _runner()
    _adaptive_setup(monkeypatch, escalate=False)
    cfg = {"llm_provider": "google", "quick_think_llm": "q", "deep_think_llm": "d"}
    r._maybe_apply_adaptive_depth(cfg)
    assert cfg["quick_think_llm"] == "q"  # unchanged


def test_adaptive_depth_too_few_samples(monkeypatch):
    r = _runner()

    def _boom(prompt):
        raise RuntimeError("provider error")
    _adaptive_setup(monkeypatch, invoke=_boom)
    cfg = {"llm_provider": "google", "quick_think_llm": "q", "deep_think_llm": "d"}
    r._maybe_apply_adaptive_depth(cfg)
    assert cfg["quick_think_llm"] == "q"  # all samples failed → no escalation


# ===========================================================================
# _record_disagreement_and_maybe_inject_classical — auto-inject path
# ===========================================================================

def test_disagreement_auto_injects_classical(monkeypatch):
    r = _runner()
    r.session["report_sections"] = {"bull_history": "Bull bull bull.",
                                    "bear_history": "Bear bear bear."}
    import agenticwhales.disagreement as dmod
    import agenticwhales.classical as cmod
    monkeypatch.setattr(dmod, "record_disagreement",
                        lambda **k: SimpleNamespace(similarity=0.95, rating_agreement=True))
    monkeypatch.setattr(dmod, "should_auto_inject", lambda recipe_row, sim: True)
    result = SimpleNamespace(
        decision=SimpleNamespace(rating=SimpleNamespace(value="bearish"),
                                 model_dump=lambda mode: {"rating": "bearish"}),
        radar=SimpleNamespace(model_dump=lambda mode: {"score": 1}),
        aggregate_score=0.4)
    monkeypatch.setattr(cmod, "analyze_classical", lambda t, d: result)
    r._record_disagreement_and_maybe_inject_classical()
    assert any(e["type"] == "classical_voice" for e in r.events)
    assert r.session["classical_decision"] == {"rating": "bearish"}
    assert r.session["classical_score"] == 0.4
