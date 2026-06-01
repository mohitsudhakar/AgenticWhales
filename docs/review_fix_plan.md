# Floor Live Debate — Three Reviews & Fix Plan

*Branch: `review_fix`, based on `origin/main` at `dbb5fd3`. Scope: the live two-stage debate (Bull/Bear → Research Manager → Trader → Aggressive/Conservative/Neutral → Portfolio Manager) and the analysts and orchestration that feed it.*

This document does two things:

1. Captures three independent critiques of the system as it stands today.
2. Translates those critiques into a prioritized, file-level fix plan.

The three reviewers are stand-ins for the lenses we actually care about:

- **Demis Hassabis** — scientific rigor, architectural coherence, learning signal, what would push toward general competence rather than prompt-tuned mimicry.
- **Jeff Dean** — systems engineering, latency, cost, reliability, observability, the cost-of-running-this-in-production lens.
- **Cliff Asness** — quant trading reality: where's the edge, how is it measured, is this just dressed-up beta, what are the transaction costs.

---

## 1. Reviews

### 1.1 Demis Hassabis — scientific & architectural

What's good. The Heterogeneity Mandate ([agenticwhales/default_config.py:40-69](agenticwhales/default_config.py)) is a real piece of thinking: most "multi-agent" systems wave at "diversity of perspectives" without identifying *why* it matters. Naming the failure mode (correlated upstream bias propagating through a shared synthesizer) and citing a specific τ table is the kind of intellectual seriousness I want to see in this space. Two further design choices are genuinely good. First, `blind_first_round` ([agenticwhales/default_config.py:100](agenticwhales/default_config.py), [agenticwhales/agents/researchers/bull_researcher.py:30-36](agenticwhales/agents/researchers/bull_researcher.py)) preserves the independence condition that any wisdom-of-crowds argument needs — it's the same intuition that makes independent samples in ensembling work. Second, the typed `QuantRadar` from the new Quant Analyst ([agenticwhales/agents/schemas.py:233](agenticwhales/agents/schemas.py)) gives the synthesizer a channel that isn't gameable by rhetorical fluency. That's the strongest single design move in the system.

What I find unconvincing. The agents have no learning signal. The Reflector and the FinMem-style layered memory ([agenticwhales/default_config.py:101-110](agenticwhales/default_config.py)) are retrieval, not learning — you observe outcomes (the trade wins, the trade loses) and that information never updates the policy. No reward model, no DPO against realized PnL, not even a thin baseline that re-weights memory by outcome. This is the AlphaGo-vs-expert-system gap. A system that doesn't get sharper with experience is — in the long run — a static rule book. The debate itself is linear, not a search. Each debate is a fixed-length round-robin; the candidate-decision space (tier × position size × stops × horizon) is small enough to tree-search over with a value head, and that's the structurally interesting move you're not making. The 5-tier output (Buy/Overweight/Hold/Underweight/Sell at [agenticwhales/agents/managers/research_manager.py:43-48](agenticwhales/agents/managers/research_manager.py) and [agenticwhales/agents/managers/portfolio_manager.py:74-84](agenticwhales/agents/managers/portfolio_manager.py)) is sell-side mimicry that throws away information; an AI-native design would emit a posterior over outcomes and let the trader do Kelly sizing.

The empirical foundation is also thinner than the code makes it sound. Shehata & Li (2026) is cited as if its τ measurements were settled science; they aren't. The same module that cites the paper should be *measuring* τ on this system's own debates (synthesizer correction-rejection rate, decision-disagreement-vs-PnL correlation) and surfacing it. Right now this is theology with footnotes. And the Mandate it's built on is silently broken at the seam where it matters most: under default config, both synthesizers — Research Manager and Portfolio Manager — resolve to the same provider, so the kinship-locked pattern you wrote 30 lines of comments to avoid is reproduced inside your own synthesizer chain ([agenticwhales/graph/trading_graph.py:210-243](agenticwhales/graph/trading_graph.py)).

What I'd build next. Make the Portfolio Manager output a posterior over realized 30-day PnL distributions instead of a tier. Backtest. Use the calibration error as a reward signal to fine-tune a small judge model that replaces the synthesizer prompt. Now you have a system that gets sharper with experience instead of with prompt edits.

