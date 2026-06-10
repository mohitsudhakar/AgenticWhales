"""Findings persistence + forward validation — the seam that keeps the coach's
central claim ("this bias cost you $X") falsifiable. Memstore mode, no network."""

import pytest
from fastapi.testclient import TestClient

import web.auth as auth
import web.coach_api as coach_api
import web.server as server
from agenticwhales import coach
from agenticwhales.coach import RoundTrip


@pytest.fixture
def client():
    return TestClient(server.app)


# --------------------------------------------------------------------------- #
# Unit: stable leak keys + the forward resolver
# --------------------------------------------------------------------------- #

def test_leak_key_is_stable_and_copy_independent():
    assert coach.leak_key("Disposition effect (holding losers, cutting winners)") == "disposition_effect"
    assert coach.leak_key("Overtrading / cost drag") == "overtrading_cost_drag"
    assert coach.leak_key("Revenge trading (sizing up after losses)") == "revenge_trading"
    # Reworded copy with the same kind prefix keeps the key.
    assert coach.leak_key("Disposition effect (new wording)") == "disposition_effect"


def _trip(sym, entry, exit_, pnl, *, qty=100, entry_px=50.0, hold=7):
    exit_px = entry_px + pnl / qty
    return RoundTrip(symbol=sym, entry_date=entry, exit_date=exit_, qty=qty,
                     entry_px=entry_px, exit_px=exit_px, pnl=pnl, hold_days=hold,
                     return_pct=pnl / (qty * entry_px) * 100)


def _disciplined_forward():
    """Six symmetric, same-size, same-hold trades after the window: no leak fires."""
    return [
        _trip("NEWA", "2025-05-01", "2025-05-08", 300),
        _trip("NEWB", "2025-05-12", "2025-05-19", 300),
        _trip("NEWC", "2025-05-22", "2025-05-29", 300),
        _trip("NEWD", "2025-06-02", "2025-06-09", -300),
        _trip("NEWE", "2025-06-11", "2025-06-18", -300),
        _trip("NEWF", "2025-06-20", "2025-06-27", -300),
    ]


def _leaky_forward():
    """Forward trades that repeat the inverted-payoff/disposition pattern:
    small fast wins, big slow losses."""
    return [
        _trip("OLDA", "2025-05-01", "2025-05-02", 100, hold=1),
        _trip("OLDB", "2025-05-05", "2025-05-06", 100, hold=1),
        _trip("OLDC", "2025-05-08", "2025-06-20", -900, hold=43),
        _trip("OLDD", "2025-05-12", "2025-06-25", -900, hold=44),
        _trip("OLDE", "2025-05-15", "2025-06-30", -900, hold=46),
    ]


def test_resolver_waits_for_enough_forward_trades():
    finding = {"leak_key": "disposition_effect", "window_end": "2025-04-15"}
    assert coach.resolve_finding_forward(finding, _disciplined_forward()[:3]) is None


def test_resolver_marks_fixed_leak_not_persisted():
    finding = {"leak_key": "disposition_effect", "window_end": "2025-04-15"}
    res = coach.resolve_finding_forward(finding, _disciplined_forward())
    assert res == {"persisted": False, "forward_dollars": 0.0, "n_forward_trades": 6}


def test_resolver_marks_repeated_leak_persisted_with_dollars():
    finding = {"leak_key": "disposition_effect", "window_end": "2025-04-15"}
    res = coach.resolve_finding_forward(finding, _leaky_forward())
    assert res is not None and res["persisted"] is True
    assert res["forward_dollars"] > 0


def test_resolver_skips_price_path_leaks():
    finding = {"leak_key": "no_stop_loss", "window_end": "2025-04-15"}
    assert coach.resolve_finding_forward(finding, _disciplined_forward()) is None


def test_resolver_only_looks_forward():
    """Trades inside the original window must not count toward resolution."""
    finding = {"leak_key": "disposition_effect", "window_end": "2025-06-30"}
    assert coach.resolve_finding_forward(finding, _leaky_forward()) is None


