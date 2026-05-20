"""Paper-trading engine — account, positions, sizing, order placement.

This module owns:
  - Per-user `PaperAccount` + `PaperPosition` state read/write
  - Kelly-flavored position sizing from `PortfolioDecision` scalars
  - `place_order(...)` — atomic order placement (via Postgres RPC in prod,
    Python fallback in dev) that updates positions + cash + realized PnL in
    one logical transaction
  - Conviction-score derivation from `PortfolioDecision`
  - Idempotent legacy `portfolio.json` import on first touch
  - Helper to produce a position block for LLM prompts (replaces the previous
    `portfolio.format_for_prompt` JSON-backed path)

Direction of paper orders is set by rating sign; magnitude comes from
quarter-Kelly (or deci-Kelly, until calibration data exists). The 5-tier
rating no longer drives sizing — see plan §1.4.3 / Demis review D3.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .agents.schemas import (
    GuardOutcome,
    ImpersonationToken,
    OrderSide,
    OrderStatus,
    PaperAccount,
    PaperPosition,
    PortfolioDecision,
    PortfolioRating,
)

log = logging.getLogger(__name__)

DEFAULT_STARTING_CASH = 100_000.0
SECONDS_PER_DAY = 86_400.0

# Direction multiplier per rating. Magnitude (size) is set by Kelly; this is
# purely sign. Hold = 0 (no trade).
_RATING_DIRECTION: Dict[PortfolioRating, int] = {
    PortfolioRating.BUY: 1,
    PortfolioRating.OVERWEIGHT: 1,
    PortfolioRating.HOLD: 0,
    PortfolioRating.UNDERWEIGHT: -1,
    PortfolioRating.SELL: -1,
}


# ---------------------------------------------------------------------------
# Sizing — Kelly-flavored, decoupled from rating magnitude
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SizingResult:
    """Output of `kelly_sizing`. `qty` is signed, in shares/units. `fraction`
    is the final Kelly fraction applied (post-cap), positive even for shorts.
    """

    qty: float
    fraction: float
    direction: int  # -1, 0, +1


def kelly_sizing(
    decision: PortfolioDecision,
    nav: float,
    last_price: float,
    kelly_fraction_cap: float = 0.10,
    user_id: Optional[str] = None,
) -> SizingResult:
    """Compute a Kelly-flavored target quantity for the decision.

    Kelly bet sizing: f* = (p*(b+1) - 1) / b, where:
      p = probability of profit
      b = expected reward / expected loss (R:R, in payoff terms)

    We apply a fractional Kelly multiplier (`kelly_fraction_cap`) to dampen
    over-betting under uncalibrated probability inputs. Default cap = 0.10
    (deci-Kelly) — tightened from quarter-Kelly until Phase 2 calibration
    data lets us safely loosen.

    Returns qty=0 on:
      - rating == Hold
      - missing required scalars on the decision
      - non-positive last_price / nav
      - degenerate Kelly fraction (f* <= 0)
    """
    direction = _RATING_DIRECTION.get(decision.rating, 0)
    if direction == 0:
        return SizingResult(qty=0.0, fraction=0.0, direction=0)

    if nav <= 0 or last_price <= 0:
        return SizingResult(qty=0.0, fraction=0.0, direction=direction)

    raw_p = decision.prob_of_profit
    er = decision.expected_return_pct
    if raw_p is None or er is None:
        return SizingResult(qty=0.0, fraction=0.0, direction=direction)

    # Phase 2 deliverable #3: when the user has opted in to their
    # calibration head, fold their per-user Platt scaling into the raw
    # probability before sizing. Pass-through when no user_id is supplied
    # (lets unit tests size without spinning up the storage layer).
    if user_id:
        from . import calibration as _cal
        p = _cal.apply_if_opted_in(user_id, raw_p) or raw_p
    else:
        p = raw_p

    # The "loss leg" we feed Kelly is the expected downside if the trade is
    # stopped out. Priority order (Demis review D8):
    #   1. Explicit stop_loss price (the PM's own invalidation level) —
    #      converted to a pct vs current price. This is the truthful, asymmetric
    #      measure of risk. Volatility is symmetric and over-estimates downside.
    #   2. expected_volatility_pct — symmetric proxy. Conservative when stop is
    #      missing but inflates `b`, leading to under-betting. Acceptable
    #      fallback.
    #   3. |expected_return_pct| — last resort (b=1). Practically equivalent to
    #      flat-coin sizing.
    expected_loss: Optional[float] = None
    if decision.stop_loss is not None and last_price > 0:
        # Long: stop is below entry. Loss leg = (entry - stop) / entry.
        # Short: stop is above entry. Loss leg = (stop - entry) / entry.
        # Direction sign already captured by `direction`; the loss leg is
        # always positive.
        if direction > 0 and decision.stop_loss < last_price:
            expected_loss = (last_price - decision.stop_loss) / last_price * 100.0
        elif direction < 0 and decision.stop_loss > last_price:
            expected_loss = (decision.stop_loss - last_price) / last_price * 100.0
        # A malformed stop (above current for a long, below for a short) is
        # discarded — fall through to vol.
    if not expected_loss or expected_loss <= 0:
        expected_loss = decision.expected_volatility_pct or abs(er)
    if expected_loss <= 0:
        return SizingResult(qty=0.0, fraction=0.0, direction=direction)

    # Kelly's `b` is win-payoff / loss-payoff. Use absolute magnitudes —
    # direction is already captured by the rating sign.
    b = abs(er) / expected_loss
    if b <= 0:
        return SizingResult(qty=0.0, fraction=0.0, direction=direction)

    p_clamped = max(0.0, min(1.0, p))
    f_star = (p_clamped * (b + 1) - 1) / b
    if f_star <= 0:
        return SizingResult(qty=0.0, fraction=0.0, direction=direction)

    fraction = min(f_star * kelly_fraction_cap, 1.0)
    dollars = nav * fraction
    qty = dollars / last_price
    return SizingResult(qty=qty * direction, fraction=fraction, direction=direction)


def score_from_decision(decision: PortfolioDecision) -> int:
    """Derive an integer conviction score in [1, 10] from PM scalars.

    Sharpe-flavored: `prob_of_profit * (expected_return / expected_volatility)`,
    bucketed into 10 bins. Falls back to a coarse rating-based score when the
    scalars are missing — this is the only place where the rating is allowed
    to influence conviction, and it's a fallback, not the primary path.

    The score is informational (UI / threshold check), not a sizing input.
    """
    p = decision.prob_of_profit
    er = decision.expected_return_pct
    ev = decision.expected_volatility_pct
    if p is not None and er is not None and ev is not None and ev > 0:
        signal = p * (abs(er) / ev)
        # Map [0, 0.6] → [1, 10] linearly, clamped.
        normalized = max(0.0, min(1.0, signal / 0.6))
        return max(1, min(10, int(round(1 + normalized * 9))))

    fallback = {
        PortfolioRating.BUY:         9,
        PortfolioRating.OVERWEIGHT:  7,
        PortfolioRating.HOLD:        3,
        PortfolioRating.UNDERWEIGHT: 6,
        PortfolioRating.SELL:        8,
    }
    return fallback.get(decision.rating, 5)


# ---------------------------------------------------------------------------
# Slippage
# ---------------------------------------------------------------------------

def apply_slippage(side: OrderSide, market_price: float, slippage_bps: int) -> float:
    """Adjust a fill price for symmetric basis-point slippage.

    Buys / covers pay up; sells / shorts receive down. 1 bps = 0.01%.
    """
    if market_price <= 0 or slippage_bps <= 0:
        return market_price
    delta = market_price * (slippage_bps / 10_000.0)
    if side in (OrderSide.BUY, OrderSide.COVER):
        return market_price + delta
    return market_price - delta


# ---------------------------------------------------------------------------
# Place order — atomic in prod via RPC, Python-side fallback in dev
# ---------------------------------------------------------------------------

@dataclass
class PlaceOrderResult:
    order_id: str
    status: OrderStatus
    fill_price: float
    qty: float
    kelly_fraction: float
    idempotent: bool = False


def place_order(
    token: ImpersonationToken,
    *,
    fire_id: str,
    session_id: Optional[str],
    recipe_id: Optional[str],
    ticker: str,
    side: OrderSide,
    qty: float,
    market_price: float,
    slippage_bps: int,
    decision: PortfolioDecision,
    conviction: int,
    kelly_fraction: float,
    guard: GuardOutcome,
) -> PlaceOrderResult:
    """Write a paper order row + update positions + adjust cash, atomically.

    Idempotent on `(user_id, fire_id, ticker, side)` — a duplicate call with
    the same fire_id returns the existing order rather than double-writing.
    This makes the scheduler safe to retry without corrupting the books.

    In prod the entire flow runs inside a Postgres `paper_place_order` RPC;
    in dev fallback we run the same logic in Python with a per-user lock
    (best-effort — not crash-safe). The Python fallback is gated on
    `web.auth._db_writable()` being false, so prod always uses the RPC.
    """
    from web import auth  # lazy import; avoids cycle

    side_value = side.value if isinstance(side, OrderSide) else str(side)
    fill_price = apply_slippage(side, market_price, slippage_bps)
    order_id = uuid.uuid4().hex

    if not guard.allowed and guard.allowed_qty <= 0:
        # Blocked: write a 'blocked' order row for audit + UI, no position change.
        status = OrderStatus.BLOCKED
        write_qty = abs(qty)
    elif guard.allowed_qty < abs(qty):
        # Partial clamp: status='clamped', actual qty = guard.allowed_qty.
        status = OrderStatus.CLAMPED
        write_qty = guard.allowed_qty
    else:
        status = OrderStatus.FILLED
        write_qty = abs(qty)

    payload = {
        "id": order_id,
        "user_id": token.user_id,
        "session_id": session_id,
        "recipe_id": recipe_id,
        "fire_id": fire_id,
        "ticker": ticker.upper(),
        "side": side_value,
        "qty": write_qty,
        "fill_price": fill_price,
        "slippage_bps": slippage_bps,
        "gross_value": write_qty * fill_price,
        "pm_rating": decision.rating.value if hasattr(decision.rating, "value") else str(decision.rating),
        "conviction_score": conviction,
        "expected_return_pct": decision.expected_return_pct,
        "expected_volatility_pct": decision.expected_volatility_pct,
        "prob_of_profit": decision.prob_of_profit,
        "expected_hold_days": decision.expected_hold_days,
        "kelly_fraction": kelly_fraction,
        "status": status.value,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }

    # Idempotency check — both paths.
    existing = auth.find_paper_order_idem(token.user_id, fire_id, ticker.upper(), side_value)
    if existing:
        return PlaceOrderResult(
            order_id=existing["id"],
            status=OrderStatus(existing["status"]),
            fill_price=float(existing["fill_price"]),
            qty=float(existing["qty"]),
            kelly_fraction=float(existing.get("kelly_fraction") or 0.0),
            idempotent=True,
        )

    # Try the atomic Postgres RPC first (Phase 1.5). On success it returns
    # `{order_id, idempotent}`; on failure (RPC not installed, transport error,
    # etc.) we fall through to the Python implementation. The RPC path is
    # crash-safe — insert + position + cash all commit atomically.
    rpc_payload = {
        "p_user_id": token.user_id,
        "p_fire_id": fire_id,
        "p_recipe_id": recipe_id,
        "p_session_id": session_id,
        "p_ticker": ticker.upper(),
        "p_side": side_value,
        "p_qty": write_qty,
        "p_fill_price": fill_price,
        "p_slippage_bps": slippage_bps,
        "p_pm_rating": payload["pm_rating"],
        "p_conviction": conviction,
        "p_expected_return_pct": decision.expected_return_pct,
        "p_expected_volatility_pct": decision.expected_volatility_pct,
        "p_prob_of_profit": decision.prob_of_profit,
        "p_expected_hold_days": decision.expected_hold_days,
        "p_kelly_fraction": kelly_fraction,
        "p_status": status.value,
    }
    rpc_result = auth.call_paper_place_order_rpc(rpc_payload)
    if rpc_result is not None:
        # RPC succeeded — books are now committed in a single Postgres
        # transaction. Mirror to the in-memory store too so dev/test reads
        # against `_memstore` keep working.
        auth.insert_paper_order(payload)
        if status in (OrderStatus.FILLED, OrderStatus.CLAMPED):
            _mirror_fill_to_memstore(token.user_id, ticker.upper(), side, write_qty, fill_price)
        return PlaceOrderResult(
            order_id=rpc_result.get("order_id", order_id),
            status=status,
            fill_price=fill_price,
            qty=write_qty,
            kelly_fraction=kelly_fraction,
            idempotent=bool(rpc_result.get("idempotent")),
        )

    # Fallback Python flow (dev / RPC not installed). Same semantics, three
    # separate REST calls — NOT atomic but matches Phase 1 behaviour exactly.
    auth.insert_paper_order(payload)
    if status in (OrderStatus.FILLED, OrderStatus.CLAMPED):
        _apply_fill_python(token.user_id, ticker.upper(), side, write_qty, fill_price)

    return PlaceOrderResult(
        order_id=order_id,
        status=status,
        fill_price=fill_price,
        qty=write_qty,
        kelly_fraction=kelly_fraction,
    )


def _mirror_fill_to_memstore(
    user_id: str, ticker: str, side: OrderSide, qty: float, fill_price: float,
) -> None:
    """After the RPC commits in Postgres, sync the in-memory `_memstore` so
    reads via the dev fallback see consistent state. The RPC is the source of
    truth in prod; this is purely cosmetic for local dev + tests."""
    _apply_fill_python(user_id, ticker, side, qty, fill_price)


def _apply_fill_python(
    user_id: str,
    ticker: str,
    side: OrderSide,
    qty: float,
    fill_price: float,
) -> None:
    """Apply a filled order to positions + cash. Python fallback path.

    Long math:
      Buy: avg_cost = (existing_qty*avg + qty*fill) / (existing_qty + qty);
           qty += qty; cash -= qty*fill
      Sell: realized_pnl += (fill - avg_cost) * sell_qty;
            qty -= sell_qty; cash += sell_qty*fill;
            if qty becomes 0 → delete row
    Short math (mirror of long with qty < 0; collateral reserved on open):
      Short: cash += qty*fill but reserve qty*fill collateral;
             avg_cost set on first open / averaged on add
      Cover: realized_pnl += (avg_cost - fill) * cover_qty;
             reduce |qty|; release collateral; if qty=0 → delete
    """
    from web import auth  # lazy

    account = auth.load_paper_account(user_id) or _seed_account(user_id)
    pos = auth.load_paper_position(user_id, ticker)

    cash = float(account["cash"])
    realized = float(account["realized_pnl"])
    short_collateral = float(account.get("short_collateral_reserved") or 0.0)

    new_qty: float
    new_avg: float

    if side == OrderSide.BUY:
        existing_qty = float(pos["qty"]) if pos else 0.0
        existing_avg = float(pos["avg_cost"]) if pos else 0.0
        if existing_qty >= 0:
            # Adding to a long (or opening one).
            total = existing_qty + qty
            new_avg = (existing_qty * existing_avg + qty * fill_price) / total if total > 0 else fill_price
            new_qty = total
        else:
            # Buying through a short — covers first, then opens long with remainder.
            cover_qty = min(qty, abs(existing_qty))
            realized += (existing_avg - fill_price) * cover_qty
            short_collateral = max(0.0, short_collateral - cover_qty * existing_avg)
            remaining = qty - cover_qty
            new_qty = existing_qty + cover_qty + remaining  # could go positive
            new_avg = fill_price if remaining > 0 else existing_avg
        cash -= qty * fill_price

    elif side == OrderSide.SELL:
        existing_qty = float(pos["qty"]) if pos else 0.0
        existing_avg = float(pos["avg_cost"]) if pos else 0.0
        if existing_qty <= 0:
            log.warning("sell on flat/short qty for %s/%s — treating as no-op", user_id, ticker)
            return
        sell_qty = min(qty, existing_qty)
        realized += (fill_price - existing_avg) * sell_qty
        cash += sell_qty * fill_price
        new_qty = existing_qty - sell_qty
        new_avg = existing_avg if new_qty > 0 else 0.0

    elif side == OrderSide.SHORT:
        existing_qty = float(pos["qty"]) if pos else 0.0
        existing_avg = float(pos["avg_cost"]) if pos else 0.0
        # qty here is positive (size of short to open); position qty goes negative.
        total_short = abs(existing_qty) + qty if existing_qty <= 0 else qty
        new_avg = (
            (abs(existing_qty) * existing_avg + qty * fill_price) / total_short
            if total_short > 0 else fill_price
        )
        new_qty = -total_short if existing_qty <= 0 else -qty
        cash += qty * fill_price                    # proceeds credited
        short_collateral += qty * fill_price        # but reserved as collateral

    elif side == OrderSide.COVER:
        existing_qty = float(pos["qty"]) if pos else 0.0
        existing_avg = float(pos["avg_cost"]) if pos else 0.0
        if existing_qty >= 0:
            log.warning("cover on flat/long qty for %s/%s — treating as no-op", user_id, ticker)
            return
        cover_qty = min(qty, abs(existing_qty))
        realized += (existing_avg - fill_price) * cover_qty
        cash -= cover_qty * fill_price
        short_collateral = max(0.0, short_collateral - cover_qty * existing_avg)
        new_qty = existing_qty + cover_qty
        new_avg = existing_avg if new_qty < 0 else 0.0

    else:
        log.warning("unknown order side: %s", side)
        return

    auth.upsert_paper_account(
        user_id=user_id,
        cash=cash,
        realized_pnl=realized,
        short_collateral_reserved=short_collateral,
    )
    if abs(new_qty) < 1e-9:
        auth.delete_paper_position(user_id, ticker)
    else:
        auth.upsert_paper_position(
            user_id=user_id,
            ticker=ticker,
            qty=new_qty,
            avg_cost=new_avg,
            last_price=fill_price,
        )


def _seed_account(user_id: str) -> Dict[str, Any]:
    """Create a fresh paper account row at the default starting cash."""
    from web import auth
    auth.upsert_paper_account(
        user_id=user_id,
        cash=DEFAULT_STARTING_CASH,
        realized_pnl=0.0,
        short_collateral_reserved=0.0,
        starting_cash=DEFAULT_STARTING_CASH,
    )
    return {
        "user_id": user_id,
        "starting_cash": DEFAULT_STARTING_CASH,
        "cash": DEFAULT_STARTING_CASH,
        "realized_pnl": 0.0,
        "short_collateral_reserved": 0.0,
    }


# ---------------------------------------------------------------------------
# NAV / MTM
# ---------------------------------------------------------------------------

def compute_nav_from_rows(
    account_row: Dict[str, Any],
    position_rows: List[Dict[str, Any]],
) -> Tuple[float, float]:
    """Single source of truth for NAV math. Operates on raw DB row dicts.

    NAV = cash + sum(qty * last_price) for longs
              + sum((avg_cost - last_price) * |qty|) for shorts.
    Unrealized PnL = NAV - cash (i.e. position-level mark-to-market vs avg
    cost). Realized PnL is already folded into `cash` via order writes, so
    it does NOT appear here.

    For shorts: when the short was opened we credited `cash += qty*fill` and
    reserved that amount as `short_collateral`. The short's economic value is
    `(avg_cost - last_price) * |qty|` — positive when price drops below entry
    (we profit), negative when price rises (we owe more to cover). That's
    the term added to NAV.

    Why this helper exists: prior to this refactor `paper.compute_nav` and
    the inline calculation in `web/server.py::get_paper_account` had two
    slightly different formulas that mostly agreed. They were collapsed
    into this single function (Jeff Dean review #7).
    """
    cash = float(account_row.get("cash") or 0.0)
    nav = cash
    unrealized = 0.0
    for p in position_rows:
        last = p.get("last_price")
        if last is None:
            continue
        qty = float(p["qty"])
        avg = float(p["avg_cost"])
        last = float(last)
        if qty > 0:
            nav += qty * last
            unrealized += (last - avg) * qty
        else:
            # Short economic value: gain if price has dropped.
            short_value = (avg - last) * abs(qty)
            nav += short_value
            unrealized += short_value
    return nav, unrealized


def compute_nav(account: PaperAccount, positions: List[PaperPosition]) -> Tuple[float, float]:
    """Pydantic-typed wrapper around `compute_nav_from_rows` for in-process
    callers. Both ultimately compute the same number."""
    return compute_nav_from_rows(
        {"cash": account.cash},
        [
            {
                "qty": p.qty,
                "avg_cost": p.avg_cost,
                "last_price": p.last_price,
            }
            for p in positions
        ],
    )


# ---------------------------------------------------------------------------
# Position prompt block (replaces portfolio.format_for_prompt JSON path)
# ---------------------------------------------------------------------------

def positions_for_prompt(user_id: str, ticker: str) -> str:
    """Return a prompt-ready directive block describing the user's position(s).

    Falls back to the legacy `portfolio.format_for_prompt(...)` path when no
    paper position exists yet — that keeps existing ad-hoc analyses working
    during the migration window. Phase 2 will remove the JSON fallback.
    """
    from web import auth

    rows = auth.list_paper_positions(user_id, ticker=ticker.upper())
    if not rows:
        # Fall through to the legacy disk-JSON portfolio for now.
        from . import portfolio as legacy_portfolio
        return legacy_portfolio.format_for_prompt(ticker)

    # Reuse the legacy formatter shape by handing it a synthetic positions dict.
    from . import portfolio as legacy_portfolio
    legacy_positions = {
        r["ticker"]: {
            "qty": float(r["qty"]),
            "avg_cost": float(r["avg_cost"]) if r.get("avg_cost") is not None else None,
        }
        for r in rows
    }
    # Pretend we're rendering a single position via the legacy path; reuse its
    # vocab tables for free.
    block_lines: List[str] = []
    for sym, pos in legacy_positions.items():
        block_lines.append(legacy_portfolio._describe_one(sym, pos))
    block_lines = [b for b in block_lines if b]
    if not block_lines:
        return ""
    bullets = "\n".join(f"  • {line}" for line in block_lines)

    # Determine net side via the legacy helper.
    net = legacy_portfolio._net_side(legacy_positions) or "LONG"
    vocab = (
        legacy_portfolio._VOCAB_LONG if net == "LONG"
        else legacy_portfolio._VOCAB_SHORT if net == "SHORT"
        else legacy_portfolio._VOCAB_MIXED
    )
    return (
        "════════ USER'S CURRENT POSITION (paper) ════════\n"
        f"The user already holds the following {ticker.upper()}-related position(s):\n"
        f"{bullets}\n\n"
        f"{vocab}\n"
        "Every recommendation must be a *delta* to the position above.\n"
        "════════════════════════════════════════"
    )


# ---------------------------------------------------------------------------
# Legacy `portfolio.json` migration — idempotent, run-once
# ---------------------------------------------------------------------------

def import_legacy_portfolio(token: ImpersonationToken) -> int:
    """Migrate `~/.agenticwhales/portfolio.json` into the paper account tables.

    Returns the number of positions imported (0 if no-op).

    Idempotent: if the user already has a non-default paper_account or the
    JSON has already been renamed, returns 0 without touching anything.
    """
    from pathlib import Path
    import os

    from . import portfolio as legacy_portfolio
    from web import auth

    path = Path(os.path.expanduser("~/.agenticwhales/portfolio.json"))
    if not path.exists():
        return 0

    existing = auth.load_paper_account(token.user_id)
    if existing and float(existing.get("realized_pnl") or 0.0) != 0.0:
        log.info("paper account for %s already has trading history; skipping legacy import", token.user_id)
        return 0
    if existing and abs(float(existing["cash"]) - float(existing["starting_cash"])) > 0.01:
        log.info("paper account for %s differs from starting cash; skipping legacy import", token.user_id)
        return 0

    data = legacy_portfolio.load_all()
    if not data:
        return 0

    starting_cash = float(os.getenv("AGENTICWHALES_PAPER_STARTING_CASH", DEFAULT_STARTING_CASH))
    cash_remaining = starting_cash
    imported = 0

    for sym, pos in data.items():
        qty = float(pos.get("qty") or 0.0)
        if qty == 0:
            continue
        avg = float(pos.get("avg_cost") or 0.0) or 0.0
        cash_remaining -= qty * avg if avg > 0 else 0.0
        auth.upsert_paper_position(
            user_id=token.user_id,
            ticker=sym.upper(),
            qty=qty,
            avg_cost=avg,
            last_price=None,
        )
        imported += 1

    auth.upsert_paper_account(
        user_id=token.user_id,
        starting_cash=starting_cash,
        cash=cash_remaining,
        realized_pnl=0.0,
        short_collateral_reserved=0.0,
    )

    # Rename to prevent re-import.
    try:
        ts = int(time.time())
        path.rename(path.with_suffix(f".imported.{ts}"))
    except OSError as exc:
        log.warning("could not rename legacy portfolio.json: %s", exc)

    log.info("imported %d legacy positions for %s", imported, token.user_id)
    return imported


# `should_escalate_reasoning` lived here in the v1 vertical but was never
# wired into any runner path — dead code. Removed in the post-Demis pass
# (review item D4). Phase 2 will re-introduce adaptive reasoning depth as
# part of the broader cognitive-journal package, at which point the
# variance heuristic gets its own home next to the calibration head.
