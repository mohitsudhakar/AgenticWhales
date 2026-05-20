

def create_bull_researcher(llm, blind_first_round: bool = False):
    """Build the Bull Analyst node.

    ``blind_first_round`` enforces independence on the opening turn: when
    True and the debate has just started (count <= 1, i.e. this is Bull's
    opening or Bear's reply right after Bull opens), the prompt hides the
    prior debate history and the opponent's last argument. Bull writes its
    opening from research alone, so the first round captures two
    independent priors rather than two anchored-on-each-other priors.
    From round 2, full history is visible for genuine rebuttal.
    """
    def bull_node(state) -> dict:
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")
        bull_history = investment_debate_state.get("bull_history", "")

        current_response = investment_debate_state.get("current_response", "")
        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]
        position_block = (state.get("current_position") or "").strip()
        snapshot_block = (state.get("market_snapshot") or "").strip()
        prefix_parts = [b for b in (snapshot_block, position_block) if b]
        position_prefix = "\n\n".join(prefix_parts) + "\n\n" if prefix_parts else ""

        count = investment_debate_state.get("count", 0)
        is_blind = blind_first_round and count <= 1
        if is_blind:
            history_block = ""
            opponent_block = (
                "This is your independent opening — write your bull case based on the "
                "research alone, without anchoring on any prior bear argument."
            )
            engagement_clause = "open the debate by laying out the strongest possible bull case"
        else:
            history_block = f"Conversation history of the debate: {history}\n"
            opponent_block = f"Last bear argument: {current_response}"
            engagement_clause = "deliver a compelling bull argument, refute the bear's concerns, and engage in a dynamic debate that demonstrates the strengths of the bull position"

        prompt = f"""{position_prefix}You are a Bull Analyst advocating for investing in the stock. Your task is to build a strong, evidence-based case emphasizing growth potential, competitive advantages, and positive market indicators. Leverage the provided research and data to address concerns and counter bearish arguments effectively. If the user already has a position above, frame your bull case as what it implies for that specific position (using the vocabulary listed in the position block).

Key points to focus on:
- Growth Potential: Highlight the company's market opportunities, revenue projections, and scalability.
- Competitive Advantages: Emphasize factors like unique products, strong branding, or dominant market positioning.
- Positive Indicators: Use financial health, industry trends, and recent positive news as evidence.
- Bear Counterpoints: Critically analyze the bear argument with specific data and sound reasoning, addressing concerns thoroughly and showing why the bull perspective holds stronger merit.
- Engagement: Present your argument in a conversational style, engaging directly with the bear analyst's points and debating effectively rather than just listing data.

Resources available:
Market research report: {market_research_report}
Social media sentiment report: {sentiment_report}
Latest world affairs news: {news_report}
Company fundamentals report: {fundamentals_report}
{history_block}{opponent_block}
Use this information to {engagement_clause}.
"""

        response = llm.invoke(prompt)

        argument = f"Bull Analyst: {response.content}"

        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bull_history": bull_history + "\n" + argument,
            "bear_history": investment_debate_state.get("bear_history", ""),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
        }

        return {"investment_debate_state": new_investment_debate_state}

    return bull_node
