# Vol-selling sleeve test — is the variance risk premium harvestable without blowing up?

The explicit next step from the [trend memo](2026-06-08-trend-test.md): a defined-risk
premium-selling backtest on SPY measuring variance-risk-premium capture AND tail behaviour
across 2008/2020/2022 — the regimes where naive vol sellers die. Monthly cycle: at each
third-Friday expiry, sell options at a fixed standard-deviation distance implied by VIX,
hold to expiry, settle at intrinsic. `tools/run_volsell_test.py`. Engine:
`agenticwhales/options_lab.py`. Data adapter (real chains, Greeks + IV):
`agenticwhales/dataflows/massive_options.py` (Massive.com; `finnhub_options.py` as the
cheap alternative).

**Pricing honesty up front:** only path to 2008 is model pricing — Black-Scholes entry
premiums at VIX, settlement at intrinsic (model-free). Two anchors keep the model honest:
(1) the synthetic ATM put-write is validated against the **CBOE PutWrite index (^PUT)** —
real S&P option prices, no model; (2) with `MASSIVE_API_KEY` set, entry premiums are
compared to real SPY chains (not yet run — key unset; see Gate).

## Engine validation vs reality (2006–2026)

| | Sharpe | return | maxDD | corr (monthly) |
|---|---|---|---|---|
| synthetic ATM put-write | 0.83 | +563% | 31.7% | — |
| **CBOE ^PUT (real prices)** | **0.64** | **+321%** | **32.8%** | **0.96** |

Regime slices agree too (2008: −19.4% vs −22.8%; 2020: −3.9% vs +0.4%; 2022: −2.4% vs
−4.1%). **Read:** the model reproduces the *shape* of put-selling P&L almost exactly
(0.96 correlation, same drawdowns) but **overstates long-run return** (premiums too rich:
flat vol at VIX ≈ variance-swap level, no skew, optimistic fills). All synthetic numbers
below are therefore **upper bounds**; the honest discount on returns is roughly a third
over 20 years.

## Results (Sharpe / total return % / maxDD %), 2006–2026, monthly roll grid

| | full period | 2008 ret/DD/worst-mo | 2020 | 2022 | corr vs SPY |
|---|---|---|---|---|---|
| Buy&Hold SPY (total ret) | 0.68 / +752 / 48 | −32 / 44 / **−25** | +13 / 31 / **−31** | −11 / 17 / −11 | 1.00 |
| CBOE ^PUT (real) | 0.64 / +321 / 33 | −23 / 33 / — | +0.4 / 28 / — | −4 / 13 / — | — |
| put-write 1-SD OTM | 0.25 / +41 / 28 | −12 / 18 / −17 | **−22 / 28 / −28** | +0.5 / 4 / −4 | +0.66 |
| strangle 1-SD (0.25× ntl) | 1.32 / +71 / 6.9 | +0.5 / 3.7 / −3.7 | −4 / 6.9 / −6.4 | +2.7 / 0.7 / −0.7 | +0.52 |
| **condor 1SD/2SD (0.25×)** | **2.60 / +77 / 1.6** | **+2.3 / 1.6 / −1.6** | **+1.2 / 1.5 / −0.8** | **+2.3 / 0.7 / −0.7** | **+0.33** |

VRP capture (1-SD put-write): mean implied−realized = **+3.4 vol pts**, positive in **83%
of months** — consistent with the literature. Win rate 91%, avg loss −4.1%, **worst single
month −27.6% (March 2020)**: one month erased ~5 years of collected premium.

## Read
- **The premium is real; the naive harvest is not.** VRP exists (+3.4 pts, 83% of months),
  but the 1-SD cash-secured put-write is the textbook blow-up: Sharpe 0.25 over 20 years,
  −22% in 2020 *while SPY made +13%*. Selling far-OTM naked is the worst of both worlds —
  tiny premium, full tail. The trend memo's fear is confirmed, with numbers.
- **Strike distance is monotone, not knife-edge:** 0.5-SD Sharpe 0.54 > 1.0-SD 0.25 >
  1.5-SD 0.08. Closer to ATM earns more (the real ^PUT at ATM: 0.64) — but with
  equity-sized drawdowns either way. Distance alone does not manage the tail.
- **Structure does. The defined-risk condor (short 1-SD strangle + long 2-SD wings, 0.25×
  notional) transforms the tail:** worst month −1.6% across all three crisis years,
  *positive* in 2008, 2020 AND 2022, structural max loss 6.8% of equity, SPY correlation
  0.33. It made +2.3% in 2022 — the year both trend (−0.5%) and buy&hold (−11%) struggled —
  which is exactly the sideways-sleeve role.
- **Costs barely matter at monthly cadence** (3%→10% premium haircut moves full-period
  return by ~11 pts), so the result isn't a fee artifact.

## Caveats (the condor number is NOT yet believable)
- **No skew is the big one.** Flat-vol BS underprices 2-SD put wings relative to real
  markets (steep index put skew), so the condor's net credit — and therefore its Sharpe
  2.6 — is overstated *specifically where this structure is sensitive*. The ATM validation
  does **not** validate the skew-dependent structures. Real-chain calibration via the
  Massive adapter (free tier ≈ 2y of history) is mandatory before believing any condor
  number. Direction of bias for the strangle is mixed (short put leg understated by skew,
  short call leg overstated); for the condor it is unambiguously flattering.
- **Roll-date marking hides intra-month pain.** Hold-to-expiry P&L at monthly marks; real
  mark-to-market drawdowns (and margin calls on the naked variants) are strictly worse.
  The condor's capped structure is least exposed to this, the strangle most.
- European exercise, continuous strikes, no early assignment, no gap-through-wing
  slippage, single underlying (SPY), taxes ignored.
- **Multiple testing:** 3 structures × sweeps were tried; the condor is the best of them.
  IS (2006–15, incl. GFC) Sharpe 1.98 → OOS (2016–26, incl. 2020/2022) 3.39 — no IS→OOS
  degradation, which mitigates but does not eliminate the concern.

## Verdict & gate
**The sleeve stays GATED — not in the product UI.** Two of three tail tests resolve
clearly: naive short-vol fails (put-write −28% month confirms the blow-up regime); the
defined-risk condor passes 2008/2020/2022 *in-model* with the lowest SPY correlation of
anything tested so far. But the condor's economics ride on wing prices the model flatters.
Promotion gate, in order:
1. Set `MASSIVE_API_KEY` (.env) and run the real-chain calibration block in
   `tools/run_volsell_test.py`: real vs model premium ratios on ~24 recent expiries, for
   the short strikes AND the wings.
2. Re-run the condor with skew-corrected premiums (or directly on real chain history via
   the flat files); tail behaviour must survive.
3. Only then: test the condor as a third sleeve next to the 50/50 trend/buy&hold blend.