### 1.2 Jeff Dean — systems & infrastructure

Critical path. Under default config and one ticker: 5 analysts (each looping with its tool node) → 4 sequential investment-debate turns → 1 Research Manager → 1 Trader → 6 sequential risk-debate turns → 1 Portfolio Manager. That's ~18 sequential third-party LLM calls per ticker, plus tool-loop iterations. At even 4s per call the wall-clock floor is 70s+ and most of that is unnecessary.

Defects in the code. Three are real bugs and one is a latent gotcha:

1. **The five analysts are wired sequentially despite sharing no state.** [agenticwhales/graph/setup.py:178-194](agenticwhales/graph/setup.py): each analyst's clear-node adds an edge to the next analyst. They're independent — each just appends its own report. Fan out from `START`, fan in at `Bull Researcher`. This is the single largest wall-clock win and a straightforward LangGraph refactor.
2. **Both synthesizers resolve to the same provider.** [agenticwhales/graph/trading_graph.py:210-243](agenticwhales/graph/trading_graph.py) walks `synthesizer_provider_preference` and picks the first non-upstream provider. Called twice (Research Manager, Portfolio Manager) with the same input → same answer. The `role` parameter is accepted and used only in the log line. With default config both managers land on Anthropic claude-opus-4-6. The Heterogeneity Mandate is inert at the second seam.
3. **Adjacent risk debaters collide.** [agenticwhales/graph/trading_graph.py:298-303](agenticwhales/graph/trading_graph.py) assigns Neutral to `usable[2 % len]`. With the default `debater_provider_preference: ["google", "deepseek"]` (len 2) that's `usable[0]` — same as Aggressive. The code comment at lines 264-269 promises that adjacent debaters in the round-robin never match. The code doesn't enforce that.
4. **The blind opening turn of every debate is serialized for no reason.** [agenticwhales/agents/researchers/bull_researcher.py:30-36](agenticwhales/agents/researchers/bull_researcher.py), [agenticwhales/agents/risk_mgmt/aggressive_debator.py:31-38](agenticwhales/agents/risk_mgmt/aggressive_debator.py): the first turn is by construction independent. The graph still goes Bull → Bear → ... and Aggressive → Conservative → Neutral. Fork the openings, sync, then serialize the rebuttals.

Cost and bandwidth. Both WebSocket payloads ([web/runner.py:328-371](web/runner.py)) and per-turn LLM prompts ([bull_researcher.py:38-41](agenticwhales/agents/researchers/bull_researcher.py)) re-send the full accumulated debate history on every turn. Over a 4-turn debate that's O(N²) tokens and bandwidth. Switch the wire to delta events; switch the prompt to Anthropic's Messages-API with prompt caching on the static system prompt + report payload. The latter alone should cut synthesizer token spend by ~50% on long debates.

Reliability and observability. There is no retry, no circuit breaker, no rate-limit awareness around provider calls — a single 429 from one provider mid-debate kills the session. The diversification fallback at [trading_graph.py:245-250](agenticwhales/graph/trading_graph.py) logs at INFO when no candidate is usable, which means a missing `ANTHROPIC_API_KEY` silently degrades the Mandate in production. Promote to WARN, surface a "DIVERSIFICATION DEGRADED" banner in the web UI, and consider failing closed for production deployments. There are no structured metrics. A system that makes load-bearing claims about τ and Λ should be measuring them on its own traffic — synthesizer-vs-debater agreement rate, decision tier distribution by synthesizer provider, per-agent latency and token cost histograms. Add OpenTelemetry spans per node; with 18 sequential calls a trace makes diagnosing slow runs trivial.

What I'd build next. A provider-agnostic LLM proxy in front of `create_llm_client` that does batching, per-provider rate-limit awareness, retry/circuit-breaker, prompt cache, and per-turn streaming-vs-non-streaming selection. Today every one of those concerns is missing or split across per-provider client classes.

### 1.3 Cliff Asness — quant trading reality

