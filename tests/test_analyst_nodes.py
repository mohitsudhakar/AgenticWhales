"""Coverage for the prose analyst node factories (market/news/social/
fundamentals) and the quant analyst. Each node builds a ChatPromptTemplate and
pipes it into llm.bind_tools(...). We use a RunnableLambda-backed fake LLM so
the `prompt | llm` composition is a real runnable; no network, no real model.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.runnables import RunnableLambda

from agenticwhales.agents.analysts.market_analyst import create_market_analyst
from agenticwhales.agents.analysts.news_analyst import create_news_analyst
from agenticwhales.agents.analysts.social_media_analyst import create_social_media_analyst
from agenticwhales.agents.analysts.fundamentals_analyst import create_fundamentals_analyst
from agenticwhales.agents.analysts.quant_analyst import create_quant_analyst


class _FakeLLM:
    """A fake whose bind_tools returns a RunnableLambda emitting a canned result."""

    def __init__(self, tool_calls=None, content="THE REPORT"):
        self._tool_calls = tool_calls or []
        self._content = content

    def bind_tools(self, tools):
        result = SimpleNamespace(tool_calls=self._tool_calls, content=self._content)
        return RunnableLambda(lambda _messages: result)


def _state():
    return {"trade_date": "2024-01-02", "company_of_interest": "AAPL", "messages": []}


# ===========================================================================
# prose analysts — report set only when no tool calls
# ===========================================================================

@pytest.mark.parametrize("factory,report_key", [
    (create_market_analyst, "market_report"),
    (create_news_analyst, "news_report"),
    (create_social_media_analyst, "sentiment_report"),
    (create_fundamentals_analyst, "fundamentals_report"),
])
def test_prose_analyst_emits_report_when_no_tool_calls(factory, report_key):
    node = factory(_FakeLLM(tool_calls=[], content="FINAL REPORT"))
    out = node(_state())
    assert out[report_key] == "FINAL REPORT"
    assert out["messages"][0].content == "FINAL REPORT"


@pytest.mark.parametrize("factory,report_key", [
    (create_market_analyst, "market_report"),
    (create_news_analyst, "news_report"),
    (create_social_media_analyst, "sentiment_report"),
    (create_fundamentals_analyst, "fundamentals_report"),
])
def test_prose_analyst_no_report_when_tool_calls(factory, report_key):
    node = factory(_FakeLLM(tool_calls=[{"name": "get_news", "args": {}, "id": "1"}]))
    out = node(_state())
    # still gathering evidence → report stays empty
    assert out[report_key] == ""


# ===========================================================================
# quant analyst — two-stage flow
# ===========================================================================

def test_quant_analyst_loops_back_on_tool_calls(monkeypatch):
    # bind_structured is called at factory time — stub it out.
    import agenticwhales.agents.analysts.quant_analyst as qa
    monkeypatch.setattr(qa, "bind_structured", lambda llm, schema, name: object())
    node = create_quant_analyst(_FakeLLM(tool_calls=[{"name": "get_stock_data",
                                                      "args": {}, "id": "1"}]))
    out = node(_state())
    assert "quant_radar" not in out  # still gathering → just messages
    assert out["messages"]


def test_quant_analyst_emits_radar_when_done(monkeypatch):
    import agenticwhales.agents.analysts.quant_analyst as qa
    monkeypatch.setattr(qa, "bind_structured", lambda llm, schema, name: object())
    monkeypatch.setattr(qa, "invoke_structured_or_freetext",
                        lambda structured, llm, prompt, render, name: "RADAR MD")
    node = create_quant_analyst(_FakeLLM(tool_calls=[], content="quant prose"))
    out = node(_state())
    assert out["quant_radar"] == "RADAR MD"
