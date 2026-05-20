"""Heterogeneity Mandate — fail-fast invariant check.

The core architectural claim of AgenticWhales is that **synthesizers must be
drawn from a different model family than the upstream debaters**, and ideally
the upstream debaters themselves are spread across families. The empirical
basis is in tests/evals/diversity_engine_eval.py; the design rationale is
in :mod:`agenticwhales.default_config`.

The diversification logic in :class:`agenticwhales.graph.trading_graph.AgenticWhalesGraph`
is *runtime tolerant*: when API keys are missing for the preferred provider,
it warns and falls back to the upstream LLM rather than failing the run.
That's correct for credential gaps but masks **config bugs** — e.g. the
preference list contains only the upstream provider, or the preference list
is empty, or a provider name is mistyped. Those will silently downgrade the
system from "diversified" to "single-family" without ever raising.

This module catches config-level violations at graph construction time so
they fail loudly, while leaving the existing credential-gap fallback path
intact for legitimate degraded operation.
"""

from __future__ import annotations

from typing import Any, Mapping

# Providers the graph knows how to construct via the diversification path.
# Kept in sync with ``AgenticWhalesGraph._PROVIDER_ENV_KEY`` and the
# ``MODEL_OPTIONS`` catalog. Unknown providers in the preference list almost
# always indicate a typo.
KNOWN_DIVERSIFICATION_PROVIDERS: frozenset[str] = frozenset({
    "openai", "anthropic", "google", "deepseek", "xai",
})


class HeterogeneityConfigError(ValueError):
    """Raised when the heterogeneity-mandate config is internally inconsistent."""


def heterogeneity_check(config: Mapping[str, Any]) -> None:
    """Validate the Heterogeneity Mandate config; raise on violation.

    Checks (all only fire when the corresponding ``diversify_*`` flag is on):

    1. The preference list is non-empty.
    2. The preference list contains at least one provider that differs from
       ``llm_provider`` (the upstream). If every entry equals upstream, no
       diversification is possible and the flag is a lie.
    3. Every entry in the preference list is a known provider name — typos
       silently downgrade the system, so we reject them at startup.

    Pure: takes config, raises or returns. No I/O, no LLM calls.
    """
    upstream = str(config.get("llm_provider", "") or "").lower()

    if config.get("diversify_synthesizers"):
        _check_preference_list(
            config.get("synthesizer_provider_preference"),
            role="synthesizer",
            upstream=upstream,
        )

    if config.get("diversify_debaters"):
        _check_preference_list(
            config.get("debater_provider_preference"),
            role="debater",
            upstream=upstream,
        )


def _check_preference_list(
    preference: Any,
    *,
    role: str,
    upstream: str,
) -> None:
    if not preference:
        raise HeterogeneityConfigError(
            f"diversify_{role}s=True but {role}_provider_preference is empty; "
            f"either disable diversification or list at least one alternative "
            f"provider that differs from llm_provider='{upstream}'."
        )

    normalized = [str(p).lower() for p in preference]

    unknown = [p for p in normalized if p not in KNOWN_DIVERSIFICATION_PROVIDERS]
    if unknown:
        raise HeterogeneityConfigError(
            f"{role}_provider_preference contains unknown provider(s) "
            f"{unknown!r}; expected one of {sorted(KNOWN_DIVERSIFICATION_PROVIDERS)}. "
            f"Typos silently downgrade the heterogeneity mandate."
        )

    if upstream and all(p == upstream for p in normalized):
        raise HeterogeneityConfigError(
            f"diversify_{role}s=True but every entry in {role}_provider_preference "
            f"equals llm_provider='{upstream}'; no diversification is possible. "
            f"Add at least one provider from a different family."
        )