Where is the backtest. I see a lot of elaborate machinery — five analyst LLMs, a structured Quant Analyst, a synthesizer-gated debate architecture, citations to a 2026 paper — and zero evidence that any of it produces a positive Sharpe ratio out of sample after realistic costs. I searched the repo. There is no backtest harness. There is no walk-forward validation. There is no out-of-sample evaluation against forward-realized prices. Every design parameter — `max_debate_rounds: 2`, the choice of `synthesizer_provider_preference`, the analyst order, the Quant Analyst's six dimensions — is unfalsifiable until that exists. You are tuning hyperparameters against nothing.

Where is the cost model. I see no transaction costs, no slippage, no market impact, no bid-ask spread modeled anywhere in the decision output. The Portfolio Manager emits `stop_loss` and `take_profit` ([agenticwhales/agents/managers/portfolio_manager.py:74-84](agenticwhales/agents/managers/portfolio_manager.py)) as if execution were free. For a strategy whose decision cadence is event-driven by news flow and which makes discretionary tier calls on individual equities, you will be the slow money in every print you trade against. A realistic estimate is 5-15 bps per round-trip for liquid US large caps and substantially more for anything mid-cap and below. Without that in the loss function you're optimizing fantasy.

Five LLMs that share a training corpus are not five independent analysts. The system treats the Market / Social / News / Fundamentals / Quant analyst stack as if it were an ensemble of independent views. They are all reading roughly the same news flow filtered through models that were trained on roughly the same internet. When they agree on a `Buy`, you are paying for correlated noise dressed up as conviction. The Heterogeneity Mandate addresses this at the synthesizer layer (good) and at the debater layer (good). It does nothing for the upstream analysts. All five analysts are bound to `quick_thinking_llm`, which under default config is OpenAI gpt-5.4-mini ([agenticwhales/graph/setup.py:85-121](agenticwhales/graph/setup.py)). That's a single point of upstream bias feeding everything downstream.

The output format is sell-side garbage. A 5-tier rating (Buy / Overweight / Hold / Underweight / Sell) is what equity research desks emit because their customer is a portfolio manager whose risk model lives elsewhere. For a quantitative system, the right output is an expected return, a confidence (or full distribution), and a position size that follows from a portfolio variance constraint. Tier-level output also makes it impossible to backtest with proper attribution — what was the realized PnL of "Overweight calls in the bottom decile of conviction"? You can't ask the question.

Risk management as debate is theater. Three LLMs arguing about position sizing ([agenticwhales/agents/risk_mgmt/](agenticwhales/agents/risk_mgmt/)) is a category error. Real risk management is a position-sizing layer that solves a portfolio variance budget given a covariance estimate and the current book; it isn't an Aggressive/Conservative/Neutral debate. The "self-adaptive risk" idea in [portfolio_manager.py:42-51](agenticwhales/agents/managers/portfolio_manager.py) — tighten on drawdowns, loosen on recent alpha — is *literally* what every blown-up trend-follower has done since 1987. Drawdown-conditional risk reduction sells the bottom.

The Quant Analyst's six dimensions are technical indicators. Volatility, S/R strength, breakout likelihood, momentum strength, pattern reliability, trend certainty ([agenticwhales/agents/analysts/quant_analyst.py](agenticwhales/agents/analysts/quant_analyst.py)). The academic literature on whether these predict cross-sectional returns at any meaningful horizon is voluminous and brutal. Most of the effect that survives is momentum, and most of the momentum that survives is sector-level and slow. If the radar is going to inform synthesizer decisions, validate each dimension against forward returns at the horizons you intend to trade.

What would convince me. (1) A walk-forward backtest over at least 10 years of US large/mid-cap equities with realistic per-trade transaction costs and a turnover-aware Sharpe. (2) Capacity analysis — what AUM does this thing scale to before its own market impact eats the edge. (3) Attribution — when the system makes money, is it from beta, from sector exposure, from the documented factor zoo, or from genuine alpha. (4) A statistically powered N of decisions. How many independent decisions per year per ticker; what's the t-stat of the resulting return stream against the null.

---

## 2. Consolidated concerns

