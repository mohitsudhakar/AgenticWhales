"""Unit coverage for the testable surface of graph/trading_graph.py — provider
kwargs, credential gating, provider-LLM creation, synthesizer + debater
diversification selection, memory-v2 augmentation, extended-reflection cadence,
and state logging. The class is instantiated via __new__ to skip the heavy
graph-building __init__; only the attributes each method touches are set.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agenticwhales.graph import trading_graph as tg
from agenticwhales.graph.trading_graph import AgenticWhalesGraph


def _bare(**attrs):
    g = AgenticWhalesGraph.__new__(AgenticWhalesGraph)
    g.config = {}
    g.callbacks = None
    g.deep_thinking_llm = "DEEP"
    g.quick_thinking_llm = "QUICK"
    for k, v in attrs.items():
        setattr(g, k, v)
    return g


# ===========================================================================
# _get_provider_kwargs
# ===========================================================================

def test_provider_kwargs_google():
    g = _bare()
    g.config = {"llm_provider": "google", "google_thinking_level": "high"}
    assert g._get_provider_kwargs() == {"thinking_level": "high"}


def test_provider_kwargs_openai():
    g = _bare()
    g.config = {"llm_provider": "openai", "openai_reasoning_effort": "high"}
    assert g._get_provider_kwargs() == {"reasoning_effort": "high"}


def test_provider_kwargs_anthropic():
    g = _bare()
    g.config = {"llm_provider": "anthropic", "anthropic_effort": "medium"}
    assert g._get_provider_kwargs() == {"effort": "medium"}


def test_provider_kwargs_empty_when_unset():
    g = _bare()
    g.config = {"llm_provider": "google"}  # no thinking level
    assert g._get_provider_kwargs() == {}


# ===========================================================================
# _provider_has_credentials
# ===========================================================================

def test_provider_has_credentials(monkeypatch):
    g = _bare()
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    assert g._provider_has_credentials("google") is True
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert g._provider_has_credentials("deepseek") is False
    # unknown provider → trusted
    assert g._provider_has_credentials("customthing") is True


# ===========================================================================
# _create_provider_llm
# ===========================================================================

def test_create_provider_llm_success(monkeypatch):
    g = _bare()
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setattr(tg, "MODEL_OPTIONS", {"google": {"deep": [("Gemini", "gem")]}})
    monkeypatch.setattr(tg, "create_llm_client",
                        lambda **k: SimpleNamespace(get_llm=lambda: "LLM"))
    assert g._create_provider_llm("google", "deep") == "LLM"


def test_create_provider_llm_no_credentials(monkeypatch):
    g = _bare()
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert g._create_provider_llm("deepseek") is None


def test_create_provider_llm_no_catalog(monkeypatch):
    g = _bare()
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setattr(tg, "MODEL_OPTIONS", {})
    assert g._create_provider_llm("google", "deep") is None


def test_create_provider_llm_passes_callbacks(monkeypatch):
    g = _bare(callbacks=["cb"])
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setattr(tg, "MODEL_OPTIONS", {"openai": {"quick": [("GPT", "g")]}})
    seen = {}
    monkeypatch.setattr(tg, "create_llm_client",
                        lambda **k: seen.update(k) or SimpleNamespace(get_llm=lambda: "L"))
    g._create_provider_llm("openai", "quick")
    assert seen["callbacks"] == ["cb"]


# ===========================================================================
# _build_diversified_synthesizer_llm
# ===========================================================================

def test_synthesizer_off_returns_deep():
    g = _bare()
    g.config = {"diversify_synthesizers": False}
    assert g._build_diversified_synthesizer_llm("research") == "DEEP"


def test_synthesizer_picks_first_usable(monkeypatch):
    g = _bare()
    g.config = {"diversify_synthesizers": True, "llm_provider": "google",
                "synthesizer_provider_preference": ["google", "anthropic"]}
    # google == upstream → skipped; anthropic → returns an llm
    monkeypatch.setattr(g, "_create_provider_llm",
                        lambda prov, mode="deep": "ANTHRO" if prov == "anthropic" else None)
    assert g._build_diversified_synthesizer_llm("research") == "ANTHRO"


def test_synthesizer_falls_back_when_none_usable(monkeypatch):
    g = _bare()
    g.config = {"diversify_synthesizers": True, "llm_provider": "google",
                "synthesizer_provider_preference": ["deepseek"]}
    monkeypatch.setattr(g, "_create_provider_llm", lambda prov, mode="deep": None)
    assert g._build_diversified_synthesizer_llm("portfolio") == "DEEP"


# ===========================================================================
# _build_debater_llms
# ===========================================================================

def test_debaters_off_all_quick():
    g = _bare()
    g.config = {"diversify_debaters": False}
    out = g._build_debater_llms()
    assert set(out) == {"bull", "bear", "aggressive", "conservative", "neutral"}
    assert all(v == "QUICK" for v in out.values())


def test_debaters_no_usable_providers(monkeypatch):
    g = _bare()
    g.config = {"diversify_debaters": True, "debater_provider_preference": ["deepseek"]}
    monkeypatch.setattr(g, "_create_provider_llm", lambda prov, mode="quick": None)
    out = g._build_debater_llms()
    assert all(v == "QUICK" for v in out.values())


def test_debaters_assigns_two_providers(monkeypatch):
    g = _bare()
    g.config = {"diversify_debaters": True,
                "debater_provider_preference": ["anthropic", "openai"]}
    monkeypatch.setattr(g, "_provider_has_credentials", lambda p: True)
    monkeypatch.setattr(g, "_create_provider_llm",
                        lambda prov, mode="quick": f"LLM:{prov}")
    out = g._build_debater_llms()
    assert out["bull"] == "LLM:anthropic" and out["bear"] == "LLM:openai"
    assert out["neutral"] == "LLM:anthropic"  # index 2 % 2 == 0


# ===========================================================================
# _augment_with_memory_v2
# ===========================================================================

def test_augment_memory_no_user_returns_base():
    g = _bare(user_id=None)
    assert g._augment_with_memory_v2("AAPL", "base") == "base"


def test_augment_memory_no_results(monkeypatch):
    g = _bare(user_id="u1")
    monkeypatch.setattr("agenticwhales.memory_v2.retrieve_relevant",
                        lambda *a, **k: [])
    assert g._augment_with_memory_v2("AAPL", "base") == "base"


def test_augment_memory_prepends_block(monkeypatch):
    g = _bare(user_id="u1")
    res = [SimpleNamespace(body="A long lesson about momentum.", cosine=0.9,
                           predictiveness=0.7, score=0.63)]
    monkeypatch.setattr("agenticwhales.memory_v2.retrieve_relevant",
                        lambda *a, **k: res)
    out = g._augment_with_memory_v2("AAPL", "base ctx")
    assert "Outcome-predictive journal retrievals" in out and "base ctx" in out


# ===========================================================================
# _maybe_run_extended_reflection
# ===========================================================================

class _MemLog:
    def __init__(self, since=None, entries=None):
        self._since = since
        self._entries = entries or []
        self.stored = None

    def days_since_last_extended_reflection(self):
        return self._since

    def load_entries(self):
        return self._entries

    def store_extended_reflection(self, date, content):
        self.stored = (date, content)


def test_extended_reflection_disabled_when_no_interval():
    g = _bare(memory_log=_MemLog())
    g.config = {}
    g._maybe_run_extended_reflection("2024-01-02")  # no raise, nothing stored


def test_extended_reflection_skips_when_recent():
    g = _bare(memory_log=_MemLog(since=2))
    g.config = {"extended_reflection_interval_days": 30}
    g._maybe_run_extended_reflection("2024-01-02")
    assert g.memory_log.stored is None


def test_extended_reflection_stores_content():
    entries = [{"date": "2024-01-01", "ticker": "AAPL", "pending": False}]
    mem = _MemLog(since=None, entries=entries)
    g = _bare(memory_log=mem,
              reflector=SimpleNamespace(extended_reflection=lambda recent: "RETRO"))
    g.config = {"extended_reflection_interval_days": 30,
                "extended_reflection_window_days": 365}
    g._maybe_run_extended_reflection("2024-01-02")
    assert g.memory_log.stored == ("2024-01-02", "RETRO")


def test_extended_reflection_handles_reflector_error():
    entries = [{"date": "2024-01-01", "ticker": "AAPL", "pending": False}]
    mem = _MemLog(since=None, entries=entries)

    def _boom(recent):
        raise RuntimeError("llm down")
    g = _bare(memory_log=mem,
              reflector=SimpleNamespace(extended_reflection=_boom))
    g.config = {"extended_reflection_interval_days": 30,
                "extended_reflection_window_days": 365}
    g._maybe_run_extended_reflection("2024-01-02")
    assert g.memory_log.stored is None


def test_extended_reflection_no_entries():
    g = _bare(memory_log=_MemLog(since=None, entries=[]))
    g.config = {"extended_reflection_interval_days": 30}
    g._maybe_run_extended_reflection("2024-01-02")
    assert g.memory_log.stored is None


# ===========================================================================
# _log_state
# ===========================================================================

def _final_state():
    return {
        "company_of_interest": "AAPL", "trade_date": "2024-01-02",
        "market_report": "m", "sentiment_report": "s", "news_report": "n",
        "fundamentals_report": "f",
        "investment_debate_state": {"bull_history": "bh", "bear_history": "beh",
                                    "history": "h", "current_response": "cr",
                                    "judge_decision": "jd"},
        "trader_investment_plan": "plan",
        "risk_debate_state": {"aggressive_history": "ah", "conservative_history": "ch",
                              "neutral_history": "nh", "history": "h",
                              "judge_decision": "jd"},
        "investment_plan": "ip", "final_trade_decision": "BUY",
    }


def test_log_state_writes_json(tmp_path):
    g = _bare(log_states_dict={}, ticker="AAPL")
    g.config = {"results_dir": str(tmp_path)}
    g._log_state("2024-01-02", _final_state())
    out = tmp_path / "AAPL" / "TradingAgentsStrategy_logs" / "full_states_log_2024-01-02.json"
    assert out.exists()
    assert g.log_states_dict["2024-01-02"]["final_trade_decision"] == "BUY"


# ===========================================================================
# __init__ construction (LLM clients faked → compiles the graph offline)
# ===========================================================================

def test_graph_init_compiles(monkeypatch, tmp_path):
    from agenticwhales.default_config import DEFAULT_CONFIG

    class _LLM:
        def bind_tools(self, tools):
            return self

    monkeypatch.setattr(tg, "create_llm_client",
                        lambda **k: SimpleNamespace(get_llm=lambda: _LLM()))

    cfg = DEFAULT_CONFIG.copy()
    cfg.update({
        "llm_provider": "google", "quick_think_llm": "gemini-x",
        "deep_think_llm": "gemini-x", "backend_url": None,
        "max_debate_rounds": 1, "max_risk_discuss_rounds": 1,
        "results_dir": str(tmp_path), "data_cache_dir": str(tmp_path),
        "memory_log_path": str(tmp_path / "mem.md"),
        "diversify_synthesizers": False, "diversify_debaters": False,
    })
    g = AgenticWhalesGraph(["market"], config=cfg, debug=False)
    assert g.graph is not None
    assert g.workflow is not None
    # synthesizers fall back to the deep LLM (diversification off)
    assert g.research_manager_llm is g.deep_thinking_llm
    # process_signal delegates to the signal processor
    assert g.curr_state is None
