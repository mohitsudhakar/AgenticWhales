# Daily Executive Critique — 2026-05-19

**Reviewers:** Sundar Pichai (product / GTM / ecosystem), Demis Hassabis (research / evaluation / AI), Jeff Dean (systems / infra / reliability).

**Method.** Each reviewer was given the same repo state. Their lens-specific critiques are summarised, then debated against the other two, then resolved into a single decision + a concrete fix anchored to files. Fixes are ordered by severity, not by reviewer.

---

## 0. Executive summary

> **Sundar:** "Architecturally honest, commercially mute. There is a product here, but the marquee is set up for researchers, the door is unlocked, and there is no cashier."
>
> **Demis:** "The system *measures* learning but does not *do* learning. Brier, calibration, behavioural detectors — all present, none closed-loop. The bull/bear theatre is the most expensive thing you do, and it isn't adversarial."
>
> **Jeff:** "Single-process is fine until it isn't. The budget cap is non-atomic, leadership is best-effort, and there is no retry on a provider 429. None of those will hold past the first 50 concurrent recipes."

**Top-five decisions (consensus):**

1. **Close the learning loop** — auto-apply calibrated Platt fits and outcome-weighted memory into the PM prompt path; stop offering "suggestions."
2. **Make the budget gate atomic** — Postgres `SELECT … FOR UPDATE` debit before fire, refund-on-skip, not best-effort log after.
3. **Fix heterogeneity at runtime** — fail loud (or `WARN`+`metrics.inc`) when synthesiser/debater falls back to upstream provider; never silent.
4. **Productise the ladder** — kill `/analyze` as a peer route to `/fund`; demote it to `/fund/advanced`; rewrite the README hero around the learning loop, not the feature list.
5. **Real adversaries for bull/bear** — require *cross-family* models (enforced by `heterogeneity_check`), measure debate-diversity post-hoc against a baseline, and degrade `RecommendationStrength` when diversity is low.

The remainder of this doc records every point each reviewer raised, the cross-examination, and the resolved fix.

---

## 1. Closing the learning loop  *(Demis raises, Jeff seconds, Sundar reframes)*

**Demis.** `agenticwhales/outcomes.py` resolves trades into `(predicted_prob, realized_hit)` pairs and writes them to `decision_outcomes`. `agenticwhales/calibration.py:334–378` *offers a suggestion*. `agenticwhales/memory_v2.py` scores entries by `1 - mean_brier` but never re-scores. `agenticwhales/adaptive.py:42–95` escalates on token-Jaccard variance, not on outcomes. **The loop is open.** We are running a learning-shaped harness that never learns.

**Jeff (agree, with mechanism).** Even if you wanted to close it, today's plumbing won't carry it cleanly — no injectable RNG (`agenticwhales/paper.py:70`), no injectable clock (`web/runner.py:161,239`), traces don't survive the cross-thread hop from scheduler → runner thread. You can't auto-tune what you can't replay.

**Sundar (push-back).** Auto-applying calibration to real users without consent is a *trust* event, not just an engineering event. The "offer a suggestion" pattern exists for a reason: in finance, silent recalibration is the kind of thing that ends up in a regulator's letter. If you close it, make the audit trail loud.

**Decision.** Close the loop, but with three guard-rails:
- **Apply automatically only above N=100 per user** (raise from the current `UNLOCK_N=30` in `agenticwhales/calibration.py:54`). 30 is under-powered for a 2-param Platt; 100 gives the slope CI room.
- **Write every auto-apply into `audit_log`** with `actor='calibrator'`, `metadata={a, b, n, ci}`. The user can override or freeze in `/fund` settings.
- **Add an "auto-tune disabled" pill** to the PM card so the user always sees calibration state.

**Fixes.**
- [agenticwhales/calibration.py:54](agenticwhales/calibration.py:54) — raise `UNLOCK_N` from 30 → 100; add `AUTO_APPLY_N=100`; emit `audit_log` row on apply.
- [agenticwhales/adaptive.py:42](agenticwhales/adaptive.py:42) — replace token-Jaccard escalation with a weekly fit over `(brier_at_quick, brier_at_deep)` per ticker family; the threshold becomes the break-even of deep-model cost vs Brier delta.
- [agenticwhales/memory_v2.py](agenticwhales/memory_v2.py) — add a nightly re-rank job (piggyback on `outcome_resolver_nightly`); decay `predictiveness` with a half-life so stale entries lose weight.
- [agenticwhales/paper.py:70](agenticwhales/paper.py:70), [web/runner.py:161](web/runner.py:161) — inject `Clock` and `RNG` from a single context-vars-backed providers module so replays are deterministic.

