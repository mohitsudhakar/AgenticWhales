"""Unit tests for the options lab — pricing math and the vol-selling backtester.

All deterministic synthetic data; no network, no keys.
"""

from __future__ import annotations

import datetime as _dt
import math

import numpy as np
import pandas as pd
import pytest

from agenticwhales import options_lab as ol

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- pricing ----

def test_bs_atm_call_known_value():
    # ATM, sigma=0.2, T=1, r=q=0 -> ~7.9656% of spot (classic reference value)
    assert ol.bs_price("C", 100, 100, 1.0, 0.2) == pytest.approx(7.9656, abs=1e-3)


def test_bs_put_call_parity():
    s, k, t, sig, r, q = 105.0, 98.0, 0.25, 0.31, 0.03, 0.01
    c = ol.bs_price("C", s, k, t, sig, r, q)
    p = ol.bs_price("P", s, k, t, sig, r, q)
    assert c - p == pytest.approx(s * math.exp(-q * t) - k * math.exp(-r * t), abs=1e-9)


def test_bs_expiry_is_intrinsic():
    assert ol.bs_price("P", 90, 100, 0.0, 0.5) == pytest.approx(10.0)
    assert ol.bs_price("C", 90, 100, 0.0, 0.5) == 0.0


def test_bs_rejects_bad_type():
    with pytest.raises(ValueError):
        ol.bs_price("X", 100, 100, 1.0, 0.2)


def test_implied_vol_round_trip():
    price = ol.bs_price("P", 100, 92, 0.08, 0.27, 0.02, 0.015)
    iv = ol.implied_vol("P", price, 100, 92, 0.08, 0.02, 0.015)
    assert iv == pytest.approx(0.27, abs=1e-4)


def test_implied_vol_out_of_bounds_returns_none():
    # Below intrinsic / above spot: no vol can produce these prices.
    assert ol.implied_vol("C", 200.0, 100, 100, 0.5) is None
    assert ol.implied_vol("C", 0.0, 100, 100, 0.5) is None


# ------------------------------------------------------------ expiry cycle ----

def test_third_friday_known_dates():
    assert ol.third_friday(2024, 6) == _dt.date(2024, 6, 21)
    assert ol.third_friday(2008, 10) == _dt.date(2008, 10, 17)
    assert ol.third_friday(2026, 1) == _dt.date(2026, 1, 16)


def test_roll_dates_use_prior_trading_day_on_holiday():
    idx = pd.bdate_range("2024-06-01", "2024-08-01")
    tf = pd.Timestamp(ol.third_friday(2024, 6))
    idx_holiday = idx[idx != tf]  # drop the third Friday, as for a market holiday
    rolls = ol.roll_dates(idx_holiday, _dt.date(2024, 6, 1), _dt.date(2024, 8, 1))
    assert rolls[0] == tf - pd.Timedelta(days=1)
    assert rolls[1] == pd.Timestamp(ol.third_friday(2024, 7))


# ---------------------------------------------------------------- backtest ----

def _flat_market(price=100.0, iv=0.2, years=2):
    idx = pd.bdate_range("2020-01-01", periods=int(252 * years))
    close = pd.Series(price, index=idx)
    return close, pd.Series(iv, index=idx)


def test_putwrite_flat_market_collects_premium():
    close, iv = _flat_market()
    res = ol.backtest(ol.VolSellSpec("pw", "putwrite", sd=1.0), close, iv)
    assert res.n_trades >= 20
    assert (res.trades["payoff_pct"] == 0).all()      # never breached in a flat market
    assert (res.trades["pnl_pct"] > 0).all()
    assert float(res.equity.iloc[-1]) > 1.0
    # rv ~ 0 in a flat market, so VRP per trade ~ the full implied vol
    assert res.trades["vrp"].dropna().mean() == pytest.approx(0.2, abs=1e-6)


def _crash_market(drop=0.30):
    """Flat at 100, then a one-window crash to 100*(1-drop) that sticks."""
    close, iv = _flat_market()
    crash_day = pd.Timestamp(ol.third_friday(2020, 6)) + pd.Timedelta(days=5)
    close[close.index >= crash_day] = 100.0 * (1 - drop)
    return close, iv


