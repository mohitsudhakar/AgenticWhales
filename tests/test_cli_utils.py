"""Coverage for cli/utils.py — interactive prompt wrappers. questionary's
text/select/checkbox are monkeypatched to return a stub whose .ask() yields a
canned value, so the post-prompt branching (custom IDs, exit-on-None, provider
tuples) runs without a TTY. _fetch_openrouter_models' network call is stubbed.
"""

from __future__ import annotations

import pytest

from cli import utils as u
from cli.models import AnalystType


class _Q:
    """Stub for a questionary prompt object: .ask() returns the canned value."""
    def __init__(self, value):
        self._value = value

    def ask(self):
        return self._value


@pytest.fixture
def qpatch(monkeypatch):
    """Install per-prompt return values. Returns a configurator."""
    state = {"text": [], "select": [], "checkbox": []}

    def _make(kind):
        def _fn(*a, **k):
            q = state[kind]
            return _Q(q.pop(0) if q else None)
        return _fn

    monkeypatch.setattr(u.questionary, "text", _make("text"))
    monkeypatch.setattr(u.questionary, "select", _make("select"))
    monkeypatch.setattr(u.questionary, "checkbox", _make("checkbox"))
    # Choice/Style are referenced inside the functions; keep them harmless.
    monkeypatch.setattr(u.questionary, "Choice", lambda *a, **k: ("choice", a, k))
    monkeypatch.setattr(u.questionary, "Style", lambda *a, **k: None)
    return state


# ---------------------------------------------------------------------------
# normalize_ticker_symbol / get_ticker / get_analysis_date
# ---------------------------------------------------------------------------

def test_normalize_ticker_symbol():
    assert u.normalize_ticker_symbol("  aapl ") == "AAPL"
    assert u.normalize_ticker_symbol("cnc.to") == "CNC.TO"


def test_get_ticker_returns_normalized(qpatch):
    qpatch["text"].append("  nvda ")
    assert u.get_ticker() == "NVDA"


def test_get_ticker_exits_when_blank(qpatch):
    qpatch["text"].append("")  # falsy → exit(1)
    with pytest.raises(SystemExit):
        u.get_ticker()


def test_get_analysis_date_returns_trimmed(qpatch):
    qpatch["text"].append(" 2024-01-02 ")
    assert u.get_analysis_date() == "2024-01-02"


def test_get_analysis_date_exits_when_blank(qpatch):
    qpatch["text"].append(None)
    with pytest.raises(SystemExit):
        u.get_analysis_date()


# ---------------------------------------------------------------------------
# select_analysts / select_research_depth
# ---------------------------------------------------------------------------

def test_select_analysts_returns_choices(qpatch):
    qpatch["checkbox"].append([AnalystType.MARKET, AnalystType.NEWS])
    assert u.select_analysts() == [AnalystType.MARKET, AnalystType.NEWS]


def test_select_analysts_exits_when_empty(qpatch):
    qpatch["checkbox"].append([])
    with pytest.raises(SystemExit):
        u.select_analysts()


def test_select_research_depth_returns_value(qpatch):
    qpatch["select"].append(3)
    assert u.select_research_depth() == 3


def test_select_research_depth_exits_when_none(qpatch):
    qpatch["select"].append(None)
    with pytest.raises(SystemExit):
        u.select_research_depth()


# ---------------------------------------------------------------------------
# OpenRouter model selection + network fetch
# ---------------------------------------------------------------------------

def test_fetch_openrouter_models_parses(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"name": "M1", "id": "a/m1"}, {"id": "b/m2"}]}

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    out = u._fetch_openrouter_models()
    assert out == [("M1", "a/m1"), ("b/m2", "b/m2")]


def test_fetch_openrouter_models_handles_error(monkeypatch):
    import requests
    def _boom(*a, **k):
        raise RuntimeError("net down")
    monkeypatch.setattr(requests, "get", _boom)
    assert u._fetch_openrouter_models() == []


def test_select_openrouter_model_picks_listed(qpatch, monkeypatch):
    monkeypatch.setattr(u, "_fetch_openrouter_models", lambda: [("M1", "a/m1")])
    qpatch["select"].append("a/m1")
    assert u.select_openrouter_model() == "a/m1"


def test_select_openrouter_model_custom_path(qpatch, monkeypatch):
    monkeypatch.setattr(u, "_fetch_openrouter_models", lambda: [])
    qpatch["select"].append("custom")
    qpatch["text"].append("  org/model ")
    assert u.select_openrouter_model() == "org/model"


def test_prompt_custom_model_id(qpatch):
    qpatch["text"].append("  my/model ")
    assert u._prompt_custom_model_id() == "my/model"


# ---------------------------------------------------------------------------
# _select_model dispatch (openrouter / azure / catalog / custom / exit)
# ---------------------------------------------------------------------------

def test_select_model_openrouter_delegates(monkeypatch):
    monkeypatch.setattr(u, "select_openrouter_model", lambda: "or/model")
    assert u._select_model("OpenRouter", "quick") == "or/model"


def test_select_model_azure_prompts_deployment(qpatch):
    qpatch["text"].append("  my-deploy ")
    assert u._select_model("azure", "deep") == "my-deploy"


def test_select_model_catalog_choice(qpatch, monkeypatch):
    monkeypatch.setattr(u, "get_model_options", lambda p, m: [("Disp", "val")])
    qpatch["select"].append("val")
    assert u._select_model("google", "quick") == "val"


def test_select_model_catalog_custom(qpatch, monkeypatch):
    monkeypatch.setattr(u, "get_model_options", lambda p, m: [("Custom", "custom")])
    qpatch["select"].append("custom")
    qpatch["text"].append("typed/id")
    assert u._select_model("google", "deep") == "typed/id"


def test_select_model_catalog_exits_on_none(qpatch, monkeypatch):
    monkeypatch.setattr(u, "get_model_options", lambda p, m: [("Disp", "val")])
    qpatch["select"].append(None)
    with pytest.raises(SystemExit):
        u._select_model("google", "quick")


def test_shallow_and_deep_wrappers(monkeypatch):
    monkeypatch.setattr(u, "_select_model", lambda p, m: f"{p}:{m}")
    assert u.select_shallow_thinking_agent("google") == "google:quick"
    assert u.select_deep_thinking_agent("google") == "google:deep"


# ---------------------------------------------------------------------------
# select_llm_provider
# ---------------------------------------------------------------------------

def test_select_llm_provider_returns_tuple(qpatch):
    qpatch["select"].append(("google", None))
    assert u.select_llm_provider() == ("google", None)


def test_select_llm_provider_exits_on_none(qpatch):
    qpatch["select"].append(None)
    with pytest.raises(SystemExit):
        u.select_llm_provider()


# ---------------------------------------------------------------------------
# effort / thinking / language pickers
# ---------------------------------------------------------------------------

def test_ask_openai_reasoning_effort(qpatch):
    qpatch["select"].append("high")
    assert u.ask_openai_reasoning_effort() == "high"


def test_ask_anthropic_effort(qpatch):
    qpatch["select"].append("medium")
    assert u.ask_anthropic_effort() == "medium"


def test_ask_gemini_thinking_config(qpatch):
    qpatch["select"].append("minimal")
    assert u.ask_gemini_thinking_config() == "minimal"


def test_ask_output_language_simple(qpatch):
    qpatch["select"].append("Spanish")
    assert u.ask_output_language() == "Spanish"


def test_ask_output_language_custom(qpatch):
    qpatch["select"].append("custom")
    qpatch["text"].append(" Turkish ")
    assert u.ask_output_language() == "Turkish"