---

## 2. Atomic budget enforcement  *(Jeff raises, Sundar amplifies, Demis declines to comment)*

**Jeff.** `web/scheduler.py:645–667` reads `recipe.token_cost_usd` and `user.daily_spend_cap_usd` then fires. Two leader candidates that briefly co-exist (see §4) will each see budget available, each fire. `agenticwhales/llm_clients/cost_middleware.py:46–159` debits *after* the run, best-effort, exceptions logged. The cap is a soft suggestion.

**Sundar.** This is *also* the monetisation gate. If we ever ship tiers, the cap *is* the product. Soft caps mean every paid customer eventually sees a spend they didn't authorise.

**Decision.** Move the debit to *before* the fire as a transactional reservation, refund on skip/fail.

**Fix.**
- Add an RPC `reserve_spend(user_id, recipe_id, est_usd)` in [docs/supabase-schema.sql](docs/supabase-schema.sql) — `SELECT … FOR UPDATE` on `user_spend_daily`, INSERT into `spend_reservations` with `fire_id`, return `(allowed: bool, balance: numeric)`.
- [web/scheduler.py:645](web/scheduler.py:645) — gate the fire on `reserve_spend` return value, not a separate read.
- [agenticwhales/llm_clients/cost_middleware.py:46](agenticwhales/llm_clients/cost_middleware.py:46) — at end-of-fire, *settle* the reservation with actuals (debit may be < or > estimate); refund on `skipped`/`failed`.
- Add `tests/integ/test_budget_atomicity.py` — two concurrent fires of the same user, est_usd > half-cap, asserts only one passes the gate.

---

## 3. Provider rate-limit (429) handling  *(Jeff raises, Demis agrees, Sundar agrees)*

**Jeff.** Grep for `429`, `retry`, `backoff` in `agenticwhales/llm_clients/` → zero matches. A single 429 fails the whole graph fire. There is no token bucket, no jittered retry, no circuit breaker per provider.

**Demis.** This also corrupts evals. A 429-induced fallback to upstream provider during a debate silently violates the heterogeneity mandate (see §5) and the Brier comparison loses validity for that run.

**Decision.** Add a thin retry wrapper inside `cost_middleware`, not in each provider client — one place, one policy.

**Fix.**
- New file `agenticwhales/llm_clients/retry.py` — `@with_retry(provider, max_tries=4, base=0.5, cap=8.0, jitter=0.25, retry_on={429, 5xx})`. Token-bucket per `(provider, model)` keyed in `cost_middleware._token_bucket`.
- Decorate `factory.create_llm_client` outputs so every provider inherits it.
- Emit `aw_llm_retry_total{provider, model, reason}` and `aw_llm_giveup_total`.
- On giveup: re-raise `ProviderUnavailable`, which the runner converts to `status='failed_provider'` (separate from `failed_model`, so we can alert on it).

---

## 4. Leader-election safety  *(Jeff raises, Demis agrees by analogy, Sundar pragmatic)*

**Jeff.** `web/scheduler.py:444,486–488` — heartbeat refresh 5 s when leader, 15 s as candidate, 30 s stale → election. A GC pause or CPU starvation > 30 s gives you two leaders until the old one notices. Combined with §2, dual leaders × two fires = double-charged users.

**Sundar.** Customers will not forgive a "we charged you twice because of GC." This must be a hard rail.

**Demis.** It also breaks the canary in `adaptive.py` — two leaders mean two simultaneous prompt-eval runs, doubling the cost and corrupting the comparison set.

**Decision.** Promote the leader claim from a heartbeat-table check to the Postgres advisory lock that is already claimed in `lifespan`. Every leader-only action must call `pg_try_advisory_xact_lock` *inside the action's transaction* — not just check a heartbeat row.

**Fix.**
- [web/scheduler.py:486](web/scheduler.py:486) — replace `_is_leader` boolean check with `with advisory_xact_lock(LEADER_KEY): …` around every scheduled job's critical section (the `reserve_spend` RPC, the outcome resolver write batch, the prompt-eval kick-off).
- The heartbeat row stays — but as observability, not as authority.
- Add `tests/integ/test_scheduler_leader_failover.py` (currently missing per README:245) — simulate a 60 s leader pause, assert no double-fire of the same recipe.

