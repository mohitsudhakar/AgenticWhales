"""Plan ladder + entitlements — the single source of truth for what each
plan includes.

The internal `profiles.tier` values (novice / intermediate / master) predate
the product plans and only ever drove Analyst Desk quotas. They map 1:1 onto
the customer-facing plans and are never shown in the UI again:

    novice -> Free ("Know your leaks")
    intermediate -> Plus ("Fix them")
    master -> Pro ("Your trading back office")

Billing is DARK (validation-gated, owner decision): with
`AGENTICWHALES_BILLING_ENFORCED=0` (the default) every `check()` passes, so
beta users keep every feature — but the limit metadata still flows to the UI
so plan labels and upgrade nudges render. Flipping the env var turns the
gates on without a code change. Analyst-brief quotas are NOT routed through
this module: they're usage protection (enforced since the desk launched),
not monetization.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from web import auth

UNLIMITED = None  # readable sentinel inside the matrices below

TIER_TO_PLAN = {"novice": "free", "intermediate": "plus", "master": "pro"}

PLANS: Dict[str, Dict[str, Any]] = {
    "free": {"label": "Free", "tagline": "Know your leaks",
             "price_monthly": 0, "founding_monthly": 0},
    "plus": {"label": "Plus", "tagline": "Fix them",
             "price_monthly": 19, "founding_monthly": 9},
    "pro": {"label": "Pro", "tagline": "Your trading back office",
            "price_monthly": 49, "founding_monthly": 29},
}

# What each plan includes. Booleans gate features; numbers are daily/active
# caps (None = unlimited). Keep in sync with /pricing and the spec:
# docs/product/premium-features-spec.md
ENTITLEMENTS: Dict[str, Dict[str, Any]] = {
    "free": {
        "briefs_per_day": 3,
        "pretrade_per_day": 5,
        "max_active_rules": 2,
        "auto_sync": False,
        "email_digest": False,
        "partner": False,
        "violation_alerts": False,
        "counterfactual_curve": False,
        "benchmark_percentile": False,
        "standing_briefs": False,
        "eval_mode": False,
    },
    "plus": {
        "briefs_per_day": 50,
        "pretrade_per_day": UNLIMITED,
        "max_active_rules": UNLIMITED,
        "auto_sync": True,
        "email_digest": True,
        "partner": True,
        "violation_alerts": True,
        "counterfactual_curve": True,
        "benchmark_percentile": True,
        "standing_briefs": False,
        "eval_mode": False,
    },
    "pro": {
        "briefs_per_day": UNLIMITED,
        "pretrade_per_day": UNLIMITED,
        "max_active_rules": UNLIMITED,
        "auto_sync": True,
        "email_digest": True,
        "partner": True,
        "violation_alerts": True,
        "counterfactual_curve": True,
        "benchmark_percentile": True,
        "standing_briefs": True,
        "eval_mode": True,
    },
}

# Which plan unlocks each gated feature — drives "Included in <plan>" labels.
FEATURE_MIN_PLAN: Dict[str, str] = {
    "auto_sync": "plus", "email_digest": "plus", "partner": "plus",
    "violation_alerts": "plus", "counterfactual_curve": "plus",
    "benchmark_percentile": "plus",
    "standing_briefs": "pro", "eval_mode": "pro",
}


def billing_enforced() -> bool:
    return os.getenv("AGENTICWHALES_BILLING_ENFORCED", "0").lower() in ("1", "true", "yes")


def plan_for_user(user_id: str) -> str:
    """The user's plan key ('free' | 'plus' | 'pro')."""
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return "free"
    return TIER_TO_PLAN.get(auth.get_user_tier(user_id), "free")


def entitlement(user_id: str, key: str) -> Any:
    """The raw entitlement value for this user's plan (bool, int, or None)."""
    return ENTITLEMENTS[plan_for_user(user_id)].get(key)


def check(user_id: str, key: str, *, used: Optional[int] = None) -> bool:
    """Is the user allowed `key` (optionally given current usage `used`)?

    Always True while billing isn't enforced — the beta keeps every feature
    on; the UI still labels gated features so the journey to pay is visible.
    """
    if not billing_enforced():
        return True
    val = entitlement(user_id, key)
    if isinstance(val, bool):
        return val
    if val is None:  # unlimited
        return True
    return (used or 0) < int(val)


def plan_summary(user_id: str) -> Dict[str, Any]:
    """Everything the UI needs to render plan state: /api/account/plan."""
    plan = plan_for_user(user_id)
    return {
        "plan": plan,
        "label": PLANS[plan]["label"],
        "tagline": PLANS[plan]["tagline"],
        "entitlements": ENTITLEMENTS[plan],
        "feature_min_plan": FEATURE_MIN_PLAN,
        "billing_enforced": billing_enforced(),
        "beta": not billing_enforced(),
    }
