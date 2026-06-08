# Diversified trend test — can we make money without predicting, in both directions?

Time-series momentum (long if 12m return > 0, short/flat if < 0, vol-targeted) across an
uncorrelated 9-ETF basket (SPY QQQ EFA TLT IEF GLD DBC UUP VNQ), vs buy-and-hold, net of
5 bps. `tools/run_trend_test.py`. Engine: `agenticwhales/strategy_lab.py`.

## Results (Sharpe / total return % / max drawdown %)

| | 2020 crash | 2022 bear | 2023-24 bull | full 2018-2026 |
|---|---|---|---|---|
| **Buy&Hold SPY** | 0.08 / −3 / **34** | −0.75 / **−19** / 25 | **1.84 / +57** / 10 | 0.80 / **+213** / **34** |
| Buy&Hold basket | 0.23 / +1 / 17 | −1.06 / −12 / 15 | 1.49 / +26 / 7 | 0.96 / +122 / 18 |
| Trend long/flat | 0.52 / +1 / **5** | −1.28 / −5 / **5** | 1.16 / +12 / 4 | 0.93 / +54 / **8.5** |
| Trend long/short | **0.60 / +1.6 / 3** | **−0.03 / −0.5** / 8 | 0.36 / +4 / 6 | 0.44 / +24 / 11 |
| **50/50 blend** | 0.31 / +1.4 / 11 | −1.23 / −8.5 / 10 | 1.41 / +19 / 5.5 | **1.01** / +86 / 12 |

## Read
- **The thesis holds for crash defense.** In the 2020 crash and 2022 bear — where Buy&Hold
  SPY lost 19–34% — diversified trend stayed **positive (+1.6% in 2020) to flat (−0.5% in
  2022)** with single-digit drawdowns. Non-predictive, works in both directions. ✅
- **The 50/50 blend (buy&hold + trend overlay) is the product.** Best risk-adjusted return
  of everything (**Sharpe 1.01 vs SPY 0.80**), ~**1/3 the drawdown** (12% vs 34%), while
  still capturing real bull upside (+86% full period; +19% in 2023-24).
- **The honest cost: absolute return.** Pure SPY made +213% (a historic bull). The blend
  made +86%. You trade total return for far smaller losses, positive crash returns, and
  higher Sharpe. For "make money consistently + manage risk" (the stated goal), the blend
  wins; for "max return in a bull," leveraged SPY wins — until it doesn't.
- **This is risk-premia harvesting, not alpha/skill.** You're paid to bear trend-reversal
  and equity risk, with diversification + vol-targeting as the risk management.

## Caveats (don't over-extrapolate)
- **Period-favorable.** 2018–2026 had trendy crashes (2020, 2022). Trend-following had a
  brutal *drought* 2010–2019 (whipsaws); that window would look much worse. Needs
  walk-forward across decades before believing it's durable.
- One parameter set (12m / 15% vol), one basket, ~in-sample. Costs at 5 bps; daily
  vol-targeting adds turnover; shorts have borrow costs; taxes ignored.
- **No sideways/vol-selling sleeve** — that needs historical options/implied-vol data
  (ORATS/CBOE). Both trend and buy&hold struggle in pure chop; harvesting sideways markets
  is the vol-selling premium, which carries the tail risk and is the next thing to test.

## Next steps
1. **Walk-forward** the blend across 2007–2026 (multiple regimes incl. the trend drought) to
   confirm it isn't period-luck.
2. **Vol-selling sideways sleeve** once options data is sourced — defined-risk only.
3. **Risk-managed leverage**: the blend's 12% drawdown + Sharpe 1.01 can be levered ~1.5×
   toward SPY-like return while keeping the crash protection — the right way to add leverage.
