"""Trigger conditions — typed predicates that fire recipes on market events.

A recipe can carry a `TriggerCondition` (stored as JSONB in `recipes.trigger_conditions`).
When the streaming worker pushes a normalized event onto the event bus, the trigger
engine evaluates every active recipe whose tickers intersect the event symbol set.
If a condition matches, the recipe is fired immediately (subject to per-recipe
streaming rate-limit + global cost cap).

Two surfaces use the same condition language:
  * Streaming (`web.streaming_worker`)        — live events vs current snapshot
  * Backtest (`agenticwhales.backtest`)        — historical events vs as-of snapshot

Five primitive kinds + AND / OR composites.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Sequence, Union

from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)


class TriggerKind(str, Enum):
    PRICE_MOVE = "price_move"          # |Δprice / ref_price| ≥ threshold over a window
    VOLUME_SPIKE = "volume_spike"      # volume_now / avg_volume ≥ multiplier
    NEWS_KEYWORD = "news_keyword"      # headline / body contains any of keywords
    INDICATOR_CROSS = "indicator_cross"  # fast indicator crosses slow indicator
    TIME = "time"                      # specific time-of-day (UTC)


class PriceMoveCondition(BaseModel):
    kind: Literal[TriggerKind.PRICE_MOVE] = TriggerKind.PRICE_MOVE
    threshold_pct: float = Field(gt=0, lt=1, description="0.03 = 3%")
    window_minutes: int = Field(ge=1, le=24 * 60, default=60)
    direction: Literal["up", "down", "either"] = "either"


class VolumeSpikeCondition(BaseModel):
    kind: Literal[TriggerKind.VOLUME_SPIKE] = TriggerKind.VOLUME_SPIKE
    multiplier: float = Field(gt=1, description="3.0 = volume 3x the 20-day average")
    avg_window_days: int = Field(ge=1, le=90, default=20)


class NewsKeywordCondition(BaseModel):
    kind: Literal[TriggerKind.NEWS_KEYWORD] = TriggerKind.NEWS_KEYWORD
    keywords: List[str] = Field(min_length=1, max_length=20)
    case_sensitive: bool = False


class IndicatorCrossCondition(BaseModel):
    kind: Literal[TriggerKind.INDICATOR_CROSS] = TriggerKind.INDICATOR_CROSS
    fast: str = Field(description="indicator name, e.g. 'sma_20'")
    slow: str = Field(description="indicator name, e.g. 'sma_50'")
    direction: Literal["above", "below"] = "above"


class TimeCondition(BaseModel):
    kind: Literal[TriggerKind.TIME] = TriggerKind.TIME
    hour_utc: int = Field(ge=0, le=23)
    minute_utc: int = Field(ge=0, le=59, default=0)


PrimitiveCondition = Union[
    PriceMoveCondition,
    VolumeSpikeCondition,
    NewsKeywordCondition,
    IndicatorCrossCondition,
    TimeCondition,
]


class CompositeCondition(BaseModel):
    """AND/OR of primitive conditions. No nested composites — keeps the matcher flat
    and avoids JSONB unbounded depth in the database."""

    kind: Literal["and", "or"]
    children: List[PrimitiveCondition] = Field(min_length=1, max_length=10)


TriggerCondition = Union[PrimitiveCondition, CompositeCondition]


def parse_condition(raw: Any) -> Optional[TriggerCondition]:
    """Parse a JSON-loaded dict into a typed condition, or None if not configured.

    Unknown kinds raise — silent acceptance would defeat the type discipline."""
    if raw is None or raw == {} or raw == "":
        return None
    if isinstance(raw, BaseModel):
        return raw  # already parsed
    if not isinstance(raw, dict):
        raise ValueError(f"trigger_conditions must be a JSON object, got {type(raw).__name__}")
    kind = raw.get("kind")
    if kind in ("and", "or"):
        children = [_parse_primitive(c) for c in raw.get("children", [])]
        return CompositeCondition(kind=kind, children=children)
    return _parse_primitive(raw)


def _parse_primitive(raw: Dict[str, Any]) -> PrimitiveCondition:
    kind = raw.get("kind")
    if kind == TriggerKind.PRICE_MOVE.value:
        return PriceMoveCondition.model_validate(raw)
    if kind == TriggerKind.VOLUME_SPIKE.value:
        return VolumeSpikeCondition.model_validate(raw)
    if kind == TriggerKind.NEWS_KEYWORD.value:
        return NewsKeywordCondition.model_validate(raw)
    if kind == TriggerKind.INDICATOR_CROSS.value:
        return IndicatorCrossCondition.model_validate(raw)
    if kind == TriggerKind.TIME.value:
        return TimeCondition.model_validate(raw)
    raise ValueError(f"unknown trigger condition kind: {kind!r}")


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketSnapshot:
    """The minimum state needed to evaluate any primitive condition.

    Populated by the caller from whichever data source is available:
      * Streaming: ring buffer of recent ticks + cached daily OHLCV
      * Backtest:  historical OHLCV truncated to as-of date

    All optional — the matcher returns False for any condition whose required
    field is missing rather than raising, so a partial snapshot still works.
    """

    symbol: str
    last_price: Optional[float] = None
    ref_price: Optional[float] = None             # for price_move: price at start of window
    volume_now: Optional[float] = None
    avg_volume: Optional[float] = None
    headline: Optional[str] = None
    body: Optional[str] = None
    indicators: Optional[Dict[str, float]] = None  # current values
    prev_indicators: Optional[Dict[str, float]] = None  # previous bar's values
    utc_hour: Optional[int] = None
    utc_minute: Optional[int] = None


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.matched


def evaluate(condition: TriggerCondition, snapshot: MarketSnapshot) -> MatchResult:
    """Evaluate one condition against a market snapshot.

    Returns a `MatchResult` whose `.reason` field is human-readable — used by
    the audit log and the streaming worker's "why fired" annotation."""
    if isinstance(condition, CompositeCondition):
        results = [evaluate(c, snapshot) for c in condition.children]
        if condition.kind == "and":
            ok = all(r.matched for r in results)
        else:
            ok = any(r.matched for r in results)
        if not ok:
            return MatchResult(False, f"{condition.kind} not satisfied")
        return MatchResult(True, "; ".join(r.reason for r in results if r.matched))
    return _evaluate_primitive(condition, snapshot)


