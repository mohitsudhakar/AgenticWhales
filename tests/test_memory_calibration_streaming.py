"""Bundle coverage: memory_v2 vector helpers + readers, calibration data pulls,
and the streaming worker lifecycle/binding loader. All offline."""

from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace

import pytest

from agenticwhales.agents.schemas import Recipe, RecipeStatus, ScheduleKind


@pytest.fixture(autouse=True)
def _wipe():
    from web import auth
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


# ===========================================================================
# memory_v2
# ===========================================================================

def test_unpack_vector_roundtrip():
    from agenticwhales import memory_v2 as m
    blob = struct.pack("<3f", 1.0, 2.0, 3.0)
    assert m._unpack_vector(blob) == pytest.approx([1.0, 2.0, 3.0])
    # hex string with \x prefix
    assert m._unpack_vector("\\x" + blob.hex()) == pytest.approx([1.0, 2.0, 3.0])
    # plain hex string
    assert m._unpack_vector(blob.hex()) == pytest.approx([1.0, 2.0, 3.0])


def test_unpack_vector_bad_inputs():
    from agenticwhales import memory_v2 as m
    assert m._unpack_vector(None) == []
    assert m._unpack_vector("not-hex-zz") == []
    assert m._unpack_vector(12345) == []


def test_embed_gemini_via_fake(monkeypatch):
    from agenticwhales import memory_v2 as m
    import langchain_google_genai as lg

    class _FakeEmb:
        def __init__(self, model=None, google_api_key=None):
            pass

        def embed_query(self, text):
            return [0.1, 0.2, 0.3]

    monkeypatch.setattr(lg, "GoogleGenerativeAIEmbeddings", _FakeEmb)
    vec = m.embed_gemini("hello", model="text-embedding-004")
    assert vec == pytest.approx([0.1, 0.2, 0.3])


def test_list_embeddings_memstore():
    from web import auth
    from agenticwhales import memory_v2 as m
    auth._memstore[("memory_embeddings", "e1")] = {
        "entry_id": "e1", "user_id": "u1", "source": "journal"}
    auth._memstore[("memory_embeddings", "e2")] = {
        "entry_id": "e2", "user_id": "u1", "source": "paper_order"}
    assert len(m.list_embeddings("u1")) == 2
    assert len(m.list_embeddings("u1", source="journal")) == 1


def test_predictiveness_for():
    from web import auth
    from agenticwhales import memory_v2 as m
    # no paper_order_id → default
    assert m._predictiveness_for({}) == m.PREDICTIVENESS_DEFAULT
    # linked but no outcome → default
    assert m._predictiveness_for({"paper_order_id": "missing"}) == m.PREDICTIVENESS_DEFAULT
    # low Brier → high predictiveness
    auth._memstore[("decision_outcomes", "o1")] = {"brier_component": 0.04}
    score = m._predictiveness_for({"paper_order_id": "o1"})
    assert score == pytest.approx(0.96)


# ===========================================================================
# calibration
# ===========================================================================

def test_outcome_pairs():
    from web import auth
    from agenticwhales import calibration as cal
    auth._memstore[("decision_outcomes", "o1")] = {
        "user_id": "u1", "predicted_prob_of_profit": 0.7, "hit": True}
    auth._memstore[("decision_outcomes", "o2")] = {
        "user_id": "u1", "predicted_prob_of_profit": None, "hit": False}  # skipped
    pairs = cal._outcome_pairs("u1")
    assert pairs == [(0.7, True)]


def test_all_fits_memstore():
    from web import auth
    from agenticwhales import calibration as cal
    auth._memstore[("calibration_models", "m1")] = {
        "user_id": "u1", "regime": "all", "fitted_at": "2024-01-02"}
    auth._memstore[("calibration_models", "m2")] = {
        "user_id": "u1", "regime": "bull", "fitted_at": "2024-01-03"}
    assert len(cal._all_fits("u1")) == 2
    assert len(cal._all_fits("u1", regime="bull")) == 1


# ===========================================================================
# streaming_worker
# ===========================================================================

def _recipe(rid="r1", **kw):
    base = dict(
        id=rid, user_id="u1", name="R", tickers=["AAPL"],
        llm_provider="google", quick_model="q", deep_model="d",
        bull_model="x", bear_model="y",
        status=RecipeStatus.ACTIVE, schedule_kind=ScheduleKind.MANUAL,
        market_hours_only=False, max_daily_token_cost_usd=5.0,
        consecutive_failures=0,
        trigger_conditions={"kind": "price_move", "threshold_pct": 0.03,
                            "direction": "either"},
    )
    base.update(kw)
    return Recipe(**base)


def _worker():
    from web.streaming_worker import StreamingWorker
    from agenticwhales.streaming import InMemoryStreamClient
    return StreamingWorker(
        fire_recipe=lambda *a, **k: None,
        is_leader_fn=lambda: True,
        equity_client_factory=lambda syms: InMemoryStreamClient([]),
        crypto_client_factory=lambda syms: InMemoryStreamClient([]),
    )


def test_load_bindings_filters():
    w = _worker()
    w._load_bindings([
        _recipe("r1"),
        _recipe("r2", status=RecipeStatus.PAUSED),       # inactive → skipped
        _recipe("r3", trigger_conditions=None),          # no condition → skipped
    ])
    assert set(w._bindings.keys()) == {"r1"}


def test_load_bindings_bad_condition_skipped():
    w = _worker()
    w._load_bindings([_recipe("r1", trigger_conditions={"kind": "bogus_kind"})])
    assert w._bindings == {}


def test_start_and_stop():
    w = _worker()

    async def go():
        await w.start([_recipe("r1")])
        assert w._queue is not None
        assert "r1" in w._bindings
        await w.stop()
        assert w._queue is None

    asyncio.run(go())


def test_start_idempotent_when_running():
    w = _worker()

    async def go():
        await w.start([_recipe("r1")])
        q = w._queue
        await w.start([_recipe("r1")])  # already running → returns
        assert w._queue is q
        await w.stop()

    asyncio.run(go())