---

## 5. Heterogeneity: enforced at config, lost at runtime  *(Demis raises, Sundar reframes as a brand promise)*

**Demis.** `agenticwhales/heterogeneity.py` validates the *config*. `agenticwhales/graph/trading_graph.py:114` falls back to upstream provider on credential miss with `warning`, not error. Bull/bear are role-prompted variants of the same model in practice (`agenticwhales/agents/researchers/bull_researcher.py`, `bear_researcher.py`). The architecture *assumes* role conditioning yields orthogonal arguments; there is no evidence it does. The mock eval `tests/evals/diversity_engine_eval.py:96` measures with `MockDecisionMaker(seed=42)`, not real LLMs.

**Sundar.** If we tell users we are running a "multi-provider debate," running it on one provider when a key is missing is a quiet brand lie. It's the difference between "diversified" and "you said diversified."

**Jeff.** A silent fallback also corrupts the `aw_llm_call_seconds{provider,model,agent}` histogram — you'll think you're running Anthropic when you're actually running Gemini. That's exactly the kind of observability rot that takes weeks to detect.

**Decision.** Heterogeneity becomes a runtime invariant, not a startup invariant. Fail loud on fallback; never re-route the bull and bear into the same family without explicit opt-in.

**Fix.**
- [agenticwhales/heterogeneity.py:39](agenticwhales/heterogeneity.py:39) — add `runtime_heterogeneity_assert(actual_models: dict)` called at the start of each graph fire.
- [agenticwhales/graph/setup.py](agenticwhales/graph/setup.py) — replace the silent fallback with: (a) if `AGENTICWHALES_ALLOW_HETEROGENEITY_FALLBACK=1`, log + `metrics.aw_heterogeneity_fallback_total.inc`; (b) otherwise raise `HeterogeneityViolation`. Default off.
- Add a post-debate **diversity score** computed in [agenticwhales/disagreement.py:70](agenticwhales/disagreement.py:70) using two different distances (cosine on hashed-TF *and* edit distance on the structured `claim_set`) — if median < 0.4 across the last 50 fires, surface a banner in `/fund`.

---

## 6. Bull/Bear are not adversaries  *(Demis raises, Sundar reframes, Jeff costs it out)*

**Demis.** A single LLM doing two roles will find correlated arguments. The disagreement metric is cosine over hashed TF (`agenticwhales/disagreement.py:70–83`); it only computes *after* the debate. There's no signal during the debate, no penalty for low diversity, no incentive for genuine disagreement.

**Sundar.** "Multi-agent debate" is on the README hero. If the debate is theatre, the hero is theatre, and that's the kind of thing that ends up in a Hacker News thread.

**Jeff.** It's also the most expensive subsystem per fire. If the debate doesn't materially change the PM decision distribution vs a single-prompt baseline, we are spending Brier-equivalent money for Brier-equivalent output.

**Decision.** Treat heterogeneity as the *primary* invariant of the bull/bear stage. Add an ablation that measures whether the debate moves the decision.

**Fix.**
- Hard-require `bull.model.family != bear.model.family` in `heterogeneity_check`; reject the config otherwise.
- Wire `agenticwhales/ablation.py` to log a *shadow* single-LLM run alongside each real debate (sampled at 5%, off the critical path) — compare PM decision change rate, Brier delta. If after N=200 the debate does not improve Brier by ≥ 0.01 on the median user, recommend deprecation in the next exec critique.
- [agenticwhales/agents/researchers/bull_researcher.py](agenticwhales/agents/researchers/bull_researcher.py) — pass an explicit `claim_set` schema (list of structured claims) so disagreement can be measured at the *claim* level, not the surface text level.

---

## 7. Eval rigor: low N-floor, unseeded, no held-out set  *(Demis raises, Jeff agrees, Sundar declines)*

**Demis.** `PROMPT_EVAL_MIN_N=20` (`agenticwhales/adaptive.py:46`) is below the standard for classifier confidence; live LLM calls aren't seeded; the "canary" baseline is the prior prompt on the same live corpus, not a held-out set. None of this is reproducible.

**Jeff.** And cost-wise the weekly eval is firing four times what it would if we had a stable held-out set we re-ran each week.

**Decision.** Carve a held-out set; raise the floor; admit that LLM stochasticity won't be eliminated and instead average across `K=3` seeds per prompt-variant.

