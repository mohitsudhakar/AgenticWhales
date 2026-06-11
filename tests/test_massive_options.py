"""Unit tests for the Massive.com (ex-Polygon) options dataflow vendor.

All HTTP is mocked via the injectable ``http_get`` callable and the S3 layer
via an injected fake client, so no live network and no API key are required.
"""

from __future__ import annotations

import datetime as _dt
import gzip

import pytest

from agenticwhales.dataflows import massive_options as mo

pytestmark = pytest.mark.unit


# ----------------------------------------------------------- ticker helpers ----

def test_build_and_parse_option_ticker_round_trip():
    t = mo.build_option_ticker("spy", _dt.date(2024, 12, 20), "P", 450.0)
    assert t == "O:SPY241220P00450000"
    parsed = mo.parse_option_ticker(t)
    assert parsed == {
        "underlying": "SPY",
        "expiry": _dt.date(2024, 12, 20),
        "contract_type": "P",
        "strike": 450.0,
    }


def test_fractional_strike_encoding():
    assert mo.build_option_ticker("SPY", _dt.date(2026, 1, 16), "call", 452.5).endswith("C00452500")
    assert mo.parse_option_ticker("O:SPY260116C00452500")["strike"] == 452.5


def test_malformed_ticker_rejected():
    with pytest.raises(ValueError):
        mo.parse_option_ticker("SPY241220P00450000")
    with pytest.raises(ValueError):
        mo.build_option_ticker("SPY", _dt.date(2024, 12, 20), "straddle", 450)


# ------------------------------------------------------------------- auth ----

