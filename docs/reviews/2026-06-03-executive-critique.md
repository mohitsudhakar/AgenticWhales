# Executive Critique — 2026-06-03

> **Format.** A threefold review of the *entire* AgenticWhales product, run as a
> debate between three reviewer lenses — **Sundar Pichai** (product /
> distribution / trust), **Demis Hassabis** (research rigor / measurable edge),
> **Jeff Dean** (systems / scale / cost). Each raises points, they *disagree with
> each other*, and every open thread is driven to a **decision + concrete fix**.
> Grounded in the code on `roadmap-north-star` today.
>
> **This is the third run.** [2026-06-01](2026-06-01-executive-critique.md) set a
> decision register; [2026-06-02](2026-06-02-executive-critique.md) split M1 and
> applied a trust fix to the working tree. This run holds *both* accountable —
> and surfaces the single fact that reframes the whole project's risk: **the
> trust fix marked "DONE" yesterday was never committed, so production is *still*
> serving the defect 48 hours after it was first decided.** Calls made without
> the operator present are flagged **[chair]** and are reversible.

---

## 0. The state in one paragraph: the loop isn't doc-vs-code, it's *applied-vs-shipped*

Yesterday's critique diagnosed a "doc-to-ship" gap and felt it had broken it by
*applying* the P0 trust fix in-run. It had not. Twenty-four hours later the
scoreboard is worse than "nothing shipped" — it's "the one thing we called done
is one `git checkout` from gone":

| Decided | When | Marked | Actual state today | Evidence |
|---|---|---|---|---|
| **A** — kill the `100+` floor / `2×` multiplier | 06-02 | **"DONE this run"** | **Uncommitted in a dirty working tree; never committed, never merged** | `git diff` shows the fix only in the working tree; **`main` still has `DISPLAY_FLOOR = 100`, `DOUBLE_THRESHOLD = 50`, and hardcoded `100+`** ([waitlist.py:138](../../web/waitlist.py), [welcome.html:311](../../web/static/welcome.html) on `main`). The deployed `agenticwhales` app (fly, `sjc`) is **still fabricating social proof on a finance funnel.** |
| **B** — split M1's acceptance bar (smoke + evidence) | 06-02 | "roadmap edit pending" | **Not applied** (until this run) | ROADMAP.md last touched 06-01; M1 still read "≥1 ticker over ≥1 year." |
| **C** — re-cut M1 → M1a/M1b; fix cache ref | 06-02 | "roadmap edit pending" | **Not applied** (until this run) | M1 still cited `auth.find_cached_session`; no M1a/M1b. |
| **E** — turnover-matched random baseline | 06-02 | "roadmap edit pending" | **Not applied** (until this run) | M3 gauntlet had four baselines, not five. |
| **F** — operating layer (WIP=1, stop-list) | 06-02 | "roadmap edit pending" | **Not applied** (until this run) | No "now/next/stop" block in the roadmap. |
| **M1** — the real-LLM backtest itself | 06-01 | P0, "start here" | **Not started (day 3)** | [backtest.py:16](../../agenticwhales/backtest.py) still says live mode is *"deferred"*; `momentum_stub_generator` is still the only generator; no `backtest_generator.py`. |

**The through-line.** The previous two critiques produced *decisions* and even
*working-tree edits* — and then the session ended and the edits evaporated into
an uncommitted diff that the next run can't see was meant to ship. The bottleneck
was never "we write docs instead of code." It's that **work that lives only in a
working tree is not work that shipped.** A critique that "applies a fix" but
doesn't commit it has produced the *appearance* of progress and none of the
substance — which is strictly worse than producing nothing, because it lets the
register read "DONE" while production serves the defect. This run therefore does
two things: (1) **applies the four ratified-but-unmade roadmap edits** (B, C, E,
F) so they stop being "pending" forever, and (2) makes "**committed and merged to
`main`**" the explicit, written definition of *shipped* — and names the operator
action that closes A for real.

---

## 1. Sundar — product, distribution, trust

**S1. The headline: production is still lying to strangers about a finance
product, 48h after we agreed to stop.** This is no longer a copy problem; it's a
*deployment* problem. The fix exists, is correct, and is tested — and none of
that matters because it sits in an uncommitted working tree on a feature branch
that is **one documentation commit ahead of `main`**. The deployed app still
shows `100+` and doubles the count. The lesson compounds yesterday's: "decisions
without an owner who ships them are theater" → **"fixes without a commit are a
rehearsal of theater."**

