"""Integration tests: as-of guard wired into route_to_vendor().

Proves that driving the real dataflow at a historical date is blocked by the
guard — look-ahead is structurally impossible when ``as_of_date`` is set.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest

from agenticwhales.asof import LookAheadViolation, as_of_date, current_as_of, is_strict
from agenticwhales.dataflows.interface import route_to_vendor

AS_OF = dt.date(2024, 6, 1)


# ── helpers ────────────────────────────────────────────────────────────────

def _mock_vendor(*_args, **_kwargs) -> str:
    """Stub that returns the args it was called with so we can inspect them."""
    return "vendor_called"


# ── no as-of → passthrough ─────────────────────────────────────────────────

def test_no_asof_passes_through():
    """Without as_of_date, route_to_vendor is completely transparent."""
    with patch.dict("agenticwhales.dataflows.interface.VENDOR_METHODS",
                    {"get_stock_data": {"yfinance": _mock_vendor}}, clear=True):
        result = route_to_vendor("get_stock_data", "AAPL", "2024-01-01", "2025-06-01")
        assert result == "vendor_called"


# ── truncate future dates ──────────────────────────────────────────────────

def test_truncates_future_end_date():
    """end_date past as-of is silently truncated to as-of."""
    captured = []

    def capture_vendor(*args):
        captured.extend(args)
        return "ok"

    with patch.dict("agenticwhales.dataflows.interface.VENDOR_METHODS",
                    {"get_stock_data": {"yfinance": capture_vendor}}, clear=True):
        with as_of_date(AS_OF):
            route_to_vendor("get_stock_data", "AAPL", "2024-01-01", "2025-06-01")

    # args: (symbol, start_date, end_date) → end_date should be truncated
    assert captured == ["AAPL", "2024-01-01", AS_OF.isoformat()]


def test_truncates_future_curr_date():
    """curr_date past as-of is silently truncated to as-of."""
    captured = []

    def capture_vendor(*args):
        captured.extend(args)
        return "ok"

    with patch.dict("agenticwhales.dataflows.interface.VENDOR_METHODS",
                    {"get_fundamentals": {"yfinance": capture_vendor}}, clear=True):
        with as_of_date(AS_OF):
            route_to_vendor("get_fundamentals", "AAPL", "2025-06-01")

    # args: (ticker, curr_date) → curr_date should be truncated
    assert captured == ["AAPL", AS_OF.isoformat()]


def test_end_date_already_in_bounds_is_unchanged():
    """A date <= as-of passes through untouched."""
    captured = []

    def capture_vendor(*args):
        captured.extend(args)
        return "ok"

    with patch.dict("agenticwhales.dataflows.interface.VENDOR_METHODS",
                    {"get_stock_data": {"yfinance": capture_vendor}}, clear=True):
        with as_of_date(AS_OF):
            route_to_vendor("get_stock_data", "AAPL", "2024-01-01", "2024-05-01")

    assert captured == ["AAPL", "2024-01-01", "2024-05-01"]


# ── strict mode raises ─────────────────────────────────────────────────────

def test_strict_mode_raises():
    """In strict mode, any future date raises LookAheadViolation."""
    with patch.dict("agenticwhales.dataflows.interface.VENDOR_METHODS",
                    {"get_stock_data": {"yfinance": _mock_vendor}}, clear=True):
        with as_of_date(AS_OF, strict=True):
            with pytest.raises(LookAheadViolation, match="strict"):
                route_to_vendor("get_stock_data", "AAPL", "2024-01-01", "2025-06-01")


# ── non-date methods are unaffected ────────────────────────────────────────

@pytest.mark.parametrize("method,args", [
    ("get_congress_trades", ("AAPL", 10)),
    ("get_x_trade_recs", ("@someuser", 5)),
    ("get_insider_transactions", ("AAPL",)),
])
def test_non_date_methods_unchanged(method, args):
    """Methods without date args pass through even with as-of set."""
    captured = []

    def capture_vendor(*vargs):
        captured.extend(vargs)
        return "ok"

    vend = {method: {"yfinance": capture_vendor}}
    with patch.dict("agenticwhales.dataflows.interface.VENDOR_METHODS",
                    vend, clear=True):
        with as_of_date(AS_OF, strict=True):
            route_to_vendor(method, *args)

    assert list(captured) == list(args)


# ── optional / missing date ────────────────────────────────────────────────

def test_null_curr_date_passes_through():
    """When curr_date is None (optional arg), guard is a no-op."""
    captured = []

    def capture_vendor(*args):
        captured.extend(args)
        return "ok"

    with patch.dict("agenticwhales.dataflows.interface.VENDOR_METHODS",
                    {"get_balance_sheet": {"yfinance": capture_vendor}}, clear=True):
        with as_of_date(AS_OF, strict=True):
            route_to_vendor("get_balance_sheet", "AAPL", "quarterly", None)

    assert captured == ["AAPL", "quarterly", None]


# ── end-to-end: guard is actually effective ────────────────────────────────

def test_guard_is_effective_against_historical_replay():
    """The real scenario: replaying Jan 2024 with a 2025 end_date.

    Without the guard the vendor sees 2025 and returns future data.
    With the guard it only sees up to the as-of date.
    """
    captured_end = []

    def capture_vendor(symbol, start, end):
        captured_end.append(end)
        return "data"

    with patch.dict("agenticwhales.dataflows.interface.VENDOR_METHODS",
                    {"get_stock_data": {"yfinance": capture_vendor}}, clear=True):
        with as_of_date("2024-01-15"):
            route_to_vendor("get_stock_data", "AAPL", "2024-01-01", "2024-03-01")

    # The requested end_date was 2024-03-01, which exceeds as_of 2024-01-15.
    # Without the guard the vendor would fetch 2 extra months of future data.
    # With the guard, the end_date is truncated.
    assert captured_end == ["2024-01-15"]
