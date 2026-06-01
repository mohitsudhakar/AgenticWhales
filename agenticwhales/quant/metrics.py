"""Realized risk metrics computed from a price series.

All statistics are derived from daily **log returns** and annualized with a
252-trading-day convention. These are deliberately small, stable formulas;
they are unit-tested in ``tests/test_quant_math.py`` against known values.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def log_returns(prices: pd.Series) -> pd.Series:
    """Daily log returns of a price series (first value is NaN)."""
    out = np.log(prices).diff()
    out.name = "log_return"
    return out


def annualized_return(rets: pd.Series) -> float:
    """Mean daily return scaled to a year. NaN for an empty series."""
    clean = rets.dropna()
    if clean.empty:
        return float("nan")
    return float(clean.mean() * TRADING_DAYS)


def annualized_volatility(rets: pd.Series, *, annualize: bool = True) -> float:
    """Sample standard deviation of returns (annualized by default)."""
    clean = rets.dropna()
    if clean.empty:
        return float("nan")
    sigma = float(clean.std(ddof=1))
    return sigma * np.sqrt(TRADING_DAYS) if annualize else sigma


def sharpe_ratio(rets: pd.Series, *, risk_free: float = 0.0, annualize: bool = True) -> float:
    """Sharpe ratio. ``risk_free`` is an annual rate, converted per-period.

    Returns NaN for an empty series or one with zero dispersion.
    """
    clean = rets.dropna()
    if clean.empty:
        return float("nan")
    rf_period = risk_free / TRADING_DAYS
    excess = clean - rf_period
    sigma = float(excess.std(ddof=1))
    if sigma == 0:
        return float("nan")
    sharpe = float(excess.mean()) / sigma
    return sharpe * np.sqrt(TRADING_DAYS) if annualize else sharpe


def max_drawdown(rets: pd.Series) -> float:
    """Maximum drawdown (a non-positive number) of wealth compounded from
    log returns. NaN for an empty series."""
    clean = rets.dropna()
    if clean.empty:
        return float("nan")
    wealth = np.exp(clean.cumsum())
    running_peak = wealth.cummax()
    drawdown = wealth / running_peak - 1.0
    return float(drawdown.min())


def risk_metrics(
    prices: pd.Series,
    *,
    symbol: str,
    risk_free: float = 0.0,
) -> dict:
    """One-call snapshot: annualized return / vol, Sharpe, max drawdown.

    ``prices`` should be a date-indexed adjusted-close series. Returns a
    dict with the fields the markdown formatter expects.
    """
    prices = prices.dropna()
    if len(prices) < 2:
        return {
            "symbol": symbol,
            "rows": int(len(prices)),
            "annual_return": float("nan"),
            "annual_volatility": float("nan"),
            "sharpe": float("nan"),
            "max_drawdown": float("nan"),
            "start": "",
            "end": "",
        }
    rets = log_returns(prices)
    return {
        "symbol": symbol,
        "rows": int(len(prices)),
        "annual_return": annualized_return(rets),
        "annual_volatility": annualized_volatility(rets),
        "sharpe": sharpe_ratio(rets, risk_free=risk_free),
        "max_drawdown": max_drawdown(rets),
        "start": pd.Timestamp(prices.index.min()).strftime("%Y-%m-%d"),
        "end": pd.Timestamp(prices.index.max()).strftime("%Y-%m-%d"),
    }
