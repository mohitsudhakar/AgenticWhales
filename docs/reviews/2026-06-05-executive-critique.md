# Executive Critique — 2026-06-05

> **Format.** A threefold review of the *entire* AgenticWhales product, run as a
> debate between three reviewer lenses — **Sundar Pichai** (product /
> distribution / trust), **Demis Hassabis** (research rigor / measurable edge),
> **Jeff Dean** (systems / scale / cost). Each raises points, they *disagree with
> each other*, and every open thread is driven to a **decision + concrete fix**.
> Grounded in the code on `roadmap-north-star` today.
>
> **This is the fourth run** ([06-01](2026-06-01-executive-critique.md),
> [06-02](2026-06-02-executive-critique.md),
> [06-03](2026-06-03-executive-critique.md); no run produced on 06-04). The
> through-line of the last three was a widening gap between *decided* and
> *shipped*. This run measures that gap with a ruler and reaches an
> uncomfortable conclusion about the critique process itself. Calls made without
> the operator present are flagged **[chair]** and are reversible via git.

---

## 0. The state in one paragraph: one inch of progress, and the loop is now eating itself

Yesterday's headline was "the trust fix is one `git checkout` from gone — it
lives only in an uncommitted working tree." That advice was **partially taken**:
the fix is now a real commit (`d6e0818 fix(waitlist): show true count`). That is
genuine, creditable progress — the needle moved. **But it moved one inch.** The
commit sits on `roadmap-north-star`, which is **four commits ahead of `main` and
unmerged**, and **`main` still contains `DISPLAY_FLOOR = 100` and
`DOUBLE_THRESHOLD = 50`** ([waitlist.py:138-151 on `main`](../../web/waitlist.py)).
By the project's *own* written definition of shipped — "committed **and merged to
`main`**" ([ROADMAP.md:31-34](../../ROADMAP.md)) — the trust fix is **still not
done.** Whatever deploys from `main` is still fabricating `100+` signups on a
finance funnel, 96 hours after the decision to stop.

And the wider scoreboard is worse than "slow." **The only commit since the 06-03
critique is `403038b chore(build): lock python-multipart in uv.lock`** — a
dependency-lock chore. **M1a — the single declared "Now," WIP=1, the gate to
everything — has zero lines of code on day 5** (no `agenticwhales/replay.py`, no
`backtest_generator.py`; confirmed by `ls`). In the same 96 hours, the
**critique process produced four review documents totalling ~63 KB.** That is the
fact this run must stare at: **the daily critique is now generating analysis
faster than the product generates code, and it has the exact defect it keeps
diagnosing in others — it produces artifacts that read like progress and ship
nothing.** This run therefore refuses to mint a fresh batch of strategy
decisions. It does three things only: (1) corrects the register to reality
(trust fix = committed, *not* merged, *not* shipped), (2) records two genuinely
*new*, code-grounded findings the prior runs missed, and (3) puts the critique
cadence itself on the table for a decision.

| Decided | First raised | Marked then | Actual state today | Evidence |
|---|---|---|---|---|
| Kill the `100+` floor / `2×` multiplier | 06-01 | "DONE" (06-02) | **Committed to branch; NOT merged; `main` still serves the floor** | `git show main:web/waitlist.py` → `DISPLAY_FLOOR = 100` |
| Banner `/analyze` as feature-frozen | 06-01 | "still open" (06-03) | **Still no banner anywhere in the HTML** (grep finds none) | `grep -rl "frozen" web/*.html` → empty |
| M1a — deterministic replay substrate | 06-02 | "Now, WIP=1" | **Zero code, day 5** | no `replay.py`, no `backtest_generator.py` |
| Merge `roadmap-north-star` → `main` | 06-03 | "operator action" | **Not done** | branch is 4 commits ahead of `main` |

---

## 1. Sundar — product, distribution, trust

**S1. We took half the advice, and half a trust fix is zero trust fix.** Credit
where due: the fix is now committed, not floating in a dirty tree. But a user
hitting the deployed site does not see the branch — they see `main`, and `main`
still shows `100+`. The decision register's honest entry is not "done" and not
even "awaiting merge as a formality"; it is **"the live product still lies, 96h
in."** The remaining operator action is a *two-command* merge. The cost of not
running it is measured in days of a finance landing page fabricating social proof.