**Fix.**
- New table `prompt_eval_holdout` (50 frozen `(ticker, as_of, market_snapshot_hash)` rows, sampled stratified by sector + decision direction). Schema added to [docs/supabase-schema.sql](docs/supabase-schema.sql).
- Raise `PROMPT_EVAL_MIN_N` to 50 in [agenticwhales/adaptive.py:46](agenticwhales/adaptive.py:46).
- Run `K=3` per variant; report Brier mean ± std; only adopt if mean improvement > 1.96 × std.
- Add `agenticwhales/adaptive.py::evaluate_prompt_variant` arg `seed_set: list[int]` so the harness is replayable.

---

## 8. Calibration: point estimates only, small N  *(Demis raises, Sundar agrees on UX)*

**Demis.** Per-user Platt fit at N=30 with no CV, no bootstrap. Output is a single scalar — no quantile, no posterior. Position sizing collapses uncertainty into a point.

**Sundar.** "67% confidence" and "67% ± 14%" are very different user experiences. The latter is honest; the former is overclaiming.

**Decision.** Keep Platt for now, but emit a bootstrap CI; expose it in the UI; let the Kelly sizer use the lower CI bound as the win-rate input (Kelly with uncertainty discount).

**Fix.**
- [agenticwhales/calibration.py:85](agenticwhales/calibration.py:85) — add `bootstrap_platt(n_boot=200)`, return `(a, b, a_ci, b_ci)`.
- [agenticwhales/paper.py:70](agenticwhales/paper.py:70) — Kelly fraction uses `p_lower_ci` not `p_mean`.
- `/fund` PM card: render `prob_of_profit` as `mean (lower–upper)` with the lower band coloured.

---

## 9. Look-ahead risk in live outcome resolution  *(Demis raises, Jeff agrees, Sundar shrugs)*

**Demis.** `agenticwhales/asof.py` is a clean ContextVar guard for backtests. In live execution there is no `as_of_date` context, so the decorator is a no-op. Outcome resolution in `agenticwhales/outcomes.py:79–86` reads from `market_snapshot` whose freshness/revision semantics aren't documented. yfinance *does* revise adjusted close for splits & dividends.

**Decision.** Document the contract and enforce it.

**Fix.**
- Add a docstring at [agenticwhales/market_snapshot.py](agenticwhales/market_snapshot.py) declaring "latest-close, revision-stable after T+1 settlement, sourced from yfinance with split/div adjustment as of fetch time."
- In `outcomes.py:79`, refuse to resolve an outcome whose `realized_at` is < 2 trading days past `expected_hold_end` (settlement buffer), and persist the data hash next to the resolved row so a later revision can be detected.

---

## 10. Threading model: one OS thread per recipe fire  *(Jeff raises, Demis declines, Sundar pragmatic)*

**Jeff.** `web/runner.py:210–211` spawns a daemon thread per fire. WS events flow back via `loop.call_soon_threadsafe`. Fine at 10 concurrent. Painful at 100. A blocking LLM client stalls *its* thread, not the loop — but at 500 active fires that is 500 MB of stack plus context-switch overhead.

**Sundar.** Not the first thing to fix — we are nowhere near 500 concurrent fires today, and a refactor here ripples through the runner, batch runner, scheduler, streaming worker. Tag it for the day we cross 50.

**Decision.** Defer the rewrite. Instrument the ceiling so we know when we're approaching it. Add a hard cap.

**Fix.**
- Add `aw_runner_active_threads` gauge in [agenticwhales/observability.py](agenticwhales/observability.py).
- Add `AGENTICWHALES_MAX_CONCURRENT_FIRES=32` env (configurable); the scheduler refuses to dispatch beyond it and queues with backpressure.
- File an explicit "P2: async runner refactor" entry in `AgenticWhales_Future.md` — owner unassigned, target after >50 concurrent in prod for a week.

---

## 11. Streaming worker: per-symbol memory + queue policy  *(Jeff raises)*

**Jeff.** `web/streaming_worker.py:143` queue is `maxsize=8192` with no drop policy — producer waits. `_state` dict grows with every unique symbol; no eviction. Recipe `max_fires_per_hour` is good; symbol fan-in is not.

**Decision.** Drop-oldest on queue full (price ticks are tolerant of loss); LRU-evict symbols not referenced by any active recipe.

**Fix.**
- [web/streaming_worker.py:143](web/streaming_worker.py:143) — `Queue` → bounded `deque(maxlen=8192)` with a `queue_drop_total` counter on overflow.
- Add `_evict_unreferenced_symbols()` to the worker's housekeeping tick (every 60 s); track via `aw_streaming_symbols_active`.