| # | Concern | Raised by | Severity |
|---|---|---|---|
| C1 | Both synthesizers resolve to the same provider under defaults — Heterogeneity Mandate inert at the second synthesizer | Demis, Jeff | **Critical** |
| C2 | Risk debaters Neutral & Aggressive collide on the same provider when `debater_provider_preference` has len < 3 | Jeff | **Critical** |
| C3 | No backtest / no out-of-sample validation harness — every design parameter is unfalsifiable | Cliff, Demis | **Critical** |
| C4 | All five upstream analysts share a single model (`quick_thinking_llm`) — kinship-locked upstream above the analyst stack | Cliff | High |
| C5 | Analysts run sequentially despite shared-nothing state | Jeff | High (latency) |
| C6 | No transaction cost / slippage / market impact in the decision output | Cliff | High |
| C7 | 5-tier output discards information needed for both AI-native trading and proper attribution | Demis, Cliff | High |
| C8 | Blind round-1 debate turns are serialized despite being independent by construction | Jeff | Medium (latency) |
| C9 | Per-turn LLM prompts re-include full history (O(N²) tokens, no prompt caching) | Jeff | Medium (cost) |
| C10 | WebSocket re-sends full debate history each turn (O(N²) bandwidth) | Jeff | Medium |
| C11 | Diversification fallback logs at INFO; can silently degrade in production | Jeff | Medium |
| C12 | No retries / circuit breaker around provider calls — one 429 kills the session | Jeff | Medium |
| C13 | No τ / Λ / σ instrumentation — the theoretical claims aren't validated on real traffic | Demis, Jeff | Medium |
| C14 | "Self-adaptive risk" tightens on drawdowns / loosens on recent alpha — sells-the-bottom anti-pattern | Cliff | Medium |
| C15 | Reflector is retrieval, not policy update — no outcome-grounded learning | Demis | Long-term |
| C16 | Debate is linear, not a search over decision alternatives | Demis | Long-term |
| C17 | Quant Analyst 6-dim radar dimensions are unvalidated against forward returns | Cliff | Long-term |

---

## 3. Fix plan

Four phases. Each item lists the concerns it addresses, the files touched, and a verifiable acceptance criterion. The phases are sized so Phase 1 can land this week, Phase 2 over the next two weeks, Phase 3 in roughly a month, and Phase 4 as the next quarter's roadmap. Phase 1 and Phase 2 are pure engineering; Phase 3 is where validation begins; Phase 4 is the structural redesign that the Demis and Cliff reviews point to.

### Phase 1 — Critical bug fixes (week 1) — *Landed on `review_fix`*

All four items shipped on the `review_fix` branch with 7 new integration tests; full suite of 99 tests passes locally with no regressions.

**P1.1 — Make `_build_diversified_synthesizer_llm` actually role-aware. ✅ Done.**
Addresses: C1.
Files touched: [agenticwhales/graph/trading_graph.py](agenticwhales/graph/trading_graph.py) — `__init__` now passes `exclude={research_manager_provider}` to the Portfolio Manager call; `_build_diversified_synthesizer_llm` takes an optional `exclude` set and skips both upstream and excluded candidates; per-role resolved provider stored in `self.diversification_status`.
Verified by: `tests/integration/test_floor_pipeline.py::test_default_config_synthesizers_use_different_providers` and `::test_portfolio_manager_falls_back_when_only_one_synth_provider_available`.

**P1.2 — Fix the Neutral/Aggressive debater collision. ✅ Done.**
Addresses: C2.
Files touched: [agenticwhales/default_config.py](agenticwhales/default_config.py) — `debater_provider_preference` now defaults to `["google", "deepseek", "xai"]` (added xai as the third entry). [agenticwhales/graph/trading_graph.py](agenticwhales/graph/trading_graph.py) `_build_debater_llms` — explicit `len(usable) < 3` WARN, per-debater provider recorded in `diversification_status`, and adjacent-pair collision detection that marks colliding slots as `degraded`.
Verified by: `::test_default_config_no_adjacent_debater_collision` and `::test_two_debater_providers_marks_collision_as_degraded`.

