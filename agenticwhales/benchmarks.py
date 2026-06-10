"""Discipline-score benchmarks — REAL cohort percentiles or nothing.

The honesty rule (2026-06-10 compliance review): a comparative claim
("top X%") may only come from measured data. Published behavioral-finance
research (Barber & Odean 2000; Odean 1998) motivates *which* leaks the coach
detects, but contains no discipline-score distribution — so no percentile is
shown until the real cohort reaches `COHORT_MIN` distinct traders. Until then
the UI says exactly that: "cohort percentiles unlock at N traders."

Threshold rationale: at n=50 a single user moves a midpoint percentile by ~2
points — inside the resolution of the 10-point bands we display. Below that,
the claim is sampling noise.
"""

from __future__ import annotations

import os
from typing import Dict, List, Sequence

DEFAULT_COHORT_MIN = 50


def cohort_min() -> int:
    try:
        return max(2, int(os.getenv("AGENTICWHALES_COHORT_MIN", str(DEFAULT_COHORT_MIN))))
    except ValueError:
        return DEFAULT_COHORT_MIN


def percentile_rank(score: float, cohort_scores: Sequence[float]) -> float:
    """Midpoint percentile: (below + half of equals) / n. Deterministic,
    tie-stable, in [0, 100]."""
    n = len(cohort_scores)
    if n == 0:
        return 50.0
    below = sum(1 for s in cohort_scores if s < score)
    equal = sum(1 for s in cohort_scores if s == score)
    return round(100.0 * (below + 0.5 * equal) / n, 1)


def band(percentile: float) -> str:
    """10/25-point bands — coarse on purpose; early cohorts are lumpy."""
    if percentile >= 90:
        return "top 10%"
    if percentile >= 75:
        return "top 25%"
    if percentile >= 50:
        return "top half"
    if percentile >= 25:
        return "bottom half"
    if percentile >= 10:
        return "bottom 25%"
    return "bottom 10%"


def score_benchmark(score: float, cohort_scores: Sequence[float]) -> Dict:
    """Benchmark payload for a score. Two honest modes only:

    - cohort == "real":   enough traders -> percentile + band + a label that
                          NAMES the cohort and its size.
    - cohort == "pending": not enough -> NO comparative claim; the payload
                          says when percentiles unlock.
    """
    n = len(cohort_scores)
    threshold = cohort_min()
    if n < threshold:
        return {
            "cohort": "pending",
            "n_cohort": n,
            "unlock_at": threshold,
            "label": f"cohort percentiles unlock at {threshold} traders "
                     f"({n} audited so far)",
        }
    p = percentile_rank(score, cohort_scores)
    return {
        "cohort": "real",
        "n_cohort": n,
        "percentile": p,
        "band": band(p),
        "label": f"{band(p)} of {n} AgenticWhales traders",
    }
