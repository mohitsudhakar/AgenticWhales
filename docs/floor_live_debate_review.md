# Floor Live Debate — Agent & Model Review

*Generated 2026-05-19. Scope: the live two-stage debate (Bull/Bear → Research Manager; Aggressive/Conservative/Neutral → Portfolio Manager) and the analysts that feed it. Citations are to file:line at HEAD (commit `dbb5fd3`).*

---

## TL;DR

- The "floor" is a LangGraph pipeline that streams two debates to the web UI and CLI: an **investment debate** (Bull vs. Bear, judged by the Research Manager) and a **risk debate** (Aggressive/Conservative/Neutral, judged by the Portfolio Manager).
- Model assignment is *role-aware*: analysts and trader use a "quick" upstream LLM, debaters are spread across a `debater_provider_preference` list, and the two synthesizers are pulled from a separate `synthesizer_provider_preference` list. The intent — citing Shehata & Li (2026) — is to break correlated bias between upstream and synthesizer.
- The intent is good and the theoretical hooks are well-cited. The **execution** has three concrete defects worth fixing now (see "Concrete issues" below):
  1. With only 2 entries in `debater_provider_preference`, **Neutral and Aggressive collide on the same provider** despite the comment promising adjacent debaters never match.
  2. **Both synthesizers (Research Manager and Portfolio Manager) resolve to the same provider** under the default config, undermining the Heterogeneity Mandate at the gate that matters most.
  3. The five upstream analysts are wired **sequentially** even though they share no state — a trivially parallelizable critical path.

---

## 1. Pipeline map

```
START
  └─ Market Analyst ──► Social ──► News ──► Fundamentals ──► Quant   (chained, all on quick_thinking_llm)
        (each loops with its tool node until done)
                                                                 │
                                                                 ▼
                              ┌────────── Bull Researcher ◄───────┐
                              │   blind round 1, then rebuttals   │
                              └──► Bear Researcher ───────────────┘
                                          │
                                          ▼  count ≥ 2 * max_debate_rounds (default 4 turns)
                                  Research Manager  ──► investment_plan (typed ResearchPlan)
                                          │
                                          ▼
                                       Trader  ──► trader_investment_plan (typed TraderProposal)
                                          │
                                          ▼
                          ┌── Aggressive ─► Conservative ─► Neutral ──┐
                          └───────────────── round-robin ─────────────┘
                                          │
                                          ▼  count ≥ 3 * max_risk_discuss_rounds (default 6 turns)
                                  Portfolio Manager ──► final_trade_decision (typed PortfolioDecision)
                                          │
                                          ▼
                                         END
```

References: [agenticwhales/graph/setup.py:152](agenticwhales/graph/setup.py:152) (workflow assembly), [agenticwhales/graph/conditional_logic.py:54](agenticwhales/graph/conditional_logic.py:54) (`should_continue_debate`), [agenticwhales/graph/conditional_logic.py:65](agenticwhales/graph/conditional_logic.py:65) (`should_continue_risk_analysis`).

---

## 2. Agent → LLM map (resolved with default config)

