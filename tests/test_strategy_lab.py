"""Tests for the strategy-lab backtester — lock the cash accounting + causality."""

import datetime as _dt

import numpy as np
import pandas as pd
import pytest

from agenticwhales import strategy_lab as sl


def _trend_history(days=400, start="2024-01-01", slope=0.0007):
    dates = pd.bdate_range(start=start, periods=days)
    price = 100 * np.cumprod(1 + slope + 0.004 * np.sin(np.arange(days) / 9))
    return pd.DataFrame({"Open": price * 0.999, "High": price * 1.008,
                         "Low": price * 0.992, "Close": price}, index=dates)


def test_buyhold_tracks_underlying():
    h = _trend_history()
    s, e = h.index[60].date(), h.index[-1].date()
    spec = sl.StrategySpec("bh", "buyhold", allow_short=False)
    r = sl.backtest(spec, h, s, e, cost_bps=0.0)
    navs = [n for _, n in r.equity]
    # Buy&hold return should be within a hair of the close-to-close move (entry next open).
    underlying = float(h.loc[h.index.date <= e, "Close"].iloc[-1]) / float(h.loc[h.index.date >= s, "Open"].iloc[0]) - 1
    bt_ret = navs[-1] / navs[0] - 1
    assert abs(bt_ret - underlying) < 0.05


def test_cost_reduces_return():
    h = _trend_history()
    s, e = h.index[60].date(), h.index[-1].date()
    spec = sl.StrategySpec("trend", "trend", {"fast": 10, "slow": 30},
                           allow_short=True, vol_target_annual=0.15)
    free = sl.metrics(sl.backtest(spec, h, s, e, cost_bps=0.0))
    costed = sl.metrics(sl.backtest(spec, h, s, e, cost_bps=100.0))
    assert costed["total_return_pct"] <= free["total_return_pct"]


def test_trend_makes_money_in_smooth_uptrend():
    # Smooth, near-monotonic uptrend so the trend follower stays long (no whipsaw).
    dates = pd.bdate_range(start="2024-01-01", periods=400)
    price = 100 * np.cumprod(1 + 0.0012 + 0.0005 * np.sin(np.arange(400) / 40))
    h = pd.DataFrame({"Open": price * 0.999, "High": price * 1.003,
                      "Low": price * 0.997, "Close": price}, index=dates)
    s, e = h.index[120].date(), h.index[-1].date()
    spec = sl.StrategySpec("trend", "trend", {"fast": 20, "slow": 50}, allow_short=False)
    r = sl.metrics(sl.backtest(spec, h, s, e, cost_bps=5.0))
    assert r["total_return_pct"] > 0


def test_spec_roundtrip_dict():
    spec = sl.StrategySpec("x", "breakout", {"window": 40}, stop_loss_atr=2.0)
    d = spec.to_dict()
    assert d["signal"] == "breakout" and d["params"]["window"] == 40 and d["stop_loss_atr"] == 2.0


def test_portfolio_runs_over_universe():
    h = {s: _trend_history(start=f"2024-0{i+1}-01") for i, s in enumerate(["A", "B", "C"])}
    spec = sl.StrategySpec("bh", "buyhold", allow_short=False)
    out = sl.run_portfolio(spec, h, _dt.date(2024, 7, 1), _dt.date(2025, 6, 1))
    assert "sharpe" in out and "per_symbol" in out and len(out["per_symbol"]) == 3