def test_get_api_key_requires_env(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    with pytest.raises(mo.MassiveOptionsError, match="MASSIVE_API_KEY"):
        mo.get_api_key()


def test_legacy_polygon_key_accepted(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    monkeypatch.setenv("POLYGON_API_KEY", "legacy-key")
    assert mo.get_api_key() == "legacy-key"


# ------------------------------------------------------- contracts + pages ----

def _two_page_stub(capture):
    """First call returns a page with next_url; the cursor call returns page 2."""

    def _get(url, headers, params):
        capture.setdefault("calls", []).append((url, dict(headers), dict(params)))
        if "cursor" in url:
            return {"results": [{"ticker": "O:SPY241220P00455000", "strike_price": 455.0}]}
        return {
            "results": [{"ticker": "O:SPY241220P00450000", "strike_price": 450.0}],
            "next_url": "https://api.massive.com/v3/reference/options/contracts?cursor=abc",
        }

    return _get


def test_list_option_contracts_paginates_and_authenticates():
    capture = {}
    rows = mo.list_option_contracts(
        "spy",
        as_of="2024-11-20",
        expired=True,
        contract_type="put",
        expiration_date="2024-12-20",
        strike_price_gte=440.0,
        strike_price_lte=460.0,
        api_key="k123",
        http_get=_two_page_stub(capture),
    )
    assert [r["strike_price"] for r in rows] == [450.0, 455.0]
    first_url, first_headers, first_params = capture["calls"][0]
    assert first_url.endswith("/v3/reference/options/contracts")
    assert first_headers["Authorization"] == "Bearer k123"
    assert first_params["underlying_ticker"] == "SPY"
    assert first_params["as_of"] == "2024-11-20"
    assert first_params["expired"] == "true"
    assert first_params["contract_type"] == "put"
    assert first_params["expiration_date"] == "2024-12-20"
    assert first_params["strike_price.gte"] == 440.0
    # the cursor URL already encodes the query: no params re-sent, auth kept
    cursor_url, cursor_headers, cursor_params = capture["calls"][1]
    assert "cursor=abc" in cursor_url
    assert cursor_headers["Authorization"] == "Bearer k123"
    assert cursor_params == {}


def test_request_failure_wrapped():
    def _boom(url, headers, params):
        raise RuntimeError("connection refused")

    with pytest.raises(mo.MassiveOptionsError, match="connection refused"):
        mo.list_option_contracts("SPY", api_key="k", http_get=_boom)


def test_nearest_contract():
    contracts = [
        {"strike_price": 440.0},
        {"strike_price": 452.0},
        {"strike_price": 455.0},
        {"strike_price": "garbage"},
    ]
    assert mo.nearest_contract(contracts, 453.0)["strike_price"] == 452.0
    assert mo.nearest_contract([], 453.0) is None


# ---------------------------------------------------------------- daily bars ----

def test_get_option_daily_bars_parses_frame():
    def _stub(url, headers, params):
        assert "/v2/aggs/ticker/O:SPY241220P00450000/range/1/day/" in url
        return {
            "results": [
                {"t": 1718841600000, "o": 1.0, "h": 1.5, "l": 0.9, "c": 1.2, "v": 321},
                {"t": 1718928000000, "o": 1.2, "h": 1.3, "l": 1.0, "c": 1.1, "v": 100},
            ]
        }

    df = mo.get_option_daily_bars(
        "O:SPY241220P00450000", "2024-06-20", "2024-06-25", api_key="k", http_get=_stub
    )
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df["close"].iloc[0] == 1.2
    assert str(df.index[0].date()) == "2024-06-20"


def test_get_option_daily_bars_empty_for_untraded_contract():
    df = mo.get_option_daily_bars(
        "O:SPY241220P00450000", "2024-06-20", "2024-06-25",
        api_key="k", http_get=lambda u, h, p: {"results": []},
    )
    assert df.empty
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]


# ------------------------------------------------------------------ snapshot ----

def test_chain_snapshot_normalizes_greeks_and_iv():
    def _stub(url, headers, params):
        assert url.endswith("/v3/snapshot/options/SPY")
        return {
            "results": [
                {
                    "details": {
                        "ticker": "O:SPY260116P00600000",
                        "strike_price": 600.0,
                        "expiration_date": "2026-01-16",
                        "contract_type": "put",
                    },
                    "greeks": {"delta": -0.16, "gamma": 0.01, "theta": -0.08, "vega": 0.4},
                    "implied_volatility": 0.182,
                    "open_interest": 12000,
                    "last_quote": {"bid": 3.1, "ask": 3.3},
                    "day": {"close": 3.2},
                }
            ]
        }

    rows = mo.get_option_chain_snapshot("spy", api_key="k", http_get=_stub)
    assert rows == [
        {
            "ticker": "O:SPY260116P00600000",
            "strike": 600.0,
            "expiry": "2026-01-16",
            "contract_type": "P",
            "iv": 0.182,
            "delta": -0.16,
            "gamma": 0.01,
            "theta": -0.08,
            "vega": 0.4,
            "open_interest": 12000,
            "bid": 3.1,
            "ask": 3.3,
            "day_close": 3.2,
        }
    ]


# ---------------------------------------------------------------- flat files ----

class _FakeS3:
    """Writes a canned gzipped CSV; counts calls to verify the cache."""

    def __init__(self, csv_text):
        self.csv_text = csv_text
        self.calls = []

    def download_file(self, bucket, key, dest):
        self.calls.append((bucket, key))
        with gzip.open(dest, "wt") as fh:
            fh.write(self.csv_text)


_FLAT_CSV = (
    "ticker,volume,open,close,high,low,window_start,transactions\n"
    "O:SPY241220P00450000,10,1.0,1.2,1.3,0.9,1718928000000000000,5\n"
    "O:SPYG241220P00080000,3,0.5,0.6,0.7,0.4,1718928000000000000,2\n"
)


def test_download_flat_file_caches(tmp_path):
    fake = _FakeS3(_FLAT_CSV)
    date = _dt.date(2024, 6, 20)
    p1 = mo.download_flat_file(date, cache_dir=tmp_path, s3_client=fake)
    p2 = mo.download_flat_file(date, cache_dir=tmp_path, s3_client=fake)
    assert p1 == p2
    assert p1.exists()
    assert fake.calls == [("flatfiles", "us_options_opra/day_aggs_v1/2024/06/2024-06-20.csv.gz")]


def test_load_day_aggregates_filters_exact_underlying(tmp_path):
    fake = _FakeS3(_FLAT_CSV)
    df = mo.load_day_aggregates(
        _dt.date(2024, 6, 20), underlying="SPY", cache_dir=tmp_path, s3_client=fake
    )
    # "SPY" must not also match "SPYG" contracts
    assert list(df["ticker"]) == ["O:SPY241220P00450000"]


def test_flat_file_needs_credentials_without_injected_client(tmp_path, monkeypatch):
    monkeypatch.delenv("MASSIVE_S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("MASSIVE_S3_SECRET_ACCESS_KEY", raising=False)
    with pytest.raises(mo.MassiveOptionsError):
        mo.download_flat_file(_dt.date(2024, 6, 21), cache_dir=tmp_path)
