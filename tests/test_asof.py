"""Tests for the as-of-date look-ahead guard."""

from __future__ import annotations

import datetime as dt

import pytest

from agenticwhales.asof import (
    LookAheadViolation,
    as_of_date,
    assert_as_of,
    bounded_to_as_of,
    current_as_of,
)


class TestContext:
    def test_unset_returns_none(self):
        assert current_as_of() is None

    def test_set_via_string(self):
        with as_of_date("2024-06-01") as bound:
            assert bound == dt.date(2024, 6, 1)
            assert current_as_of() == dt.date(2024, 6, 1)
        assert current_as_of() is None

    def test_set_via_date(self):
        with as_of_date(dt.date(2024, 6, 1)):
            assert current_as_of() == dt.date(2024, 6, 1)

    def test_set_via_datetime(self):
        with as_of_date(dt.datetime(2024, 6, 1, 12, 30)):
            assert current_as_of() == dt.date(2024, 6, 1)

    def test_nesting_restores(self):
        with as_of_date("2024-06-01"):
            with as_of_date("2024-03-01"):
                assert current_as_of() == dt.date(2024, 3, 1)
            assert current_as_of() == dt.date(2024, 6, 1)

    def test_none_unsets(self):
        with as_of_date("2024-06-01"):
            with as_of_date(None):
                assert current_as_of() is None


class TestDecorator:
    def test_no_bound_passes_through(self):
        @bounded_to_as_of()
        def fetch(symbol, start_date, end_date):
            return (symbol, start_date, end_date)

        assert fetch("AAPL", start_date="2024-01-01", end_date="2024-12-31") == \
            ("AAPL", "2024-01-01", "2024-12-31")

    def test_end_at_bound_unchanged(self):
        @bounded_to_as_of()
        def fetch(symbol, start_date, end_date):
            return end_date

        with as_of_date("2024-06-01"):
            assert fetch("AAPL", start_date="2024-01-01", end_date="2024-06-01") == "2024-06-01"

    def test_end_past_bound_truncated(self):
        @bounded_to_as_of()
        def fetch(symbol, start_date, end_date):
            return end_date

        with as_of_date("2024-06-01"):
            assert fetch("AAPL", start_date="2024-01-01", end_date="2024-12-31") == "2024-06-01"

    def test_empty_window_raises(self):
        @bounded_to_as_of()
        def fetch(symbol, start_date, end_date):
            return end_date

        with as_of_date("2024-06-01"):
            with pytest.raises(LookAheadViolation, match="empty"):
                fetch("AAPL", start_date="2024-12-01", end_date="2024-12-31")

    def test_positional_arg(self):
        @bounded_to_as_of(date_arg="end_date", date_arg_pos=2)
        def fetch(symbol, start_date, end_date):
            return end_date

        with as_of_date("2024-06-01"):
            assert fetch("AAPL", "2024-01-01", "2024-12-31") == "2024-06-01"


class TestAssertHelper:
    def test_noop_outside_bound(self):
        assert_as_of("2030-01-01")  # no raise

    def test_raises_when_past(self):
        with as_of_date("2024-06-01"):
            with pytest.raises(LookAheadViolation):
                assert_as_of("2024-06-02")

    def test_ok_when_within(self):
        with as_of_date("2024-06-01"):
            assert_as_of("2024-05-15")  # no raise
            assert_as_of("2024-06-01")  # boundary inclusive
