"""Recipes — persistent, scheduled debate runs.

A recipe is the unit of autonomy in Phase 1: a stored configuration
(tickers, analysts, model choices, schedule) that the scheduler fires on a
cron/interval/manual trigger. Each fire produces a regular `sessions` row
linked back to the recipe.

This module owns:
  - The `Recipe` Pydantic model (re-exported from `agenticwhales.agents.schemas`)
  - Heterogeneity validation (Bull and Bear models MUST come from different
    families — Sanjay/Demis review)
  - Thin CRUD wrappers around `web.auth` storage helpers

The actual scheduler lives in `web.scheduler`.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

from .agents.schemas import (
    ImpersonationToken,
    OutputPolicy,
    Recipe,
    RecipeStatus,
    ScheduleKind,
)

log = logging.getLogger(__name__)

# Model-family prefix table. The principle: two models from the same family
# share most of their training distribution + RLHF priors, so their
# disagreement is mostly noise, not signal. Heterogeneity enforcement at
# recipe-create time rejects same-family Bull/Bear pairings.
MODEL_FAMILY_PREFIXES: Dict[str, tuple[str, ...]] = {
    "openai":    ("gpt-", "o1-", "o3-", "o4-", "o5-", "chatgpt-"),
    "anthropic": ("claude-",),
    "google":    ("gemini-", "palm-"),
    "deepseek":  ("deepseek-",),
    "xai":       ("grok-",),
    "zhipu":     ("glm-",),
    "qwen":      ("qwen-",),
    "ollama":    ("llama", "mistral", "qwen2", "phi"),
}


class HeterogeneityError(ValueError):
    """Raised when a recipe's Bull and Bear come from the same model family."""


def family_of(model: str) -> str:
    """Return a normalized family key for the given model identifier.

    Falls back to `"unknown"` rather than raising — an unknown model is treated
    as its own family. This is intentional: a custom / local model SHOULD be
    treated as distinct from the well-known families. The risk is that two
    unknown models on the same backend pass the heterogeneity check; we accept
    that trade-off because the production failure mode (silently identical
    models) is worse than the development friction (false-positive diversity).
    """
    if not model:
        return "unknown"
    m = model.strip().lower()
    for fam, prefixes in MODEL_FAMILY_PREFIXES.items():
        if any(m.startswith(p) for p in prefixes):
            return fam
    return "unknown"


def _validate_timeframes(raw: Any) -> List[str]:
    """Validate Phase 3 multi-timeframe selection. Empty / None → default ['1d']."""
    if not raw:
        return ["1d"]
    from .dag import CANONICAL_TIMEFRAMES  # local to avoid import cycle
    if isinstance(raw, str):
        raw = [raw]
    valid = set(CANONICAL_TIMEFRAMES)
    out: List[str] = []
    for tf in raw:
        s = str(tf).strip().lower()
        if s and s in valid and s not in out:
            out.append(s)
    return out or ["1d"]


def _validate_trigger_conditions(raw: Any) -> Optional[Dict[str, Any]]:
    """Validate the (optional) trigger condition payload at recipe-create time.

    Stored back as a plain JSON dict so the column stays JSONB-friendly, but the
    parse pass catches typos / unknown kinds up front instead of at streaming
    fire-time. Returns None for empty payloads."""
    if raw is None or raw == {} or raw == "":
        return None
    from .triggers import parse_condition  # local to avoid import cycle
    parsed = parse_condition(raw)
    return parsed.model_dump(mode="json") if parsed else None


def validate_heterogeneity(bull_model: str, bear_model: str) -> None:
    """Raise `HeterogeneityError` if Bull and Bear share a model family.

    Called by recipe-create / recipe-update validators. Two unknown models
    both classified as `"unknown"` are also rejected — operationally a user
    setting both to the same locally-running checkpoint is the most common
    failure mode here.
    """
    fam_bull = family_of(bull_model)
    fam_bear = family_of(bear_model)
    if fam_bull == fam_bear:
        raise HeterogeneityError(
            f"Bull and Bear must come from different model families. "
            f"Both '{bull_model}' and '{bear_model}' resolve to family '{fam_bull}'. "
            f"Pick two distinct families (e.g. one Google + one DeepSeek) so the "
            f"debate isn't correlated ensembling."
        )