**S2. The `/analyze` freeze was decided twice and bannered zero times.** Decision
E-prior (06-01) said freeze `/analyze` and banner it as a legacy surface. There
is still **no "feature-frozen" banner anywhere in the HTML** (grep finds none).
New users land on a surface with the *full model picker* — the most
configuration-heavy screen in the product — implying it's the maintained one,
while `/fund` is the actual product. Every day this is unbannered, we train users
on the wrong surface.

**S3. We are accumulating a register of "done" claims we can't trust.** If A was
"DONE" but production serves the defect, the value of the register itself is now
in question. A decision log whose statuses don't correspond to reality is a
liability — it manufactures false confidence. The fix is mechanical (a status can
only be "done" when merged) but the credibility cost is real.

## 2. Demis — research rigor, measurable edge

**D1. The backtest harness bakes in the *exact weak label* the North Star
condemns.** [backtest.py:19](../../agenticwhales/backtest.py) resolves outcomes as
"mark realized PnL on stop-loss hit or hold-days expiry" — i.e. sign-of-PnL. That
is precisely the "hit = positive PnL" weakness flagged as **G3** in
[NORTH_STAR.md:99](../../NORTH_STAR.md) and slated for repair only at M5. But M1
is the harness that's supposed to *produce the edge proof* — if it measures edge
with a benchmark-blind label, the go/no-go in M3 inherits the flaw. M1b's
*evidence* output must be **benchmark-relative (alpha vs. SPY, vol-scaled) from
day one**, not retrofitted at M5.

**D2. Determinism is still entirely theoretical because M1a doesn't exist.** The
harness wraps `as_of_date` around a *pre-loaded DataFrame*
([backtest.py:9](../../agenticwhales/backtest.py)) — which protects the *stub*
generator, whose only input is that frame. But the moment a real LLM generator
makes a tool call (news, prices, embeddings), that call **bypasses the wrapper
entirely** and reads live, post-as-of data. The guard checks the *requested*
date, not the *content's* date. Until M1a freezes *tool outputs*, every "backtest"
number is contaminated by look-ahead and is not evidence. This is why M1a, not
M1b, is the real "start here."

**D3. Calibration provenance (D-prior) is still unaddressed and the clock is
ticking the wrong way.** With no `outcome_source` label, the moment M1b produces
backtest outcomes they become indistinguishable from paper/live ones in
`decision_outcomes`, and the Platt fitter will silently train on simulated
fills. The cheap enum must land *before* M1b writes its first outcome row, not
after — otherwise we contaminate the calibration store and can't cleanly separate
it later.

## 3. Jeff Dean — systems, scale, cost

**J1. M1's evidence run has no cost ceiling and will collide with the global cost
cap.** [backtest.py](../../agenticwhales/backtest.py) has **zero cost guard**
(grep confirms: the only "cap" is `kelly_cap`, a sizing param). The evidence
panel Demis wants — ≥20 names × ~3y daily × ~7 agents × 2 providers — is on the
order of **10⁵ LLM calls**. The cost middleware enforces a *global daily/monthly*
cap; a multi-year run will therefore either (a) trip that cap mid-run and produce
a **non-deterministic partial curve** (the run dies at a different date depending
on what else spent budget that day), or (b) get silently throttled. M1b needs a
**per-backtest budget envelope** distinct from the global cap, plus a **dry-run
cost estimate emitted before the run starts**. You cannot reproduce a curve whose
length depends on the day's remaining budget.

**J2. The uncommitted working tree is itself the systems bug.** The reason A
"shipped" yesterday and is gone today is a process with no durability: each
scheduled session ends with a dirty tree, and the next one starts from it with no
signal that those edits were meant to be permanent. One `git stash`, one
`checkout`, one branch switch, and the only trust fix in the repo is erased with
no trace in history. **Work that isn't committed didn't happen** — for a system
that runs autonomously across sessions, an uncommitted edit is not "applied," it's
"queued for deletion." Every "fix applied in this run" the critiques produce has
this defect.

**J3. Branch topology hides the problem.** `roadmap-north-star` is exactly **one
commit ahead of `main`** (the roadmap doc) — so to a glance, "we have a branch
with work on it." But the *trust fix isn't in that one commit*; it's in the
uncommitted delta. The branch looks like progress and contains, in committed
form, only a planning document. This is the doc-to-ship gap made literal in the
git graph.

