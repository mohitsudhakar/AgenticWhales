"""HTTP-level smoke tests against the FastAPI TestClient.

Covers the routing changes, Phase 3 endpoints, and the save-as-recurring
flow that the hero on /fund uses. Runs against the in-memory fallback so
no Supabase / Alpaca / yfinance is required.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from web import auth, server


@pytest.fixture
def client():
    # Default to follow_redirects=False so we can inspect 307s explicitly.
    c = TestClient(server.app)
    c.follow_redirects = False
    return c


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


class TestRouting:
    def test_root_serves_landing_page(self, client):
        """`/` is the landing / sign-in gate; once authed the page forwards
        to /fund via client-side JS. We just verify it returns 200 HTML."""
        r = client.get("/")
        assert r.status_code == 200
        # Landing page should reference /fund somewhere in its body or script.
        assert "/fund" in r.text or "landing" in r.text.lower()

    def test_fund_page_serves(self, client):
        r = client.get("/fund")
        assert r.status_code == 200
        assert "Agentic Whales · Fund" in r.text or 'data-section="overview"' in r.text

    def test_analyze_page_serves_legacy_bundle(self, client):
        r = client.get("/analyze")
        assert r.status_code == 200
        # The legacy bundle has the "Let's go" copy on its Go button.
        assert "Let's go" in r.text or "f-ticker" in r.text


class TestHealthz:
    def test_healthz_alive(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json().get("status") in {"ok", "alive"}


class TestBacktestEndpoint:
    def test_runs_against_synthetic_history(self, client, monkeypatch):
        # Replace yfinance fetcher with a pre-built DataFrame so the test
        # doesn't hit the network. We patch the underlying engine's
        # `_load_history`.
        import datetime as _dt
        import pandas as pd
        import random

        def _build_df(start, days):
            rng = random.Random(1)
            rows = []
            price = 100.0
            day = start
            while len(rows) < days:
                if day.weekday() < 5:
                    ret = 0.003 + rng.gauss(0.0, 0.01)
                    open_ = price
                    close = price * (1.0 + ret)
                    rows.append({
                        "Date": pd.Timestamp(day),
                        "Open": round(open_, 2),
                        "High": round(max(open_, close) * 1.005, 2),
                        "Low": round(min(open_, close) * 0.995, 2),
                        "Close": round(close, 2),
                        "Volume": 1_000_000,
                    })
                    price = close
                day += _dt.timedelta(days=1)
            return pd.DataFrame(rows).set_index("Date")

        import agenticwhales.backtest as bt
        monkeypatch.setattr(bt, "_load_history",
                             lambda sym, s, e: _build_df(s, 200))

        r = client.post("/api/backtest/run", json={
            "ticker": "AAPL",
            "from_date": "2024-01-01",
            "to_date": "2024-04-01",
            "starting_cash": 100_000,
            "kelly_cap": 0.10,
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["symbol"] == "AAPL"
        assert body["total_decisions"] >= 0
        assert isinstance(body["equity_curve"], list)
        assert isinstance(body["trades"], list)

    def test_bad_date_rejected(self, client):
        r = client.post("/api/backtest/run", json={
            "ticker": "AAPL",
            "from_date": "not-a-date",
            "to_date": "2024-04-01",
        })
        assert r.status_code in (400, 422)


class TestConvictionTimeseries:
    def test_empty_returns_empty_points(self, client):
        r = client.get("/api/paper/conviction/timeseries?half_life_days=5&limit=10")
        assert r.status_code == 200
        body = r.json()
        assert body["points"] == []

    def test_returns_decayed_series(self, client):
        # Seed one conviction score directly into the memstore.
        from datetime import datetime, timedelta, timezone
        recorded = (datetime.now(tz=timezone.utc) - timedelta(days=5)).isoformat()
        auth._memstore[("conviction_scores", "cs-1")] = {
            "id": 1, "user_id": "anonymous",
            "ticker": "AAPL", "rating": "Overweight",
            "conviction_score": 8,
            "recorded_at": recorded,
        }
        r = client.get("/api/paper/conviction/timeseries?half_life_days=5&limit=10&ticker=AAPL")
        assert r.status_code == 200
        body = r.json()
        assert len(body["points"]) == 1
        # 5 days at half-life 5 → decayed = raw / 2. Tolerance is loose because
        # decay is computed against wall-clock now(), which is a few ms after
        # the row was seeded — an exact 1e-9 match is impossible by design.
        assert abs(body["points"][0]["raw_score"] - 8.0) < 1e-9
        assert abs(body["points"][0]["decayed_score"] - 4.0) < 1e-3


class TestStreamingEventsEndpoint:
    def test_returns_audited_fires(self, client):
        from agenticwhales.audit import audit
        audit("system", "streaming.fire", target_user_id="anonymous",
              metadata={"recipe_id": "r-1", "symbol": "AAPL",
                        "reason": "price moved +0.50%", "fire_id": "f-1"})
        r = client.get("/api/streaming/events?limit=20")
        assert r.status_code == 200
        body = r.json()
        assert len(body["events"]) == 1
        ev = body["events"][0]
        assert ev["symbol"] == "AAPL"
        assert ev["reason"].startswith("price moved")


class TestSaveAsRecurringHappyPath:
    def test_post_recipe_with_heterogeneous_pair_succeeds(self, client):
        # The hero's save flow POSTs a payload with bull = google deep,
        # bear = deepseek deep. The endpoint must accept it.
        payload = {
            "name": "AAPL — saved from analysis",
            "tickers": ["AAPL"],
            "analysts": ["market", "quant", "news"],
            "llm_provider": "google",
            "quick_model": "gemini-3-flash-preview",
            "deep_model": "gemini-3.1-pro-preview",
            "bull_model": "gemini-3.1-pro-preview",
            "bear_model": "deepseek-v4-pro",
            "schedule_kind": "manual",
            "output_policy": "notify",
            "conviction_threshold": 7,
            "max_daily_token_cost_usd": 5.0,
            "market_hours_only": True,
        }
        r = client.post("/api/recipes", json=payload)
        assert r.status_code in (200, 201), r.text
        recipe = r.json()
        assert recipe["name"].startswith("AAPL")
        assert recipe["bull_model"] == "gemini-3.1-pro-preview"
        assert recipe["bear_model"] == "deepseek-v4-pro"

    def test_same_family_pair_rejected(self, client):
        # Bull + Bear both Google → validator rejects with 400.
        payload = {
            "name": "AAPL — same fam",
            "tickers": ["AAPL"],
            "analysts": ["market"],
            "llm_provider": "google",
            "quick_model": "gemini-3-flash-preview",
            "deep_model": "gemini-3.1-pro-preview",
            "bull_model": "gemini-3.1-pro-preview",
            "bear_model": "gemini-3-flash-preview",
            "schedule_kind": "manual",
            "output_policy": "notify",
        }
        r = client.post("/api/recipes", json=payload)
        assert r.status_code == 400
        assert "model families" in r.text.lower() or "heterogen" in r.text.lower()
