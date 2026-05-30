"""Unit tests for the Marketto-backed quant risk-metrics dataflow + tool.

Hermetic: the OHLCV loader is injected with a deterministic synthetic
price frame, so no network and no real ``load_ohlcv`` call. The compute
path requires the optional ``marketto`` package; those tests skip cleanly
when it is not installed. The graceful-degradation path is tested without
marketto via monkeypatching.
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


# ---- compute path (requires marketto) --------------------------------------


def test_compute_risk_metrics_returns_expected_fields():
    pytest.importorskip("marketto")
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
    pytest.importorskip("marketto")
    df = _synthetic_ohlcv(n=40)
    metrics = quant_metrics.compute_risk_metrics("AAA", df, look_back_days=252)
    assert metrics["rows"] == 40  # fewer than the window → use everything


def test_get_risk_metrics_with_injected_loader_renders_markdown():
    pytest.importorskip("marketto")
    df = _synthetic_ohlcv()
    out = quant_metrics.get_risk_metrics(
        "AAA", "2024-01-15", look_back_days=252, loader=lambda s, d: df
    )
    assert "Realized risk metrics" in out
    assert "Annualized volatility" in out
    assert "Sharpe ratio" in out
    assert "Max drawdown" in out


def test_get_risk_metrics_handles_insufficient_history():
    pytest.importorskip("marketto")
    one_row = _synthetic_ohlcv(n=1)
    out = quant_metrics.get_risk_metrics(
        "AAA", "2024-01-15", loader=lambda s, d: one_row
    )
    assert "insufficient price history" in out


def test_get_risk_metrics_handles_loader_errors():
    pytest.importorskip("marketto")

    def boom(symbol, curr_date):
        raise RuntimeError("network down")

    out = quant_metrics.get_risk_metrics("AAA", "2024-01-15", loader=boom)
    assert "could not load price data" in out


# ---- graceful degradation (no marketto) ------------------------------------


def test_get_risk_metrics_degrades_without_marketto(monkeypatch):
    monkeypatch.setattr(quant_metrics, "_marketto_available", lambda: False)
    out = quant_metrics.get_risk_metrics(
        "AAA", "2024-01-15", loader=lambda s, d: _synthetic_ohlcv()
    )
    assert "marketto" in out.lower()


def test_compute_risk_metrics_raises_without_marketto(monkeypatch):
    monkeypatch.setattr(quant_metrics, "_marketto_available", lambda: False)
    with pytest.raises(RuntimeError):
        quant_metrics.compute_risk_metrics("AAA", _synthetic_ohlcv())


# ---- tool wrapper is importable + correctly shaped -------------------------


def test_tool_is_registered_and_callable():
    from agenticwhales.agents.utils.quant_metrics_tools import get_risk_metrics

    assert get_risk_metrics.name == "get_risk_metrics"
    # The langchain @tool wraps the function; description comes from docstring.
    assert "risk metrics" in get_risk_metrics.description.lower()
