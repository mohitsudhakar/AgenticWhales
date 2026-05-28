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
    # upstream `deep_think_llm` provider.
    #
    # Design heuristic (correlated-failure reduction): when the synthesizer
    # shares a model family with the upstream debaters it tends to inherit
    # their biases — a Bull/Bear pair that both lean optimistic on a sector
    # is more likely to be rubber-stamped by a same-family judge than by a
    # cross-family one. Drawing the synthesizer from a different family is
    # the cheapest available diversification.
    #
    # This is an architectural choice, not a proven theorem. The empirical
    # claim ("synthesizer-family diversity reduces miscalibration vs.
    # all-same-family") is measured by the Diversity Engine eval — see
    # tests/evals/diversity_engine_eval.py.
    "diversify_synthesizers": True,
    # Ordered preference for synthesizer providers (used by both Research
    # Manager and Portfolio Manager). We walk this list and pick the first
    # provider that (a) has its API key set and (b) is not the upstream
    # provider (matching upstream gives no diversity benefit). If nothing
    # in the list is usable, we fall back to the default deep-think LLM.
    #
    # Order rationale (internal probes, not a published result):
    # tools/probe_tau_v2_deepseek.py and probe_tau_v3_deepseek.py measured
    # correction-acceptance rates across providers on a synthetic
    # disagreement set. Anthropic and DeepSeek showed the highest rate of
    # accepting a well-reasoned counter-argument from an upstream debater
    # (i.e. they "judge" rather than "rubber-stamp"); DeepSeek is ~6-8x
    # cheaper than Anthropic, so we list Anthropic first for quality and
    # DeepSeek second for cost. Google sits behind both as a fallback.
    # OpenAI/xAI omitted from the default — add them to this list to
    # enable, credentials are picked up automatically.
    "synthesizer_provider_preference": ["anthropic", "deepseek", "google"],
    # When True, the Bull/Bear researchers and Aggressive/Conservative/Neutral
    # risk debaters are spread across multiple providers to break the
    # all-same-family upstream pattern.
    #
    # Design heuristic: even with a cross-family synthesizer, if every
    # upstream debater is the same family they tend to converge on a
    # united front — Bull and Bear both reason from the same priors, and
    # the disagreement they surface is shallow. Spreading the debaters
    # across providers makes the disagreement deeper and the synthesis
    # job harder (which is what we want).
    #
    # The empirical claim ("debater-family diversity raises disagreement
    # signal vs noise") is measured by the Diversity Engine eval.
    "diversify_debaters": True,
    # Ordered preference for debater providers (Bull/Bear, Aggressive/Conservative/Neutral).
    # Excludes Anthropic by default — Anthropic is reserved for synthesizers,
    # so debaters use other families to maximize provider distance from
    # the synthesizer. Bull and Bear are assigned providers[0] and
    # providers[1]; the three risk debaters cycle through with modular
    # indexing. Providers without API keys are skipped at wiring time,
    # with fallback to the default quick-thinking LLM.
    "debater_provider_preference": ["google", "deepseek"],
    # Checkpoint/resume: when True, LangGraph saves state after each node
    # so a crashed run can resume from the last successful step.
    "checkpoint_enabled": False,
    # Output language for analyst reports and final decision
    # Internal agent debate stays in English for reasoning quality
    "output_language": "English",
    # Debate and discussion settings.
    # Round count is 2 (opening + one rebuttal). The original upstream
    # framework used 5; we found in internal smoke tests that beyond round 2
    # the debaters tend to repeat earlier arguments with progressively
    # weaker variations rather than introduce new evidence, so the marginal
    # token spend stops buying signal. The diversification settings above
    # ensure the two rounds carry real adversarial content rather than
    # converging on a united front, which is where 2 rounds would otherwise
    # be too short.
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
        "political_data": "quiverquant",     # Congressional disclosed trades
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
    },
}