---

## 4. The debates, one by one — and where they land

### Debate A — "DONE" that wasn't: commit-and-merge as the only definition of done (S1/S3 + J2, consensus)

- **Sundar:** A was correct and tested yesterday. The failure is that it never
  left the working tree, so production still serves the defect. The fix is to
  **commit it and merge to `main`** — and to redefine "done" so this can't recur.
- **Jeff:** The deeper bug is durability: an autonomous loop that applies edits
  but never commits is guaranteed to lose them. "Done" must mean *merged*, full
  stop. A working-tree edit is not a deliverable.
- **Demis:** No dissent — and the same rule protects the science: an
  uncommitted, unreproducible backtest is worthless for the same reason an
  uncommitted fix is.

> **Decision A [chair] — applied + escalated.** (1) Made the definition of
> *shipped* explicit in the roadmap operating layer: a milestone is done only
> when **committed and merged to `main`**, never when it merely exists in a
> working tree. (2) **Operator action required (cannot be auto-shipped):** commit
> the staged waitlist trust fix and merge `roadmap-north-star` → `main`, then
> deploy — *this is the only thing that removes the fabricated `100+` from a live
> finance funnel.* One line:
> `git commit -am "fix(waitlist): show true count, no fabricated floor" && git checkout main && git merge roadmap-north-star`.
> I did **not** auto-commit/merge/deploy: that is an outward, irreversible action
> the task did not authorize. But the register may **no longer mark A "done"** —
> its honest status is **"fix written, awaiting commit + merge."**

### Debate B — The four ratified roadmap edits that were never made (B/C/E/F from 06-02)

- **Sundar:** These were *unanimously decided* yesterday with the exact fix
  spelled out. They sat as "pending" for 24h and would sit forever. Apply them
  now, in this run — they're reversible roadmap text.
- **Jeff:** Agreed; M1a (the substrate) is the one that unblocks everything and
  the one the original roadmap didn't even name. The split must land.
- **Demis:** And the evidence bar (panel + dispersion) and the turnover-matched
  baseline are non-negotiable for the result to mean anything.

> **Decision B [chair] — DONE in this run.** Applied all four ratified edits to
> [ROADMAP.md](../../ROADMAP.md): (i) **operating layer** — "Now = M1a (WIP 1)",
> next M1b→M2→M3, an explicit **stop-list** (no portfolio/data/coach/exec until
> M3 verdicts), and the committed-and-merged definition of done; (ii) **M1 re-cut
> into M1a** (content-addressed replay store + frozen tool-data snapshots,
> `temp=0`; cache reference corrected away from `auth.find_cached_session`) **and
> M1b** (the generator, with smoke + evidence bars); (iii) **M3 gauntlet** now
> includes the **cost-and-turnover-matched random sizer**; (iv) critical-path
> diagram + next-action footer updated to M1a→M1b. Reversible via git.

### Debate C — The harness measures the wrong thing (D1, with Jeff on cost)

- **Demis:** M1b is the proof engine, yet it labels outcomes by sign-of-PnL — the
  G3 weakness. Its evidence output must be benchmark-relative from day one or M3's
  verdict is built on a contaminated label.
- **Jeff:** Cheap to do at curve-construction time — we already compute the
  equity curve; subtracting a SPY/benchmark curve is one more series, not a new
  subsystem. Do it in M1b, not M5.
- **Sundar:** And the *chart M3 ships* should be the benchmark-relative one —
  "beat the market net of cost," not "made money," is the claim a skeptic
  respects.

> **Decision C.** **M1b's evidence output must be benchmark-relative (alpha vs.
> SPY, vol-scaled), not sign-of-PnL.** Folded into M1b's evidence-bar wording in
> the roadmap this run; the *label change* in
> [outcomes.py](../../agenticwhales/outcomes.py) / `decision_outcomes` is still
> M5's deliverable, but M1b must compute its curve relative to a benchmark
> regardless of when the stored label catches up. *Owner: brain.*

### Debate D — `outcome_source` must land before M1b writes a row (D3, with Jeff)

- **Demis:** The instant M1b produces backtest outcomes, they pollute the
  calibration store unless tagged. The enum must exist *first*.
- **Jeff:** It's a one-column migration + a filter in the fitter. Sequence it as a
  prerequisite of M1b, not a follow-on.
