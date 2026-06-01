"""Coverage for the researcher + risk-management debator node factories.
Each factory returns a graph node that builds a prompt and calls llm.invoke.
We use a fake llm that records the prompt and returns a canned .content, then
assert state-threading and the blind/non-blind prompt branches. No real LLM.
"""

from __future__ import annotations

import pytest

from agenticwhales.agents.researchers.bull_researcher import create_bull_researcher
from agenticwhales.agents.researchers.bear_researcher import create_bear_researcher
from agenticwhales.agents.risk_mgmt.aggressive_debator import create_aggressive_debator
from agenticwhales.agents.risk_mgmt.conservative_debator import create_conservative_debator
from agenticwhales.agents.risk_mgmt.neutral_debator import create_neutral_debator


class _FakeLLM:
    def __init__(self, content="canned argument"):
        self.content = content
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return type("Resp", (), {"content": self.content})()


def _reports():
    return {
        "market_report": "MKT",
        "sentiment_report": "SENT",
        "news_report": "NEWS",
        "fundamentals_report": "FUND",
    }


# ===========================================================================
# Researchers (investment_debate_state)
# ===========================================================================

def _inv_state(count=0, **over):
    s = {"history": "h", "bull_history": "bh", "bear_history": "beh",
         "current_response": "prev", "count": count}
    s.update(over)
    return s


@pytest.mark.parametrize("factory,speaker,hist_key", [
    (create_bull_researcher, "Bull Analyst", "bull_history"),
    (create_bear_researcher, "Bear Analyst", "bear_history"),
])
def test_researcher_non_blind_threads_state(factory, speaker, hist_key):
    llm = _FakeLLM()
    node = factory(llm, blind_first_round=False)
    state = {**_reports(), "investment_debate_state": _inv_state(count=2),
             "current_position": "LONG 10 AAPL", "market_snapshot": "snap"}
    out = node(state)
    ids = out["investment_debate_state"]
    assert ids["count"] == 3
    assert f"{speaker}:" in ids["current_response"]
    assert f"{speaker}:" in ids[hist_key]
    # non-blind prompt shows the opponent's last argument + history
    assert "Conversation history of the debate" in llm.prompts[0]
    # position + snapshot prefix present
    assert "snap" in llm.prompts[0] and "LONG 10 AAPL" in llm.prompts[0]


@pytest.mark.parametrize("factory", [create_bull_researcher, create_bear_researcher])
def test_researcher_blind_first_round_hides_history(factory):
    llm = _FakeLLM()
    node = factory(llm, blind_first_round=True)
    state = {**_reports(), "investment_debate_state": _inv_state(count=0)}
    node(state)
    assert "Conversation history of the debate" not in llm.prompts[0]
    assert "independent opening" in llm.prompts[0]


def test_researcher_no_position_prefix_when_blank():
    llm = _FakeLLM()
    node = create_bull_researcher(llm)
    state = {**_reports(), "investment_debate_state": _inv_state(),
             "current_position": "   ", "market_snapshot": ""}
    node(state)
    # nothing to prefix → prompt starts with the persona line
    assert llm.prompts[0].startswith("You are a Bull Analyst")


# ===========================================================================
# Risk-mgmt debators (risk_debate_state)
# ===========================================================================

def _risk_state(count=0, **over):
    s = {"history": "h", "aggressive_history": "ah", "conservative_history": "ch",
         "neutral_history": "nh", "current_aggressive_response": "ar",
         "current_conservative_response": "cr", "current_neutral_response": "nr",
         "count": count}
    s.update(over)
    return s


RISK = [
    (create_aggressive_debator, "Aggressive", "Aggressive"),
    (create_conservative_debator, "Conservative", "Conservative"),
    (create_neutral_debator, "Neutral", "Neutral"),
]


@pytest.mark.parametrize("factory,speaker,latest", RISK)
def test_risk_non_blind_threads_state(factory, speaker, latest):
    llm = _FakeLLM()
    node = factory(llm, blind_first_round=False)
    state = {**_reports(), "risk_debate_state": _risk_state(count=3),
             "trader_investment_plan": "PLAN", "current_position": "SHORT 5 NVDA"}
    out = node(state)
    rds = out["risk_debate_state"]
    assert rds["count"] == 4
    assert rds["latest_speaker"] == latest
    assert f"{speaker} Analyst:" in rds["history"]
    assert "current conversation history" in llm.prompts[0]
    assert "PLAN" in llm.prompts[0] and "SHORT 5 NVDA" in llm.prompts[0]


@pytest.mark.parametrize("factory,speaker,latest", RISK)
def test_risk_blind_first_round_hides_peers(factory, speaker, latest):
    llm = _FakeLLM()
    node = factory(llm, blind_first_round=True)
    state = {**_reports(), "risk_debate_state": _risk_state(count=1),
             "trader_investment_plan": "PLAN"}
    out = node(state)
    assert out["risk_debate_state"]["latest_speaker"] == latest
    assert "independent opening" in llm.prompts[0]
    assert "current conversation history" not in llm.prompts[0]


@pytest.mark.parametrize("factory,speaker,latest", RISK)
def test_risk_blind_disabled_when_count_high(factory, speaker, latest):
    # blind_first_round=True but count>2 → full peer history shown
    llm = _FakeLLM()
    node = factory(llm, blind_first_round=True)
    state = {**_reports(), "risk_debate_state": _risk_state(count=5),
             "trader_investment_plan": "PLAN"}
    node(state)
    assert "current conversation history" in llm.prompts[0]
