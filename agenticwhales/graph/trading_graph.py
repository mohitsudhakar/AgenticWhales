# AgenticWhales/graph/trading_graph.py

import logging
import os
from pathlib import Path
import json
from datetime import datetime, timedelta
from typing import Dict, Any, Tuple, List, Optional

import yfinance as yf

logger = logging.getLogger(__name__)

from langgraph.prebuilt import ToolNode

from agenticwhales.llm_clients import create_llm_client
from agenticwhales.llm_clients.model_catalog import MODEL_OPTIONS

from agenticwhales.agents import *
from agenticwhales.default_config import DEFAULT_CONFIG
from agenticwhales.agents.utils.memory import TradingMemoryLog
from agenticwhales.agents.utils.agent_states import (
    AgentState,
    InvestDebateState,
    RiskDebateState,
)
from agenticwhales.dataflows.config import set_config

# Import the new abstract tool methods from agent_utils
from agenticwhales.agents.utils.agent_utils import (
    get_stock_data,
    get_indicators,
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement,
    get_news,
    get_insider_transactions,
    get_global_news
)

from .checkpointer import checkpoint_step, clear_checkpoint, get_checkpointer, thread_id
from .conditional_logic import ConditionalLogic
from .setup import GraphSetup
from .propagation import Propagator
from .reflection import Reflector
from .signal_processing import SignalProcessor


