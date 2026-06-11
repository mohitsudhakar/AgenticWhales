"""API tests for the defensive-overlay endpoint — TestClient, no network."""

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import web.overlay_api as overlay_api
import web.server as server
from agenticwhales import overlay


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(overlay_api, "_CACHE", {})
    monkeypatch.setattr(overlay_api, "_CACHE_AT", {})
    return TestClient(server.app)


def _closes(days: int = 300) -> pd.Series:
    idx = pd.bdate_range("2024-01-02", periods=days)
    return pd.Series(100 * np.cumprod(np.full(days, 1.0008)), index=idx)


def test_overlay_status_ok(client, monkeypatch):
    monkeypatch.setattr(
        overlay, "fetch_overlay_status",
        lambda leverage=1.0: overlay.compute_overlay(
            {"SPY": _closes(), "TLT": _closes()}, leverage=leverage))
    r = client.get("/api/overlay/status")
    assert r.status_code == 200
    j = r.json()
    assert len(j["sleeves"]) == 2
    assert j["equity_exposure_pct"] > 0
    assert "not" in j["disclaimer"].lower() and "advice" in j["disclaimer"].lower()
    assert j["evidence"]["full_period"]


def test_overlay_status_leverage_validated(client):
    r = client.get("/api/overlay/status?leverage=5")
    assert r.status_code == 422        # outside the validated 0..2 range


def test_overlay_status_unavailable_is_503(client, monkeypatch):
    def boom(leverage=1.0):
        raise RuntimeError("no data")
    monkeypatch.setattr(overlay, "fetch_overlay_status", boom)
    r = client.get("/api/overlay/status")
    assert r.status_code == 503


def test_overlay_status_cached_per_leverage(client, monkeypatch):
    calls = []

    def fake(leverage=1.0):
        calls.append(leverage)
        return overlay.compute_overlay({"SPY": _closes()}, leverage=leverage)

    monkeypatch.setattr(overlay, "fetch_overlay_status", fake)
    client.get("/api/overlay/status?leverage=1.0")
    client.get("/api/overlay/status?leverage=1.0")
    client.get("/api/overlay/status?leverage=1.5")
    assert calls == [1.0, 1.5]
