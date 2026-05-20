"""Tests for the Heterogeneity Mandate config check.

Pure config validation — no LLM construction, no network. The check fails
loud on config bugs that would silently downgrade the system to single-family
while still allowing the runtime credential-gap fallback elsewhere.
"""

import pytest

from agenticwhales.heterogeneity import (
    HeterogeneityConfigError,
    heterogeneity_check,
)


def _base(**overrides):
    cfg = {
        "llm_provider": "openai",
        "diversify_synthesizers": False,
        "diversify_debaters": False,
        "synthesizer_provider_preference": [],
        "debater_provider_preference": [],
    }
    cfg.update(overrides)
    return cfg


@pytest.mark.unit
class TestHeterogeneityCheck:
    def test_no_op_when_both_diversifications_off(self):
        # Should not raise even with junk preference lists.
        heterogeneity_check(_base(synthesizer_provider_preference=["bogus"]))

    def test_passes_with_valid_synthesizer_diversification(self):
        heterogeneity_check(_base(
            diversify_synthesizers=True,
            synthesizer_provider_preference=["anthropic", "deepseek"],
        ))

    def test_passes_with_valid_debater_diversification(self):
        heterogeneity_check(_base(
            diversify_debaters=True,
            debater_provider_preference=["google", "deepseek"],
        ))

    def test_empty_preference_raises_for_synthesizer(self):
        with pytest.raises(HeterogeneityConfigError, match="synthesizer_provider_preference is empty"):
            heterogeneity_check(_base(
                diversify_synthesizers=True,
                synthesizer_provider_preference=[],
            ))

    def test_empty_preference_raises_for_debater(self):
        with pytest.raises(HeterogeneityConfigError, match="debater_provider_preference is empty"):
            heterogeneity_check(_base(
                diversify_debaters=True,
                debater_provider_preference=None,
            ))

    def test_preference_equals_upstream_raises(self):
        with pytest.raises(HeterogeneityConfigError, match="no diversification is possible"):
            heterogeneity_check(_base(
                llm_provider="openai",
                diversify_synthesizers=True,
                synthesizer_provider_preference=["openai", "OpenAI"],
            ))

    def test_typo_in_preference_raises(self):
        # "anthropc" → typo; would silently fall through credentials check.
        with pytest.raises(HeterogeneityConfigError, match="unknown provider"):
            heterogeneity_check(_base(
                diversify_synthesizers=True,
                synthesizer_provider_preference=["anthropc"],
            ))

    def test_case_insensitive(self):
        # Upstream and preferences compared case-insensitively.
        heterogeneity_check(_base(
            llm_provider="OPENAI",
            diversify_synthesizers=True,
            synthesizer_provider_preference=["Anthropic", "DEEPSEEK"],
        ))

    def test_default_config_passes(self):
        # The shipped default config must satisfy the mandate.
        from agenticwhales.default_config import DEFAULT_CONFIG

        heterogeneity_check(DEFAULT_CONFIG)