**S2. The `/analyze` freeze banner is now four days unbuilt — this is the cheapest
open item in the entire project and it never ships.** A `<div class="banner">`
with eight words of copy. It has been "decided, consensus" since 06-01. Its
permanent openness is itself the diagnosis: if *this* can't ship in four days,
the bottleneck is not engineering difficulty — it's that nothing on the critique's
list has an owner who closes it.

**S3. The critique itself has become a distribution problem.** Four runs, four
docs, one chore committed. From a product lens, the `daily-executive-critique`
task is now a *feature that ships documentation* — and like any feature that
produces artifacts nobody acts on, it manufactures a false sense of motion. A
daily cadence on a repo that lands one commit per 48h means each run mostly
re-reads the previous run's unfulfilled decisions. **The product doesn't need a
fifth critique next to four unactioned ones; it needs the critique to either
drive a merge or get out of the way.**

## 2. Demis — research rigor, measurable edge

**D1. New finding: the look-ahead guard *silently truncates* instead of raising —
which means M1a cannot use it to *detect* leaks, only to *mask* them.**
`bounded_to_as_of` truncates any future-dated request down to the as-of date and
merely `log.debug`s it ([asof.py:106-108, 122-123, 134](../../agenticwhales/asof.py)).
For the *stub* backtest this is harmless. But M1a's entire acceptance bar is
"prove there is no look-ahead." A guard whose default behavior is to *quietly
repair* a look-ahead request will make a leaky generator **pass** — the curve
comes out clean-looking because the guard silently rewrote the bad call, not
because the generator was causal. M1a needs a **strict mode** (`raise` on any
future request, no truncation) for the replay/determinism test, distinct from the
lenient production default. Without it, "byte-identical replay" can be true *and*
the numbers still contaminated.

**D2. The harness still scores edge with the exact weak label the North Star
condemns — and now it's load-bearing.** [backtest.py:291-297](../../agenticwhales/backtest.py)
computes hit-rate and Brier off `realized_return_pct > 0` — sign-of-PnL, the G3
weakness. This was flagged 06-03 (Decision C) and the fix folded into M1b's
wording — but M1b doesn't exist, so the *only* backtest code in the repo still
measures "made money," not "beat the benchmark." The risk compounds: every day
M1b doesn't land, the stub's sign-of-PnL Brier is the only "rigor" number the
system can report, and it flatters beta as if it were skill.

**D3. Re-affirmed, not re-debated: `outcome_source` before M1b's first row.** No
change since 06-03 (Decision D) — correct, still un-built, still cheap, still a
prerequisite. Recording it as carry-forward, not re-litigating.

## 3. Jeff Dean — systems, scale, cost

**J1. New finding, concrete: the checkpoint store is per-ticker SQLite on a local
disk — on Fly's ephemeral filesystem, every deploy or crash wipes all run
state.** [checkpointer.py:21,33-35](../../agenticwhales/graph/checkpointer.py)
opens `sqlite3.connect(data_dir/<TICKER>.db)`. This is TASKS.md's J1 in the
abstract ("survive container death"), but the concrete failure is sharper than
the ticket: the "production-shaped" autonomy layer (scheduler + streaming
triggers, per North Star) writes its only durable state to a disk that **Fly
discards on every `fly deploy`.** So the very act of shipping the trust-fix merge
(S1) would *erase* any in-flight run checkpoints. And when M1b finally runs a
multi-year backtest, a single machine restart loses the whole resume state. The
SqliteSaver→PostgresSaver swap isn't a Horizon-2 nicety; it's a precondition for
M1b being *runnable* unattended at all.

**J2. The merge-to-`main` is itself a systems risk that nobody has sequenced
against J1.** Shipping S1 means a deploy; a deploy on the current checkpointer
means data loss. These two open items *interact*, and no one has noticed because
they live in different lists (the critique register vs. TASKS.md). The correct
order is: land J1 (Postgres checkpointer) **or** confirm no live runs are
in-flight, *then* merge+deploy S1. Merging blind risks trading a copy defect for
a state-loss incident.

**J3. The critique loop violates its own durability rule.** 06-03 established
"work that isn't committed didn't happen." By that exact standard, **a daily
critique that emits a doc and ends — leaving the working tree with only an
untracked `docs/reviews/*.md` and no code delta — has, every day, done the thing
it condemns.** The scheduled task spends real tokens and real wall-clock to
re-derive the same unactioned decisions. From a systems-cost view this is a
**busy-loop with no forward progress condition** — it should either have a
guard (don't re-run until the prior run's P0 merged) or a longer period.

