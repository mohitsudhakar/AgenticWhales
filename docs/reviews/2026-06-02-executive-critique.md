# Executive Critique — 2026-06-02

> **Format.** A threefold review of the *entire* AgenticWhales product, run as a
> debate between three reviewer lenses — **Sundar Pichai** (product /
> distribution / trust), **Demis Hassabis** (research rigor / measurable edge),
> **Jeff Dean** (systems / scale / cost). Each raises points, they *disagree
> with each other*, and every open thread is driven to a **decision + concrete
> fix**. Grounded in the code on `roadmap-north-star` today.
>
> **This is the second run.** Yesterday's critique
> ([2026-06-01](2026-06-01-executive-critique.md)) produced a decision register
> A–G. This run does two things yesterday's could not: (1) it **holds those
> decisions accountable** against what actually shipped in 24h, and (2) it
> critiques the **one real artifact produced since** — [ROADMAP.md](../../ROADMAP.md)
> (milestones M1–M16). Where a call was made without the operator present it is
> flagged **[chair]** and is reversible.

---

## 0. The state in one paragraph: the plan got better, the product did not move

In the 24 hours since the last critique, three commits landed:
`docs(roadmap)` (the M1–M16 plan), a marketing-landing merge already in flight,
and a `/fund` init fix. **The roadmap is genuinely good** — it maps the eight
North-Star gaps 1:1 to sequenced milestones, each with a deliverable, an
acceptance bar, and the real files to touch. But measured against yesterday's
decisions, the scoreboard is stark:

| Yesterday's decision | Priority | Status today | Evidence |
|---|---|---|---|
| **A** — reframe Strategy Lab as "backtest your thesis," no system-edge claims | P0 | **Mostly already-honest, partial** | Hero copy is defensible ("a committee of AI analysts that researches, debates, sizes every trade", [welcome.html:293](../../web/static/welcome.html)); soft "compound into an edge" at [welcome.html:351](../../web/static/welcome.html) is the only residue. |
| **B** — kill the `100+` floor / `2×` waitlist multiplier | **P0 quick win** | **Was still live 24h later → fixed in *this* run** | Pre-fix: `DISPLAY_FLOOR=100`, `shown = n*2` in [waitlist.py](../../web/waitlist.py); hardcoded `100+` at welcome.html. |
| **C / M1** — real-LLM walk-forward backtest | P0 | **Not started** | No `agenticwhales/graph/backtest_generator.py`; [backtest.py:17](../../agenticwhales/backtest.py) still reads *"deferred"*; `momentum_stub_generator` is still the only generator. |
| **D** — cost-per-decision on outcomes + `/metrics` | P1 | Not started | — |
| **E** — freeze `/analyze`, banner it | P1 | Not started | No "feature-frozen" banner present. |
| **F** — real embedding key, then learning-loop A/Bs | P2 | Not started (correctly gated on C) | — |

**The through-line for today:** the team converts ideas into *documents* faster
than into *shipped code*. We now hold NORTH_STAR + ROADMAP + GUIDEBOOK +
AgenticWhales_Future + two executive critiques — five-plus strategy artifacts —
and **zero** of the scoreboard they all agree is the only thing that matters.
Worse, the single trivial trust fix from yesterday (Decision B) was left live in
a *financial* funnel for another day. Planning is not the bottleneck. Execution
is. This critique is therefore deliberately short on new strategy and long on
**making the existing plan executable and actually executed.**

---

## 1. Sundar — product, distribution, trust

**S1. A ratified P0 trust fix sat un-shipped for a day.** Decision B was
unanimous yesterday and is a three-line change. It was still serving fabricated
social proof (`100+`, doubled) to real signups on a *finance* landing page 24h
later. The lesson isn't "decide it again" — it's that **decisions without an
owner who ships them are theater.** (Applied in this run; see §4-A.)

**S2. The doc-to-ship ratio is now the product risk.** Five strategy docs, two
critiques, a beautiful roadmap — and the funnel still can't show a stranger one
true number about whether the fund works. Roadmaps don't convert; an equity
curve does. Every hour spent re-articulating the plan is an hour the plan isn't
being executed. *Stop writing strategy. Start M1.*

