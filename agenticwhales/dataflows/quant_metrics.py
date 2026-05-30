"""Quantitative risk metrics for the Quant Analyst.

AgenticWhales fetches OHLCV and technical indicators, but nothing else in
the repo computes *realized* risk statistics — annualized volatility,
Sharpe, or max drawdown. Yet the Portfolio Manager's ``PortfolioDecision``
schema explicitly asks the LLM to self-report ``expected_volatility_pct``
"anchored in historical realized vol", and the Kelly sizer + calibration
head both depend on that number being grounded rather than hallucinated.

This module closes that gap. It reuses the OHLCV DataFrame that
``stockstats_utils.load_ohlcv`` already produces — cached and filtered to
``curr_date`` to prevent look-ahead bias — and runs it through the
in-tree ``agenticwhales.quant`` math. No re-fetch, no future data, and no
external dependency.

Design mirrors the rest of ``dataflows/``: a string-returning public
function for LLM consumption, plus a separable pure-compute helper
(``compute_risk_metrics``) that takes a DataFrame so tests stay hermetic.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from agenticwhales.quant import risk_metrics

logger = logging.getLogger(__name__)


def _adjusted_close(df: pd.DataFrame) -> pd.Series:
    """Extract a date-indexed adjusted-close series from an AW OHLCV frame.

    ``load_ohlcv`` returns columns ``Date, Open, High, Low, Close, Volume``
    with ``auto_adjust=True`` (so ``Close`` is already split/dividend
    adjusted). We tolerate an explicit ``Adj Close`` if present.
    """
    cols = {str(c).lower().replace(" ", "_"): c for c in df.columns}
    frame = df.copy()
    if "date" in cols:
        frame = frame.set_index(cols["date"])
    frame.index = pd.to_datetime(frame.index)
    price_col = cols.get("adj_close") or cols.get("close")
    if price_col is None:
        raise ValueError("OHLCV frame has no close/adj_close column")
    series = pd.to_numeric(frame[price_col], errors="coerce")
    return series.sort_index()


def compute_risk_metrics(
    symbol: str,
    df: pd.DataFrame,
    *,
    look_back_days: int = 252,
    risk_free: float = 0.0,
) -> dict:
    """Compute realized risk metrics for ``symbol`` from an OHLCV frame.

    Pure function — no I/O — so it can be unit-tested with synthetic data.
    Returns the metrics dict plus the look-back window that was used.
    """
    prices = _adjusted_close(df).dropna()
    if look_back_days and len(prices) > look_back_days:
        prices = prices.tail(look_back_days)
    out = risk_metrics(prices, symbol=symbol, risk_free=risk_free)
    out["look_back_days"] = int(look_back_days)
    return out


def format_risk_metrics(metrics: dict) -> str:
    """Render a metrics dict as a compact markdown block for the LLM."""
    def pct(x: float) -> str:
        return f"{x * 100:+.2f}%" if x == x else "n/a"  # x==x guards NaN

    def num(x: float) -> str:
        return f"{x:+.3f}" if x == x else "n/a"

    rows = metrics.get("rows", 0)
    lines = [
        f"### Realized risk metrics — {metrics.get('symbol', '?')}",
        f"_Window: {metrics.get('start', '?')} → {metrics.get('end', '?')} "
        f"({rows} trading days, look-back {metrics.get('look_back_days', '?')})_",
        "",
        f"- **Annualized return:** {pct(metrics.get('annual_return', float('nan')))}",
        f"- **Annualized volatility:** {pct(metrics.get('annual_volatility', float('nan')))}",
        f"- **Sharpe ratio:** {num(metrics.get('sharpe', float('nan')))}",
        f"- **Max drawdown:** {pct(metrics.get('max_drawdown', float('nan')))}",
        "",
        "_Use these realized figures to ground volatility / probability "
        "estimates — do not invent values that contradict them._",
    ]
    return "\n".join(lines)


def get_risk_metrics(
    symbol: str,
    curr_date: str,
    look_back_days: int = 252,
    *,
    risk_free: float = 0.0,
    loader: Optional[object] = None,
) -> str:
    """Public entry point: realized risk metrics for ``symbol`` as markdown.

    Loads OHLCV via ``stockstats_utils.load_ohlcv`` (cached, filtered to
    ``curr_date`` for look-ahead safety), computes the metrics, and returns
    a formatted block. ``loader`` is injectable for testing; defaults to the
    real ``load_ohlcv``.
    """
    if loader is None:
        from agenticwhales.dataflows.stockstats_utils import load_ohlcv as loader  # type: ignore

    try:
        df = loader(symbol, curr_date)
    except Exception as exc:  # network / data errors shouldn't crash the graph
        logger.warning("get_risk_metrics: failed to load OHLCV for %s: %s", symbol, exc)
        return f"Risk metrics unavailable for {symbol}: could not load price data ({exc})."

    if df is None or len(df) < 2:
        return f"Risk metrics unavailable for {symbol}: insufficient price history."

    try:
        metrics = compute_risk_metrics(
            symbol, df, look_back_days=look_back_days, risk_free=risk_free
        )
    except Exception as exc:
        logger.warning("get_risk_metrics: computation failed for %s: %s", symbol, exc)
        return f"Risk metrics unavailable for {symbol}: {exc}."

    return format_risk_metrics(metrics)