def _evaluate_primitive(condition: PrimitiveCondition, snapshot: MarketSnapshot) -> MatchResult:
    if isinstance(condition, PriceMoveCondition):
        if snapshot.last_price is None or snapshot.ref_price is None or snapshot.ref_price == 0:
            return MatchResult(False, "missing price data")
        change = (snapshot.last_price - snapshot.ref_price) / snapshot.ref_price
        if condition.direction == "up" and change < 0:
            return MatchResult(False, f"price down {change:.2%}, want up")
        if condition.direction == "down" and change > 0:
            return MatchResult(False, f"price up {change:.2%}, want down")
        if abs(change) < condition.threshold_pct:
            return MatchResult(False, f"|Δ| {abs(change):.2%} < {condition.threshold_pct:.2%}")
        return MatchResult(True, f"price moved {change:+.2%}")

    if isinstance(condition, VolumeSpikeCondition):
        if not snapshot.volume_now or not snapshot.avg_volume:
            return MatchResult(False, "missing volume data")
        ratio = snapshot.volume_now / snapshot.avg_volume
        if ratio < condition.multiplier:
            return MatchResult(False, f"volume {ratio:.2f}× < {condition.multiplier:.2f}×")
        return MatchResult(True, f"volume spiked {ratio:.2f}× avg")

    if isinstance(condition, NewsKeywordCondition):
        text = " ".join(filter(None, [snapshot.headline, snapshot.body]))
        if not text:
            return MatchResult(False, "no news text")
        hay = text if condition.case_sensitive else text.lower()
        for kw in condition.keywords:
            needle = kw if condition.case_sensitive else kw.lower()
            if needle in hay:
                return MatchResult(True, f"matched keyword '{kw}'")
        return MatchResult(False, "no keyword matched")

    if isinstance(condition, IndicatorCrossCondition):
        ind = snapshot.indicators or {}
        prev = snapshot.prev_indicators or {}
        if condition.fast not in ind or condition.slow not in ind:
            return MatchResult(False, "missing indicators")
        fast_now, slow_now = ind[condition.fast], ind[condition.slow]
        if condition.fast not in prev or condition.slow not in prev:
            # First bar — can't detect cross, only level
            if condition.direction == "above" and fast_now > slow_now:
                return MatchResult(True, f"{condition.fast} > {condition.slow} (level)")
            if condition.direction == "below" and fast_now < slow_now:
                return MatchResult(True, f"{condition.fast} < {condition.slow} (level)")
            return MatchResult(False, "first bar; no cross detected")
        fast_prev, slow_prev = prev[condition.fast], prev[condition.slow]
        if condition.direction == "above":
            crossed = fast_prev <= slow_prev and fast_now > slow_now
        else:
            crossed = fast_prev >= slow_prev and fast_now < slow_now
        if crossed:
            return MatchResult(True, f"{condition.fast} crossed {condition.direction} {condition.slow}")
        return MatchResult(False, f"no {condition.direction} cross")

    if isinstance(condition, TimeCondition):
        if snapshot.utc_hour is None:
            return MatchResult(False, "no clock data")
        if snapshot.utc_hour != condition.hour_utc:
            return MatchResult(False, f"hour {snapshot.utc_hour} != {condition.hour_utc}")
        if snapshot.utc_minute is not None and snapshot.utc_minute != condition.minute_utc:
            return MatchResult(False, f"minute {snapshot.utc_minute} != {condition.minute_utc}")
        return MatchResult(True, f"time {condition.hour_utc:02d}:{condition.minute_utc:02d} UTC")

    return MatchResult(False, f"unhandled condition type {type(condition).__name__}")


def required_history_days(condition: TriggerCondition) -> int:
    """Hint for streaming-cache + backtest-warmup: how many days of OHLCV
    we need to keep around to evaluate this condition."""
    if isinstance(condition, CompositeCondition):
        return max((required_history_days(c) for c in condition.children), default=1)
    if isinstance(condition, VolumeSpikeCondition):
        return condition.avg_window_days
    if isinstance(condition, IndicatorCrossCondition):
        # Heuristic: longest 'slow' value mentioned, default 50
        for token in [condition.slow, condition.fast]:
            for sep in ("_", "-"):
                if sep in token:
                    try:
                        return max(50, int(token.split(sep)[-1]))
                    except ValueError:
                        pass
        return 50
    return 1