---

## 12. Tracing across the runner-thread hop  *(Jeff raises)*

**Jeff.** Correlation ID is context-var (good for same-thread). No OTel span is propagated from scheduler coroutine → runner thread → LLM call. The README claims "every recipe fire is one trace" — today it's three disjoint traces stitched only by `fire_id`.

**Decision.** Carry the OTel context explicitly.

**Fix.**
- [web/runner.py:210](web/runner.py:210) — capture `otel_context = trace.set_span_in_context(current_span)`; pass into the thread; re-attach with `attach(otel_context)` inside `_run_safe`.
- Add a smoke test in `tests/test_observability_trace.py` that asserts `trace_id` equality across scheduler-emitted and runner-emitted spans.

---

## 13. Two surfaces, no ladder  *(Sundar raises, Demis declines, Jeff supports on auth/quota)*

**Sundar.** `/fund` is the product; `/analyze` is legacy; the README admits this but the routes are peers. The default `/` redirect to `/fund` is correct, but the cross-link in the fund footer (`fund.html:49`) says "↗ /analyze (power user)" — that implicitly *downgrades* `/fund` ("power users go there"). New visitors don't know which is the front door.

**Jeff.** Two surfaces also means duplicated quota enforcement and two paths to test. Already a source of drift in `web/server.py`.

**Decision.** Demote `/analyze` to `/fund/advanced`. Single product surface, single auth path.

**Fix.**
- [web/server.py](web/server.py) — `/analyze` → 308 redirect to `/fund/advanced`; mount the legacy template at the new path.
- Update [web/static/index.html](web/static/index.html) hero copy + remove the "↗ /analyze (power user)" footer link.
- Tighten the README hero to a single sentence pointing at `/fund` only.

---

## 14. Differentiation buried  *(Sundar raises)*

**Sundar.** The README hero reads as feature soup. The fork's actual deltas over upstream TradingAgents — paper trading, autonomy spine, journal, learning loop — are scattered across lines 56, 234, 502. A new visitor cannot distinguish AgenticWhales from the upstream repo.

**Decision.** Rewrite the hero around the learning loop, not the architecture.

**Fix.**
- [README.md:2](README.md:2) — replace the current pitch with: *"AgenticWhales is a multi-agent trading research platform that closes the loop: every paper trade resolves into a calibrated Brier score that re-weights memory, behavioural detectors, and prompt selection. Forked from TauricResearch/TradingAgents; the deltas live in `/fund` — autonomy, paper trading, journal, calibration."*
- Add a "What we changed vs upstream" subsection right after the hero.
- Move the star-history badge to point to *this* repo, not the upstream (README:287).

---

## 15. Monetisation is a stub  *(Sundar raises, Jeff supports via §2)*

**Sundar.** `profiles.tier` enum is `novice|intermediate|master`. Pricing is "stubs until pricing is finalised." There is no upgrade CTA, no billing surface, no per-tier feature gate beyond the daily count. Cost tracking is admin-only.

**Decision.** Don't ship pricing today, but stop shipping *non-decisions*. Wire the rails so a pricing decision can land in one PR.

**Fix.**
- Per-tier `feature_flags` JSON on `profiles` — `{can_use_streaming, max_concurrent_recipes, max_daily_spend_usd}`. Read by `cost_middleware` and `streaming_worker`.
- User-facing "Spend today / cap" pill in `/fund` (already a metric — surface it).
- Add a `pricing_decision` placeholder doc with the three open questions (free quota, paid floor, conversion CTA placement) — flag for a /schedule follow-up when pricing is decided.

---

## 16. Ecosystem: no broker, no export  *(Sundar raises)*

**Sundar.** Paper-only is fine for the research crowd, but the journal, decisions, and outcomes have no export. No CSV, no Parquet, no notebook handoff. Quant researchers — *the primary persona on the architecture page* — cannot get their data out without raw SQL.

**Decision.** Ship a read-only export surface; defer real-broker integration.

**Fix.**
- New endpoint `GET /api/export/{kind}.parquet?from=&to=` for `decisions | outcomes | journal | paper_orders` — service-role read, RLS-respecting.
- New CLI: `agenticwhales export decisions --from 2026-01-01 --out decisions.parquet`.
- Defer Interactive Brokers / Tradier wiring per phase6-downside-mitigation.md — that's a regulated path; do not start without compliance review.

