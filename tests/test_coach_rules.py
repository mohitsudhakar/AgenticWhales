"""Rule book engine: template seeding, violation detection (idempotent),
streaks, IDOR guards, pre-trade personalization, compliant copy."""

import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

import web.coach_api as coach_api
import web.server as server
from agenticwhales import coach, coach_rules, pretrade
from agenticwhales.coach import RoundTrip
from web import auth

# Causal phrasing the compliance review banned from violation/digest copy.
CAUSAL_RE = re.compile(r"cost you|would have saved|lost because", re.IGNORECASE)


@pytest.fixture
def client():
    return TestClient(server.app)


def _trip(sym, entry, exit_, pnl, *, qty=100, entry_px=50.0, hold=5):
    return RoundTrip(symbol=sym, entry_date=entry, exit_date=exit_, qty=qty,
                     entry_px=entry_px, exit_px=entry_px + pnl / qty, pnl=pnl,
                     hold_days=hold, return_pct=pnl / (qty * entry_px) * 100)


# --------------------------------------------------------------------------- #
# Seeding + params
# --------------------------------------------------------------------------- #

def test_suggest_rules_seeds_per_leak_kind_no_dupes():
    findings = [
        {"id": "f1", "user_id": "u1", "leak_key": "revenge_trading"},
        {"id": "f2", "user_id": "u1", "leak_key": "oversizing_tail_risk"},
        {"id": "f3", "user_id": "u1", "leak_key": "revenge_trading"},  # dup kind
    ]
    rules = coach_rules.suggest_rules(findings, existing_rules=[])
    kinds = sorted(r["rule_kind"] for r in rules)
    assert kinds == ["cooldown_after_loss", "max_risk_pct", "size_cap_x_median"]
    assert all(r["status"] == "suggested" for r in rules)
    # Re-suggest with these existing -> nothing new (deterministic ids too).
    again = coach_rules.suggest_rules(findings, existing_rules=rules)
    assert again == []


def test_clamp_params_bounds():
    p = coach_rules.clamp_params("cooldown_after_loss", {"cooldown_hours": 9999})
    assert p["cooldown_hours"] == 168
    p = coach_rules.clamp_params("size_cap_x_median", {"cap_x": 0.1, "junk": 5})
    assert p["cap_x"] == 1.0 and "junk" not in p
    assert coach_rules.clamp_params("nonexistent", {"x": 1}) == {}


def test_rule_copy_passes_directive_tripwire():
    for templates in coach_rules.RULE_TEMPLATES.values():
        for t in templates:
            for s in (t["label"], t["description"]):
                assert not pretrade.contains_directive(s), s


# --------------------------------------------------------------------------- #
# Violation detection
# --------------------------------------------------------------------------- #

def _rule(kind, params, *, adopted="2025-01-01", leak="revenge_trading", rid="r1"):
    return {"id": rid, "user_id": "u1", "leak_key": leak, "rule_kind": kind,
            "params": params, "status": "active", "checkable_from_fills": True,
            "adopted_at": adopted}


def test_cooldown_violation_detected_and_gated_on_adoption():
    trips = [
        _trip("AAA", "2025-02-01", "2025-02-10", -500),       # the loss
        _trip("BBB", "2025-02-11", "2025-02-20", 200),        # next-day re-entry
    ]
    rule = _rule("cooldown_after_loss", {"cooldown_hours": 48})
    vs = coach_rules.detect_violations([rule], trips, user_id="u1")
    assert len(vs) == 1 and vs[0].symbol == "BBB"
    assert "date-only" in vs[0].evidence            # honesty about granularity
    assert not CAUSAL_RE.search(vs[0].evidence)
    assert not pretrade.contains_directive(vs[0].evidence)
    # Same trades, rule adopted AFTER the entry -> no violation.
    late = _rule("cooldown_after_loss", {"cooldown_hours": 48}, adopted="2025-03-01")
    assert coach_rules.detect_violations([late], trips, user_id="u1") == []


def test_cooldown_after_win_is_not_a_violation():
    trips = [
        _trip("AAA", "2025-02-01", "2025-02-10", 500),        # a WIN
        _trip("BBB", "2025-02-11", "2025-02-20", 200),
    ]
    rule = _rule("cooldown_after_loss", {"cooldown_hours": 48})
    assert coach_rules.detect_violations([rule], trips, user_id="u1") == []


