"""J3 (2026-06-08 executive critique): prove a *graph-driven* run cannot
fetch future data.

The unit suite (tests/test_asof_dataflow.py) shows ``route_to_vendor()``
enforces the as-of guard when called directly. What it never proved is the
end-to-end guarantee: that an *agent-graph* run — the production LangGraph
wiring, real ToolNodes, the real langchain tools the analysts call — cannot
reach a vendor with a date past as-of, no matter what the LLM asks for.

This test drives the real graph from ``GraphSetup`` with an adversarial
scripted LLM that requests **future-dated data from every tool it is
offered**, against stub vendors patched into ``VENDOR_METHODS`` (i.e. *below*
``route_to_vendor``, so the whole dataflow toolchain runs for real) that
record every call they receive. The assertions:

  * control (no as-of): the future-dated requests DO reach the vendor — the
    harness is genuinely leaky by construction, so the guarded runs are not
    vacuous;
  * under ``as_of_date``: every analyst tool fires, and no date that reaches
    a vendor exceeds as-of (future requests are truncated to exactly as-of);
  * under ``as_of_date(strict=True)``: future-dated requests never reach a
    vendor at all — ``LookAheadViolation`` fires inside ``route_to_vendor``
    and surfaces as an error ToolMessage, and the graph still terminates;
  * toolchain level: each date-bearing analyst tool raises
    ``LookAheadViolation`` in strict mode before any vendor dispatch.

Hermetic: no network, no API keys, no Docker — which is why this file is
*not* marked ``pytest.mark.integration`` (that marker gates the
testcontainers suite); it runs in the default ``pytest -q`` sweep.
"""

from __future__ import annotations

import datetime as dt
import re
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langgraph.prebuilt import ToolNode

from agenticwhales.agents.utils.agent_utils import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_global_news,
    get_income_statement,
    get_indicators,
    get_insider_transactions,
    get_news,
    get_risk_metrics,
    get_stock_data,
)
from agenticwhales.asof import LookAheadViolation, as_of_date
from agenticwhales.graph.conditional_logic import ConditionalLogic
from agenticwhales.graph.propagation import Propagator
from agenticwhales.graph.setup import GraphSetup

AS_OF = dt.date(2024, 6, 1)
FUTURE = "2025-03-01"        # what the scripted LLM asks for — past AS_OF
PAST_START = "2024-01-02"    # in-bounds start so truncation never empties a window

# The args the scripted analyst LLM passes whenever a tool with this name is
# offered to it. Deliberately leaky: every end/current date is past AS_OF.
LEAKY_TOOL_ARGS = {
    "get_stock_data": {"symbol": "AAPL", "start_date": PAST_START, "end_date": FUTURE},
    "get_indicators": {"symbol": "AAPL", "indicator": "rsi", "curr_date": FUTURE,
                       "look_back_days": 30},
    "get_fundamentals": {"ticker": "AAPL", "curr_date": FUTURE},
    "get_balance_sheet": {"ticker": "AAPL", "freq": "quarterly", "curr_date": FUTURE},
    "get_cashflow": {"ticker": "AAPL", "freq": "quarterly", "curr_date": FUTURE},
    "get_income_statement": {"ticker": "AAPL", "freq": "quarterly", "curr_date": FUTURE},
    "get_news": {"ticker": "AAPL", "start_date": PAST_START, "end_date": FUTURE},
    "get_global_news": {"curr_date": FUTURE, "look_back_days": 7, "limit": 5},
}

