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
        monkeypatch.setenv(env_var, os.environ.get(env_var, "placeholder"))


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


@pytest.fixture()
def mock_llm_client():
    client = MagicMock()
    client.get_llm.return_value = MagicMock()
    with patch(
        "agenticwhales.llm_clients.factory.create_llm_client",
        return_value=client,
    ):
        yield client
