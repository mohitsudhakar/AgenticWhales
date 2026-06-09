# Executive critique — threefold panel (2026-06-08)

> **Format.** Three reviewers — **Sundar Pichai** (product, focus, distribution,
> trust), **Demis Hassabis** (scientific rigor, evaluation, what "intelligence"
> means here), **Jeff Dean** (systems, engineering, simplicity, scale) — critique
> the *whole* repo as it stands today. For each point I (the facilitator) record
> their claim, **debate it** (push back where they overreach, concede where
> they're right), and land a **decision + concrete fix** with the file/seam it
> touches. A consolidated decision log and a prioritized action list close the doc.
>
> **The fact that frames everything.** The repo's canonical docs
> ([README.md](../../README.md), [NORTH_STAR.md](../../NORTH_STAR.md),
> [ROADMAP.md](../../ROADMAP.md)) still declare the **autonomous fund** "the
> product" and explicitly forbid building the coach until milestone **M1 proves the
> LLM debate has an edge**. But the edge was *empirically killed* — the look-ahead-proof
> edge probe ([2026-06-07-edge-probe-findings.md](2026-06-07-edge-probe-findings.md))
> returned **KILL**: the debate loses to a random coin and to buy-and-hold in both
> regimes. And the *entire recent commit history* (the last ~15 commits) is the
> **coach** — SnapTrade, Plaid, OCR, upload progress, continuous timeline,
> auth-aware journey. **The code pivoted; the canonical docs did not.** This
> divergence is the spine of every reviewer's strongest point.

---

## Reviewer 1 — Sundar Pichai (product · focus · distribution · trust)

### S1. "Your repo can't answer 'what is this product?' — and that's a kill-shot for focus."
**Claim.** README opens with `/fund` as "the product" and `/analyze` as "legacy."
The coach — the thing you've shipped every commit for two weeks — **isn't even
mentioned in the README**, and the North Star still orders the whole company
around proving fund edge that the probe already disproved. A new engineer, an
investor, or a future-you reading this repo gets a coherent story about the wrong
product. Pick one. The coach is the product; the fund is a research lab. Say so,
everywhere, today.

**Debate.** I push back on the implied "kill the fund." The fund work is not dead
weight — two assets survived the probe: (1) the *analysis* surfacing (the probe
found the debate's theses correctly identify regimes; it's the autonomous
*decision policy* that's timid and edgeless), and (2) the non-predictive
trend-overlay ([2026-06-08-trend-test.md](2026-06-08-trend-test.md)) which is a
legitimate, if period-favorable, risk-premia product. Sundar concedes: the fund
becomes the **lab that feeds the coach** (surface the analysis to a human; offer
the trend-overlay as a defensive allocation), not a deprecated branch. But the
naming and the docs must stop lying.

**Decision (Sundar, conceded by facilitator).** Reframe the product surface:
**coach = the product, fund = the lab.** Fix the docs first — they are the cheapest,
highest-leverage artifact and they are currently actively misleading.

**Fix.**
- Rewrite [README.md](../../README.md) §1: lead with the coach (`/coach`), describe
  `/fund` and `/analyze` as the research lab the coach's analysis is drawn from.
- Add a short `PRODUCT.md` (or repoint [NORTH_STAR.md](../../NORTH_STAR.md)) that
  states the post-probe thesis in one paragraph: *"We sell discipline, not alpha."*
  NORTH_STAR §6 already says this in passing — promote it to the headline.
- Mark [ROADMAP.md](../../ROADMAP.md) M1–M3 (the prove-the-edge gate) as **resolved:
  KILL** with a pointer to the probe memo, so nobody re-litigates the dead milestone.

### S2. "You are one screenshot away from being an unlicensed investment adviser."
**Claim.** A consumer app that ingests a user's real brokerage history (via
SnapTrade/Plaid), tells them "this bias cost you $4,200," and runs a *pre-trade
decision-support check* is walking straight at the line between "education" and
"investment advice / financial recommendation." On top of that you're holding
brokerage-linked data. There is no visible disclaimer posture, no data-handling
statement, no "not advice" framing in the product copy.

**Debate.** I partly resist: the design law in [coach.py](../../agenticwhales/coach.py)
("dollar figures are deterministic and defensible; the LLM narrates, never
invents the numbers") is *exactly* the right defensive posture — a backward-looking,
arithmetic counterfactual on the user's own trades is much closer to "your bank
statement" than to "buy this stock." Sundar holds firm on the forward-looking
piece: the **pre-trade check** ([pretrade.py](../../agenticwhales/pretrade.py),
[decision_support.py](../../agenticwhales/decision_support.py)) is forward-looking
and is where advice-liability lives. We agree to split the two: retrospective
coaching is low-risk and should be the marketed wedge; the pre-trade check must be
explicitly framed as a *discipline checklist*, not a recommendation, and must never
emit a buy/sell directive.

**Decision (Sundar).** Adopt a two-tier framing: retrospective coach = the
product's front door; pre-trade check = "discipline checklist," advice-disclaimed.
Ship the compliance copy and a data-handling statement *before* any public launch.

**Fix.**
- Add a persistent "Educational, not investment advice" disclaimer to the coach UI
  ([web/static](../../web/static)) and a one-line data-handling note ("read-only;
  we never place orders; brokerage tokens are stored encrypted / not at all").
- Audit [pretrade.py](../../agenticwhales/pretrade.py) output to confirm it returns
  *process* guidance (sizing/risk/rule checks) and never a directional
  recommendation; add a test asserting no buy/sell directive leaks into the
  user-facing string.
- Document the SnapTrade/Plaid token lifecycle in
  [docs/notes/snaptrade-integration.md](../notes/snaptrade-integration.md) (storage,
  scope = read-only, revocation) — it's half-there; finish the data-posture section.

### S3. "What is the activation moment, and can the user share it?"
**Claim.** The wedge is "connect your broker → see in dollars what your trading
habits cost you." That's a strong aha. But there's no defined activation metric and
no shareable artifact. Consumer products live or die on the first-session "whoa" and
its shareability. Guest mode exists (good — `optional_user_id` lets people try
before signing in), but what's the funnel and what's the loop?

**Debate.** No real disagreement — this is a gap, not a flaw. I narrow it: don't
build growth machinery now; define the *one* activation event and instrument it.

**Decision (Sundar).** Define activation = "user uploads/links history and views
their first quantified leak card." Instrument it; make the leak card a clean,
screenshot-friendly artifact (it already renders as insight cards per the commit
history).

**Fix.**
- Add an analytics event at the point the first insight card renders
  ([web/coach_api.py](../../web/coach_api.py) results path) and surface it in
  [web/admin.py](../../web/admin.py).
- Make the insight card export-as-image friendly (anonymized) for organic sharing.

---

## Reviewer 2 — Demis Hassabis (rigor · evaluation · "intelligence")

### D1. "The edge probe is the best thing in this repo. Now don't repeat the sin you just diagnosed."
**Claim.** Genuine credit: the edge probe is exemplary applied science — a *cheap
tripwire* that kills the existential thesis early, with look-ahead made
*structurally* impossible (pure function of an as-of-bounded slice), an honest
forced-commit ablation that isolates RLHF timidity, and a written KILL verdict.
That is how you do this. **But** the coach now makes its own central empirical
claim — *"this bias cost you $X, and this rule fixes it"* — and that claim is
**unvalidated**. The counterfactual is deterministic (good), but a deterministic
computation of *the wrong counterfactual* is still wrong. The real question is
forward-looking and untested: **do users who adopt the flagged rule actually
improve their forward P&L / behavior?** You killed the fund for asserting edge
without measuring it; do not now ship a coach that asserts impact without measuring it.

**Debate.** I push back on feasibility: a forward holdout on real users needs users
and months. Demis concedes the timeline but not the principle — the *design* must
make the claim falsifiable from day one. Land on: instrument the prediction now so
the validation is possible later, and in the meantime label the dollar figures as
*historical attribution* ("this is what it cost you," past tense, defensible), not
*causal promise* ("fixing this will make you $X," which is the unvalidated leap).

**Decision (Demis).** The coach's dollar figures stay framed as **historical
attribution** (defensible arithmetic) until a forward study exists. Build the
measurement seam now: log each flagged leak + the rule, so a later cohort analysis
can test "did followers improve?"

**Fix.**
- In [coach.py](../../agenticwhales/coach.py), persist each emitted finding with a
  stable `leak_id`, the dollar attribution, and the prescribed rule (a
  `coach_findings` table — extend [docs/migrations](../migrations)).
- Add a forward-outcome resolver analogous to
  [outcomes.py](../../agenticwhales/outcomes.py): once a user has new trades after a
  finding, recompute whether the leaked behavior persisted — the seed of a real
  effectiveness metric. Keep present copy in the *past tense*.

### D2. "The trend overlay is risk-premia cosplaying as a result until you walk it forward."
**Claim.** The trend test is *honestly* captioned ("risk-premia harvesting, not
alpha/skill," "period-favorable," "~in-sample"). Credit for the honesty. But it is
one parameter set (12m/15% vol), one basket, on a window (2018–2026) that contains
*trend-following's two best crashes*. Trend-following had a brutal drought 2010–2019.
Until you walk it forward across that drought, "crash defense" is a marketing claim,
not a finding — and a consumer who allocates to it on the strength of this table is
being sold period-luck.

**Debate.** None — this is already listed as caveat + next-step #1 in the memo. I
elevate it from "next step" to **gate**: the trend overlay may not be *marketed* as
crash defense until the walk-forward exists.

**Decision (Demis).** Gate any user-facing "crash defense" framing of the trend
overlay on the 2007–2026 walk-forward (incl. the 2010–19 drought).

**Fix.**
- Run the walk-forward in [strategy_lab.py](../../agenticwhales/strategy_lab.py) /
  [tools/run_trend_test.py](../../tools/run_trend_test.py) across 2007–2026; commit
  the report to [docs/reviews](.). Until then, the overlay is "experimental" in any UI.

### D3. "Your evaluation rigor (Brier/calibration) is now orphaned. Repurpose it; don't waste it."
**Claim.** The calibration / Brier / "knows-what-it-knows" machinery
([calibration.py](../../agenticwhales/calibration.py),
[outcomes.py](../../agenticwhales/outcomes.py)) was the fund's conscience. With the
fund deprioritized it's pointed at nothing. But the *coach* makes probabilistic-ish
claims too (this bias is *likely* hurting you). The discipline of calibrating a
stated confidence against realized outcomes is exactly what should sit on top of the
coach's findings. Move the conscience to where the product now lives.

**Debate.** Jeff (below) will argue this machinery is dead weight to delete. I hold
the tension open and resolve it jointly: the *harness* (outcome resolution, Brier
scoring, reliability curves) is reusable IP; the *fund-specific wiring* (PM scalar
calibration, prompt-eval canary) is archivable. Demis agrees: repurpose the harness
onto coach findings, archive the rest.

**Decision (Demis, reconciled with Jeff J1).** Keep the eval *harness* and retarget
it at the coach; quarantine the fund-specific learning-loop wiring (see J1).

**Fix.**
- Generalize the resolver in [outcomes.py](../../agenticwhales/outcomes.py) so its
  Brier/reliability scoring can score *any* dated prediction, then feed it the coach
  findings from D1.

---

## Reviewer 3 — Jeff Dean (systems · engineering · simplicity · scale)

### J1. "~1,650 lines of fund learning-loop machinery are now semi-orphaned. Carrying cost is real."
**Claim.** [ablation.py](../../agenticwhales/ablation.py),
[adaptive.py](../../agenticwhales/adaptive.py),
[conviction_decay.py](../../agenticwhales/conviction_decay.py),
[heterogeneity.py](../../agenticwhales/heterogeneity.py),
[disagreement.py](../../agenticwhales/disagreement.py),
[memory_v2.py](../../agenticwhales/memory_v2.py),
[calibration.py](../../agenticwhales/calibration.py) — ~1.65k LOC of sophisticated
machinery built to make the fund learn. The fund's edge is dead. This code now sits
in the main package looking like product, carrying test burden and reader-confusion
tax, and it will rot silently. Either it earns its place or it moves out of the way.

**Debate.** I push back against deletion — per D3 some of it (the eval harness) is
reusable, and the analysis surfacing still feeds the coach. Jeff narrows: he doesn't
want it *deleted*, he wants it **honestly labeled and quarantined** so it stops
masquerading as load-bearing product. Agreed.

**Decision (Jeff, reconciled with Demis D3).** Quarantine fund-era machinery under a
clear boundary (e.g. an `agenticwhales/lab/` namespace or explicit
module-docstring "EXPERIMENTAL — fund lab, not in the coach path" headers + a
section in [ARCHITECTURE.md](../../ARCHITECTURE.md)). Keep what D3 repurposes; mark
the rest experimental.

**Fix.**
- Add a "fund lab vs. coach product" boundary to
  [ARCHITECTURE.md](../../ARCHITECTURE.md) and a top-of-file status banner to each of
  the modules above; do not silently leave them implying they're product.

### J2. "The monolith is the right call — but the coach's heavy path can starve the streaming worker."
**Claim.** One uvicorn process runs HTTP + the APScheduler + the Alpaca streaming
worker + nightly crons + the outcome resolver + the coach's async upload jobs + SSE.
Leader-elected by a Postgres advisory lock. That's clever and cheap and **correct for
this stage** — I'm *not* asking for microservices. But the coach now does parallel
transaction extraction + OCR on uploaded PDFs (per commit `b8806be`) on that same
event loop. A CPU-bound OCR/extract job can block the loop and starve the leader-only
WS pump and the SSE heartbeats. That's a latency footgun hiding in a clean design.

**Debate.** I confirm the monolith stays — splitting it now would be premature. Jeff
agrees and narrows the ask to one concrete guardrail: get CPU-bound work off the loop
and bound the coach's concurrency.

**Decision (Jeff).** Keep the monolith. Ensure CPU-bound coach work (OCR, heavy
parse) runs in a thread/process pool with a bounded concurrency, and document the
"don't block the leader loop" rule.

**Fix.**
- In [web/coach_api.py](../../web/coach_api.py), confirm OCR/extract runs via
  `run_in_executor` / a bounded pool (not inline on the loop); cap concurrent
  extraction jobs. Add a note in [ARCHITECTURE.md](../../ARCHITECTURE.md) that the
  leader loop must never run unbounded CPU work.

### J3. "Credit: the as-of hole is closed. But it's never been proven end-to-end, and embeddings still cheat."
**Claim.** The edge-probe memo flagged a real correctness hole — the as-of guard was
"wired into nothing." That's now fixed: `as_of_date` enforces at the
`route_to_vendor()` chokepoint in
[dataflows/interface.py](../../agenticwhales/dataflows/interface.py), through which
all analyst data tools route. Good. **But** two things remain: (1) there is no
integration test proving a *graph-driven* run under `as_of_date` actually cannot
fetch future data — the guarantee is asserted, not tested at the graph level; and
(2) memory embeddings still default to a hashing trick when no embedding key is set
([memory_v2.py](../../agenticwhales/memory_v2.py)), which silently degrades any
retrieval quality claim.

**Debate.** None on (1) — a guard not covered by an end-to-end test is one refactor
away from silently breaking. On (2), since memory_v2 is being quarantined to the lab
(J1), I downgrade it: decide explicitly (real embeddings or honest "off"), don't
half-ship it.

**Decision (Jeff).** Add a graph-level as-of integration test. For embeddings: make
the hashing-trick fallback *loud* (log + flag) or disable retrieval when no real
embedder is configured — no silent degradation.

**Fix.**
- New test in [tests/integ](../../tests/integ): drive the graph under
  `as_of_date(past)` against a stub vendor that records requested dates; assert no
  request exceeds the as-of date.
- In [memory_v2.py](../../agenticwhales/memory_v2.py), surface the embedder mode and
  refuse to report retrieval quality on the hashing fallback.

---

## Consolidated decision log — who said what, what we landed

| # | Reviewer | Their point | Debate outcome | Landed decision | Primary seam |
|---|---|---|---|---|---|
| S1 | Sundar | Repo can't say what the product is; docs describe the dead fund | Conceded fund→lab, not kill | Coach = product, fund = lab; fix docs first | README, NORTH_STAR, ROADMAP |
| S2 | Sundar | One step from "investment advice"; no disclaimer/data posture | Split: retrospective low-risk, pre-trade is the liability | Two-tier framing + disclaimers + data statement | web/static, pretrade.py, snaptrade notes |
| S3 | Sundar | No activation metric / shareable moment | Narrowed: instrument one event, don't build growth machinery | Activation = first leak card viewed; instrument it | coach_api.py, admin.py |
| D1 | Demis | Coach asserts impact without measuring — the fund's sin again | Conceded timeline, held principle | Dollar figures = historical attribution; build forward-validation seam | coach.py, outcomes.py, migrations |
| D2 | Demis | Trend overlay is period-favorable risk-premia, not a result | Agreed; elevated next-step to a gate | No "crash defense" marketing until 2007–2026 walk-forward | strategy_lab.py, run_trend_test.py |
| D3 | Demis | Eval/Brier rigor is orphaned — repurpose it | Reconciled with J1: keep harness, archive fund wiring | Retarget the eval harness at coach findings | outcomes.py, calibration.py |
| J1 | Jeff | ~1.65k LOC fund machinery is semi-orphaned, rotting | Not delete — quarantine + honest labels | `lab/` boundary + EXPERIMENTAL banners | ARCHITECTURE.md, the 7 modules |
| J2 | Jeff | Monolith is right, but coach OCR can starve the leader loop | Keep monolith; bound CPU work | CPU-bound work off-loop + bounded concurrency | coach_api.py, ARCHITECTURE.md |
| J3 | Jeff | As-of hole closed (credit); untested end-to-end; embeddings cheat | None on test; downgrade embeddings to explicit | Graph-level as-of test; loud embedder fallback | tests/integ, memory_v2.py |

## Where reviewers disagreed (and the resolution)
- **Demis (D3) vs. Jeff (J1)** on the learning-loop machinery: Demis wanted to
  *repurpose* it, Jeff wanted it *out of the product path*. **Resolution:** split the
  asset — keep the **eval harness** (resolver + Brier + reliability) and retarget it
  at coach findings (D1/D3); **quarantine** the fund-specific wiring (PM-scalar
  calibration, prompt-eval canary, conviction decay, disagreement) under an
  EXPERIMENTAL lab boundary (J1). Both satisfied.
- **Sundar (S1)** leaned toward "the fund is dead," but the facilitator + Demis held
  that the *analysis surfacing* and the *trend overlay* survived the probe. **Resolution:**
  fund becomes the lab that feeds the coach, not a deprecated branch.

## Prioritized action list (highest leverage first)
1. **Fix the docs (S1).** Cheapest, highest leverage; the repo currently tells a
   coherent story about the wrong product. README + NORTH_STAR headline + mark M1–M3
   KILL. *Half a day.*
2. **Disclaimer + data-handling posture (S2).** Gating for any public exposure of a
   consumer fintech that touches brokerage data. *1–2 days.*
3. **Coach findings persistence + forward-validation seam (D1).** Makes the product's
   central claim falsifiable; reuses the eval harness (D3). *2–3 days.*
4. **Quarantine fund machinery + boundary doc (J1/D3).** Stops the rot and the reader
   confusion; clarifies what's product. *1 day.*
5. **Bound coach CPU concurrency off the leader loop (J2).** Prevents a latency
   footgun in the live worker. *Half a day.*
6. **Graph-level as-of integration test (J3).** Locks in the correctness fix so it
   can't silently regress. *Half a day.*
7. **Trend walk-forward 2007–2026 (D2).** Gate before any "crash defense" claim. *1–2 days.*

## The one-line verdict (all three concur)
> The engineering is strong, the edge probe is exemplary science, and the pivot to
> the coach is the *right* product call. The single biggest liability is that **the
> repo's own canonical story still describes the product you killed** — fix the
> narrative, make the coach's impact claim falsifiable, and quarantine the fund lab
> so it stops masquerading as product. Build proof for the claim you're now actually
> making.

---

*Generated by the daily executive-critique scheduled task. Reviewer voices are
simulated personas used as critique lenses; decisions are recommendations for the
operator, not actions taken. No code was modified by this run.*
