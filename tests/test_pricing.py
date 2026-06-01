"""Tests for the versioned LLM pricing table."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from agenticwhales.llm_clients.pricing import (
    PriceRow,
    clear_cache,
    cost_for,
    known_models,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_cache()
    yield
    clear_cache()


class TestCostFor:
    def test_gemini_flash_input_only(self):
        # 1M input @ 0.075 → $0.075
        cost = cost_for("google", "gemini-3-flash-preview", input_tokens=1_000_000)
        assert cost == pytest.approx(Decimal("0.075"))

    def test_gemini_pro_mixed(self):
        # 100k input @ 1.25/1M + 50k output @ 10/1M = $0.125 + $0.50 = $0.625
        cost = cost_for(
            "google", "gemini-3.1-pro-preview",
            input_tokens=100_000, output_tokens=50_000,
        )
        assert cost == pytest.approx(Decimal("0.625"))

    def test_deepseek_cheap(self):
        cost = cost_for("deepseek", "deepseek-v4", input_tokens=1_000_000, output_tokens=1_000_000)
        # 0.27 + 1.10 = 1.37
        assert cost == pytest.approx(Decimal("1.37"))

    def test_unknown_model_raises(self):
        with pytest.raises(ValueError, match="no pricing"):
            cost_for("openai", "totally-fake-model-9000", input_tokens=10)

    def test_provider_case_insensitive(self):
        c1 = cost_for("google", "gemini-3-flash-preview", input_tokens=1000)
        c2 = cost_for("GOOGLE", "gemini-3-flash-preview", input_tokens=1000)
        assert c1 == c2


class TestKnownModels:
    def test_seed_models_present(self):
        models = dict(known_models())
        assert "gemini-3-flash-preview" in (m for _, m in known_models())
        assert any(p == "deepseek" for p, _ in known_models())

    def test_returns_sorted(self):
        ms = known_models()
        assert ms == sorted(ms)


class TestEffectiveAt:
    def test_uses_most_recent_effective(self, monkeypatch):
        # Simulate a future price change by injecting a newer seed.
        from agenticwhales.llm_clients import pricing as pricing_mod
        newer = PriceRow(
            provider="deepseek", model="deepseek-v4",
            input_per_1m=Decimal("0.50"), output_per_1m=Decimal("2.00"),
            cache_read_per_1m=None, reasoning_per_1m=None,
            effective_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )
        monkeypatch.setattr(
            pricing_mod, "_LOCAL_SEED",
            pricing_mod._LOCAL_SEED + [newer],
        )
        clear_cache()

        # Query for a future date — should pick the newer row.
        future = datetime(2027, 6, 1, tzinfo=timezone.utc)
        cost_now = cost_for("deepseek", "deepseek-v4", input_tokens=1_000_000)
        cost_future = cost_for("deepseek", "deepseek-v4", input_tokens=1_000_000, at=future)
        assert cost_future > cost_now
        assert cost_future == pytest.approx(Decimal("0.50"))