**P1.3 — Promote diversification fallback to WARN and surface it. ✅ Done.**
Addresses: C11.
Files touched: [agenticwhales/graph/trading_graph.py](agenticwhales/graph/trading_graph.py) — every fallback path now `logger.warning`s with the role and the reason. New `AgenticWhalesGraph.get_diversification_status()` public method. [web/runner.py](web/runner.py) — new `_set_diversification_status` helper and one-shot emit right after graph construction. [web/static/index.html](web/static/index.html), [web/static/app.js](web/static/app.js), [web/static/styles.css](web/static/styles.css) — new `#s-diversification-banner` element with green-OK / yellow-degraded variants, populated by the new event handler.
Verified by: `::test_diversification_status_shape`, `::test_only_upstream_creds_set_marks_all_diversified_slots_degraded`, manual UI spot-check pending.

**P1.4 — Phase 1 regression baseline. ✅ Done.**
Files added: [tests/integration/test_floor_pipeline.py](tests/integration/test_floor_pipeline.py) — 7 tests, all green, no live API calls, total runtime ~1s.
Coverage: synthesizer non-collision, debater non-collision, status shape, top-level degraded flag, 2-provider partial collision, fully degraded fallback, single-synthesizer fallback. The full 99-test suite still passes.

Definition of Done for `review_fix`: all four items landed; opens a PR for review.

### Phase 2 — Performance, cost, reliability (weeks 2-3)

**P2.1 — Parallelize the analyst stage. *Blocked on subgraph refactor (discovery during phase2_perf).***
Addresses: C5.
Discovery: the original "fan out from START, fan in before Bull Researcher" plan does not work as-stated because the five analysts share `state["messages"]` via LangGraph's `add_messages` reducer (see [agenticwhales/agents/utils/agent_states.py:46](agenticwhales/agents/utils/agent_states.py) — `AgentState(MessagesState)`). Each analyst's `should_continue_*` in [agenticwhales/graph/conditional_logic.py](agenticwhales/graph/conditional_logic.py) reads `state["messages"][-1].tool_calls`, which becomes ambiguous when multiple analysts append to the message list concurrently. The per-analyst `Msg Clear` (via `create_msg_delete` in [agenticwhales/agents/utils/agent_utils.py:45](agenticwhales/agents/utils/agent_utils.py)) also assumes sequential execution — it removes ALL messages from shared state.
Required design (next branch): encapsulate each analyst's `[analyst node → tool node → loop]` pipeline as a LangGraph subgraph with its own private state TypedDict containing a `messages` field. Each subgraph reads `company_of_interest` / `trade_date` from parent state and writes back only the relevant `*_report` field. Parent graph fan-outs from `START` to the 5 subgraphs; LangGraph's barrier semantics ensure Bull Researcher fires once after all subgraphs complete.
Files (estimated): new `agenticwhales/graph/analyst_subgraph.py` (defines the per-analyst subgraph factory + state schema), refactor of [agenticwhales/graph/setup.py:80-194](agenticwhales/graph/setup.py) to fan out, retire of [agenticwhales/graph/conditional_logic.py](agenticwhales/graph/conditional_logic.py)'s per-analyst `should_continue_*` functions (replaced by per-subgraph internal logic).
Acceptance unchanged: a five-analyst run completes in ~max(analyst latency) instead of sum; wall-clock under half of the current sequential baseline. Integration test asserts both parallelism (via timing) and correctness (each report field still populated).

**P2.4 — WebSocket delta streaming. *Promoted ahead of P2.1 in this branch.***
Addresses: C10.
See full description below; lands first because it's self-contained and doesn't depend on the subgraph refactor.

**P2.2 — Parallelize blind round-1 debate openings.**
Addresses: C8.
Files: [agenticwhales/graph/setup.py:199-240](agenticwhales/graph/setup.py), [agenticwhales/agents/researchers/bull_researcher.py:30-36](agenticwhales/agents/researchers/bull_researcher.py), [agenticwhales/agents/risk_mgmt/](agenticwhales/agents/risk_mgmt/) (all three debaters).
Change: when `blind_first_round: True`, fork Bull and Bear from a common entry node and join before round 2; same shape for the three risk debaters' first turn.
Acceptance: with `blind_first_round: True`, round-1 turns observe wall-clock close to max(per-turn-latency) instead of sum.