# Position of the end/current date in each vendor method's positional args.
# Declared independently of interface._DATE_ARG_POS on purpose: if the
# production map drifts, this test fails instead of mirroring the drift.
VENDOR_DATE_POS = {
    "get_stock_data": 2,
    "get_indicators": 2,
    "get_fundamentals": 1,
    "get_balance_sheet": 2,
    "get_cashflow": 2,
    "get_income_statement": 2,
    "get_news": 2,
    "get_global_news": 0,
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


class LeakyScriptedLLM:
    """Stands in for every agent in the graph.

    Analysts (via ``bind_tools``) request future-dated data for every tool
    they are offered on their first turn, then write a report once tool
    results (or tool errors) come back. Prose agents (researchers, debaters)
    get canned text from ``invoke``. Structured output is unsupported, so
    Trader / managers take the production free-text fallback path.
    """

    def bind_tools(self, tools):
        offered = [t.name for t in tools]

        def respond(prompt_value):
            messages = prompt_value.to_messages()
            if any(isinstance(m, ToolMessage) for m in messages):
                return AIMessage(content="FINAL REPORT: evidence gathered.")
            calls = [
                {"name": name, "args": LEAKY_TOOL_ARGS[name],
                 "id": f"call_{i}_{name}", "type": "tool_call"}
                for i, name in enumerate(offered)
                if name in LEAKY_TOOL_ARGS
            ]
            if not calls:
                return AIMessage(content="FINAL REPORT: no data needed.")
            return AIMessage(content="", tool_calls=calls)

        return RunnableLambda(respond)

    def with_structured_output(self, schema):
        raise NotImplementedError("scripted test LLM is free-text only")

    def invoke(self, prompt):
        return AIMessage(content="Scripted synthesis.")


def _recording_stub_vendors(record):
    """One stub vendor per method; each appends (method, args) to `record`."""
    def make(method):
        def vendor_stub(*args, **kwargs):
            record.append((method, args + tuple(kwargs.values())))
            return f"[stub:{method}] ok"
        return vendor_stub
    return {m: {"stub": make(m)} for m in LEAKY_TOOL_ARGS}


def _build_graph(llm):
    """The production graph shape: GraphSetup with the same five ToolNodes
    AgenticWhalesGraph._create_tool_nodes builds (default error handling and
    all), compiled with the real conditional logic."""
    tool_nodes = {
        "market": ToolNode([get_stock_data, get_indicators]),
        "social": ToolNode([get_news]),
        "news": ToolNode([get_news, get_global_news, get_insider_transactions]),
        "fundamentals": ToolNode([get_fundamentals, get_balance_sheet,
                                  get_cashflow, get_income_statement]),
        "quant": ToolNode([get_stock_data, get_indicators, get_risk_metrics]),
    }
    setup = GraphSetup(
        quick_thinking_llm=llm,
        deep_thinking_llm=llm,
        tool_nodes=tool_nodes,
        conditional_logic=ConditionalLogic(max_debate_rounds=1,
                                           max_risk_discuss_rounds=1),
    )
    workflow = setup.setup_graph(["market", "quant", "social", "news", "fundamentals"])
    return workflow.compile()


def _drive_graph(*, bound=None, strict=False):
    """Run the full graph against recording stub vendors; return
    (recorded_calls, final_state)."""
    record = []
    propagator = Propagator()
    graph = _build_graph(LeakyScriptedLLM())
    state = propagator.create_initial_state("AAPL", AS_OF.isoformat())
    args = propagator.get_graph_args()
    with patch.dict("agenticwhales.dataflows.interface.VENDOR_METHODS",
                    _recording_stub_vendors(record), clear=True):
        if bound is None:
            final = graph.invoke(state, **args)
        else:
            with as_of_date(bound, strict=strict):
                final = graph.invoke(state, **args)
    return record, final


def _dates_in(call_args):
    """Every parseable YYYY-MM-DD in a recorded call's arguments."""
    return [dt.date.fromisoformat(a[:10]) for a in call_args
            if isinstance(a, str) and _DATE_RE.match(a)]


# ── control: the harness really is leaky ───────────────────────────────────

def test_control_without_asof_future_dates_reach_vendor():
    """Without the guard, the scripted LLM's future-dated requests reach the
    vendor verbatim. This is the non-vacuity check for everything below."""
    record, final = _drive_graph(bound=None)

    leaked = [d for _, args in record for d in _dates_in(args) if d > AS_OF]
    assert leaked, "control run should leak future dates without as_of_date"
    # Every date-bearing analyst tool actually fired.
    assert {m for m, _ in record} == set(LEAKY_TOOL_ARGS)
    assert final["final_trade_decision"]


# ── the J3 guarantee: graph-driven run cannot fetch future data ────────────

def test_graph_driven_run_cannot_fetch_future_data():
    """Full agent-graph run under as_of_date: every tool the analysts use
    fires, and no request that reaches a vendor exceeds the as-of date."""
    record, final = _drive_graph(bound=AS_OF)

    # Non-vacuous: all eight date-bearing vendor methods were exercised
    # through the real ToolNode → langchain tool → route_to_vendor chain.
    assert {m for m, _ in record} == set(LEAKY_TOOL_ARGS)

    # The core J3 assertion: nothing past as-of ever reached a vendor —
    # scanned across EVERY argument of EVERY recorded call.
    violations = [(m, d) for m, args in record for d in _dates_in(args) if d > AS_OF]
    assert violations == []

    # And the truncation is exact: each method's end/current date arrived
    # as precisely the as-of date, not merely "something in the past".
    for method, args in record:
        assert args[VENDOR_DATE_POS[method]] == AS_OF.isoformat(), method

    # The run completed end-to-end (analysts → debate → trader → PM).
    assert final["final_trade_decision"]
    assert final["market_report"]


def test_strict_graph_run_blocks_future_requests_before_vendor():
    """Strict mode (the replay-acceptance posture): future-dated requests
    raise inside route_to_vendor, so they never reach a vendor at all. The
    ToolNode surfaces the violation as an error ToolMessage and the graph
    still terminates instead of hanging mid-backtest."""
    record, final = _drive_graph(bound=AS_OF, strict=True)

    # Every scripted request was future-dated, so in strict mode no
    # date-bearing vendor method may have been dispatched.
    assert record == []
    assert final["final_trade_decision"]


# ── toolchain level: each analyst tool enforces strict as-of ───────────────

@pytest.mark.parametrize("tool", [
    get_stock_data, get_indicators, get_fundamentals, get_balance_sheet,
    get_cashflow, get_income_statement, get_news, get_global_news,
], ids=lambda t: t.name)
def test_analyst_tool_raises_lookahead_in_strict_mode(tool):
    """Invoking the langchain tools directly (the exact objects the ToolNodes
    wrap) with a future-dated request raises LookAheadViolation before any
    vendor dispatch."""
    record = []
    with patch.dict("agenticwhales.dataflows.interface.VENDOR_METHODS",
                    _recording_stub_vendors(record), clear=True):
        with as_of_date(AS_OF, strict=True):
            with pytest.raises(LookAheadViolation):
                tool.invoke(LEAKY_TOOL_ARGS[tool.name])
    assert record == []
