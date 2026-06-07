# AgenticWhales — Tasks

Repo-specific task tracker. Managed via `/track` (run from this repo) or edit by hand.
**Status:** `[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked · priorities `P0` urgent · `P1` important · `P2` nice-to-have

## Open

- [ ] (P0) M1a — build the deterministic replay substrate (`agenticwhales/replay.py` + extend `asof.py`) so replaying one date twice yields byte-identical model inputs — the sole "Now" (WIP=1); nothing else in Horizon 1 starts until it passes.
- [ ] (P0) J1 — swap per-ticker `SqliteSaver` for LangGraph `PostgresSaver` on Supabase so runs survive container death.
- [ ] (P0) J3 — add an in-process batched `CostAccumulator` with circuit breaker in `cost_middleware.py` so synchronous Supabase cost writes stop blocking every LLM call.
- [ ] (P1) M1b — implement `llm_decision_generator` driving the real graph against M1a's snapshot, with a per-backtest budget cap, replacing `momentum_stub_generator`.
- [ ] (P1) M2 — replace the flat `max_slippage_bps` placeholder with a spread + size-aware market-impact model in `paper.py`/`risk.py`/`backtest.py`.
- [ ] (P1) M3 — produce the go/no-go report comparing the net-of-cost curve against buy-and-hold, equal-weight, flat-coin, Classical-alone, and a turnover-matched random sizer.
- [ ] (P1) S2/S3 — add the `Broker` ABC (PaperBroker only) compliance boundary and `entitlements.py` + `profiles.tier` policy subsystem.
- [ ] (P1) J8 — adopt `supabase/migrations/` and split the monolithic `supabase-schema.sql` dump into ordered numbered migrations (blocks J2/J4/S5/S3/D2).
- [ ] (P1) J5/J6 — add OpenTelemetry tracing on `propagate()` and parallelize the M/Q/S/N/F analysts to cut the 5× latency tax.
- [ ] (P1) D6 — wire `disagreement.compute(...)` into `ConditionalLogic` to end debate rounds early once debaters converge.
- [ ] (P1) Go live on X recs — paste `X_BEARER_TOKEN` into `.env`, then run the live `get_x_trade_recs` smoke test (currently offline stub; PR #10 mergeable).
- [ ] (P2) Build Feature 6 derivatives hedging — OQL semantic parser, Greek-mapping heuristics, IBKR/Tradier multi-leg routing (gated behind Tier 3 / $25k PDT; currently spec-only).

## Done

- [x] (P1) 2026-06-06 — strict (raise-on-future) mode for the as-of guard (`as_of_date(d, strict=True)`); M1a's determinism test must run in strict mode so a leaky generator can't pass the byte-identical bar (Decision C, 06-05 critique). Code + 6 tests + ROADMAP acceptance note.
- [x] (P1) D4 — signal processing prefers structured `PortfolioDecision.rating` over the regex fallback.
- [x] (P1) D1 — Heterogeneity Mandate made first-class with a diagram callout and fail-fast `heterogeneity_check()`.
- [x] (P1) D5 — tool outputs tagged with source/url and wrapped in `<external_data>` guards with a prompt-injection smoke test.
- [x] (P1) S1 — Persona × Surface × Tier overlay added to ARCHITECTURE.md §0.
- [x] (P1) Structured-output decision agents — Research Manager, Trader, PM return typed Pydantic via `with_structured_output` (v0.2.4).
- [x] (P1) Persistent decision log + LangGraph checkpoint resume landed, replacing per-agent BM25 memory (v0.2.4).
