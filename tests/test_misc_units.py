"""Bundle coverage for several small modules: adaptive prompt-eval harness,
graph checkpointer cleanup, transactions text chunker, extended reflection,
and the fire-cost recorder. All offline."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _wipe():
    from web import auth
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


# ===========================================================================
# adaptive.evaluate_prompt_variant + list_recent_evals
# ===========================================================================

def test_evaluate_prompt_variant_promotes_and_lists():
    from web import auth
    from agenticwhales import adaptive
    # 20 resolved outcomes, all hits, live prompt claimed only 0.5.
    for i in range(20):
        auth._memstore[("decision_outcomes", f"o{i}")] = {
            "paper_order_id": f"o{i}", "user_id": "u1", "hit": True,
            "predicted_prob_of_profit": 0.5,
        }
    result = adaptive.evaluate_prompt_variant(
        "u1", variant="sharper", scorer=lambda r: 0.9)
    assert result is not None and result.promoted is True
    assert result.n_samples == 20
    evals = adaptive.list_recent_evals("u1")
    assert len(evals) == 1 and evals[0]["variant"] == "sharper"


def test_evaluate_prompt_variant_below_min_n_returns_none():
    from web import auth
    from agenticwhales import adaptive
    auth._memstore[("decision_outcomes", "o0")] = {
        "paper_order_id": "o0", "user_id": "u1", "hit": True,
        "predicted_prob_of_profit": 0.5,
    }
    assert adaptive.evaluate_prompt_variant("u1", variant="x", scorer=lambda r: 0.9) is None


# ===========================================================================
# graph.checkpointer cleanup
# ===========================================================================

def test_clear_all_checkpoints(tmp_path):
    from agenticwhales.graph import checkpointer
    assert checkpointer.clear_all_checkpoints(tmp_path) == 0  # dir absent
    cp = tmp_path / "checkpoints"
    cp.mkdir()
    (cp / "a.db").write_text("x")
    (cp / "b.db").write_text("y")
    assert checkpointer.clear_all_checkpoints(tmp_path) == 2
    assert list(cp.glob("*.db")) == []


def test_clear_checkpoint_missing_db_noop(tmp_path):
    from agenticwhales.graph import checkpointer
    # no db file for this ticker → returns without raising
    checkpointer.clear_checkpoint(tmp_path, "AAPL", "2024-01-02")


# ===========================================================================
# transactions.parser.chunk_text
# ===========================================================================

def test_chunk_text():
    from agenticwhales.transactions.parser import chunk_text
    assert chunk_text("short", 100) == ["short"]
    text = "\n".join(f"line{i}" for i in range(10))  # 10 lines
    chunks = chunk_text(text, 20)
    assert len(chunks) > 1
    assert "\n".join(chunks).replace("\n", "") == text.replace("\n", "")


# ===========================================================================
# graph.reflection.extended_reflection
# ===========================================================================

def test_extended_reflection_empty():
    from agenticwhales.graph.reflection import Reflector
    r = Reflector(SimpleNamespace(invoke=lambda m: SimpleNamespace(content="x")))
    assert r.extended_reflection([]) == ""


def test_extended_reflection_builds_window():
    from agenticwhales.graph.reflection import Reflector
    seen = {}
    llm = SimpleNamespace(invoke=lambda messages: seen.update(msgs=messages)
                          or SimpleNamespace(content="RETROSPECTIVE"))
    r = Reflector(llm)
    entries = [{"date": "2024-01-02", "ticker": "AAPL", "rating": "Buy",
                "raw": 0.05, "alpha": 0.02, "reflection": "good call"}]
    out = r.extended_reflection(entries)
    assert out == "RETROSPECTIVE"
    # the window text reached the model
    human = seen["msgs"][1][1]
    assert "AAPL" in human and "good call" in human


# ===========================================================================
# cost_middleware.record_fire_cost
# ===========================================================================

def test_record_fire_cost_returns_decimal():
    from agenticwhales.llm_clients.cost_middleware import record_fire_cost
    cost = record_fire_cost(
        user_id="u1", recipe_id=None, session_id="s1",
        provider="google", quick_model="q", deep_model="d",
        stats={"tokens_in": 1000, "tokens_out": 500, "llm_calls": 3, "tool_calls": 1},
    )
    assert isinstance(cost, Decimal) and cost >= 0


def test_record_fire_cost_model_usage_branch():
    from agenticwhales.llm_clients.cost_middleware import record_fire_cost
    cost = record_fire_cost(
        user_id="u1", recipe_id="rec1", session_id="s2",
        provider="google", quick_model="q", deep_model="d",
        stats={"tokens_in": 0, "tokens_out": 0,
               "model_usage": {"gemini": {"tokens_in": 100, "tokens_out": 50}}},
    )
    assert isinstance(cost, Decimal)
