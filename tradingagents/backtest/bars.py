"""Historical bar loader for the backtest harness.

Uses yfinance — the same vendor the live agent graph already depends on —
so the data the agents would have seen is consistent with the data we
replay through the executor.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd


def load_history(
    symbol: str,
    start: str,
    end: str,
    *,
    interval: str = "1d",
) -> pd.DataFrame:
    """Return a DataFrame of OHLCV bars indexed by date (UTC-naive).

    Columns: ``open``, ``high``, ``low``, ``close``, ``volume`` (lowercase).
    """
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    df = ticker.history(start=start, end=end, interval=interval, auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = df.rename(columns=str.lower)
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    df = df[keep].copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


def synthetic_bars(
    prices: list[float],
    *,
    start: str = "2025-01-01",
    freq: str = "B",
) -> pd.DataFrame:
    """Generate a deterministic OHLCV frame for tests. Open == close == price."""
    idx = pd.date_range(start=start, periods=len(prices), freq=freq)
    return pd.DataFrame(
        {
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": [0] * len(prices),
        },
        index=idx,
    )
