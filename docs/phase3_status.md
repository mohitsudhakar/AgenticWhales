# Phase 3 Status

*Snapshot of the four-phase floor-live-debate fix plan as we cross into Phase 3 (validation & instrumentation). Branch: `phase3-status`, based on `main` at commit `dbb5fd3`. Today: 2026-05-20.*

This document gives a single read on where everything stands across the four phases that came out of the three-reviewer assessment of the floor live debate (Demis Hassabis / Jeff Dean / Cliff Asness). It is meant to be self-contained for someone landing on `main` — full reviews and the fix plan live in [PR #7](https://github.com/mohitsudhakar/AgenticWhales/pull/7).

---

## 1. TL;DR

- **Phase 1 — Critical bug fixes.** ✅ Complete. Two `Critical`-severity defects (synthesizer collision, debater collision) are fixed; diversification status is now surfaced to the web UI. Open as [PR #7](https://github.com/mohitsudhakar/AgenticWhales/pull/7) to `main`. 7 new tests, 99 total passing.
- **Phase 2 — Performance, cost, reliability.** Partial. 1 of 5 items shipped: LLM retry + per-provider failure counter ([PR #8](https://github.com/mohitsudhakar/AgenticWhales/pull/8) into `review_fix`). One item (parallel analysts) hit an architectural complication and is now correctly scoped for a follow-up. Three items remain.
- **Phase 3 — Validation & instrumentation.** Substantially started by [PR #4](https://github.com/mohitsudhakar/AgenticWhales/pull/4) (brokerage execution + backtest harness), which delivers the walk-forward harness + slippage/commission model. Five PR-specific follow-ups remain on that PR before it can merge, plus the τ-instrumentation item and the agent-driven backtest baseline.
- **Phase 4 — Architectural.** Not started. 7 items, sized for a quarter.
- **Test status.** Across all open branches: 109 tests pass locally, no regressions.

---

## 2. Where each phase stands

### Phase 1 — Critical bug fixes ([PR #7](https://github.com/mohitsudhakar/AgenticWhales/pull/7))

All four items shipped; ready for merge to `main`.

| Item | Concern | Status |
|---|---|---|
| **P1.1** — Make `_build_diversified_synthesizer_llm` role-aware | C1 (synthesizer collision under default config) | ✅ Shipped. Both Research Manager and Portfolio Manager now resolve to different providers; per-role assignment stored in `AgenticWhalesGraph.diversification_status`. |
| **P1.2** — Fix Neutral/Aggressive debater collision | C2 (three-way risk debate round-robin had only 2 providers) | ✅ Shipped. `debater_provider_preference` default extended to `["google", "deepseek", "xai"]`; `len(usable) < 3` triggers an explicit WARN and marks the colliding slots as `degraded`. |
| **P1.3** — Promote diversification fallback to WARN + UI banner | C11 (silent degradation in production) | ✅ Shipped. Every fallback path now `logger.warning`s with role + reason; new `get_diversification_status()` public method; new one-shot WebSocket event from `SessionRunner`; new green-OK / yellow-degraded banner in the web UI. |
| **P1.4** — Phase 1 regression baseline | foundation | ✅ Shipped. `tests/integration/test_floor_pipeline.py`, 7 tests, ~1 s runtime, no live API calls. |

### Phase 2 — Performance, cost, reliability ([PR #8](https://github.com/mohitsudhakar/AgenticWhales/pull/8))

1 of 5 shipped. P2.1 hit an architectural complication and has been correctly re-scoped.

| Item | Concern | Status | Notes |
|---|---|---|---|
| **P2.1** — Parallelize the analyst stage | C5 (sequential analyst critical path) | 🚧 **Re-scoped.** Discovered during this branch: the 5 analysts share `state["messages"]` via LangGraph's `add_messages` reducer, and each analyst's `should_continue_*` reads `last_message.tool_calls`. The original "fan out from `START`" plan does not work as-stated. Proper design — each analyst as a compiled LangGraph subgraph with its own private messages stream — captured in the updated plan in PR #7. Estimated 2-3 hours of careful work on its own branch. |
| **P2.2** — Parallelize blind round-1 debate openings | C8 | ⏳ Pending. Same subgraph machinery as P2.1 unlocks it. |
| **P2.3** — Anthropic prompt caching | C9 (O(N²) tokens per debate) | ⏳ Pending. Tractable and self-contained — straightforward follow-up. |
| **P2.4** — WebSocket delta streaming | C10 (O(N²) bandwidth per debate) | ⏳ Pending. Tractable and self-contained — recommended as the next item to land. |
| **P2.5** — LLM retry + per-provider failure counter | C12 (single 429 kills the session) | ✅ Shipped. `apply_retry()` wraps every LLM Runnable with langchain's `.with_retry()` (3 attempts, exponential jitter by default); thread-safe per-provider failure counter exposes the data layer for a full circuit breaker. 10 new tests. |

### Phase 3 — Validation & instrumentation ([PR #4](https://github.com/mohitsudhakar/AgenticWhales/pull/4) + remaining items)

Mostly delivered by PR #4 (brokerage execution + backtest harness). The harness mechanics are clean — walk-forward with fill-at-next-bar-open (no lookahead), 67 new tests — but the headline number in the PR description (`AAPL 2024 BUY-only @ 10% target weight → +3.40%`) is a constant-rating wire-check, not an agent-driven validation.

| Item | Status | What PR #4 delivered | What remains |
|---|---|---|---|
| **P3.1** — Walk-forward backtest harness | ✅ Substantial | `tradingagents/backtest/{harness, decision_source, metrics, bars, runner}.py`. Walk-forward fill semantics correct. `AgentGraphDecisionSource` wired (just not exercised in CI). | Five PR-specific fixes before merge: (a) module rename `tradingagents/` → `agenticwhales/`; (b) Sharpe needs configurable risk-free rate (currently assumes 0); (c) turnover metric; (d) N / t-stat surfacing; (e) reword the "AAPL 2024 → +3.40%" claim to label it a wire-check rather than a validation. |
| **P3.2** — Transaction cost model | ✅ Partial | `SimulatedBroker` supports `slippage_bps`, `commission_per_share`, `commission_min`. CLI defaults to 5 bps. | Liquidity-bucket model (large/mid/small/micro cap with different bp tiers); market-impact heuristic; gross-vs-net surfacing in summary reports. |
| **P3.3** — τ / Λ / σ instrumentation | ⏳ Not started | — | Synthesizer-vs-debater agreement rate, decision tier distribution by synthesizer provider, per-agent latency and token cost histograms. Newly tractable because we now have (decision, realized PnL) pairs from the harness. |
| **P3.4** — Same-day same-ticker cache | ⏳ Not started | — | Cache key `{ticker, date, agent, tool_args_hash, prompt_hash}` with TTL keyed off market close. |
| **P3.5** — Agent-driven backtest baseline (new in PR #7) | ⏳ Not started | — | Run `AgentGraphDecisionSource` over S&P 100, 2022-2024, weekly rebalance. Report gross + net Sharpe, turnover, max drawdown, t-stat, tier-rating attribution. **Gating data point for every Phase 4 decision** — without it, Phase 4 is unfalsifiable. |

### Phase 4 — Architectural (next quarter)

None started. 7 items, listed for visibility:

| Item | Concern | Sized for |
|---|---|---|
| **P4.1** — Replace 5-tier rating with continuous output schema | C7, partial C15 | Quarter |
| **P4.2** — Diversify the analyst stack | C4 (analysts share single upstream model) | 1-2 weeks |
| **P4.3** — Outcome-grounded DPO judge | C15 | Quarter |
| **P4.4** — Decision-space tree search | C16 | Quarter (blocked on P4.3) |
| **P4.5** — Validate QuantRadar dimensions against forward returns | C17 | 1-2 weeks once P3.5 lands |
| **P4.6** — Variance-budget sizing (replaces drawdown-conditional risk) | C14 | 1 week (PR #4 created the right architectural seam — `SizingPolicy`) |
| **P4.7** — Provider-agnostic LLM proxy | consolidates C9, C10, C12 | 1-2 weeks |

---

## 3. Open PRs at this snapshot

```
PR #4  worktree-live-trader-executor → main    OPEN  brokerage + backtest harness
PR #7  review_fix                    → main    OPEN  Phase 1 critical fixes
PR #8  phase2_perf                   → review_fix  OPEN  Phase 2 P2.5 (LLM retry)
PR …   phase3-status                 → main    OPEN  this doc
```

Recommended merge order:

1. **PR #7** (Phase 1) — fixes two `Critical`-severity defects affecting every default-config run. Independent of PR #4. Land first.
2. **PR #8** (P2.5 retry) — based on PR #7. Rebase target updates to `main` once PR #7 lands, then merge.
3. **PR #4** (brokerage + backtest) — after the five §5 follow-ups in `docs/pr4_assessment.md` (which lives on `review_fix`) are addressed. Module rename to `agenticwhales/` is the only hard blocker; the other four are quality fixes.
4. **This PR (phase3-status)** — informational, mergeable in any order.

---

## 4. Test status

- **`main` (today)**: 32 tests pass (pre-existing).
- **`review_fix` after PR #7**: 99 tests pass (32 pre-existing + 7 Phase 1 integration tests + 60 memory log tests).
- **`phase2_perf` after PR #8**: 109 tests pass (99 + 10 retry tests).
- **PR #4 after its own changes**: 99 reported in its PR description (32 pre-existing + 67 broker/harness tests).

No regressions in any branch. CI is not yet hooked up to fail on test failure for any of these branches; that's a separate Phase 2 follow-up worth scoping.

---

## 5. Critical observations from the work so far

Three findings worth flagging.

**1. P2.1 was under-scoped in the original plan.**
The plan called "parallelize the analysts" a straightforward LangGraph refactor. It isn't. The 5 analysts share `state["messages"]` via the `add_messages` reducer; each analyst's "am I done?" conditional reads `last_message.tool_calls`, which becomes ambiguous in parallel mode; the per-analyst `Msg Clear` deletes ALL messages from shared state. The correct design uses LangGraph subgraphs, one per analyst, each with its own private messages stream. The updated plan (in PR #7) captures this. Net impact: P2.1 is still high-value, but it should be scheduled as a focused branch with a proper integration test, not lumped into a general Phase 2 sweep.

**2. PR #4's headline number is a wire-check, not a validation.**
The "AAPL 2024 BUY-only @ 10% target weight → +3.40%" result uses `FixedRatingDecisionSource(Buy)`. AAPL returned ~30% in 2024; 10% × 30% with 90% cash drag is exactly the expected outcome. This validates the harness mechanics (walk-forward sequencing, slippage application, mark-to-market) but does NOT validate the agent system. The actual validation is **P3.5** — drive the harness with `AgentGraphDecisionSource` over a real universe. Until that runs, every Phase 4 design choice is unfalsifiable. This is the single most important next data point.

**3. The Heterogeneity Mandate (Shehata & Li 2026) is now machinery without measurement.**
Phase 1 made the Mandate work correctly under default config — synthesizers don't collide, debaters don't collide, degradation is surfaced. But the empirical claim that motivates the Mandate (τ measurements, Λ collapse, σ scaling) is never measured on our own traffic. **P3.3** — synthesizer-vs-debater agreement rate by model family, decision tier distribution by synthesizer provider — is the work that turns this from theology-with-footnotes into falsifiable engineering. Newly tractable because PR #4 gives us (decision, realized PnL) pairs to correlate against.

---

## 6. Recommended next moves

In priority order:

1. **Merge PR #7** to unblock everything else. Two critical fixes, 7 tests, well-tested.
2. **Rebase PR #8 onto `main`** and merge. Adds reliability with no behaviour change.
3. **Address the five §5 follow-ups on PR #4** and merge it. After that, the backtest harness becomes the system of record for evaluating every subsequent change.
4. **Open a new branch for P3.5 (agent-driven backtest baseline).** This is the gating data point. Until it runs, the Heterogeneity Mandate, the Quant Analyst's six dimensions, the synthesizer prompts, and every Phase 4 idea are all undifferentiated guesses.
5. **In parallel, open a new branch for P2.4 (WebSocket delta streaming).** Self-contained, real UX win, doesn't depend on the harness.
6. **Then P2.1 (parallel analysts) with the subgraph design.** Largest wall-clock win for live debate.
7. **P3.3 (τ instrumentation)** once P3.5 produces enough trace data to compute meaningful agreement rates.

---

## 7. What's NOT covered by this doc

- The five PR-specific follow-ups on PR #4 (Sharpe rf, turnover, t-stat, module rename, wire-check rewording). Those live in `docs/pr4_assessment.md` on the `review_fix` branch (PR #7).
- The 17-concern table that frames all four phases. That lives in `docs/review_fix_plan.md` on the `review_fix` branch (PR #7).
- The per-agent provider-model map and the three named reviews. Those live in `docs/floor_live_debate_review.md` on the `review_fix` branch (PR #7).

Merging PR #7 will land all three of those docs on `main` and make every cross-reference in this status doc directly clickable.
