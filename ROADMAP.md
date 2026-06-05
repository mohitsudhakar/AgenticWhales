# AgenticWhales — Roadmap to the North Star

> **What this doc is.** The execution plan that turns [NORTH_STAR.md](NORTH_STAR.md)
> (vision + the eight gaps G1–G8) into shippable milestones. Each milestone has a
> concrete deliverable, an **acceptance bar** (how we know it's done), and the
> **files/seams** in the codebase where the work lands. The next session should be
> able to open this, pick the top unblocked milestone, and start building.
>
> **Read order:** [NORTH_STAR.md](NORTH_STAR.md) for *why* → this for *what next*.
>
> **The one rule that orders everything:** build the scoreboard before feeding the
> brain. We do not invest in portfolio construction, better data, the behavioral
> coach, or live execution until **M1 proves the LLM debate has an edge** (or tells
> us it doesn't). Sequencing below honors that.

---

## Operating layer — what's actually in flight

> Added per the 2026-06-02 executive critique (Decision F). A dependency DAG is
> not an operating plan; without a single "now" and a stop-list, work begins
> everywhere and lands nowhere — which is how a ratified P0 slipped two days.

- **Now (WIP = 1):** **M1a** — the deterministic replay substrate. *Nothing else
  in Horizon 1 starts until M1a replays one date byte-identically twice.*
- **Next:** M1b (the generator on the substrate), then M2 → M3 (the go/no-go).
- **Stop-list — do not touch until M3 returns a verdict:** portfolio construction
  (M6), data vendors (M11), behavioral coach (M12), execution/broker (M14),
  multi-market (M15). Building any of these before the edge is proven is building
  on an unproven foundation.
- **Definition of shipped:** a milestone is *done* only when its code is
  **committed and merged to `main`**, not when it exists in a working tree. (The
  2026-06-01 trust fix sat applied-but-uncommitted for 48h while production kept
  serving the defect — "done in a working tree" is not done.)

---

## How to read a milestone

```
M# — Title                                    [gap it closes] · [horizon]
Goal: one sentence.
Deliverable: the concrete artifact that ships.
Acceptance bar: the measurable condition that closes it.
Lands in: the real files / seams to touch.
Depends on: prerequisite milestones.
```

Status legend: ☐ not started · ◐ in progress · ☑ done.

---

## Horizon 1 — Prove or kill the edge

> Nothing else matters until this horizon returns a number. Target: a go/no-go
> on *"does the real LLM debate beat Classical-alone and a flat-coin prior,
> out-of-sample, net of realistic costs?"*

> **M1 was re-cut into M1a + M1b** per the 2026-06-02 critique (Decisions B, C).
> As originally written, M1 bundled a substrate that does not exist (a
> deterministic replay store) with the generator that runs on it — unbounded and
> certain to stall. Split, M1a is independently testable and M1b becomes a small
> wrapper. The original acceptance bar ("≥1 ticker, ≥1 year") was one draw from
> one regime — an anecdote, not a scoreboard; it is now two bars (smoke + evidence).

### ☐ M1a — Deterministic replay substrate  · G1 · H1  ★ Now (WIP=1)
**Goal:** make a dated decision *reproducible* — same date in, byte-identical
inputs out — so any equity curve built on it is evidence, not noise.
**Deliverable:** a **content-addressed replay cache** keyed on
`(ticker, as-of-date, prompt-hash, model, config)`, immutable, plus **frozen
tool-data snapshots** so that analysts which fetch news / prices / embeddings
read from the snapshot, not the live web.
**Acceptance bar:** replay the *same* date twice → **byte-identical** model
inputs (smoke bar). Two determinism traps must be closed and tested:
- **LLM nondeterminism** — force `temperature = 0` for replay; any `temp > 0`
  makes the curve unreproducible.
- **Tool-data look-ahead** — the `asof` guard checks the *requested* date, not
  the *content's* date; a live fetch pulls post-as-of data straight through it.
  The snapshot must freeze *tool outputs*, not just the date parameter.
**Lands in:** new `agenticwhales/replay.py` (the content-addressed store +
snapshot freezer); `agenticwhales/asof.py` (extend the guard to assert on
snapshot provenance). **Do not** reuse `auth.find_cached_session` — that cache is
keyed for live session reuse, not dated deterministic replay.
**Depends on:** nothing. **Start here.**

### ☐ M1b — Real-LLM decision generator on the substrate  · G1 · H1
**Goal:** run the *actual* agent graph at each historical date and produce a
walk-forward equity curve, not a stub.
**Deliverable:** an `llm_decision_generator(...)` conforming to the existing
`DecisionGenerator` protocol, driving the real graph against M1a's snapshot, plus
a CLI/endpoint to run a dated range and emit metrics.
**Acceptance bar — two bars, not one:**
- *Smoke* (entry): one ticker, one date, `temp=0`, replayed twice byte-identical,
  zero look-ahead — runs end-to-end as an N=1 smoke test *before* any multi-year
  run, so we don't discover a leak after burning the budget.
- *Evidence* (exit, feeds M3): a **panel of ≥20 names across sectors + ≥1 crypto
  over ≥3 years spanning at least one drawdown regime**, reporting the
  **distribution** of per-name Sharpe / max-drawdown / hit-rate / turnover — not a
  single aggregate number.
**Lands in:** `agenticwhales/backtest.py` (`run_backtest(decision_fn=...)`, seam
exists; stub is `momentum_stub_generator`); new
`agenticwhales/graph/backtest_generator.py` (wraps `AgenticWhalesGraph` to read
only from M1a's snapshot).
**Cost envelope (required before the evidence run):** a dry-run estimate of
`dates × names × agents × providers` LLM calls and a **per-backtest budget cap**
distinct from the global daily/monthly cost cap — otherwise a multi-year panel run
either trips the global cap mid-run (producing a non-deterministic partial curve)
or silently throttles. `agenticwhales/backtest.py` has *no* cost guard today.
**Depends on:** M1a.

### ☐ M2 — Realistic slippage / market-impact model  · G6(partial) · H1
**Goal:** backtests and paper trades pay realistic costs so the equity curve
doesn't lie.
**Deliverable:** a cost model (spread + size-aware impact) replacing the flat
`max_slippage_bps` placeholder, applied in both the fill engine and the backtest.
**Acceptance bar:** a configurable impact function; M1's curve recomputed with
costs on, and the cost drag reported as a line item.
**Lands in:** `agenticwhales/paper.py` (`_apply_fill_python`, slippage calc),
`agenticwhales/risk.py` (`max_slippage_bps`), `agenticwhales/backtest.py`.
**Depends on:** M1 (so we measure the same curve with/without costs).

### ☐ M3 — Baseline gauntlet + the go/no-go report  · G1 · H1
**Goal:** answer the existential question with a chart, not a vibe.
**Deliverable:** a report comparing M1's net-of-cost curve against **buy-and-hold,
equal-weight, flat-coin p=0.5, Classical-Analyst-alone** (`classical.analyze_classical`),
**and a cost-and-turnover-matched random sizer** (same names, same trade count,
same costs, random signs). Added per Decision E: beating buy-and-hold can be pure
beta; beating a turnover-matched random trader on the *same* names net of cost is
the first honest evidence of *skill*, not exposure.
**Acceptance bar:** a committed report (numbers + plot) and an explicit verdict:
*does the debate beat Classical-alone **and the turnover-matched random sizer**,
net of cost?* If no, that's a valid, valuable result — it redirects the whole
roadmap.
**Lands in:** `tests/evals/` (new `llm_backtest_eval.py`), `tests/evals/reports/`.
**Depends on:** M1, M2.

### ☐ M4 — Calibration as a measured experiment  · G3(partial) · H1
**Goal:** prove (or disprove) that Platt calibration improves results, not just
that the mechanism runs.
**Deliverable:** an experiment over resolved outcomes: out-of-sample Brier and
realized PnL **with vs. without** calibration applied.
**Acceptance bar:** a reliability curve + a measured Δ-Brier and Δ-PnL; ship the
answer. Promote `apply_if_opted_in` from opt-in-by-faith to backed-by-evidence.
**Lands in:** `agenticwhales/calibration.py`, `agenticwhales/outcomes.py`
(needs the M5 labels to be meaningful), `tests/evals/`.
**Depends on:** M1 (enough resolved outcomes), ideally M5.

**Horizon-1 exit criteria:** a committed equity curve vs. four baselines, a
calibration reliability curve, and a written go/no-go. *This is the gate to H2.*

---

## Horizon 2 — Make it a fund, not a stock-picker

### ☐ M5 — Better outcome labels  · G3 · H2
**Goal:** stop scoring on `hit = positive PnL` (a weak target).
**Deliverable:** risk-adjusted, benchmark-relative outcome labels (alpha vs.
SPY, vol-scaled) alongside the raw hit.
**Acceptance bar:** `decision_outcomes` carries the new labels; calibration +
memory + prompt-eval all train on them.
**Lands in:** `agenticwhales/outcomes.py` (`_resolve_one`, `OutcomeRow`,
`brier_component`), the `decision_outcomes` schema (`docs/migrations/`).
**Depends on:** M1.

### ☐ M6 — Portfolio construction + book-level risk  · G2 · H2  ★ the "fund" milestone
**Goal:** reason about the *book*, not one name at a time.
**Deliverable:** a portfolio layer — position→book aggregation, gross/net
exposure limits, correlation-aware sizing (shrink correlated bets), a rebalance
loop. `RiskGuard` evolves from per-trade clamp to book-level budget allocator.
**Acceptance bar:** the allocator respects gross/net + per-name + correlation
budgets on a multi-position book; a rebalance produces target weights; tested.
**Lands in:** `agenticwhales/risk.py` (`RiskGuard` → allocator),
`agenticwhales/paper.py` (NAV/exposure aggregation), new
`agenticwhales/portfolio_construction.py`.
**Depends on:** M1/M3 (only build the fund brain once the stock-picker is proven).

### ☐ M7 — Regime-aware aggregate de-risking  · G2 · H2
**Goal:** scale *aggregate* risk by market regime, not just per-name vol.
**Deliverable:** a vol/trend regime detector that modulates the book-level risk
budget (extends the per-name `vol_regime_multiplier` already in `classical.py`).
**Acceptance bar:** measurable drawdown reduction in M1's backtest with regime
de-risking on vs. off.
**Lands in:** `agenticwhales/classical.py` (`vol_regime_multiplier`),
M6's allocator.
**Depends on:** M6.

### ☐ M8 — Per-agent evals + hallucination guards  · G5 · H2
**Goal:** measure whether each agent earns its cost and isn't making things up.
**Deliverable:** (a) graded rubrics per analyst + an ablation showing each
agent's marginal lift; (b) a guard that cross-checks PM scalars against cited
tool evidence and flags unsupported theses.
**Acceptance bar:** an ablation table (each agent's Δ decision-quality); a
hallucination flag rate reported per run.
**Lands in:** `tests/evals/` (extend `diversity_engine_eval.py`), a new guard in
`web/runner.py` post-decision path, `agenticwhales/disagreement.py`.
**Depends on:** M1.

### ☐ M9 — Validate memory retrieval  · G3 · H2
**Goal:** prove outcome-predictive retrieval improves next-call accuracy.
**Deliverable:** an A/B — decision quality with memory-v2 retrieval on vs. off.
**Acceptance bar:** a measured lift (or a kill decision); ship the number.
**Lands in:** `agenticwhales/memory_v2.py` (`retrieve_relevant`,
`_predictiveness_for`), `tests/evals/`.
**Depends on:** M1, M5.

---

## Horizon 3 — Compounding intelligence + trust

### ☐ M10 — Candidate-prompt registry + shadow A/B  · G3 · H3
Promote `adaptive.evaluate_prompt_variant` from a flat-coin canary to a real
registry with shadow scoring and auto-promotion gated on out-of-sample Brier.
**Lands in:** `agenticwhales/adaptive.py`, the weekly prompt-eval cron in
`web/scheduler.py`. **Depends on:** M4, M5.

### ☐ M11 — Point-in-time data + measured alt-data IC  · G4 · H3
Move to survivorship-bias-free, point-in-time fundamentals; treat the existing
congress-trades / X-recs signals as **features with measured information
coefficient**, not decoration.
**Lands in:** `agenticwhales/dataflows/*` (vendor adapters), the signal modules,
`tests/evals/` (IC measurement). **Depends on:** M1, M3.

### ☐ M12 — NLP behavioral coach with $-attributed bias cost  · G7 · H3
Layer embeddings/sequence modeling over the journal; cluster trades into setups;
attribute drawdown to behavioral patterns with a quantified "this bias costs you
X% expectancy." Upgrades the four fixed-threshold detectors.
**Lands in:** `agenticwhales/behavioral.py`, `agenticwhales/memory_v2.py`,
the Journal UI. **Depends on:** M5.

### ☐ M13 — Live track record + return attribution  · G8 · H3
A first-class, immutable, signed track record; decompose returns by signal,
agent, and regime. Builds on the existing `audit_log` + attestation + RLS.
**Lands in:** new `decision_outcomes` rollups, `web/admin.py` (a track-record
dashboard), `docs/migrations/`. **Depends on:** M1, M8.

---

## Horizon 4 — Real capital (gated on everything above)

### ☐ M14 — Execution simulator → paper broker → live broker
Order-routing logic (limit/TWAP/VWAP), realistic fills against a book, then —
behind a hard custody/compliance boundary — a paper-first then live broker
adapter. **Depends on:** M2, M6, M13.

### ☐ M15 — Multi-market, multi-currency
Extend beyond US equities/crypto; per-market calendars already exist in
`agenticwhales/calendar.py`. **Depends on:** M6.

### ☐ M16 — Identity / reputation / custody framework
The far-horizon trust/custody story. The attestation + audit + RLS groundwork
exists to build on. **Depends on:** M13, M14.

---

## The scoreboard (from NORTH_STAR §5 — what every milestone reports to)

| Dimension | Metric | Bar | First measured by |
|---|---|---|---|
| Edge | Walk-forward Sharpe vs. buy-and-hold & flat-coin | beats both, OOS | M1/M3 |
| Edge | Information ratio vs. benchmark | > 0.5, stable | M3 |
| Calibration | Expected Calibration Error (ECE) | < 0.05, improving | M4 |
| Knows-what-it-knows | Brier vs. flat-coin canary | strictly better | M4 |
| Survival | Max drawdown vs. benchmark | materially lower | M3/M7 |
| Learning | Δ OOS Brier after calibration/memory on | negative (improvement) | M4/M9 |
| Cost discipline | $ per decision vs. realized edge | edge ≫ cost | M2/M3 |
| Agent value | Marginal decision-quality per agent (ablation) | each pays for itself | M8 |
| Trust | % decisions evidence-supported | → 100% | M8 |
| Behavioral | Quantified $ cost of detected biases | reported, trending down | M12 |

---

## Critical path (the short version)

```
M1a (replay substrate) ─► M1b (real-LLM gen) ─► M2 (costs) ─► M3 (go/no-go) ◀─ H1 gate
        │                        │                                  │
        │                        └─► M5 (labels) ─► M4 (calibration) ▼ (only if M3="yes")
        │                        └─► M8 (agent evals)      M6 (portfolio) ─► M7 (regime)
        └─ byte-identical replay  └─► M9 (memory)                  │
           is the gate to M1b                          M10–M13 (compounding + trust)
                                                                   │
                                                        M14–M16 (real capital)
```

**Next action for the next session:** start **M1a** (the only "now") — create
`agenticwhales/replay.py` with a content-addressed store keyed on
`(ticker, as-of-date, prompt-hash, model, config)` and a tool-output snapshot
freezer, with `temp=0` enforced. Its acceptance test is the gate to M1b: replay
one date twice and assert the model inputs are **byte-identical**. Only then write
`agenticwhales/graph/backtest_generator.py` (M1b) to run the real graph against
that frozen snapshot.

---

*Milestones map 1:1 to the gaps G1–G8 and horizons H1–H4 in
[NORTH_STAR.md](NORTH_STAR.md). Update the ☐/◐/☑ status as work lands.*
