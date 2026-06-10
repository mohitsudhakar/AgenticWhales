"""Unit tests for the defensive-overlay engine — synthetic prices, no network."""

import math

import numpy as np
import pandas as pd
import pytest

from agenticwhales import overlay


def _series(daily_ret: float, *, days: int = 300, vol: float = 0.0,
            start_px: float = 100.0, seed: int = 7) -> pd.Series:
    """A geometric price path with constant drift and optional gaussian noise."""
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, vol, size=days) if vol else np.zeros(days)
    rets = daily_ret + noise
    px = start_px * np.cumprod(1.0 + rets)
    idx = pd.bdate_range("2024-01-02", periods=days)
    return pd.Series(px, index=idx)


def test_uptrend_low_vol_sleeve_fully_on():
    s = overlay.sleeve_status("spy", _series(0.0008, vol=0.002))
    assert s.symbol == "SPY"
    assert s.in_trend and s.lookback_return_pct > 0
    assert s.trend_scale == 1.0          # realized vol well under the 15% target


def test_downtrend_sleeve_off():
    s = overlay.sleeve_status("tlt", _series(-0.0008, vol=0.002))
    assert not s.in_trend
    assert s.trend_scale == 0.0


def test_high_vol_uptrend_scaled_down():
    s = overlay.sleeve_status("qqq", _series(0.004, vol=0.02, seed=3))
    assert s.in_trend
    assert 0.0 < s.trend_scale < 1.0
    # scale should equal vol_target / realized_vol
    assert s.trend_scale == pytest.approx(
        min(1.0, overlay.VOL_TARGET_ANNUAL / (s.realized_vol_pct / 100)), abs=0.01)


def test_short_history_keeps_buyhold_half_only():
    s = overlay.sleeve_status("gld", _series(0.001, days=60))
    assert not s.in_trend and s.trend_scale == 0.0


def test_compute_overlay_weights_and_cash():
    closes = {
        "SPY": _series(0.0008, vol=0.002, seed=1),   # trend on, scale 1
        "TLT": _series(-0.0008, vol=0.002, seed=2),  # trend off
    }
    st = overlay.compute_overlay(closes, leverage=1.0)
    by = {s.symbol: s for s in st.sleeves}
    # On-sleeve: (0.5 + 0.5*1)/2 = 50%. Off-sleeve: 0.5/2 = 25%.
    assert by["SPY"].target_weight_pct == pytest.approx(50.0, abs=0.2)
    assert by["TLT"].target_weight_pct == pytest.approx(25.0, abs=0.2)
    assert st.equity_exposure_pct == pytest.approx(75.0, abs=0.5)
    assert st.cash_pct == pytest.approx(100 - st.equity_exposure_pct, abs=0.01)
    assert st.trend_sleeves_on == 1
    assert st.as_of == closes["SPY"].index[-1].date().isoformat()


def test_leverage_scales_and_clamps():
    closes = {"SPY": _series(0.0008, vol=0.002)}
    base = overlay.compute_overlay(closes, leverage=1.0)
    lev = overlay.compute_overlay(closes, leverage=1.5)
    assert lev.sleeves[0].target_weight_pct == pytest.approx(
        1.5 * base.sleeves[0].target_weight_pct, abs=0.2)
    clamped = overlay.compute_overlay(closes, leverage=9.0)
    assert clamped.leverage == 2.0       # validated range only


def test_evidence_and_params_attached():
    st = overlay.compute_overlay({"SPY": _series(0.0008, vol=0.002)})
    assert st.evidence["source"].endswith("2026-06-08-trend-test.md")
    assert st.params["lookback_days"] == overlay.LOOKBACK_DAYS
    d = st.to_dict()
    assert isinstance(d["sleeves"], list) and d["sleeves"][0]["symbol"] == "SPY"


def test_fetch_overlay_status_uses_injected_fetcher():
    def fake_fetch(sym, start, end):
        return pd.DataFrame({"Close": _series(0.0008, vol=0.002).values},
                            index=_series(0.0008).index)

    st = overlay.fetch_overlay_status(fetch_ohlc=fake_fetch, basket=["SPY", "QQQ"])
    assert len(st.sleeves) == 2
    assert all(s.in_trend for s in st.sleeves)


def test_fetch_overlay_status_raises_without_data():
    with pytest.raises(RuntimeError):
        overlay.fetch_overlay_status(fetch_ohlc=lambda *a: None, basket=["SPY"])
