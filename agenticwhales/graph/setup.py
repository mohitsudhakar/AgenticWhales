# AgenticWhales/graph/setup.py

from typing import Any, Dict, Optional
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agenticwhales.agents import *
from agenticwhales.agents.utils.agent_states import AgentState

from .conditional_logic import ConditionalLogic


class GraphSetup:
    """Handles the setup and configuration of the agent graph."""

    def __init__(
        self,
        quick_thinking_llm: Any,
        deep_thinking_llm: Any,
        tool_nodes: Dict[str, ToolNode],
        conditional_logic: ConditionalLogic,
        research_manager_llm: Optional[Any] = None,
        portfolio_manager_llm: Optional[Any] = None,
        debater_llms: Optional[Dict[str, Any]] = None,
        blind_first_round: bool = False,
    ):
        """Initialize with required components.

        ``research_manager_llm`` and ``portfolio_manager_llm`` let the caller
        supply different models for each synthesizer; falls back to
        ``deep_thinking_llm`` when not provided.

        ``debater_llms`` is an optional dict keyed by debater name
        (``"bull"``, ``"bear"``, ``"aggressive"``, ``"conservative"``,
        ``"neutral"``) mapping each agent to its bound LLM. Missing entries
        fall back to ``quick_thinking_llm``.

        Architectural diversity at both synthesizers and across the upstream
        debaters is a design heuristic to reduce correlated failures
        compared with running every agent on one model family — same-family
        agents tend to share priors and converge on a united front. The
        empirical claim is measured by tests/evals/diversity_engine_eval.py.
        """
        self.quick_thinking_llm = quick_thinking_llm
        self.deep_thinking_llm = deep_thinking_llm
        self.tool_nodes = tool_nodes
        self.conditional_logic = conditional_logic
        self.research_manager_llm = research_manager_llm or deep_thinking_llm
        self.portfolio_manager_llm = portfolio_manager_llm or deep_thinking_llm

        debater_llms = debater_llms or {}
        self.bull_llm = debater_llms.get("bull", quick_thinking_llm)
        self.bear_llm = debater_llms.get("bear", quick_thinking_llm)
        self.aggressive_llm = debater_llms.get("aggressive", quick_thinking_llm)
        self.conservative_llm = debater_llms.get("conservative", quick_thinking_llm)
        self.neutral_llm = debater_llms.get("neutral", quick_thinking_llm)

        # When True, round 1 of each debate hides the opponents' arguments
        # and prior debate history — each debater writes its opening
        # independently. From round 2 onward, full history is visible for
        # genuine rebuttal. Preserves the independence condition for
        # crowd-wisdom on the prior.
        self.blind_first_round = blind_first_round

    def setup_graph(
        self, selected_analysts=["market", "quant", "social", "news", "fundamentals"]
    ):
        """Set up and compile the agent workflow graph.

        Args:
            selected_analysts (list): List of analyst types to include. Options are:
                - "market": Market analyst
                - "quant": Quant Analyst (QuantAgent 2025-style 6-dim radar)
                - "social": Social media analyst
                - "news": News analyst
                - "fundamentals": Fundamentals analyst
        """
        if len(selected_analysts) == 0:
            raise ValueError("Trading Agents Graph Setup Error: no analysts selected!")

        # Create analyst nodes
        analyst_nodes = {}
        delete_nodes = {}
        tool_nodes = {}

        if "market" in selected_analysts:
            analyst_nodes["market"] = create_market_analyst(
                self.quick_thinking_llm
            )
            delete_nodes["market"] = create_msg_delete()
            tool_nodes["market"] = self.tool_nodes["market"]

        if "social" in selected_analysts:
            analyst_nodes["social"] = create_social_media_analyst(
                self.quick_thinking_llm
            )
            delete_nodes["social"] = create_msg_delete()
            tool_nodes["social"] = self.tool_nodes["social"]

        if "news" in selected_analysts:
            analyst_nodes["news"] = create_news_analyst(
                self.quick_thinking_llm
            )
            delete_nodes["news"] = create_msg_delete()
            tool_nodes["news"] = self.tool_nodes["news"]

        if "fundamentals" in selected_analysts:
            analyst_nodes["fundamentals"] = create_fundamentals_analyst(
                self.quick_thinking_llm
            )
            delete_nodes["fundamentals"] = create_msg_delete()
            tool_nodes["fundamentals"] = self.tool_nodes["fundamentals"]

        if "quant" in selected_analysts:
            # Quant Analyst produces a structured 6-dim radar (QuantAgent
            # 2025-style). Uses the same price + indicator tools as the
            # market analyst; final output is a typed QuantRadar.
            analyst_nodes["quant"] = create_quant_analyst(
                self.quick_thinking_llm
            )
            delete_nodes["quant"] = create_msg_delete()
            tool_nodes["quant"] = self.tool_nodes["quant"]

        # Create researcher and manager nodes.
        # Bull/Bear may be on different model families when debater
        # diversification is on (avoids the all-same-family upstream
        # united-front pattern; see default_config.py for the rationale).
        bull_researcher_node = create_bull_researcher(
            self.bull_llm, blind_first_round=self.blind_first_round
        )
        bear_researcher_node = create_bear_researcher(
            self.bear_llm, blind_first_round=self.blind_first_round
        )
        research_manager_node = create_research_manager(self.research_manager_llm)
        trader_node = create_trader(self.quick_thinking_llm)

        # Create risk analysis nodes. Each debater gets its own bound LLM
        # so the three perspectives are sourced from different model
        # families; the Portfolio Manager synthesizes from a fourth
        # (architecturally distinct) family.
        aggressive_analyst = create_aggressive_debator(
            self.aggressive_llm, blind_first_round=self.blind_first_round
        )
        neutral_analyst = create_neutral_debator(
            self.neutral_llm, blind_first_round=self.blind_first_round
        )
        conservative_analyst = create_conservative_debator(
            self.conservative_llm, blind_first_round=self.blind_first_round
        )
        portfolio_manager_node = create_portfolio_manager(self.portfolio_manager_llm)

        # Create workflow
        workflow = StateGraph(AgentState)

        # Add analyst nodes to the graph
        for analyst_type, node in analyst_nodes.items():
            workflow.add_node(f"{analyst_type.capitalize()} Analyst", node)
            workflow.add_node(
                f"Msg Clear {analyst_type.capitalize()}", delete_nodes[analyst_type]
            )
            workflow.add_node(f"tools_{analyst_type}", tool_nodes[analyst_type])

        # Add other nodes
        workflow.add_node("Bull Researcher", bull_researcher_node)
        workflow.add_node("Bear Researcher", bear_researcher_node)
        workflow.add_node("Research Manager", research_manager_node)
        workflow.add_node("Trader", trader_node)
        workflow.add_node("Aggressive Analyst", aggressive_analyst)
        workflow.add_node("Neutral Analyst", neutral_analyst)
        workflow.add_node("Conservative Analyst", conservative_analyst)
        workflow.add_node("Portfolio Manager", portfolio_manager_node)

        # Define edges
        # Start with the first analyst
        first_analyst = selected_analysts[0]
        workflow.add_edge(START, f"{first_analyst.capitalize()} Analyst")

        # Connect analysts in sequence
        for i, analyst_type in enumerate(selected_analysts):
            current_analyst = f"{analyst_type.capitalize()} Analyst"
            current_tools = f"tools_{analyst_type}"
            current_clear = f"Msg Clear {analyst_type.capitalize()}"

            # Add conditional edges for current analyst
            workflow.add_conditional_edges(
                current_analyst,
                getattr(self.conditional_logic, f"should_continue_{analyst_type}"),
                [current_tools, current_clear],
            )
            workflow.add_edge(current_tools, current_analyst)

            # Connect to next analyst or to Bull Researcher if this is the last analyst
            if i < len(selected_analysts) - 1:
                next_analyst = f"{selected_analysts[i+1].capitalize()} Analyst"
                workflow.add_edge(current_clear, next_analyst)
            else:
                workflow.add_edge(current_clear, "Bull Researcher")

        # Add remaining edges
        workflow.add_conditional_edges(
            "Bull Researcher",
            self.conditional_logic.should_continue_debate,
            {
                "Bear Researcher": "Bear Researcher",
                "Research Manager": "Research Manager",
            },
        )
        workflow.add_conditional_edges(
            "Bear Researcher",
            self.conditional_logic.should_continue_debate,
            {
                "Bull Researcher": "Bull Researcher",
                "Research Manager": "Research Manager",
            },
        )
        workflow.add_edge("Research Manager", "Trader")
        workflow.add_edge("Trader", "Aggressive Analyst")
        workflow.add_conditional_edges(
            "Aggressive Analyst",
            self.conditional_logic.should_continue_risk_analysis,
            {
                "Conservative Analyst": "Conservative Analyst",
                "Portfolio Manager": "Portfolio Manager",
            },
        )
        workflow.add_conditional_edges(
            "Conservative Analyst",
            self.conditional_logic.should_continue_risk_analysis,
            {
                "Neutral Analyst": "Neutral Analyst",
                "Portfolio Manager": "Portfolio Manager",
            },
        )
        workflow.add_conditional_edges(
            "Neutral Analyst",
            self.conditional_logic.should_continue_risk_analysis,
            {
                "Aggressive Analyst": "Aggressive Analyst",
                "Portfolio Manager": "Portfolio Manager",
            },
        )

        workflow.add_edge("Portfolio Manager", END)

        return workflow
