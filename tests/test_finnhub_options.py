"""Unit tests for the Finnhub option-chain dataflow vendor (mocked HTTP)."""

from __future__ import annotations

import pytest

from agenticwhales.dataflows import finnhub_options as fo

pytestmark = pytest.mark.unit


_PAYLOAD = {
    "code": "SPY",
    "data": [
        {
            "expirationDate": "2026-07-17",
            "options": {
                "CALL": [
                    {
                        "strike": 620.0,
                        "lastPrice": 4.2,
                        "bid": 4.1,
                        "ask": 4.3,
                        "volume": 1000,
                        "openInterest": 5000,
                        "impliedVolatility": 18.5,
                        "delta": 0.31,
                        "gamma": 0.02,
                        "theta": -0.05,
                        "vega": 0.6,
                    }
                ],
                "PUT": [
                    {
                        "strike": 560.0,
                        "lastPrice": 3.0,
                        "bid": 2.9,
                        "ask": 3.1,
                        "volume": 800,
                        "openInterest": 7000,
                        "impliedVolatility": 22.0,
                        "delta": -0.16,
                    }
                ],
            },
        }
    ],
}


def test_chain_is_flattened_and_iv_normalized():
    capture = {}

    def _stub(url, headers, params):
        capture.update(url=url, headers=headers, params=params)
        return _PAYLOAD

    rows = fo.get_option_chain("spy", api_key="fk", http_get=_stub)
    assert capture["url"].endswith("/stock/option-chain")
    assert capture["headers"]["X-Finnhub-Token"] == "fk"
    assert capture["params"] == {"symbol": "SPY"}
    assert len(rows) == 2
    call = next(r for r in rows if r["contract_type"] == "C")
    put = next(r for r in rows if r["contract_type"] == "P")
    assert call["strike"] == 620.0
    assert call["iv"] == pytest.approx(0.185)  # finnhub quotes IV in percent
    assert put["iv"] == pytest.approx(0.22)
    assert put["expiry"] == "2026-07-17"
    assert put["delta"] == -0.16
    assert put["gamma"] is None  # absent fields stay None, not KeyError


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    with pytest.raises(fo.FinnhubOptionsError, match="FINNHUB_API_KEY"):
        fo.get_option_chain("SPY", http_get=lambda u, h, p: _PAYLOAD)


def test_fetch_failure_wrapped():
    def _boom(url, headers, params):
        raise RuntimeError("rate limited")

    with pytest.raises(fo.FinnhubOptionsError, match="rate limited"):
        fo.get_option_chain("SPY", api_key="fk", http_get=_boom)