**S3. The roadmap has no dates, no owners, no WIP limit.** M1–M16 is a perfect
dependency DAG and a poor *operating* plan: nothing says *who* does M1, *by
when*, or *what we refuse to start until it lands*. A plan that lets work begin
anywhere will see work begin everywhere — which is exactly how B slipped.

## 2. Demis — research rigor, measurable edge

**D1. M1's acceptance bar is statistically a single sample, and M3 gates the
whole company on it.** [ROADMAP.md:46](../../ROADMAP.md) sets M1's bar at
*"≥1 ticker over ≥1 year."* One ticker, one year is **one draw from one
regime** — an anecdote, not a scoreboard. M3 then makes a go/no-go on the entire
roadmap from it. That is the cardinal sin of quant: you will either greenlight on
noise or kill on noise, and you won't know which. The bar must specify a **panel
and report dispersion**, not a point estimate.

**D2. M4 trains calibration on *backtest* outcomes — calibrating to your own
assumptions.** [ROADMAP.md:87](../../ROADMAP.md) has M4 depend on M1 "for enough
resolved outcomes." But backtest-resolved outcomes are simulated under M1's own
fill/slippage model. Fit Platt to those and you've calibrated the model to its
own priors, then shipped it as if it earned live trust. **Outcome provenance
(backtest vs. live) must be a first-class label**, and calibration must never be
*promoted* on backtest-only outcomes.

**D3. The flat-coin baseline is necessary but not sufficient.** M3's gauntlet
(buy-and-hold, equal-weight, flat-coin, Classical-alone) is good. The one that
actually threatens the thesis is missing: a **cost-and-turnover-matched random
trader** with the same position-sizing and the same number of trades. Beating
buy-and-hold can be pure beta exposure; beating a turnover-matched random sizer
on the *same names* is the first honest evidence of skill.

## 3. Jeff Dean — systems, scale, cost

**J1. M1 cites the wrong cache, and the real determinism traps are unspecced.**
[ROADMAP.md:53](../../ROADMAP.md) says reuse "the session cache pattern in
`web/server.py` / `auth.find_cached_session`." That cache is keyed for *live
session reuse*, not for deterministic dated replay. Replay needs a **dedicated
content-addressed cache keyed on (ticker, as-of-date, prompt-hash, model,
config)**, immutable. And the harder problems aren't named at all: (a) **LLM
nondeterminism** — any `temperature > 0` makes the curve unreproducible; force
`temp=0` for replay; (b) **tool-data look-ahead** — analysts that fetch news /
prices / embeddings *live* will pull post-as-of data straight through the
`asof` guard, because the guard checks the *requested* date, not the *content's*
date. The snapshot must freeze tool outputs, not just the date parameter.

**J2. M1 is mis-scoped as "depends on nothing — start here."** As written, M1
bundles two jobs: *build the replay substrate* (the content-addressed snapshot +
response cache, which does **not** exist — [asof.py](../../agenticwhales/asof.py)
is a guard, not a store) and *write the generator*. Bundled, M1 is unbounded and
will stall. Split it: **M1a = the deterministic replay substrate; M1b = the
generator that runs on it.** That also makes M1a independently testable (replay
the same date twice → byte-identical inputs).

**J3. We will spend real money on M1 before we know if it can produce a signal.**
The cheapest possible first pass — single ticker, single quick model, `temp=0`,
fully cached — should run *end-to-end on one date* as a smoke test before any
multi-year, multi-name run. Validate the pipeline is deterministic and
look-ahead-free on N=1 day, *then* scale to D1's panel. Don't discover the
snapshot leaks look-ahead after burning the budget on a year of dates.

---

## 4. The debates, one by one — and where they land

### Debate A — The un-shipped P0 (S1, consensus)

- **Sundar:** B was decided, trivial, and trust-critical. It should never have
  survived a day. Ship it now, in this run.
- **Demis / Jeff:** No dissent. Showing a fabricated count on a finance product
  is the one thing in this repo that is unambiguously wrong.

