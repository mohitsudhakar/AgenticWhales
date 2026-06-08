#!/usr/bin/env python
"""Root-cause diagnostics for the edge probe — is there ANY skill, stripped of beta?

Reconstructs every cached decision (no new LLM calls) and computes beta-neutral
skill metrics the headline Sharpe can't show:

  * Directional hit-rate + winner/loser asymmetry + per-bet expectancy (net of cost)
  * Information Coefficient (IC): rank-correlation of the model's cross-sectional
    rating vs forward return, per month — the cleanest "does it rank names better
    than chance?" metric, immune to the market-drift benchmark problem.
  * Market-neutral (dollar-neutral long/short) book Sharpe — pure selection skill.
  * Calibration of prob_of_profit vs realized outcome.

Run AFTER the probe has populated the caches.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from agenticwhales import edge_probe as ep

PANEL = ep.DEFAULT_PANEL
WINDOWS = [ep.W1_DRAWDOWN, ep.W2_BULL_OOS]


def load_history(symbol, window):
    import yfinance as yf
    start = _dt.date.fromisoformat(window.start) - _dt.timedelta(days=400)
    end = _dt.date.fromisoformat(window.end)
    df = yf.Ticker(symbol).history(start=start.isoformat(),
                                   end=(end + _dt.timedelta(days=1)).isoformat())
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


def _boom(*a, **k):
    raise RuntimeError("cache miss (no LLM allowed in diagnostics)")


def forward_returns(history, window, schedule):
    """Realized underlying return for a position entered the open after each
    schedule date and exited the open after the next schedule date."""
    start = _dt.date.fromisoformat(window.start)
    end = _dt.date.fromisoformat(window.end)
    days = [ts.date() for ts in history.index if start <= ts.date() <= end]
    open_by_day = {ts.date(): float(history.loc[ts, "Open"]) for ts in history.index
                   if start <= ts.date() <= end}

    def next_open(after):
        for d in days:
            if d > after:
                return open_by_day[d], d
        return None, None

    out = {}
    sched = list(schedule)
    for i, d in enumerate(sched):
        entry, ed = next_open(d)
        if entry is None:
            continue
        if i + 1 < len(sched):
            exit_px, _ = next_open(sched[i + 1])
        else:
            exit_px = open_by_day[days[-1]]
        if exit_px:
            out[d] = exit_px / entry - 1.0
    return out


def reconstruct(variant, cache_file):
    cache = ep.DecisionCache(Path.home() / ".tradingagents" / cache_file,
                             model="deepseek-v4-pro", variant=variant)
    rows = []  # (window, ym, symbol, weight, prob, fwd_ret)
    for window in WINDOWS:
        for symbol in PANEL:
            try:
                hist = load_history(symbol, window)
            except Exception:
                continue
            sched = ep.monthly_schedule(hist, window)
            decs = ep.generate_llm_decisions(symbol, hist, sched,
                                             invoke_text=_boom, invoke_structured=_boom,
                                             cache=cache, strict=False)
            fwd = forward_returns(hist, window, sched)
            for d, dec in decs.items():
                if d not in fwd:
                    continue
                rows.append((window.name, (d.year, d.month), symbol,
                             ep.RATING_WEIGHT.get(dec.rating, 0.0),
                             dec.prob_of_profit, fwd[d]))
    return pd.DataFrame(rows, columns=["window", "ym", "symbol", "w", "prob", "fwd"])


def _spearman(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 3 or np.all(a == a[0]):
        return np.nan
    ra = pd.Series(a).rank().values
    rb = pd.Series(b).rank().values
    if np.std(ra) == 0 or np.std(rb) == 0:
        return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])


def analyze(df, label, cost_bps=10.0):
    print(f"\n================  {label}  (N={len(df)} bets)  ================")
    active = df[df.w != 0].copy()
    if active.empty:
        print("  no active bets"); return
    # Directional return per unit exposure, sign of the bet.
    active["dir_ret"] = np.sign(active.w) * active.fwd
    active["pnl"] = active.w * active.fwd
    cost = 2 * cost_bps / 1e4 * active.w.abs()
    active["pnl_net"] = active.pnl - cost

    hit = (active.dir_ret > 0).mean()
    wins = active.dir_ret[active.dir_ret > 0]
    losses = active.dir_ret[active.dir_ret < 0]
    avg_win = wins.mean() if len(wins) else 0.0
    avg_loss = losses.mean() if len(losses) else 0.0
    print(f"  directional hit-rate : {hit:.1%}   (>50% = better than a coin)")
    print(f"  avg winner / avg loser: {avg_win:+.3%} / {avg_loss:+.3%}   "
          f"payoff ratio {abs(avg_win/avg_loss) if avg_loss else float('nan'):.2f}")
    print(f"  expectancy per bet    : gross {active.pnl.mean():+.3%}   "
          f"net of cost {active.pnl_net.mean():+.3%}   (>0 = makes money)")

    # Information coefficient: per-month cross-sectional rank corr.
    ics = []
    for (_, _), g in df.groupby(["window", "ym"]):
        if g.w.nunique() > 1 and len(g) >= 3:
            ic = _spearman(g.w.values, g.fwd.values)
            if not np.isnan(ic):
                ics.append(ic)
    if ics:
        ics = np.array(ics)
        t = ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics))) if len(ics) > 1 and ics.std() > 0 else float('nan')
        print(f"  Information Coeff (IC): mean {ics.mean():+.3f}  over {len(ics)} months  "
              f"t-stat {t:+.2f}   (IC≈0 ⇒ no selection skill; |t|>2 ⇒ significant)")

    # Market-neutral (dollar-neutral) monthly book: demeaned weights within month.
    book = []
    for (_, _), g in df.groupby(["window", "ym"]):
        if len(g) < 2:
            continue
        wmn = g.w - g.w.mean()
        denom = wmn.abs().sum()
        if denom > 0:
            book.append(float((wmn * g.fwd).sum() / denom))
    if book:
        book = np.array(book)
        sharpe = book.mean() / book.std(ddof=1) * np.sqrt(12) if book.std() > 0 else 0.0
        print(f"  market-neutral book   : mean monthly {book.mean():+.3%}  "
              f"ann.Sharpe {sharpe:+.2f}  over {len(book)} months   (pure selection, beta removed)")

    # Calibration of prob_of_profit.
    cal = active.dropna(subset=["prob"])
    if len(cal) > 10:
        c = np.corrcoef(cal.prob, (cal.dir_ret > 0).astype(float))[0, 1]
        print(f"  prob calibration corr : {c:+.3f}   (corr of stated prob_of_profit vs win; ~0 ⇒ confidence is noise)")


if __name__ == "__main__":
    for variant, cache_file, label in [
        ("", "edge_probe_cache.jsonl", "BASELINE (Hold allowed)"),
        ("forced-commit", "edge_probe_cache_forced.jsonl", "FORCED-COMMIT (Hold forbidden)"),
    ]:
        df = reconstruct(variant, cache_file)
        analyze(df, label)
