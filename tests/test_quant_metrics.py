"""Unit tests for the quant risk-metrics dataflow + tool.

Hermetic: the OHLCV loader is injected with a deterministic synthetic
price frame, so no network and no real ``load_ohlcv`` call. The risk math
itself lives in ``agenticwhales.quant`` (see test_quant_math.py).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from agenticwhales.dataflows import quant_metrics

pytestmark = pytest.mark.unit


def _synthetic_ohlcv(n: int = 300, seed: int = 7) -> pd.DataFrame:
    """Deterministic GBM-ish daily bars in AgenticWhales' load_ohlcv shape."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n)
    rets = rng.normal(0.0004, 0.013, size=n)
    close = 100.0 * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, 0.004, size=n)))
    low = close * (1 - np.abs(rng.normal(0, 0.004, size=n)))
    open_ = np.empty(n)
    open_[0] = 100.0
    open_[1:] = close[:-1]
    volume = rng.integers(1_000_000, 5_000_000, size=n).astype(float)
    return pd.DataFrame(
        {
            "Date": dates,
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": volume,
        }
    )


# ---- compute path ----------------------------------------------------------


def test_compute_risk_metrics_returns_expected_fields():
    df = _synthetic_ohlcv()
    metrics = quant_metrics.compute_risk_metrics("AAA", df, look_back_days=252)
    for key in (
        "symbol",
        "annual_return",
        "annual_volatility",
        "sharpe",
        "max_drawdown",
        "rows",
        "start",
        "end",
        "look_back_days",
    ):
        assert key in metrics
    assert metrics["symbol"] == "AAA"
    # Window respected: 252 of the 300 supplied days.
    assert metrics["rows"] == 252
    # Volatility is a positive, sane annualized number for ~1.3% daily vol.
    assert 0.05 < metrics["annual_volatility"] < 1.0
    # Max drawdown is non-positive.
    assert metrics["max_drawdown"] <= 0.0


def test_compute_risk_metrics_short_history_uses_all_rows():
    df = _synthetic_ohlcv(n=40)
    metrics = quant_metrics.compute_risk_metrics("AAA", df, look_back_days=252)
    assert metrics["rows"] == 40  # fewer than the window → use everything


def test_compute_risk_metrics_accepts_explicit_adj_close():
    df = _synthetic_ohlcv(n=60)
    df["Adj Close"] = df["Close"] * 0.9  # an explicit adjusted column wins
    metrics = quant_metrics.compute_risk_metrics("AAA", df, look_back_days=252)
    assert metrics["rows"] == 60


def test_get_risk_metrics_with_injected_loader_renders_markdown():
    df = _synthetic_ohlcv()
    out = quant_metrics.get_risk_metrics(
        "AAA", "2024-01-15", look_back_days=252, loader=lambda s, d: df
    )
    assert "Realized risk metrics" in out
    assert "Annualized volatility" in out
    assert "Sharpe ratio" in out
    assert "Max drawdown" in out


def test_get_risk_metrics_handles_insufficient_history():
    one_row = _synthetic_ohlcv(n=1)
    out = quant_metrics.get_risk_metrics(
        "AAA", "2024-01-15", loader=lambda s, d: one_row
    )
    assert "insufficient price history" in out


def test_get_risk_metrics_handles_loader_errors():
    def boom(symbol, curr_date):
        raise RuntimeError("network down")

    out = quant_metrics.get_risk_metrics("AAA", "2024-01-15", loader=boom)
    assert "could not load price data" in out


def test_get_risk_metrics_handles_compute_errors():
    # A frame with no usable price column → compute raises → graceful message.
    bad = pd.DataFrame({"Date": pd.bdate_range("2024-01-02", periods=3),
                        "Volume": [1, 2, 3]})
    out = quant_metrics.get_risk_metrics("AAA", "2024-01-15", loader=lambda s, d: bad)
    assert "unavailable" in out.lower()


# ---- tool wrapper is importable + correctly shaped -------------------------


def test_tool_is_registered_and_callable():
    from agenticwhales.agents.utils.quant_metrics_tools import get_risk_metrics

    assert get_risk_metrics.name == "get_risk_metrics"
    # The langchain @tool wraps the function; description comes from docstring.
    assert "risk metrics" in get_risk_metrics.description.lower()
