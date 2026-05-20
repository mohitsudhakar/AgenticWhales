"""Tests for the market-hours predicate + ticker→exchange heuristic."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agenticwhales.calendar import (
    CRYPTO_CODE,
    derive_exchange,
    is_market_open,
)


class TestDeriveExchange:
    def test_equity_tickers(self):
        assert derive_exchange(["AAPL"]) == "XNYS"
        assert derive_exchange(["AAPL", "MSFT", "GOOG"]) == "XNYS"

    def test_crypto_tickers(self):
        assert derive_exchange(["BTC-USD"]) == CRYPTO_CODE
        assert derive_exchange(["ETH-USDT"]) == CRYPTO_CODE

    def test_futures_tickers(self):
        assert derive_exchange(["ES=F"]) == "XCME"
        assert derive_exchange(["GC=F"]) == "XCME"

    def test_mixed_resolves_to_most_restrictive(self):
        # Equity dominates — we never want to fire a recipe at 03:00 ET
        # just because BTC is in the basket.
        assert derive_exchange(["AAPL", "BTC-USD"]) == "XNYS"
        assert derive_exchange(["ES=F", "BTC-USD"]) == "XCME"

    def test_empty_defaults_to_xnys(self):
        assert derive_exchange([]) == "XNYS"


class TestMarketHours:
    def test_crypto_always_open(self):
        # Saturday 3am UTC — equity markets closed.
        sat_3am = datetime(2026, 5, 16, 3, 0, tzinfo=timezone.utc)
        assert is_market_open(CRYPTO_CODE, sat_3am) is True

    def test_weekend_closed_for_equity(self):
        sat_noon = datetime(2026, 5, 16, 16, 0, tzinfo=timezone.utc)
        assert is_market_open("XNYS", sat_noon) is False

    def test_weekday_during_session(self):
        # Monday 2026-05-18 18:00 UTC = 14:00 ET — solidly inside the session.
        mon_2pm_et = datetime(2026, 5, 18, 18, 0, tzinfo=timezone.utc)
        assert is_market_open("XNYS", mon_2pm_et) is True

    def test_weekday_before_open(self):
        # Monday 2026-05-18 12:00 UTC = 08:00 ET — pre-market.
        mon_8am_et = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
        assert is_market_open("XNYS", mon_8am_et) is False

    def test_weekday_after_close(self):
        # Monday 2026-05-18 21:00 UTC = 17:00 ET — after-hours.
        mon_5pm_et = datetime(2026, 5, 18, 21, 0, tzinfo=timezone.utc)
        assert is_market_open("XNYS", mon_5pm_et) is False