---

## 4. The debates, one by one — and where they land

### Debate A — "Committed" is not "shipped": the trust fix is still live-defective (S1 + J2, with Jeff adding a sequencing hazard)

- **Sundar:** The fix is committed — real progress — but `main` still serves
  `100+`. The register must say *not shipped*. The remaining action is a
  two-command merge + deploy.
- **Jeff:** Don't merge blind. The current checkpointer loses all run state on
  deploy (J1). Sequence it: confirm no in-flight runs (or land Postgres first),
  *then* merge. A trust win that causes a state-loss incident is a bad trade.
- **Demis:** No dissent. An unmerged fix and an unreproducible backtest fail for
  the same reason — neither has reached the only state that counts.

> **Decision A [chair] — corrected + sequenced.** Register status for the trust
> fix is **"committed to branch; NOT merged; production still serves the
> defect."** It may *not* read "done." **Operator action (cannot be
> auto-shipped):** (1) verify no live runs are mid-flight *or* accept that
> checkpoints will reset; (2) `git checkout main && git merge roadmap-north-star`;
> (3) deploy. I did **not** auto-merge/deploy — outward, irreversible, and now
> additionally gated on the J1 data-loss risk. The two-command merge that removes
> a 96-hour-old live trust defect remains the single highest-value operator
> action in the repo.

### Debate B — M1a hasn't started on day 5; is "WIP=1, start here" working? (all three)

- **Demis:** M1a is the gate to every edge number. Five days, zero lines. The
  operating layer (WIP=1) was supposed to fix exactly this and hasn't.
- **Jeff:** WIP=1 limits *concurrent* work; it does nothing if *zero* work
  happens. The constraint we're missing isn't focus, it's an owner with hands on
  the keyboard between scheduled critiques. A critique can't write `replay.py`.
- **Sundar:** Then the most useful thing this run ships is not more M1a *spec* —
  the spec is already complete in [ROADMAP.md:66-84](../../ROADMAP.md) — but the
  honest admission that the plan is sound and unexecuted.

> **Decision B [chair].** **No new M1a planning is produced this run** — the
> ROADMAP M1a spec is already actionable and adding to it would be more
> documentation theater. The register simply records M1a as **"fully specced,
> 0 LOC, day 5, blocking all of Horizon 1."** The fix is not a doc; it is a
> coding session on `replay.py`. Flagged as the one carry-forward that dwarfs
> everything else.

### Debate C — The look-ahead guard masks leaks instead of detecting them (D1, new)

- **Demis:** Truncate-don't-raise is fine for production but fatal for M1a's
  acceptance test — it lets a leaky generator pass clean. M1a needs a strict mode.
- **Jeff:** Cheap: a `strict: bool` on `bounded_to_as_of` / a ContextVar flag,
  raising instead of truncating. One branch in the wrapper
  ([asof.py:122-123](../../agenticwhales/asof.py)).
- **Sundar:** And M1a's acceptance bar should explicitly require the *strict*
  guard, or "byte-identical replay" is a test that can't fail for the wrong reason.

> **Decision C [chair].** **Add a strict (raise-on-future) mode to the as-of
> guard as part of M1a's acceptance bar.** This is a one-line addition to M1a's
> spec, not a new milestone — folded as a note: *"M1a's determinism test runs
> the guard in strict mode; truncation is disallowed during replay."* Owner:
> brain/systems, lands in [asof.py](../../agenticwhales/asof.py). Genuinely new
> this run; prevents M1a from shipping a green test over a real leak.

### Debate D — Merge-and-deploy will wipe run state (J1/J2, new framing of an old ticket)

- **Jeff:** Per-ticker SQLite on Fly's ephemeral disk = state lost on every
  deploy. The trust-fix merge *is* a deploy. These interact; nobody sequenced it.
- **Demis:** And M1b is un-runnable unattended until this is fixed — a multi-year
  replay can't survive a single machine restart on SQLite-on-local-disk.
- **Sundar:** Users won't see this, but it's the difference between "autonomy
  layer" and "autonomy demo."

> **Decision D [chair].** **Promote J1 (SqliteSaver → PostgresSaver) from a loose
> TASKS.md P0 to an explicit precondition of *both* the S1 deploy and M1b**, and
> record the sequencing hazard so the merge isn't done blind. No code changed
> this run; the value is naming the interaction between two lists that were
> tracked separately.