**P2.3 — Anthropic prompt caching for synthesizer prompts.**
Addresses: C9.
Files: [agenticwhales/llm_clients/anthropic_client.py](agenticwhales/llm_clients/anthropic_client.py), [agenticwhales/agents/managers/research_manager.py](agenticwhales/agents/managers/research_manager.py), [agenticwhales/agents/managers/portfolio_manager.py](agenticwhales/agents/managers/portfolio_manager.py).
Change: split the synthesizer prompt into (a) static system + role + framing (cacheable) and (b) variable debate history (uncached). Use Anthropic's `cache_control` markers on (a). Measure cache hit rate.
Acceptance: synthesizer input-token cost drops by ~50% on representative debates; cache hit rate logged.

**P2.4 — WebSocket delta streaming.**
Addresses: C10.
Files: [web/runner.py:166-372](web/runner.py), web client (whatever consumes the events).
Change: emit `{"type": "report_delta", "section": ..., "appended": <new chunk>}` instead of the full accumulated string. Keep a `{"type": "report"}` snapshot on initial subscribe so late-joining clients get the full state.
Acceptance: byte-volume over a representative session drops by approximately the expected O(N) instead of O(N²) shape.

**P2.5 — Provider retry + lightweight circuit breaker. ✅ Done (retry shipped; full circuit-breaker as follow-up).**
Addresses: C12.
Files added: [agenticwhales/llm_clients/retry.py](agenticwhales/llm_clients/retry.py), [tests/test_llm_retry.py](tests/test_llm_retry.py) (10 new tests).
Files touched: [agenticwhales/graph/trading_graph.py](agenticwhales/graph/trading_graph.py) — `apply_retry(...)` wraps both upstream LLMs and every diversified-provider LLM.
Delivered: `apply_retry()` uses langchain's built-in `.with_retry()` with exponential jitter; defaults of 3 attempts come from `AGENTICWHALES_LLM_RETRY_ATTEMPTS` / `AGENTICWHALES_LLM_RETRY_JITTER` env vars. Per-provider failure counter (`record_provider_failure` / `record_provider_success` / `get_failure_counts`) provides the data layer for a full circuit breaker.
Remaining for full circuit-breaker (follow-up): open/half-open/closed state machine with cooldown, integration with the Phase 1 diversification fallback so an open circuit triggers a slot-level fallback rather than a hard error, and a `circuit_open` event emitted to the web UI.
Verified: all 109 tests pass (99 prior + 10 new retry tests), no regressions.

### Phase 3 — Validation & instrumentation (weeks 4-6)

**P3.1 — Walk-forward backtest harness. *Delivered in PR #4 (pending merge of five follow-ups).***
Addresses: C3.
Delivered: `tradingagents/backtest/{harness,decision_source,metrics,bars,runner}.py`, walk-forward fill-at-next-open semantics, `AgentGraphDecisionSource` wire, 119 lines of tests.
Remaining before this item is closed: the five PR-specific fixes in [pr4_assessment.md §5](pr4_assessment.md) — module rename to `agenticwhales/`, Sharpe risk-free correction, turnover metric, t-stat surfacing, and rewording of the constant-rating smoke-test claim. After those land, this item is complete.
Note: the constant-rating wire-check is *not* a validation of the agent system; the actual validation moved to **P3.5** below.

