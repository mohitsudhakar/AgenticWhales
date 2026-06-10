#!/usr/bin/env python
"""Vol-selling sleeve test — does the variance risk premium survive the tails?

The explicit next step from docs/reviews/2026-06-08-trend-test.md: a
defined-risk premium-selling backtest (monthly ~1-SD short strangle /
put-write on SPY, small size) measured for VRP capture AND tail behaviour
across 2008/2020/2022 — the regimes where naive vol sellers blow up.

Engine: agenticwhales/options_lab.py. Data: SPY price series + VIX (entry IV
and strike placement) + 13w T-bill yield, all via yfinance — the only path
that reaches 2008. Entry premiums are Black-Scholes from VIX; settlement is
intrinsic (model-free). Two honesty anchors:

1. The synthetic ATM put-write is validated against the CBOE PutWrite index
   (^PUT) — REAL S&P option prices, no model — over the same dates.
2. With MASSIVE_API_KEY set, entry premiums are compared against real SPY
   chain prices via dataflows/massive_options.py for recent expiries.

GATED RESEARCH — results feed a memo in docs/reviews/, not the product UI.
"""
from __future__ import annotations

import datetime as _dt
import math

import pandas as pd

from agenticwhales import options_lab as ol

START, END = _dt.date(2006, 1, 1), _dt.date(2026, 6, 20)
REGIME_YEARS = (2008, 2020, 2022)


def load():
    import yfinance as yf

    out = {}
    for sym, key in (("SPY", "spy"), ("^VIX", "vix"), ("^IRX", "irx"), ("^PUT", "put_index")):
        df = yf.Ticker(sym).history(start="2004-01-01", end="2026-06-21", auto_adjust=False)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        out[key] = df
    spy = out["spy"]
    return {
        "close": spy["Close"],                      # price series: option strikes/settlement
        "spy_tr": spy["Adj Close"],                 # total-return series: fair B&H benchmark
        "iv": out["vix"]["Close"] / 100.0,
        "rate": out["irx"]["Close"] / 100.0,
        "put_index": out["put_index"]["Close"],
    }


def fmt(name, curve):
    sh, ret, mdd = ol.curve_metrics(curve)
    return f"  {name:26} | Sharpe {sh:5.2f} / return {ret:8.1f}% / maxDD {mdd:5.1f}%"


def slice_years(curve, year):
    return curve[curve.index.year == year]


def bench_curve(series, rolls):
    """A buy&hold benchmark sampled on the same monthly roll grid."""
    c = series.reindex(rolls).dropna()
    return c / c.iloc[0]


def curve_as_result(name, curve):
    """Wrap a plain benchmark curve so year_stats treats each month as a trade."""
    trades = pd.DataFrame(
        {"expiry": curve.index[1:], "pnl_pct": curve.pct_change().dropna().values * 100.0}
    )
    return ol.VolSellResult(ol.VolSellSpec(name, "putwrite"), curve, trades)


def regime_table(rows):
    print(f"  {'':26} | " + " | ".join(f"{y}: ret/maxDD/worst-trade" for y in REGIME_YEARS))
    for name, res in rows:
        cells = []
        for y in REGIME_YEARS:
            st = ol.year_stats(res, y)
            cells.append(
                f"{st['return_pct']:+6.1f} / {st['maxdd_pct']:4.1f} / {st['worst_trade_pct']:+6.1f}"
                if st else " " * 24
            )
        print(f"  {name:26} | " + " | ".join(cells))


def real_chain_calibration(close, iv, rate, rolls):
    """Compare model entry premiums vs real SPY chain prices (Massive adapter).

    Only runs when MASSIVE_API_KEY is set; free tier covers ~2y of history,
    which is exactly the calibration window we use."""
    from agenticwhales.dataflows import massive_options as mo

    try:
        mo.get_api_key()
    except mo.MassiveOptionsError:
        print("  (skipped: MASSIVE_API_KEY not set — model premiums are UNCALIBRATED")
        print("   against real chains; set the key in .env and re-run.)")
        return
    ratios = []
    pairs = list(zip(rolls, rolls[1:]))[-24:]
    for t0, t1 in pairs:
        try:
            s0, sigma = float(close[t0]), float(iv[t0])
            r = float(rate[t0]) if not math.isnan(float(rate[t0])) else 0.0
            t_years = (t1 - t0).days / 365.0
            k_put = s0 * math.exp(-sigma * math.sqrt(t_years))
            contracts = mo.list_option_contracts(
                "SPY", as_of=t0.date().isoformat(), expired=True, contract_type="put",
                expiration_date=t1.date().isoformat(),
                strike_price_gte=k_put * 0.97, strike_price_lte=k_put * 1.03,
            )
            chosen = mo.nearest_contract(contracts, k_put)
            if not chosen:
                continue
            bars = mo.get_option_daily_bars(
                chosen["ticker"], t0.date().isoformat(), t0.date().isoformat()
            )
            if bars.empty:
                continue
            real = float(bars["close"].iloc[0])
            model = ol.bs_price("P", s0, float(chosen["strike_price"]), t_years, sigma, r, 0.017)
            if model > 0:
                ratios.append(real / model)
                print(f"  {t0.date()}  K={chosen['strike_price']:7.2f}  real {real:6.2f}  model {model:6.2f}  ratio {real/model:5.2f}")
        except Exception as e:  # noqa: BLE001 — best-effort per expiry
            print(f"  {t0.date()}  skipped ({e})")
    if ratios:
        print(f"  mean real/model premium ratio over {len(ratios)} expiries: {sum(ratios)/len(ratios):.2f}")


