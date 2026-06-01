"""Natural-language → tracked trading-strategy compiler + backtest generator.

This is the "strategy agent" referenced on the landing page: a user describes a
thesis in plain English —

    "if SMCI breaks $1,200 on more than 2x average volume, fade it"

— and an LLM compiles it into a structured :class:`StrategySpec`: an entry
*trigger* (reusing the typed condition language in :mod:`agenticwhales.triggers`),
a direction (go long / fade), a stop-loss, and a hold horizon.

The same spec drives two surfaces:
  * Backtest — :func:`strategy_decision_generator` turns the spec into a
    :class:`DecisionGenerator` for :func:`agenticwhales.backtest.run_backtest`,
    so the user immediately sees results on real historical market data.
  * Live recipe — the spec's trigger serializes straight into a recipe's
    ``trigger_conditions`` JSONB so the streaming worker fires it 24/7.

Compilation is routed through ``agenticwhales.llm_clients`` (the project
invariant) and the LLM is injectable for tests. Analysis only — never trades.
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd

from .agents.schemas import PortfolioDecision, PortfolioRating
from .triggers import (
    MarketSnapshot,
    TriggerCondition,
    evaluate,
    parse_condition,
)

log = logging.getLogger(__name__)


COMPILE_SYSTEM = """You are a quantitative strategy compiler. You convert a trader's plain-English thesis into a SINGLE structured JSON rule that a backtester can execute. You do NOT give advice; you only translate.

Return ONLY a JSON object with EXACTLY this shape:
{
  "name": "short human label, <= 6 words",
  "direction": "long" | "fade",
  "entry": { ...ONE trigger condition... },
  "stop_loss_pct": 0.05,
  "hold_days": 20,
  "rationale": "one sentence paraphrasing the user's rule"
}

DIRECTION:
- "long"  = the user wants to BUY / go long / accumulate when the condition hits.
- "fade"  = the user wants to SELL / short / fade / bet against the move when the condition hits.
  Map "fade it", "short", "bet against", "it'll reverse" to "fade".

ENTRY — choose the ONE trigger kind that best matches the thesis:
1. price_move — a % move over a window:
   {"kind":"price_move","threshold_pct":0.05,"window_minutes":1440,"direction":"up"|"down"|"either"}
   (threshold_pct is a FRACTION: 0.05 = 5%. window_minutes: 1440 = one day.)
2. volume_spike — volume vs its average:
   {"kind":"volume_spike","multiplier":2.0,"avg_window_days":20}
   (multiplier 2.0 = "2x average volume".)
3. indicator_cross — a fast indicator crossing a slow one:
   {"kind":"indicator_cross","fast":"sma_20","slow":"sma_50","direction":"above"|"below"}
   (Use sma_N / ema_N names. "golden cross" = sma_50 above sma_200. "death cross" = sma_50 below sma_200.
    "breaks above its 50-day average" = fast close, slow sma_50, above.)
4. price_level — price crossing an absolute dollar level:
   {"kind":"price_level","level":1200.0,"direction":"above"|"below"}
   (Use this for "breaks $1,200", "falls below $50", etc.)

COMPOSITE — if the thesis combines two conditions ("breaks $1200 AND on 2x volume"):
   {"kind":"and","children":[ {...}, {...} ]}   (or "or")

RULES:
- Infer sensible defaults when the user is vague: stop_loss_pct 0.05, hold_days 20.
- "breaks $1,200 on more than 2x average volume, fade it" =>
  direction "fade", entry = and(price_level 1200 above, volume_spike 2.0).
