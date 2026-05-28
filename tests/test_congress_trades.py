"""Unit tests for the congressional-trades dataflow vendor + tool.

All HTTP is mocked via the injectable ``http_get`` callable, so no live
network and no API key are required.
"""

from __future__ import annotations

import pytest

from agenticwhales.dataflows.congress_trades import (
    CongressTradesError,
    fetch_congress_trades,
    get_congress_trades,
)

pytestmark = pytest.mark.unit


_QUIVER_PAYLOAD = [
    {
        "Representative": "Jane Doe",
        "Chamber": "House",
        "Transaction": "Purchase",
        "Ticker": "AAPL",
        "Amount": "$1,001 - $15,000",
        "TransactionDate": "2024-03-10",
        "ReportDate": "2024-04-01",
        "Party": "D",
    },
    {
        "Representative": "John Smith",
        "Chamber": "Senate",
        "Transaction": "Sale",
        "Ticker": "AAPL",
        "Amount": "$15,001 - $50,000",
        "TransactionDate": "2024-05-22",
        "ReportDate": "2024-06-05",
        "Party": "R",
    },
]


def _stub(payload, capture=None):
    def _get(url, headers, params):
        if capture is not None:
            capture["url"] = url
            capture["headers"] = headers
        return payload
    return _get


class TestFetch:
    def test_normalizes_and_sorts_newest_first(self):
        recs = fetch_congress_trades("aapl", http_get=_stub(_QUIVER_PAYLOAD))
        assert len(recs) == 2
        # Sorted by transaction_date descending.
        assert recs[0]["transaction_date"] == "2024-05-22"
        assert recs[0]["representative"] == "John Smith"
        assert recs[0]["transaction"] == "Sale"
        assert recs[1]["transaction_date"] == "2024-03-10"

    def test_ticker_uppercased_in_url(self):
        capture = {}
        fetch_congress_trades("aapl", http_get=_stub(_QUIVER_PAYLOAD, capture))
        assert capture["url"].endswith("/AAPL")

    def test_api_key_becomes_bearer_header(self):
        capture = {}
        fetch_congress_trades("AAPL", api_key="secret123", http_get=_stub([], capture))
        assert capture["headers"]["Authorization"] == "Bearer secret123"

    def test_envelope_payload_accepted(self):
        recs = fetch_congress_trades("AAPL", http_get=_stub({"data": _QUIVER_PAYLOAD}))
        assert len(recs) == 2

    def test_limit_applied(self):
        recs = fetch_congress_trades("AAPL", limit=1, http_get=_stub(_QUIVER_PAYLOAD))
        assert len(recs) == 1
        assert recs[0]["transaction_date"] == "2024-05-22"  # newest kept

    def test_empty_ticker_returns_empty(self):
        assert fetch_congress_trades("", http_get=_stub(_QUIVER_PAYLOAD)) == []

    def test_http_failure_raises_vendor_error(self):
        def _boom(url, headers, params):
            raise ConnectionError("network down")

        with pytest.raises(CongressTradesError):
            fetch_congress_trades("AAPL", http_get=_boom)


class TestReport:
    def test_report_contains_table_and_counts(self):
        report = get_congress_trades("AAPL", http_get=_stub(_QUIVER_PAYLOAD))
        assert "Congressional Trades for AAPL" in report
        assert "purchases: 1" in report
        assert "sales: 1" in report
        assert "Jane Doe" in report
        assert "John Smith" in report
        assert "| Date |" in report  # markdown table header

    def test_report_no_trades(self):
        report = get_congress_trades("ZZZZ", http_get=_stub([]))
        assert "No disclosed congressional transactions" in report

    def test_report_handles_backend_error_gracefully(self):
        def _boom(url, headers, params):
            raise TimeoutError("slow")

        report = get_congress_trades("AAPL", http_get=_boom)
        assert "Data unavailable" in report


class TestRouting:
    def test_route_to_vendor_dispatches_to_quiverquant(self, monkeypatch):
        from agenticwhales.dataflows import interface

        captured = {}

        def _fake(ticker, limit=50, **kwargs):
            captured["ticker"] = ticker
            captured["limit"] = limit
            return f"report for {ticker}"

        monkeypatch.setitem(
            interface.VENDOR_METHODS["get_congress_trades"], "quiverquant", _fake
        )
        out = interface.route_to_vendor("get_congress_trades", "TSLA", 10)
        assert out == "report for TSLA"
        assert captured == {"ticker": "TSLA", "limit": 10}

    def test_category_lookup(self):
        from agenticwhales.dataflows import interface

        assert interface.get_category_for_method("get_congress_trades") == "political_data"


class TestTool:
    def test_tool_wraps_output_in_external_data(self, monkeypatch):
        from agenticwhales.agents.utils import news_data_tools

        monkeypatch.setattr(
            news_data_tools,
            "route_to_vendor",
            lambda method, *a, **k: "## Congressional Trades for AAPL\n\nbody",
        )
        out = news_data_tools.get_congress_trades.invoke({"ticker": "AAPL", "limit": 5})
        assert "<external_data" in out
        assert "congress_trades" in out
        assert "Congressional Trades for AAPL" in out
