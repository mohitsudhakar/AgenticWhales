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
    # When True, the Portfolio Manager (final decision-maker) uses a
    # synthesizer chosen from `pm_provider_preference` rather than the
    # upstream provider, to reduce correlated failure modes from a single
    # model family's training data and RLHF biases.
    "diversify_portfolio_manager": True,
    # Ordered preference for the Portfolio Manager's provider. We walk this
    # list and pick the first provider that (a) has its API key set and
    # (b) is not the upstream provider (matching upstream gives no diversity
    # benefit). If nothing in the list is usable, we fall back to the default
    # deep-think LLM. DeepSeek is the only default candidate: it shows low
    # correction-rejection rates in our internal probes
    # (tools/probe_tau_v2_deepseek.py) and is ~6-8x cheaper than Anthropic.
    # Users with an Anthropic key may extend this list — Shehata & Li (2026)
    # measured Claude as the lowest-τ synthesizer in their swarm experiments.
    "pm_provider_preference": ["deepseek"],
    # Checkpoint/resume: when True, LangGraph saves state after each node
    # so a crashed run can resume from the last successful step.
    "checkpoint_enabled": False,
    # Output language for analyst reports and final decision
    # Internal agent debate stays in English for reasoning quality
    "output_language": "English",
    # Debate and discussion settings
    "max_debate_rounds": 5,
    "max_risk_discuss_rounds": 5,
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