---

## 17. Community scaffolding missing  *(Sundar raises)*

**Sundar.** No CONTRIBUTING.md, no CODE_OF_CONDUCT.md, no issue/PR templates, no public roadmap. CHANGELOG is good. Star-history badge points to upstream.

**Decision.** Bare-minimum scaffolding now; don't over-invest until there is a community to scaffold *for*.

**Fix.**
- Add `.github/ISSUE_TEMPLATE/bug.md` and `.github/ISSUE_TEMPLATE/feature.md` (1 page each).
- Add `CONTRIBUTING.md` referencing the test command and PR norms already implicit in CHANGELOG.
- Defer Discord / forum until the first external PR lands.

---

## 18. Regulatory positioning  *(Sundar raises)*

**Sundar.** Paper-only + research disclaimer is defensible today. Phase 6 (real-broker + options spreads) will need teeth.

**Decision.** Keep the disclaimer; add a one-sentence "paper-only" banner on every `/fund` page; do not start Phase 6 wiring without a documented compliance review.

**Fix.**
- `/fund` global banner: "📄 Paper-only. No real-money trading. Outputs are not financial advice."
- Add a `COMPLIANCE_TODO.md` listing the unresolved items from phase6-downside-mitigation.md (Tier 3 options approval, $25k margin check, jurisdiction check) — to be cleared before any real-broker code merges.

---

## Decision matrix (who said what, what we are doing)

| # | Topic | Raised by | Contested by | Decision |
|---|---|---|---|---|
| 1 | Close learning loop | Demis | Sundar (audit) | Auto-apply at N=100, audit-log every change |
| 2 | Atomic budget gate | Jeff | — | `reserve_spend` RPC, settle/refund |
| 3 | 429 retry | Jeff | — | `with_retry` wrapper in `cost_middleware` |
| 4 | Leader-election safety | Jeff | — | Advisory-lock-per-job, not heartbeat-checked |
| 5 | Heterogeneity at runtime | Demis | Sundar (brand) | Fail loud unless explicit opt-in env |
| 6 | Bull/Bear adversarial | Demis | Sundar (brand), Jeff (cost) | Cross-family hard-required, shadow ablation |
| 7 | Eval rigor | Demis | Jeff (cost) | Held-out set, K=3 seeds, N=50 floor |
| 8 | Calibration uncertainty | Demis | Sundar (UX) | Bootstrap CI, lower-CI Kelly |
| 9 | Look-ahead in live outcomes | Demis | — | Settlement buffer, persist data hash |
| 10 | Thread-per-fire | Jeff | Sundar (defer) | Cap+instrument now, refactor at 50 concurrent |
| 11 | Streaming worker mem | Jeff | — | LRU evict, drop-oldest |
| 12 | OTel cross-thread | Jeff | — | Explicit context propagation |
| 13 | `/fund` vs `/analyze` | Sundar | — | `/analyze` → `/fund/advanced` |
| 14 | Differentiation | Sundar | — | Rewrite hero around the loop |
| 15 | Monetisation rails | Sundar | Jeff (via §2) | `feature_flags` JSON, spend pill |
| 16 | Export & broker | Sundar | — | Parquet export now, IB deferred |
| 17 | Community scaffolding | Sundar | — | Minimal `.github/` only |
| 18 | Regulatory | Sundar | — | Banner + COMPLIANCE_TODO |

---

## Ordering for the next sprint

P0 (correctness / trust):
- §2 atomic budget gate
- §3 429 retry
- §4 leader-election advisory-lock-per-job

P1 (research integrity):
- §5 heterogeneity runtime invariant
- §1 close the learning loop (apply + audit-log)
- §6 cross-family bull/bear + claim-set schema

P2 (product surface):
- §13 collapse `/analyze` into `/fund/advanced`
- §14 README hero rewrite
- §15 feature_flags JSON + spend pill

P3 (everything else):
- §7 held-out eval set
- §8 bootstrap CI
- §9 settlement buffer
- §11 streaming LRU
- §12 OTel cross-thread
- §16 Parquet export
- §17 issue templates
- §18 paper-only banner

P4 (deferred, instrumented):
- §10 async runner refactor — gauge + cap, refactor at >50 concurrent for a week
- IB / real-broker — blocked on `COMPLIANCE_TODO.md`

---

*Generated by the daily-executive-critique scheduled task on 2026-05-19. No code was changed by this run; this is a decisions doc.*
