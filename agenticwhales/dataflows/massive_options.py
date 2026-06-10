"""Massive.com (formerly Polygon.io) historical US options data vendor.

The missing piece for the sideways / vol-selling sleeve (see
``docs/reviews/2026-06-08-trend-test.md``): historical option chains with
Greeks + implied vol, including *expired* contracts, plus bulk flat-file
downloads for systematic backtesting. Massive was picked over Finnhub for
the flat files and full OPRA history; ``finnhub_options.py`` is the cheap
alternative.

What this module exposes
------------------------
- ``list_option_contracts``  — reference contracts (incl. expired) for an
  underlying, with cursor pagination.
- ``get_option_daily_bars`` — daily OHLCV aggregates for one option ticker.
- ``get_option_chain_snapshot`` — the live chain with Greeks + IV per
  contract (snapshot only; Massive does not serve *historical* Greeks/IV —
  derive those from prices via ``agenticwhales.options_lab.implied_vol``).
- ``download_flat_file`` / ``load_day_aggregates`` — bulk OPRA day
  aggregates via the S3-compatible flat-file endpoint, cached locally.
- ``build_option_ticker`` / ``parse_option_ticker`` — OCC-style ticker
  helpers (``O:SPY241220P00450000``).

Auth: ``MASSIVE_API_KEY`` env var for REST (``POLYGON_API_KEY`` accepted as
a legacy fallback); ``MASSIVE_S3_ACCESS_KEY_ID`` / ``MASSIVE_S3_SECRET_ACCESS_KEY``
for flat files. See ``.env.example``.

GATED RESEARCH — this vendor backs offline research tools only
(``tools/run_volsell_test.py``). It is deliberately NOT registered in
``dataflows/interface.py`` routing and must not surface in the product UI
until the vol-selling memo shows acceptable tail behaviour.

The HTTP layer is injectable (same pattern as ``congress_trades.py``) so
tests never require live network or a key.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

# Type of the injectable HTTP getter: (url, headers, params) -> parsed JSON.
HttpGet = Callable[[str, Dict[str, str], Dict[str, Any]], Any]

# api.polygon.io remains a working alias post-rebrand; prefer the new domain.
MASSIVE_BASE_URL = "https://api.massive.com"
MASSIVE_S3_ENDPOINT = "https://files.massive.com"
MASSIVE_S3_BUCKET = "flatfiles"

_MAX_PAGES = 50  # pagination safety cap (1000 contracts/page)

_OPTION_TICKER_RE = re.compile(r"^O:(?P<und>[A-Z.]+)(?P<exp>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


class MassiveOptionsError(Exception):
    """Raised when the Massive options backend cannot be reached/parsed."""


def get_api_key() -> str:
    """REST API key from ``MASSIVE_API_KEY`` (legacy ``POLYGON_API_KEY`` accepted)."""
    key = os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY")
    if not key:
        raise MassiveOptionsError(
            "MASSIVE_API_KEY environment variable is not set (see .env.example)."
        )
    return key


def _default_http_get(url: str, headers: Dict[str, str], params: Dict[str, Any]) -> Any:
    """Real HTTP fetch via ``requests``. Imported lazily so importing this
    module (e.g. during test collection) doesn't require the network."""
    import requests

    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# --------------------------------------------------------------------------- #
# OCC-style option tickers
# --------------------------------------------------------------------------- #

def build_option_ticker(underlying: str, expiry: _dt.date, contract_type: str, strike: float) -> str:
    """``("SPY", 2024-12-20, "P", 450.0) -> "O:SPY241220P00450000"``."""
    cp = contract_type.strip().upper()[:1]
    if cp not in ("C", "P"):
        raise ValueError(f"contract_type must be C or P, got {contract_type!r}")
    return f"O:{underlying.strip().upper()}{expiry:%y%m%d}{cp}{int(round(strike * 1000)):08d}"


def parse_option_ticker(ticker: str) -> Dict[str, Any]:
    """Inverse of :func:`build_option_ticker`; raises on malformed tickers."""
    m = _OPTION_TICKER_RE.match(ticker.strip())
    if not m:
        raise ValueError(f"not an option ticker: {ticker!r}")
    exp = _dt.datetime.strptime(m.group("exp"), "%y%m%d").date()
    return {
        "underlying": m.group("und"),
        "expiry": exp,
        "contract_type": m.group("cp"),
        "strike": int(m.group("strike")) / 1000.0,
    }


# --------------------------------------------------------------------------- #
# REST: reference contracts, aggregates, chain snapshot
# --------------------------------------------------------------------------- #