class AgenticWhalesGraph:
    """Main class that orchestrates the trading agents framework."""

    def __init__(
        self,
        selected_analysts=["market", "quant", "social", "news", "fundamentals"],
        debug=False,
        config: Dict[str, Any] = None,
        callbacks: Optional[List] = None,
    ):
        """Initialize the trading agents graph and components.

        Args:
            selected_analysts: List of analyst types to include
            debug: Whether to run in debug mode
            config: Configuration dictionary. If None, uses default config
            callbacks: Optional list of callback handlers (e.g., for tracking LLM/tool stats)
        """
        self.debug = debug
        self.config = config or DEFAULT_CONFIG
        self.callbacks = callbacks or []

        # Update the interface's config
        set_config(self.config)

        # Create necessary directories
        os.makedirs(self.config["data_cache_dir"], exist_ok=True)
        os.makedirs(self.config["results_dir"], exist_ok=True)

        # Initialize LLMs with provider-specific thinking configuration
        llm_kwargs = self._get_provider_kwargs()

        # Add callbacks to kwargs if provided (passed to LLM constructor)
        if self.callbacks:
            llm_kwargs["callbacks"] = self.callbacks

        deep_client = create_llm_client(
            provider=self.config["llm_provider"],
            model=self.config["deep_think_llm"],
            base_url=self.config.get("backend_url"),
            **llm_kwargs,
        )
        quick_client = create_llm_client(
            provider=self.config["llm_provider"],
            model=self.config["quick_think_llm"],
            base_url=self.config.get("backend_url"),
            **llm_kwargs,
        )

        self.deep_thinking_llm = deep_client.get_llm()
        self.quick_thinking_llm = quick_client.get_llm()
        # Per-role record of which provider each diversified slot ended up on,
        # plus a per-role ``degraded`` flag that's True when the Heterogeneity
        # Mandate could not be satisfied for that slot (fell back to upstream,
        # or — for debaters — collided with an adjacent slot). P1.3 surfaces
        # this to the web UI; P1.4 tests assert on it. The upstream provider
        # used for the analyst stack and Trader is recorded for completeness.
        self.diversification_status: Dict[str, Dict[str, Any]] = {
            "upstream": {
                "provider": self.config.get("llm_provider", "").lower(),
                "degraded": False,
            },
        }
        # Heterogeneity Mandate (Shehata & Li 2026, Cor. 1): both synthesizers
        # — Research Manager and Portfolio Manager — should be architecturally
        # distinct from upstream agents AND from each other (the Portfolio
        # Manager is downstream of the Research Manager's ``investment_plan``,
        # so they form a synthesizer chain; sharing a model family there
        # reproduces the kinship-locked pattern the Mandate exists to break).
        # P1.1: thread an ``exclude`` set so the second call cannot pick the
        # same provider the first call already took.
        self.research_manager_llm = self._build_diversified_synthesizer_llm(
            "research_manager", exclude=set()
        )
        rm_provider = self.diversification_status["research_manager"]["provider"]
        self.portfolio_manager_llm = self._build_diversified_synthesizer_llm(
            "portfolio_manager", exclude={rm_provider}
        )
        # Debater diversification: spread Bull/Bear and the three risk
        # debaters across non-synthesizer providers to break the kinship-
        # locked upstream pattern (Peer Pressure configurations in Table 1).
        self.debater_llms = self._build_debater_llms()

        self.memory_log = TradingMemoryLog(self.config)

        # Create tool nodes
        self.tool_nodes = self._create_tool_nodes()

        # Initialize components
        self.conditional_logic = ConditionalLogic(
            max_debate_rounds=self.config["max_debate_rounds"],
            max_risk_discuss_rounds=self.config["max_risk_discuss_rounds"],
        )
        self.graph_setup = GraphSetup(
            self.quick_thinking_llm,
            self.deep_thinking_llm,
            self.tool_nodes,
            self.conditional_logic,
            research_manager_llm=self.research_manager_llm,
            portfolio_manager_llm=self.portfolio_manager_llm,
            debater_llms=self.debater_llms,
            blind_first_round=self.config.get("blind_first_round", False),
        )

        self.propagator = Propagator()
        self.reflector = Reflector(self.quick_thinking_llm)
        self.signal_processor = SignalProcessor(self.quick_thinking_llm)

        # State tracking
        self.curr_state = None
        self.ticker = None
        self.log_states_dict = {}  # date to full state dict

        # Set up the graph: keep the workflow for recompilation with a checkpointer.
        self.workflow = self.graph_setup.setup_graph(selected_analysts)
        self.graph = self.workflow.compile()
        self._checkpointer_ctx = None

    def _get_provider_kwargs(self) -> Dict[str, Any]:
        """Get provider-specific kwargs for LLM client creation."""
        kwargs = {}
        provider = self.config.get("llm_provider", "").lower()

        if provider == "google":
            thinking_level = self.config.get("google_thinking_level")
            if thinking_level:
                kwargs["thinking_level"] = thinking_level

        elif provider == "openai":
            reasoning_effort = self.config.get("openai_reasoning_effort")
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort

        elif provider == "anthropic":
            effort = self.config.get("anthropic_effort")
            if effort:
                kwargs["effort"] = effort

        return kwargs

    # API-key env var for each provider that can serve as a diversified
    # synthesizer or debater. Used to skip preference-list candidates whose
    # credentials are not configured.
    _PROVIDER_ENV_KEY = {
        "deepseek": "DEEPSEEK_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "google": "GOOGLE_API_KEY",
        "openai": "OPENAI_API_KEY",
        "xai": "XAI_API_KEY",
    }

    def _provider_has_credentials(self, provider: str) -> bool:
        """True when the provider has API credentials configured."""
        env_key = self._PROVIDER_ENV_KEY.get(provider.lower())
        if env_key is None:
            return True  # unknown providers: trust the caller
        return bool(os.environ.get(env_key))

    def _create_provider_llm(self, provider: str, mode: str = "deep") -> Optional[Any]:
        """Create an LLM for a provider using the catalog's top model in mode.

        ``mode`` is "deep" or "quick". Returns None if the provider has no
        catalog entry (e.g. an unusable custom provider) or if credentials
        are missing.
        """
        provider = provider.lower()
        if not self._provider_has_credentials(provider):
            return None
        options = MODEL_OPTIONS.get(provider, {}).get(mode) or []
        if not options:
            return None
        model = options[0][1]

        extra: Dict[str, Any] = {}
        if self.callbacks:
            extra["callbacks"] = self.callbacks
        return create_llm_client(
            provider=provider, model=model, base_url=None, **extra,
        ).get_llm()

    def _build_diversified_synthesizer_llm(
        self,
        role: str,
        exclude: Optional[set] = None,
    ) -> Any:
        """Return the LLM the named synthesizer (research or portfolio mgr) should use.

        Both synthesizers are subject to the Synthesizer Gating Theorem
        (Shehata & Li 2026, Thm 1) — terminal swarm integrity is gated by
        the synthesizer's receptive logic (Tribalism Coefficient τ), not by
        upstream agent quality. A synthesizer drawn from the same model
        family as upstream agents inherits their correlated biases and the
        Attention Latch (Λ → 2) collapses the swarm toward μ = 1.0.

        We walk ``synthesizer_provider_preference`` and pick the first
        provider that (a) differs from the upstream provider, (b) is not in
        ``exclude`` (used to keep the two synthesizers in the chain on
        different providers — P1.1), and (c) has credentials configured.
        Falls back to the default deep-think LLM when no candidate is
        usable, so missing optional credentials never break a run; in that
        case the slot is recorded as ``degraded`` so the UI can surface it.
        """
        exclude = {p.lower() for p in (exclude or set())}
        upstream = self.config.get("llm_provider", "").lower()

        if not self.config.get("diversify_synthesizers"):
            self.diversification_status[role] = {
                "provider": upstream,
                "degraded": True,
                "reason": "diversify_synthesizers disabled",
            }
            return self.deep_thinking_llm

        preference = self.config.get("synthesizer_provider_preference") or []

        for candidate in preference:
            candidate = candidate.lower()
            if candidate == upstream:
                continue  # same family as upstream — no diversity gain
            if candidate in exclude:
                continue  # already taken by another synthesizer (P1.1)
            llm = self._create_provider_llm(candidate, mode="deep")
            if llm is None:
                continue
            logger.info(
                "%s diversification active: synthesizer will use %s (upstream=%s, excluded=%s).",
                role, candidate, upstream, sorted(exclude) or "none",
            )
            self.diversification_status[role] = {
                "provider": candidate,
                "degraded": False,
            }
            return llm

        logger.warning(
            "%s diversification DEGRADED: no usable candidate in "
            "synthesizer_provider_preference (upstream=%s, excluded=%s); "
            "falling back to upstream deep-think LLM. The Heterogeneity "
            "Mandate is not satisfied for this slot.",
            role, upstream, sorted(exclude) or "none",
        )
        self.diversification_status[role] = {
            "provider": upstream,
            "degraded": True,
            "reason": "no usable preference provider; fell back to upstream",
        }
        return self.deep_thinking_llm

    def _build_debater_llms(self) -> Dict[str, Any]:
        """Return per-debater LLMs keyed by agent name.

        Returns a dict with keys: "bull", "bear", "aggressive", "conservative",
        "neutral". Each value is an LLM instance to bind to that agent's node.
        When ``diversify_debaters`` is off or no preference providers are
        usable, all five fall back to ``quick_thinking_llm`` (the previous
        behaviour).

        Assignment policy (Shehata & Li 2026, Table 1): assign providers
        from ``debater_provider_preference`` to break the "Peer Pressure"
        united-front upstream pattern. Bull and Bear get providers[0] and
        providers[1 % len]; the three risk debaters cycle through with
        modular indexing so no two adjacent debaters in the round-robin
        share a provider. The risk round-robin is Agg → Con → Neu → Agg,
        so we need at least 3 usable providers to guarantee that — with
        only 2, the third slot wraps around to providers[0] and collides
        with the first. P1.2: when ``len(usable) < 3``, log a WARN and
        mark each colliding debater slot as ``degraded`` so the UI can
        surface the issue. The default ``debater_provider_preference``
        now includes a third entry (xai) so this path is the exception
        rather than the rule.
        """
        upstream_provider = self.config.get("llm_provider", "").lower()
        default = self.quick_thinking_llm
        debaters = ["bull", "bear", "aggressive", "conservative", "neutral"]
        result: Dict[str, Any] = {name: default for name in debaters}

        if not self.config.get("diversify_debaters"):
            for name in debaters:
                self.diversification_status[name] = {
                    "provider": upstream_provider,
                    "degraded": True,
                    "reason": "diversify_debaters disabled",
                }
            return result

        preference = self.config.get("debater_provider_preference") or []
        usable: list = []
        for candidate in preference:
            candidate = candidate.lower()
            if not self._provider_has_credentials(candidate):
                continue
            llm = self._create_provider_llm(candidate, mode="quick")
            if llm is None:
                continue
            usable.append((candidate, llm))

        if not usable:
            logger.warning(
                "Debater diversification DEGRADED: no usable providers in "
                "debater_provider_preference; all five debaters fall back to "
                "the upstream quick LLM (%s).",
                upstream_provider,
            )
            for name in debaters:
                self.diversification_status[name] = {
                    "provider": upstream_provider,
                    "degraded": True,
                    "reason": "no usable preference provider; fell back to upstream",
                }
            return result

        if len(usable) < 3:
            logger.warning(
                "Debater diversification PARTIALLY DEGRADED: only %d usable "
                "debater providers (%s); the three-way risk debate (Agg → Con "
                "→ Neu → Agg) requires 3 to guarantee no adjacent collision. "
                "Add a third entry to debater_provider_preference, or set the "
                "missing API keys, to fully satisfy the Heterogeneity Mandate.",
                len(usable),
                [p for p, _ in usable],
            )

        # Cycle assignment so adjacent slots never share a provider when
        # ``len(usable) >= 3``. Bull/Bear are pairwise; Agg/Con/Neu form
        # the three-way round-robin.
        bull_idx, bear_idx = 0, 1 % len(usable)
        agg_idx, con_idx, neu_idx = 0 % len(usable), 1 % len(usable), 2 % len(usable)
        result["bull"] = usable[bull_idx][1]
        result["bear"] = usable[bear_idx][1]
        result["aggressive"] = usable[agg_idx][1]
        result["conservative"] = usable[con_idx][1]
        result["neutral"] = usable[neu_idx][1]

        # Record per-debater provider + degraded flag (collision = degraded).
        per_slot_idx = {
            "bull": bull_idx,
            "bear": bear_idx,
            "aggressive": agg_idx,
            "conservative": con_idx,
            "neutral": neu_idx,
        }
        provider_for_slot = {
            name: usable[idx][0] for name, idx in per_slot_idx.items()
        }
        # Adjacency: investment debate is Bull↔Bear; risk debate is
        # Agg→Con, Con→Neu, Neu→Agg.
        adjacent_pairs = [
            ("bull", "bear"),
            ("aggressive", "conservative"),
            ("conservative", "neutral"),
            ("neutral", "aggressive"),
        ]
        degraded_slots = set()
        for a, b in adjacent_pairs:
            if provider_for_slot[a] == provider_for_slot[b]:
                degraded_slots.add(a)
                degraded_slots.add(b)
        for name in debaters:
            self.diversification_status[name] = {
                "provider": provider_for_slot[name],
                "degraded": name in degraded_slots,
            }
            if name in degraded_slots:
                self.diversification_status[name]["reason"] = (
                    "collides with an adjacent debater (insufficient providers)"
                )

        logger.info(
            "Debater diversification: assignments=%s, degraded=%s",
            provider_for_slot,
            sorted(degraded_slots) or "none",
        )
        return result

    def get_diversification_status(self) -> Dict[str, Any]:
        """Return a snapshot of per-role provider assignments + degradation flags.

        Public read used by the web layer (P1.3) to display a banner when
        the Heterogeneity Mandate could not be fully satisfied for the
        current session, and by tests (P1.4) to assert on the assignments.
        """
        roles = dict(self.diversification_status)
        degraded = any(v.get("degraded") for k, v in roles.items() if k != "upstream")
        return {
            "degraded": degraded,
            "assignments": roles,
        }

    def _create_tool_nodes(self) -> Dict[str, ToolNode]:
        """Create tool nodes for different data sources using abstract methods."""
        return {
            "market": ToolNode(
                [
                    # Core stock data tools
                    get_stock_data,
                    # Technical indicators
                    get_indicators,
                ]
            ),
            "social": ToolNode(
                [
                    # News tools for social media analysis
                    get_news,
                ]
            ),
            "news": ToolNode(
                [
                    # News and insider information
                    get_news,
                    get_global_news,
                    get_insider_transactions,
                ]
            ),
            "fundamentals": ToolNode(
                [
                    # Fundamental analysis tools
                    get_fundamentals,
                    get_balance_sheet,
                    get_cashflow,
                    get_income_statement,
                ]
            ),
            "quant": ToolNode(
                [
                    # Quant Analyst shares price + indicator tools with the
                    # market analyst — its output shape (6-dim radar) is the
                    # differentiator, not the inputs.
                    get_stock_data,
                    get_indicators,
                ]
            ),
        }

    def _fetch_returns(
        self, ticker: str, trade_date: str, holding_days: int = 5
    ) -> Tuple[Optional[float], Optional[float], Optional[int]]:
        """Fetch raw and alpha return for ticker over holding_days from trade_date.

        Returns (raw_return, alpha_return, actual_holding_days) or
        (None, None, None) if price data is unavailable (too recent, delisted,
        or network error).
        """
        try:
            start = datetime.strptime(trade_date, "%Y-%m-%d")
            end = start + timedelta(days=holding_days + 7)  # buffer for weekends/holidays
            end_str = end.strftime("%Y-%m-%d")

            stock = yf.Ticker(ticker).history(start=trade_date, end=end_str)
            spy = yf.Ticker("SPY").history(start=trade_date, end=end_str)

            if len(stock) < 2 or len(spy) < 2:
                return None, None, None

            actual_days = min(holding_days, len(stock) - 1, len(spy) - 1)
            raw = float(
                (stock["Close"].iloc[actual_days] - stock["Close"].iloc[0])
                / stock["Close"].iloc[0]
            )
            spy_ret = float(
                (spy["Close"].iloc[actual_days] - spy["Close"].iloc[0])
                / spy["Close"].iloc[0]
            )
            alpha = raw - spy_ret
            return raw, alpha, actual_days
        except Exception as e:
            logger.warning(
                "Could not resolve outcome for %s on %s (will retry next run): %s",
                ticker, trade_date, e,
            )
            return None, None, None

    def _resolve_pending_entries(self, ticker: str) -> None:
        """Resolve pending log entries for ticker at the start of a new run.

        Fetches returns for each same-ticker pending entry, generates reflections,
        then writes all updates in a single atomic batch write to avoid redundant I/O.
        Skips entries whose price data is not yet available (too recent or delisted).

        Trade-off: only same-ticker entries are resolved per run.  Entries for
        other tickers accumulate until that ticker is run again.
        """
        pending = [e for e in self.memory_log.get_pending_entries() if e["ticker"] == ticker]
        if not pending:
            return

        updates = []
        for entry in pending:
            raw, alpha, days = self._fetch_returns(ticker, entry["date"])
            if raw is None:
                continue  # price not available yet — try again next run
            reflection = self.reflector.reflect_on_final_decision(
                final_decision=entry.get("decision", ""),
                raw_return=raw,
                alpha_return=alpha,
            )
            updates.append({
                "ticker": ticker,
                "trade_date": entry["date"],
                "raw_return": raw,
                "alpha_return": alpha,
                "holding_days": days,
                "reflection": reflection,
            })

        if updates:
            self.memory_log.batch_update_with_outcomes(updates)

    def propagate(self, company_name, trade_date):
        """Run the trading agents graph for a company on a specific date.

        When ``checkpoint_enabled`` is set in config, the graph is recompiled
        with a per-ticker SqliteSaver so a crashed run can resume from the last
        successful node on a subsequent invocation with the same ticker+date.
        """
        self.ticker = company_name

        # Resolve any pending memory-log entries for this ticker before the pipeline runs.
        self._resolve_pending_entries(company_name)

        # Recompile with a checkpointer if the user opted in.
        if self.config.get("checkpoint_enabled"):
            self._checkpointer_ctx = get_checkpointer(
                self.config["data_cache_dir"], company_name
            )
            saver = self._checkpointer_ctx.__enter__()
            self.graph = self.workflow.compile(checkpointer=saver)

            step = checkpoint_step(
                self.config["data_cache_dir"], company_name, str(trade_date)
            )
            if step is not None:
                logger.info(
                    "Resuming from step %d for %s on %s", step, company_name, trade_date
                )
            else:
                logger.info("Starting fresh for %s on %s", company_name, trade_date)

        try:
            return self._run_graph(company_name, trade_date)
        finally:
            if self._checkpointer_ctx is not None:
                self._checkpointer_ctx.__exit__(None, None, None)
                self._checkpointer_ctx = None
                self.graph = self.workflow.compile()

    def _run_graph(self, company_name, trade_date):
        """Execute the graph and write the resulting state to disk and memory log."""
        # Initialize state — inject memory log context, position, and price anchor.
        from agenticwhales import portfolio as _portfolio
        from agenticwhales.market_snapshot import fetch_snapshot_block as _fetch_snap

        # Layered scored retrieval when the per-layer budget is configured,
        # else the legacy count-based context. The scored path returns
        # top-K per layer (FinMem optimum K=5) using recency + relevance +
        # importance, and bumps access counters for retrieved entries.
        top_k = self.config.get("memory_top_k_per_layer")
        if top_k:
            past_context = self.memory_log.get_scored_context(
                company_name, top_k_per_layer=top_k,
            )
        else:
            past_context = self.memory_log.get_past_context(company_name)
        recent_performance = self.memory_log.get_recent_performance_block(company_name)
        current_position = _portfolio.format_for_prompt(company_name)
        market_snapshot = _fetch_snap(company_name, trade_date)
        init_agent_state = self.propagator.create_initial_state(
            company_name,
            trade_date,
            past_context=past_context,
            current_position=current_position,
            market_snapshot=market_snapshot,
            recent_performance=recent_performance,
        )
        args = self.propagator.get_graph_args()

        # Inject thread_id so same ticker+date resumes, different date starts fresh.
        if self.config.get("checkpoint_enabled"):
            tid = thread_id(company_name, str(trade_date))
            args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = tid

        if self.debug:
            trace = []
            for chunk in self.graph.stream(init_agent_state, **args):
                if len(chunk["messages"]) == 0:
                    pass
                else:
                    chunk["messages"][-1].pretty_print()
                    trace.append(chunk)
            final_state = trace[-1]
        else:
            final_state = self.graph.invoke(init_agent_state, **args)

        # Store current state for reflection.
        self.curr_state = final_state

        # Log state to disk.
        self._log_state(trade_date, final_state)

        # Store decision for deferred reflection on the next same-ticker run.
        self.memory_log.store_decision(
            ticker=company_name,
            trade_date=trade_date,
            final_trade_decision=final_state["final_trade_decision"],
        )

        # Periodic extended reflection: every N trading days, synthesize
        # recent immediate reflections into a deep-layer lesson (Yu et al.
        # 2023 §3.2.1). Cheap when the window is empty — one LLM call only
        # when actually triggered.
        self._maybe_run_extended_reflection(trade_date)

        # Clear checkpoint on successful completion to avoid stale state.
        if self.config.get("checkpoint_enabled"):
            clear_checkpoint(
                self.config["data_cache_dir"], company_name, str(trade_date)
            )

        return final_state, self.process_signal(final_state["final_trade_decision"])

    def _maybe_run_extended_reflection(self, trade_date) -> None:
        """Run an M-day retrospective when enough days have elapsed.

        Cadence is controlled by ``extended_reflection_interval_days``
        (None disables the pass). The retrospective window is set by
        ``extended_reflection_window_days``.
        """
        interval = self.config.get("extended_reflection_interval_days")
        if not interval:
            return

        since = self.memory_log.days_since_last_extended_reflection()
        # Either never run before, or enough time has passed.
        if since is not None and since < interval:
            return

        window = self.config.get("extended_reflection_window_days", 30)
        all_entries = [
            e for e in self.memory_log.load_entries()
            if not e.get("pending") and e.get("ticker") != "_EXTENDED_"
        ]
        if not all_entries:
            return

        try:
            cutoff = datetime.strptime(str(trade_date), "%Y-%m-%d") - timedelta(days=window)
            recent = [
                e for e in all_entries
                if datetime.strptime(e["date"], "%Y-%m-%d") >= cutoff
            ]
        except ValueError:
            recent = all_entries[-10:]  # safe fallback

        if not recent:
            return

        try:
            content = self.reflector.extended_reflection(recent)
        except Exception as e:
            logger.warning("Extended reflection failed (%s); skipping this cycle.", e)
            return

        if content and content.strip():
            self.memory_log.store_extended_reflection(str(trade_date), content.strip())
            logger.info(
                "Stored extended reflection covering %d entries over the last %d days.",
                len(recent), window,
            )

    def _log_state(self, trade_date, final_state):
        """Log the final state to a JSON file."""
        self.log_states_dict[str(trade_date)] = {
            "company_of_interest": final_state["company_of_interest"],
            "trade_date": final_state["trade_date"],
            "market_report": final_state["market_report"],
            "sentiment_report": final_state["sentiment_report"],
            "news_report": final_state["news_report"],
            "fundamentals_report": final_state["fundamentals_report"],
            "investment_debate_state": {
                "bull_history": final_state["investment_debate_state"]["bull_history"],
                "bear_history": final_state["investment_debate_state"]["bear_history"],
                "history": final_state["investment_debate_state"]["history"],
                "current_response": final_state["investment_debate_state"][
                    "current_response"
                ],
                "judge_decision": final_state["investment_debate_state"][
                    "judge_decision"
                ],
            },
            "trader_investment_decision": final_state["trader_investment_plan"],
            "risk_debate_state": {
                "aggressive_history": final_state["risk_debate_state"]["aggressive_history"],
                "conservative_history": final_state["risk_debate_state"]["conservative_history"],
                "neutral_history": final_state["risk_debate_state"]["neutral_history"],
                "history": final_state["risk_debate_state"]["history"],
                "judge_decision": final_state["risk_debate_state"]["judge_decision"],
            },
            "investment_plan": final_state["investment_plan"],
            "final_trade_decision": final_state["final_trade_decision"],
        }

        # Save to file
        directory = Path(self.config["results_dir"]) / self.ticker / "TradingAgentsStrategy_logs"
        directory.mkdir(parents=True, exist_ok=True)

        log_path = directory / f"full_states_log_{trade_date}.json"
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(self.log_states_dict[str(trade_date)], f, indent=4)

    def process_signal(self, full_signal):
        """Process a signal to extract the core decision."""
        return self.signal_processor.process_signal(full_signal)
