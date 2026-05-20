"""Multi-timeframe fan-out helper for Phase 3.

A thesis can analyze the same ticker on multiple timeframes (1m, 1h, 1d) and
fold them into one decision. The fan-out strategy is deliberately simple:

  1. Spawn N independent SessionRunner invocations (one per timeframe) with
     timeframe-specific lookback windows and indicator settings. Each produces
     its own `PortfolioDecision`.
  2. Merge those decisions with weights derived from the expected hold horizon —
     short holds weight 1m more, long holds weight 1d more.
  3. Emit a single merged `PortfolioDecision` for downstream sizing.

This module owns *only* the helpers; the runner orchestration that calls them
lives in `web.runner`. The split keeps this module pure / testable.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence

from .agents.schemas import PortfolioDecision, PortfolioRating

log = logging.getLogger(__name__)


# Canonical timeframes (string keys for JSON-friendly storage on Recipe.timeframes).
CANONICAL_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")

# Map each timeframe to a representative horizon in days. Used to compute
# per-timeframe weights from an expected-hold-days target.
_TF_DAYS: Dict[str, float] = {
    "1m": 1.0 / (60 * 6.5),
    "5m": 5.0 / (60 * 6.5),
    "15m": 15.0 / (60 * 6.5),
    "1h": 1.0 / 6.5,
    "4h": 4.0 / 6.5,
    "1d": 1.0,
}


# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------

def tf_weights(timeframes: Sequence[str], expected_hold_days: float = 5.0) -> Dict[str, float]:
    """Weight each timeframe by how close it is to ~1/5 of the expected hold.

    Trader rule-of-thumb: your analysis timeframe should be about a fifth of
    your hold horizon — fine enough to see structure, coarse enough to avoid
    noise. Weight = `1 / (1 + |log(tf_days / ideal_tf_days)|)`, normalized.

    Empty input → empty output. Unknown timeframe codes are ignored.
    """
    ideal = max(expected_hold_days / 5.0, 1e-6)
    raw: Dict[str, float] = {}
    for tf in timeframes:
        days = _TF_DAYS.get(tf)
        if days is None or days <= 0:
            continue
        ratio = days / ideal
        raw[tf] = 1.0 / (1.0 + abs(math.log(ratio)))
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in raw.items()}


# ---------------------------------------------------------------------------
# Decision merge
# ---------------------------------------------------------------------------

_RATING_SCALE: Dict[PortfolioRating, int] = {
    PortfolioRating.SELL: -2,
    PortfolioRating.UNDERWEIGHT: -1,
    PortfolioRating.HOLD: 0,
    PortfolioRating.OVERWEIGHT: 1,
    PortfolioRating.BUY: 2,
}

_SCALE_TO_RATING: List[PortfolioRating] = [
    PortfolioRating.SELL,
    PortfolioRating.UNDERWEIGHT,
    PortfolioRating.HOLD,
    PortfolioRating.OVERWEIGHT,
    PortfolioRating.BUY,
]


def merge_decisions(
    decisions: Mapping[str, PortfolioDecision],
    weights: Optional[Mapping[str, float]] = None,
) -> Optional[PortfolioDecision]:
    """Combine per-timeframe decisions into a single decision.

    * Rating is the weighted average mapped back onto the 5-tier scale.
    * `expected_return_pct`, `expected_volatility_pct`, `prob_of_profit`,
      `expected_hold_days`, `confidence`, `stop_loss_pct` are weighted averages.
    * `stop_loss` is the median across decisions that have one (more robust
      than a weighted mean to a single outlier).
    * `reasoning` is the longest input reasoning, with a header noting the merge.
    * The composite carries the *minimum* `prob_of_profit` and *maximum*
      `expected_volatility_pct` from the underlying decisions in the notes,
      so the human reader can see disagreement at a glance.

    Returns None if no usable decisions are supplied.
    """
    if not decisions:
        return None
    weights = weights or {tf: 1.0 / len(decisions) for tf in decisions}
    # Normalize across the actual decisions we have.
    total_w = sum(weights.get(tf, 0.0) for tf in decisions)
    if total_w <= 0:
        return None

    norm_w = {tf: weights.get(tf, 0.0) / total_w for tf in decisions}

    rating_score = sum(_RATING_SCALE[d.rating] * norm_w[tf] for tf, d in decisions.items())
    # Map back to nearest 5-tier bucket.
    rating_idx = max(0, min(4, int(round(rating_score)) + 2))
    rating = _SCALE_TO_RATING[rating_idx]

    def w_avg(getter, default=0.0) -> float:
        total = 0.0
        seen = 0.0
        for tf, d in decisions.items():
            v = getter(d)
            if v is None:
                continue
            total += float(v) * norm_w[tf]
            seen += norm_w[tf]
        return total / seen if seen > 0 else default

    expected_return = w_avg(lambda d: d.expected_return_pct)
    expected_vol = w_avg(lambda d: d.expected_volatility_pct, default=20.0)
    prob = max(0.0, min(1.0, w_avg(lambda d: d.prob_of_profit, default=0.5)))
    hold_days = max(1, int(round(w_avg(lambda d: d.expected_hold_days, default=5))))

    stops = [d.stop_loss for d in decisions.values() if d.stop_loss is not None]
    stop = statistics.median(stops) if stops else None

    min_prob = min((d.prob_of_profit for d in decisions.values()
                    if d.prob_of_profit is not None), default=prob)
    max_vol = max((d.expected_volatility_pct for d in decisions.values()
                   if d.expected_volatility_pct is not None), default=expected_vol)
    disagree = round(_disagreement_index(decisions), 2)

    longest_reason = max(
        (d.investment_thesis for d in decisions.values()),
        key=len, default="",
    )
    summaries = [
        f"{tf}: {d.rating.value} (p={d.prob_of_profit}, er={d.expected_return_pct})"
        for tf, d in decisions.items()
    ]

    notes_text = (
        f"Multi-TF merge. min_prob={min_prob:.2f} max_vol={max_vol:.2f} "
        f"disagree={disagree}"
    )
    return PortfolioDecision(
        rating=rating,
        stop_loss=stop,
        expected_return_pct=expected_return,
        expected_volatility_pct=expected_vol,
        prob_of_profit=prob,
        expected_hold_days=hold_days,
        executive_summary=(
            f"Multi-TF merge of {len(decisions)} timeframe(s). "
            f"Weighted rating={rating.value}, disagreement={disagree}. "
            f"Per-TF: " + "; ".join(summaries)
        ),
        investment_thesis=(
            (longest_reason or "Multi-timeframe merge.") + " | " + notes_text
        ),
    )


def _disagreement_index(decisions: Mapping[str, PortfolioDecision]) -> float:
    """0.0 = perfect agreement, 1.0 = max spread across the 5-tier rating.

    Uses sample stddev of the rating score, normalized to the maximum
    possible stddev (`2.0` = SELL-to-BUY span)."""
    scores = [_RATING_SCALE[d.rating] for d in decisions.values()]
    if len(scores) < 2:
        return 0.0
    return min(1.0, statistics.pstdev(scores) / 2.0)
