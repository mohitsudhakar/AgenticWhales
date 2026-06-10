"""Cached OHLC price fetcher (yfinance) — shared by the coach's price-based
counterfactuals and the pre-trade decision-support read.

Network-touching and therefore injected into the otherwise-pure coach/pretrade
modules, so their core logic stays unit-testable offline.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Dict, Optional, Tuple

import pandas as pd

log = logging.getLogger(__name__)

_CACHE: Dict[Tuple[str, str, str], pd.DataFrame] = {}


def fetch_ohlc(symbol: str, start, end) -> Optional[pd.DataFrame]:
    """Return a date-indexed OHLC DataFrame for [start, end], or None on failure.

    Cached in-process by (symbol, start, end). Index is tz-naive dates.
    """
    s = str(start)[:10]
    e = str(end)[:10]
    key = (symbol.upper(), s, e)
    if key in _CACHE:
        return _CACHE[key]
    try:
        import yfinance as yf
        end_excl = (_dt.date.fromisoformat(e) + _dt.timedelta(days=1)).isoformat()
        df = yf.Ticker(symbol.upper()).history(start=s, end=end_excl)
        if df is None or df.empty:
            return None
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        _CACHE[key] = df
        return df
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_ohlc %s %s..%s failed: %s", symbol, s, e, exc)
        return None
