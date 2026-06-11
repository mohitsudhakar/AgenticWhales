"""Finnhub.io US equity option chains — the cheap alternative to Massive.

Finnhub serves option chains (including archived expired contracts on paid
tiers) with per-contract implied vol and Greeks fields, at $12-100/mo vs
Massive's bulk flat files. Picked as the fallback vendor in
``docs/reviews/2026-06-08-trend-test.md``; ``massive_options.py`` is the
primary adapter for systematic backtesting (bulk history).

Auth: ``FINNHUB_API_KEY`` env var (see ``.env.example``).

GATED RESEARCH — offline research tools only; deliberately NOT registered
in ``dataflows/interface.py`` routing. Must not surface in the product UI
until the vol-selling memo shows acceptable tail behaviour.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

# Type of the injectable HTTP getter: (url, headers, params) -> parsed JSON.
HttpGet = Callable[[str, Dict[str, str], Dict[str, Any]], Any]

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"


class FinnhubOptionsError(Exception):
    """Raised when the Finnhub options backend cannot be reached/parsed."""


def get_api_key() -> str:
    key = os.getenv("FINNHUB_API_KEY")
    if not key:
        raise FinnhubOptionsError(
            "FINNHUB_API_KEY environment variable is not set (see .env.example)."
        )
    return key


def _default_http_get(url: str, headers: Dict[str, str], params: Dict[str, Any]) -> Any:
    import requests

    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_option_chain(
    symbol: str,
    *,
    api_key: Optional[str] = None,
    http_get: Optional[HttpGet] = None,
    base_url: str = FINNHUB_BASE_URL,
) -> List[Dict[str, Any]]:
    """Fetch the option chain for an underlying, normalized to flat dicts.

    Returns one dict per contract: expiry, contract_type ("C"/"P"), strike,
    last, bid, ask, volume, open_interest, iv, delta, gamma, theta, vega.
    IV comes back as a percentage from Finnhub; normalized here to a decimal
    (0.20 == 20 vol) to match ``options_lab`` conventions.
    """
    getter = http_get or _default_http_get
    headers = {"X-Finnhub-Token": api_key or get_api_key()}
    url = f"{base_url.rstrip('/')}/stock/option-chain"
    try:
        payload = getter(url, headers, {"symbol": symbol.strip().upper()})
    except Exception as e:  # noqa: BLE001
        raise FinnhubOptionsError(f"Failed to fetch option chain for {symbol}: {e}") from e

    out: List[Dict[str, Any]] = []
    for expiry_block in (payload or {}).get("data") or []:
        expiry = expiry_block.get("expirationDate")
        options = expiry_block.get("options") or {}
        for side, cp in (("CALL", "C"), ("PUT", "P")):
            for c in options.get(side) or []:
                iv = c.get("impliedVolatility")
                out.append(
                    {
                        "expiry": expiry,
                        "contract_type": cp,
                        "strike": c.get("strike"),
                        "last": c.get("lastPrice"),
                        "bid": c.get("bid"),
                        "ask": c.get("ask"),
                        "volume": c.get("volume"),
                        "open_interest": c.get("openInterest"),
                        "iv": iv / 100.0 if isinstance(iv, (int, float)) else None,
                        "delta": c.get("delta"),
                        "gamma": c.get("gamma"),
                        "theta": c.get("theta"),
                        "vega": c.get("vega"),
                    }
                )
    return out
