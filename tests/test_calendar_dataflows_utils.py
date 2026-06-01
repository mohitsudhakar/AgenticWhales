"""Coverage for agenticwhales/calendar.py (market-hours predicate) and
agenticwhales/dataflows/utils.py (small pure helpers). No network: xcal is a
local library; the fallback path is exercised by forcing _HAS_XCAL off.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from agenticwhales import calendar as cal
from agenticwhales.dataflows import utils as dfu


# ===========================================================================
# calendar.derive_exchange
# ===========================================================================

@pytest.mark.parametrize("tickers,expected", [
    (["AAPL"], "XNYS"),
    (["ETH-USD"], cal.CRYPTO_CODE),
    (["BTC-USDT"], cal.CRYPTO_CODE),
    (["USDC-USD"], cal.CRYPTO_CODE),
    (["ES=F"], "XCME"),
    (["EURUSD=X"], cal.CRYPTO_CODE),
    ([], cal.DEFAULT_EXCHANGE),
    (["", "  "], cal.DEFAULT_EXCHANGE),
])
def test_derive_exchange_single_class(tickers, expected):
    assert cal.derive_exchange(tickers) == expected


def test_derive_exchange_mixed_is_most_restrictive():
    # equity present → XNYS wins over futures + crypto
    assert cal.derive_exchange(["AAPL", "ES=F", "BTC-USD"]) == "XNYS"
    # futures over crypto when no equity
    assert cal.derive_exchange(["ES=F", "BTC-USD"]) == "XCME"


# ===========================================================================
# calendar.is_market_open
# ===========================================================================

def test_crypto_always_open():
    assert cal.is_market_open(cal.CRYPTO_CODE) is True


def test_is_market_open_none_defaults_to_crypto_false_path():
    # None exchange code → DEFAULT_EXCHANGE (XNYS), not crypto.
    weekend = datetime(2024, 1, 6, 15, 0, tzinfo=timezone.utc)  # Saturday
    assert cal.is_market_open(None, when=weekend) is False


def test_is_market_open_xcal_weekday_open_close():
    # Tue 2024-01-02 15:00 UTC = 10:00 ET → open; 23:00 UTC = 18:00 ET → closed.
    open_dt = datetime(2024, 1, 2, 15, 0, tzinfo=timezone.utc)
    closed_dt = datetime(2024, 1, 2, 23, 0, tzinfo=timezone.utc)
    assert cal.is_market_open("XNYS", when=open_dt) is True
    assert cal.is_market_open("XNYS", when=closed_dt) is False


def test_is_market_open_xcal_exception_falls_back(monkeypatch):
    class _BadXcal:
        @staticmethod
        def get_calendar(code):
            raise RuntimeError("boom")

    monkeypatch.setattr(cal, "xcal", _BadXcal)
    # Tue 15:00 UTC = 10:00 ET → fallback says open.
    open_dt = datetime(2024, 1, 2, 15, 0, tzinfo=timezone.utc)
    assert cal.is_market_open("XNYS", when=open_dt) is True


def test_is_market_open_without_xcal_uses_fallback(monkeypatch):
    monkeypatch.setattr(cal, "_HAS_XCAL", False)
    weekend = datetime(2024, 1, 6, 15, 0, tzinfo=timezone.utc)  # Saturday
    weekday = datetime(2024, 1, 2, 15, 0, tzinfo=timezone.utc)  # Tue 10:00 ET
    assert cal.is_market_open("XNYS", when=weekend) is False
    assert cal.is_market_open("XNYS", when=weekday) is True


# ===========================================================================
# calendar._fallback_open
# ===========================================================================

def test_fallback_open_weekend_closed():
    assert cal._fallback_open(datetime(2024, 1, 6, 15, 0, tzinfo=timezone.utc)) is False


def test_fallback_open_within_hours():
    # 14:30 UTC = 09:30 ET exactly → open
    assert cal._fallback_open(datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)) is True


def test_fallback_open_after_close():
    # 21:30 UTC = 16:30 ET → closed
    assert cal._fallback_open(datetime(2024, 1, 2, 21, 30, tzinfo=timezone.utc)) is False


def test_fallback_open_naive_datetime():
    # Naive datetime is treated as UTC then converted to ET.
    naive = datetime(2024, 1, 2, 15, 0)  # → 10:00 ET, weekday
    assert cal._fallback_open(naive) is True


# ===========================================================================
# calendar.next_open
# ===========================================================================

def test_next_open_crypto_returns_after():
    after = datetime(2024, 1, 6, 0, 0, tzinfo=timezone.utc)
    assert cal.next_open(cal.CRYPTO_CODE, after) == after


def test_next_open_xcal_returns_future_session():
    after = datetime(2024, 1, 6, 0, 0, tzinfo=timezone.utc)  # Saturday
    nxt = cal.next_open("XNYS", after)
    assert isinstance(nxt, datetime) and nxt > after


def test_next_open_without_xcal_is_none(monkeypatch):
    monkeypatch.setattr(cal, "_HAS_XCAL", False)
    assert cal.next_open("XNYS", datetime(2024, 1, 6, tzinfo=timezone.utc)) is None


def test_next_open_xcal_exception_is_none(monkeypatch):
    class _BadXcal:
        @staticmethod
        def get_calendar(code):
            raise RuntimeError("boom")

    monkeypatch.setattr(cal, "xcal", _BadXcal)
    assert cal.next_open("XNYS", datetime(2024, 1, 6, tzinfo=timezone.utc)) is None


# ===========================================================================
# dataflows/utils.py
# ===========================================================================

def test_save_output_writes_csv(tmp_path, capsys):
    df = pd.DataFrame({"a": [1, 2]})
    out = tmp_path / "x.csv"
    dfu.save_output(df, "TAG", str(out))
    assert out.exists()
    assert "TAG saved to" in capsys.readouterr().out
    back = pd.read_csv(out, index_col=0)
    assert list(back["a"]) == [1, 2]


def test_save_output_no_path_is_noop(tmp_path):
    df = pd.DataFrame({"a": [1]})
    dfu.save_output(df, "TAG", None)  # no path → nothing written, no raise


def test_get_current_date_format():
    s = dfu.get_current_date()
    datetime.strptime(s, "%Y-%m-%d")  # parses → correct format


def test_decorate_all_methods_wraps_callables():
    calls = []

    def deco(fn):
        def inner(*a, **k):
            calls.append(fn.__name__)
            return fn(*a, **k)
        return inner

    @dfu.decorate_all_methods(deco)
    class C:
        def foo(self):
            return "foo"

        def bar(self):
            return "bar"

    c = C()
    assert c.foo() == "foo" and c.bar() == "bar"
    assert "foo" in calls and "bar" in calls


def test_get_next_weekday_from_string_weekday():
    # 2024-01-02 is a Tuesday → unchanged
    assert dfu.get_next_weekday("2024-01-02") == datetime(2024, 1, 2)


def test_get_next_weekday_from_saturday_advances():
    # 2024-01-06 Saturday (weekday 5) → +1 day to Sunday? rule: 7 - 5 = 2 days → Monday
    res = dfu.get_next_weekday("2024-01-06")
    assert res.weekday() == 0  # Monday


def test_get_next_weekday_from_sunday_advances():
    # 2024-01-07 Sunday (weekday 6) → 7 - 6 = 1 day → Monday
    res = dfu.get_next_weekday("2024-01-07")
    assert res.weekday() == 0


def test_get_next_weekday_accepts_datetime():
    dt = datetime(2024, 1, 2, 9, 30)  # Tuesday
    assert dfu.get_next_weekday(dt) == dt