def main():
    print("loading SPY / VIX / IRX / CBOE PUT…")
    d = load()
    close, iv, rate = d["close"], d["iv"], d["rate"]
    rolls = ol.roll_dates(close.dropna().index, START, END)
    print(f"loaded; {len(rolls)} monthly rolls {rolls[0].date()} -> {rolls[-1].date()}\n")

    def run(name, structure, **kw):
        return ol.backtest(ol.VolSellSpec(name, structure, **kw), close, iv, rate, START, END)

    # ---- 1) validate the engine: synthetic ATM put-write vs CBOE ^PUT ----
    print("VALIDATION — synthetic ATM put-write vs CBOE PutWrite index (real option prices)")
    atm = run("putwrite_atm", "putwrite", sd=0.0)
    put_real = bench_curve(d["put_index"], atm.equity.index)
    print(fmt("synthetic ATM put-write", atm.equity))
    print(fmt("CBOE ^PUT (real prices)", put_real))
    joint = pd.concat([atm.equity.pct_change(), put_real.pct_change()], axis=1).dropna()
    print(f"  monthly-return correlation: {joint.corr().iloc[0, 1]:.3f}")
    for y in REGIME_YEARS:
        sa, ra, ma = ol.curve_metrics(slice_years(atm.equity, y))
        sp, rp, mp = ol.curve_metrics(slice_years(put_real, y))
        print(f"  {y}: synthetic {ra:+6.1f}% (DD {ma:4.1f}%)  vs  ^PUT {rp:+6.1f}% (DD {mp:4.1f}%)")

    # ---- 2) the sleeve candidates vs buy&hold ----
    print("\nFULL 2006-2026 — candidates vs benchmarks (monthly roll grid)")
    pw1 = run("putwrite_1sd", "putwrite", sd=1.0)
    str1 = run("strangle_1sd", "strangle", sd=1.0, notional_frac=0.25)
    cond = run("condor_1sd_2sd", "condor", sd=1.0, wing_sd=2.0, notional_frac=0.25)
    spy_bh = bench_curve(d["spy_tr"], pw1.equity.index)
    print(fmt("Buy&Hold SPY (total ret)", spy_bh))
    print(fmt("CBOE ^PUT (real prices)", put_real))
    print(fmt("put-write 1-SD OTM", pw1.equity))
    print(fmt("strangle 1-SD (0.25x ntl)", str1.equity))
    print(fmt("condor 1SD/2SD (0.25x)", cond.equity))
    print(f"  condor structural max loss: {cond.max_loss_bound_pct:.1f}% of equity in the worst month")

    print("\nTAIL REGIMES — return% / maxDD% / worst single trade%")
    regime_table([
        ("Buy&Hold SPY", curve_as_result("spy", spy_bh)),
        ("put-write 1-SD OTM", pw1),
        ("strangle 1-SD (0.25x)", str1),
        ("condor 1SD/2SD (0.25x)", cond),
    ])

    print("\nCORRELATION of monthly returns vs SPY (the sleeve's diversification value)")
    spy_rets = spy_bh.pct_change()
    for name, res in [("put-write 1-SD", pw1), ("strangle 1-SD", str1), ("condor 1SD/2SD", cond)]:
        joint_rets = pd.concat([res.equity.pct_change(), spy_rets], axis=1).dropna()
        print(f"  {name:26} | corr {joint_rets.corr().iloc[0, 1]:+.2f}")

    # ---- 3) VRP capture ----
    print("\nVRP CAPTURE (put-write 1-SD)")
    for k, v in ol.vrp_summary(pw1).items():
        print(f"  {k:28} {v if isinstance(v, str) else round(v, 2)}")

    # ---- 4) robustness: strike distance, costs, IS/OOS halves ----
    print("\nROBUSTNESS — strike distance (put-write)")
    for sd in (0.5, 1.0, 1.5):
        res = run(f"pw_{sd}sd", "putwrite", sd=sd)
        print(fmt(f"put-write {sd:.1f}-SD", res.equity))
    print("ROBUSTNESS — entry cost haircut (put-write 1-SD)")
    for c in (0.03, 0.05, 0.10):
        res = run(f"pw_cost{c}", "putwrite", sd=1.0, cost_frac=c)
        print(fmt(f"cost {c*100:.0f}% of premium", res.equity))
    print("IN-SAMPLE 2006-2015 (incl. GFC) vs OUT-OF-SAMPLE 2016-2026 (incl. 2020/2022)")
    for lo, hi, tag in ((2006, 2015, "IS "), (2016, 2026, "OOS")):
        seg = pw1.equity[(pw1.equity.index.year >= lo) & (pw1.equity.index.year <= hi)]
        segc = cond.equity[(cond.equity.index.year >= lo) & (cond.equity.index.year <= hi)]
        print(fmt(f"{tag} put-write 1-SD", seg))
        print(fmt(f"{tag} condor 1SD/2SD", segc))

    # ---- 5) real-chain calibration via the Massive adapter ----
    print("\nREAL-CHAIN CALIBRATION (dataflows/massive_options.py)")
    real_chain_calibration(close, iv, rate, rolls)


if __name__ == "__main__":
    main()
