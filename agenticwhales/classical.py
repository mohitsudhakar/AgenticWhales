"""Classical (non-LLM) analyst — Phase 2 deliverable #6.

Option A (deterministic signal stack) + Option D (deterministic QuantRadar
generator) per the agreed plan. Pure-Python signals computed from OHLCV
prices; combined via weighted vote; emitted as a `PortfolioDecision`.

Why we want this: the Bull/Bear/PM debate is heterogeneous *across LLM
families* but still LLM-driven. A rules-based voice with totally different
failure modes makes the disagreement-index meaningful and surfaces cases
where the LLM debate is talking itself into a thesis the math doesn't
support.

Signals (each emits direction ∈ {-1,0,+1} + strength ∈ [0,1]):

  - **Momentum (12-1):** total return over past ~252 trading days,
    excluding the most-recent ~21 days. Classic Jegadeesh-Titman factor.
  - **Mean reversion (Bollinger):** last close vs 20-day Bollinger Bands.
    Above upper band → short signal; below lower → long.
  - **Trend (50/200 SMA):** golden cross → long bias, death cross → short.
    Strength scales with the spread.
  - **Volatility regime (ATR percentile):** trailing-year ATR percentile.
    High vol *dampens* conviction (multiplies the combined strength).

Aggregation: weighted vote → score ∈ [-1, +1] → 5-tier rating.

QuantRadar (Option D): same OHLCV input, 6 deterministic dimensions
matching the existing `QuantRadar` Pydantic schema. The Classical Analyst
hands out this radar to downstream consumers — Bull/Bear see both the
LLM-driven Quant Analyst's radar AND the Classical Analyst's deterministic
one. The two radars disagreeing IS the signal.

Operationally pure: takes a ticker + a date, returns a `PortfolioDecision`
+ a `QuantRadar`. No state, no I/O beyond OHLCV fetch. Easy to test, easy
to swap signals, easy to extend.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

import pandas as pd

from .agents.schemas import PortfolioDecision, PortfolioRating, QuantRadar


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

MOMENTUM_LOOKBACK = 252      # ~12 months of trading days
MOMENTUM_SKIP = 21           # skip last ~1 month
MOMENTUM_THRESHOLD = 0.10    # |return| > 10% → direction signal

BOLLINGER_WINDOW = 20
BOLLINGER_K = 2.0

SMA_FAST = 50
SMA_SLOW = 200

ATR_WINDOW = 14
ATR_LOOKBACK_FOR_PERCENTILE = 252

# Weighted vote across signals. Pick sums to 1.0 so the score stays in [-1,+1].
SIGNAL_WEIGHTS = {
    "momentum": 0.35,
    "trend": 0.30,
    "mean_reversion": 0.20,
    "vol_regime": 0.15,   # vol dampens via separate multiplier, not direction
}

# Direction multiplier per rating cutoff.
RATING_CUTOFFS = [
    ( 0.50, PortfolioRating.BUY),
    ( 0.20, PortfolioRating.OVERWEIGHT),
    (-0.20, PortfolioRating.HOLD),
    (-0.50, PortfolioRating.UNDERWEIGHT),
    (-1.01, PortfolioRating.SELL),
]


# ---------------------------------------------------------------------------
# Pure-function signal helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Signal:
    name: str
    direction: int        # -1, 0, +1
    strength: float       # 0..1
    notes: str = ""


def _safe_pct(num: float, den: float) -> float:
    return float(num) / float(den) if den else 0.0


def momentum_signal(close: pd.Series) -> Signal:
    """12-1 momentum. Direction from total return ex-last-month; strength
    scales with magnitude. Stable on missing data (returns neutral)."""
    if len(close) < MOMENTUM_LOOKBACK + MOMENTUM_SKIP:
        return Signal("momentum", 0, 0.0, "insufficient history")
    p_old = float(close.iloc[-(MOMENTUM_LOOKBACK + MOMENTUM_SKIP)])
    p_ref = float(close.iloc[-MOMENTUM_SKIP])
    if p_old <= 0:
        return Signal("momentum", 0, 0.0, "zero / negative reference price")
    ret = (p_ref - p_old) / p_old
    direction = 1 if ret > MOMENTUM_THRESHOLD else (-1 if ret < -MOMENTUM_THRESHOLD else 0)
    strength = min(1.0, abs(ret) / 0.40)  # 40% return → max strength
    return Signal("momentum", direction, strength,
                  f"12-1 return {ret*100:.1f}%")


def bollinger_signal(close: pd.Series) -> Signal:
    """Mean-reversion: price relative to Bollinger Bands. Outside upper →
    short; below lower → long; inside → neutral."""
    if len(close) < BOLLINGER_WINDOW:
        return Signal("mean_reversion", 0, 0.0, "insufficient history")
    window = close.iloc[-BOLLINGER_WINDOW:]
    mean = float(window.mean())
    std = float(window.std(ddof=0))
    if std <= 0:
        return Signal("mean_reversion", 0, 0.0, "zero stdev")
    upper = mean + BOLLINGER_K * std
    lower = mean - BOLLINGER_K * std
    price = float(close.iloc[-1])
    # Reversion: above upper → expect drop → short signal (direction = -1).
    if price > upper:
        z = (price - upper) / std
        return Signal("mean_reversion", -1, min(1.0, z / 2.0),
                      f"price {price:.2f} above upper band {upper:.2f}")
    if price < lower:
        z = (lower - price) / std
        return Signal("mean_reversion", 1, min(1.0, z / 2.0),
                      f"price {price:.2f} below lower band {lower:.2f}")
    return Signal("mean_reversion", 0, 0.0, "price inside bands")


def trend_signal(close: pd.Series) -> Signal:
    """50/200 SMA cross. Golden cross → long; death cross → short.
    Strength scales with the percent spread between the two SMAs."""
    if len(close) < SMA_SLOW:
        return Signal("trend", 0, 0.0, "insufficient history for 200 SMA")
    sma_fast = float(close.iloc[-SMA_FAST:].mean())
    sma_slow = float(close.iloc[-SMA_SLOW:].mean())
    if sma_slow <= 0:
        return Signal("trend", 0, 0.0, "zero slow SMA")
    spread = (sma_fast - sma_slow) / sma_slow
    direction = 1 if spread > 0 else (-1 if spread < 0 else 0)
    strength = min(1.0, abs(spread) / 0.20)   # 20% spread → max strength
    return Signal("trend", direction, strength,
                  f"50 SMA {sma_fast:.2f} vs 200 SMA {sma_slow:.2f} "
                  f"({spread*100:+.1f}% spread)")


def atr_value(high: pd.Series, low: pd.Series, close: pd.Series,
              window: int = ATR_WINDOW) -> float:
    """Simple ATR(14) over True Range."""
    if len(close) < window + 1:
        return 0.0
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.tail(window).mean())


def vol_regime_multiplier(high: pd.Series, low: pd.Series, close: pd.Series) -> Tuple[float, str]:
    """Compute trailing-year ATR percentile. Returns a multiplier in [0.5, 1.0]
    that dampens the combined directional score when vol is unusually high.

    Logic: highest decile vol → multiplier 0.5; lowest decile → 1.0.
    Idea is that extreme vol means the signals are less reliable; we don't
    flip the sign, we shrink the magnitude."""
    if len(close) < ATR_LOOKBACK_FOR_PERCENTILE + ATR_WINDOW + 1:
        return 1.0, "insufficient history"
    # Rolling ATR over the trailing year, then percentile of latest.
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    rolling = tr.rolling(window=ATR_WINDOW).mean().tail(ATR_LOOKBACK_FOR_PERCENTILE).dropna()
    if rolling.empty:
        return 1.0, "insufficient ATR history"
    latest = float(rolling.iloc[-1])
    pct = (rolling <= latest).mean()
    # pct ∈ [0, 1]; map to multiplier [0.5, 1.0] inversely.
    multiplier = 1.0 - 0.5 * pct
    return multiplier, f"ATR percentile {pct*100:.0f}% → conviction × {multiplier:.2f}"


# ---------------------------------------------------------------------------
# Aggregation → PortfolioDecision
# ---------------------------------------------------------------------------

def _rating_for(score: float) -> PortfolioRating:
    for cutoff, rating in RATING_CUTOFFS:
        if score >= cutoff:
            return rating
    return PortfolioRating.SELL


def _compose_decision(
    score: float,
    signals: list,
    vol_note: str,
    last_price: float,
    atr: float,
) -> PortfolioDecision:
    """Map aggregated score + signals → a fully-populated PortfolioDecision."""
    rating = _rating_for(score)
    confidence = max(1, min(10, int(round(abs(score) * 10))))
    direction_sign = 1 if score > 0 else -1 if score < 0 else 0

    # Bracket levels from ATR — 1 ATR stop, 2 ATR target (R:R 2:1).
    if direction_sign > 0:
        stop_loss = max(0.0, last_price - atr)
        take_profit = last_price + 2 * atr
    elif direction_sign < 0:
        stop_loss = last_price + atr
        take_profit = max(0.0, last_price - 2 * atr)
    else:
        stop_loss = None
        take_profit = None

    # Scalars: keep conservative — Classical Analyst is honest about its limits.
    expected_return_pct = direction_sign * abs(score) * 15.0   # cap ~15% as the central tendency
    annualized_vol = (atr / last_price * 100 * math.sqrt(252)) if last_price > 0 else None
    prob_of_profit = max(0.30, min(0.70, 0.50 + score * 0.20))
    expected_hold_days = 45

    rationale_lines = [
        f"**Classical aggregate score:** {score:+.3f} → {rating.value}",
        "",
        "**Signals:**",
    ] + [f"- {s.name}: dir {s.direction:+d}, str {s.strength:.2f} — {s.notes}" for s in signals] + [
        "",
        f"**Vol regime:** {vol_note}",
        "",
        "Classical Analyst is a deterministic rules-based voice; it carries no narrative or news context. "
        "Use it as an adversarial check on the LLM debate's qualitative arguments.",
    ]

    return PortfolioDecision(
        rating=rating,
        executive_summary=(
            f"Classical aggregate: {rating.value} (score {score:+.3f}). "
            f"Direction from weighted signals: momentum, trend, mean-reversion, "
            f"vol-regime-dampened."
        ),
        investment_thesis="\n".join(rationale_lines),
        stop_loss=stop_loss,
        take_profit=take_profit,
        expected_return_pct=expected_return_pct,
        expected_volatility_pct=annualized_vol,
        prob_of_profit=prob_of_profit,
        expected_hold_days=expected_hold_days,
        time_horizon=f"~{expected_hold_days} trading days",
    )


def _compose_radar(
    high: pd.Series, low: pd.Series, close: pd.Series,
    signals: list,
    aggregate_score: float,
) -> QuantRadar:
    """Deterministic 6-dim radar mirroring the existing `QuantRadar` schema.

    Each axis is on a 1-10 integer scale; the rubric maps directly to
    measurable quantities:
      - volatility_risk: ATR percentile (we already computed)
      - sr_strength:     distance from rolling 20-day high/low to current
      - breakout_likelihood: inverse Bollinger width (squeeze → high)
      - momentum_strength: |12-1 return| → 1-10
      - pattern_reliability: 1 for now (real pattern detection is Phase 3)
      - trend_certainty: |50/200 SMA spread| → 1-10
    """
    # Helper: bucket a fractional value into a 1-10 score.
    def to_score(x: float) -> int:
        return max(1, min(10, int(round(1 + x * 9))))

    # Volatility.
    _, vol_note = vol_regime_multiplier(high, low, close)
    vol_pct = float(vol_note.split("ATR percentile ")[1].split("%")[0]) / 100.0 \
        if "ATR percentile" in vol_note else 0.5
    volatility_risk = to_score(vol_pct)

    # S/R strength: how close are we to a 20-day high/low? Tight to either =
    # strong nearby level. Strength = 1 - relative_distance.
    window = close.tail(20)
    hi = float(window.max()); lo = float(window.min())
    if hi > lo:
        d_hi = abs(float(close.iloc[-1]) - hi) / (hi - lo)
        d_lo = abs(float(close.iloc[-1]) - lo) / (hi - lo)
        sr_strength = to_score(1.0 - min(d_hi, d_lo))
    else:
        sr_strength = 1

    # Breakout likelihood: Bollinger squeeze. Narrower band relative to history → higher.
    if len(close) >= 60:
        recent_std = float(close.tail(20).std(ddof=0))
        long_std = float(close.tail(60).std(ddof=0))
        squeeze = 1.0 - min(1.0, recent_std / max(long_std, 1e-9)) if long_std > 0 else 0.0
    else:
        squeeze = 0.0
    breakout_likelihood = to_score(squeeze)

    # Momentum strength.
    mom = next((s for s in signals if s.name == "momentum"), None)
    momentum_strength = to_score(mom.strength if mom else 0.0)

    # Trend certainty.
    trend = next((s for s in signals if s.name == "trend"), None)
    trend_certainty = to_score(trend.strength if trend else 0.0)

    # Pattern reliability: not implemented deterministically yet. Score 1.
    pattern_reliability = 1

    if aggregate_score > 0.05:
        direction = "long"
    elif aggregate_score < -0.05:
        direction = "short"
    else:
        direction = "neutral"

    return QuantRadar(
        volatility_risk=volatility_risk,
        sr_strength=sr_strength,
        breakout_likelihood=breakout_likelihood,
        momentum_strength=momentum_strength,
        pattern_reliability=pattern_reliability,
        trend_certainty=trend_certainty,
        direction=direction,
        reasoning=(
            "Deterministic radar derived from OHLCV. ATR percentile drives "
            "volatility_risk; 20-day high/low proximity drives sr_strength; "
            "rolling-stdev squeeze drives breakout_likelihood. No LLM in the loop."
        ),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClassicalResult:
    decision: PortfolioDecision
    radar: QuantRadar
    aggregate_score: float
    signals: list                       # list of `Signal` dataclasses
    last_price: float


def analyze_classical(ticker: str, curr_date: str) -> Optional[ClassicalResult]:
    """End-to-end deterministic analysis for a ticker as-of a date.

    Returns None if not enough price history exists (silent — callers fall
    back to running without the Classical voice). Look-ahead-safe: uses
    `dataflows.stockstats_utils.load_ohlcv` which already date-filters.
    """
    from .dataflows.stockstats_utils import load_ohlcv

    try:
        df = load_ohlcv(ticker, curr_date)
    except Exception:
        return None
    if df is None or df.empty or len(df) < SMA_SLOW:
        return None

    close = pd.to_numeric(df["Close"], errors="coerce").dropna().reset_index(drop=True)
    high  = pd.to_numeric(df["High"],  errors="coerce").dropna().reset_index(drop=True)
    low   = pd.to_numeric(df["Low"],   errors="coerce").dropna().reset_index(drop=True)
    if close.empty:
        return None

    signals = [
        momentum_signal(close),
        trend_signal(close),
        bollinger_signal(close),
    ]
    vol_mult, vol_note = vol_regime_multiplier(high, low, close)

    # Weighted vote — direction × strength × weight, summed.
    raw_score = 0.0
    for s in signals:
        weight = SIGNAL_WEIGHTS.get(s.name, 0.0)
        raw_score += s.direction * s.strength * weight
    # Vol regime acts as a multiplier on magnitude, not a direction signal.
    raw_score *= vol_mult
    raw_score = max(-1.0, min(1.0, raw_score))

    atr = atr_value(high, low, close)
    last_price = float(close.iloc[-1])

    decision = _compose_decision(raw_score, signals, vol_note, last_price, atr)
    radar = _compose_radar(high, low, close, signals, raw_score)
    return ClassicalResult(
        decision=decision, radar=radar,
        aggregate_score=raw_score, signals=signals, last_price=last_price,
    )