**P3.2 — Transaction cost model. *Partial — slippage/commission knobs delivered by PR #4; liquidity-bucket model remains.***
Addresses: C6.
Delivered: `SimulatedBroker` supports `slippage_bps`, `commission_per_share`, `commission_min` ([tradingagents/execution/brokers/simulated.py](https://github.com/mohitsudhakar/AgenticWhales/blob/worktree-live-trader-executor/tradingagents/execution/brokers/simulated.py)). CLI defaults to 5 bps slippage.
Remaining: (a) liquidity-bucket cost model — different bp/commission tiers for large / mid / small / micro cap — plumbed into the harness; (b) market-impact heuristic for backtests at larger AUMs (square-root model is fine for v1); (c) surface the bucket assignment and the gross-vs-net delta in the backtest summary report.
Acceptance: backtest summary reports gross Sharpe, net-of-cost Sharpe, and per-bucket cost attribution; the gross-vs-net delta is non-trivial on a turnover-heavy run.

**P3.3 — τ instrumentation.**
Addresses: C13.
Files: [web/runner.py](web/runner.py), new `agenticwhales/telemetry/` module.
Change: at each debate-end, compute (a) a "stranger-rejection rate" — when the synthesizer's verdict disagrees with a debater, is the disagreement uncorrelated with whether the debater's model family matches the synthesizer's; (b) decision tier distribution by synthesizer provider; (c) per-agent latency and token cost histograms. Emit as structured logs and a Prometheus endpoint.
Acceptance: dashboard shows τ-proxy time series; we can answer "is the Heterogeneity Mandate doing what it claims on our traffic" with data instead of citations.

**P3.4 — Same-day same-ticker cache.**
Addresses: latency + cost, not in the table but a quick win.
Files: new `agenticwhales/cache/` module, hooked into the analyst tool nodes and into `create_llm_client`.
Change: cache key `{ticker, date, agent, tool_call_args_hash, prompt_hash}` with a TTL that expires at next market close. Both data fetches and LLM completions are cacheable.
Acceptance: re-running AAPL on 2026-05-19 reuses cached responses and completes substantially faster than the first run.

**P3.5 — Agent-driven backtest baseline. *New, unlocked by PR #4.***
Addresses: C3 properly (the PR #4 constant-rating wire-check is not a validation), and is the data prerequisite for C13, C15, C17.
Files: new `agenticwhales/backtest/configs/sp100_2022_2024_weekly.yaml`, results checked in under `docs/backtests/`.
Change: run `AgentGraphDecisionSource` over the S&P 100 universe, 2022-01-01 → 2024-12-31, `rebalance_every_n_bars=5` (weekly), default `SizingPolicy`, default 5 bps slippage. Report gross and net Sharpe, turnover, max drawdown, t-stat against zero excess return, and attribution by tier-rating bucket.
Acceptance: a markdown report at `docs/backtests/baseline_v1.md` with numbers, charts, and an honest one-paragraph "do these agents have edge" verdict. This is the gating data point for every Phase 4 design decision; without it, Phase 4 is unfalsifiable.

### Phase 4 — Architectural (next quarter)

**P4.1 — Replace the 5-tier rating with a continuous output schema.**
Addresses: C7, partial C15.
Files: [agenticwhales/agents/schemas.py:171](agenticwhales/agents/schemas.py) (`PortfolioDecision`), all downstream consumers (Trader, Portfolio Manager, web UI, backtest).
Change: emit `expected_return_30d`, `confidence_low`, `confidence_high`, `recommended_position_size_pct`, alongside (not in place of) the existing tier for the web UI. Position size follows from a variance-budget rule, not from the synthesizer's qualitative judgment.
Acceptance: backtest can compute portfolio-level PnL using the continuous output; attribution can be done by quantile of `expected_return_30d` or by confidence band.

**P4.2 — Diversify the analyst stack.**
Addresses: C4.
Files: [agenticwhales/graph/trading_graph.py](agenticwhales/graph/trading_graph.py), [agenticwhales/graph/setup.py:85-121](agenticwhales/graph/setup.py), [agenticwhales/default_config.py](agenticwhales/default_config.py).
Change: extend the diversification machinery to analysts. Add `diversify_analysts: bool` and `analyst_provider_preference: list[str]`; assign each of Market / Social / News / Fundamentals / Quant to a different provider where credentials allow. Falls back gracefully.
Acceptance: with three or more analyst providers available, no two analysts share a provider.

**P4.3 — Outcome-grounded learning loop.**
Addresses: C15.
Files: new `agenticwhales/learning/` module, integrated with the memory layer and the synthesizer judges.
Change: build a small judge model (or LoRA adapter) trained via DPO on pairs `(debate, decision_a, decision_b)` where `decision_a` had higher realized risk-adjusted return than `decision_b` over the documented horizon. Replace the synthesizer system prompt's "Authority Framing" with the trained judge. Keep the prompt version as fallback. Gate behind a config flag.
Acceptance: side-by-side backtest of prompted-synthesizer vs. trained-judge over a held-out year shows the trained judge has lower decision-tier calibration error.

**P4.4 — Decision-space tree search.**
Addresses: C16.
Files: new node between `Research Manager` and `Trader`, optional and feature-flagged.
Change: at each decision point, expand 3-5 candidate trade hypotheses (tier × size × stop × horizon), score each with a value head (initially: the trained judge from P4.3), keep the top-1. The judge model produces the value estimate; the search makes the decision.
Acceptance: backtest with the search node enabled has measurably better risk-adjusted return than the linear-debate baseline.

**P4.5 — Validate the Quant Analyst's six dimensions.**
Addresses: C17.
Files: new `tests/validation/test_quant_radar_predictive.py`.
Change: for each of the six dimensions in `QuantRadar`, plot its forecast-vs-realized scatter at the relevant horizon across the backtest universe. Drop any dimension that fails to correlate with forward returns above a documented threshold.
Acceptance: each surviving dimension has a documented IC (information coefficient) and a documented half-life.

**P4.6 — Variance-budget sizing (replaces drawdown-conditional risk).**
Addresses: C14.
Files: new `agenticwhales/execution/sizing_variance_budget.py` (alternative `SizingPolicy` implementation alongside the default one delivered in PR #4), [agenticwhales/agents/managers/portfolio_manager.py:42-51](agenticwhales/agents/managers/portfolio_manager.py) (remove drawdown-conditional language from the prompt).
Change: implement an alternative `SizingPolicy` whose `target_qty()` solves for share count under a portfolio-variance budget given a configurable per-name volatility estimate and target portfolio vol. Wire as a config option. Remove the "tighten on drawdowns, loosen on recent alpha" language from the PM prompt — that rule belongs in the sizing layer where it can be a fixed variance budget rather than a regret-driven heuristic.
Acceptance: P3.5 backtest re-run with the variance-budget policy shows no worse risk-adjusted return and substantially smaller realized drawdowns than the default `SizingPolicy`.
Note: PR #4 created the right architectural surface (`SizingPolicy`) for this fix. What was previously a prompt rewrite is now a ~100-line additional policy class.

**P4.7 — Provider-agnostic LLM proxy.**
Addresses: consolidates C9, C10, C12, and the prompt caching from P2.3 into one place.
Files: refactor of `agenticwhales/llm_clients/`.
Change: a single proxy in front of `create_llm_client` that owns batching, per-provider rate-limit awareness, retry + circuit breaker, prompt cache, structured logs, and per-call telemetry. Per-provider clients become thin adapters under the proxy.
Acceptance: every concern listed above is configured in one place; per-provider clients shrink to ~50 lines each.

---

## 4. Out of scope / parking lot

- Replacing yfinance with a paid market data vendor. The current design works as long as the backtest harness uses point-in-time snapshots; vendor swap is a Phase 5+ concern and is its own project.
- Reinforcement learning over multiple decisions (vs. single-decision DPO in P4.3). Premature until P4.1 and P4.3 are in place.
- Options, futures, crypto. The decision schema, the cost model, and the validation harness are all equity-shaped. Multi-asset is a future expansion, not a current concern.
- UI redesign. The "live debate" stream is the most engaging part of the product; any redesign happens after P2.4 and P1.3 land so it's built on the right event shape.

---

## 5. Definition of done for `review_fix`

The branch ships when:

- All Phase 1 items (P1.1–P1.4) merged with passing tests in CI.
- The two critical-severity defects (C1 and C2) are fixed with explicit regression tests.
- The diversification status surface (P1.3) is live in the web UI.
- Phase 2 items have tracking issues opened and the largest (P2.1 — parallel analysts) has a draft PR.
- Phase 3 and Phase 4 items have one-paragraph design notes filed under `docs/design/`.

After that, this branch's job is done; Phase 2 onward gets its own branches.