- Output STRICT JSON only, no prose, no code fences."""


_VALID_DIRECTIONS = ("long", "fade")


@dataclass
class StrategySpec:
    """A compiled, executable trading strategy."""

    name: str
    direction: str                 # 'long' | 'fade'
    entry_raw: Dict[str, Any]      # raw trigger dict (may include price_level)
    stop_loss_pct: float
    hold_days: int
    rationale: str
    source_text: str               # the original NL thesis

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "direction": self.direction,
            "entry": self.entry_raw,
            "stop_loss_pct": self.stop_loss_pct,
            "hold_days": self.hold_days,
            "rationale": self.rationale,
            "source_text": self.source_text,
        }

    def to_trigger_conditions(self) -> Optional[Dict[str, Any]]:
        """Serialize the entry to a recipe ``trigger_conditions`` JSONB payload.

        ``price_level`` is not a streaming trigger kind, so it is lowered to a
        ``price_move`` approximation for the live path; the backtest path uses
        the exact level. Composite ``and``/``or`` are passed through with each
        child lowered the same way.
        """
        return _lower_for_recipe(self.entry_raw)


class StrategyError(Exception):
    """Raised when a thesis cannot be compiled into a valid strategy."""


def _clamp(v: Any, lo: float, hi: float, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return max(lo, min(hi, f))


def _lower_for_recipe(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Recipe trigger_conditions can't carry price_level; approximate it."""
    if not isinstance(entry, dict):
        return None
    kind = entry.get("kind")
    if kind in ("and", "or"):
        children = [_lower_for_recipe(c) for c in entry.get("children", [])]
        children = [c for c in children if c]
        if not children:
            return None
        return {"kind": kind, "children": children}
    if kind == "price_level":
        # Lower to a 3% directional move as a streaming-friendly stand-in.
        direction = "up" if entry.get("direction") == "above" else "down"
        return {"kind": "price_move", "threshold_pct": 0.03, "window_minutes": 1440, "direction": direction}
    return entry


def compile_strategy(
    thesis: str,
    *,
    llm=None,
    provider: str = "google",
    model: str = "gemini-3-flash-preview",
    base_url: Optional[str] = None,
) -> StrategySpec:
    """Compile a plain-English thesis into a :class:`StrategySpec` via an LLM."""
    thesis = (thesis or "").strip()
    if not thesis:
        raise StrategyError("Empty thesis.")

    if llm is None:
        from agenticwhales.llm_clients import create_llm_client

        llm = create_llm_client(provider=provider, model=model, base_url=base_url).get_llm()

    from agenticwhales.transactions.extract import _invoke_llm, parse_json_loose

    user = f'Compile this trading thesis into the JSON rule:\n\n"""\n{thesis}\n"""'
    try:
        raw = _invoke_llm(llm, COMPILE_SYSTEM, user)
        parsed = parse_json_loose(raw)
    except Exception as e:  # noqa: BLE001
        raise StrategyError(f"Could not compile thesis: {e}") from e

    if not isinstance(parsed, dict):
        raise StrategyError("Compiler did not return a JSON object.")

    direction = str(parsed.get("direction", "long")).strip().lower()
    if direction not in _VALID_DIRECTIONS:
        direction = "long"

    entry = parsed.get("entry")
    if not isinstance(entry, dict) or not entry:
        raise StrategyError("Compiled strategy is missing an entry condition.")

    # Validate the entry parses (price_level is validated separately below since
    # it's a backtest-only synthetic kind not known to triggers.parse_condition).
    _validate_entry(entry)

    return StrategySpec(
        name=str(parsed.get("name") or "Untitled strategy")[:64],
        direction=direction,
        entry_raw=entry,
        stop_loss_pct=_clamp(parsed.get("stop_loss_pct"), 0.005, 0.5, 0.05),
        hold_days=int(_clamp(parsed.get("hold_days"), 1, 252, 20)),
        rationale=str(parsed.get("rationale") or "").strip(),
        source_text=thesis,
    )


def _validate_entry(entry: Dict[str, Any]) -> None:
    """Validate an entry dict, allowing the synthetic price_level kind."""
    kind = entry.get("kind")
    if kind in ("and", "or"):
        kids = entry.get("children") or []
        if not kids:
            raise StrategyError("Composite entry has no children.")
        for c in kids:
            _validate_entry(c)
        return
    if kind == "price_level":
        if "level" not in entry:
            raise StrategyError("price_level entry needs a 'level'.")
        return
    # Defer to triggers.parse_condition for the real kinds.
    parse_condition(entry)


# ---------------------------------------------------------------------------
# Backtest decision generator
# ---------------------------------------------------------------------------

