#!/usr/bin/env python
"""Walk-forward / robustness test of the trend+buy&hold blend, 2006-2026.

The single most important check: is the diversified-trend result period-luck, or
does it hold across the GFC, the 2010s trend DROUGHT, 2020, and 2022? Plus a
parameter-robustness sweep (is it knife-edge on the 12m lookback?) and a
risk-managed leverage sweep.

7-ETF basket with long history so every sleeve is active from 2006:
SPY QQQ EFA TLT IEF GLD VNQ (stocks / intl / long+mid bonds / gold / REIT).
"""
from __future__ import annotations

import datetime as _dt

import pandas as pd

from agenticwhales import strategy_lab as sl

BASKET = ["SPY", "QQQ", "EFA", "TLT", "IEF", "GLD", "VNQ"]
START, END = _dt.date(2006, 1, 1), _dt.date(2026, 6, 20)


def load():
    import yfinance as yf
    out = {}
    for s in BASKET:
        df = yf.Ticker(s).history(start="2004-01-01", end="2026-06-21")
        if df.empty:
            continue
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        out[s] = df
    return out


def port_curve(spec, hist, cost_bps=5.0):
    curves = []
    for h in hist.values():
        r = sl.backtest(spec, h, START, END, cost_bps=cost_bps)
        ser = pd.Series({d: v for d, v in r.equity})
        if len(ser) > 1:
            ser.index = pd.to_datetime(ser.index)
            curves.append(ser.sort_index() / ser.iloc[0])
    return pd.concat(curves, axis=1).mean(axis=1).dropna() if curves else None


def cmetrics(curve):
    rets = curve.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * (252 ** 0.5)) if rets.std() > 0 else 0.0
    peak, mdd = -1e18, 0.0
    for v in curve.values:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak > 0 else 0.0)
    return sharpe, (float(curve.iloc[-1]) / float(curve.iloc[0]) - 1) * 100, mdd * 100


def blend_of(bask, tr):
    idx = bask.index.intersection(tr.index)
    return (1 + 0.5 * bask[idx].pct_change().fillna(0) + 0.5 * tr[idx].pct_change().fillna(0)).cumprod()


def lever(curve, L, fin=0.03):
    rets = curve.pct_change().dropna()
    return (1 + L * rets - (L - 1) * fin / 252).cumprod()


def yr_slice(curve, yr):
    s = curve[(curve.index.year == yr)]
    return s


def main():
    print("loading…")
    H = load()
    print(f"loaded {len(H)}: {', '.join(H)}\n")
    BH = sl.StrategySpec("bh", "buyhold", allow_short=False)

    def trend(lb):
        return sl.StrategySpec("tr", "momentum", params={"lookback": lb},
                               allow_short=False, vol_target_annual=0.15)

    spy = port_curve(BH, {"SPY": H["SPY"]})
    bask = port_curve(BH, H)
    trlf = port_curve(trend(252), H)
    blend = blend_of(bask, trlf)

    # ---- year-by-year (return% / maxDD%) ----
    print("YEAR-BY-YEAR  (return% | maxDD%)")
    print(f"  {'year':4} | {'SPY B&H':>16} | {'Blend':>16} | {'winner':>6}")
    pos_blend = pos_spy = years = 0
    worst_blend = worst_spy = (None, 999.0)
    for yr in range(2006, 2027):
        sy, by = yr_slice(spy, yr), yr_slice(blend, yr)
        if len(sy) < 5 or len(by) < 5:
            continue
        years += 1
        _, sret, smdd = cmetrics(sy)
        _, bret, bmdd = cmetrics(by)
        pos_blend += bret > 0
        pos_spy += sret > 0
        if bret < worst_blend[1]:
            worst_blend = (yr, bret)
        if sret < worst_spy[1]:
            worst_spy = (yr, sret)
        win = "Blend" if bret > sret else "SPY"
        print(f"  {yr:4} | {sret:+7.1f} / {smdd:4.0f}    | {bret:+7.1f} / {bmdd:4.0f}    | {win:>6}")

    print(f"\n  positive years — SPY {pos_spy}/{years}, Blend {pos_blend}/{years}")
    print(f"  worst year — SPY {worst_spy[0]} {worst_spy[1]:+.0f}%, Blend {worst_blend[0]} {worst_blend[1]:+.0f}%")

    # ---- full-period aggregate ----
    print("\nFULL 2006-2026  (Sharpe / return% / maxDD%)")
    for name, c in [("Buy&Hold SPY", spy), ("Buy&Hold basket", bask),
                    ("Trend long/flat", trlf), ("50/50 blend", blend)]:
        sh, ret, mdd = cmetrics(c)
        print(f"  {name:18} | {sh:5.2f} / {ret:8.1f} / {mdd:5.1f}")

    # ---- parameter robustness (is it knife-edge on 12m?) ----
    print("\nPARAMETER ROBUSTNESS — blend Sharpe/maxDD across trend lookbacks")
    for lb in (126, 189, 252, 315):
        b = blend_of(bask, port_curve(trend(lb), H))
        sh, ret, mdd = cmetrics(b)
        print(f"  lookback {lb:3}d ({lb//21}m) | Sharpe {sh:5.2f} / return {ret:7.1f}% / maxDD {mdd:5.1f}%")

    # ---- risk-managed leverage on the blend ----
    print("\nLEVERAGE on the blend (3% financing)  vs SPY for reference")
    sh, ret, mdd = cmetrics(spy)
    print(f"  {'SPY B&H (1.0x)':18} | Sharpe {sh:5.2f} / return {ret:8.1f}% / maxDD {mdd:5.1f}%")
    for L in (1.0, 1.5, 2.0):
        sh, ret, mdd = cmetrics(lever(blend, L))
        print(f"  blend {L:.1f}x          | Sharpe {sh:5.2f} / return {ret:8.1f}% / maxDD {mdd:5.1f}%")


if __name__ == "__main__":
    main()