- **Sundar:** And the dashboard labels which curve is backtest vs. live, or we
  fool ourselves.

> **Decision D.** **Promote `outcome_source` (`backtest|paper|live`) to a
> prerequisite of M1b**, not an M4/M5 follow-on. **Fix:** add the column +
> fitter-filter ([outcomes.py](../../agenticwhales/outcomes.py),
> `calibration.apply_if_opted_in`) before M1b's first run; calibration *promotion*
> excludes `backtest` rows. *Owner: brain.* Carried from 06-02 Decision D, now
> re-sequenced earlier. *(Roadmap note left for operator; not auto-edited into the
> milestone bodies to avoid over-restructuring M4/M5 unattended.)*

### Debate E — A per-backtest cost envelope (J1, consensus)

- **Jeff:** No cost ceiling in the harness + a global daily cap = a backtest whose
  length depends on the day's spend. That's non-deterministic by construction.
  Add a per-run budget and a pre-run estimate.
- **Demis:** A curve that dies at a budget-dependent date is not reproducible and
  therefore not evidence — this is a rigor requirement, not just an ops nicety.
- **Sundar:** Also a cost-discipline story for the funnel later ("$ per decision
  vs. edge" is on the scoreboard).

> **Decision E.** **M1b requires a per-backtest budget envelope + a dry-run cost
> estimate, separate from the global cost cap.** Added to M1b's body in the
> roadmap this run ("Cost envelope (required before the evidence run)"). *Owner:
> systems.* **Cheap, and gates the evidence run.**

---

## 5. Decision register (who said what → what we do)

| # | Decision | Raised by | Resolved against | Priority | Status |
|---|---|---|---|---|---|
| A | "Done" = committed + merged; the 06-02 trust fix is **not done** until merged to `main` (operator action) | Sundar (S1/S3), Jeff (J2) | consensus | **P0** | **Definition shipped; the fix awaits operator commit+merge** |
| B | Apply the 4 ratified-but-unmade roadmap edits (M1a/M1b split, evidence bar, turnover baseline, operating layer) | the 06-02 register | consensus | **P0** | **DONE this run** (ROADMAP.md edited) |
| C | M1b's evidence output must be benchmark-relative, not sign-of-PnL | Demis (D1) | Jeff (cheap), Sundar | **P1** | folded into M1b wording; outcomes label = M5 |
| D | `outcome_source` enum is a **prerequisite of M1b**, not an M4/M5 follow-on | Demis (D3) | Jeff (cheap migration) | **P1** | re-sequenced; note left for operator |
| E | Per-backtest budget envelope + pre-run cost estimate, distinct from the global cap | Jeff (J1) | consensus | **P1** | **DONE this run** (added to M1b body) |
| — | Banner `/analyze` as feature-frozen (E-prior, 06-01) | Sundar (S2) | consensus | **P1** | **still un-done; 2 days open** |

**Carry-forward, re-affirmed:** the M1 backtest (06-01 C-prior) remains the only
thing that matters and is **not started on day 3**. Cost-per-decision on
`/metrics` (D-prior) and the embedding key before learning A/Bs (F-prior) remain
correct and untouched.

## 6. The single thread through all of it

Three runs in, the reviewers still agree on one artifact — a cheap,
deterministic, look-ahead-free, real-LLM walk-forward equity curve vs. baselines
— and it still does not exist. But the *new* lesson of this run is sharper than
"we haven't built it." It's that **we have been mistaking applied edits for
shipped work**, and the proof is sitting in production right now: a finance
landing page fabricating `100+` signups two days after we unanimously agreed to
stop, because the fix was never committed. The most valuable thing this critique
can do is not generate a fourth strategy artifact. It's to (1) make
*committed-and-merged* the only definition of done, (2) finally apply the four
roadmap edits that were decided and then orphaned, and (3) put one git command in
front of the operator that removes the live trust defect.

The product doesn't need more decisions. It needs the decisions it already made
to reach `main`.

---

*Generated autonomously by the `daily-executive-critique` scheduled task on
2026-06-03. This run applied the four ratified roadmap edits from 2026-06-02
(Decisions B/C/E/F) to [ROADMAP.md](../../ROADMAP.md) and corrected the register's
false "DONE" on the trust fix. It did **not** commit, merge, or deploy — those are
outward actions the operator must take (see Decision A). Calls made without the
operator present are flagged **[chair]** and are reversible via git.*
