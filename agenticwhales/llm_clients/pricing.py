"""Versioned LLM pricing — append-only.

The pricing table is the source of truth for "what did this call cost?"
Pricing rows are append-only: a new effective_at date means a new row, never
an update of an existing row. That way historical cost calculations stay
reproducible.

In prod, the table is `public.llm_pricing` in Postgres (seeded by the schema
migration). In dev/CI we fall back to an in-process snapshot so tests don't
need a database. The local snapshot mirrors the seed in the schema file —
keep them in sync when adding new models.

The bidirectional version-pin: the cost_middleware records `cost_usd` on
every `llm_call_log` row, so even if pricing changes tomorrow, today's spend
attribution stays accurate.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# 5-minute cache for pricing lookups — pricing doesn't change intra-day.
_CACHE_TTL_SEC = 300.0
_cache_lock = threading.RLock()
_cache: Dict[Tuple[str, str], Tuple[float, "PriceRow"]] = {}


@dataclass(frozen=True)
class PriceRow:
    provider: str
    model: str
    input_per_1m: Decimal
    output_per_1m: Decimal
    cache_read_per_1m: Optional[Decimal]
    reasoning_per_1m: Optional[Decimal]
    effective_at: datetime
    source_url: Optional[str] = None


# Local seed — kept in sync with docs/supabase-schema.sql.
# Used in dev/CI when Postgres isn't reachable. Production reads from DB.
_LOCAL_SEED: List[PriceRow] = [
    PriceRow("google",   "gemini-3-flash-preview",
             Decimal("0.075"), Decimal("0.30"), None, None,
             datetime(2026, 1, 1, tzinfo=timezone.utc), "https://ai.google.dev/pricing"),
    PriceRow("google",   "gemini-3.1-pro-preview",
             Decimal("1.25"),  Decimal("10.00"), None, None,
             datetime(2026, 1, 1, tzinfo=timezone.utc), "https://ai.google.dev/pricing"),
    PriceRow("deepseek", "deepseek-v4",
             Decimal("0.27"),  Decimal("1.10"),  None, None,
             datetime(2026, 1, 1, tzinfo=timezone.utc),
             "https://api-docs.deepseek.com/quick_start/pricing"),
    PriceRow("openai",   "gpt-5.4-mini",
             Decimal("0.15"),  Decimal("0.60"),  None, None,
             datetime(2026, 1, 1, tzinfo=timezone.utc), "https://openai.com/api/pricing/"),
    PriceRow("openai",   "gpt-5.4",
             Decimal("2.50"),  Decimal("10.00"), None, None,
             datetime(2026, 1, 1, tzinfo=timezone.utc), "https://openai.com/api/pricing/"),
    PriceRow("anthropic","claude-4.6-haiku",
             Decimal("0.80"),  Decimal("4.00"),  None, None,
             datetime(2026, 1, 1, tzinfo=timezone.utc), "https://docs.anthropic.com/pricing"),
    PriceRow("anthropic","claude-4.6-sonnet",
             Decimal("3.00"),  Decimal("15.00"), None, None,
             datetime(2026, 1, 1, tzinfo=timezone.utc), "https://docs.anthropic.com/pricing"),
]


def _normalize(provider: str, model: str) -> Tuple[str, str]:
    return (provider or "").strip().lower(), (model or "").strip()


def _row_for(provider: str, model: str, at: Optional[datetime] = None) -> Optional[PriceRow]:
    """Return the active price row for (provider, model) at the given time.

    Picks the row with the largest `effective_at` that is <= `at`. Reads from
    Postgres when available; falls back to the local seed.

    Cache strategy: we cache the "current price" (at=None) result for 5 min,
    but historical lookups (`at` set explicitly) always bypass the cache
    because they're used for reconstructing past cost — accuracy beats speed,
    and historical lookups are rare.
    """
    key = _normalize(provider, model)
    if at is None:
        now = time.time()
        with _cache_lock:
            cached = _cache.get(key)
            if cached and cached[0] > now:
                return cached[1]
        row = _fetch_from_db(*key, at=None) or _from_seed(*key, at=None)
        if row is not None:
            with _cache_lock:
                _cache[key] = (now + _CACHE_TTL_SEC, row)
        return row

    # Historical lookup — bypass cache.
    return _fetch_from_db(*key, at=at) or _from_seed(*key, at=at)


def _from_seed(provider: str, model: str, at: Optional[datetime]) -> Optional[PriceRow]:
    target = at or datetime.now(tz=timezone.utc)
    candidates = [
        r for r in _LOCAL_SEED
        if r.provider == provider and r.model == model and r.effective_at <= target
    ]
    return max(candidates, key=lambda r: r.effective_at) if candidates else None


def _fetch_from_db(provider: str, model: str, at: Optional[datetime]) -> Optional[PriceRow]:
    """Pull the active price row from Postgres. Returns None on any failure."""
    try:
        from web import auth  # lazy import; avoids cycle at module load
        return auth.fetch_price_row(provider, model, at)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("pricing DB lookup failed (%s/%s): %s", provider, model, exc)
        return None


def cost_for(
    provider: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_tokens: int = 0,
    cache_read_tokens: int = 0,
    at: Optional[datetime] = None,
) -> Decimal:
    """Compute the dollar cost of an LLM call at the active price.

    Raises `ValueError` if the (provider, model) pair has no pricing — silent
    zero-cost would corrupt budget enforcement.
    """
    row = _row_for(provider, model, at=at)
    if row is None:
        raise ValueError(f"no pricing for {provider}/{model} at {at}")

    cost = (
        (Decimal(max(input_tokens, 0)) * row.input_per_1m) +
        (Decimal(max(output_tokens, 0)) * row.output_per_1m)
    )
    if reasoning_tokens and row.reasoning_per_1m is not None:
        cost += Decimal(max(reasoning_tokens, 0)) * row.reasoning_per_1m
    elif reasoning_tokens:
        # Reasoning priced as output when no dedicated rate is set.
        cost += Decimal(max(reasoning_tokens, 0)) * row.output_per_1m
    if cache_read_tokens and row.cache_read_per_1m is not None:
        cost += Decimal(max(cache_read_tokens, 0)) * row.cache_read_per_1m
    return cost / Decimal(1_000_000)


def clear_cache() -> None:
    """Test helper: drop the in-process pricing cache."""
    with _cache_lock:
        _cache.clear()


def known_models() -> List[Tuple[str, str]]:
    """Return the (provider, model) pairs we have pricing for. Seed + DB merged."""
    seen = {(r.provider, r.model) for r in _LOCAL_SEED}
    try:
        from web import auth
        for prov, mdl in auth.list_priced_models():
            seen.add((prov, mdl))
    except Exception:
        pass
    return sorted(seen)
