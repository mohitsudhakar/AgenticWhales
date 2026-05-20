"""Tests for `agenticwhales.memory_v2` — Phase 2 deliverable #4.

Three layers:
  1. Pure math — embed + cosine deterministic + invariants.
  2. Persistence round-trip — upsert + list + vector unpack.
  3. Outcome-predictive re-ranker — entries with low Brier outrank
     topically-equivalent ones with high Brier.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from agenticwhales import memory_v2
from web import auth


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


# ---------------------------------------------------------------------------
# Embedding math
# ---------------------------------------------------------------------------

class TestEmbed:
    def test_deterministic(self):
        a = memory_v2.embed("buy strong momentum AAPL")
        b = memory_v2.embed("buy strong momentum AAPL")
        assert a == b

    def test_dim_matches_const(self):
        v = memory_v2.embed("anything")
        assert len(v) == memory_v2.EMBED_DIM

    def test_cosine_self_is_one(self):
        v = memory_v2.embed("buy AAPL momentum")
        assert memory_v2.cosine(v, v) == pytest.approx(1.0, rel=1e-6)

    def test_cosine_orthogonal_text_low(self):
        a = memory_v2.embed("buy AAPL momentum technicals")
        b = memory_v2.embed("crude oil OPEC quota supply shock")
        sim = memory_v2.cosine(a, b)
        assert sim < 0.2

    def test_unsupported_model_raises(self):
        with pytest.raises(ValueError):
            memory_v2.embed("x", model="not-a-real-model")

    def test_default_resolution_prefers_env(self, monkeypatch):
        monkeypatch.setenv("AGENTICWHALES_EMBEDDING_MODEL", "hashing-trick")
        memory_v2.reset_default_model_cache()
        assert memory_v2._default_embedding_model() == "hashing-trick"

    def test_default_resolution_picks_gemini_when_keyed(self, monkeypatch):
        monkeypatch.delenv("AGENTICWHALES_EMBEDDING_MODEL", raising=False)
        monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
        memory_v2.reset_default_model_cache()
        assert memory_v2._default_embedding_model() == memory_v2.GEMINI_DEFAULT_MODEL

    def test_default_falls_back_to_hashing_trick(self, monkeypatch):
        monkeypatch.delenv("AGENTICWHALES_EMBEDDING_MODEL", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        memory_v2.reset_default_model_cache()
        assert memory_v2._default_embedding_model() == "hashing-trick"

    def test_gemini_failure_falls_back_silently(self, monkeypatch):
        """When Gemini errors (no key, quota, network), embed() must not
        raise — it should fall back to hashing-trick so retrieval stays up."""
        from agenticwhales import memory_v2 as mv2

        def boom(text, **_):
            raise RuntimeError("simulated provider error")
        monkeypatch.setattr(mv2, "embed_gemini", boom)
        vec = mv2.embed("hello world", model="text-embedding-004")
        # Should be the hashing-trick fallback length.
        assert len(vec) == mv2.EMBED_DIM


# ---------------------------------------------------------------------------
# Pack / unpack round-trip
# ---------------------------------------------------------------------------

class TestVectorBlob:
    def test_pack_unpack_round_trip(self):
        v = memory_v2.embed("the quick brown fox")
        blob_hex = memory_v2._pack_vector(v).hex()
        recovered = memory_v2._unpack_vector(blob_hex)
        assert len(recovered) == len(v)
        # Float32 has limited precision; check approximately.
        for a, b in zip(v, recovered):
            assert a == pytest.approx(b, abs=1e-5)

    def test_unpack_empty_returns_empty(self):
        assert memory_v2._unpack_vector(None) == []
        assert memory_v2._unpack_vector("") == []


# ---------------------------------------------------------------------------
# upsert_embedding + list_embeddings
# ---------------------------------------------------------------------------

class TestStorage:
    def test_upsert_then_list(self):
        memory_v2.upsert_embedding(
            entry_id="e1", user_id="u-1", source="journal",
            text="strong buy momentum apple",
        )
        rows = memory_v2.list_embeddings("u-1")
        assert len(rows) == 1
        assert rows[0]["entry_id"] == "e1"
        assert rows[0]["source"] == "journal"

    def test_filter_by_source(self):
        memory_v2.upsert_embedding(
            entry_id="e1", user_id="u-1", source="journal", text="journal entry"
        )
        memory_v2.upsert_embedding(
            entry_id="e2", user_id="u-1", source="paper_order", text="order rationale"
        )
        only_journal = memory_v2.list_embeddings("u-1", source="journal")
        assert [r["entry_id"] for r in only_journal] == ["e1"]


# ---------------------------------------------------------------------------
# Retrieval + re-ranker
# ---------------------------------------------------------------------------

def _seed_journal_entry(user_id, *, entry_id, body, paper_order_id=None,
                        kind="note", is_draft=False):
    row = {
        "id": entry_id,
        "user_id": user_id,
        "session_id": None,
        "paper_order_id": paper_order_id,
        "thesis_id": None,
        "kind": kind,
        "body": body,
        "sentiment_score": None,
        "is_draft": is_draft,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    auth.save_journal_entry(row)
    memory_v2.index_journal_entry(row)
    return row


def _seed_outcome(paper_order_id, *, user_id, brier_component):
    auth._memstore[("decision_outcomes", paper_order_id)] = {
        "paper_order_id": paper_order_id,
        "user_id": user_id,
        "ticker": "AAPL",
        "predicted_return_pct": 5.0,
        "predicted_volatility_pct": 20.0,
        "predicted_prob_of_profit": 0.6,
        "predicted_hold_days": 30,
        "realized_return_pct": 8.0,
        "realized_at": datetime.now(tz=timezone.utc).isoformat(),
        "hit": True,
        "brier_component": brier_component,
        "resolved_at": datetime.now(tz=timezone.utc).isoformat(),
    }


class TestRetrieval:
    def test_drafts_not_indexed(self):
        _seed_journal_entry("u-1", entry_id="e-draft",
                            body="text content", is_draft=True)
        rows = memory_v2.list_embeddings("u-1")
        assert rows == []

    def test_cosine_orders_topically_close_first(self):
        _seed_journal_entry("u-1", entry_id="e1",
                            body="strong buy momentum apple technicals upside")
        _seed_journal_entry("u-1", entry_id="e2",
                            body="crude oil OPEC supply quota disruption")
        results = memory_v2.retrieve_relevant("u-1", "apple momentum buy", k=2)
        assert results[0].entry_id == "e1"

    def test_outcome_predictiveness_boosts_high_brier_loser_down(self):
        # Two entries topically identical, but e1 is tied to a great
        # past call (low Brier) and e2 to a terrible one (high Brier).
        # Even though cosine is identical, e1 should outrank e2.
        _seed_journal_entry("u-1", entry_id="e-good",
                            body="overconfident on AI tickers reasoning was strong",
                            paper_order_id="po-good")
        _seed_journal_entry("u-1", entry_id="e-bad",
                            body="overconfident on AI tickers reasoning was strong",
                            paper_order_id="po-bad")
        _seed_outcome("po-good", user_id="u-1", brier_component=0.05)
        _seed_outcome("po-bad",  user_id="u-1", brier_component=0.95)

        results = memory_v2.retrieve_relevant("u-1", "AI ticker overconfidence", k=2)
        assert results[0].entry_id == "e-good"
        assert results[0].predictiveness > results[1].predictiveness

    def test_empty_query_returns_empty(self):
        _seed_journal_entry("u-1", entry_id="e1", body="content")
        assert memory_v2.retrieve_relevant("u-1", "") == []

    def test_no_entries_returns_empty(self):
        assert memory_v2.retrieve_relevant("u-1", "anything") == []

    def test_cross_model_isolation_in_retrieval(self, monkeypatch):
        """An entry indexed under a different model_id should be skipped on
        retrieval (cosine across different embedding spaces is meaningless)."""
        monkeypatch.delenv("AGENTICWHALES_EMBEDDING_MODEL", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        memory_v2.reset_default_model_cache()

        # Indexed under hashing-trick (the current default).
        _seed_journal_entry("u-1", entry_id="e-current",
                            body="current model entry content")

        # Manually inject a row tagged with a *different* model_id.
        # `retrieve_relevant` should silently skip it.
        from web import auth
        auth._memstore[("memory_embeddings", "e-other")] = {
            "entry_id": "e-other",
            "user_id": "u-1",
            "source": "journal",
            "model_id": "text-embedding-004",
            # Bytes for a fake 768-dim vector.
            "vector_bytes": memory_v2._pack_vector([0.0] * 768).hex(),
            "created_at": "2026-05-19T00:00:00+00:00",
        }

        results = memory_v2.retrieve_relevant("u-1", "content")
        # Only e-current should come back — e-other is in a different space.
        assert all(r.entry_id != "e-other" for r in results)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

class TestPublicHook:
    def test_index_journal_entry_skips_drafts(self):
        memory_v2.index_journal_entry({
            "id": "e-draft", "user_id": "u-1", "body": "x", "is_draft": True,
        })
        assert memory_v2.list_embeddings("u-1") == []

    def test_index_journal_entry_indexes_non_draft(self):
        memory_v2.index_journal_entry({
            "id": "e1", "user_id": "u-1", "body": "real entry", "is_draft": False,
        })
        rows = memory_v2.list_embeddings("u-1")
        assert len(rows) == 1
