"""CSV transaction parser into typed :class:`Transaction` models.

CSV is the primary supported input path (pdf.ts used pdfjs and is TS-only,
so it is intentionally not ported — the LLM extraction path in
``extract.py`` covers free-text/PDF-derived text instead).

The parser is tolerant of common brokerage exports (Robinhood and similar):
header names are matched case-insensitively against a set of aliases, and
numeric fields are cleaned of ``$``, commas and parentheses (the latter
meaning a negative accounting value) the same way the TS ``num()`` helper
did. Sign normalization mirrors extract.ts: cash leaving the account (buys,
withdrawals, fees) is negative; cash entering (sells, dividends, deposits,
interest) is positive.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from typing import Iterable, List, Optional

from .models import Transaction

# Header aliases -> canonical field. Lowercased, stripped for matching.
_FIELD_ALIASES = {
    "date": "date",
    "activity date": "date",
    "trade date": "date",
    "process date": "date",
    "settle date": "date",
    "transaction date": "date",
    "type": "type",
    "trans code": "type",
    "transaction": "type",
    "transaction type": "type",
    "activity": "type",
    "action": "type",
    "symbol": "symbol",
    "ticker": "symbol",
    "instrument": "symbol",
    "description": "description",
    "desc": "description",
    "quantity": "quantity",
    "qty": "quantity",
    "shares": "quantity",
    "price": "price",
    "unit price": "price",
    "amount": "amount",
    "total": "amount",
    "net amount": "amount",
    "value": "amount",
}

# Maps a free-text transaction code/word to a canonical type label
# (same vocabulary the LLM extraction prompt uses).
_TYPE_PATTERNS = [
    (re.compile(r"\boption", re.IGNORECASE), None),  # handled below w/ buy/sell
    (re.compile(r"buy|bought|\bbtc\b|\bboc\b", re.IGNORECASE), "Buy"),
    (re.compile(r"sell|sold|\bstc\b|\bsoc\b", re.IGNORECASE), "Sell"),
    (re.compile(r"div", re.IGNORECASE), "Dividend"),
    (re.compile(r"deposit|ach in", re.IGNORECASE), "Deposit"),
    (re.compile(r"withdraw|ach out", re.IGNORECASE), "Withdrawal"),
    (re.compile(r"interest", re.IGNORECASE), "Interest"),
    (re.compile(r"fee", re.IGNORECASE), "Fee"),
    (re.compile(r"transfer", re.IGNORECASE), "Transfer"),
]

_NUM_STRIP = re.compile(r"[$,]")
_PAREN = re.compile(r"^\((.*)\)$")
_CASH_IN = re.compile(r"sell|sold|div|deposit|interest|stc|soc", re.IGNORECASE)
_CASH_OUT = re.compile(r"buy|bought|withdraw|fee|btc|boc", re.IGNORECASE)


def _num(v) -> float:
    """Parse a possibly-formatted number. Mirrors TS num()."""
    if isinstance(v, (int, float)):
        return float(v) if v == v and v not in (float("inf"), float("-inf")) else 0.0
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return 0.0
        neg = False
        m = _PAREN.match(s)
        if m:  # (123.45) -> -123.45 accounting notation
            neg = True
            s = m.group(1)
        s = _NUM_STRIP.sub("", s).strip()
        try:
            n = float(s)
        except ValueError:
            return 0.0
        return -n if neg else n
    return 0.0


def _normalize_date(raw: str) -> str:
    """Return ISO yyyy-mm-dd if parseable, else the trimmed raw string."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%b %d, %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _classify_type(raw_type: str, description: str) -> str:
    """Map a raw transaction code to a canonical type label."""
    blob = f"{raw_type} {description}"
    is_option = bool(re.search(r"\boption|\bcall\b|\bput\b", blob, re.IGNORECASE))
    for pat, label in _TYPE_PATTERNS:
        if label is None:
            continue
        if pat.search(blob):
            if is_option and label in ("Buy", "Sell"):
                return f"Option {label}"
            return label
    # Unmatched: keep the source's own type label, or "" when it's blank so
    # genuinely empty rows are dropped by the caller's filter.
    return raw_type.strip()


