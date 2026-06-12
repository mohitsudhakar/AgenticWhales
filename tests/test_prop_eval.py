"""Prop-firm evaluation tracker: deterministic scorekeeping, no directives."""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

import web.server as server
import web.coach_api as coach_api
from agenticwhales import coach, prop_eval, pretrade
from agenticwhales.transactions.models import Transaction
from web import auth


def _t(date, typ, sym, qty, px):
    return Transaction(date=date, type=typ, symbol=sym, quantity=qty, price=px,
                       amount=(qty * px) * (-1 if typ == "Buy" else 1))


def _trips(rows):
    txns = []
    for buy_d, sell_d, sym, qty, b, s in rows:
        txns += [_t(buy_d, "Buy", sym, qty, b), _t(sell_d, "Sell", sym, qty, s)]
    return coach.reconstruct_round_trips(txns)


def test_resolve_config_derives_dollars_from_preset():
    cfg = prop_eval.resolve_config({"preset": "ftmo_style", "account_size": 100_000,
                                    "start_date": "2026-01-01"})
    assert cfg["profit_target"] == 10_000
    assert cfg["daily_loss_limit"] == 5_000
    assert cfg["max_drawdown"] == 10_000


def test_evaluate_tracks_target_breaches_and_drawdown():
    trips = _trips([
        ("2026-01-02", "2026-01-05", "AAPL", 100, 100, 130),   # +3000
        ("2026-01-06", "2026-01-07", "MSFT", 100, 100, 75),    # -2500 (daily breach @2000)
        ("2026-01-08", "2026-01-12", "NVDA", 100, 100, 160),   # +6000 -> cum 6500
    ])
    cfg = {"preset": "custom", "account_size": 50_000, "start_date": "2026-01-01",
           "profit_target": 6_000, "daily_loss_limit": 2_000, "max_drawdown": 5_000}
    r = prop_eval.evaluate(cfg, trips, today=dt.date(2026, 1, 15))
    assert r["net_pnl"] == 6500.0
    assert r["status"] == "target_reached"
    assert len(r["daily_breaches"]) == 1
    assert r["daily_breaches"][0]["date"] == "2026-01-07"
    assert r["max_drawdown_seen"] == 2500.0 and r["drawdown_breached"] is False
    assert r["worst_day"]["pnl"] == -2500.0
    assert r["n_trading_days"] == 3


def test_evaluate_window_excludes_outside_trades():
    trips = _trips([("2025-12-01", "2025-12-05", "OLD", 10, 100, 50),   # before start
                    ("2026-01-02", "2026-01-03", "IN", 10, 100, 110)])
    r = prop_eval.evaluate({"account_size": 10_000, "start_date": "2026-01-01"},
                           trips, today=dt.date(2026, 1, 10))
    assert r["net_pnl"] == 100.0


def test_evaluate_copy_carries_no_directives():
    trips = _trips([("2026-01-02", "2026-01-05", "AAPL", 500, 100, 60)])
    r = prop_eval.evaluate({"preset": "ftmo_style", "account_size": 100_000,
                            "start_date": "2026-01-01"},
                           trips, today=dt.date(2026, 1, 10))
    def _strings(o):
        if isinstance(o, dict):
            for v in o.values(): yield from _strings(v)
        elif isinstance(o, list):
            for v in o: yield from _strings(v)
        else:
            yield str(o)
    for s in _strings(r):
        assert not pretrade.contains_directive(s), s


@pytest.fixture
def client():
    return TestClient(server.app)


def test_eval_api_roundtrip(client):
    uid = "eval-user-1"
    server.app.dependency_overrides[auth.get_current_user_id] = lambda: uid
    server.app.dependency_overrides[coach_api.optional_user_id] = lambda: uid
    try:
        assert client.get("/api/coach/eval").json()["configured"] is False
        r = client.post("/api/coach/eval", json={
            "preset": "ftmo_style", "account_size": 100000,
            "start_date": "2026-01-01"}).json()
        assert r["configured"] and r["profit_target"] == 10000
        assert client.get("/api/coach/eval").json()["configured"] is True
        assert client.post("/api/coach/eval", json={
            "preset": "nope", "account_size": 1, "start_date": "2026-01-01"}).status_code == 400
        assert client.post("/api/coach/eval", json={
            "preset": "custom", "account_size": 0, "start_date": "2026-01-01"}).status_code == 400
        assert client.delete("/api/coach/eval").json()["ok"] is True
        assert client.get("/api/coach/eval").json()["configured"] is False
    finally:
        server.app.dependency_overrides.clear()


def test_delete_coach_data_clears_eval_and_standing_brief(client):
    uid = "eval-del-1"
    auth.upsert_coach_eval(uid, {"preset": "custom", "account_size": 1000,
                                 "start_date": "2026-01-01"})
    auth.upsert_standing_brief(uid, {"tickers": ["AAPL"], "active": True})
    server.app.dependency_overrides[coach_api.optional_user_id] = lambda: uid
    try:
        client.post("/api/coach/data/delete")
        assert auth.get_coach_eval(uid) is None
        assert auth.get_standing_brief(uid) is None
    finally:
        server.app.dependency_overrides.clear()
