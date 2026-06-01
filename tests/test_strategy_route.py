"""Route test for POST /api/strategy/backtest — NL thesis → compile → backtest.

Patches the compiler (no LLM) and the OHLCV loader (no network) so the
endpoint's wiring is exercised deterministically.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from web import auth, server


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(auth, "_db_writable", lambda: False)
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


@pytest.fixture
def client():
    c = TestClient(server.app)
    c.follow_redirects = False
    return c


def _synthetic(symbol, start, end):
    idx = pd.date_range(start="2023-01-01", periods=160, freq="D")
    base = 100 + np.cumsum(np.random.default_rng(1).normal(0, 1, 160))
    return pd.DataFrame(
        {"Open": base, "High": base + 1, "Low": base - 1, "Close": base,
         "Volume": [1_000_000] * 160},
        index=idx,
    )


def test_strategy_backtest_compiles_and_runs(client, monkeypatch):
    import agenticwhales.backtest as bt
    import agenticwhales.strategy as strat

    monkeypatch.setattr(bt, "_load_history", _synthetic)

    spec = strat.StrategySpec(
        name="MA reclaim", direction="long",
        entry_raw={"kind": "indicator_cross", "fast": "close", "slow": "sma_20", "direction": "above"},
        stop_loss_pct=0.05, hold_days=15, rationale="reclaim", source_text="reclaim ma",
    )
    monkeypatch.setattr(strat, "compile_strategy", lambda *a, **k: spec)

    r = client.post("/api/strategy/backtest", json={
        "thesis": "go long when it reclaims the 20-day average",
        "ticker": "AAPL",
        "from_date": "2023-02-01",
        "to_date": "2023-06-01",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["strategy"]["name"] == "MA reclaim"
    assert body["strategy"]["direction"] == "long"
    bt_res = body["backtest"]
    assert bt_res["symbol"] == "AAPL"
    assert "equity_curve" in bt_res
    assert "total_return_pct" in bt_res


def test_strategy_backtest_compile_failure_is_422(client, monkeypatch):
    import agenticwhales.strategy as strat

    def _boom(*a, **k):
        raise strat.StrategyError("could not parse thesis")

    monkeypatch.setattr(strat, "compile_strategy", _boom)
    r = client.post("/api/strategy/backtest", json={
        "thesis": "gibberish", "ticker": "AAPL",
        "from_date": "2023-02-01", "to_date": "2023-06-01",
    })
    assert r.status_code == 422


def test_backtest_run_bad_data_is_422(client, monkeypatch):
    import agenticwhales.backtest as bt

    def _empty(symbol, start, end):
        raise ValueError("no OHLCV data")

    monkeypatch.setattr(bt, "_load_history", _empty)
    r = client.post("/api/backtest/run", json={
        "ticker": "ZZZZ", "from_date": "2023-02-01", "to_date": "2023-06-01",
    })
    assert r.status_code == 422
