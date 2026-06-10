"""Normalize SnapTrade activities into the coach's `Transaction` model.

A SnapTrade "universal activity" nests the instrument a few ways depending on
brokerage; we extract a ticker robustly and keep only buy/sell trades (the
round-trip reconstruction is long-equity-only for now — options/dividends/
transfers are skipped, flagged for a later extension).
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..transactions.models import Transaction

_BUY = {"BUY", "BOUGHT", "BUYTOOPEN", "BUYTOCLOSE"}
_SELL = {"SELL", "SOLD", "SELLTOOPEN", "SELLTOCLOSE"}


def _ticker(activity: Dict) -> str:
    sym = activity.get("symbol") or activity.get("option_symbol") or {}
    if isinstance(sym, str):
        return sym.upper()
    if isinstance(sym, dict):
        # SnapTrade nests as {"symbol": {"symbol": "AAPL", ...}} or {"raw_symbol": "AAPL"}
        inner = sym.get("symbol")
        if isinstance(inner, dict):
            return str(inner.get("symbol") or inner.get("raw_symbol") or "").upper()
        return str(sym.get("raw_symbol") or inner or sym.get("ticker") or "").upper()
    return ""


def normalize_activity(activity: Dict) -> Optional[Transaction]:
    """One SnapTrade activity -> a `Transaction`, or None if it isn't a trade."""
    raw_type = str(activity.get("type") or activity.get("action") or "").upper().replace(" ", "")
    if raw_type in _BUY:
        ttype = "Buy"
    elif raw_type in _SELL:
        ttype = "Sell"
    else:
        return None  # dividend / transfer / fee / option-expiry — not a round-trip leg

    symbol = _ticker(activity)
    qty = float(activity.get("units") or activity.get("quantity") or 0) or 0.0
    price = float(activity.get("price") or 0) or 0.0
    amount = activity.get("amount")
    if amount is None:
        amount = (-qty * price) if ttype == "Buy" else (qty * price)
    date = str(activity.get("trade_date") or activity.get("settlement_date") or activity.get("date") or "")[:10]
    if not symbol or qty <= 0 or price <= 0:
        return None
    return Transaction(date=date, type=ttype, symbol=symbol,
                       quantity=abs(qty), price=price, amount=float(amount))


def normalize_activities(activities: List[Dict]) -> List[Transaction]:
    out = []
    for a in activities or []:
        try:
            t = normalize_activity(a)
        except Exception:  # noqa: BLE001
            t = None
        if t is not None:
            out.append(t)
    return out
