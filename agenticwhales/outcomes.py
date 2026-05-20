"""Decision-outcome resolver — closes the Phase 1 → Phase 2 learning loop.

For every filled `paper_orders` row, this module computes the realized
return after the expected hold period elapsed (or now, whichever is later)
and writes a `decision_outcomes` row capturing the prediction-vs-reality
tuple. Phase 2's calibration head + outcome-predictive memory retriever
both train on this table.

Design notes:
  - Idempotent: re-runs reconcile against the existing row by `paper_order_id`.
  - Pulls latest close from `market_snapshot` (existing dataflow shim) — we
    deliberately don't add a new market-data dependency for Phase 1.
  - "Hit" is defined as `realized_return_pct > 0` for longs and `< 0` for
    shorts (i.e. did the trade make money). This is the binary outcome the
    Brier component scores against `prob_of_profit`.
  - We resolve only orders that are *both* `status='filled'` AND past their
    `expected_hold_days`. Clamped orders count as filled at the clamped
    quantity. Blocked orders are skipped entirely.

Operational shape: today this is a callable function invoked manually or
from a scheduled job (Phase 3 will add an APScheduler nightly trigger).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from .agents.schemas import OrderSide
from .market_snapshot import fetch_snapshot_block

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutcomeRow:
    paper_order_id: str
    user_id: str
    ticker: str
    predicted_return_pct: Optional[float]
    predicted_volatility_pct: Optional[float]
    predicted_prob_of_profit: Optional[float]
    predicted_hold_days: Optional[int]
    realized_return_pct: Optional[float]
    realized_at: Optional[str]  # ISO
    hit: Optional[bool]
    brier_component: Optional[float]


# ---------------------------------------------------------------------------
# Price resolution
# ---------------------------------------------------------------------------

def _parse_snapshot_close(block: str) -> Optional[float]:
    """Best-effort extract of the latest close from the market_snapshot block.

    The block is a directive string the analysts read; format varies by
    provider but consistently includes a 'Latest close' line. This is the
    same heuristic the runner uses for its `_latest_price_for`.
    """
    for line in (block or "").splitlines():
        low = line.lower()
        if "latest close" not in low:
            continue
        try:
            after = line.split(":", 1)[1]
            for tok in after.replace("$", " ").replace(",", " ").split():
                try:
                    return float(tok)
                except ValueError:
                    continue
        except Exception:
            continue
    return None


def _latest_close(ticker: str, as_of_date: str) -> Optional[float]:
    """Look up the latest available close for a ticker at the given date."""
    try:
        block = fetch_snapshot_block(ticker, as_of_date)
    except Exception as exc:
        log.warning("snapshot fetch failed for %s/%s: %s", ticker, as_of_date, exc)
        return None
    return _parse_snapshot_close(block)


# ---------------------------------------------------------------------------
# Outcome computation
# ---------------------------------------------------------------------------

def _is_hit(realized_return_pct: float) -> bool:
    """Did this trade make money?

    `realized_return_pct` is already in trade-PnL terms (sign-flipped for
    shorts in `_resolve_one`), so a hit is simply a positive value.
    """
    return realized_return_pct > 0


def _brier(p: Optional[float], hit: bool) -> Optional[float]:
    """Single-trial Brier component (predicted - actual)^2."""
    if p is None:
        return None
    target = 1.0 if hit else 0.0
    return (float(p) - target) ** 2


def _order_due(order: Dict[str, Any], now: datetime) -> bool:
    """True iff this order has cleared its expected hold period."""
    hold_days = order.get("expected_hold_days")
    if not hold_days:
        # Default to 30 days when the PM didn't say. Phase 2's calibration
        # will tighten this once we have more data.
        hold_days = 30
    try:
        created_at = datetime.fromisoformat(
            str(order["created_at"]).replace("Z", "+00:00"),
        )
    except Exception:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return now >= created_at + timedelta(days=int(hold_days))


def _resolve_one(order: Dict[str, Any], now: datetime) -> Optional[OutcomeRow]:
    """Compute an `OutcomeRow` for one paper_order, or None when not yet due."""
    if order.get("status") != "filled":
        # Skip blocked / clamped-to-zero orders. Clamped-with-fill orders
        # are still `status='clamped'` but represent real fills; we resolve
        # them too.
        if order.get("status") != "clamped":
            return None
        # Clamped-with-fill: qty > 0, fall through.

    if not _order_due(order, now):
        return None

    ticker = order["ticker"]
    fill_price = float(order["fill_price"])
    as_of_date = now.date().isoformat()
    realized_close = _latest_close(ticker, as_of_date)
    if realized_close is None or fill_price <= 0:
        return None

    side = order["side"]
    raw_return = (realized_close - fill_price) / fill_price * 100.0
    # Shorts: the trade profits when price drops. Flip sign.
    if side in (OrderSide.SHORT.value, OrderSide.SELL.value):
        realized_return_pct = -raw_return
    else:
        realized_return_pct = raw_return

    hit = _is_hit(realized_return_pct)
    prob = order.get("prob_of_profit")
    brier = _brier(prob, hit)

    return OutcomeRow(
        paper_order_id=order["id"],
        user_id=order["user_id"],
        ticker=ticker,
        predicted_return_pct=order.get("expected_return_pct"),
        predicted_volatility_pct=order.get("expected_volatility_pct"),
        predicted_prob_of_profit=prob,
        predicted_hold_days=order.get("expected_hold_days"),
        realized_return_pct=realized_return_pct,
        realized_at=now.isoformat(),
        hit=hit,
        brier_component=brier,
    )


def resolve_outcomes_for_user(
    user_id: str,
    *,
    now: Optional[datetime] = None,
    limit: int = 200,
) -> List[OutcomeRow]:
    """Walk recent paper orders for a user; write outcome rows for those due.

    Returns the list of newly-written rows. Idempotent: previously-resolved
    orders short-circuit on the `decision_outcomes.paper_order_id` PK.
    """
    from web import auth

    now = now or datetime.now(tz=timezone.utc)
    orders = auth.list_paper_orders(user_id, limit=limit)
    if not orders:
        return []

    # Index existing outcomes by paper_order_id to skip already-resolved.
    # Always scan the memstore (the source of truth in dev fallback) AND
    # query the DB when configured — the union is the real "already seen" set.
    existing: set = set()
    for (table, _), row in auth._memstore.items():
        if table == "decision_outcomes" and row.get("user_id") == user_id:
            existing.add(row["paper_order_id"])
    if auth._db_writable():
        try:
            rows = auth._select_columns(
                "decision_outcomes",
                filters={"user_id": user_id},
                select="paper_order_id",
            )
            existing.update(r["paper_order_id"] for r in rows)
        except Exception:
            pass

    written: List[OutcomeRow] = []
    for o in orders:
        if o["id"] in existing:
            continue
        outcome = _resolve_one(o, now)
        if outcome is None:
            continue
        _persist(outcome)
        written.append(outcome)
    return written


def _persist(outcome: OutcomeRow) -> None:
    """Idempotent upsert into `decision_outcomes`."""
    from web import auth

    row = {
        "paper_order_id": outcome.paper_order_id,
        "user_id": outcome.user_id,
        "ticker": outcome.ticker,
        "predicted_return_pct": outcome.predicted_return_pct,
        "predicted_volatility_pct": outcome.predicted_volatility_pct,
        "predicted_prob_of_profit": outcome.predicted_prob_of_profit,
        "predicted_hold_days": outcome.predicted_hold_days,
        "realized_return_pct": outcome.realized_return_pct,
        "realized_at": outcome.realized_at,
        "hit": outcome.hit,
        "brier_component": outcome.brier_component,
        "resolved_at": outcome.realized_at,
    }
    # In-memory + Postgres paths both flow through _upsert_columns.
    auth._memstore[("decision_outcomes", outcome.paper_order_id)] = row
    try:
        auth._upsert_columns("decision_outcomes", row, on_conflict="paper_order_id")
    except Exception:
        # In-memory only; nothing more to do.
        pass


# ---------------------------------------------------------------------------
# Aggregate helpers (used by Phase 2 calibration head; harmless in Phase 1)
# ---------------------------------------------------------------------------

def list_outcomes_for_user(
    user_id: str, *, limit: int = 200,
) -> List[Dict[str, Any]]:
    """Convenience reader. Returns the dict form for downstream calibration."""
    from web import auth

    if auth._db_writable():
        try:
            return auth._select_columns(
                "decision_outcomes",
                filters={"user_id": user_id},
                order="resolved_at.desc",
                limit=limit,
            ) or []
        except Exception:
            pass

    rows = [
        row for (t, _), row in auth._memstore.items()
        if t == "decision_outcomes" and row.get("user_id") == user_id
    ]
    rows.sort(key=lambda r: r.get("resolved_at") or "", reverse=True)
    return rows[:limit]


def brier_score(user_id: str, *, limit: int = 200) -> Optional[float]:
    """Mean Brier component across the user's resolved outcomes.

    Returns None when there are no resolved outcomes with a probability.
    """
    rows = list_outcomes_for_user(user_id, limit=limit)
    components = [r["brier_component"] for r in rows if r.get("brier_component") is not None]
    if not components:
        return None
    return sum(float(c) for c in components) / len(components)