> **Decision A [chair] — DONE in this run.** Removed the `100+` floor and `2×`
> multiplier. `display_count` now returns the **true count**, or `0` (counter
> hidden) below a real `PROOF_MIN = 25` threshold — no fabricated floor.
> **Fix applied:** [waitlist.py](../../web/waitlist.py) (`display_count` +
> `PROOF_MIN`), [welcome.html](../../web/static/welcome.html) (dropped the
> hardcoded `100+`, render only when the real figure clears the threshold),
> [server.py:228](../../web/server.py) (docstring), and
> [test_waitlist.py](../../tests/test_waitlist.py) (curve + endpoint tests
> rewritten to assert the honest contract — **21 passed**). Reversible via git.

### Debate B — M1's acceptance bar: anecdote vs. scoreboard (D1 vs. J3)

- **Demis:** "≥1 ticker, ≥1 year" cannot gate a company. Require a panel — e.g.
  **≥20 names across sectors + ≥1 crypto, ≥3 years spanning at least one
  drawdown regime** — and report the *distribution* of per-name Sharpe, not a
  single number.
- **Jeff:** Agreed on the *exit* bar, but the *entry* bar should stay tiny.
  Don't run the panel until N=1 day proves the pipeline is deterministic and
  leak-free (J3). Two bars, not one.
- **Sundar:** Fine, as long as the *shippable artifact* M3 produces is the
  panel-level curve with dispersion — that's what makes the go/no-go credible to
  anyone outside the team.

> **Decision B.** **Split M1's bar into a smoke bar and an evidence bar.**
> **Fix:** edit [ROADMAP.md](../../ROADMAP.md) M1 → (i) *smoke acceptance*: one
> ticker, one date, `temp=0`, replayed twice byte-identical, zero look-ahead;
> (ii) *evidence acceptance* (feeds M3): a panel of ≥20 names + ≥1 crypto over
> ≥3 years including a drawdown regime, reporting per-name Sharpe **dispersion**,
> not just the aggregate. *Owner: brain.*

### Debate C — M1 is two milestones (J1 + J2, with Demis on determinism)

- **Jeff:** M1 secretly contains the substrate that doesn't exist. Split into
  **M1a (content-addressed replay cache + frozen tool-data snapshot, `temp=0`)**
  and **M1b (the `DecisionGenerator` that runs on it)**. M1a is the one that
  makes the whole thing reproducible — and the one the roadmap doesn't name.
- **Demis:** Determinism *is* the science here — an unreproducible backtest is
  not evidence. M1a's acceptance test (replay a date twice → identical inputs)
  is non-negotiable.
- **Sundar:** Don't let the split become an excuse for two months of substrate
  work with nothing demoable. M1b must be runnable on a single cached date the
  moment M1a covers one date.

