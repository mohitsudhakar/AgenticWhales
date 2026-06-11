"""Defensive overlay — the second product: the walk-forward-validated trend blend.

What this is
------------
The edge probe killed the predictive-alpha thesis, but one strategy survived
20 years of walk-forward (2006–2026, through the GFC, the 2010s trend drought,
2020 and 2022 — `tools/run_walkforward.py`, memo in
`docs/reviews/2026-06-08-trend-test.md`): a 50/50 blend of

  * buy & hold of a diversified 7-ETF basket, and
  * 12-month time-series momentum (long/flat) on the same basket,
    vol-targeted to 15% annualized per sleeve.

Blend Sharpe 0.91 vs SPY 0.64, max drawdown 20% vs 55%, worst year −17% vs
−36%, robust across 6–15 month lookbacks. This is **risk-premia harvesting,
not alpha** — no prediction anywhere; the value is the payoff structure.

What this module does
---------------------
Turns that committed research result into a live, deterministic product
surface: *given today's prices, what are the blend's target weights?* Each
sleeve is long its buy-&-hold half always; the trend half is on only when the
12-month return is positive, scaled down when realized vol runs above target.

Design law (same as the coach): every number here is deterministic arithmetic
on price history. No LLM, no forecast, no recommendation — the output is the
mechanical state of a published rule set, framed educational. Execution is the
user's decision; this module never places orders.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import pandas as pd

TRADING_DAYS = 252

# The exact configuration validated by the 2006-2026 walk-forward. Changing any
# of these means re-running tools/run_walkforward.py and committing a new memo.
BASKET = ["SPY", "QQQ", "EFA", "TLT", "IEF", "GLD", "VNQ"]
LOOKBACK_DAYS = 252          # 12-month time-series momentum
VOL_TARGET_ANNUAL = 0.15     # per-sleeve vol target (trend half)
VOL_WINDOW = 20              # realized-vol estimation window (matches strategy_lab)
BLEND_TREND_FRACTION = 0.5   # 50/50 buy&hold / trend

# Committed walk-forward evidence (docs/reviews/2026-06-08-trend-test.md).
# Static by design: the UI shows the *audited* record, not a live recompute.
EVIDENCE = {
    "source": "docs/reviews/2026-06-08-trend-test.md",
    "window": "2006-2026 walk-forward (GFC, 2010s trend drought, 2020, 2022)",
    "full_period": [
        {"name": "Buy&Hold SPY", "sharpe": 0.64, "return_pct": 746, "max_dd_pct": 55},
        {"name": "Buy&Hold basket", "sharpe": 0.82, "return_pct": 564, "max_dd_pct": 30},
        {"name": "Trend long/flat", "sharpe": 0.91, "return_pct": 248, "max_dd_pct": 12},
        {"name": "50/50 blend", "sharpe": 0.91, "return_pct": 390, "max_dd_pct": 20},
    ],
    "worst_year": {"blend": {"year": 2022, "return_pct": -17},
                   "spy": {"year": 2008, "return_pct": -36}},
    "leverage": [
        {"name": "blend 1.0x", "sharpe": 0.91, "return_pct": 388, "max_dd_pct": 20},
        {"name": "blend 1.5x", "sharpe": 0.80, "return_pct": 647, "max_dd_pct": 29},
        {"name": "blend 2.0x", "sharpe": 0.74, "return_pct": 996, "max_dd_pct": 38},
    ],
    "parameter_robustness": "blend Sharpe 0.89-0.94 across 6/9/12/15-month lookbacks",
    "honest_tradeoff": "Lower total return than pure SPY in a long bull, bought "
                       "with ~1/3 the drawdown and a far better worst case. "
                       "Risk-premia harvesting, not alpha.",
}


@dataclass
class SleeveStatus:
    symbol: str
    in_trend: bool                  # 12m return > 0 -> trend half is on
    lookback_return_pct: float      # the 12m return behind the signal
    realized_vol_pct: float         # annualized 20d realized vol
    trend_scale: float              # min(1, vol_target / realized_vol) when on, else 0
    target_weight_pct: float        # final weight, % of equity (at requested leverage)

    def to_dict(self) -> Dict:
        return self.__dict__.copy()


@dataclass
class OverlayStatus:
    as_of: str
    leverage: float
    sleeves: List[SleeveStatus] = field(default_factory=list)
    equity_exposure_pct: float = 0.0   # sum of sleeve weights
    cash_pct: float = 0.0              # 100 - exposure (negative = borrowed)
    trend_sleeves_on: int = 0
    params: Dict = field(default_factory=dict)
    evidence: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        d = self.__dict__.copy()
        d["sleeves"] = [s.to_dict() for s in self.sleeves]
        return d


def _realized_vol(close: pd.Series, window: int = VOL_WINDOW) -> float:
    r = close.pct_change().dropna().tail(window)
    return float(r.std() * math.sqrt(TRADING_DAYS)) if len(r) > 2 else float("nan")


def sleeve_status(symbol: str, close: pd.Series, *,
                  lookback_days: int = LOOKBACK_DAYS,
                  vol_target: float = VOL_TARGET_ANNUAL) -> SleeveStatus:
    """Mechanical state of one sleeve: trend on/off + vol-target scaling.

    Mirrors strategy_lab's `momentum` signal (long/flat) with
    `vol_target_annual` and `max_leverage=1.0` — the exact spec the
    walk-forward validated.
    """
    close = close.dropna()
    if len(close) <= lookback_days:
        return SleeveStatus(symbol=symbol.upper(), in_trend=False,
                            lookback_return_pct=0.0, realized_vol_pct=0.0,
                            trend_scale=0.0, target_weight_pct=0.0)
    last = float(close.iloc[-1])
    base = float(close.iloc[-lookback_days - 1])
    lb_ret = last / base - 1.0 if base else 0.0
    in_trend = lb_ret > 0
    rv = _realized_vol(close)
    scale = 0.0
    if in_trend:
        scale = 1.0 if (math.isnan(rv) or rv <= 0) else min(1.0, vol_target / rv)
    return SleeveStatus(
        symbol=symbol.upper(), in_trend=in_trend,
        lookback_return_pct=round(lb_ret * 100, 1),
        realized_vol_pct=round(rv * 100, 1) if not math.isnan(rv) else 0.0,
        trend_scale=round(scale, 3), target_weight_pct=0.0,
    )


def compute_overlay(closes: Dict[str, pd.Series], *, leverage: float = 1.0,
                    as_of: Optional[str] = None,
                    lookback_days: int = LOOKBACK_DAYS,
                    vol_target: float = VOL_TARGET_ANNUAL,
                    blend: float = BLEND_TREND_FRACTION) -> OverlayStatus:
    """Target weights for the blend, from Close series (pure; no network).

    Per symbol at leverage 1: weight = (1-blend)/N  +  blend * trend_scale / N
    — the buy-&-hold half is always on; the trend half is on only in an
    uptrend, shrunk when realized vol exceeds the target. Leverage scales all
    weights; the remainder (can be negative) is cash.
    """
    leverage = max(0.0, min(2.0, float(leverage)))   # validated range only
    n = len(closes) or 1
    sleeves: List[SleeveStatus] = []
    for sym in sorted(closes):
        s = sleeve_status(sym, closes[sym], lookback_days=lookback_days,
                          vol_target=vol_target)
        w = ((1.0 - blend) + blend * s.trend_scale) / n * leverage
        s.target_weight_pct = round(w * 100, 1)
        sleeves.append(s)
    exposure = round(sum(s.target_weight_pct for s in sleeves), 1)
    dates = [c.index[-1] for c in closes.values() if len(c)]
    stamp = as_of or (max(dates).date().isoformat() if dates
                      else _dt.date.today().isoformat())
    return OverlayStatus(
        as_of=stamp, leverage=leverage, sleeves=sleeves,
        equity_exposure_pct=exposure, cash_pct=round(100 - exposure, 1),
        trend_sleeves_on=sum(1 for s in sleeves if s.in_trend),
        params={"basket": [s.symbol for s in sleeves],
                "lookback_days": lookback_days, "vol_target_annual": vol_target,
                "blend_trend_fraction": blend, "vol_window": VOL_WINDOW},
        evidence=EVIDENCE,
    )


def fetch_overlay_status(*, leverage: float = 1.0,
                         fetch_ohlc: Optional[Callable] = None,
                         basket: Optional[List[str]] = None) -> OverlayStatus:
    """Live overlay status from market data (the network-touching wrapper)."""
    if fetch_ohlc is None:
        from . import prices
        fetch_ohlc = prices.fetch_ohlc
    end = _dt.date.today()
    start = end - _dt.timedelta(days=600)   # ~252 trading days + vol window + buffer
    closes: Dict[str, pd.Series] = {}
    for sym in (basket or BASKET):
        df = fetch_ohlc(sym, start.isoformat(), end.isoformat())
        if df is not None and len(df) and "Close" in df:
            closes[sym] = df["Close"]
    if not closes:
        raise RuntimeError("no price data available for the overlay basket")
    return compute_overlay(closes, leverage=leverage)
