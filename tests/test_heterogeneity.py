"""Tests for heterogeneity enforcement on recipe creation.

Bull and Bear models MUST come from different model families. This catches
the most common LLM-as-multi-agent failure mode: identical priors masquerading
as adversarial debate.
"""

from __future__ import annotations

import pytest

from agenticwhales.recipes import (
    HeterogeneityError,
    build_recipe,
    family_of,
    validate_heterogeneity,
)


class TestFamilyDetection:
    @pytest.mark.parametrize("model,family", [
        ("gpt-5.4",                "openai"),
        ("gpt-5.4-mini",           "openai"),
        ("o4-mini",                "openai"),
        ("claude-4.6-sonnet",      "anthropic"),
        ("claude-3.5-haiku",       "anthropic"),
        ("gemini-3.1-pro-preview", "google"),
        ("gemini-3-flash-preview", "google"),
        ("deepseek-v4",            "deepseek"),
        ("grok-3",                 "xai"),
        ("glm-4.6",                "zhipu"),
        ("qwen-2.5-72b",           "qwen"),
        ("llama3:70b",             "ollama"),
    ])
    def test_known_prefixes(self, model, family):
        assert family_of(model) == family

    def test_unknown_returns_unknown(self):
        assert family_of("custom-internal-model-v2") == "unknown"

    def test_blank_returns_unknown(self):
        assert family_of("") == "unknown"


class TestValidator:
    def test_passes_with_distinct_families(self):
        # No exception.
        validate_heterogeneity("gpt-5.4", "claude-4.6-sonnet")
        validate_heterogeneity("gemini-3.1-pro-preview", "deepseek-v4")

    @pytest.mark.parametrize("bull,bear", [
        ("gpt-5.4", "gpt-5.4-mini"),
        ("claude-4.6-sonnet", "claude-4.6-haiku"),
        ("gemini-3.1-pro-preview", "gemini-3-flash-preview"),
    ])
    def test_rejects_same_family_pairs(self, bull, bear):
        with pytest.raises(HeterogeneityError):
            validate_heterogeneity(bull, bear)

    def test_rejects_two_unknown_models(self):
        # Operationally the most common footgun: two custom local models on
        # the same backend. Treated as same family ('unknown').
        with pytest.raises(HeterogeneityError):
            validate_heterogeneity("local-model-a", "local-model-b")


class TestBuildRecipe:
    def _base(self, **overrides):
        form = {
            "name": "verify",
            "tickers": ["AAPL"],
            "llm_provider": "google",
            "quick_model": "gemini-3-flash-preview",
            "deep_model": "gemini-3.1-pro-preview",
            "bull_model": "deepseek-v4",
            "bear_model": "gemini-3.1-pro-preview",
            "analysts": ["market", "quant"],
            "schedule_kind": "interval",
            "schedule_expr": "60s",
        }
        form.update(overrides)
        return form

    def test_builds_valid_recipe(self):
        rec = build_recipe(self._base(), user_id="u-1")
        assert rec.user_id == "u-1"
        assert rec.tickers == ["AAPL"]
        assert rec.bull_model == "deepseek-v4"
        assert rec.bear_model == "gemini-3.1-pro-preview"

    def test_rejects_same_family_bull_bear(self):
        form = self._base(bull_model="gemini-3-flash-preview")
        with pytest.raises(HeterogeneityError):
            build_recipe(form, user_id="u-1")

    def test_uppercases_tickers(self):
        rec = build_recipe(self._base(tickers=["aapl", "msft"]), user_id="u-1")
        assert rec.tickers == ["AAPL", "MSFT"]

    def test_requires_bull_and_bear(self):
        form = self._base()
        form["bull_model"] = ""
        with pytest.raises(ValueError):
            build_recipe(form, user_id="u-1")
