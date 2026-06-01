"""Self-contained quantitative metrics for AgenticWhales.

Pure pandas/numpy implementations of the realized risk statistics the
Quant Analyst grounds its radar in — annualized return, annualized
volatility, Sharpe ratio, and maximum drawdown. Formerly provided by the
external ``marketto`` library; folded in here so AgenticWhales owns the
math with no extra dependency (it only ever used this slice of marketto).
"""

from agenticwhales.quant.metrics import (
    TRADING_DAYS,
    annualized_return,
    annualized_volatility,
    log_returns,
    max_drawdown,
    risk_metrics,
    sharpe_ratio,
)

__all__ = [
    "TRADING_DAYS",
    "annualized_return",
    "annualized_volatility",
    "log_returns",
    "max_drawdown",
    "risk_metrics",
    "sharpe_ratio",
]
