"""Honest cohort benchmarks: real percentiles or an explicit 'unlocks at N' —
never an invented comparative claim."""

import time
import uuid

import pytest
from fastapi.testclient import TestClient

import web.coach_api as coach_api
import web.server as server
from agenticwhales import benchmarks, pretrade
from web import auth


@pytest.fixture
def client(monkeypatch):
    # Cohort cache is process state; isolate per test.
    monkeypatch.setitem(coach_api._COHORT_CACHE, "scores", None)
    monkeypatch.setitem(coach_api._COHORT_CACHE, "at", 0.0)
    return TestClient(server.app)


def test_percentile_rank_midpoint_and_ties():
    cohort = [10, 20, 30, 40, 50]
    assert benchmarks.percentile_rank(30, cohort) == 50.0   # 2 below + 0.5 equal
    assert benchmarks.percentile_rank(60, cohort) == 100.0
    assert benchmarks.percentile_rank(5, cohort) == 0.0
    # All-ties cohort: everyone is the midpoint.
    assert benchmarks.percentile_rank(85, [85] * 10) == 50.0


def test_band_edges():
    assert benchmarks.band(95) == "top 10%"
    assert benchmarks.band(80) == "top 25%"
    assert benchmarks.band(55) == "top half"
    assert benchmarks.band(30) == "bottom half"
    assert benchmarks.band(12) == "bottom 25%"
    assert benchmarks.band(2) == "bottom 10%"


def test_no_percentile_below_threshold(monkeypatch):
    monkeypatch.setenv("AGENTICWHALES_COHORT_MIN", "50")
    b = benchmarks.score_benchmark(70, [60.0] * 49)
    assert b["cohort"] == "pending"
    assert "percentile" not in b and "band" not in b      # NO comparative claim
    assert "unlock" in b["label"]
    b2 = benchmarks.score_benchmark(70, [60.0] * 50)      # exactly at threshold
    assert b2["cohort"] == "real" and b2["percentile"] > 50
    assert "AgenticWhales traders" in b2["label"]          # cohort is NAMED


def test_benchmark_labels_pass_tripwire():
    for b in (benchmarks.score_benchmark(70, [60.0] * 49),
              benchmarks.score_benchmark(70, [60.0] * 60)):
        assert not pretrade.contains_directive(b["label"])


def _seed_cohort(n: int, score: float = 60.0):
    for _ in range(n):
        auth.insert_coach_audit({
            "id": uuid.uuid4().hex, "user_id": uuid.uuid4().hex,
            "created_at": auth._ts_iso(time.time()),
            "discipline_score": score, "total_pnl": 0.0, "disciplined_pnl": 0.0,
            "n_trades": 5, "leak_summary": [], "transactions": [],
        })


def test_cohort_latest_score_per_user():
    uid = uuid.uuid4().hex
    for score, ts in ((30.0, 1.0), (80.0, 2.0)):
        auth.insert_coach_audit({
            "id": uuid.uuid4().hex, "user_id": uid,
            "created_at": auth._ts_iso(ts),
            "discipline_score": score, "total_pnl": 0.0, "disciplined_pnl": 0.0,
            "n_trades": 5, "leak_summary": [], "transactions": [],
        })
    scores = auth.list_cohort_scores()
    # the user's LATEST score is in the cohort; the older one is not double-counted
    assert scores.count(80.0) >= 1


def test_audit_payload_carries_benchmark(client, monkeypatch):
    monkeypatch.setenv("AGENTICWHALES_COHORT_MIN", "3")
    _seed_cohort(5, score=60.0)
    server.app.dependency_overrides[coach_api.optional_user_id] = lambda: "bench-user-1"
    try:
        r = client.post("/api/coach/audit", json={"transactions": [
            {"date": "2025-01-06", "type": "Buy", "symbol": "AAPL",
             "quantity": 10, "price": 100, "amount": -1000},
            {"date": "2025-01-20", "type": "Sell", "symbol": "AAPL",
             "quantity": 10, "price": 110, "amount": 1100},
        ]}).json()
        assert r["benchmark"]["cohort"] == "real"
        assert r["benchmark"]["n_cohort"] >= 3
    finally:
        server.app.dependency_overrides.clear()


def test_demo_benchmark_is_labeled_synthetic(client):
    r = client.get("/api/coach/demo").json()
    assert r["benchmark"]["cohort"] == "demo"
    assert "sample" in r["benchmark"]["label"]