The defaults in [agenticwhales/default_config.py](agenticwhales/default_config.py) — assuming `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, and `DEEPSEEK_API_KEY` are all set — resolve as follows. Catalog entries come from [agenticwhales/llm_clients/model_catalog.py](agenticwhales/llm_clients/model_catalog.py).

### Analysts (upstream of the debate)

| Agent | File | LLM slot bound | Resolved model |
|---|---|---|---|
| Market Analyst | [agenticwhales/agents/analysts/market_analyst.py:11](agenticwhales/agents/analysts/market_analyst.py:11) | `quick_thinking_llm` | OpenAI **gpt-5.4-mini** |
| Social Media Analyst | [agenticwhales/agents/analysts/social_media_analyst.py:6](agenticwhales/agents/analysts/social_media_analyst.py:6) | `quick_thinking_llm` | OpenAI **gpt-5.4-mini** |
| News Analyst | [agenticwhales/agents/analysts/news_analyst.py:11](agenticwhales/agents/analysts/news_analyst.py:11) | `quick_thinking_llm` | OpenAI **gpt-5.4-mini** |
| Fundamentals Analyst | [agenticwhales/agents/analysts/fundamentals_analyst.py:14](agenticwhales/agents/analysts/fundamentals_analyst.py:14) | `quick_thinking_llm` | OpenAI **gpt-5.4-mini** |
| Quant Analyst (new, `dbb5fd3`) | [agenticwhales/agents/analysts/quant_analyst.py:47](agenticwhales/agents/analysts/quant_analyst.py:47) | `quick_thinking_llm`, then bound to a typed `QuantRadar` schema | OpenAI **gpt-5.4-mini** |

Wired at [agenticwhales/graph/setup.py:85-121](agenticwhales/graph/setup.py:85). All five share the same model — the chain is a homogeneous upstream.

### Investment debate

| Agent | File | LLM slot bound | Resolved model (default) |
|---|---|---|---|
| Bull Researcher | [agenticwhales/agents/researchers/bull_researcher.py:3](agenticwhales/agents/researchers/bull_researcher.py:3) | `debater_llms["bull"]` → `usable[0]` | Google **gemini-3-flash-preview** |
| Bear Researcher | [agenticwhales/agents/researchers/bear_researcher.py:3](agenticwhales/agents/researchers/bear_researcher.py:3) | `debater_llms["bear"]` → `usable[1 % len]` | DeepSeek **deepseek-v4-flash** |
| **Research Manager** (judge) | [agenticwhales/agents/managers/research_manager.py:13](agenticwhales/agents/managers/research_manager.py:13) | `research_manager_llm` from `synthesizer_provider_preference` first non-upstream | Anthropic **claude-opus-4-6** |

Assignment logic: [agenticwhales/graph/trading_graph.py:252-311](agenticwhales/graph/trading_graph.py:252).

### Risk debate

| Agent | File | LLM slot bound | Resolved model (default) |
|---|---|---|---|
| Aggressive Analyst | [agenticwhales/agents/risk_mgmt/aggressive_debator.py:3](agenticwhales/agents/risk_mgmt/aggressive_debator.py:3) | `debater_llms["aggressive"]` → `usable[0 % len]` | Google **gemini-3-flash-preview** |
| Conservative Analyst | [agenticwhales/agents/risk_mgmt/conservative_debator.py:3](agenticwhales/agents/risk_mgmt/conservative_debator.py:3) | `debater_llms["conservative"]` → `usable[1 % len]` | DeepSeek **deepseek-v4-flash** |
| Neutral Analyst | [agenticwhales/agents/risk_mgmt/neutral_debator.py:3](agenticwhales/agents/risk_mgmt/neutral_debator.py:3) | `debater_llms["neutral"]` → `usable[2 % len]` | Google **gemini-3-flash-preview** ⚠️ collides with Aggressive |
| **Portfolio Manager** (judge) | [agenticwhales/agents/managers/portfolio_manager.py:24](agenticwhales/agents/managers/portfolio_manager.py:24) | `portfolio_manager_llm` from `synthesizer_provider_preference` first non-upstream | Anthropic **claude-opus-4-6** ⚠️ same as Research Manager |

### Off-debate but adjacent

| Agent | File | LLM slot | Resolved model |
|---|---|---|---|
| Trader | [agenticwhales/agents/trader/](agenticwhales/agents/trader/) (created [agenticwhales/graph/setup.py:134](agenticwhales/graph/setup.py:134)) | `quick_thinking_llm` | OpenAI **gpt-5.4-mini** |
| Reflector | [agenticwhales/graph/reflection.py](agenticwhales/graph/reflection.py) (wired [agenticwhales/graph/trading_graph.py:135](agenticwhales/graph/trading_graph.py:135)) | `quick_thinking_llm` | OpenAI **gpt-5.4-mini** |
| Signal Processor | [agenticwhales/graph/signal_processing.py](agenticwhales/graph/signal_processing.py) (wired [agenticwhales/graph/trading_graph.py:136](agenticwhales/graph/trading_graph.py:136)) | `quick_thinking_llm` | OpenAI **gpt-5.4-mini** |

---

## 3. Model selection mechanism (how the slots are filled)

Three independent decision paths in [agenticwhales/graph/trading_graph.py:80-311](agenticwhales/graph/trading_graph.py:80):

1. **Upstream (`quick_thinking_llm`, `deep_thinking_llm`)** — built from `config["llm_provider"]` + `config["quick_think_llm"]` / `config["deep_think_llm"]` via the `create_llm_client` factory ([agenticwhales/llm_clients/factory.py](agenticwhales/llm_clients/factory.py)). One provider, two model SKUs.
2. **Synthesizers** — `_build_diversified_synthesizer_llm()` ([trading_graph.py:210](agenticwhales/graph/trading_graph.py:210)) walks `synthesizer_provider_preference`, skipping any provider that matches upstream or lacks credentials. Picks the catalog's first "deep" model for that provider.
3. **Debaters** — `_build_debater_llms()` ([trading_graph.py:252](agenticwhales/graph/trading_graph.py:252)) walks `debater_provider_preference`, filters to providers with credentials, then assigns bull/aggressive → `usable[0]`, bear/conservative → `usable[1 % len]`, neutral → `usable[2 % len]`.

Factory dispatch: OpenAI-compatible providers (openai, xai, deepseek, qwen, glm, ollama, openrouter) all use `OpenAIClient` under the hood — a nice piece of consolidation. Anthropic, Google, and Azure each have their own client.

---

## 4. The "live" surface

### Web (WebSocket)
- Streaming loop: [web/runner.py:320-371](web/runner.py:320). The `SessionRunner._run()` watches each `graph.stream()` chunk for `investment_debate_state` and `risk_debate_state`, deduplicates by comparing against `last["bull"|"bear"|"agg"|...]`, and pushes `{"type": "report", ...}` events.
- Status events: [web/runner.py:166](web/runner.py:166) `_set_report` → `_broadcast`; agent status transitions emit `{"type": "agent_status", ...}`.
- Team grouping for the UI: [web/runner.py:43-48](web/runner.py:43) — Research Team / Trading Team / Risk Management / Portfolio Management.

### CLI
- [cli/main.py:1091-1110](cli/main.py:1091). Same chunk loop, but updates Rich panels in place.

Both surfaces are read-only views over the same `graph.stream()`; there is no separate debate orchestration loop.

---

## 5. Theoretical foundations as cited in code

The configuration is heavily anchored to a single 2026 paper. Relevant claims, with the cite-bearing lines:

- **Synthesizer Gating Theorem (Shehata & Li 2026, Thm 1)** — [default_config.py:42-48](agenticwhales/default_config.py:42), [trading_graph.py:213-218](agenticwhales/graph/trading_graph.py:213). Terminal swarm integrity gated by the synthesizer's "Tribalism Coefficient τ"; same-family-as-upstream pushes the Attention Latch Λ → 2 and error → 1.0.
- **Resilience Inequality (Cor. 1)** — Heterogeneity Mandate: the synthesizer node must be architecturally distinct ([default_config.py:46-48](agenticwhales/default_config.py:46)).
- **Sycophantic Scaling Law** — motivates dropping `max_debate_rounds` from 5 to 2 ([default_config.py:87-91](agenticwhales/default_config.py:87)).
- **Peer Pressure / kinship-locked upstream (Table 1)** — motivates `diversify_debaters` and per-debater model spread ([default_config.py:62-69](agenticwhales/default_config.py:62)).
- **Tribalism τ measurements (Table 2)** — justifies `synthesizer_provider_preference: ["anthropic", "deepseek", "google"]` ([default_config.py:55-60](agenticwhales/default_config.py:55)).
- **QuantAgent 2025 style 6-dim radar** — motivates the new Quant Analyst's typed `QuantRadar` ([agenticwhales/agents/analysts/quant_analyst.py:12](agenticwhales/agents/analysts/quant_analyst.py:12)).
- **FinMem / Yu et al. (2023) Table 5** — motivates `memory_top_k_per_layer: 5` and the layered reflection cadence ([default_config.py:103-110](agenticwhales/default_config.py:103)).

---

## 6. Demis-style review

*Frame: is this scientifically serious? Is the architecture pushing toward general intelligence on the underlying problem (well-calibrated trading decisions under uncertainty), or is it engineering decoration over the same brittle prompt?*

**What I like.**

1. **You named the failure modes.** Most "multi-agent" systems wave at "diversity of perspectives" without specifying *why*. The Heterogeneity Mandate is the right level of analysis — correlated upstream errors propagate through a single synthesizer regardless of how many debaters you bolt on. The fact that the comments cite a specific theorem and a specific table of measured τ values is the kind of intellectual seriousness I want to see. [default_config.py:42-69](agenticwhales/default_config.py:42)
2. **Independence in round 1.** `blind_first_round` ([default_config.py:100](agenticwhales/default_config.py:100), implementation [bull_researcher.py:30-36](agenticwhales/agents/researchers/bull_researcher.py:30)) is exactly the right move: it preserves the independence condition for any wisdom-of-crowds claim. This is the same intuition that drives independent samples in ensemble methods. Cheap, principled, well-motivated.
3. **Structured outputs as a second modality.** The new Quant Analyst emits a typed `QuantRadar` ([schemas.py:233](agenticwhales/agents/schemas.py:233)). Forcing a numeric 6-dim signal alongside the prose debate is doing the equivalent of multi-modal fusion — it gives the synthesizers a channel that isn't gameable by rhetorical fluency. This is the strongest single design choice in the system.
4. **Typed plans across the pipeline** — `ResearchPlan`, `TraderProposal`, `PortfolioDecision`, `QuantRadar` ([schemas.py:61,109,171,233](agenticwhales/agents/schemas.py:61)). Decisions are first-class data, not free text. That alone makes the system evaluable.

**Where it falls short of the standard I'd want.**

1. **No learning loop.** Everything is hand-tuned. The Reflector and the FinMem-style layered memory ([default_config.py:101-110](agenticwhales/default_config.py:101)) are *retrieval*, not learning. You have a system that produces graded decisions and observes outcomes (prices move, the trade wins or loses) — and that outcome never updates the policy. No reward model, no DPO on synthesizer judgements against realized PnL, not even a thin baseline that re-weights memory by outcome. This is the AlphaGo-vs-expert-system gap. The agents will be as smart in a year as they are today, modulo whatever the upstream providers do for you.
2. **The debate is linear, not a search.** Each debate is a fixed-length round-robin. The structurally interesting move — at every node, expand multiple alternative arguments, score them with a value head, and only keep the most promising branch — never happens. This is the AlphaZero idea in a setting that begs for it: the candidate-action space (Buy/Overweight/Hold/Underweight/Sell × position size × stops × time horizon) is small enough to tree-search over.
3. **No theory of mind between debaters.** Bull doesn't simulate what Bear will say; Bear doesn't simulate what Bull will say. They take turns. With strong models on both sides, you'd get much sharper opening positions if each side were allowed an internal "what's the strongest objection?" pass before speaking — a la Negotiation/Debate work from Irving et al. The blind first round preserves *independence* but throws away *strategic anticipation*.
4. **Discrete 5-tier ratings throw away information.** [research_manager.py:43-48](agenticwhales/agents/managers/research_manager.py:43) and [portfolio_manager.py:74-84](agenticwhales/agents/managers/portfolio_manager.py:74) emit Buy/Overweight/Hold/Underweight/Sell. An AI-native design would emit a distribution over outcomes (or at minimum a calibrated probability + expected return + variance), and let the Trader do Kelly-style sizing. The current setup is mimicking sell-side analyst conventions for human consumption.
5. **The empirical claim leans on a single recent paper.** Shehata & Li (2026) is cited as if its τ measurements were settled science. They are not. The same code that cites the paper should be *measuring* τ on this system's own debates (synthesizer correction-rejection rate, decision-disagreement-vs-PnL correlation) and surfacing it in a dashboard. Cite the paper *and* validate it on your data. Right now this is theology, not science.
6. **The Heterogeneity Mandate is silently broken at the most important seam.** Both synthesizers — Research Manager and Portfolio Manager — resolve to the same provider under the default config (anthropic/claude-opus-4-6). Portfolio Manager is downstream of Research Manager's `investment_plan`, so it has Research Manager as an upstream signal source. The Mandate's whole argument is that the synthesizer must be architecturally distinct from its upstream. Under the defaults, you've reproduced the exact kinship-locked pattern you wrote the paper-citing comments to avoid. (See "Concrete issues #2".)
7. **No counterfactual evaluation.** The whole pipeline can be run on historical dates. There's no scaffolding I can see for "run debate on 2025-01-15 with information cutoff 2025-01-14 and grade against the next 30 trading days." Without that, every design choice (debate rounds, blind opening, model assignment) is unfalsifiable.

**One ambitious thing I'd build next.** Make the Portfolio Manager output a posterior over realized 30-day PnL distributions rather than a tier. Backtest. Use the calibration error as a reward signal to fine-tune a small judge model that replaces the synthesizer prompt. Now you have a system that gets sharper with experience instead of with prompt edits.

---

## 7. Jeff-Dean-style review

*Frame: how does this run in production? What's the critical path, what's the cost per decision, where does it break, what's missing for observability?*

**Critical path.** With the default config and one ticker:

- 5 analysts × ~1 LLM call each (more with tool loops) — **sequential** ([setup.py:178-194](agenticwhales/graph/setup.py:178))
- Bull/Bear: `2 × max_debate_rounds = 4` sequential calls
- Research Manager: 1
- Trader: 1
- Risk debate: `3 × max_risk_discuss_rounds = 6` sequential calls
- Portfolio Manager: 1

→ ~18 sequential LLM calls per ticker, plus tool-loop iterations inside each analyst. At even 4s per call, that's 70+ seconds wall-clock with zero parallelism. For a multi-ticker portfolio sweep, this compounds linearly.

**Things I would fix this quarter.**

1. **Parallelize the analysts.** They are wired sequentially ([setup.py:178-194](agenticwhales/graph/setup.py:178): `add_edge(current_clear, next_analyst)`). They share no state — each just appends its own report. Fan them out from `START`, fan in before `Bull Researcher`. Expected wall-clock reduction: ~4× on the analyst stage.
2. **Parallelize the blind opening turn of every debate.** With `blind_first_round: True`, Bull and Bear's first turn are by construction independent (each hides the other's output, [bull_researcher.py:30-36](agenticwhales/agents/researchers/bull_researcher.py:30), [bear_researcher.py:25-32](agenticwhales/agents/researchers/bear_researcher.py:25)). Same for the first turn of all three risk debaters ([aggressive_debator.py:31-38](agenticwhales/agents/risk_mgmt/aggressive_debator.py:31)). The graph still serializes them. Fork on first round, sync, then go serial for rebuttals. Saves 1 + 2 sequential calls per debate.
3. **The Neutral/Aggressive collision bug.** [trading_graph.py:298-303](agenticwhales/graph/trading_graph.py:298) computes `result["neutral"] = usable[2 % len(usable)][1]`. With the default `debater_provider_preference: ["google", "deepseek"]` (len 2), this is `usable[0]` — same as Aggressive. The code comment at [trading_graph.py:264-269](agenticwhales/graph/trading_graph.py:264) promises "adjacent debaters in the round-robin never match," and then the code doesn't enforce it. The fix: require `len(usable) >= 3` for the three-way risk debate, or rotate the assignment so collisions land on the non-adjacent pair (Agg/Neu instead of Agg/Con or Con/Neu — but in a circular round-robin they are *all* adjacent). The cleanest fix is to add a third provider to the default preference list.
4. **The two synthesizers resolve to the same provider.** [trading_graph.py:210-243](agenticwhales/graph/trading_graph.py:210) walks `synthesizer_provider_preference` and picks the first non-upstream provider. Called twice (Research Manager, Portfolio Manager) with the same inputs → same answer. The `role` parameter is passed in and used only for logging ([line 240](agenticwhales/graph/trading_graph.py:240)). The fix is one line of bookkeeping — pick `usable[0]` for `research_manager`, `usable[1]` for `portfolio_manager`. Today's default leaves the Heterogeneity Mandate inert at the second synthesizer.
5. **WebSocket bandwidth is O(N²) over a debate.** [web/runner.py:328-339](web/runner.py:328) broadcasts the full `bull_history` / `bear_history` string on every change — and that history grows monotonically. Each broadcast retransmits everything said so far. For 4 investment debate turns + 6 risk debate turns × ~2KB/turn × many connected clients, this adds up. Switch to delta streaming: emit only the new turn, let the client concatenate. Free 2-5× bandwidth reduction.
6. **Per-turn prompts are O(N²) tokens.** Same issue at the LLM layer: each debate turn's prompt re-includes the full `history` ([bull_researcher.py:38-41](agenticwhales/agents/researchers/bull_researcher.py:38)). On Anthropic, use the Messages API with explicit turn structure + prompt caching on the static system prompt + report payload. This alone could cut synthesizer cost by 50%+ on long debates.
7. **No batching across tickers.** A portfolio sweep over N tickers runs N independent pipelines. The synthesizer prompts are independent — they're ideal candidates for Anthropic's Batch API (50% discount, async). The web side has some batch plumbing already (commit `f48f409` "cancel in-flight sessions and batches"); push it down into the synthesizer layer.
8. **Silent fallback hides degradation.** [trading_graph.py:245-250](agenticwhales/graph/trading_graph.py:245): when no diversification candidate is usable, the system falls back to the upstream deep-think LLM and only logs at INFO. Operationally this means a missing `ANTHROPIC_API_KEY` silently disables the Heterogeneity Mandate while still emitting decisions with the same confidence. Promote to WARN, surface it on the web UI as a per-session banner ("Synthesizer diversification: DEGRADED — synthesizer is on upstream provider"), and consider failing closed in production.
9. **No retries / circuit breakers visible in client code.** [llm_clients/factory.py](agenticwhales/llm_clients/factory.py) and the per-provider clients hand back a langchain LLM and trust the upstream SDK. For a system that does 18+ sequential calls per ticker on third-party APIs, you need bounded retries with jitter and a per-provider circuit breaker. A single 429 from one provider mid-debate currently kills the whole session.
10. **No cache for same-ticker-same-day.** Two runs of AAPL on 2026-05-19 re-fetch the same yfinance data and re-run the same analyst prompts. Add a deterministic cache key (`{ticker, date, analyst, tool_call_args_hash}`) with a TTL keyed off market close.

**Observability gaps.**

- No structured metrics. The system makes load-bearing claims about τ (Tribalism Coefficient), Λ (Attention Latch), and σ scaling, and then measures none of them on its own traffic. Instrument:
  - Synthesizer-vs-debater agreement rate (proxy for τ).
  - Decision tier distribution over time, segmented by synthesizer provider.
  - Per-agent latency and token cost histograms.
  - Round-2 rebuttal length vs. round-1 opening length (proxy for whether rebuttals are substantive or sycophantic).
- No tracing. With 18 sequential LLM calls per decision, OpenTelemetry spans per node would make diagnosing slow runs trivial.
- Logging is `logger.info` strings ([trading_graph.py:239-243, 245-249, 310](agenticwhales/graph/trading_graph.py:239)). Structured logs with provider/role/tokens/latency fields would let you analyze provider performance across runs.

**One ambitious thing I'd build next.** A provider-agnostic LLM proxy in front of `create_llm_client` that does: request batching, per-provider rate-limit awareness, retry/circuit-breaker, prompt cache, and per-turn streaming-vs-non-streaming selection based on whether a WebSocket client is connected. Today every one of those concerns is either missing or split across the per-provider client classes.

---

## 8. Concrete issues found (highest impact first)

| # | Issue | Where | Severity |
|---|---|---|---|
| 1 | Both synthesizers (Research Manager, Portfolio Manager) resolve to the **same provider** under defaults — defeats the Heterogeneity Mandate at the seam where it matters most | [trading_graph.py:210-243](agenticwhales/graph/trading_graph.py:210) | **High** |
| 2 | With `debater_provider_preference: ["google", "deepseek"]` (default), Neutral and Aggressive land on the same provider despite the code comment promising otherwise | [trading_graph.py:298-303](agenticwhales/graph/trading_graph.py:298) + [default_config.py:79](agenticwhales/default_config.py:79) | **High** |
| 3 | Analyst chain runs serially despite zero shared state | [graph/setup.py:178-194](agenticwhales/graph/setup.py:178) | **High** (latency) |
| 4 | Blind-round-1 debate turns serialize unnecessarily | [graph/setup.py:199-240](agenticwhales/graph/setup.py:199) | Medium (latency) |
| 5 | WebSocket re-sends full debate history each turn (O(N²) bandwidth) | [web/runner.py:328-371](web/runner.py:328) | Medium |
| 6 | Per-turn LLM prompts re-include full debate history (O(N²) tokens, no cache directives) | [bull_researcher.py:38-41](agenticwhales/agents/researchers/bull_researcher.py:38), [aggressive_debator.py:43-48](agenticwhales/agents/risk_mgmt/aggressive_debator.py:43) | Medium (cost) |
| 7 | Diversification fallback logs at INFO; can silently degrade in prod | [trading_graph.py:245-250, 289-294](agenticwhales/graph/trading_graph.py:245) | Medium |
| 8 | No retries, no circuit breaker around provider calls — one 429 kills the session | [llm_clients/factory.py](agenticwhales/llm_clients/factory.py) | Medium |
| 9 | No outcome-grounded learning loop — Reflector is retrieval, not policy update | system-wide | Long-term |
| 10 | No τ / Λ / σ instrumentation on real traffic — the system can't validate its own theoretical claims | system-wide | Long-term |

---

## 9. Suggested next steps (in priority order)

1. **Fix issue #1 in 10 lines:** in `_build_diversified_synthesizer_llm`, accept an `exclude: set[str]` of providers already taken by other synthesizers, and pass `{research_manager_provider}` when building the portfolio manager. Use `role` to disambiguate.
2. **Fix issue #2 by adding `"openai"` or `"xai"` to the default `debater_provider_preference`**, so the round-robin doesn't collapse. Or detect `len(usable) < 3` and fall back to upstream for the third slot with a WARN.
3. **Parallelize analysts (issue #3).** This is the single largest wall-clock win and a straightforward LangGraph refactor — fan out from `START`, join at `Bull Researcher`.
4. **Add a "diversification status" panel to the web UI** showing the resolved provider for every role and a red badge when the Mandate is degraded.
5. **Add a counterfactual backtest harness.** Take a list of (ticker, date) pairs, freeze the data sources to point-in-time snapshots, run the pipeline, score against forward-looking PnL. Without this, none of the heterogeneity claims are testable on your own data.
6. **Instrument τ.** Measure the synthesizer's "stranger-rejection rate" — how often the judge sides with the debater whose model family matches its own. Plot it per session. If the Heterogeneity Mandate is doing what it claims, this should hover near 50%; if it's near 100% you have a problem the prompts aren't fixing.
