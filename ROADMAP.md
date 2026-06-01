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

### ☐ M1 — Replayable real-LLM walk-forward backtest  · G1 · H1  ★ highest leverage
**Goal:** run the *actual* agent graph at each historical date and produce a
walk-forward equity curve, not a stub.
**Deliverable:** an `llm_decision_generator(...)` that conforms to the existing
`DecisionGenerator` protocol and drives the real graph, plus a CLI/endpoint to
run a dated range and emit metrics.
**Acceptance bar:** for ≥1 ticker over ≥1 year, produce an equity curve with
Sharpe, max-drawdown, hit-rate, turnover — reproducible from cached as-of data,
with **zero look-ahead** (the `asof` guard holds across the whole run).
**Lands in:**
- `agenticwhales/backtest.py` — `run_backtest(decision_fn=...)` already accepts a
  generator (the seam exists; stub is `momentum_stub_generator`).
- New: `agenticwhales/graph/backtest_generator.py` — wraps `AgenticWhalesGraph`
  + `asof.as_of_date(...)` so each day's decision sees only past data.
- Cost control: cache per (ticker, date, config) so a re-run is cheap; reuse the
  session cache pattern in `web/server.py` / `web/auth.find_cached_session`.
**Depends on:** nothing. **Start here.**

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
equal-weight, flat-coin p=0.5, and Classical-Analyst-alone** (`classical.analyze_classical`).
**Acceptance bar:** a committed report (numbers + plot) and an explicit verdict:
*does the debate beat Classical-alone net of cost?* If no, that's a valid,
valuable result — it redirects the whole roadmap.
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
M1 (real-LLM backtest)  ──►  M2 (costs)  ──►  M3 (go/no-go)  ◀── the H1 gate
        │                                          │
        └─► M5 (labels) ─► M4 (calibration)        ▼  (only if M3 says "yes")
        └─► M8 (agent evals)              M6 (portfolio) ─► M7 (regime)
        └─► M9 (memory)                              │
                                          M10–M13 (compounding + trust)
                                                     │
                                          M14–M16 (real capital)
```

**Next action for the next session:** start **M1** — write
`agenticwhales/graph/backtest_generator.py` that adapts `AgenticWhalesGraph`
into a `DecisionGenerator` under the `asof` guard, and wire it into
`backtest.run_backtest(decision_fn=...)`. The seam already exists; the stub
generator (`momentum_stub_generator`) shows the exact shape to match.

---

*Milestones map 1:1 to the gaps G1–G8 and horizons H1–H4 in
[NORTH_STAR.md](NORTH_STAR.md). Update the ☐/◐/☑ status as work lands.*
