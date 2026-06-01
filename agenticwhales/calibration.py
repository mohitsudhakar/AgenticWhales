"""Per-user calibration head — Phase 2 deliverable #3.

The Portfolio Manager's `prob_of_profit` is a self-reported number from an
LLM. Empirically LLMs are systematically miscalibrated: a stated 80%
probability event happens closer to 60% of the time. Using the raw value
to drive Kelly sizing over-bets the upside and amplifies losses on the
miscalibrated tail.

Fix: a per-user **Platt scaling** layer that learns the user-specific map
`p_calibrated = sigmoid(a * logit(p_raw) + b)` from the user's own resolved
outcomes. Once enough data exists AND the user opts in, `paper.kelly_sizing`
runs the raw probability through this map before computing the Kelly
fraction.

Design choices:

- **Per-user, not global.** Each user's calibration is a mix of their PM
  prompt config, their ticker universe, their journal-driven feedback loop.
  A global model would average over wildly different regimes.

- **Opt-in.** When the calibration head improves Brier on the user's data
  we surface a card on Overview ("Your fund has learned your calibration —
  apply it?"). The user clicks yes; we flip `calibration_models.applied=true`
  and `kelly_sizing` starts using it. They can revoke any time.

- **Unlock gate.** We don't fit / suggest applying until the user has
  N ≥ 30 resolved outcomes with non-null `predicted_prob_of_profit`.
  Below that, the fit is dominated by noise.

- **No external dependency.** Platt scaling is a 2-parameter logistic
  regression. We do the fit by gradient descent in numpy. Same answer as
  sklearn for any practical sample size we'll see in v1.

- **Audit + opt-in toggle live in `web/auth.py` storage**, not here. This
  module is pure math + business rules; storage is injected via the lazy
  imports below.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)


# --- tunables --------------------------------------------------------------

# Minimum sample size before we fit + suggest applying. Below this the Platt
# fit is dominated by noise and would do more harm than good.
UNLOCK_N = 30

# Maximum Brier the calibrated head is allowed before we suggest applying.
# A calibrated head worse than 0.30 is so bad that we'd rather the user
# keep the raw probabilities, which at least carry the LLM's prior.
MAX_BRIER_FOR_SUGGEST = 0.30

# Platt scaling gradient-descent hyperparameters. Small dataset, small loop.
PLATT_LR = 0.05
PLATT_EPOCHS = 1000
EPS = 1e-9


# ---------------------------------------------------------------------------
# Core math
# ---------------------------------------------------------------------------

def _logit(p: float) -> float:
    """Clipped logit to avoid +/- inf on probabilities at the extremes."""
    p = max(EPS, min(1.0 - EPS, p))
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def fit_platt(
    pairs: List[Tuple[float, bool]],
    *,
    lr: float = PLATT_LR,
    epochs: int = PLATT_EPOCHS,
) -> Tuple[float, float]:
    """Fit `p_cal = sigmoid(a * logit(p_raw) + b)` to (raw_prob, hit) pairs.

    Pairs are `(raw_prob_of_profit, did_trade_hit)`. The map is the standard
    Platt scaling reduction of binary calibration to 2-param logistic
    regression.

    Returns `(a, b)`. `a=1, b=0` is the identity map — emitted when there's
    no data to fit or every point already agrees with the raw probability.

    The descent uses cross-entropy loss with mild L2 regularization on the
    slope so a tiny sample doesn't collapse to a degenerate `a≈0`.
    """
    if not pairs:
        return (1.0, 0.0)

    # Pre-compute logits + targets.
    xs = [_logit(p) for p, _ in pairs]
    ys = [1.0 if h else 0.0 for _, h in pairs]
    n = len(pairs)

    a, b = 1.0, 0.0
    l2 = 0.001  # weak prior toward identity
    for _ in range(epochs):
        ga = 0.0
        gb = 0.0
        for x, y in zip(xs, ys):
            pred = _sigmoid(a * x + b)
            err = pred - y
            ga += err * x
            gb += err
        # Gradient + L2 on `a-1` keeps the identity as the regularizer mean.
        a -= lr * (ga / n + l2 * (a - 1.0))
        b -= lr * (gb / n)
    return (a, b)


def apply_platt(a: float, b: float, p_raw: float) -> float:
    """Apply a fitted Platt map to a raw probability."""
    return _sigmoid(a * _logit(p_raw) + b)


def brier(pairs: List[Tuple[float, bool]]) -> float:
    """Mean Brier score for (predicted_prob, hit) pairs. Lower is better.

    Returns 0.25 (the uninformative baseline) when there are no pairs so
    downstream comparisons remain well-defined."""
    if not pairs:
        return 0.25
    total = 0.0
    for p, h in pairs:
        target = 1.0 if h else 0.0
        total += (p - target) ** 2
    return total / len(pairs)


# ---------------------------------------------------------------------------
# High-level fit + storage
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CalibrationFit:
    user_id: str
    regime: str
    a: float
    b: float
    n_samples: int
    brier_before: float
    brier_after: float
    fitted_at: str   # ISO timestamp


def _outcome_pairs(user_id: str) -> List[Tuple[float, bool]]:
    """Pull resolved outcomes for the user and project to `(prob, hit)`."""
    from agenticwhales import outcomes as outcomes_mod
    rows = outcomes_mod.list_outcomes_for_user(user_id, limit=10_000) or []
    out: List[Tuple[float, bool]] = []
    for r in rows:
        p = r.get("predicted_prob_of_profit")
        hit = r.get("hit")
        if p is None or hit is None:
            continue
        try:
            out.append((float(p), bool(hit)))
        except (TypeError, ValueError):
            continue
    return out


def fit_for_user(user_id: str, *, regime: str = "all") -> Optional[CalibrationFit]:
    """Fit + persist a calibration row for one user. Returns the new fit, or
    None if the user is below the unlock gate (N < UNLOCK_N).

    Idempotent in the sense that re-running with the same data produces the
    same `(a, b)` to machine precision — descent is deterministic. Each
    successful fit writes a new row keyed on `(user_id, regime, fitted_at)`
    so we keep history; the current row is the most recent.

    The `applied` flag is NOT set here. The user opts in via
    `opt_in(user_id, regime)`. Fit is observation; apply is action.
    """
    pairs = _outcome_pairs(user_id)
    if len(pairs) < UNLOCK_N:
        return None

    a, b = fit_platt(pairs)
    b_before = brier(pairs)
    b_after = brier([(apply_platt(a, b, p), h) for p, h in pairs])
    fitted_at = datetime.now(tz=timezone.utc).isoformat()

    fit = CalibrationFit(
        user_id=user_id, regime=regime,
        a=a, b=b, n_samples=len(pairs),
        brier_before=b_before, brier_after=b_after,
        fitted_at=fitted_at,
    )
    _persist(fit)
    return fit


def _persist(fit: CalibrationFit) -> None:
    from web import auth
    row = {
        "user_id": fit.user_id,
        "regime": fit.regime,
        "a": fit.a,
        "b": fit.b,
        "n_samples": fit.n_samples,
        "brier_before": fit.brier_before,
        "brier_after": fit.brier_after,
        "applied": False,        # opt-in is a separate action
        "fitted_at": fit.fitted_at,
    }
    auth._memstore[("calibration_models", f"{fit.user_id}|{fit.regime}|{fit.fitted_at}")] = row
    try:
        auth._upsert_columns("calibration_models", row,
                             on_conflict="user_id,regime,fitted_at")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Storage helpers — current state for a user
# ---------------------------------------------------------------------------

def current_fit(user_id: str, *, regime: str = "all") -> Optional[CalibrationFit]:
    """Return the most-recent fit for a user × regime, or None."""
    rows = _all_fits(user_id, regime=regime)
    if not rows:
        return None
    rows.sort(key=lambda r: r.get("fitted_at") or "", reverse=True)
    r = rows[0]
    return CalibrationFit(
        user_id=r["user_id"], regime=r["regime"],
        a=float(r["a"]), b=float(r["b"]),
        n_samples=int(r["n_samples"]),
        brier_before=float(r.get("brier_before") or 0.0),
        brier_after=float(r.get("brier_after") or 0.0),
        fitted_at=str(r["fitted_at"]),
    )


def is_opted_in(user_id: str, *, regime: str = "all") -> bool:
    """True iff the user has explicitly opted in to apply the most-recent
    fit. The map is NOT applied to sizing until this returns True."""
    rows = _all_fits(user_id, regime=regime)
    if not rows:
        return False
    rows.sort(key=lambda r: r.get("fitted_at") or "", reverse=True)
    return bool(rows[0].get("applied"))


def opt_in(user_id: str, *, regime: str = "all", apply: bool = True) -> bool:
    """Flip the `applied` flag on the most-recent fit. Returns whether a
    change was made (False if there's no fit to flip)."""
    from web import auth
    rows = _all_fits(user_id, regime=regime)
    if not rows:
        return False
    rows.sort(key=lambda r: r.get("fitted_at") or "", reverse=True)
    target = rows[0]
    target["applied"] = bool(apply)
    auth._memstore[(
        "calibration_models",
        f"{target['user_id']}|{target['regime']}|{target['fitted_at']}",
    )] = target
    try:
        auth._upsert_columns("calibration_models", target,
                             on_conflict="user_id,regime,fitted_at")
    except Exception:
        pass
    return True


def _all_fits(user_id: str, *, regime: Optional[str] = None) -> List[dict]:
    """Internal: pull every fit row for a user × (optional) regime."""
    from web import auth
    if auth._db_writable():
        filters = {"user_id": user_id}
        if regime:
            filters["regime"] = regime
        try:
            return auth._select_columns(
                "calibration_models", filters=filters,
                order="fitted_at.desc", limit=100,
            ) or []
        except Exception:
            pass
    out = [
        r for (t, _), r in auth._memstore.items()
        if t == "calibration_models" and r.get("user_id") == user_id
        and (regime is None or r.get("regime") == regime)
    ]
    return out


# ---------------------------------------------------------------------------
# Application — public hook for paper.kelly_sizing
# ---------------------------------------------------------------------------

def apply_if_opted_in(
    user_id: str, p_raw: Optional[float], *, regime: str = "all",
) -> Optional[float]:
    """Return the calibrated probability if the user has opted in AND a fit
    exists, else return `p_raw` unchanged.

    Called from `paper.kelly_sizing`. This is the only point where the
    calibration map is allowed to influence sizing — keeping the surface
    minimal makes the user-facing opt-in toggle audit-able.
    """
    if p_raw is None:
        return None
    if not is_opted_in(user_id, regime=regime):
        return p_raw
    fit = current_fit(user_id, regime=regime)
    if fit is None:
        return p_raw
    return apply_platt(fit.a, fit.b, p_raw)


# ---------------------------------------------------------------------------
# Suggestion logic — does the Overview card prompt the user to opt in?
# ---------------------------------------------------------------------------

def suggestion(user_id: str, *, regime: str = "all") -> dict:
    """Return a dict the UI can render directly:

      - `status`: 'no_fit' (need more outcomes), 'available' (fit ready,
        user not opted in), 'applied' (user opted in), 'no_improvement'
        (fit ready but doesn't beat raw — don't suggest).
      - `n`, `brier_before`, `brier_after`, `improvement`, `fitted_at`:
        numbers for the card.
      - `unlock_n`: the gate (so the UI can render a progress bar).
    """
    fit = current_fit(user_id, regime=regime)
    if fit is None:
        # Try a fit on the fly so the suggestion stays fresh after new
        # outcomes resolve. The fit only happens if we're past the gate.
        fit = fit_for_user(user_id, regime=regime)
    if fit is None:
        # Still below the gate.
        pairs = _outcome_pairs(user_id)
        return {
            "status": "no_fit",
            "n": len(pairs),
            "unlock_n": UNLOCK_N,
            "regime": regime,
        }

    improvement = fit.brier_before - fit.brier_after
    applied = is_opted_in(user_id, regime=regime)
    if applied:
        status = "applied"
    elif fit.brier_after > MAX_BRIER_FOR_SUGGEST or improvement <= 0:
        status = "no_improvement"
    else:
        status = "available"

    return {
        "status": status,
        "n": fit.n_samples,
        "unlock_n": UNLOCK_N,
        "brier_before": fit.brier_before,
        "brier_after": fit.brier_after,
        "improvement": improvement,
        "fitted_at": fit.fitted_at,
        "a": fit.a, "b": fit.b,
        "regime": regime,
    }