def _snapshot_from_history(symbol: str, history: pd.DataFrame, entry: Dict[str, Any]) -> MarketSnapshot:
    """Build a MarketSnapshot from a history slice for trigger evaluation."""
    close = history["Close"].astype(float)
    vol = history["Volume"].astype(float) if "Volume" in history else None
    last = float(close.iloc[-1])

    # ref_price for price_move: close one trading day ago (window ~1d).
    ref = float(close.iloc[-2]) if len(close) > 1 else last

    volume_now = float(vol.iloc[-1]) if vol is not None and len(vol) else None
    avg_volume = float(vol.iloc[-21:-1].mean()) if vol is not None and len(vol) > 21 else (
        float(vol.iloc[:-1].mean()) if vol is not None and len(vol) > 1 else None
    )

    # Indicators for indicator_cross (sma_N / ema_N referenced by the entry).
    indicators: Dict[str, float] = {}
    prev_indicators: Dict[str, float] = {}
    for name in _indicator_names(entry):
        series = _indicator_series(name, close)
        if series is not None and len(series.dropna()) >= 2:
            indicators[name] = float(series.iloc[-1])
            prev_indicators[name] = float(series.iloc[-2])
    # Treat a bare "close" indicator name as the last price (for "breaks above sma_50").
    if "close" in _indicator_names(entry):
        indicators.setdefault("close", last)
        prev_indicators.setdefault("close", float(close.iloc[-2]) if len(close) > 1 else last)

    return MarketSnapshot(
        symbol=symbol,
        last_price=last,
        ref_price=ref,
        volume_now=volume_now,
        avg_volume=avg_volume,
        indicators=indicators or None,
        prev_indicators=prev_indicators or None,
    )


def _indicator_names(entry: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    if not isinstance(entry, dict):
        return out
    if entry.get("kind") in ("and", "or"):
        for c in entry.get("children", []):
            out.extend(_indicator_names(c))
    elif entry.get("kind") == "indicator_cross":
        out.extend([str(entry.get("fast", "")), str(entry.get("slow", ""))])
    return [n for n in out if n]


def _indicator_series(name: str, close: pd.Series) -> Optional[pd.Series]:
    name = name.lower().strip()
    if name == "close":
        return close
    for prefix, fn in (("sma_", "sma"), ("ema_", "ema")):
        if name.startswith(prefix):
            try:
                n = int(name.split("_", 1)[1])
            except (ValueError, IndexError):
                return None
            if fn == "sma":
                return close.rolling(n).mean()
            return close.ewm(span=n, adjust=False).mean()
    return None


def _eval_entry(entry: Dict[str, Any], snap: MarketSnapshot) -> bool:
    """Evaluate an entry dict (handling the synthetic price_level kind)."""
    kind = entry.get("kind")
    if kind == "and":
        return all(_eval_entry(c, snap) for c in entry.get("children", []))
    if kind == "or":
        return any(_eval_entry(c, snap) for c in entry.get("children", []))
    if kind == "price_level":
        if snap.last_price is None:
            return False
        level = float(entry.get("level", 0) or 0)
        if entry.get("direction") == "below":
            return snap.last_price <= level
        return snap.last_price >= level
    # Real trigger kinds.
    try:
        cond: TriggerCondition = parse_condition(entry)
    except Exception:  # noqa: BLE001
        return False
    if cond is None:
        return False
    return bool(evaluate(cond, snap))


def strategy_decision_generator(spec: StrategySpec):
    """Return a DecisionGenerator that fires the strategy when its entry hits.

    Long → OVERWEIGHT; fade → UNDERWEIGHT. Stop and hold come from the spec.
    """
    rating = PortfolioRating.OVERWEIGHT if spec.direction == "long" else PortfolioRating.UNDERWEIGHT

    def _gen(symbol: str, as_of: _dt.date, history: pd.DataFrame) -> Optional[PortfolioDecision]:
        if history is None or len(history) < 25:
            return None
        snap = _snapshot_from_history(symbol, history, spec.entry_raw)
        if not _eval_entry(spec.entry_raw, snap):
            return None  # no entry today → Hold (no order)
        last = float(history["Close"].astype(float).iloc[-1])
        if spec.direction == "long":
            stop = last * (1.0 - spec.stop_loss_pct)
            expected = spec.stop_loss_pct * 2.0 * 100.0
        else:
            stop = last * (1.0 + spec.stop_loss_pct)
            expected = spec.stop_loss_pct * 2.0 * 100.0
        return PortfolioDecision(
            rating=rating,
            stop_loss=round(stop, 4),
            expected_return_pct=round(expected, 2),
            expected_volatility_pct=20.0,
            prob_of_profit=0.55,
            expected_hold_days=spec.hold_days,
            executive_summary=f"{spec.name}: entry condition met for {symbol} at {last:.2f}.",
            investment_thesis=spec.rationale or spec.source_text,
        )

    return _gen
