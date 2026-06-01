"""Adaptive reasoning depth + prompt-eval harness — Phase 2 deliverable #9.

Two related primitives:

  - `should_escalate(samples, threshold)` — given a list of cheap "trial"
    outputs from the quick model, return True iff their disagreement is
    above threshold. When True, the runner upgrades that fire to the deep
    model + an extra research round. When False, save the cost.

  - `evaluate_prompt_variant(...)` — replay a sample of resolved outcomes
    against a candidate system-prompt variant, score Brier-vs-baseline,
    persist a `prompt_evals` row, optionally promote the variant.

The two share an ethos: spend more LLM money only where it measurably
helps. Adaptive depth applies that test PER-FIRE; prompt-eval applies it
PER-WEEK over the user's accumulated corpus.

We deliberately keep the interfaces stub-friendly. `should_escalate` takes
a list of strings — the caller decides whether those came from a real LLM
or a deterministic stub. `evaluate_prompt_variant` takes a `scorer`
callable so tests can inject a known-quality scorer without touching the
LLM. This lets the harness work in CI without an API key.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# How disagreement-y the quick-pass outputs have to be before we escalate.
# Measured as normalized token-Jaccard distance averaged across pairs.
# 0.30 is empirically: "outputs share ~70% vocabulary; one notable
# divergence in the remaining 30%." Adjust per-user via
# `risk_limits.adaptive_depth_variance_threshold` (Phase 1 schema column).
DEFAULT_VARIANCE_THRESHOLD = 0.30

# Minimum sample size before we trust a prompt-eval result. Below this the
# delta-Brier signal is dominated by noise.
PROMPT_EVAL_MIN_N = 20

# Minimum Brier improvement (absolute) before a variant gets promoted.
PROMPT_EVAL_MIN_IMPROVEMENT = 0.02


# ---------------------------------------------------------------------------
# Adaptive depth — pre-fire variance check
# ---------------------------------------------------------------------------

def _tokenize(s: str) -> set:
    return {t for t in s.lower().split() if t.isalpha()}


def _pairwise_token_diff(samples: Sequence[str]) -> float:
    """Mean pairwise normalized Jaccard distance across samples. Returns 0
    for empty / single-sample input."""
    sets = [_tokenize(s) for s in samples if s]
    if len(sets) < 2:
        return 0.0
    pairs = 0
    total = 0.0
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            inter = len(sets[i] & sets[j])
            union = len(sets[i] | sets[j])
            if union == 0:
                continue
            total += 1.0 - (inter / union)
            pairs += 1
    return total / pairs if pairs else 0.0


def should_escalate(
    samples: Sequence[str],
    *,
    threshold: float = DEFAULT_VARIANCE_THRESHOLD,
) -> bool:
    """Decide whether to escalate from quick → deep reasoning based on the
    disagreement among `samples`. Pure function — caller supplies the
    samples and the threshold; we just compute the metric and compare.

    The caller is the runner. The samples come from running the quick model
    3× at high temperature on a one-shot rating prompt. Disagreement > 30%
    by default → the answer is sensitive to noise → use the deep model.
    """
    if not samples or len(samples) < 2 or threshold <= 0:
        return False
    return _pairwise_token_diff(samples) > threshold


# ---------------------------------------------------------------------------
# Prompt-eval harness
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PromptEvalResult:
    user_id: str
    variant: str
    baseline_brier: float
    variant_brier: float
    n_samples: int
    promoted: bool
    improvement: float                     # baseline - variant; positive = better
    evaluated_at: str                      # ISO


def evaluate_prompt_variant(
    user_id: str,
    *,
    variant: str,
    scorer: Callable[[Dict[str, Any]], Optional[float]],
    baseline_scorer: Optional[Callable[[Dict[str, Any]], Optional[float]]] = None,
    min_n: int = PROMPT_EVAL_MIN_N,
    min_improvement: float = PROMPT_EVAL_MIN_IMPROVEMENT,
) -> Optional[PromptEvalResult]:
    """Replay the user's resolved decisions against a candidate prompt
    variant, score Brier-vs-baseline, persist + (optionally) promote.

    `scorer(outcome_row) -> predicted_prob_of_profit_in_[0,1] | None`. The
    harness calls this for each historical decision; None means "skip this
    sample." For real usage, `scorer` calls the candidate prompt against
    the stored input context and parses the resulting prob. For tests we
    inject a deterministic stub.

    `baseline_scorer` defaults to "use the predicted_prob_of_profit already
    stored on the outcome row" — i.e. the live prompt's behavior.

    Returns None if N below threshold (silent skip; same as calibration
    head's unlock gate).
    """
    from agenticwhales import outcomes as outcomes_mod
    rows = outcomes_mod.list_outcomes_for_user(user_id, limit=1000)
    valid = [r for r in rows if r.get("hit") is not None]
    if len(valid) < min_n:
        return None

    if baseline_scorer is None:
        baseline_scorer = lambda r: r.get("predicted_prob_of_profit")

    baseline_brier: List[float] = []
    variant_brier: List[float] = []
    for r in valid:
        hit = 1.0 if r["hit"] else 0.0
        bp = baseline_scorer(r)
        vp = scorer(r)
        if bp is None or vp is None:
            continue
        try:
            baseline_brier.append((float(bp) - hit) ** 2)
            variant_brier.append((float(vp) - hit) ** 2)
        except (TypeError, ValueError):
            continue
    if not baseline_brier:
        return None

    b_brier = statistics.mean(baseline_brier)
    v_brier = statistics.mean(variant_brier)
    improvement = b_brier - v_brier
    promoted = improvement >= min_improvement

    result = PromptEvalResult(
        user_id=user_id, variant=variant,
        baseline_brier=b_brier, variant_brier=v_brier,
        n_samples=len(baseline_brier),
        promoted=promoted, improvement=improvement,
        evaluated_at=datetime.now(tz=timezone.utc).isoformat(),
    )
    _persist_eval(result)
    return result


def _persist_eval(r: PromptEvalResult) -> None:
    from web import auth
    row = {
        "user_id": r.user_id,
        "variant": r.variant,
        "baseline_brier": r.baseline_brier,
        "variant_brier": r.variant_brier,
        "n_samples": r.n_samples,
        "promoted": r.promoted,
        "evaluated_at": r.evaluated_at,
    }
    pk = f"{r.user_id}|{r.variant}|{r.evaluated_at}"
    auth._memstore[("prompt_evals", pk)] = row
    try:
        auth._upsert_columns("prompt_evals", row)
    except Exception:
        pass


def list_recent_evals(user_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
    from web import auth
    if auth._db_writable():
        try:
            rows = auth._select_columns(
                "prompt_evals",
                filters={"user_id": user_id},
                order="evaluated_at.desc",
                limit=limit,
            ) or []
        except Exception:
            rows = []
    else:
        rows = []
    if not rows:
        rows = [
            r for (t, _), r in auth._memstore.items()
            if t == "prompt_evals" and r.get("user_id") == user_id
        ]
    rows.sort(key=lambda r: r.get("evaluated_at") or "", reverse=True)
    return rows[:limit]