def _signed_amount(raw_amount, type_label: str, blob: str) -> float:
    """Apply the buys-negative / sells-positive sign convention.

    If the source already carries a sign (e.g. negative for buys), we respect
    its magnitude but re-derive the sign from the activity so that exports
    using unsigned amounts and exports using signed amounts both normalize to
    the same convention.
    """
    val = abs(_num(raw_amount))
    if val == 0:
        return 0.0
    if _CASH_OUT.search(blob) or type_label in ("Buy", "Option Buy", "Withdrawal", "Fee"):
        return -val
    if _CASH_IN.search(blob) or type_label in ("Sell", "Option Sell", "Dividend", "Deposit", "Interest"):
        return val
    # Unknown activity: preserve whatever sign the source provided.
    return _num(raw_amount)


def _resolve_columns(fieldnames: Iterable[str]) -> dict:
    """Map actual CSV headers to canonical field names."""
    mapping = {}
    for col in fieldnames or []:
        key = (col or "").strip().lower()
        if key in _FIELD_ALIASES:
            canonical = _FIELD_ALIASES[key]
            # First matching column wins (don't clobber an earlier date col).
            mapping.setdefault(canonical, col)
    return mapping


def parse_transactions_csv(text: str) -> List[Transaction]:
    """Parse CSV text into a list of :class:`Transaction`.

    Args:
        text: Raw CSV content (including a header row).

    Returns:
        Transactions in file order. Rows with no type, symbol, and a zero
        amount are dropped (matching the TS extraction filter).
    """
    reader = csv.DictReader(io.StringIO(text))
    cols = _resolve_columns(reader.fieldnames or [])
    out: List[Transaction] = []
    for row in reader:
        raw_type = (row.get(cols.get("type", ""), "") or "").strip()
        description = (row.get(cols.get("description", ""), "") or "").strip()
        symbol = (row.get(cols.get("symbol", ""), "") or "").strip().upper()
        date = _normalize_date(row.get(cols.get("date", ""), ""))
        quantity = _num(row.get(cols.get("quantity", ""), ""))
        price = _num(row.get(cols.get("price", ""), ""))

        type_label = _classify_type(raw_type, description)
        blob = f"{raw_type} {description}"
        amount = _signed_amount(row.get(cols.get("amount", ""), ""), type_label, blob)

        txn = Transaction(
            date=date,
            type=type_label,
            symbol=symbol,
            description=description,
            quantity=quantity,
            price=price,
            amount=amount,
        )
        if txn.type or txn.symbol or txn.amount != 0:
            out.append(txn)
    return out


def parse_transactions_csv_file(path: str, encoding: str = "utf-8-sig") -> List[Transaction]:
    """Read a CSV file from disk and parse it. ``utf-8-sig`` strips any BOM."""
    with open(path, "r", encoding=encoding, newline="") as fh:
        return parse_transactions_csv(fh.read())


def normalize_transaction(raw: dict) -> Transaction:
    """Normalize a loosely-typed dict (e.g. from an LLM) into a Transaction.

    Mirrors normalizeTransaction() in extract.ts: stringify/strip text fields,
    uppercase the symbol, coerce numbers.
    """
    return Transaction(
        date=str(raw.get("date", "") or "").strip(),
        type=str(raw.get("type", "Other") or "Other").strip(),
        symbol=str(raw.get("symbol", "") or "").strip().upper(),
        description=str(raw.get("description", "") or "").strip(),
        quantity=_num(raw.get("quantity")),
        price=_num(raw.get("price")),
        amount=_num(raw.get("amount")),
    )


def dedupe(txns: List[Transaction]) -> List[Transaction]:
    """Drop exact-duplicate transactions. Mirrors dedupe() in extract.ts."""
    seen = set()
    out: List[Transaction] = []
    for t in txns:
        key = (t.date, t.type, t.symbol, t.quantity, t.price, t.amount)
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def chunk_text(text: str, size: int) -> List[str]:
    """Split text on line boundaries into <=size chunks. Mirrors chunkText()."""
    if len(text) <= size:
        return [text]
    chunks: List[str] = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > size and cur:
            chunks.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + line
    if cur:
        chunks.append(cur)
    return chunks
