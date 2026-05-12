"""Broker REST endpoints: /api/broker/* and /api/sessions/{sid}/execute."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tradingagents.agents.schemas import PortfolioRating, render_pm_decision, PortfolioDecision
from tradingagents.execution.brokers import SimulatedBroker


@pytest.fixture
def client(monkeypatch):
    """FastAPI TestClient with auth forced to anonymous + broker creds cleared."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    from fastapi.testclient import TestClient
    from web.server import app
    from web.auth import get_current_user_id, ANONYMOUS_USER_ID

    app.dependency_overrides[get_current_user_id] = lambda: ANONYMOUS_USER_ID
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user_id, None)


@pytest.fixture
def fake_broker():
    broker = SimulatedBroker(starting_cash=10_000)
    broker.set_reference_price("AAPL", 100)
    return broker


def _patch_build_broker(broker):
    return patch("tradingagents.execution.build_broker", return_value=broker)


@pytest.mark.unit
def test_broker_account_returns_503_without_credentials(client):
    r = client.get("/api/broker/account")
    assert r.status_code == 503
    assert "broker unavailable" in r.json()["detail"]


@pytest.mark.unit
def test_broker_account_returns_account_when_broker_available(client, fake_broker):
    with _patch_build_broker(fake_broker):
        r = client.get("/api/broker/account")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "backtest"
    assert body["account"]["cash"] == 10_000


@pytest.mark.unit
def test_broker_positions_lists_open_positions(client, fake_broker):
    # Open a position so the endpoint has something to return
    from tradingagents.execution.schemas import OrderRequest, OrderSide, OrderType
    fake_broker.place_order(OrderRequest(
        symbol="AAPL", qty=5, side=OrderSide.BUY, order_type=OrderType.MARKET,
    ))
    with _patch_build_broker(fake_broker):
        r = client.get("/api/broker/positions")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "backtest"
    assert len(body["positions"]) == 1
    assert body["positions"][0]["symbol"] == "AAPL"
    assert body["positions"][0]["qty"] == 5


@pytest.mark.unit
def test_broker_sync_mirrors_positions_to_local_file(client, fake_broker, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tradingagents.portfolio._PATH",
        tmp_path / "portfolio.json",
    )
    from tradingagents.execution.schemas import OrderRequest, OrderSide, OrderType
    fake_broker.place_order(OrderRequest(
        symbol="AAPL", qty=7, side=OrderSide.BUY, order_type=OrderType.MARKET,
    ))
    with _patch_build_broker(fake_broker):
        r = client.post("/api/broker/sync")
    assert r.status_code == 200
    body = r.json()
    assert "AAPL" in body["synced"]
    assert body["synced"]["AAPL"]["qty"] == 7


@pytest.mark.unit
def test_execute_session_404_for_missing_session(client, fake_broker):
    with _patch_build_broker(fake_broker):
        r = client.post("/api/sessions/does-not-exist/execute", json={"dry_run": True})
    assert r.status_code == 404


@pytest.mark.unit
def test_execute_session_409_when_not_completed(client, fake_broker, monkeypatch):
    from web import storage
    session_id = "test-session-pending"
    storage.save({
        "id": session_id,
        "user_id": "anonymous",
        "ticker": "AAPL",
        "analysis_date": "2026-05-12",
        "status": "running",
    })
    with _patch_build_broker(fake_broker):
        r = client.post(f"/api/sessions/{session_id}/execute")
    assert r.status_code == 409
    assert "not completed" in r.json()["detail"]


@pytest.mark.unit
def test_execute_session_dry_run_returns_intended_trade(client, fake_broker):
    from web import storage
    decision_md = render_pm_decision(PortfolioDecision(
        rating=PortfolioRating.BUY,
        executive_summary="open 10% long",
        investment_thesis="bullish",
    ))
    session_id = "test-session-completed"
    storage.save({
        "id": session_id,
        "user_id": "anonymous",
        "ticker": "AAPL",
        "analysis_date": "2026-05-12",
        "status": "completed",
        "report_sections": {"final_trade_decision": decision_md},
    })
    with _patch_build_broker(fake_broker):
        r = client.post(f"/api/sessions/{session_id}/execute", json={"dry_run": True})
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["action"] == "DRY_RUN"
    assert body["result"]["target_qty"] == 10
    # Broker untouched
    assert fake_broker.get_position("AAPL") is None


@pytest.mark.unit
def test_execute_session_places_order_when_not_dry_run(client, fake_broker):
    from web import storage
    decision_md = render_pm_decision(PortfolioDecision(
        rating=PortfolioRating.BUY,
        executive_summary="open 10% long",
        investment_thesis="bullish",
    ))
    session_id = "test-session-live"
    storage.save({
        "id": session_id,
        "user_id": "anonymous",
        "ticker": "AAPL",
        "analysis_date": "2026-05-12",
        "status": "completed",
        "report_sections": {"final_trade_decision": decision_md},
    })
    with _patch_build_broker(fake_broker):
        r = client.post(f"/api/sessions/{session_id}/execute", json={"dry_run": False})
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["action"] == "BUY"
    assert fake_broker.get_position("AAPL").qty == 10
    # Session has the execution result attached
    refreshed = storage.load(session_id)
    assert refreshed["execution"]["action"] == "BUY"
