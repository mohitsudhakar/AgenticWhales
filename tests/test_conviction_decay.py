"""Tests for conviction decay + macro re-eval gates."""

from __future__ import annotations

import datetime as dt

import pytest

from agenticwhales.conviction_decay import (
    ConvictionPoint,
    MacroDelta,
    decayed_conviction,
    macro_shifted,
    project_timeseries,
)


_NOW = dt.datetime(2026, 5, 19, 12, 0, tzinfo=dt.timezone.utc)


class TestDecay:
    def test_zero_age_no_decay(self):
        assert decayed_conviction(8.0, _NOW, now=_NOW) == 8.0

    def test_half_life_halves(self):
        recorded = _NOW - dt.timedelta(days=5)
        assert abs(decayed_conviction(8.0, recorded, now=_NOW, half_life_days=5.0) - 4.0) < 1e-9

    def test_two_half_lives_quarters(self):
        recorded = _NOW - dt.timedelta(days=10)
        v = decayed_conviction(8.0, recorded, now=_NOW, half_life_days=5.0)
        assert abs(v - 2.0) < 1e-9

    def test_string_isoformat_accepted(self):
        v = decayed_conviction(10.0, "2026-05-14T12:00:00Z", now=_NOW, half_life_days=5.0)
        assert abs(v - 5.0) < 1e-9

    def test_unix_timestamp_accepted(self):
        recorded_ts = (_NOW - dt.timedelta(days=5)).timestamp()
        v = decayed_conviction(8.0, recorded_ts, now=_NOW, half_life_days=5.0)
        assert abs(v - 4.0) < 1e-9

    def test_clock_skew_clamped_to_zero(self):
        future = _NOW + dt.timedelta(days=2)
        assert decayed_conviction(7.0, future, now=_NOW) == 7.0

    def test_half_life_zero_disables_decay(self):
        recorded = _NOW - dt.timedelta(days=100)
        assert decayed_conviction(9.0, recorded, now=_NOW, half_life_days=0) == 9.0

    def test_clamped_to_ten(self):
        assert decayed_conviction(15.0, _NOW, now=_NOW) == 10.0


class TestMacroGate:
    def test_no_data_does_not_fire(self):
        assert not macro_shifted(MacroDelta())

    def test_spy_sigma_below_threshold(self):
        assert not macro_shifted(MacroDelta(spy_sigma=1.5))

    def test_spy_sigma_above_threshold(self):
        assert macro_shifted(MacroDelta(spy_sigma=2.5))

    def test_spy_negative_sigma_also_fires(self):
        assert macro_shifted(MacroDelta(spy_sigma=-3.0))

    def test_vix_above_threshold(self):
        assert macro_shifted(MacroDelta(vix_delta=6.0))

    def test_either_threshold_sufficient(self):
        assert macro_shifted(MacroDelta(spy_sigma=2.5, vix_delta=1.0))
        assert macro_shifted(MacroDelta(spy_sigma=0.5, vix_delta=8.0))

    def test_custom_thresholds(self):
        assert macro_shifted(MacroDelta(spy_sigma=1.0),
                              spy_sigma_threshold=0.5,
                              vix_delta_threshold=100)


class TestTimeseries:
    def test_sorted_ascending(self):
        rows = [
            {"recorded_at": (_NOW - dt.timedelta(days=2)).isoformat(),
             "conviction_score": 7.0},
            {"recorded_at": (_NOW - dt.timedelta(days=5)).isoformat(),
             "conviction_score": 9.0},
        ]
        points = project_timeseries(rows, now=_NOW, half_life_days=5.0)
        assert len(points) == 2
        assert points[0].ts < points[1].ts

    def test_decay_applied(self):
        rows = [
            {"recorded_at": (_NOW - dt.timedelta(days=5)).isoformat(),
             "conviction_score": 8.0},
        ]
        points = project_timeseries(rows, now=_NOW, half_life_days=5.0)
        assert points[0].raw_score == 8.0
        assert abs(points[0].decayed_score - 4.0) < 1e-9

    def test_drops_rows_without_required_fields(self):
        rows = [
            {"recorded_at": _NOW.isoformat()},  # no score
            {"conviction_score": 5.0},          # no recorded_at
            {"recorded_at": _NOW.isoformat(), "conviction_score": 7.0},
        ]
        points = project_timeseries(rows, now=_NOW)
        assert len(points) == 1

    def test_accepts_created_at_fallback(self):
        rows = [
            {"created_at": _NOW.isoformat(), "conviction_score": 6.0},
        ]
        points = project_timeseries(rows, now=_NOW)
        assert len(points) == 1
