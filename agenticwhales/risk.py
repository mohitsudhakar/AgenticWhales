"""RiskGuard — pre-trade safety checks.

Three fail-fast checks, evaluated in order:
  1. Kill switch (global or recipe-killed) — hard block, no clamp.
  2. Daily NAV drawdown — hard block once today's NAV has fallen >
     `max_daily_drawdown_pct` from the open of the UTC day.
  3. Single-position cap — clamp position size to
     `max_position_pct` of NAV; block if clamping leaves <1 share.

Sector cap is intentionally NOT in Phase 1 (see plan §1.7 cuts) — restored in
Phase 4 alongside real broker execution.

The guard is pure given its inputs — no DB writes here. Risk-event rows are
written via `record_event(...)` which the caller invokes when `allowed=False`
OR a clamp happened. That separation makes the guard easy to test and lets
the runner decide whether to broadcast events on the session WebSocket.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .agents.schemas import (
    GuardOutcome,
    ImpersonationToken,
    PaperAccount,
    PaperPosition,
    PortfolioDecision,
)
from .paper import compute_nav

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskLimits:
    """Per-user risk configuration. Mirrors `public.risk_limits`."""

    max_position_pct: float = 0.10
    max_daily_drawdown_pct: float = 0.03
    max_slippage_bps: int = 10
    kelly_fraction_cap: float = 0.10
    adaptive_depth_variance_threshold: float = 0.30
    daily_spend_cap_usd: float = 25.0
    monthly_spend_cap_usd: float = 500.0
    allow_shorts: bool = False
    global_kill_switch: bool = False


class RiskGuard:
    """Pre-trade gate. One instance per session/fire.

    `evaluate(...)` returns a `GuardOutcome`. The caller (post_decision_hook)
    uses the outcome to decide what status the resulting paper order gets:
      - `allowed=False, allowed_qty=0`         → write order with status='blocked'
      - `allowed=True,  allowed_qty < target`  → write order with status='clamped'
      - `allowed=True,  allowed_qty == target` → write order with status='filled'

    `recipe_killed` lets the caller signal a recipe-level kill switch (separate
    from the global one) — useful for "pause this recipe right now, no more
    fires."
    """

    def __init__(
        self,
        user_id: str,
        limits: RiskLimits,
        account: PaperAccount,
        positions: List[PaperPosition],
        recipe_killed: bool = False,
    ) -> None:
        self.user_id = user_id
        self.limits = limits
        self.account = account
        self.positions = positions
        self.recipe_killed = recipe_killed
        self._nav, _ = compute_nav(account, positions)

    @property
    def nav(self) -> float:
        return self._nav

    def evaluate(
        self,
        decision: PortfolioDecision,
        ticker: str,
        target_qty: float,
        last_price: float,
    ) -> GuardOutcome:
        # Check 1: kill switches (global OR recipe-level).
        if self.limits.global_kill_switch:
            return GuardOutcome(
                allowed=False, allowed_qty=0.0, blocked_qty=abs(target_qty),
                rule="kill_switch",
                reason="global kill switch is engaged",
            )
        if self.recipe_killed:
            return GuardOutcome(
                allowed=False, allowed_qty=0.0, blocked_qty=abs(target_qty),
                rule="kill_switch", reason="recipe is in killed state",
            )

        # Check 2: daily drawdown halt.
        nav_open = self.account.nav_open_today
        if nav_open and nav_open > 0:
            dd = (self._nav - nav_open) / nav_open
            if dd <= -abs(self.limits.max_daily_drawdown_pct):
                return GuardOutcome(
                    allowed=False, allowed_qty=0.0, blocked_qty=abs(target_qty),
                    rule="daily_drawdown",
                    reason=f"daily drawdown {dd*100:.2f}% exceeds cap "
                           f"{-self.limits.max_daily_drawdown_pct*100:.2f}%",
                )

        # No qty requested → trivial pass.
        if target_qty == 0 or last_price <= 0:
            return GuardOutcome(allowed=True, allowed_qty=0.0)

        # Check 3: single-position cap.
        max_position_dollars = self._nav * self.limits.max_position_pct
        target_dollars = abs(target_qty) * last_price

        # If we already hold a position in this ticker, the "new" position size
        # is the combined exposure post-trade. Conservatively assume the trade
        # is in the same direction (worst-case cap usage).
        existing_qty = 0.0
        existing_avg = 0.0
        for p in self.positions:
            if p.ticker.upper() == ticker.upper():
                existing_qty = p.qty
                existing_avg = p.avg_cost
                break
        existing_dollars = abs(existing_qty) * (existing_avg or last_price)
        combined = existing_dollars + target_dollars

        if combined <= max_position_dollars:
            return GuardOutcome(
                allowed=True, allowed_qty=abs(target_qty), blocked_qty=0.0,
            )

        # Clamp: allow up to the cap.
        room_dollars = max(0.0, max_position_dollars - existing_dollars)
        allowed_qty = room_dollars / last_price
        if allowed_qty < 1e-6:
            return GuardOutcome(
                allowed=False, allowed_qty=0.0, blocked_qty=abs(target_qty),
                rule="max_position",
                reason=f"existing position already at or above cap "
                       f"({existing_dollars:.2f} / {max_position_dollars:.2f})",
            )

        return GuardOutcome(
            allowed=True,
            allowed_qty=allowed_qty,
            blocked_qty=abs(target_qty) - allowed_qty,
            rule="max_position",
            reason=f"clamped from {abs(target_qty):.4f} to {allowed_qty:.4f} "
                   f"to stay under {self.limits.max_position_pct*100:.1f}% cap",
        )


def record_event(
    token: ImpersonationToken,
    *,
    recipe_id: Optional[str],
    session_id: Optional[str],
    ticker: Optional[str],
    rule: str,
    details: Dict[str, Any],
) -> None:
    """Append-only write to `risk_events`."""
    from web import auth  # lazy
    auth.insert_risk_event({
        "user_id": token.user_id,
        "recipe_id": recipe_id,
        "session_id": session_id,
        "ticker": ticker.upper() if ticker else None,
        "rule": rule,
        "details": details or {},
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    })


def ensure_nav_open_today(account: PaperAccount, current_nav: float) -> bool:
    """Refresh `nav_open_today` if a new UTC day has begun.

    Returns True if a refresh happened (caller persists the updated account).
    """
    today = datetime.now(tz=timezone.utc).date().isoformat()
    if account.nav_open_today_date == today and account.nav_open_today is not None:
        return False
    account.nav_open_today = current_nav
    account.nav_open_today_date = today
    return True
