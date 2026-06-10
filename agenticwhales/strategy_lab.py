"""Strategy lab — an executable strategy spec + a walk-forward backtester.

Built for the pivot to "a system that proposes pro-trader strategies and backtests
itself" (project #3). Two design commitments, both learned the hard way from the
edge probe:

1. **Edge comes from STRUCTURE and RISK MANAGEMENT, not prediction.** The probe
   proved an LLM has zero cross-sectional selection skill on public price data
   (IC ~= 0, market-neutral Sharpe ~= 0). So strategies here are built from
   *payoff structure* (trend convexity, stops, vol-targeting, asymmetric exits) —
   the things that create a payoff ratio > 1 without forecasting.

2. **The enemy is OVERFITTING.** Every result is reported IN-SAMPLE vs
   OUT-OF-SAMPLE so degradation is visible, costs are always charged, and the
   number of strategies tried is tracked (multiple-testing awareness). A strategy
   that only shines in-sample is flagged, not celebrated.

Strategies are expressed as a constrained `StrategySpec` (not arbitrary code), so
an LLM generator can emit them safely and every idea is falsifiable. The
backtester executes a spec on one symbol; `run_portfolio` equal-weights across a
universe.

Faithful *options* backtesting needs historical implied-vol/chain data we don't
have yet — this module backtests the underlying and option-LIKE payoff structures
(stops create convexity). A real derivatives module is a follow-up gated on a data
source.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

TRADING_DAYS = 252


# --------------------------------------------------------------------------- #
# Strategy spec — the constrained, LLM-emittable language
# --------------------------------------------------------------------------- #

@dataclass
class StrategySpec:
    name: str
    signal: str                      # trend | momentum | meanrev | breakout | buyhold
    params: Dict = field(default_factory=dict)
    allow_short: bool = True
    # Risk / structure overlay (this is where asymmetric payoff is engineered):
    vol_target_annual: Optional[float] = None   # e.g. 0.15 -> scale to 15% vol
    max_leverage: float = 1.0
    stop_loss_atr: Optional[float] = None        # exit if move against by N*ATR
    trailing_stop_atr: Optional[float] = None
    take_profit_atr: Optional[float] = None
    max_hold_days: Optional[int] = None

    def to_dict(self) -> Dict:
        return {k: v for k, v in self.__dict__.items()}


# --------------------------------------------------------------------------- #
# Signals: history-up-to-today -> desired raw direction in [-1, 1] (or None=hold)
# --------------------------------------------------------------------------- #

def _sma(c: pd.Series, n: int) -> float:
    return float(c.tail(n).mean()) if len(c) >= n else float("nan")


def _signal(spec: StrategySpec, close: pd.Series) -> Optional[float]:
    p = spec.params
    if spec.signal == "buyhold":
        return 1.0
    if len(close) < 60:
        return 0.0
    last = float(close.iloc[-1])
    if spec.signal == "trend":
        f, s = _sma(close, p.get("fast", 20)), _sma(close, p.get("slow", 100))
        if math.isnan(f) or math.isnan(s):
            return 0.0
        return 1.0 if f > s else (-1.0 if spec.allow_short else 0.0)
    if spec.signal == "momentum":
        lb = p.get("lookback", 120)
        if len(close) <= lb:
            return 0.0
        r = last / float(close.iloc[-lb - 1]) - 1.0
        return 1.0 if r > 0 else (-1.0 if spec.allow_short else 0.0)
    if spec.signal == "meanrev":
        win = p.get("window", 20)
        m, sd = _sma(close, win), float(close.tail(win).std())
        if sd == 0 or math.isnan(m):
            return 0.0
        z = (last - m) / sd
        w = max(-1.0, min(1.0, -z / 2.0))           # fade the deviation
        return w if spec.allow_short else max(0.0, w)
    if spec.signal == "breakout":
        n = p.get("window", 50)
        if len(close) < n + 1:
            return 0.0
        hi = float(close.iloc[-n - 1:-1].max())
        lo = float(close.iloc[-n - 1:-1].min())
        if last >= hi:
            return 1.0
        if last <= lo:
            return -1.0 if spec.allow_short else 0.0
        return None                                  # inside the channel: hold
    raise ValueError(f"unknown signal {spec.signal!r}")


def _atr(h: pd.DataFrame, n: int = 14) -> float:
    if len(h) < n + 1:
        return float("nan")
    hi, lo, cl = h["High"], h["Low"], h["Close"]
    pc = cl.shift(1)
    tr = pd.concat([(hi - lo), (hi - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
    return float(tr.tail(n).mean())


def _realized_vol(close: pd.Series, n: int = 20) -> float:
    r = close.pct_change().dropna().tail(n)
    return float(r.std() * math.sqrt(TRADING_DAYS)) if len(r) > 2 else float("nan")


# --------------------------------------------------------------------------- #
# Backtester (one symbol). Strict-causal: signal from data<=today, fill next open.
# --------------------------------------------------------------------------- #

@dataclass
class BTResult:
    symbol: str
    equity: List[Tuple[str, float]] = field(default_factory=list)
    trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_win: float = 0.0
    gross_loss: float = 0.0


def backtest(spec: StrategySpec, history: pd.DataFrame,
             start: _dt.date, end: _dt.date, *,
             cost_bps: float = 10.0, cash: float = 100_000.0) -> BTResult:
    idx = [ts for ts in history.index if start <= ts.date() <= end]
    res = BTResult(symbol=getattr(history, "symbol", "X"))
    cash = float(cash)
    shares = 0.0
    entry_px = None
    peak_px = None
    pending_w: Optional[float] = None
    hold = 0

    for ts in idx:
        bar = history.loc[ts]
        op, hi, lo, cl = float(bar["Open"]), float(bar["High"]), float(bar["Low"]), float(bar["Close"])
        sl = history.loc[:ts]["Close"]

        # Execute queued rebalance at today's open (cash/shares accounting).
        if pending_w is not None and op > 0:
            nav_open = cash + shares * op
            target = (pending_w * nav_open) / op
            delta = target - shares
            if delta != 0:
                if shares != 0 and entry_px is not None:
                    _record(res, shares * (op - entry_px))   # realize current position
                cash -= delta * op + abs(delta) * op * cost_bps / 1e4
                shares = target
                res.trades += 1
                entry_px = op if shares != 0 else None
                peak_px = op
                hold = 0
            pending_w = None

        # Intraday risk exits (stops / take-profit) — this is where convexity /
        # payoff asymmetry is engineered.
        if shares != 0 and entry_px:
            atr = _atr(history.loc[:ts])
            exit_now = False
            if not math.isnan(atr) and atr > 0:
                if shares > 0:
                    if spec.stop_loss_atr and lo <= entry_px - spec.stop_loss_atr * atr:
                        exit_now = True
                    if spec.take_profit_atr and hi >= entry_px + spec.take_profit_atr * atr:
                        exit_now = True
                    if spec.trailing_stop_atr and peak_px and lo <= peak_px - spec.trailing_stop_atr * atr:
                        exit_now = True
                else:
                    if spec.stop_loss_atr and hi >= entry_px + spec.stop_loss_atr * atr:
                        exit_now = True
                    if spec.take_profit_atr and lo <= entry_px - spec.take_profit_atr * atr:
                        exit_now = True
            if spec.max_hold_days and hold >= spec.max_hold_days:
                exit_now = True
            if exit_now:
                _record(res, shares * (cl - entry_px))
                cash += shares * cl - abs(shares) * cl * cost_bps / 1e4
                shares = 0.0
                entry_px = None
                peak_px = None

        # Desired target -> queue for next open.
        raw = _signal(spec, sl)
        if raw is not None:
            w = raw
            if spec.vol_target_annual:
                rv = _realized_vol(sl)
                if not math.isnan(rv) and rv > 0:
                    w = raw * min(spec.max_leverage, spec.vol_target_annual / rv)
            pending_w = max(-spec.max_leverage, min(spec.max_leverage, w))

        if shares != 0 and peak_px is not None:
            peak_px = max(peak_px, hi) if shares > 0 else min(peak_px, lo)
        hold += 1
        res.equity.append((ts.date().isoformat(), round(cash + shares * cl, 2)))

    return res


def _record(res: BTResult, pnl: float):
    if pnl >= 0:
        res.wins += 1
        res.gross_win += pnl
    else:
        res.losses += 1
        res.gross_loss += -pnl


# --------------------------------------------------------------------------- #
# Metrics + portfolio
# --------------------------------------------------------------------------- #

def metrics(res: BTResult) -> Dict:
    navs = [n for _, n in res.equity]
    if len(navs) < 3:
        return {"sharpe": 0.0, "total_return_pct": 0.0, "max_dd_pct": 0.0,
                "trades": res.trades, "hit_rate": 0.0, "payoff": 0.0}
    rets = np.diff(navs) / np.array(navs[:-1])
    rets = rets[np.isfinite(rets)]
    sharpe = float(rets.mean() / rets.std() * math.sqrt(TRADING_DAYS)) if rets.std() > 0 else 0.0
    peak = -1e18
    mdd = 0.0
    for n in navs:
        peak = max(peak, n)
        mdd = max(mdd, (peak - n) / peak if peak > 0 else 0.0)
    tot = res.wins + res.losses
    return {
        "sharpe": round(sharpe, 3),
        "total_return_pct": round((navs[-1] / navs[0] - 1) * 100, 2),
        "max_dd_pct": round(mdd * 100, 2),
        "trades": res.trades,
        "hit_rate": round(res.wins / tot, 3) if tot else 0.0,
        "payoff": round((res.gross_win / res.wins) / (res.gross_loss / res.losses), 2)
                  if res.wins and res.losses and res.gross_loss > 0 else 0.0,
    }


def run_portfolio(spec: StrategySpec, histories: Dict[str, pd.DataFrame],
                  start: _dt.date, end: _dt.date, *, cost_bps: float = 10.0) -> Dict:
    """Equal-weight the strategy across the universe; average the equity curves."""
    curves = []
    per = {}
    for sym, h in histories.items():
        r = backtest(spec, h, start, end, cost_bps=cost_bps)
        per[sym] = metrics(r)
        s = pd.Series({d: v for d, v in r.equity})
        curves.append(s / s.iloc[0])
    if not curves:
        return {"sharpe": 0.0}
    port = pd.concat(curves, axis=1).mean(axis=1).dropna()
    rets = port.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * math.sqrt(TRADING_DAYS)) if rets.std() > 0 else 0.0
    peak, mdd = -1e18, 0.0
    for n in port.values:
        peak = max(peak, n)
        mdd = max(mdd, (peak - n) / peak if peak > 0 else 0.0)
    return {
        "sharpe": round(sharpe, 3),
        "total_return_pct": round((float(port.iloc[-1]) / float(port.iloc[0]) - 1) * 100, 2),
        "max_dd_pct": round(mdd * 100, 2),
        "avg_trades": round(np.mean([m["trades"] for m in per.values()]), 1),
        "med_hit_rate": round(float(np.median([m["hit_rate"] for m in per.values()])), 3),
        "med_payoff": round(float(np.median([m["payoff"] for m in per.values() if m["payoff"] > 0] or [0])), 2),
        "per_symbol": per,
    }