def build_recipe(form: Dict[str, Any], *, user_id: str, recipe_id: Optional[str] = None) -> Recipe:
    """Construct a fresh `Recipe` from validated form data.

    Heterogeneity is enforced HERE so every code path that creates a recipe
    (web API, CLI, tests) gets the same check — no duplicated validation
    elsewhere.
    """
    bull = (form.get("bull_model") or form.get("deep_model") or "").strip()
    bear = (form.get("bear_model") or form.get("quick_model") or "").strip()
    if not bull or not bear:
        raise ValueError("recipe requires both bull_model and bear_model")
    validate_heterogeneity(bull, bear)

    now = time.time()
    return Recipe(
        id=recipe_id or uuid.uuid4().hex,
        user_id=user_id,
        name=form["name"].strip(),
        tickers=[t.strip().upper() for t in form["tickers"] if t and t.strip()],
        exchange_code=(form.get("exchange_code") or "XNYS").upper(),
        analysts=list(form.get("analysts") or []),
        llm_provider=form["llm_provider"],
        quick_model=form["quick_model"],
        deep_model=form["deep_model"],
        bull_model=bull,
        bear_model=bear,
        research_depth=int(form.get("research_depth", 1)),
        output_language=form.get("output_language", "English"),
        schedule_kind=ScheduleKind(form.get("schedule_kind", "manual")),
        schedule_expr=form.get("schedule_expr"),
        misfire_grace_seconds=int(form.get("misfire_grace_seconds", 300)),
        market_hours_only=bool(form.get("market_hours_only", True)),
        max_concurrent_tickers=int(form.get("max_concurrent_tickers", 5)),
        timeframes=_validate_timeframes(form.get("timeframes")),
        streaming_max_fires_per_hour=int(form.get("streaming_max_fires_per_hour", 6)),
        trigger_conditions=_validate_trigger_conditions(form.get("trigger_conditions")),
        output_policy=OutputPolicy(form.get("output_policy", "notify")),
        conviction_threshold=int(form.get("conviction_threshold", 7)),
        max_daily_token_cost_usd=float(form.get("max_daily_token_cost_usd", 5.0)),
        auto_inject_classical=bool(form.get("auto_inject_classical", False)),
        status=RecipeStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )


# ----------------------------------------------------------------------------
# CRUD wrappers — delegate to web.auth storage helpers.
# ----------------------------------------------------------------------------

def save(recipe: Recipe, token: Optional[ImpersonationToken] = None) -> None:
    """Upsert a recipe row. Token unused in dev fallback; required in prod
    once impersonation is wired into the storage layer."""
    from web import auth  # lazy
    auth.save_recipe(recipe.model_dump(mode="json"))


def load(recipe_id: str) -> Optional[Recipe]:
    from web import auth
    row = auth.load_recipe(recipe_id)
    return Recipe.model_validate(row) if row else None


def list_for_user(user_id: str) -> List[Recipe]:
    from web import auth
    rows = auth.list_recipes(user_id)
    return [Recipe.model_validate(r) for r in rows]


def list_all_active() -> List[Recipe]:
    """Service-role-only: enumerate every active recipe across users.

    Used by the scheduler bootstrap. Backs into `web.auth.list_recipes_all_active`
    which queries with the service key (RLS bypassed by `service_role`).
    """
    from web import auth
    rows = auth.list_recipes_all_active()
    return [Recipe.model_validate(r) for r in rows]


def delete(recipe_id: str) -> bool:
    from web import auth
    return auth.delete_recipe(recipe_id)


def touch_last_run(recipe_id: str, when: Optional[float] = None) -> None:
    from web import auth
    auth.touch_recipe_last_run(recipe_id, when or time.time())


def update_status(recipe_id: str, status: RecipeStatus) -> None:
    from web import auth
    auth.update_recipe_status(recipe_id, status.value)


def bump_failures(recipe_id: str) -> int:
    """Increment consecutive_failures; return new value."""
    from web import auth
    return auth.bump_recipe_failures(recipe_id)


def reset_failures(recipe_id: str) -> None:
    from web import auth
    auth.reset_recipe_failures(recipe_id)


def filter_active(recipes: Iterable[Recipe]) -> List[Recipe]:
    return [r for r in recipes if r.status == RecipeStatus.ACTIVE]
