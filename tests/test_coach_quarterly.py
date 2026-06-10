"""Quarterly discipline summary — the share card's server-side numbers."""

from fastapi.testclient import TestClient

import web.server as server
from agenticwhales import coach


def test_quarterly_buckets_by_exit_quarter():
    q = coach.quarterly_discipline(coach.reconstruct_round_trips(coach.sample_history()))
    assert q, "sample history spans Q1/Q2 2025"
    names = [row["quarter"] for row in q]
    assert names == sorted(names)
    assert all(row["quarter"].startswith("2025-Q") for row in q)
    for row in q:
        assert {"n_trades", "pnl", "score", "quantified_leak"} <= set(row)


def test_quarterly_uses_global_median_for_consistency():
    """A quarter holding only the oversized trade must still be capped against
    the FULL-history median, not its own (which would hide the oversize)."""
    trips = coach.reconstruct_round_trips(coach.sample_history())
    q = coach.quarterly_discipline(trips)
    # Recompute one quarter by hand with the global median.
    import statistics
    global_med = statistics.median(abs(t.qty * t.entry_px) for t in trips)
    target = q[-1]
    qtrips = [t for t in trips
              if f"{int(t.exit_date[:4])}-Q{(int(t.exit_date[5:7]) - 1) // 3 + 1}"
              == target["quarter"]]
    expected = round(coach.counterfactual_disciplined(qtrips, med_notional=global_med)
                     - sum(t.pnl for t in qtrips), 2)
    assert target["quantified_leak"] == expected


def test_quarterly_in_audit_payload():
    client = TestClient(server.app)
    r = client.get("/api/coach/demo").json()
    assert r["quarterly"] and r["quarterly"][0]["quarter"].startswith("2025-Q")


def test_empty_history_no_quarters():
    assert coach.quarterly_discipline([]) == []
