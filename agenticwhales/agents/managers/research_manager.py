"""Research Manager: closes the bull/bear debate.

Two modes:
- full (default): structured ResearchPlan with a rating — feeds the Trader and
  the rest of the trading tail (recipe/lab path).
- brief: a research SYNTHESIS for a human reader — no rating, no recommendation,
  no position language. This is the Analyst Desk terminal node: the graph ends
  here, so nothing downstream ever turns the synthesis into a trade.
"""

from __future__ import annotations

from agenticwhales.agents.schemas import ResearchPlan, render_research_plan
from agenticwhales.agents.utils.agent_utils import build_instrument_context
from agenticwhales.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)

# The Analyst Desk synthesis structure. Deliberately scenario-shaped: it tells
# the reader where the evidence points and what would change each side's mind —
# never what to do. (Directive bans are load-bearing product posture; see
# web/static/methodology.html.)
BRIEF_SYNTHESIS_INSTRUCTIONS = """Close out this round of debate with a research synthesis for a human reader. You are NOT making a trading decision, and you must not issue one.

Structure the synthesis exactly as:
1. **Where the evidence converges** — points both sides effectively concede.
2. **The strongest bull argument and the strongest bear argument** — each judged on its merits, not its eloquence.
3. **Key risks and unknowns** — what this debate could not resolve with the available data.
4. **What would change each side's mind** — the concrete, observable developments that would strengthen or break each thesis.

Hard rules: no rating, no recommendation, no price target, no position or sizing language, no "investors should". This brief informs a reader who decides entirely on their own."""


def create_research_manager(llm, *, brief: bool = False):
    structured_llm = bind_structured(llm, ResearchPlan, "Research Manager")

    def research_manager_brief_node(state) -> dict:
        """Brief mode: free-text synthesis, no structured plan, no rating."""
        instrument_context = build_instrument_context(state["company_of_interest"])
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")

        snapshot_block = (state.get("market_snapshot") or "").strip()
        prefix = f"{snapshot_block}\n\n" if snapshot_block else ""

        quant_radar = (state.get("quant_radar") or "").strip()
        quant_block = (
            f"\n**Quant Radar (6-dim structured signal):** {quant_radar}\n"
            if quant_radar
            else ""
        )

        prompt = f"""{prefix}As the Research Manager and debate facilitator, {BRIEF_SYNTHESIS_INSTRUCTIONS}

{instrument_context}

---

**Synthesis integrity:** weigh the bull and bear cases on their merits, not on which side was more eloquent or spoke last. If one side's evidence is materially stronger, say so plainly; both sides being articulate is not evidence of balance.
{quant_block}
---

**Debate History:**
{history}"""

        resp = llm.invoke(prompt)
        content = getattr(resp, "content", resp)
        if isinstance(content, list):
            content = "\n".join(
                c.get("text", "") if isinstance(c, dict) else str(c) for c in content
            )
        synthesis = str(content).strip()

        return {
            "investment_debate_state": {
                "judge_decision": synthesis,
                "history": investment_debate_state.get("history", ""),
                "bear_history": investment_debate_state.get("bear_history", ""),
                "bull_history": investment_debate_state.get("bull_history", ""),
                "current_response": synthesis,
                "count": investment_debate_state["count"],
            },
            "investment_plan": synthesis,
        }

    def research_manager_node(state) -> dict:
        instrument_context = build_instrument_context(state["company_of_interest"])
        history = state["investment_debate_state"].get("history", "")

        investment_debate_state = state["investment_debate_state"]

        position_block = (state.get("current_position") or "").strip()
        snapshot_block = (state.get("market_snapshot") or "").strip()
        prefix_parts = [b for b in (snapshot_block, position_block) if b]
        position_prefix = "\n\n".join(prefix_parts) + "\n\n" if prefix_parts else ""

        quant_radar = (state.get("quant_radar") or "").strip()
        quant_block = (
            f"\n**Quant Radar (6-dim structured signal):** {quant_radar}\n"
            if quant_radar
            else ""
        )

        prompt = f"""{position_prefix}As the Research Manager and debate facilitator, your role is to critically evaluate this round of debate and deliver a clear, actionable investment plan for the trader.

{instrument_context}

---

**Decision-making integrity (Authority Framing):**
You are the synthesizer. Your job is to weigh the bull and bear cases on their merits, not to ratify whichever case is more eloquent or which side spoke last. If one side's logic is sound, accept it regardless of which agent provided it. Stranger-rejection — discounting a correct argument because it comes from a debater style you typically agree with less — is a documented failure mode in multi-agent synthesizers. Likewise, both sides being articulate is not evidence of balance; if one side's evidence is materially stronger, commit to that side.

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction in the bull thesis; recommend taking or growing the position
- **Overweight**: Constructive view; recommend gradually increasing exposure
- **Hold**: Balanced view; recommend maintaining the current position
- **Underweight**: Cautious view; recommend trimming exposure
- **Sell**: Strong conviction in the bear thesis; recommend exiting or avoiding the position

If a USER'S CURRENT POSITION block is shown above, you MUST translate the rating into the side-specific vocabulary defined there. For example, if the user is short, "Buy" means add to the short, NOT buy stock. Reserve Hold for situations where the evidence on both sides is genuinely balanced.
{quant_block}
---

**Debate History:**
{history}"""

        investment_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_research_plan,
            "Research Manager",
        )

        new_investment_debate_state = {
            "judge_decision": investment_plan,
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": investment_plan,
            "count": investment_debate_state["count"],
        }

        return {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": investment_plan,
        }

    return research_manager_brief_node if brief else research_manager_node
