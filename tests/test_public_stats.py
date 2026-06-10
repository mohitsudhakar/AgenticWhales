"""Public stats: tiered k-anonymity enforced server-side, early mode, caching."""

import time
import uuid

import pytest
from fastapi.testclient import TestClient

import web.coach_api as coach_api
import web.server as server
from web import auth


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(coach_api._STATS_CACHE, "data", None)
    monkeypatch.setitem(coach_api._STATS_CACHE, "at", 0.0)
    return TestClient(server.app)


def _seed_user(score=50.0, leaks=None, dollars=100.0):
    auth.insert_coach_audit({
        "id": uuid.uuid4().hex, "user_id": uuid.uuid4().hex,
        "created_at": auth._ts_iso(time.time()),
        "discipline_score": score, "total_pnl": 0.0, "disciplined_pnl": 0.0,
        "n_trades": 5,
        "leak_summary": [{"name": n, "dollars": dollars} for n in (leaks or [])],
        "transactions": [],
    })


def test_round_2sig():
    assert coach_api._round_2sig(0) == 0.0
    assert coach_api._round_2sig(123456) == 120000
    assert coach_api._round_2sig(87.6) == 88.0


def test_early_mode_below_threshold(monkeypatch):
    monkeypatch.setenv("AGENTICWHALES_PUBLIC_STATS_MIN_USERS", "100000")
    s = coach_api._public_stats_compute()
    assert s["mode"] == "early" and s["unlock_at"] == 100000
    assert "median_score" not in s and "leaks" not in s


def test_bucket_and_dollar_suppression(monkeypatch):
    monkeypatch.setenv("AGENTICWHALES_PUBLIC_STATS_MIN_USERS", "10")
    monkeypatch.setenv("AGENTICWHALES_PUBLIC_STATS_MIN_USERS_DOLLARS", "25")
    # 12 users with disposition effect (>=10: pct visible, <25: dollars hidden);
    # 3 users with revenge trading (<10: bucket suppressed entirely).
    for _ in range(12):
        _seed_user(leaks=["Disposition effect (holding losers, cutting winners)"])
    for _ in range(3):
        _seed_user(leaks=["Revenge trading (sizing up after losses)"])
    s = coach_api._public_stats_compute()
    assert s["mode"] == "live"
    keys = {l["leak"] for l in s["leaks"]}
    assert "revenge_trading" not in keys                 # bucket suppressed
    dispo = next(l for l in s["leaks"] if l["leak"] == "disposition_effect")
    assert "pct_of_traders" in dispo
    assert "historical_dollars" not in dispo             # < 25 users: no dollars


def test_dollars_unlock_at_higher_threshold(monkeypatch):
    monkeypatch.setenv("AGENTICWHALES_PUBLIC_STATS_MIN_USERS", "5")
    monkeypatch.setenv("AGENTICWHALES_PUBLIC_STATS_MIN_USERS_DOLLARS", "8")
    for _ in range(9):
        _seed_user(leaks=["Overtrading / cost drag"], dollars=111.11)
    s = coach_api._public_stats_compute()
    row = next(l for l in s["leaks"] if l["leak"] == "overtrading_cost_drag")
    assert "historical_dollars" in row
    # 2-significant-figure rounding, never exact sums.
    assert row["historical_dollars"] == coach_api._round_2sig(row["historical_dollars"])


def test_endpoint_caches(client, monkeypatch):
    calls = []
    real = coach_api._public_stats_compute

    def counting():
        calls.append(1)
        return real()
    monkeypatch.setattr(coach_api, "_public_stats_compute", counting)
    client.get("/api/coach/public-stats")
    client.get("/api/coach/public-stats")
    assert len(calls) == 1


def test_stats_page_serves(client):
    r = client.get("/stats")
    assert r.status_code == 200
    assert "State of Retail Discipline" in r.text
