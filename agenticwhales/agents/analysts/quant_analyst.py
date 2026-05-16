"""Quant Analyst — QuantAgent-style 6-dim radar (Xiong et al. 2025).

Acts as a parallel track to the prose-based Market Analyst: gathers price
+ indicator data via the same tools, then emits a compact structured
``QuantRadar`` that downstream synthesizers can read without parsing
narrative.

The radar's 6 axes (volatility, S/R strength, breakout likelihood,
momentum strength, pattern reliability, trend certainty) plus an explicit
direction call are the QuantAgent paper's RiskAgent inputs. Surfacing
them as a typed object — rather than buried in prose — sidesteps
"rhetorical fluency" bias at the synthesizer.
"""

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from agenticwhales.agents.schemas import QuantRadar, render_quant_radar
from agenticwhales.agents.utils.agent_utils import (
    build_instrument_context,
    get_indicators,
    get_language_instruction,
    get_stock_data,
)
from agenticwhales.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)


_SYSTEM_MESSAGE = (
    "You are a quantitative analyst. Your job is to produce a compact, "
    "structured 6-dim radar signal from price action and technical "
    "indicators — nothing else. Use the available tools to fetch the "
    "stock's recent OHLC history and a small set of indicators (RSI, "
    "MACD, ATR, Bollinger Bands, 50/200 SMA, RoC). Then evaluate the six "
    "axes of the QuantRadar schema on a 1-10 integer scale each.\n\n"
    "Scoring discipline: anchor every score in a SPECIFIC indicator "
    "reading. Do not score from intuition. Use the lower half of the "
    "scale (1-5) for absent / weak signals; reserve 8-10 for textbook "
    "conditions. Mid-range (5-7) is the right answer when evidence is "
    "mixed.\n\n"
    "Output ONLY the structured QuantRadar — no preamble, no prose "
    "outside the schema's reasoning field. Be terse and grounded."
)


def create_quant_analyst(llm):
    """Build the Quant Analyst node.

    Two-stage flow:
      1. The tool-using LLM gathers OHLC + indicator data. While it still
         emits tool_calls, the graph routes back through the tools node.
      2. When the LLM stops calling tools, we invoke the structured
         binding to convert the gathered evidence into the 6-dim radar.
    """
    structured_llm = bind_structured(llm, QuantRadar, "Quant Analyst")

    def quant_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])

        tools = [get_stock_data, get_indicators]

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants. "
                    "Use the provided tools to gather price + indicator evidence, then "
                    "produce the QuantRadar. If you have all the data you need, stop "
                    "calling tools and emit your final answer.\n"
                    "You have access to the following tools: {tool_names}.\n{system_message}\n"
                    "For your reference, the current date is {current_date}. {instrument_context}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=_SYSTEM_MESSAGE + get_language_instruction())
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)
        result = chain.invoke(state["messages"])

        # Tools still needed — let the graph loop back through tools_quant.
        if result.tool_calls:
            return {"messages": [result]}

        # All evidence gathered. Convert the LLM's final natural-language
        # synthesis into the structured radar via a second, structured call.
        analysis_prose = result.content or ""
        radar_prompt = (
            "Based on the price + indicator evidence gathered above, "
            "produce the QuantRadar for this instrument. Anchor every "
            "score in a specific indicator reading from the evidence.\n\n"
            f"Analyst notes:\n{analysis_prose}"
        )
        radar_markdown = invoke_structured_or_freetext(
            structured_llm,
            llm,
            radar_prompt,
            render_quant_radar,
            "Quant Analyst",
        )

        return {
            "messages": [result],
            "quant_radar": radar_markdown,
        }

    return quant_analyst_node