def _paginate(
    url: str,
    params: Dict[str, Any],
    api_key: Optional[str],
    http_get: Optional[HttpGet],
) -> List[Dict[str, Any]]:
    """Follow ``next_url`` cursor pagination, returning concatenated results."""
    getter = http_get or _default_http_get
    headers = {"Authorization": f"Bearer {api_key or get_api_key()}"}
    results: List[Dict[str, Any]] = []
    for _ in range(_MAX_PAGES):
        try:
            payload = getter(url, headers, params)
        except Exception as e:  # noqa: BLE001
            raise MassiveOptionsError(f"Massive request failed for {url}: {e}") from e
        page = payload.get("results") or []
        if not isinstance(page, list):
            raise MassiveOptionsError(f"unexpected Massive payload shape at {url}")
        results.extend(r for r in page if isinstance(r, dict))
        url = payload.get("next_url") or ""
        params = {}  # the cursor URL already encodes the query
        if not url:
            break
    return results


def list_option_contracts(
    underlying: str,
    *,
    as_of: Optional[str] = None,
    expired: Optional[bool] = None,
    contract_type: Optional[str] = None,
    expiration_date: Optional[str] = None,
    expiration_date_gte: Optional[str] = None,
    expiration_date_lte: Optional[str] = None,
    strike_price_gte: Optional[float] = None,
    strike_price_lte: Optional[float] = None,
    limit: int = 1000,
    api_key: Optional[str] = None,
    http_get: Optional[HttpGet] = None,
    base_url: str = MASSIVE_BASE_URL,
) -> List[Dict[str, Any]]:
    """List option contracts (including expired) for an underlying.

    ``as_of`` (yyyy-mm-dd) is the key argument for backtests: the contract
    universe as it existed on that date. Returns the raw reference dicts
    (``ticker``, ``strike_price``, ``expiration_date``, ``contract_type``, ...).
    """
    params: Dict[str, Any] = {
        "underlying_ticker": underlying.strip().upper(),
        "limit": max(1, min(limit, 1000)),
        "sort": "strike_price",
        "order": "asc",
    }
    if as_of:
        params["as_of"] = as_of
    if expired is not None:
        params["expired"] = "true" if expired else "false"
    if contract_type:
        params["contract_type"] = contract_type.lower()
    if expiration_date:
        params["expiration_date"] = expiration_date
    if expiration_date_gte:
        params["expiration_date.gte"] = expiration_date_gte
    if expiration_date_lte:
        params["expiration_date.lte"] = expiration_date_lte
    if strike_price_gte is not None:
        params["strike_price.gte"] = strike_price_gte
    if strike_price_lte is not None:
        params["strike_price.lte"] = strike_price_lte
    url = f"{base_url.rstrip('/')}/v3/reference/options/contracts"
    return _paginate(url, params, api_key, http_get)


def nearest_contract(contracts: List[Dict[str, Any]], strike: float) -> Optional[Dict[str, Any]]:
    """Pick the listed contract whose strike is closest to ``strike``."""
    best, best_dist = None, float("inf")
    for c in contracts:
        try:
            dist = abs(float(c["strike_price"]) - strike)
        except (KeyError, TypeError, ValueError):
            continue
        if dist < best_dist:
            best, best_dist = c, dist
    return best


