# Edge probe — findings memo (2026-06-07)

> **One line:** A cheap, look-ahead-proof backtest says the LLM debate has **no
> usable edge** (it loses to buy-and-hold and, as shipped, to a random coin) — but
> the *reason* is diagnosable and partly fixable: the model's RLHF caution
> suppresses real directional signal. Forcing it to commit beats a random coin in
> both regimes, yet still cannot beat passively holding the same names. **Do not
> build the alpha substrate; the defensible product is decision-discipline, not
> market-beating returns.**

## What was tested

The North Star G1 question is existential: does the real LLM decision beat naive
baselines, out-of-sample, net of cost? The full M1a→M3 build to answer it
rigorously is weeks of work. This probe is a **cheap tripwire** to kill the thesis
early if it's dead — and it tests a deliberately *reduced* form:

> Does an LLM **bull/bear/judge debate over point-in-time technical features**
> beat a **turnover-matched random sizer**, **Classical-alone**, and
> **buy-and-hold**, net of cost, across two market regimes?

- **Model:** `deepseek-v4-pro`, `temp=0`.
- **Panel (10):** AAPL, MSFT, NVDA, JPM, XOM, JNJ, WMT, GLD (gold), SPY, QQQ.
- **Windows (chosen to straddle the model's knowledge cutoff):**
  - **W1 — tariff-shock drawdown** (2025-02-01 → 2025-07-31): the April-2025
    "Liberation Day" crash + V-recovery. High-vol stress; *partly in the model's
    training memory* (a tailwind that biases toward false positives).
  - **W2 — bull grind, out-of-sample** (2025-08-01 → 2026-05-31): steady uptrend,
    *largely post-cutoff* → the cleaner read.
- **Cost:** 8–10 bps/leg, applied identically to every strategy. Monthly rebalance.

### Why a negative result is trustworthy

The probe's decision function is a **pure function of the as-of-bounded price
slice** — no tool access, no web fetch — so look-ahead is structurally
impossible. This matters because of a finding below.

## 🔴 Infrastructure finding (independent of the verdict)

**The as-of look-ahead guard is wired into nothing.** `bounded_to_as_of` /
`assert_as_of` exist in `asof.py` but are applied to **zero** analysts or dataflow
accessors. Driving the *real* graph at a historical date would fetch current data
straight through the guard and manufacture a fake positive. **The real M1a must
wire the guard into the data accessors before any graph-driven backtest can be
trusted.** (The probe sidesteps this by only consuming the bounded slice.)

## Results

Median Sharpe across the 10-name panel (per-name, monthly rebalance, net of cost):

### Baseline — Hold allowed (this is the shipped decision policy)

| window | LLM | random | classical | buy&hold | beats random | beats classical | beats buy&hold |
|---|---|---|---|---|---|---|---|
| W1 drawdown | −0.07 | −0.02 | 0.00 | 0.81 | 5/10 | 4/10 | 2/10 |
| W2 OOS bull | −0.50 | +0.05 | +0.83 | 1.54 | 3/10 | 0/10 | 1/10 |

**Verdict: KILL** — loses to a turnover-matched random sizer in both regimes. A
5/10 win-rate vs. a coin is the textbook signature of *no skill*.

### Forced-commit — Hold forbidden (ablation isolating timidity)

| window | LLM | random | classical | buy&hold | beats random | beats classical | beats buy&hold |
|---|---|---|---|---|---|---|---|
| W1 drawdown | **+0.66** | +0.01 | 0.00 | 0.81 | **8/10** | 6/10 | 3/10 |
| W2 OOS bull | **+0.27** | −0.03 | +0.83 | 1.54 | **6/10** | 3/10 | 1/10 |

**Verdict: AMBER** — beats random in both windows, but loses to Classical-alone
out-of-sample and to buy-and-hold everywhere.

## What it means — the mechanism

1. **The analysis is fine; the decision policy is the problem.** Removing exactly
   one option — "Hold" — flipped the panel from a coin (5/10 vs random) to clearly
   beating a coin (8/10), in both windows. The debate *does* extract weak
   directional signal; the RLHF "be cautious, don't get blamed" reflex suppresses
   it. In its own theses the baseline repeatedly recognized strong uptrends
   ("price at 52-week highs, 15.8% 120-day return, orderly") and then rated
   **Hold**. A trader commits; an RLHF assistant hedges.

2. **But the latent signal is too weak to be an edge.** Even forced to commit, it
   **loses to passively holding the same names** (3/10 in the drawdown, 1/10 in
   the bull market) and to a dumb trend-follower out-of-sample. It beats a *coin*;
   it does not beat *doing nothing*. Per-name results swing wildly (−2.6 to +1.4)
   — big bets that sometimes hit, the signature of *no stable edge*.

Contributing factors, ranked: (1) RLHF timidity [dominant, baseline]; (2) a
mean-reversion lens on technicals that fades trends; (3) information starvation —
chart only, no news/fundamentals [deliberate, to guarantee no look-ahead];
(4) monthly cadence can't react to fast regime shifts; (5) near-efficient markets
— the floor: no edge over passive holding even with timidity fixed.

## Recommendation

- **Do not build the M1a→M3 alpha substrate to chase returns.** There is no edge
  over buy-and-hold in this (admittedly reduced) test, and the prior is against
  one existing.
- **Do wire the as-of guard into the data accessors** regardless — it's a real
  correctness hole and cheap to fix.
- **Redirect the product** toward decision-discipline / behavioral coaching, where
  the bar is "beat the user's own undisciplined self," not "beat the market." The
  timidity finding suggests the system's *analysis* is worth surfacing to a human
  even though its *autonomous trading* is not.

## Caveats (bounds on the claim)

Technicals-only and monthly cadence (a reduced thesis, not the full product);
small per-name trade counts → noisy per-name Sharpe (panel medians are the signal);
W1 is partly in the model's training memory; a single model. These bound the
result but do **not** overturn the robust, large-margin loss to buy-and-hold.

## Reproduce

```
python tools/run_edge_probe.py            # baseline (Hold allowed)
python tools/run_edge_probe.py --force-commit   # ablation (Hold forbidden)
```

Decisions are cached per (symbol, date, features, model, variant) and the run is
resumable. Core logic + 19 offline tests: `agenticwhales/edge_probe.py`,
`tests/test_edge_probe.py`. Auto-generated per-run reports:
`2026-06-07-edge-probe.md` (baseline) and `2026-06-07-edge-probe-forced.md`.
