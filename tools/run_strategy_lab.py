#!/usr/bin/env python
"""Benchmark battery for the strategy lab — establishes the bar the LLM must clear.

Runs a set of hand-written STRUCTURAL strategies across the 10-name panel, in W1
(in-sample-ish) and W2 (out-of-sample), all vs buy-and-hold. The question:
can any simple structure beat buy-and-hold OOS, net of cost — and does engineering
asymmetric exits (stops) actually lift the payoff ratio above 1?
"""
from __future__ import annotations

import datetime as _dt

import pandas as pd

from agenticwhales import edge_probe as ep
from agenticwhales import strategy_lab as sl

PANEL = ep.DEFAULT_PANEL
W1, W2 = ep.W1_DRAWDOWN, ep.W2_BULL_OOS


def load_all():
    import yfinance as yf
    start = _dt.date.fromisoformat(W1.start) - _dt.timedelta(days=400)
    end = _dt.date.fromisoformat(W2.end)
    out = {}
    for s in PANEL:
        df = yf.Ticker(s).history(start=start.isoformat(),
                                  end=(end + _dt.timedelta(days=1)).isoformat())
        if df.empty:
            continue
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        out[s] = df
    return out


SPECS = [
    sl.StrategySpec("buy&hold", "buyhold", allow_short=False),
    sl.StrategySpec("trend 20/100 LS voltgt", "trend", {"fast": 20, "slow": 100},
                    allow_short=True, vol_target_annual=0.15),
    sl.StrategySpec("trend 20/100 long-only voltgt", "trend", {"fast": 20, "slow": 100},
                    allow_short=False, vol_target_annual=0.15),
    sl.StrategySpec("trend+stops 20/100 LO", "trend", {"fast": 20, "slow": 100},
                    allow_short=False, vol_target_annual=0.15,
                    stop_loss_atr=2.0, trailing_stop_atr=3.0),
    sl.StrategySpec("momentum 120 LO voltgt", "momentum", {"lookback": 120},
                    allow_short=False, vol_target_annual=0.15),
    sl.StrategySpec("meanrev 20 LS", "meanrev", {"window": 20}, allow_short=True),
    sl.StrategySpec("breakout 50 LO trail3", "breakout", {"window": 50},
                    allow_short=False, vol_target_annual=0.15, trailing_stop_atr=3.0),
]


def main():
    print("loading histories…")
    H = load_all()
    print(f"loaded {len(H)} symbols\n")
    hdr = f"{'strategy':32} | {'W1 Shrp':>7} {'W1 ret%':>7} {'W1 dd%':>6} | {'W2 Shrp':>7} {'W2 ret%':>7} {'W2 dd%':>6} | {'hit':>4} {'payoff':>6}"
    print(hdr); print("-" * len(hdr))
    for spec in SPECS:
        r1 = sl.run_portfolio(spec, H, _dt.date.fromisoformat(W1.start), _dt.date.fromisoformat(W1.end))
        r2 = sl.run_portfolio(spec, H, _dt.date.fromisoformat(W2.start), _dt.date.fromisoformat(W2.end))
        print(f"{spec.name:32} | {r1['sharpe']:7.2f} {r1['total_return_pct']:7.2f} {r1['max_dd_pct']:6.1f} | "
              f"{r2['sharpe']:7.2f} {r2['total_return_pct']:7.2f} {r2['max_dd_pct']:6.1f} | "
              f"{r2.get('med_hit_rate',0):4.2f} {r2.get('med_payoff',0):6.2f}")
    print("\nKey question: does any structural strategy's W2 (OOS) Sharpe beat buy&hold's,")
    print("and does engineering stops lift the payoff ratio above ~1?")


if __name__ == "__main__":
    main()
