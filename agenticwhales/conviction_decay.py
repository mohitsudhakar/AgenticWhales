"""Conviction-score time-decay + macro re-evaluation gates (Phase 3 #4).

A `conviction_scores` row is recorded at the moment of decision. Without decay,
a 9/10 conviction call from 21 days ago shows up alongside today's 7/10 — but a
21-day-old call is far less informative for *now*. Exponential decay with a
configurable half-life keeps the score honest.

The "macro re-eval" gate detects when broad market conditions have shifted
materially since a conviction was recorded (large SPY move or VIX jump), and
returns True so the caller (UI / scheduler) knows to prompt the user to refresh
or auto-fires a re-analysis.

Pure functions — caller passes in current macro snapshots and decay parameters.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass
from typing import Iterable, List, Optional


DEFAULT_HALF_LIFE_DAYS = 5.0
DEFAULT_SPY_SIGMA_THRESHOLD = 2.0
DEFAULT_VIX_DELTA_THRESHOLD = 5.0


# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------

def decayed_conviction(
    raw_score: float,
    recorded_at,
    *,
    now=None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """Apply exponential time-decay to a raw conviction score.

    `decayed = raw * 2^(-age_days / half_life)`. Score is clamped to [0, 10].
    Negative age (clock skew) is treated as 0. Half-life ≤ 0 → no decay.
    """
    if half_life_days <= 0:
        return float(raw_score)
    when = _coerce_dt(recorded_at)
    base = _coerce_dt(now) if now is not None else _dt.datetime.now(tz=_dt.timezone.utc)
    age_days = max(0.0, (base - when).total_seconds() / 86400.0)
    factor = 2.0 ** (-age_days / half_life_days)
    return max(0.0, min(10.0, float(raw_score) * factor))


# ---------------------------------------------------------------------------
# Macro re-eval gate
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MacroDelta:
    """Snapshot of macro conditions at two points in time, for the re-eval gate.

    `spy_sigma` is the realized move in SPY since the conviction was recorded,
    expressed in stdevs of its recent daily return distribution. `vix_delta`
    is the absolute change in VIX level (not pct). Both are optional — a
    missing field skips that leg of the gate.
    """

    spy_sigma: Optional[float] = None
    vix_delta: Optional[float] = None


def macro_shifted(
    delta: MacroDelta,
    *,
    spy_sigma_threshold: float = DEFAULT_SPY_SIGMA_THRESHOLD,
    vix_delta_threshold: float = DEFAULT_VIX_DELTA_THRESHOLD,
) -> bool:
    """Return True if the macro snapshot crosses either threshold.

    Conservative defaults: SPY moved >2σ from when the conviction was recorded,
    or VIX shifted >5 points. Tuned to fire on real regime changes (a quiet
    drift over a week doesn't trigger), not noise.
    """
    if delta.spy_sigma is not None and abs(delta.spy_sigma) >= spy_sigma_threshold:
        return True
    if delta.vix_delta is not None and abs(delta.vix_delta) >= vix_delta_threshold:
        return True
    return False


# ---------------------------------------------------------------------------
# Timeseries projection (UI chart helper)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConvictionPoint:
    ts: _dt.datetime
    raw_score: float
    decayed_score: float


def project_timeseries(
    rows: Iterable[dict],
    *,
    now=None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> List[ConvictionPoint]:
    """Map raw conviction rows into the timeseries the /fund chart consumes.

    Each input row must have `recorded_at` (str / datetime) and
    `conviction_score`. Output is sorted ascending by timestamp, with raw and
    decayed scores. Decay is computed as-of `now` (default: utcnow), so the
    *most recent* point shows the live decayed value the UI displays.
    """
    base = _coerce_dt(now) if now is not None else _dt.datetime.now(tz=_dt.timezone.utc)
    points: List[ConvictionPoint] = []
    for row in rows:
        score = row.get("conviction_score")
        rec_at = row.get("recorded_at") or row.get("created_at")
        if score is None or rec_at is None:
            continue
        when = _coerce_dt(rec_at)
        decayed = decayed_conviction(
            float(score), when, now=base, half_life_days=half_life_days,
        )
        points.append(ConvictionPoint(
            ts=when, raw_score=float(score), decayed_score=decayed,
        ))
    points.sort(key=lambda p: p.ts)
    return points


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_dt(value) -> _dt.datetime:
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    if isinstance(value, _dt.date):
        return _dt.datetime.combine(value, _dt.time.min, tzinfo=_dt.timezone.utc)
    if isinstance(value, (int, float)):
        return _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc)
    s = str(value).strip()
    try:
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        try:
            return _dt.datetime.fromtimestamp(float(s), tz=_dt.timezone.utc)
        except ValueError as exc:
            raise ValueError(f"cannot coerce {value!r} to datetime") from exc