def test_size_cap_violation_with_positive_pnl_stays_neutral():
    trips = [_trip(f"S{i}", f"2025-01-{i+1:02d}", f"2025-01-{i+2:02d}", 50)
             for i in range(5)]
    trips.append(_trip("BIG", "2025-02-01", "2025-02-05", 900, qty=1000))  # 10x, WON
    rule = _rule("size_cap_x_median", {"cap_x": 2.0}, leak="oversizing_tail_risk")
    vs = coach_rules.detect_violations([rule], trips, user_id="u1")
    assert len(vs) == 1 and vs[0].dollars == 900.0   # positive P&L, honestly
    assert "realized $" in vs[0].evidence
    assert not CAUSAL_RE.search(vs[0].evidence)


def test_violation_ids_are_idempotent():
    trips = [
        _trip("AAA", "2025-02-01", "2025-02-10", -500),
        _trip("BBB", "2025-02-11", "2025-02-20", 200),
    ]
    rule = _rule("cooldown_after_loss", {"cooldown_hours": 48})
    a = coach_rules.detect_violations([rule], trips, user_id="u1")
    b = coach_rules.detect_violations([rule], list(trips), user_id="u1")
    assert [v.id for v in a] == [v.id for v in b]


def test_weekly_budget_violation():
    trips = [_trip(f"W{i}", "2025-03-03", "2025-03-07", 10) for i in range(4)]
    rule = _rule("max_trades_per_week", {"max_trades": 2}, leak="overtrading_cost_drag")
    vs = coach_rules.detect_violations([rule], trips, user_id="u1")
    assert len(vs) == 1
    assert "2 beyond" in vs[0].evidence and vs[0].dollars == 20.0


def test_streak_counts_weeks_without_violation():
    today = dt.date(2025, 6, 16)
    rules = [_rule("cooldown_after_loss", {}, adopted="2025-05-19")]
    assert coach_rules.compute_streak(rules, [], today=today)["weeks"] >= 4
    events = [{"occurred_on": "2025-06-16"}]   # violation this week
    assert coach_rules.compute_streak(rules, events, today=today)["weeks"] == 0
    assert coach_rules.compute_streak([], [], today=today)["weeks"] == 0


# --------------------------------------------------------------------------- #
# API: seeding, adoption, IDOR, the audit hook, deletion
# --------------------------------------------------------------------------- #

def _signin(uid):
    server.app.dependency_overrides[coach_api.optional_user_id] = lambda: uid


def _txns_for(trips):
    out = []
    for t in trips:
        out.append({"date": t.entry_date, "type": "Buy", "symbol": t.symbol,
                    "quantity": t.qty, "price": t.entry_px, "amount": -t.qty * t.entry_px})
        out.append({"date": t.exit_date, "type": "Sell", "symbol": t.symbol,
                    "quantity": t.qty, "price": t.exit_px, "amount": t.qty * t.exit_px})
    return out


def test_rules_api_lifecycle_and_violation_hook(client):
    _signin("rules-user-1")
    try:
        # Seed findings (demo trader has revenge/oversizing/disposition leaks).
        client.post("/api/coach/audit", json={"use_demo": True})
        rules = client.get("/api/coach/rules").json()["rules"]
        assert rules and all(r["status"] == "suggested" for r in rules)
        size_cap = next(r for r in rules if r["rule_kind"] == "size_cap_x_median")

        # Adopt + tune it (params clamped server-side).
        r = client.post(f"/api/coach/rules/{size_cap['id']}",
                        json={"status": "active", "params": {"cap_x": 99}})
        assert r.status_code == 200
        rule = r.json()["rule"]
        assert rule["status"] == "active" and rule["adopted_at"]
        assert rule["params"]["cap_x"] == 5.0                   # clamped
        client.post(f"/api/coach/rules/{size_cap['id']}",
                    json={"params": {"cap_x": 2.0}})

        # Backdate adoption so historical demo trades are in scope, then
        # re-audit: the hook should mint violations exactly once.
        auth.update_coach_rule(size_cap["id"], {"adopted_at": "2025-01-01"})
        client.post("/api/coach/audit", json={"use_demo": True})
        events = auth.list_coach_rule_events("rules-user-1")
        assert events, "demo trader's MSTR position should violate the size cap"
        n = len(events)
        client.post("/api/coach/audit", json={"use_demo": True})   # re-audit
        assert len(auth.list_coach_rule_events("rules-user-1")) == n  # idempotent
        assert all(not CAUSAL_RE.search(e["evidence"]) for e in events)

        # Violations land in the durable audit log too.
        assert auth.list_audit(actor="rules-user-1", action="coach_rule_violation",
                               limit=10)
    finally:
        server.app.dependency_overrides.clear()


