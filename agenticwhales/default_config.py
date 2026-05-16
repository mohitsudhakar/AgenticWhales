import os

# On-disk data path is intentionally kept as ~/.tradingagents/ to preserve
# existing user data after the rename to AgenticWhales. New installations
# can opt into ~/.agenticwhales/ by setting AGENTICWHALES_DATA_DIR.
_AGENTICWHALES_HOME = os.environ.get(
    "AGENTICWHALES_DATA_DIR",
    os.path.join(os.path.expanduser("~"), ".tradingagents"),
)


def _envvar(new_name: str, legacy_name: str, default: str) -> str:
    """Read AGENTICWHALES_* first, fall back to TRADINGAGENTS_*, then default."""
    return os.getenv(new_name) or os.getenv(legacy_name) or default


DEFAULT_CONFIG = {
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": _envvar("AGENTICWHALES_RESULTS_DIR", "TRADINGAGENTS_RESULTS_DIR", os.path.join(_AGENTICWHALES_HOME, "logs")),
    "data_cache_dir": _envvar("AGENTICWHALES_CACHE_DIR", "TRADINGAGENTS_CACHE_DIR", os.path.join(_AGENTICWHALES_HOME, "cache")),
    "memory_log_path": _envvar("AGENTICWHALES_MEMORY_LOG_PATH", "TRADINGAGENTS_MEMORY_LOG_PATH", os.path.join(_AGENTICWHALES_HOME, "memory", "trading_memory.md")),
    # Optional cap on the number of resolved memory log entries. When set,
    # the oldest resolved entries are pruned once this limit is exceeded.
    # Pending entries are never pruned. None disables rotation entirely.
    "memory_log_max_entries": None,
    # LLM settings
    "llm_provider": "openai",
    "deep_think_llm": "gpt-5.4",
    "quick_think_llm": "gpt-5.4-mini",
    # When None, each provider's client falls back to its own default endpoint
    # (api.openai.com for OpenAI, generativelanguage.googleapis.com for Gemini, ...).
    # The CLI overrides this per provider when the user picks one. Keeping a
    # provider-specific URL here would leak (e.g. OpenAI's /v1 was previously
    # being forwarded to Gemini, producing malformed request URLs).
    "backend_url": None,
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    "anthropic_effort": None,           # "high", "medium", "low"
    # When True, BOTH synthesizers (Research Manager and Portfolio Manager)
    # use an LLM drawn from `synthesizer_provider_preference` rather than the
    # upstream `deep_think_llm` provider. Shehata & Li (2026) prove the
    # Synthesizer Gating Theorem: terminal swarm integrity is a gated
    # function of the synthesizer's receptive logic (τ, the Tribalism
    # Coefficient). Sharing a model family with upstream agents inherits
    # their correlated biases (Λ→2, error→1.0). The Heterogeneity Mandate
    # (Resilience Inequality, Corollary 1) is the technical requirement
    # that the synthesizer node be architecturally distinct.
    "diversify_synthesizers": True,
    # Ordered preference for synthesizer providers (used by both Research
    # Manager and Portfolio Manager). We walk this list and pick the first
    # provider that (a) has its API key set and (b) is not the upstream
    # provider (matching upstream gives no diversity benefit). If nothing
    # in the list is usable, we fall back to the default deep-think LLM.
    # Order rationale: Shehata & Li (2026), Table 2, measured Claude Sonnet
    # 4.6 as the lowest-τ synthesizer (4.5–31.2% vs Gemini's 60.1–98.9%);
    # DeepSeek shows similarly low correction-rejection in our internal
    # probes (tools/probe_tau_v2_deepseek.py) and is ~6–8x cheaper than
    # Anthropic. OpenAI/xAI omitted from the default — add them to this
    # list to enable, and credentials are picked up automatically.
    "synthesizer_provider_preference": ["anthropic", "deepseek", "google"],
    # When True, the Bull/Bear researchers and Aggressive/Conservative/Neutral
    # risk debaters are spread across multiple providers to break the
    # "Peer Pressure" / kinship-locked upstream pattern (Shehata & Li 2026,
    # Table 1: GGC, PPG, CCP configurations). With a united upstream front,
    # the Attention Latch Factor Λ approaches 2 even when the synthesizer
    # is architecturally distinct — terminal error rises to ~60% even with
    # B=1.0 (Logic Oracle case). Heterogenizing the upstream agents is the
    # only way to keep Λ near 1 and the linear gating equation valid.
    "diversify_debaters": True,
    # Ordered preference for debater providers (Bull/Bear, Aggressive/Conservative/Neutral).
    # Excludes Anthropic by default — Anthropic is reserved for synthesizers,
    # so debaters use the remaining low-tribalism families to maximize
    # architectural distance from the synthesizer (per the Heterogeneity
    # Mandate). Bull and Bear are assigned providers[0] and providers[1];
    # the three risk debaters cycle through with modular indexing. Providers
    # without API keys are skipped at wiring time, with fallback to the
    # default quick-thinking LLM.
    "debater_provider_preference": ["google", "deepseek"],
    # Checkpoint/resume: when True, LangGraph saves state after each node
    # so a crashed run can resume from the last successful step.
    "checkpoint_enabled": False,
    # Output language for analyst reports and final decision
    # Internal agent debate stays in English for reasoning quality
    "output_language": "English",
    # Debate and discussion settings.
    # Round count reduced from 5 → 2 per Shehata & Li (2026) Sycophantic
    # Scaling Law: σ scales exponentially with task complexity K, and each
    # extra round amplifies σ further when upstream is kinship-locked. With
    # the debater diversification above, 2 rounds gives a genuine
    # opening + rebuttal without compounding sycophantic pressure.
    "max_debate_rounds": 2,
    "max_risk_discuss_rounds": 2,
    # When True, the first turn of each debate is "blind" — Bull / Bear /
    # the first risk debater write their opening WITHOUT seeing the opponent's
    # output (and the opponent's first turn does not see the prior opener
    # either). Subsequent rounds are full-history rebuttals. Preserves the
    # independence condition for crowd-wisdom on the prior, while still
    # allowing genuine adversarial debate from round 2.
    "blind_first_round": True,
    # FinMem-style layered memory (Phase B).
    # Top-K per layer for past-context retrieval. Yu et al. (2023) ablation
    # in Table 5 shows K=5 gives the best Sharpe ratio (2.4960) and lowest
    # max drawdown — higher K (10) raises CR but hurts risk-adjusted return.
    "memory_top_k_per_layer": 5,
    # Trigger an extended (M-day retrospective) reflection every N trading
    # days. The extended reflection synthesizes recent immediate reflections
    # into a higher-order lesson and writes it to the deep layer.
    "extended_reflection_interval_days": 10,
    "extended_reflection_window_days": 30,
    "max_recur_limit": 1000,
    # Data vendor configuration
    # Category-level configuration (default for all tools in category)
    "data_vendors": {
        "core_stock_apis": "yfinance",       # Options: alpha_vantage, yfinance
        "technical_indicators": "yfinance",  # Options: alpha_vantage, yfinance
        "fundamental_data": "yfinance",      # Options: alpha_vantage, yfinance
        "news_data": "yfinance",             # Options: alpha_vantage, yfinance
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
    },
}
