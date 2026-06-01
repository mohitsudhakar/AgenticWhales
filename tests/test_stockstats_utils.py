"""Coverage for agenticwhales/dataflows/stockstats_utils.py — yfinance retry
wrapper, dataframe cleaning, look-ahead filtering, cached OHLCV load, and the
stockstats indicator lookup. yf.download is monkeypatched; no network.
"""

from __future__ import annotations

import pandas as pd
import pytest

from agenticwhales.dataflows import stockstats_utils as su
from yfinance.exceptions import YFRateLimitError


# ===========================================================================
# yf_retry
# ===========================================================================

def test_yf_retry_returns_immediately():
    assert su.yf_retry(lambda: 42) == 42


def test_yf_retry_recovers_after_rate_limit(monkeypatch):
    monkeypatch.setattr(su.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise YFRateLimitError()
        return "ok"

    assert su.yf_retry(_flaky, max_retries=3, base_delay=0.01) == "ok"
    assert calls["n"] == 2


def test_yf_retry_raises_after_exhaustion(monkeypatch):
    monkeypatch.setattr(su.time, "sleep", lambda s: None)

    def _always():
        raise YFRateLimitError()

    with pytest.raises(YFRateLimitError):
        su.yf_retry(_always, max_retries=2, base_delay=0.01)


# ===========================================================================
# _clean_dataframe
# ===========================================================================

def test_clean_dataframe_drops_bad_dates_and_fills():
    df = pd.DataFrame({
        "Date": ["2024-01-01", "not-a-date", "2024-01-03"],
        "Open": [None, 2.0, 3.0], "High": [1, 2, 3], "Low": [1, 2, 3],
        "Close": [10.0, 11.0, 12.0], "Volume": [100, 200, 300],
    })
    out = su._clean_dataframe(df)
    # bad-date row dropped → 2 rows; the leading None Open was ff/bfilled
    assert len(out) == 2
    assert out["Open"].isna().sum() == 0 and out["Close"].isna().sum() == 0


# ===========================================================================
# filter_financials_by_date
# ===========================================================================

def test_filter_financials_drops_future_columns():
    df = pd.DataFrame(
        [[1, 2, 3]],
        columns=pd.to_datetime(["2023-12-31", "2024-06-30", "2024-12-31"]),
    )
    out = su.filter_financials_by_date(df, "2024-07-01")
    assert list(out.columns) == list(pd.to_datetime(["2023-12-31", "2024-06-30"]))


def test_filter_financials_noop_when_empty_or_no_date():
    df = pd.DataFrame()
    assert su.filter_financials_by_date(df, "2024-01-01").empty
    df2 = pd.DataFrame({"a": [1]})
    assert su.filter_financials_by_date(df2, "").equals(df2)


# ===========================================================================
# load_ohlcv + get_stock_stats (cached / mocked download)
# ===========================================================================

@pytest.fixture
def fake_market(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    idx = pd.date_range("2024-01-01", periods=40, freq="D", name="Date")
    prices = pd.DataFrame({
        "Open": [100 + i for i in range(40)],
        "High": [101 + i for i in range(40)],
        "Low": [99 + i for i in range(40)],
        "Close": [100 + i for i in range(40)],
        "Volume": [1_000 + i for i in range(40)],
    }, index=idx)
    monkeypatch.setattr(su.yf, "download", lambda *a, **k: prices.copy())
    return tmp_path


def test_load_ohlcv_downloads_then_caches(fake_market):
    df = su.load_ohlcv("AAPL", "2024-01-20")
    # look-ahead filter keeps only rows up to curr_date
    assert df["Date"].max() <= pd.Timestamp("2024-01-20")
    # a cache file was written
    assert any(p.suffix == ".csv" for p in fake_market.iterdir())
    # second call reads from the cache (download not needed)
    df2 = su.load_ohlcv("AAPL", "2024-01-10")
    assert df2["Date"].max() <= pd.Timestamp("2024-01-10")


def test_get_stock_stats_returns_indicator(fake_market):
    val = su.StockstatsUtils.get_stock_stats("AAPL", "macd", "2024-01-30")
    assert val != "N/A: Not a trading day (weekend or holiday)"


def test_get_stock_stats_non_trading_day(fake_market):
    # a date past the dataset → no matching row → N/A message
    val = su.StockstatsUtils.get_stock_stats("AAPL", "macd", "2024-12-31")
    assert val == "N/A: Not a trading day (weekend or holiday)"