> **Decision C.** **Re-cut M1 into M1a + M1b in the roadmap**, and **correct the
> cache reference**. **Fix:** ROADMAP M1 "Lands in" should cite a *new*
> content-addressed replay cache (key: ticker, as-of-date, prompt-hash, model,
> config), **not** `auth.find_cached_session`; add explicit line items for
> `temp=0` replay and **frozen tool-data snapshots** (close the live-fetch
> look-ahead hole the `asof` guard doesn't catch). M1a's acceptance: same-date
> replay is byte-identical. *Owner: brain + systems.*

### Debate D — Calibration provenance (D2, with Jeff)

- **Demis:** Calibration fit on backtest outcomes calibrates to the model's own
  fill assumptions. That must never be promoted as live-earned trust.
- **Jeff:** Cheap to enforce — add an `outcome_source` enum (`backtest` |
  `paper` | `live`) to `decision_outcomes` and make the calibration fitter
  filter on it.
- **Sundar:** And the dashboard must label which curve is which, or we'll fool
  ourselves and then our users.

> **Decision D.** **Make outcome provenance first-class; gate calibration
> promotion on non-backtest outcomes.** **Fix:** add `outcome_source` to
> `decision_outcomes` ([outcomes.py](../../agenticwhales/outcomes.py) +
> migration); `calibration.apply_if_opted_in` and the fitter exclude
> `backtest` rows from *promotion* (may still *report* on them). Sequenced into
> M4/M5. *Owner: brain.*

### Debate E — Turnover-matched random baseline (D3)

- **Demis:** Add a cost-and-turnover-matched random sizer on the same names to
  M3's gauntlet — it's the baseline that isolates skill from beta.
- **Jeff:** Nearly free; it reuses M1's fill engine with random signs.
- **Sundar:** It's also the most *honest* chart for the funnel — "beats a coin
  flip trading the same names, net of cost" is a claim a skeptic respects.

> **Decision E.** **Add a turnover-matched random baseline to the M3 gauntlet.**
> **Fix:** extend M3's baseline set in [ROADMAP.md:68](../../ROADMAP.md) and the
> eval to include a random-sign sizer matched on trade count + cost. *Owner:
> brain.* **Cheap.**

### Debate F — The roadmap needs an operating layer (S3, consensus)

- **Sundar:** Add owners, a "now / next / later" WIP cap, and the explicit
  *stop-list* — what we will not touch until M1 lands.
- **Jeff:** Make M1a the single "now." One in-flight milestone until the
  scoreboard exists.
- **Demis:** Agreed — focus is the only thing that gets us a number.

> **Decision F [chair].** **Add a thin operating layer to the roadmap:** a
> "Now = M1a (only)" marker, a one-in-flight WIP rule for Horizon 1, and a
> stop-list (no portfolio/data/coach/exec work until M3 returns a verdict).
> **Fix:** a short header block in [ROADMAP.md](../../ROADMAP.md). No new
> milestones. Reversible. *Owner: product.*

---

## 5. Decision register (who said what → what we do)

| # | Decision | Raised by | Resolved against | Priority | Status |
|---|---|---|---|---|---|
| A | Kill the `100+` floor / `2×` multiplier; show the true count or hide it | Sundar (S1) | consensus | **P0** | **DONE this run** (21 tests pass) |
| B | Split M1's bar into a smoke bar (N=1 date) + an evidence bar (≥20-name panel, ≥3y, report dispersion) | Demis (D1) | Jeff's tiny-entry-bar (J3) | **P0** | roadmap edit pending |
| C | Re-cut M1 → M1a (replay substrate, `temp=0`, frozen tool data) + M1b (generator); fix the cache reference | Jeff (J1/J2) | Demis on determinism | **P0** | roadmap edit pending |
| D | Add `outcome_source`; never promote calibration on backtest-only outcomes | Demis (D2) | Jeff (cheap enum) | **P1** | folds into M4/M5 |
| E | Add a turnover-matched random baseline to the M3 gauntlet | Demis (D3) | consensus | **P1** | roadmap edit pending |
| F | Add an operating layer (owners, WIP=1 in H1, stop-list) to the roadmap | Sundar (S3) | consensus | **P1** | roadmap edit pending |

**Carry-forward from 2026-06-01 (still open, re-affirmed):** Decision C-prior
(the backtest itself), D-prior (cost-per-decision on `/metrics`), E-prior
(freeze `/analyze`), F-prior (embedding key before learning A/Bs). These remain
correct; this run did not re-litigate them — it made the M1 they all wait on
*executable*.

## 6. The single thread through all of it

Yesterday the three reviewers only stopped arguing at one artifact: a cheap,
deterministic, real-LLM walk-forward equity curve vs. baselines. **That is still
the only thing that matters — and a day later it has not been started.** Today's
job was not to re-decide it but to remove what stands between the team and
shipping it: (1) clear the one trust defect that was bleeding credibility while
un-shipped (**done — Decision A**), and (2) make M1 *actually buildable* — split
it from its hidden substrate (C), give it a bar that produces evidence rather
than an anecdote (B), close the look-ahead hole the `asof` guard misses (C), and
cap WIP to one so the next 24h produce **code, not another document** (F).

The product doesn't need more vision. It needs M1a to exist by the next run.

---

*Generated autonomously by the `daily-executive-critique` scheduled task on
2026-06-02. Decision A's code fix was applied in this run (reversible via git);
the remaining fixes are roadmap edits left for the operator. Calls made without
the operator present are flagged **[chair]**.*
