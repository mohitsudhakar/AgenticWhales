"""Coverage for agenticwhales/graph/conditional_logic.py (pure routing) and
graph/setup.py (graph construction). The node factories only close over the
LLM — they don't invoke it at build time — so a trivial fake LLM + callable
tool nodes are enough to compile the whole StateGraph offline.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agenticwhales.graph.conditional_logic import ConditionalLogic
from agenticwhales.graph.setup import GraphSetup


def _msg(tool_calls):
    return SimpleNamespace(tool_calls=tool_calls)


# ===========================================================================
# ConditionalLogic — analyst should_continue_* routing
# ===========================================================================

@pytest.mark.parametrize("analyst", ["market", "social", "news", "fundamentals", "quant"])
def test_should_continue_analyst_routes(analyst):
    cl = ConditionalLogic()
    method = getattr(cl, f"should_continue_{analyst}")
    # has tool calls → route to tools_<analyst>
    assert method({"messages": [_msg(["t"])]}) == f"tools_{analyst}"
    # no tool calls → route to clear
    clear = method({"messages": [_msg([])]})
    assert clear.startswith("Msg Clear")


# ===========================================================================
# ConditionalLogic.should_continue_debate
# ===========================================================================

def test_debate_ends_at_round_cap():
    cl = ConditionalLogic(max_debate_rounds=1)
    state = {"investment_debate_state": {"count": 2, "current_response": "Bull: x"}}
    assert cl.should_continue_debate(state) == "Research Manager"


def test_debate_bull_then_bear():
    cl = ConditionalLogic(max_debate_rounds=2)
    state = {"investment_debate_state": {"count": 0, "current_response": "Bull: x"}}
    assert cl.should_continue_debate(state) == "Bear Researcher"


def test_debate_default_to_bull():
    cl = ConditionalLogic(max_debate_rounds=2)
    state = {"investment_debate_state": {"count": 1, "current_response": "Bear: y"}}
    assert cl.should_continue_debate(state) == "Bull Researcher"


# ===========================================================================
# ConditionalLogic.should_continue_risk_analysis
# ===========================================================================

def test_risk_ends_at_round_cap():
    cl = ConditionalLogic(max_risk_discuss_rounds=1)
    state = {"risk_debate_state": {"count": 3, "latest_speaker": "Aggressive"}}
    assert cl.should_continue_risk_analysis(state) == "Portfolio Manager"


def test_risk_aggressive_to_conservative():
    cl = ConditionalLogic(max_risk_discuss_rounds=2)
    state = {"risk_debate_state": {"count": 0, "latest_speaker": "Aggressive"}}
    assert cl.should_continue_risk_analysis(state) == "Conservative Analyst"


def test_risk_conservative_to_neutral():
    cl = ConditionalLogic(max_risk_discuss_rounds=2)
    state = {"risk_debate_state": {"count": 1, "latest_speaker": "Conservative"}}
    assert cl.should_continue_risk_analysis(state) == "Neutral Analyst"


def test_risk_default_to_aggressive():
    cl = ConditionalLogic(max_risk_discuss_rounds=2)
    state = {"risk_debate_state": {"count": 1, "latest_speaker": "Neutral"}}
    assert cl.should_continue_risk_analysis(state) == "Aggressive Analyst"


# ===========================================================================
# GraphSetup.setup_graph
# ===========================================================================

class _FakeLLM:
    """Closed-over by node factories; never invoked at build time."""
    def bind_tools(self, tools):
        return self

    def invoke(self, *a, **k):  # pragma: no cover - not called during build
        return SimpleNamespace(content="x", tool_calls=[])


ANALYSTS = ["market", "quant", "social", "news", "fundamentals"]


def _setup(**over):
    fake = _FakeLLM()
    tool_nodes = {a: (lambda state: state) for a in ANALYSTS}
    kwargs = dict(
        quick_thinking_llm=fake, deep_thinking_llm=fake,
        tool_nodes=tool_nodes, conditional_logic=ConditionalLogic(),
    )
    kwargs.update(over)
    return GraphSetup(**kwargs)


def test_setup_graph_all_analysts_compiles():
    g = _setup()
    workflow = g.setup_graph(ANALYSTS)
    compiled = workflow.compile()
    assert compiled is not None
    assert "Portfolio Manager" in workflow.nodes


def test_setup_graph_single_analyst():
    g = _setup()
    workflow = g.setup_graph(["market"])
    assert "Market Analyst" in workflow.nodes
    assert "Bull Researcher" in workflow.nodes


def test_setup_graph_empty_raises():
    g = _setup()
    with pytest.raises(ValueError):
        g.setup_graph([])


def test_setup_graph_uses_debater_and_manager_overrides():
    fake = _FakeLLM()
    rm, pm = _FakeLLM(), _FakeLLM()
    debaters = {"bull": _FakeLLM(), "bear": _FakeLLM(), "aggressive": _FakeLLM(),
                "conservative": _FakeLLM(), "neutral": _FakeLLM()}
    g = _setup(research_manager_llm=rm, portfolio_manager_llm=pm,
               debater_llms=debaters, blind_first_round=True)
    assert g.research_manager_llm is rm and g.portfolio_manager_llm is pm
    assert g.bull_llm is debaters["bull"] and g.blind_first_round is True
    g.setup_graph(["market", "news"])  # builds with two analysts in sequence


def test_setup_graph_manager_fallback_to_deep():
    g = _setup()
    # no overrides → both synthesizers fall back to deep_thinking_llm
    assert g.research_manager_llm is g.deep_thinking_llm
    assert g.portfolio_manager_llm is g.deep_thinking_llm