def get_option_daily_bars(
    option_ticker: str,
    start_date: str,
    end_date: str,
    *,
    api_key: Optional[str] = None,
    http_get: Optional[HttpGet] = None,
    base_url: str = MASSIVE_BASE_URL,
) -> pd.DataFrame:
    """Daily OHLCV bars for one option contract, indexed by date.

    Works for expired contracts — this is the entry/exit price source for
    historical backtests. Returns an empty frame if the contract never traded
    in the window (thin OTM strikes do happen).
    """
    url = (
        f"{base_url.rstrip('/')}/v2/aggs/ticker/{option_ticker}"
        f"/range/1/day/{start_date}/{end_date}"
    )
    rows = _paginate(url, {"adjusted": "true", "sort": "asc", "limit": 5000}, api_key, http_get)
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["t"], unit="ms").dt.normalize()
    df = df.set_index("date").rename(
        columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    return df[["open", "high", "low", "close", "volume"]]


def get_option_chain_snapshot(
    underlying: str,
    *,
    contract_type: Optional[str] = None,
    api_key: Optional[str] = None,
    http_get: Optional[HttpGet] = None,
    base_url: str = MASSIVE_BASE_URL,
) -> List[Dict[str, Any]]:
    """Current chain snapshot with per-contract Greeks + implied vol.

    Snapshot is *live-only*; for historical IV, invert daily bar prices with
    ``options_lab.implied_vol``. Returns normalized dicts: ticker, strike,
    expiry, contract_type, iv, delta/gamma/theta/vega, open_interest, bid/ask,
    day_close.
    """
    params: Dict[str, Any] = {"limit": 250}
    if contract_type:
        params["contract_type"] = contract_type.lower()
    url = f"{base_url.rstrip('/')}/v3/snapshot/options/{underlying.strip().upper()}"
    rows = _paginate(url, params, api_key, http_get)
    out = []
    for r in rows:
        details = r.get("details") or {}
        greeks = r.get("greeks") or {}
        quote = r.get("last_quote") or {}
        day = r.get("day") or {}
        out.append(
            {
                "ticker": details.get("ticker"),
                "strike": details.get("strike_price"),
                "expiry": details.get("expiration_date"),
                "contract_type": (details.get("contract_type") or "")[:1].upper(),
                "iv": r.get("implied_volatility"),
                "delta": greeks.get("delta"),
                "gamma": greeks.get("gamma"),
                "theta": greeks.get("theta"),
                "vega": greeks.get("vega"),
                "open_interest": r.get("open_interest"),
                "bid": quote.get("bid"),
                "ask": quote.get("ask"),
                "day_close": day.get("close"),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Flat files (bulk OPRA day aggregates) — S3-compatible endpoint, local cache
# --------------------------------------------------------------------------- #

def _default_cache_dir() -> Path:
    return Path(
        os.getenv("MASSIVE_CACHE_DIR", "~/.cache/agenticwhales/massive")
    ).expanduser()


def _default_s3_client(endpoint_url: str):
    """boto3 is an optional dependency — only flat-file bulk downloads need it."""
    try:
        import boto3  # noqa: PLC0415 — optional, imported on demand
    except ImportError as e:
        raise MassiveOptionsError(
            "Flat-file downloads need boto3 (`uv add boto3`) or an injected s3_client."
        ) from e
    access = os.getenv("MASSIVE_S3_ACCESS_KEY_ID")
    secret = os.getenv("MASSIVE_S3_SECRET_ACCESS_KEY")
    if not (access and secret):
        raise MassiveOptionsError(
            "MASSIVE_S3_ACCESS_KEY_ID / MASSIVE_S3_SECRET_ACCESS_KEY are not set "
            "(flat-file credentials from the Massive dashboard; see .env.example)."
        )
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
    )


def download_flat_file(
    file_date: _dt.date,
    *,
    asset: str = "us_options_opra",
    dataset: str = "day_aggs_v1",
    cache_dir: Optional[Path] = None,
    s3_client: Any = None,
    endpoint_url: str = MASSIVE_S3_ENDPOINT,
) -> Path:
    """Download one day's flat file (e.g. all OPRA day aggregates) to the cache.

    Idempotent: returns the cached path without re-downloading. The full
    OPRA day-aggs file is ~tens of MB gzipped — cache it once, slice many
    backtests out of it.
    """
    cache = Path(cache_dir) if cache_dir else _default_cache_dir()
    key = f"{asset}/{dataset}/{file_date:%Y}/{file_date:%m}/{file_date:%Y-%m-%d}.csv.gz"
    dest = cache / key
    if dest.exists():
        return dest
    client = s3_client or _default_s3_client(endpoint_url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    try:
        client.download_file(MASSIVE_S3_BUCKET, key, str(tmp))
        tmp.replace(dest)
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise MassiveOptionsError(f"flat-file download failed for {key}: {e}") from e
    return dest


def load_day_aggregates(
    file_date: _dt.date,
    *,
    underlying: Optional[str] = None,
    cache_dir: Optional[Path] = None,
    s3_client: Any = None,
) -> pd.DataFrame:
    """Load one day of OPRA option day-aggregates, optionally filtered to an
    underlying. Columns follow the flat-file schema (ticker, volume, open,
    close, high, low, window_start, transactions)."""
    path = download_flat_file(file_date, cache_dir=cache_dir, s3_client=s3_client)
    with gzip.open(path, "rt") as fh:
        df = pd.read_csv(fh)
    if underlying:
        prefix = f"O:{underlying.strip().upper()}"
        # Exact-underlying match: the char after the prefix must be a digit so
        # "O:SPY" does not also catch "O:SPYG".
        df = df[df["ticker"].str.match(re.escape(prefix) + r"\d{6}[CP]\d{8}$")]
    return df.reset_index(drop=True)
