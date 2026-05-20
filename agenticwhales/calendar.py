"""Per-instrument market-hours predicate.

Different instruments live on different exchanges with different hours and
holiday schedules. AAPL is NYSE (closed weekends + 11 US holidays), ETH-USD
is 24/7, ES=F futures trade on CME with a different session.

We delegate to the `exchange_calendars` library when available; when it isn't
installed, we fall back to a conservative "NYSE-like 09:30-16:00 ET, Mon-Fri,
no holiday handling" predicate so the rest of the system keeps working in
dev. Production deploys MUST have `exchange_calendars` installed — the
fallback will fire recipes on US holidays.
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime
from typing import Iterable, Optional

try:
    import exchange_calendars as xcal  # type: ignore
    _HAS_XCAL = True
except ImportError:  # pragma: no cover - depends on env
    xcal = None  # type: ignore
    _HAS_XCAL = False

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - python < 3.9
    ZoneInfo = None  # type: ignore

log = logging.getLogger(__name__)

# A pseudo-exchange code for instruments that trade 24/7 (crypto, FX).
CRYPTO_CODE = "CRYPTO"

# Default exchange when none is specified.
DEFAULT_EXCHANGE = "XNYS"


def derive_exchange(tickers: Iterable[str]) -> str:
    """Best-effort default exchange for a list of tickers.

    Heuristic rules (in order):
      - Any '*-USD' or '*-USDT' or '*-USDC' suffix → CRYPTO (24/7).
      - Any '*=F' (futures) → XCME (CME / CBOT cover most of what we'd see).
      - Otherwise → XNYS (US equities; NYSE calendar covers NASDAQ too at
        the day-level granularity we care about for recipe firing).

    A mixed list resolves to the *most-restrictive* calendar: XNYS over XCME
    over CRYPTO. We don't fire a recipe at 03:00 ET just because one ticker
    is BTC; that's almost always a bug.
    """
    has_equity = False
    has_futures = False
    has_crypto = False
    for raw in tickers:
        t = (raw or "").strip().upper()
        if not t:
            continue
        if any(t.endswith(s) for s in ("-USD", "-USDT", "-USDC", "USD=X")):
            has_crypto = True
        elif t.endswith("=F"):
            has_futures = True
        else:
            has_equity = True
    if has_equity:
        return "XNYS"
    if has_futures:
        return "XCME"
    if has_crypto:
        return CRYPTO_CODE
    return DEFAULT_EXCHANGE


def is_market_open(exchange_code: str, when: Optional[datetime] = None) -> bool:
    """True if the given exchange is currently in a regular trading session."""
    exchange_code = (exchange_code or DEFAULT_EXCHANGE).upper()
    if exchange_code == CRYPTO_CODE:
        return True
    now = when or datetime.utcnow()

    if _HAS_XCAL:
        try:
            cal = xcal.get_calendar(exchange_code)
            ts = now if now.tzinfo else now.replace(tzinfo=ZoneInfo("UTC") if ZoneInfo else None)
            return bool(cal.is_open_on_minute(ts, ignore_breaks=False))
        except Exception as e:
            log.warning("calendar lookup failed for %s: %s — falling back", exchange_code, e)

    # Fallback: NYSE-like ET hours, Mon-Fri, NO holiday handling.
    return _fallback_open(now)


def _fallback_open(now: datetime) -> bool:
    """Conservative NYSE-like fallback. ET = UTC-5 (EST) or UTC-4 (EDT).

    We use a fixed UTC-5 offset because the alternative — bundling US-DST rules
    inline — would re-implement a sliver of `exchange_calendars`. The error
    band is one hour for ~7 months of the year; for "should this recipe fire?"
    that's acceptable in a fallback path that we expect production not to take.
    """
    if ZoneInfo:
        try:
            et = ZoneInfo("America/New_York")
            local = now.astimezone(et) if now.tzinfo else now.replace(tzinfo=ZoneInfo("UTC")).astimezone(et)
        except Exception:
            local = now
    else:  # pragma: no cover
        local = now

    if local.weekday() >= 5:  # Sat=5, Sun=6
        return False
    market_open = dtime(9, 30)
    market_close = dtime(16, 0)
    return market_open <= local.time() <= market_close


def next_open(exchange_code: str, after: datetime) -> Optional[datetime]:
    """Next market open after the given datetime, or None if unknown."""
    exchange_code = (exchange_code or DEFAULT_EXCHANGE).upper()
    if exchange_code == CRYPTO_CODE:
        return after
    if not _HAS_XCAL:
        return None
    try:
        cal = xcal.get_calendar(exchange_code)
        ts = after if after.tzinfo else after.replace(tzinfo=ZoneInfo("UTC") if ZoneInfo else None)
        return cal.next_open(ts).to_pydatetime()
    except Exception as e:
        log.warning("calendar next_open failed for %s: %s", exchange_code, e)
        return None
