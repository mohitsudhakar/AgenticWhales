

def create_aggressive_debator(llm, blind_first_round: bool = False):
    """Build the Aggressive Risk Analyst node.

    ``blind_first_round`` hides peer responses and the prior debate history
    when the risk debate has just opened (count <= 2, i.e. the first three
    turns are independent openings from Aggressive, Conservative, Neutral).
    From round 2 (count >= 3), full peer history is visible for rebuttal.
    """
    def aggressive_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        aggressive_history = risk_debate_state.get("aggressive_history", "")

        current_conservative_response = risk_debate_state.get("current_conservative_response", "")
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
                "case for the aggressive view based solely on the trader's plan and the "
                "underlying research, without anchoring on what conservative or neutral "
                "analysts might say."
            )
            engagement_clause = (
                "open the debate by laying out the strongest aggressive position based on the data"
            )
        else:
            peer_block = (
                f"Here is the current conversation history: {history} "
                f"Here are the last arguments from the conservative analyst: {current_conservative_response} "
                f"Here are the last arguments from the neutral analyst: {current_neutral_response}. "
                "If there are no responses from the other viewpoints yet, present your own argument based on the available data."
            )
            engagement_clause = (
                "Engage actively by addressing any specific concerns raised, refuting the weaknesses in their logic, "
                "and asserting the benefits of risk-taking to outpace market norms. Maintain a focus on debating "
                "and persuading, not just presenting data. Challenge each counterpoint to underscore why a high-risk approach is optimal"
            )

        prompt = f"""{position_prefix}As the Aggressive Risk Analyst, your role is to actively champion high-reward, high-risk opportunities, emphasizing bold strategies and competitive advantages. When evaluating the trader's decision or plan, focus intently on the potential upside, growth potential, and innovative benefits—even when these come with elevated risk. Use the provided market data and sentiment analysis to strengthen your arguments. If the user has a position above, argue for the most aggressive *delta* to that specific position the data supports — using the vocabulary in the position block. Here is the trader's decision:

{trader_decision}

Your task is to create a compelling case for the trader's decision. Incorporate insights from the following sources:

Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest World Affairs Report: {news_report}
Company Fundamentals Report: {fundamentals_report}
{peer_block}

{engagement_clause}. Output conversationally as if you are speaking without any special formatting."""

        response = llm.invoke(prompt)

        argument = f"Aggressive Analyst: {response.content}"

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": aggressive_history + "\n" + argument,
            "conservative_history": risk_debate_state.get("conservative_history", ""),
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "latest_speaker": "Aggressive",
            "current_aggressive_response": argument,
            "current_conservative_response": risk_debate_state.get("current_conservative_response", ""),
            "current_neutral_response": risk_debate_state.get(
                "current_neutral_response", ""
            ),
            "count": risk_debate_state["count"] + 1,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return aggressive_node
