"""Targeted branch coverage: memory_v2 retrieval scoring loop, calibration
fit, and the DB-writable / error branches of behavioral + outcomes readers.
All offline (hashing-trick embeddings; auth monkeypatched where DB-paths run)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def _wipe():
    from web import auth
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


# ===========================================================================
# memory_v2.retrieve_relevant — scoring loop
# ===========================================================================

def test_retrieve_relevant_scores_and_joins():
    from web import auth
    from agenticwhales import memory_v2 as m

    monkeypatch_model = m._default_embedding_model()
    vec = m.embed("AAPL momentum thesis", model=monkeypatch_model)
    auth._memstore[("memory_embeddings", "e1")] = {
        "entry_id": "j1", "user_id": "u1", "source": "journal",
        "model_id": None, "vector_bytes": m._pack_vector(vec),  # None → skip space check
    }
    auth.save_journal_entry({
        "id": "j1", "user_id": "u1", "session_id": None, "paper_order_id": None,
        "thesis_id": None, "kind": "reflection", "body": "Strong momentum lesson.",
        "sentiment_score": None, "is_draft": False,
        "created_at": "2024-01-02T00:00:00+00:00", "updated_at": "2024-01-02T00:00:00+00:00",
    })
    out = m.retrieve_relevant("u1", "AAPL momentum thesis", k=3, source_filter="journal")
    assert out and out[0].body == "Strong momentum lesson."
    assert out[0].cosine > 0


def test_retrieve_relevant_empty_query_and_no_rows():
    from agenticwhales import memory_v2 as m
    assert m.retrieve_relevant("u1", "") == []
    assert m.retrieve_relevant("nobody", "query") == []


# ===========================================================================
# calibration.fit_for_user
# ===========================================================================

def test_fit_for_user_below_gate_returns_none():
    from agenticwhales import calibration as cal
    assert cal.fit_for_user("u1") is None  # no outcomes


def test_fit_for_user_fits_and_persists():
    from web import auth
    from agenticwhales import calibration as cal
    # Seed >= UNLOCK_N outcomes with varied prob/hit.
    for i in range(cal.UNLOCK_N + 2):
        auth._memstore[("decision_outcomes", f"o{i}")] = {
            "paper_order_id": f"o{i}", "user_id": "u1",
            "predicted_prob_of_profit": 0.4 + (i % 5) * 0.1,
            "hit": (i % 2 == 0),
        }
    fit = cal.fit_for_user("u1")
    assert fit is not None and fit.n_samples >= cal.UNLOCK_N
    # persisted → _all_fits sees it
    assert cal._all_fits("u1")


# ===========================================================================
# behavioral DB-writable + error branches
# ===========================================================================

def test_list_recent_findings_db_branch(monkeypatch):
    from agenticwhales import behavioral as bh
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    monkeypatch.setattr("web.auth._db_writable", lambda: True)
    monkeypatch.setattr("web.auth._select_columns",
                        lambda *a, **k: [{"id": "f1", "user_id": "u1",
                                          "pattern": "tilt", "created_at": now_iso}])
    rows = bh.list_recent_findings("u1")
    assert len(rows) == 1 and rows[0]["pattern"] == "tilt"


def test_update_finding_state_swallows_upsert_error(monkeypatch):
    from web import auth
    from agenticwhales import behavioral as bh
    pk = "u1|tilt|x"
    auth._memstore[("behavioral_findings", pk)] = {
        "id": pk, "user_id": "u1", "pattern": "tilt"}
    monkeypatch.setattr(auth, "_upsert_columns",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    # still returns True even though the upsert raised
    assert bh.update_finding_state("u1", pk, dismissed=True) is True
    assert auth._memstore[("behavioral_findings", pk)]["dismissed"] is True


# ===========================================================================
# outcomes DB-writable readers + parse exception
# ===========================================================================

def test_list_outcomes_for_user_db_branch(monkeypatch):
    from agenticwhales import outcomes as oc
    monkeypatch.setattr("web.auth._db_writable", lambda: True)
    monkeypatch.setattr("web.auth._select_columns",
                        lambda *a, **k: [{"paper_order_id": "o1", "user_id": "u1",
                                          "resolved_at": "2024-01-02"}])
    rows = oc.list_outcomes_for_user("u1")
    assert len(rows) == 1 and rows[0]["paper_order_id"] == "o1"


def test_parse_snapshot_close_malformed_line():
    from agenticwhales import outcomes as oc
    # "Latest close" line present but no parseable number → None
    assert oc._parse_snapshot_close("Latest close: not-a-number here") is None
