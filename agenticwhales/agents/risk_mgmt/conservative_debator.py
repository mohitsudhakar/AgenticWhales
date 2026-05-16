

def create_conservative_debator(llm, blind_first_round: bool = False):
    """Build the Conservative Risk Analyst node.

    ``blind_first_round`` hides peer responses on the opening three-turn
    cycle (count <= 2); see :func:`create_aggressive_debator`.
    """
    def conservative_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        conservative_history = risk_debate_state.get("conservative_history", "")

        current_aggressive_response = risk_debate_state.get("current_aggressive_response", "")
        current_neutral_response = risk_debate_state.get("current_neutral_response", "")

        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        trader_decision = state["trader_investment_plan"]
        position_block = (state.get("current_position") or "").strip()
        snapshot_block = (state.get("market_snapshot") or "").strip()
        prefix_parts = [b for b in (snapshot_block, position_block) if b]
        position_prefix = "\n\n".join(prefix_parts) + "\n\n" if prefix_parts else ""

        count = risk_debate_state.get("count", 0)
        is_blind = blind_first_round and count <= 2
        if is_blind:
            peer_block = (
                "This is your independent opening — make the strongest possible "
                "case for the conservative view based solely on the trader's plan and the "
                "underlying research, without anchoring on what aggressive or neutral "
                "analysts might say."
            )
            engagement_clause = (
                "open the debate by laying out the strongest conservative position based on the data"
            )
        else:
            peer_block = (
                f"Here is the current conversation history: {history} "
                f"Here is the last response from the aggressive analyst: {current_aggressive_response} "
                f"Here is the last response from the neutral analyst: {current_neutral_response}. "
                "If there are no responses from the other viewpoints yet, present your own argument based on the available data."
            )
            engagement_clause = (
                "Engage by questioning their optimism and emphasizing the potential downsides they may have overlooked. "
                "Address each of their counterpoints to showcase why a conservative stance is ultimately the safest path"
            )

        prompt = f"""{position_prefix}As the Conservative Risk Analyst, your primary objective is to protect assets, minimize volatility, and ensure steady, reliable growth. You prioritize stability, security, and risk mitigation, carefully assessing potential losses, economic downturns, and market volatility. When evaluating the trader's decision or plan, critically examine high-risk elements, pointing out where the decision may expose the firm to undue risk and where more cautious alternatives could secure long-term gains. If the user has a position above, frame your critique around protecting *that specific position* — pay attention to whether it's already over-sized or near a stop, and use the vocabulary in the position block. Here is the trader's decision:

{trader_decision}

Your task is to build a convincing case for a low-risk approach adjustment to the trader's decision, drawing from the following data sources:

Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest World Affairs Report: {news_report}
Company Fundamentals Report: {fundamentals_report}
{peer_block}

{engagement_clause}. Output conversationally as if you are speaking without any special formatting."""

        response = llm.invoke(prompt)

        argument = f"Conservative Analyst: {response.content}"

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": risk_debate_state.get("aggressive_history", ""),
            "conservative_history": conservative_history + "\n" + argument,
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "latest_speaker": "Conservative",
            "current_aggressive_response": risk_debate_state.get(
                "current_aggressive_response", ""
            ),
            "current_conservative_response": argument,
            "current_neutral_response": risk_debate_state.get(
                "current_neutral_response", ""
            ),
            "count": risk_debate_state["count"] + 1,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return conservative_node