# --------------------------------------------------------------------------- #
# API flow: persist on audit -> resolve on the next audit -> reopen if leaking
# --------------------------------------------------------------------------- #

def _txns_for(trips):
    out = []
    for t in trips:
        out.append({"date": t.entry_date, "type": "Buy", "symbol": t.symbol,
                    "quantity": t.qty, "price": t.entry_px,
                    "amount": -t.qty * t.entry_px})
        out.append({"date": t.exit_date, "type": "Sell", "symbol": t.symbol,
                    "quantity": t.qty, "price": t.exit_px,
                    "amount": t.qty * t.exit_px})
    return out


def test_findings_lifecycle_via_api(client):
    server.app.dependency_overrides[coach_api.optional_user_id] = lambda: "findings-user-1"
    try:
        # Audit 1: the sample trader's leaks are persisted as OPEN findings.
        assert client.post("/api/coach/audit", json={"use_demo": True}).status_code == 200
        f = client.get("/api/coach/findings").json()
        assert f["signed_in"] is True
        open1 = [x for x in f["findings"] if not x.get("resolved_at")]
        assert open1, "first audit should open findings"
        assert all(x["leak_key"] and x["fix"] and x["window_end"] for x in open1)
        keys1 = {x["leak_key"] for x in open1}
        assert "disposition_effect" in keys1

        # Same audit again: no duplicate open rows per leak kind.
        client.post("/api/coach/audit", json={"use_demo": True})
        f2 = client.get("/api/coach/findings").json()["findings"]
        open2 = [x for x in f2 if not x.get("resolved_at")]
        assert len(open2) == len({x["leak_key"] for x in open2})

        # Audit 2 with six disciplined trades AFTER the window: prior findings
        # resolve as fixed (persisted=False) on the forward slice...
        r = client.post("/api/coach/audit",
                        json={"transactions": _txns_for(_disciplined_forward())})
        assert r.status_code == 200
        f3 = client.get("/api/coach/findings").json()["findings"]
        resolved = [x for x in f3 if x.get("resolved_at")]
        assert resolved, "forward trades should resolve open findings"
        resolvable = [x for x in resolved
                      if x["leak_key"] not in ("no_stop_loss", "cutting_winners_early")]
        assert all(x["persisted"] is False for x in resolvable)
        assert all((x.get("n_forward_trades") or 0) >= 5 for x in resolvable)

        # ...and the full-history audit reopens still-detected leaks, building
        # the longitudinal chain.
        reopened = [x for x in f3 if not x.get("resolved_at")]
        assert reopened
    finally:
        server.app.dependency_overrides.clear()


def test_activation_event_fires_once(client, monkeypatch):
    events = []
    monkeypatch.setattr(coach_api.audit_mod, "audit",
                        lambda actor, action, **kw: events.append((actor, action)))
    server.app.dependency_overrides[coach_api.optional_user_id] = lambda: "activation-user-1"
    try:
        client.post("/api/coach/audit", json={"use_demo": True})
        client.post("/api/coach/audit", json={"use_demo": True})
        activations = [e for e in events if e[1] == "coach_activation"]
        assert activations == [("activation-user-1", "coach_activation")]
    finally:
        server.app.dependency_overrides.clear()


def test_admin_coach_stats_counts_funnel(client):
    server.app.dependency_overrides[coach_api.optional_user_id] = lambda: "stats-user-1"
    try:
        client.post("/api/coach/audit", json={"use_demo": True})
        stats = auth.admin_coach_stats()
        assert stats["activated_users"] >= 1
        assert stats["total_audits"] >= 1
        assert stats["open_findings"] >= 1
    finally:
        server.app.dependency_overrides.clear()


def test_delete_data_removes_findings(client):
    server.app.dependency_overrides[coach_api.optional_user_id] = lambda: "findings-del-1"
    try:
        client.post("/api/coach/audit", json={"use_demo": True})
        assert client.get("/api/coach/findings").json()["findings"]
        d = client.post("/api/coach/data/delete").json()
        assert d["ok"] and d["findings"] >= 1
        assert client.get("/api/coach/findings").json()["findings"] == []
    finally:
        server.app.dependency_overrides.clear()
