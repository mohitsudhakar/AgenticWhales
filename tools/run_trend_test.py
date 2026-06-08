#!/usr/bin/env python
"""Diversified cross-asset trend-following vs buy-and-hold, across regimes.

The honest 'make money without predicting, in both directions' test:
time-series momentum (long if 12m return > 0, short/flat if < 0), vol-targeted,
across an uncorrelated basket (stocks / bonds / gold / commodities / USD / REIT).
Compared to buy-and-hold SPY and an equal-weight buy-and-hold of the same basket,
over the 2020 crash, the 2022 bear, the 2023-24 bull, and the full period.

No options, no leverage beyond vol-targeting cap — the cleanest read on whether a
non-predictive trend core actually defends in bears.
"""
from __future__ import annotations

import datetime as _dt

import pandas as pd

from agenticwhales import strategy_lab as sl

BASKET = ["SPY", "QQQ", "EFA", "TLT", "IEF", "GLD", "DBC", "UUP", "VNQ"]
WINDOWS = {
    "2020 COVID crash (Jan-Jun)": ("2020-01-01", "2020-06-30"),
    "2022 bear":                  ("2022-01-01", "2022-12-31"),
    "2023-24 bull":               ("2023-01-01", "2024-12-31"),
    "full 2018-2026":             ("2018-01-01", "2026-06-20"),
}


def load_all():
    import yfinance as yf
    out = {}
    for s in BASKET:
        df = yf.Ticker(s).history(start="2016-01-01", end="2026-06-21")
        if df.empty:
            print(f"  (no data for {s})"); continue
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        out[s] = df
    return out


def port_curve(spec, hist, s, e, cost_bps=5.0):
    curves = []
    for sym, h in hist.items():
        r = sl.backtest(spec, h, s, e, cost_bps=cost_bps)
        ser = pd.Series({d: v for d, v in r.equity})
        if len(ser) > 1:
            curves.append(ser / ser.iloc[0])
    if not curves:
        return None
    return pd.concat(curves, axis=1).mean(axis=1).dropna()


def curve_metrics(port):
    rets = port.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * (252 ** 0.5)) if rets.std() > 0 else 0.0
    peak, mdd = -1e18, 0.0
    for v in port.values:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak > 0 else 0.0)
    return sharpe, (float(port.iloc[-1]) / float(port.iloc[0]) - 1) * 100, mdd * 100


def main():
    print("loading basket…")
    H = load_all()
    print(f"loaded {len(H)} ETFs: {', '.join(H)}\n")

    spy = {"SPY": H["SPY"]}
    BH = sl.StrategySpec("bh", "buyhold", allow_short=False)
    TR_LF = sl.StrategySpec("trlf", "momentum", params={"lookback": 252}, allow_short=False, vol_target_annual=0.15)
    TR_LS = sl.StrategySpec("trls", "momentum", params={"lookback": 252}, allow_short=True, vol_target_annual=0.15)

    for wname, (wstart, wend) in WINDOWS.items():
        s, e = _dt.date.fromisoformat(wstart), _dt.date.fromisoformat(wend)
        spy_bh = port_curve(BH, spy, s, e)
        bask_bh = port_curve(BH, H, s, e)
        tr_lf = port_curve(TR_LF, H, s, e)
        tr_ls = port_curve(TR_LS, H, s, e)
        # 50/50 daily-rebalanced blend: basket buy&hold + trend long/flat overlay
        idx = bask_bh.index.intersection(tr_lf.index)
        br = bask_bh[idx].pct_change().fillna(0)
        tr = tr_lf[idx].pct_change().fillna(0)
        blend = (1 + 0.5 * br + 0.5 * tr).cumprod()

        rows = [("Buy&Hold SPY", spy_bh), ("Buy&Hold basket (eq-wt)", bask_bh),
                ("Trend long/flat", tr_lf), ("Trend long/short", tr_ls),
                ("50/50 basket + trend BLEND", blend)]
        print(f"=== {wname} ({wstart} → {wend}) ===")
        print(f"  {'strategy':30} | {'Sharpe':>7} {'return%':>8} {'maxDD%':>7}")
        print("  " + "-" * 60)
        for label, curve in rows:
            sh, ret, mdd = curve_metrics(curve)
            print(f"  {label:30} | {sh:7.2f} {ret:8.2f} {mdd:7.1f}")
        print()


if __name__ == "__main__":
    main()
