"""Equity curve metrics for the backtest harness.

Deliberately minimal — CAGR, Sharpe, max drawdown, hit rate, total return.
The point isn't to be a research-grade analytics library; it's to give
the user an honest number against which to compare the agent system to
"buy and hold" before they put real money behind it.
"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence

import pandas as pd


def equity_metrics(
    equity_curve: Sequence[float],
    *,
    periods_per_year: int = 252,
    initial_equity: float | None = None,
) -> Dict[str, float]:
    if len(equity_curve) < 2:
        return {
            "total_return": 0.0,
            "cagr": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "volatility": 0.0,
            "final_equity": float(equity_curve[0]) if equity_curve else 0.0,
        }

    series = pd.Series([float(v) for v in equity_curve])
    start = float(initial_equity if initial_equity is not None else series.iloc[0])
    end = float(series.iloc[-1])
    total_return = (end / start) - 1.0 if start > 0 else 0.0

    n = len(series) - 1
    years = n / periods_per_year if periods_per_year > 0 else 0.0
    cagr = (end / start) ** (1.0 / years) - 1.0 if start > 0 and years > 0 else 0.0

    returns = series.pct_change().dropna()
    vol = float(returns.std()) * math.sqrt(periods_per_year) if not returns.empty else 0.0
    mean = float(returns.mean()) * periods_per_year if not returns.empty else 0.0
    sharpe = (mean / vol) if vol > 0 else 0.0

    running_max = series.cummax()
    drawdowns = (series / running_max) - 1.0
    max_dd = float(drawdowns.min()) if not drawdowns.empty else 0.0

    return {
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "volatility": vol,
        "final_equity": end,
    }


def trade_hit_rate(trade_pnls: Sequence[float]) -> float:
    """Fraction of trades with positive realized P&L. Returns 0 for empty input."""
    if not trade_pnls:
        return 0.0
    wins = sum(1 for p in trade_pnls if p > 0)
    return wins / len(trade_pnls)
