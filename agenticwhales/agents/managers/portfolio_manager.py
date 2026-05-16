"""Portfolio Manager: synthesises the risk-analyst debate into the final decision.

Uses LangChain's ``with_structured_output`` so the LLM produces a typed
``PortfolioDecision`` directly, in a single call.  The result is rendered
back to markdown for storage in ``final_trade_decision`` so memory log,
CLI display, and saved reports continue to consume the same shape they do
today.  When a provider does not expose structured output, the agent falls
back gracefully to free-text generation.
"""

from __future__ import annotations

from agenticwhales.agents.schemas import PortfolioDecision, render_pm_decision
from agenticwhales.agents.utils.agent_utils import (
    build_instrument_context,
    get_language_instruction,
)
from agenticwhales.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)


def create_portfolio_manager(llm):
    structured_llm = bind_structured(llm, PortfolioDecision, "Portfolio Manager")

    def portfolio_manager_node(state) -> dict:
        instrument_context = build_instrument_context(state["company_of_interest"])

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        research_plan = state["investment_plan"]
        trader_plan = state["trader_investment_plan"]

        past_context = state.get("past_context", "")
        lessons_line = (
            f"- Lessons from prior decisions and outcomes:\n{past_context}\n"
            if past_context
            else ""
        )

        recent_performance = (state.get("recent_performance") or "").strip()
        recent_perf_block = (
            f"\n**Recent track record on this ticker:** {recent_performance}\n"
            "Use this as a self-adaptive risk signal: if cumulative alpha is negative, "
            "prioritize capital preservation (tighter stops, smaller size, raise the bar for Buy/Sell). "
            "If positive and trending up, you may accept higher conviction trades. "
            "This is NOT a momentum strategy — it is a state-dependent risk budget.\n"
            if recent_performance
            else ""
        )

        quant_radar = (state.get("quant_radar") or "").strip()
        quant_block = (
            f"\n**Quant Radar (6-dim structured signal):** {quant_radar}\n"
            if quant_radar
            else ""
        )

        position_block = (state.get("current_position") or "").strip()
        snapshot_block = (state.get("market_snapshot") or "").strip()
        prefix_parts = [b for b in (snapshot_block, position_block) if b]
        position_prefix = "\n\n".join(prefix_parts) + "\n\n" if prefix_parts else ""

        prompt = f"""{position_prefix}As the Portfolio Manager, synthesize the risk analysts' debate and deliver the final trading decision.

{instrument_context}

---

**Decision-making integrity (Authority Framing):**
You are the final synthesizer. Your job is to weigh the evidence on its merits, not to ratify the consensus or defer to the loudest voice. If a debater's logic is sound, accept it regardless of which agent provided it or how many others disagreed. Stranger-rejection — discounting a correct argument because it came from outside your usual reasoning style — is a documented failure mode in multi-agent synthesizers. Likewise, agreement among debaters is not evidence of correctness; if all three converge on a weak argument, you must still rule against them. Your output is the trade, not the meeting minutes.

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction to enter or add to position
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current position. Reserve for two cases: (a) the analysts' evidence is genuinely balanced — both sides bring comparable-quality evidence and the truth is unresolved; (b) the user already holds the right exposure given the evidence; OR (c) the three risk debaters do not align on direction (no 2-of-3 majority on Buy-ish vs Sell-ish vs Hold-ish). Disagreement among the three risk perspectives is itself information — when consensus is absent, the correct answer is usually no-trade. "I am not sure" is a valid conclusion. But do not retreat to Hold whenever the losing side has *any* point — most setups have asymmetric evidence quality and the call is to follow the stronger side.
- **Underweight**: Reduce exposure, take partial profits
- **Sell**: Exit position or avoid entry

If a USER'S CURRENT POSITION block is shown above, you MUST translate the rating into the side-specific vocabulary defined there. Example mappings when the user is **short**: Buy = add to short; Overweight = lightly add to short; Hold = maintain short as-is; Underweight = reduce / cover part of the short; Sell = cover the short fully or reverse to long. NEVER recommend "reducing long exposure" if the user is short — they have none. Whenever possible, name a concrete size (e.g. "cover 25%", "add 0.5x current size") and a stop / target derived from the analysts' levels.

**Bracket levels (required for any directional rating):**
For every Buy / Overweight / Underweight / Sell decision, you MUST fill `stop_loss` and `take_profit` in the structured output, derived from the analysts' support/resistance and volatility readings. Aim for a take-profit / stop-loss risk-reward ratio of at least 1.2:1 (QuantAgent 2025 baseline). Leave both null only when rating is Hold.

**Context:**
- Research Manager's investment plan: **{research_plan}**
- Trader's transaction proposal: **{trader_plan}**
{lessons_line}{recent_perf_block}{quant_block}
**Risk Analysts Debate History:**
{history}

---

Ground every conclusion in specific evidence from the analysts and weight the evidence by quality: concrete data (numbers, citations, filings) outweighs narrative, pattern-recognition, or sentiment. When one side's evidence is materially stronger — even if the other side raises some valid points — commit to that side. Reserve **Hold** for genuinely balanced cases or no-consensus among risk debaters. Both reflexive overcommitment and reflexive hedging destroy capital; calibrate to the actual evidence asymmetry.{get_language_instruction()}"""

        final_trade_decision = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_pm_decision,
            "Portfolio Manager",
        )

        new_risk_debate_state = {
            "judge_decision": final_trade_decision,
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_trade_decision,
        }

    return portfolio_manager_node
