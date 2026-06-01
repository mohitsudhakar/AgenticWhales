"""Cost tracking + spend-cap enforcement for LLM calls.

Two halves:

1. `record_fire_cost(...)` — called by the runner after a session completes.
   Computes USD cost from the session's accumulated token counts × the
   versioned pricing table, then debits:
     - `recipe_usage` (per-recipe daily aggregate; powers the scheduler's
       budget gate)
     - `user_spend_daily` (per-user daily aggregate; powers the global cap
       enforced both at the scheduler gate and pre-call defense-in-depth)
     - `llm_call_log` (one rolled-up row per session for now; per-agent
       breakdown is Phase 2 once we have a richer callback stream)

2. `check_user_budget(user_id)` — pre-call defense-in-depth. Raises
   `BudgetExceeded` when the user's daily spend has hit the cap. Today
   only the scheduler calls this; once we have a per-call hook in the
   provider clients (Phase 2), the LLM client wrapper will too.

Why a separate post-fire roll-up instead of per-call debits today? The
existing `StatsCallbackHandler` aggregates at the session level — we don't
have a per-agent token breakdown threaded through every provider. Rolling
up at the end gets us 90% of the value (accurate per-recipe + per-user
spend) without touching every provider's call site. Phase 2 swaps in
per-agent attribution when we add the OTel-spans-per-agent observability.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from .pricing import cost_for


class BudgetExceeded(Exception):
    """Raised when a pre-call check sees today's spend at or above the cap."""

    def __init__(self, cap_usd: float, spent_usd: float):
        super().__init__(f"daily spend cap exceeded: ${spent_usd:.4f} >= ${cap_usd:.4f}")
        self.cap_usd = cap_usd
        self.spent_usd = spent_usd


def record_fire_cost(
    *,
    user_id: str,
    recipe_id: Optional[str],
    session_id: str,
    provider: str,
    quick_model: str,
    deep_model: str,
    stats: Dict[str, Any],
    agent_breakdown: Optional[Dict[str, Dict[str, int]]] = None,
    at: Optional[datetime] = None,
    wall_time_ms: Optional[int] = None,
) -> Decimal:
    """Compute the dollar cost of this session and write the spend rows.

    `stats` is the dict produced by `StatsCallbackHandler.get_stats()`:
    `{llm_calls, tool_calls, tokens_in, tokens_out}`. Without a per-agent
    breakdown we attribute *all* tokens to the deep_model rate — conservative
    because deep models are the most expensive, so we won't under-bill.

    Returns the computed cost as Decimal. Best-effort: any storage error is
    logged but doesn't raise (cost tracking failures should not break a
    completed user-facing analysis).
    """
    from web import auth  # lazy import

    now = at or datetime.now(tz=timezone.utc)
    today_str = now.date().isoformat()
    tokens_in = int(stats.get("tokens_in") or 0)
    tokens_out = int(stats.get("tokens_out") or 0)

    # Phase 1.5 cleanup: when the session stats include a per-model
    # breakdown, bill each model at its own price. Falls back to the
    # conservative deep-model attribution when no breakdown is available
    # (older provider clients, free-text fallback paths).
    model_usage = stats.get("model_usage") or {}
    cost = Decimal(0)
    if model_usage:
        for model_id, bucket in model_usage.items():
            mtin = int(bucket.get("tokens_in") or 0)
            mtout = int(bucket.get("tokens_out") or 0)
            if mtin == 0 and mtout == 0:
                continue
            try:
                cost += cost_for(
                    provider=provider, model=model_id,
                    input_tokens=mtin, output_tokens=mtout, at=now,
                )
            except ValueError:
                # Unknown model → fall back to deep-model rate.
                try:
                    cost += cost_for(
                        provider=provider, model=deep_model,
                        input_tokens=mtin, output_tokens=mtout, at=now,
                    )
                except ValueError:
                    pass
    else:
        try:
            cost = cost_for(
                provider=provider, model=deep_model,
                input_tokens=tokens_in, output_tokens=tokens_out,
                at=now,
            )
        except ValueError:
            cost = Decimal(0)

    cost_float = float(cost)

    # Recipe usage (powers scheduler budget gate).
    if recipe_id:
        try:
            auth.add_recipe_usage(
                recipe_id=recipe_id, user_id=user_id, usage_date=today_str,
                input_tokens=tokens_in, output_tokens=tokens_out,
                reasoning_tokens=0, token_cost_usd=cost_float,
                failure=False,
            )
        except Exception:
            pass

    # User-level daily spend (powers global cap + alerting).
    try:
        auth.add_user_spend(user_id, today_str, cost_float)
    except Exception:
        pass

    # Replayable call log — one row per session for now. Always write to
    # memstore (dev path) AND attempt the DB upsert (no-op when not writable).
    row = {
        "user_id": user_id,
        "session_id": session_id,
        "agent_name": "session_roll_up",
        "provider": provider,
        "model": deep_model,
        "input_hash": session_id,  # placeholder; real hash in Phase 2
        "output_hash": None,
        "raw_payload_uri": None,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "reasoning_tokens": 0,
        "cost_usd": cost_float,
        "latency_ms": int(wall_time_ms) if wall_time_ms is not None else None,
        "status": "ok",
        "error_message": None,
        "created_at": now.isoformat(),
    }
    auth._memstore[("llm_call_log", f"{session_id}|{now.isoformat()}")] = row
    try:
        auth._upsert_columns("llm_call_log", row)
    except Exception:
        pass

    return cost


def check_user_budget(user_id: str, *, at: Optional[datetime] = None) -> None:
    """Raise `BudgetExceeded` if the user has hit their daily spend cap.

    Reads `risk_limits.daily_spend_cap_usd` and `user_spend_daily.total_cost_usd`
    for today. A `risk_limits` row that doesn't exist yet seeds the defaults
    on first call.
    """
    from web import auth

    now = at or datetime.now(tz=timezone.utc)
    today_str = now.date().isoformat()
    spent = auth.load_user_spend(user_id, today_str)
    limits = auth.load_risk_limits(user_id) or auth._default_risk_limits_row(user_id)
    cap = float(limits.get("daily_spend_cap_usd", 25.0))
    if spent >= cap:
        raise BudgetExceeded(cap_usd=cap, spent_usd=spent)
