"""Unit tests for the in-tree quant math (agenticwhales.quant.metrics)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from agenticwhales.quant import (
    TRADING_DAYS,
    annualized_return,
    annualized_volatility,
    log_returns,
    max_drawdown,
    risk_metrics,
    sharpe_ratio,
)

pytestmark = pytest.mark.unit


def _prices(rets: np.ndarray, start: float = 100.0) -> pd.Series:
    idx = pd.bdate_range("2024-01-02", periods=len(rets) + 1)
    return pd.Series(start * np.exp(np.cumsum(np.r_[0.0, rets])), index=idx)


def test_log_returns_basic():
    prices = pd.Series([100.0, 110.0, 99.0])
    lr = log_returns(prices)
    assert np.isnan(lr.iloc[0])
    assert lr.iloc[1] == pytest.approx(np.log(110 / 100))


def test_constant_log_return_volatility_near_zero_and_known_return():
    # Constant daily log return r → ~zero vol, annual return = r * 252.
    r = 0.001
    prices = _prices(np.full(252, r))
    rets = log_returns(prices)
    assert annualized_volatility(rets) == pytest.approx(0.0, abs=1e-6)
    assert annualized_return(rets) == pytest.approx(r * TRADING_DAYS)


def test_sharpe_exactly_zero_dispersion_is_nan():
    # An exactly-constant returns series has zero std → Sharpe is NaN.
    flat = pd.Series([0.0] * 30)
    assert np.isnan(sharpe_ratio(flat))


def test_volatility_matches_manual_annualization():
    rng = np.random.default_rng(0)
    daily = rng.normal(0, 0.01, 252)
    prices = _prices(daily)
    rets = log_returns(prices).dropna()
    expected = float(rets.std(ddof=1)) * np.sqrt(TRADING_DAYS)
    assert annualized_volatility(rets) == pytest.approx(expected)


def test_max_drawdown_known_path():
    # Up 10%, then down to -20% from the peak.
    prices = pd.Series([100.0, 110.0, 88.0],
                       index=pd.bdate_range("2024-01-02", periods=3))
    rets = log_returns(prices)
    dd = max_drawdown(rets)
    assert dd == pytest.approx(88.0 / 110.0 - 1.0, rel=1e-9)


def test_empty_series_returns_nan():
    empty = pd.Series(dtype="float64")
    assert np.isnan(annualized_return(empty))
    assert np.isnan(annualized_volatility(empty))
    assert np.isnan(sharpe_ratio(empty))
    assert np.isnan(max_drawdown(empty))


def test_sharpe_risk_free_lowers_ratio():
    rng = np.random.default_rng(1)
    prices = _prices(rng.normal(0.0008, 0.01, 252))
    rets = log_returns(prices)
    gross = sharpe_ratio(rets, risk_free=0.0)
    net = sharpe_ratio(rets, risk_free=0.05)
    assert net < gross


def test_risk_metrics_dict_shape_and_short_history():
    prices = _prices(np.full(30, 0.001))
    m = risk_metrics(prices, symbol="AAA")
    assert m["symbol"] == "AAA"
    assert m["rows"] == 31
    assert m["start"] and m["end"]

    short = risk_metrics(pd.Series([100.0], index=pd.bdate_range("2024-01-02", periods=1)),
                         symbol="AAA")
    assert short["rows"] == 1
    assert np.isnan(short["annual_volatility"])
    assert short["start"] == ""
