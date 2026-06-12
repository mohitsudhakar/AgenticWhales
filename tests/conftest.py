"""Shared pytest fixtures that prevent CI hangs when API keys are absent."""

import os
from unittest.mock import MagicMock, patch

import pytest


def pytest_configure(config):
    for marker in ("unit", "integration", "smoke"):
        config.addinivalue_line("markers", f"{marker}: {marker}-level tests")


_API_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "ZHIPU_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
)


@pytest.fixture(autouse=True)
def _dummy_api_keys(monkeypatch):
    for env_var in _API_KEY_ENV_VARS:
        # `or` (not a get-default): a .env scaffolded from the template carries
        # EMPTY values, which load_dotenv puts into os.environ as "" — that must
        # still fall back to the placeholder or key-gated tests get flaky.
        monkeypatch.setenv(env_var, os.environ.get(env_var) or "placeholder")


# Unit tests must never hit the real Supabase. The integration suite under
# tests/integ/ uses testcontainers and opts in via `@pytest.mark.integration`.
# If the developer has real creds in `.env` (recommended for the running
# server), strip them during unit-test collection so `_db_writable()` returns
# False and `_memstore` is the source of truth.
_FORCE_OFFLINE_ENV_VARS = (
    "AGENTICWHALES_SUPABASE_URL",
    "AGENTICWHALES_SUPABASE_ANON_KEY",
    "AGENTICWHALES_SUPABASE_SERVICE_KEY",
)


@pytest.fixture(autouse=True)
def _force_offline_supabase(monkeypatch, request):
    if "integration" in request.keywords:
        return  # the integration suite manages its own DB lifecycle
    for env_var in _FORCE_OFFLINE_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)


@pytest.fixture(autouse=True)
def _offline_price_fetcher(monkeypatch, request):
    """Unit tests must never hit yfinance. Endpoints that audit with the
    price-aware path (/api/coach/latest, sync, uploads) degrade to the
    price-free leak set when the fetcher returns None — same as a network
    failure in production. Tests that want price-path coverage inject their
    own fake fetcher explicitly."""
    if "integration" in request.keywords:
        return
    import agenticwhales.prices as _prices
    monkeypatch.setattr(_prices, "fetch_ohlc", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _reset_guest_upload_caps():
    """Guest document-upload caps are per-process state; isolate tests."""
    try:
        import web.coach_api as _coach_api
        _coach_api._GUEST_UPLOADS.clear()
        _coach_api._LATEST_REPORT_CACHE.clear()
    except Exception:  # noqa: BLE001 — web extras may be absent in minimal envs
        pass
    yield


@pytest.fixture()
def mock_llm_client():
    client = MagicMock()
    client.get_llm.return_value = MagicMock()
    with patch(
        "agenticwhales.llm_clients.factory.create_llm_client",
        return_value=client,
    ):
        yield client
