"""Congressional (politician) trades data vendor.

Fetches U.S. House/Senate financial-disclosure stock transactions for a
ticker. The default backend targets the QuiverQuant public API
(``https://api.quiverquant.com/beta/historical/congresstrading/<TICKER>``),
which aggregates the official House Clerk / Senate disclosures, but the
HTTP layer is fully injectable so tests never require live network.

Design notes
------------
- ``fetch_congress_trades`` accepts an optional ``http_get`` callable. The
  default implementation uses ``requests`` and a ``QUIVER_API_KEY`` bearer
  token. Tests pass a stub callable that returns canned JSON.
- Output is a human-readable Markdown-ish report string, matching the
  contract of the other dataflow vendor functions (which return formatted
  strings consumed by analyst LLMs, then provenance-wrapped at the tool
  layer).
- Analysis only. We surface what elected officials *disclosed*; we never
  place an order.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

# Type of the injectable HTTP getter: (url, headers, params) -> parsed JSON.
HttpGet = Callable[[str, Dict[str, str], Dict[str, Any]], Any]

QUIVER_BASE_URL = "https://api.quiverquant.com/beta/historical/congresstrading"


class CongressTradesError(Exception):
    """Raised when the congressional-trades backend cannot be reached/parsed."""


def _default_http_get(url: str, headers: Dict[str, str], params: Dict[str, Any]) -> Any:
    """Real HTTP fetch via ``requests``. Imported lazily so importing this
    module (e.g. during test collection) doesn't require the network."""
    import requests

    resp = requests.get(url, headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _coerce_records(payload: Any) -> List[Dict[str, Any]]:
    """Normalize the backend payload into a list of record dicts.

    QuiverQuant returns a top-level JSON array; some mirrors wrap it in a
    ``{"data": [...]}`` envelope. Accept both.
    """
    if isinstance(payload, dict):
        for key in ("data", "results", "trades"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return []
    if isinstance(payload, list):
        return payload
    return []


def _cell(value: Any) -> str:
    """Sanitize a value for a Markdown table cell: escape pipes, drop newlines.

    Disclosure feeds are third-party data; a representative name or amount
    field containing ``|`` or a line break must not break the rendered table.
    """
    return str(value or "").replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def _get(record: Dict[str, Any], *keys: str, default: str = "") -> str:
    """Return the first present, case-tolerant key from a record."""
    for k in keys:
        for candidate in (k, k.lower(), k.capitalize(), k.title()):
            if candidate in record and record[candidate] not in (None, ""):
                return str(record[candidate])
    return default


def fetch_congress_trades(
    ticker: str,
    *,
    limit: int = 50,
    api_key: Optional[str] = None,
    http_get: Optional[HttpGet] = None,
    base_url: str = QUIVER_BASE_URL,
) -> List[Dict[str, Any]]:
    """Fetch raw congressional-trade records for a ticker.

    Args:
        ticker: Stock ticker symbol.
        limit: Max records to return (most recent first when dated).
        api_key: Bearer token. Defaults to the ``QUIVER_API_KEY`` env var.
        http_get: Injectable HTTP getter (url, headers, params) -> JSON. When
            omitted, a ``requests``-based default is used. Tests inject a stub.
        base_url: API base; the ticker is appended as a path segment.

    Returns:
        A list of normalized record dicts with keys: representative, chamber,
        transaction, ticker, amount, transaction_date, disclosure_date, party.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return []

    getter = http_get or _default_http_get
    key = api_key if api_key is not None else os.getenv("QUIVER_API_KEY", "")
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    url = f"{base_url.rstrip('/')}/{ticker}"
    try:
        payload = getter(url, headers, {})
    except Exception as e:  # noqa: BLE001
        raise CongressTradesError(f"Failed to fetch congress trades for {ticker}: {e}") from e

    records = _coerce_records(payload)
    normalized = [
        {
            "representative": _get(r, "Representative", "representative", "name", "Member"),
            "chamber": _get(r, "Chamber", "chamber", "House") or "",
            "transaction": _get(r, "Transaction", "transaction", "Type", "type"),
            "ticker": _get(r, "Ticker", "ticker") or ticker,
            "amount": _get(r, "Amount", "amount", "Range", "range"),
            "transaction_date": _get(r, "TransactionDate", "transaction_date", "Traded", "Date", "date"),
            "disclosure_date": _get(r, "ReportDate", "disclosure_date", "Filed", "Disclosed"),
            "party": _get(r, "Party", "party"),
        }
        for r in records
        if isinstance(r, dict)
    ]

    def _sort_key(rec: Dict[str, Any]):
        raw = rec.get("transaction_date", "")
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return datetime.min

    normalized.sort(key=_sort_key, reverse=True)
    return normalized[: max(0, limit)]


def get_congress_trades(ticker: str, limit: int = 50, **kwargs) -> str:
    """Vendor entry point: return a formatted congressional-trades report.

    Matches the string-returning contract of the other dataflow vendor
    functions (e.g. ``get_insider_transactions``). Extra kwargs
    (``api_key``, ``http_get``, ``base_url``) are forwarded to
    :func:`fetch_congress_trades`, which keeps the HTTP layer injectable.
    """
    ticker = (ticker or "").strip().upper()
    try:
        records = fetch_congress_trades(ticker, limit=limit, **kwargs)
    except CongressTradesError as e:
        return f"## Congressional Trades for {ticker}\n\nData unavailable: {e}"

    if not records:
        return (
            f"## Congressional Trades for {ticker}\n\n"
            "No disclosed congressional transactions found for this ticker."
        )

    buys = sum(1 for r in records if "buy" in r["transaction"].lower() or "purchase" in r["transaction"].lower())
    sells = sum(1 for r in records if "sell" in r["transaction"].lower() or "sale" in r["transaction"].lower())

    lines = [
        f"## Congressional Trades for {ticker}",
        "",
        f"Disclosed transactions: {len(records)} "
        f"(purchases: {buys}, sales: {sells})",
        "",
        "| Date | Representative | Chamber | Party | Transaction | Amount |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in records:
        lines.append(
            f"| {_cell(r['transaction_date']) or '?'} | {_cell(r['representative']) or '?'} | "
            f"{_cell(r['chamber']) or '?'} | {_cell(r['party']) or '?'} | "
            f"{_cell(r['transaction']) or '?'} | {_cell(r['amount']) or '?'} |"
        )
    return "\n".join(lines)
