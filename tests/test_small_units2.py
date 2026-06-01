"""Bundle coverage for small pure helpers: asof date parsing, LLM content
normalization, strategy entry validation, disagreement reader, and the
congress-trades fetcher/coercer. All offline."""

from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace

import pytest


# ===========================================================================
# asof._parse_date
# ===========================================================================

def test_parse_date_variants():
    from agenticwhales.asof import _parse_date
    assert _parse_date(None) is None
    assert _parse_date("") is None
    assert _parse_date("garbage") is None
    assert _parse_date(_dt.date(2024, 1, 2)) == _dt.date(2024, 1, 2)
    assert _parse_date(_dt.datetime(2024, 1, 2, 9, 30)) == _dt.date(2024, 1, 2)
    assert _parse_date("2024-01-02") == _dt.date(2024, 1, 2)
    assert _parse_date("20240102") == _dt.date(2024, 1, 2)
    assert _parse_date("2024/01/02") == _dt.date(2024, 1, 2)
    assert _parse_date("2024-01-02T10:00:00Z") == _dt.date(2024, 1, 2)


# ===========================================================================
# base_client.normalize_content
# ===========================================================================

def test_normalize_content_list_blocks():
    from agenticwhales.llm_clients.base_client import normalize_content
    resp = SimpleNamespace(content=[
        {"type": "text", "text": "Hello"},
        {"type": "reasoning", "text": "ignore me"},
        "trailing",
        {"type": "image"},
    ])
    out = normalize_content(resp)
    assert out.content == "Hello\ntrailing"


def test_normalize_content_string_passthrough():
    from agenticwhales.llm_clients.base_client import normalize_content
    resp = SimpleNamespace(content="already a string")
    assert normalize_content(resp).content == "already a string"


# ===========================================================================
# strategy validators
# ===========================================================================

def test_validate_entry_composite_and_price_level():
    from agenticwhales import strategy
    strategy._validate_entry({"kind": "price_level", "level": 100})
    strategy._validate_entry({"kind": "and", "children": [
        {"kind": "price_level", "level": 100},
        {"kind": "price_move", "threshold_pct": 0.03},
    ]})


def test_validate_entry_errors():
    from agenticwhales import strategy
    with pytest.raises(strategy.StrategyError):
        strategy._validate_entry({"kind": "or", "children": []})
    with pytest.raises(strategy.StrategyError):
        strategy._validate_entry({"kind": "price_level"})  # missing level


def test_indicator_names():
    from agenticwhales.strategy import _indicator_names
    assert _indicator_names({"kind": "indicator_cross", "fast": "ema_10", "slow": "ema_50"}) \
        == ["ema_10", "ema_50"]
    composite = {"kind": "and", "children": [
        {"kind": "indicator_cross", "fast": "a", "slow": "b"},
        {"kind": "price_move"},
    ]}
    assert _indicator_names(composite) == ["a", "b"]
    assert _indicator_names("not-a-dict") == []


# ===========================================================================
# disagreement.list_for_user
# ===========================================================================

def test_disagreement_list_for_user_memstore():
    from web import auth
    from agenticwhales import disagreement
    auth._reset_memstore_for_tests()
    try:
        auth._memstore[("disagreement_log", "d1")] = {
            "user_id": "u1", "recorded_at": "2024-01-02", "similarity": 0.9}
        auth._memstore[("disagreement_log", "d2")] = {
            "user_id": "u1", "recorded_at": "2024-01-03", "similarity": 0.5}
        rows = disagreement.list_for_user("u1")
        assert [r["recorded_at"] for r in rows] == ["2024-01-03", "2024-01-02"]
        assert disagreement.list_for_user("nobody") == []
    finally:
        auth._reset_memstore_for_tests()


# ===========================================================================
# congress_trades
# ===========================================================================

def test_coerce_records():
    from agenticwhales.dataflows.congress_trades import _coerce_records
    assert _coerce_records([1, 2]) == [1, 2]
    assert _coerce_records({"data": [1]}) == [1]
    assert _coerce_records({"results": [2]}) == [2]
    assert _coerce_records({"nothing": 1}) == []
    assert _coerce_records("string") == []


def test_fetch_congress_trades_normalizes():
    from agenticwhales.dataflows.congress_trades import fetch_congress_trades
    records = [{"Representative": "Jane Doe", "Chamber": "House",
                "Transaction": "Purchase", "Ticker": "AAPL", "Amount": "$1K-$15K",
                "TransactionDate": "2024-01-02", "ReportDate": "2024-01-10",
                "Party": "I"}]
    out = fetch_congress_trades("aapl", http_get=lambda url, headers, params: records)
    assert len(out) == 1
    assert out[0]["representative"] == "Jane Doe" and out[0]["ticker"] == "AAPL"


def test_fetch_congress_trades_empty_ticker():
    from agenticwhales.dataflows.congress_trades import fetch_congress_trades
    assert fetch_congress_trades("") == []


def test_fetch_congress_trades_http_error():
    from agenticwhales.dataflows.congress_trades import (
        fetch_congress_trades, CongressTradesError)

    def _boom(url, headers, params):
        raise RuntimeError("network")
    with pytest.raises(CongressTradesError):
        fetch_congress_trades("AAPL", http_get=_boom)