def test_rule_update_idor_guard(client):
    _signin("rules-owner")
    try:
        client.post("/api/coach/audit", json={"use_demo": True})
        rid = client.get("/api/coach/rules").json()["rules"][0]["id"]
    finally:
        server.app.dependency_overrides.clear()
    _signin("rules-attacker")
    try:
        r = client.post(f"/api/coach/rules/{rid}", json={"status": "active"})
        assert r.status_code == 404      # not 403: no existence oracle
    finally:
        server.app.dependency_overrides.clear()


def test_guest_rules_empty(client):
    j = client.get("/api/coach/rules").json()
    assert j == {"signed_in": False, "rules": []}


def test_pretrade_personalized_by_rulebook(client):
    _signin("rules-user-2")
    try:
        client.post("/api/coach/audit", json={"use_demo": True})
        rules = client.get("/api/coach/rules").json()["rules"]
        stop_rule = next(r for r in rules if r["rule_kind"] == "stop_required")
        client.post(f"/api/coach/rules/{stop_rule['id']}", json={"status": "active"})

        r = client.post("/api/pretrade/check", json={
            "trade": {"symbol": "NVDA", "side": "long", "qty": 10,
                      "entry_price": 100},   # no stop set
            "equity": 100000, "include_ai": False})
        v = r.json()
        stop_check = next(c for c in v["checks"] if c["name"] == "Stop-loss")
        assert stop_check["status"] == "fail"          # escalated by the rule
        assert v["verdict"] == "BLOCK"
        assert any(a["rule_kind"] == "stop_required" for a in v["rules_applied"])
    finally:
        server.app.dependency_overrides.clear()


def test_pretrade_cooldown_rule_uses_injectable_today():
    trips = [_trip("AAA", "2025-02-01", "2025-02-10", -500)]
    v = pretrade.check_trade(
        pretrade.ProposedTrade("BBB", "long", qty=10, entry_price=100, stop_price=98),
        equity=100000, recent_trades=trips, cooldown_hours=48,
        today=dt.date(2025, 2, 11))
    assert any(c.name == "Cooldown rule" and c.status == "fail" for c in v.checks)
    # Outside the window -> no cooldown check fires.
    v2 = pretrade.check_trade(
        pretrade.ProposedTrade("BBB", "long", qty=10, entry_price=100, stop_price=98),
        equity=100000, recent_trades=trips, cooldown_hours=48,
        today=dt.date(2025, 2, 20))
    assert not any(c.name == "Cooldown rule" for c in v2.checks)


def test_rules_summary_endpoint(client):
    assert client.get("/api/coach/rules/summary").json() == {"signed_in": False}
    _signin("rules-user-3")
    try:
        client.post("/api/coach/audit", json={"use_demo": True})
        client.get("/api/coach/rules")          # seed suggestions
        s = client.get("/api/coach/rules/summary").json()
        assert s["signed_in"] and s["rules"]
        assert all("violations_30d" in r for r in s["rules"])
        assert "streak" in s and "forward_validation" in s and "recent_violations" in s
    finally:
        server.app.dependency_overrides.clear()


def test_delete_coach_data_covers_rules_and_events(client):
    _signin("rules-del-1")
    try:
        client.post("/api/coach/audit", json={"use_demo": True})
        client.get("/api/coach/rules")
        assert auth.list_coach_rules("rules-del-1")
        d = client.post("/api/coach/data/delete").json()
        assert d["ok"] and d["rules"] >= 1
        assert auth.list_coach_rules("rules-del-1") == []
        assert auth.list_coach_rule_events("rules-del-1") == []
    finally:
        server.app.dependency_overrides.clear()


def test_coach_data_tables_cover_all_coach_migrations():
    """delete_coach_data must know about every coach_*/referral table shipped
    in docs/migrations — a new table that isn't deletable is a privacy bug."""
    import pathlib
    import re as _re
    sql = " ".join(p.read_text() for p in
                   pathlib.Path("docs/migrations").glob("*.sql"))
    created = set(_re.findall(r"create table if not exists public\.((?:coach|referral)\w*)", sql))
    assert created <= set(auth.COACH_DATA_TABLES), (
        f"tables missing from delete_coach_data: {created - set(auth.COACH_DATA_TABLES)}")