def test_putwrite_crash_takes_a_loss():
    close, iv = _crash_market()
    res = ol.backtest(ol.VolSellSpec("pw", "putwrite", sd=1.0), close, iv)
    worst = res.trades.loc[res.trades["pnl_pct"].idxmin()]
    assert worst["expiry"] == pd.Timestamp(ol.third_friday(2020, 7))
    assert worst["pnl_pct"] < -10  # a 30% gap through a ~1-SD strike must hurt
    assert worst["payoff_pct"] > worst["premium_pct"]


def test_strangle_loses_on_both_breaches():
    close, iv = _flat_market()
    spec = ol.VolSellSpec("st", "strangle", sd=0.5, notional_frac=0.25)
    # rally through the call strike sticks: the call side pays out
    close[close.index >= pd.Timestamp(ol.third_friday(2020, 6)) + pd.Timedelta(days=5)] = 130.0
    res = ol.backtest(spec, close, iv)
    worst = res.trades.loc[res.trades["pnl_pct"].idxmin()]
    assert worst["pnl_pct"] < 0
    assert worst["expiry"] == pd.Timestamp(ol.third_friday(2020, 7))


def test_condor_loss_is_capped_by_the_wings():
    close, iv = _crash_market(drop=0.50)  # catastrophic gap, far through both wings
    spec = ol.VolSellSpec("cd", "condor", sd=1.0, wing_sd=2.0, notional_frac=0.25)
    res = ol.backtest(spec, close, iv)
    assert res.max_loss_bound_pct is not None
    worst_pct = float(res.trades["pnl_pct"].min())
    # the realized worst trade respects the structural cap (small interest slack)
    assert worst_pct >= -(res.max_loss_bound_pct + 0.5)
    # and the cap actually bites: a naked strangle same size loses much more
    naked = ol.backtest(
        ol.VolSellSpec("st", "strangle", sd=1.0, notional_frac=0.25), close, iv
    )
    assert float(naked.trades["pnl_pct"].min()) < worst_pct


def test_collateral_earns_the_bill_rate():
    close, iv = _flat_market()
    rate = pd.Series(0.05, index=close.index)
    with_bills = ol.backtest(ol.VolSellSpec("pw", "putwrite", sd=1.0), close, iv, rate)
    no_bills = ol.backtest(ol.VolSellSpec("pw", "putwrite", sd=1.0), close, iv)
    assert float(with_bills.equity.iloc[-1]) > float(no_bills.equity.iloc[-1])


def test_missing_iv_month_sits_in_cash():
    close, iv = _flat_market()
    iv[:] = float("nan")  # no quote ever -> never trades
    rate = pd.Series(0.04, index=close.index)
    res = ol.backtest(ol.VolSellSpec("pw", "putwrite", sd=1.0), close, iv, rate)
    assert res.n_trades == 0
    assert float(res.equity.iloc[-1]) > 1.0  # still earned bills


def test_unknown_structure_rejected():
    close, iv = _flat_market()
    with pytest.raises(ValueError):
        ol.backtest(ol.VolSellSpec("x", "calendar"), close, iv)


# ------------------------------------------------------------------ metrics ----

def test_curve_metrics_drawdown():
    curve = pd.Series(
        [1.0, 1.1, 0.99, 1.2],
        index=pd.to_datetime(["2020-01-17", "2020-02-21", "2020-03-20", "2020-04-17"]),
    )
    _, total, mdd = ol.curve_metrics(curve)
    assert total == pytest.approx(20.0)
    assert mdd == pytest.approx(10.0, abs=1e-6)


def test_year_stats_and_vrp_summary():
    close, iv = _crash_market()
    res = ol.backtest(ol.VolSellSpec("pw", "putwrite", sd=1.0), close, iv)
    st = ol.year_stats(res, 2020)
    assert 10 <= st["n_trades"] <= 12  # the 2020 slice, not the full two-year run
    assert st["n_trades"] < res.n_trades
    assert st["worst_trade_pct"] == pytest.approx(float(res.trades["pnl_pct"].min()))
    summary = ol.vrp_summary(res)
    assert summary["win_rate_pct"] < 100.0
    assert summary["worst_trade_expiry"] == str(ol.third_friday(2020, 7))
    assert summary["mean_premium_pct"] > 0
