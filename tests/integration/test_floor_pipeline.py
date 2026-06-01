"""Phase 1 regression baseline for the floor live debate.

These tests build a full ``AgenticWhalesGraph`` with mocked LLM clients and
assert on the per-role provider assignments. They are the safety net for
the Phase 2 refactors and the gate that keeps the synthesizer collision
(C1) and adjacent-debater collision (C2) from silently regressing.

No live API calls are made: ``create_llm_client`` is patched where it is
used in ``agenticwhales.graph.trading_graph``. Filesystem side effects
(cache dir, results dir, memory log) are redirected to a per-test
``tmp_path``.

The tests cover four configurations:

1. Default config with all credentials present — happy path. The two
   synthesizers should land on different providers and no adjacent
   debaters should collide.
2. Default config with the third debater provider's credentials missing —
   degraded-but-tolerable path. Adjacent debaters collide; the per-slot
   ``degraded`` flag must surface that.
3. Default config with only the upstream provider's credentials present —
   fully degraded path. All synthesizer + debater slots fall back to
   upstream and are marked degraded.
4. Default config with both synthesizer-preference providers' credentials
   missing — partial synthesizer degradation. Research Manager falls back
   to upstream; Portfolio Manager too (and the WARN logs).
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agenticwhales.default_config import DEFAULT_CONFIG


pytestmark = pytest.mark.integration


# --------------------------------------------------------------------- fixtures


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """Redirect on-disk side effects of ``AgenticWhalesGraph.__init__`` to tmp_path.

    The graph creates the cache and results directories at construction
    time and the ``TradingMemoryLog`` writes to the memory_log_path. None
    of these are interesting to the floor-pipeline tests, but they will
    pollute the user's home directory if left alone.
    """
    monkeypatch.setenv("AGENTICWHALES_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTICWHALES_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("AGENTICWHALES_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AGENTICWHALES_MEMORY_LOG_PATH", str(tmp_path / "memory.md"))


@pytest.fixture
def mock_llm_factory():
    """Replace ``create_llm_client`` (as imported into trading_graph) with a mock.

    The returned client.get_llm() yields a fresh MagicMock per call so
    callers can identify which LLM ended up bound to which slot if they
    care. The factory records the (provider, mode-derived) calls so a
    test can introspect what was requested.
    """
    calls = []

    def _factory(provider, model, base_url=None, **kwargs):
        calls.append({"provider": provider, "model": model})
        client = MagicMock(name=f"llm_client[{provider}/{model}]")
        client.get_llm.return_value = MagicMock(name=f"llm[{provider}/{model}]")
        return client

    with patch(
        "agenticwhales.graph.trading_graph.create_llm_client",
        side_effect=_factory,
    ):
        yield calls


def _build_graph(config_overrides: dict | None = None):
    """Construct an AgenticWhalesGraph with the merged config."""
    # Import here so the patch above takes effect before the class is touched.
    from agenticwhales.graph.trading_graph import AgenticWhalesGraph

    config = dict(DEFAULT_CONFIG)
    if config_overrides:
        config.update(config_overrides)

    return AgenticWhalesGraph(
        selected_analysts=["market", "quant", "social", "news", "fundamentals"],
        debug=False,
        config=config,
    )


# ------------------------------------------------------------------ happy path


def test_default_config_synthesizers_use_different_providers(
    isolated_paths, mock_llm_factory
):
    """P1.1 regression: research_manager and portfolio_manager must differ."""
    graph = _build_graph()
    status = graph.get_diversification_status()
    a = status["assignments"]

    rm = a["research_manager"]["provider"]
    pm = a["portfolio_manager"]["provider"]

    assert rm != pm, (
        f"Synthesizer collision: research_manager and portfolio_manager both "
        f"landed on {rm!r}. The Heterogeneity Mandate is broken at the "
        f"second synthesizer."
    )
    assert a["research_manager"]["degraded"] is False
    assert a["portfolio_manager"]["degraded"] is False


def test_default_config_no_adjacent_debater_collision(
    isolated_paths, mock_llm_factory
):
    """P1.2 regression: no two debaters that face each other share a provider.

    Investment debate: Bull <-> Bear.
    Risk debate round-robin: Aggressive -> Conservative -> Neutral -> Aggressive.
    With the default three-entry ``debater_provider_preference`` and all
    credentials present, every adjacent pair must differ.
    """
    graph = _build_graph()
    a = graph.get_diversification_status()["assignments"]

    adjacent_pairs = [
        ("bull", "bear"),
        ("aggressive", "conservative"),
        ("conservative", "neutral"),
        ("neutral", "aggressive"),
    ]
    for x, y in adjacent_pairs:
        assert a[x]["provider"] != a[y]["provider"], (
            f"Adjacent debater collision: {x} and {y} both on "
            f"{a[x]['provider']!r}."
        )
        assert a[x]["degraded"] is False, f"{x} is degraded under happy path"
        assert a[y]["degraded"] is False, f"{y} is degraded under happy path"


def test_diversification_status_top_level_degraded_false_on_happy_path(
    isolated_paths, mock_llm_factory
):
    status = graph_status_for_default()
    assert status["degraded"] is False


def graph_status_for_default():
    # Helper used by the assertion above; kept separate from the fixtures
    # so test parameterization stays linear.
    graph = _build_graph()
    return graph.get_diversification_status()


def test_diversification_status_shape(isolated_paths, mock_llm_factory):
    """The public shape returned to the web layer must be stable."""
    graph = _build_graph()
    status = graph.get_diversification_status()

    assert set(status.keys()) == {"degraded", "assignments"}
    assignments = status["assignments"]

    required_roles = {
        "upstream",
        "research_manager",
        "portfolio_manager",
        "bull",
        "bear",
        "aggressive",
        "conservative",
        "neutral",
    }
    assert set(assignments.keys()) == required_roles

    for role, info in assignments.items():
        assert "provider" in info, f"role {role} missing provider"
        assert "degraded" in info, f"role {role} missing degraded flag"
        assert isinstance(info["degraded"], bool)


# ------------------------------------------------- partial debater degradation


def test_two_debater_providers_marks_collision_as_degraded(
    isolated_paths, mock_llm_factory, monkeypatch, caplog
):
    """With only 2 usable debater providers, Neutral collides with Aggressive.

    This was the C2 bug before P1.2: the collision happened silently. The
    fix logs a WARN, marks the colliding slots ``degraded``, and the top-
    level status flips to degraded so the UI can banner it.
    """
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    with caplog.at_level(logging.WARNING, logger="agenticwhales.graph.trading_graph"):
        graph = _build_graph()
    status = graph.get_diversification_status()
    a = status["assignments"]

    # With ["google", "deepseek", "xai"] and xai missing, usable=[google, deepseek].
    # Adjacent risk-debate pair Neutral↔Aggressive collides on google.
    assert a["aggressive"]["provider"] == a["neutral"]["provider"]
    assert a["aggressive"]["degraded"] is True
    assert a["neutral"]["degraded"] is True
    assert status["degraded"] is True

    # WARN should explicitly mention the partial degradation.
    assert any(
        "PARTIALLY DEGRADED" in rec.message
        for rec in caplog.records
    ), "expected a PARTIALLY DEGRADED warning on len(usable)<3"


# -------------------------------------- fully degraded (only upstream creds set)


def test_only_upstream_creds_set_marks_all_diversified_slots_degraded(
    isolated_paths, mock_llm_factory, monkeypatch, caplog
):
    """If no preference provider has credentials, every diversified slot falls
    back to upstream and is marked degraded.
    """
    for env in (
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "DEEPSEEK_API_KEY",
        "XAI_API_KEY",
    ):
        monkeypatch.delenv(env, raising=False)

    with caplog.at_level(logging.WARNING, logger="agenticwhales.graph.trading_graph"):
        graph = _build_graph()
    status = graph.get_diversification_status()
    a = status["assignments"]

    upstream = DEFAULT_CONFIG["llm_provider"].lower()
    diversified_roles = [
        "research_manager",
        "portfolio_manager",
        "bull",
        "bear",
        "aggressive",
        "conservative",
        "neutral",
    ]
    for role in diversified_roles:
        assert a[role]["provider"] == upstream, (
            f"{role} did not fall back to upstream {upstream!r} "
            f"(got {a[role]['provider']!r})"
        )
        assert a[role]["degraded"] is True

    assert status["degraded"] is True


# ----------------------------------------- portfolio manager falls back alone


def test_portfolio_manager_falls_back_when_only_one_synth_provider_available(
    isolated_paths, mock_llm_factory, monkeypatch, caplog
):
    """With only Anthropic credentials in the synthesizer pool, Research
    Manager takes Anthropic and Portfolio Manager has nothing else to pick
    — it falls back to upstream and is marked degraded.
    """
    # The conftest's autouse fixture preserves whatever ANTHROPIC_API_KEY
    # is in the real env. If a developer has it set to an empty string (a
    # common pattern when copying .env templates), the autouse fixture
    # keeps it empty and _provider_has_credentials returns False. Pin it
    # to a non-empty placeholder for this test so the assertion below
    # doesn't depend on the developer's shell state.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder")
    # synthesizer_provider_preference = ["anthropic", "deepseek", "google"]
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    with caplog.at_level(logging.WARNING, logger="agenticwhales.graph.trading_graph"):
        graph = _build_graph()
    status = graph.get_diversification_status()
    a = status["assignments"]

    upstream = DEFAULT_CONFIG["llm_provider"].lower()
    assert a["research_manager"]["provider"] == "anthropic"
    assert a["research_manager"]["degraded"] is False
    assert a["portfolio_manager"]["provider"] == upstream
    assert a["portfolio_manager"]["degraded"] is True
    assert status["degraded"] is True

    # The WARN must mention the affected role.
    assert any(
        "portfolio_manager" in rec.message and "DEGRADED" in rec.message
        for rec in caplog.records
    ), "expected a WARN naming portfolio_manager and DEGRADED"