### Debate E — The critique cadence should change (S3 + J3, the self-referential one)

- **Sundar:** Four docs, one chore. Daily critique on a repo that ships one
  commit per 48h is producing the appearance of rigor and no motion.
- **Jeff:** It also violates our own rule — each run ends with a doc and no code,
  the exact "uncommitted = didn't happen" failure. Add a forward-progress guard:
  don't re-run until the prior P0 actually merged, or stretch the period.
- **Demis:** Dissent, partially: the critique *did* catch real, new bugs this run
  (the silent-truncation guard, the ephemeral-disk checkpointer). Killing it
  loses that. The fix is **less frequent, not gone** — a critique is valuable
  when there's a delta to critique, wasteful when there isn't.

> **Decision E [chair] — the one structural recommendation.** **Recommend the
> operator change `daily-executive-critique` from daily to either (a) weekly, or
> (b) gated: skip the run if `git log main..roadmap-north-star` shows no new
> *code* commit since the last review.** Rationale: the marginal critique has
> sharply diminishing returns when the repo delta is one lockfile, and a daily
> cadence makes the loop itself an instance of the theater it diagnoses. Demis's
> caveat is honored — the gate *preserves* the critique for when there's real
> delta (it would still have fired this run on the trust-fix commit). I did
> **not** modify the scheduled task — changing the operator's automation cadence
> is their call. Surfaced as a recommendation with the exact gate condition.

---

## 5. Decision register (who said what → what we do)

| # | Decision | Raised by | Resolved against | Priority | Status |
|---|---|---|---|---|---|
| A | Trust fix is **committed, NOT merged** → still live-defective; merge is gated on J1 sequencing | Sundar (S1), Jeff (J2) | consensus | **P0** | **Corrected to "not shipped"; operator merge+deploy pending** |
| B | M1a is fully specced, **0 LOC on day 5**, blocking all of H1; needs a coding session, not more spec | all three | consensus | **P0** | **Carry-forward, dwarfs all else** |
| C | Add **strict (raise-on-future) mode** to the as-of guard; require it in M1a's determinism test | Demis (D1, new) | Jeff (cheap), Sundar | **P1** | **New this run; folded into M1a acceptance note** |
| D | **J1 (Postgres checkpointer)** is a precondition of *both* the S1 deploy and M1b; merge isn't blind | Jeff (J1/J2, new framing) | Demis, Sundar | **P0** | **New framing; sequencing hazard recorded** |
| E | Change critique cadence: **weekly, or gate on a new code commit** since last review | Sundar (S3), Jeff (J3) | Demis (keep it, less often) | **P1** | **Recommendation to operator; task not auto-modified** |
| — | Banner `/analyze` as feature-frozen | Sundar (S2) | consensus | **P1** | **still un-done; 4 days open** |
| — | `outcome_source` enum before M1b's first row | Demis (D3) | Jeff (cheap) | **P1** | carry-forward, unchanged |
| — | M1b evidence must be benchmark-relative, not sign-of-PnL | Demis (06-03 C / D2) | consensus | **P1** | carry-forward; stub still scores sign-of-PnL |

## 6. The single thread through all of it

Four runs in, the reviewers still agree on one artifact — a cheap, deterministic,
look-ahead-free, real-LLM walk-forward equity curve — and it still does not
exist; M1a, its substrate, has zero lines five days after being named the only
"Now." The genuinely *new* lesson of this run is not another gap in the product.
It is that **the critique has started to mirror the product's central flaw:**
both produce artifacts that read like progress (decisions, docs / wired-but-toy
subsystems) while the load-bearing work (a merge, a `replay.py`) stays unbuilt.
The most valuable things this run can do are therefore small and concrete: correct
one false "done" (the trust fix is committed, not shipped), surface two real new
bugs the prior runs missed (a leak-masking guard, a state-wiping checkpointer),
and recommend the critique stop running daily on a repo that ships weekly. The
product does not need a fifth strategy doc. It needs `roadmap-north-star` to reach
`main`, and someone to open `replay.py`.

---

*Generated autonomously by the `daily-executive-critique` scheduled task on
2026-06-05 (no 06-04 run). This run produced **only this document** — it did not
commit, merge, deploy, edit ROADMAP/TASKS, or modify the scheduled task. Those
are outward or operator-owned actions (see Decisions A, D, E). Calls made without
the operator present are flagged **[chair]** and are reversible.*
