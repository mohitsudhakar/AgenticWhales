# AgenticWhales — End-to-End Guidebook

> **What this doc is.** A practical, code-grounded walkthrough of how to stand
> AgenticWhales up *and* how the quant engine actually decides. The
> [README](README.md) is the canonical ops reference (full env table, CLI
> surface, troubleshooting); this guide is the connective tissue — it ties
> setup → inference → services → the decision pipeline → autonomy → deployment
> into one mental model, and it opens the hood on the quant so you know what
> the numbers mean.
>
> **Not financial advice.** Everything here is paper-trading / research. See the
> disclaimer in the README.

---

## 0. The one-sentence model

A ticker goes through a **multi-agent LLM debate** (analysts → bull/bear
researchers → trader → 3-way risk debate → portfolio manager), the PM emits a
**structured `PortfolioDecision`**, that decision passes a **RiskGuard** and a
**fractional-Kelly sizer** into a **paper-trading book**, and the book's
realized outcomes feed **calibration, memory, and behavioral** subsystems back
into future runs. Everything — HTTP, scheduler, streaming, crons — runs in
**one uvicorn process**.

```
data → [analysts] → [bull⇄bear debate] → [research mgr] → [trader]
       → [aggressive⇄conservative⇄neutral risk debate] → [portfolio mgr]
       → PortfolioDecision → RiskGuard → Kelly sizing → paper fill
       → decision_outcomes (Brier) → calibration + memory + behavioral
```

---

## 1. Quickstart — fastest path to a real decision

```bash
git clone <repo> && cd AgenticWhales
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[web]'

# Minimum viable inference: one provider key. Google is the default.
echo 'GOOGLE_API_KEY=...'  >> .env

# Run everything (in-memory persistence; no Postgres needed):
agenticwhales-web          # serves /fund and /analyze on :8765
```

Then either:

- **Web:** open `http://localhost:8765/fund`, type a ticker, run an analysis.
- **CLI:** `agenticwhales analyze` (interactive picker).
- **Python:**
  ```python
  from agenticwhales.graph.trading_graph import AgenticWhalesGraph
  from agenticwhales.default_config import DEFAULT_CONFIG
  g = AgenticWhalesGraph(["market", "quant", "news"], config=DEFAULT_CONFIG)
  final_state, decision = g.propagate("AAPL", "2024-06-03")
  ```

With **no LLM key**, the graph can't run a live debate — but the CLI `backtest`
sub-command uses a deterministic stub LLM, so you can still exercise the full
sizing/risk/fill pipeline offline.

---

## 2. Models & inference setup

### 2.1 Providers

Eight provider families are wired through one factory
(`agenticwhales/llm_clients/factory.py`): **OpenAI, Anthropic, Google,
Azure OpenAI, xAI, DeepSeek, Qwen (DashScope), GLM (Zhipu), OpenRouter,
Ollama**. Set the matching key (`GOOGLE_API_KEY`, `DEEPSEEK_API_KEY`,
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …). You only need keys for the
providers you actually select.

The clients normalize provider quirks behind `BaseLLMClient`:
- list-shaped content blocks (OpenAI Responses API, Gemini 3) are flattened to
  plain strings (`normalize_content`);
- reasoning/thinking controls map to a uniform surface
  (`google_thinking_level`, `openai_reasoning_effort`, `anthropic_effort`).

### 2.2 Two model tiers + architectural diversity

Every run uses **two tiers**:

| Role | Config key | Default |
|---|---|---|
| Analyst-grade ("quick") | `quick_think_llm` | `gemini-3-flash-preview` |
| Manager-grade ("deep") | `deep_think_llm` | `gemini-3.1-pro-preview` |

On top of that, `default_config.py` turns on **diversification heuristics**
(all opt-out):

- `diversify_synthesizers: True` — the Research Manager and Portfolio Manager
  are pulled from `synthesizer_provider_preference`
  (`["anthropic", "deepseek", "google"]`) so the synthesizer isn't the same
  model family as the upstream debaters (reduces "rubber-stamp the consensus").
- `diversify_debaters: True` — Bull/Bear and the 3 risk debaters spread across
  `debater_provider_preference` (`["google", "deepseek"]`) so the upstream
  isn't a single-family united front. Missing keys silently fall back to the
  quick model.
- `blind_first_round: True` — round 1 of each debate hides the opponents'
  arguments so the opening priors are independent (crowd-wisdom condition).

The rationale is correlated-failure reduction; the empirical claim is *measured*
(`tests/evals/diversity_engine_eval.py`), not assumed. See `_build_diversified_synthesizer_llm` / `_build_debater_llms` in `graph/trading_graph.py`.

### 2.3 Embeddings (Memory v2)

`AGENTICWHALES_EMBEDDING_MODEL` selects the embedding model. With a
`GOOGLE_API_KEY`, it uses `text-embedding-004`; otherwise it **falls back to a
deterministic 1024-dim hashing trick** so retrieval works with zero keys (lower
quality, but no network). See `memory_v2.embed`.

### 2.4 Cost control

Every LLM call is metered by `llm_clients/cost_middleware.py`
(`record_fire_cost`): per-call token attribution priced via
`llm_clients/pricing.py`, written to `recipe_usage` + `user_spend_daily`, and
surfaced as the `aw_llm_call_*` Prometheus metrics and the `agenticwhales cost`
CLI. Recipes carry a `max_daily_token_cost_usd` budget gate the scheduler
honors before firing.

---

## 3. Services inside the single process

Booting `agenticwhales-web` starts all of these (see README for the full
table). The ones that matter for the quant loop:

| Service | File | When it runs |
|---|---|---|
| HTTP API + WS session streams | `web/server.py` | always |
| Recipe scheduler (APScheduler, leader-elected) | `web/scheduler.py` | leader only |
| Streaming worker (Alpaca equity IEX + crypto WS) | `web/streaming_worker.py` | leader only |
| Outcome resolver (Brier scoring) | `agenticwhales/outcomes.py` | nightly 02:00 UTC |
| Prompt-eval canary | `agenticwhales/adaptive.py` | weekly Sun 04:00 UTC |
| Behavioral detector | `agenticwhales/behavioral.py` | post-decision |
| Stuck-run reaper / stale cleanup | `web/scheduler.py` + `web/server.py` | 5-min / nightly |

**Leader election:** first worker to claim the Postgres advisory lock becomes
leader and owns the scheduler/streaming/crons; others stay hot for HTTP.
Offline (no Supabase), every worker believes it's the leader — fine for single
process.

---

## 4. How the quant actually works

This is the part the README doesn't cover. Read top-to-bottom once; it's the
whole engine.

### 4.1 Market-data layer

- **Prices/indicators:** yfinance OHLCV, cached per symbol, look-ahead-safe
  (`dataflows/stockstats_utils.py` filters rows after `curr_date`). Technical
  indicators come from `stockstats` via a vendor router
  (`dataflows/interface.py:route_to_vendor`) with rate-limit fallback.
- **Fundamentals / news:** yfinance + optional Alpha Vantage
  (`ALPHA_VANTAGE_API_KEY`) as fallback.
- **Market snapshot:** `market_snapshot.fetch_snapshot_block` produces the
  "Latest close: $X" directive string the agents (and the price-anchor parser)
  read.
- **Calendars:** `agenticwhales/calendar.py` resolves per-instrument trading
  hours via `exchange_calendars` (NYSE/CME/24×7 crypto) with a conservative
  NYSE-like fallback. Scope today is US equities + US crypto.

### 4.2 The multi-agent graph

`graph/trading_graph.py` (`AgenticWhalesGraph`) builds a LangGraph
`StateGraph` (`graph/setup.py`) wired by `graph/conditional_logic.py`:

1. **Analysts** (selectable subset of `market, quant, social, news,
   fundamentals`) each run a tool-using loop: bind tools → call the LLM →
   while it emits tool calls, route back to the tools node → when it stops,
   write the report section. Order is sequential; the first analyst is the
   `START` edge.
   - **Quant analyst** is special: it emits a structured 6-axis `QuantRadar`
     (volatility, S/R strength, breakout, momentum, pattern reliability, trend
     certainty) via a second structured call, not prose — sidesteps
     "rhetorical fluency" bias at the synthesizer.
2. **Bull ⇄ Bear researchers** debate for `max_debate_rounds` (default 2). The
   conditional router (`should_continue_debate`) bounces between them until the
   round cap, then hands to the **Research Manager** (a synthesizer LLM) which
   writes the `investment_plan` / `judge_decision`.
3. **Trader** turns the plan into a `trader_investment_plan`.
4. **Aggressive ⇄ Conservative ⇄ Neutral risk debate** runs for
   `max_risk_discuss_rounds` cycles (`should_continue_risk_analysis` cycles the
   three), then the **Portfolio Manager** synthesizes the final call.
5. **Portfolio Manager** emits the structured `PortfolioDecision` (the trusted
   output) plus the `final_trade_decision` prose.

`web/runner.py:SessionRunner` drives `graph.graph.stream(...)`, mapping each
chunk into UI events (report sections, agent statuses, token stats) and, on
completion, running the **post-decision hook**.

### 4.3 The decision schema

`agents/schemas.py:PortfolioDecision` is the contract the rest of the system
trusts (not the prose):

- `rating` — five-tier `PortfolioRating`: SELL / UNDERWEIGHT / HOLD /
  OVERWEIGHT / BUY.
- `executive_summary`, `investment_thesis` — text.
- `expected_return_pct`, `expected_volatility_pct`, `prob_of_profit`,
  `expected_hold_days`, `stop_loss`, `take_profit` — the scalars sizing/risk
  need.

A **conviction score** (1–10) is derived and recorded per fire; it decays over
time (`conviction_decay.project_timeseries`) so a week-old high-conviction call
isn't treated as fresh.

### 4.4 The Classical Analyst — the deterministic quant voice

`agenticwhales/classical.py` is a pure, rules-based signal engine that runs
**independently of the LLM debate** and can be auto-injected as a third voice
when Bull/Bear agree too much (see §4.5). It's the only part of the system with
honest, inspectable math:

| Signal | Mechanic | Weight |
|---|---|---|
| **Momentum** | 12-1 return (252-day lookback, skip last 21); direction if \|ret\| > 10% | 0.35 |
| **Trend** | 50/200 SMA cross; strength scales with % spread (20% → max) | 0.30 |
| **Mean reversion** | Bollinger (20, 2σ): above upper → short, below lower → long | 0.20 |
| **Vol regime** | trailing-year ATR percentile → a 0.5–1.0 conviction *multiplier* (dampens, never flips) | 0.15 |

Each signal returns `(direction ∈ {-1,0,+1}, strength ∈ [0,1])`. The weighted
vote sums to a `score ∈ [-1, +1]`, dampened by the vol multiplier, then mapped
to a rating:

```
score ≥  0.50 → BUY
score ≥  0.20 → OVERWEIGHT
score ≥ -0.20 → HOLD
score ≥ -0.50 → UNDERWEIGHT
else          → SELL
```

Brackets come from ATR (1 ATR stop, 2 ATR target → 2:1 R:R), `prob_of_profit`
is a conservative `0.50 + score·0.20` clamped to [0.30, 0.70], hold = 45 days.
The Classical Analyst is deliberately humble — it carries no narrative, so it's
an adversarial check on the LLM's qualitative story, not a replacement.

### 4.5 Conviction, disagreement, calibration

- **Disagreement** (`disagreement.py`): cosine similarity over the Bull/Bear
  debate histories. If the two sides are too consensus-y (low disagreement),
  the runner can **auto-inject the Classical Analyst** as a dissenting third
  voice and surface a "Classical disagrees" card.
- **Calibration** (`calibration.py`): once a user has ≥ `UNLOCK_N` (30)
  resolved outcomes, a **Platt scaling** `(a, b)` is fit on
  `(predicted_prob, hit)` pairs, persisted per regime. When the user opts in,
  `paper.kelly_sizing` runs the PM's raw `prob_of_profit` through
  `apply_platt` before sizing — i.e. the system learns the PM is over/under-
  confident and corrects it.

### 4.6 Position sizing — fractional Kelly + RiskGuard

`paper.kelly_sizing(decision, nav, last_price, kelly_fraction_cap, user_id)`:

- edge from `prob_of_profit` and `expected_return_pct` (calibrated if opted
  in); zero/negative edge → **no bet** (the system abstains and records *why* —
  HOLD rating, missing scalar, Kelly ≤ 0, or bad price — via an `abstain`
  risk event so users aren't left guessing).
- fractional Kelly, **capped** by the user's `kelly_fraction_cap` (default
  0.10) — never full Kelly.

Then `risk.RiskGuard.evaluate(...)` is the pre-trade gate
(`risk.RiskLimits`): `max_position_pct`, `max_daily_drawdown_pct`,
`max_slippage_bps`, `kelly_fraction_cap`, `global_kill_switch`, `allow_shorts`.
The guard can **hard-block** or **partially clamp** the quantity; either way it
emits a `risk_event` and the sized order proceeds only if allowed.

### 4.7 Paper fill engine + the learning loop

`paper.place_order(...)` writes the order and applies the fill
(`_apply_fill_python`: long/short/cover/flip math, average cost, realized PnL,
short collateral, NAV). A Postgres RPC (`paper_place_order`) does the same
atomically when Supabase is configured; the in-memory path mirrors it for dev.

The loop **closes** in `outcomes.py`: nightly, every paper order whose
`expected_hold_days` has elapsed is resolved — pull the realized return, decide
`hit` (positive PnL, sign-flipped for shorts), compute the **Brier component**
`(predicted_prob − actual)²`, and write a `decision_outcomes` row. Brier is the
scalar that calibration, prompt-eval, and the `ask` analytics all train on.

### 4.8 Memory

Two layers feed context back into prompts:

- **FinMem-style layered memory** (`agents/utils/memory.py`): decisions +
  reflections scored by recency (exponential decay), relevancy (Jaccard),
  importance (outcome magnitude); frequently-accessed entries get promoted from
  shallow → deep. Includes a periodic **extended reflection** (M-day
  retrospective; default every 10 days over a 30-day window).
- **Outcome-predictive retrieval** (`memory_v2.py`): journal/decision
  embeddings, cosine-scored against the query and **multiplied by
  predictiveness** (derived from the linked outcome's Brier — entries whose
  past calls came true score higher). `_augment_with_memory_v2` prepends these
  to the per-ticker context.

### 4.9 Behavioral detectors

`behavioral.py` scans the user's recent paper orders + journal after each
session (cheap, ≤500 rows) for four bias patterns:

- **Tilt** — 2+ losers in 60 min then an outsized (≥2× median) entry.
- **Revenge** — re-entering the same name/side within 30 min of a stop-out at
  ≥1.2× size.
- **Overconfidence** — ≥5 trades claiming p≥0.80 that collectively hit <50%.
- **Anchoring** — a positive journal note within 24h before a trade that landed
  >2σ below the user's mean return.

If the user opts into the cooldown circuit-breaker, a fresh tilt/revenge
finding **blocks** the next paper order (`cooldown_in_effect` → `tilt_cooldown`
risk event).

### 4.10 Adaptive depth + prompt-eval

- **Adaptive depth** (`_maybe_apply_adaptive_depth`): a 3-sample quick-model
  pre-pass; if the samples disagree above the user's variance threshold, *this*
  fire is escalated — analysts upgrade to the deep model and debate rounds +1.
  Cheap insurance against the cheap-and-wrong path on genuinely hard calls.
- **Prompt-eval** (`adaptive.evaluate_prompt_variant`): replays resolved
  outcomes against a candidate prompt, Brier-vs-baseline, promotes on
  improvement. The weekly cron runs a flat-coin *canary* — if `p=0.5` beats the
  live PM, the PM is worse than chance and needs attention.

---

## 5. Autonomy — recipes, scheduling, streaming, multi-timeframe

A **Recipe** (`recipes.py`) is a persistent thesis: tickers, analysts, models,
schedule, `output_policy` (`notify` / `alert_conviction` / `paper_trade`),
conviction threshold, budget, and optional streaming `trigger_conditions`.

- **Scheduling** (`web/scheduler.py`): `cron` (`CronTrigger.from_crontab`),
  `interval`, or `manual` (fired via `recipe trigger-now`). A gate ladder
  checks market hours, budget, kill-switch, breaker, and per-recipe concurrency
  before firing.
- **Streaming triggers** (`web/streaming_worker.py` + `streaming.py` +
  `triggers.py`): Alpaca equity-IEX + crypto-US WebSockets evaluate
  `price_move` / `volume_spike` / `news_keyword` / `indicator_cross` / `time`
  conditions (and `and`/`or` composites) per tick; a match mints a `fire_id`
  and dispatches through the *same* fire path as cron — all gates apply
  uniformly. Per-recipe `max_fires_per_hour` rate-limits.
- **Multi-timeframe fan-out** (`dag.py` + `web/server.py:_run_recipe_multitf`):
  a recipe can run per-timeframe legs and record cross-timeframe disagreement;
  the orchestrator runs the post-decision hook once against the merged
  decision.

`output_policy` controls what a fire *does*: notify only, alert when conviction
clears threshold, or actually place a paper order through §4.6–4.7.

---

## 6. Backtesting

`agenticwhales/backtest.py` (CLI `agenticwhales backtest run`) replays yfinance
OHLCV through the sizing/risk/fill pipeline with a **deterministic stub LLM** so
results are reproducible and require no key. `agenticwhales/asof.py` enforces
look-ahead safety — backtests refuse to read prices past the as-of date.

```bash
agenticwhales backtest run AAPL --from 2024-01-01 --to 2024-06-30 \
    --cash 100000 --kelly-cap 0.10 --out bt.json
```

This validates the *mechanics* (sizing, risk, fills, NAV) deterministically; it
does **not** validate LLM alpha (the stub isn't the real debate) — see the
North Star doc for why that gap matters.

---

## 7. Persistence modes

| Mode | When | Behavior |
|---|---|---|
| **In-memory** | no `AGENTICWHALES_SUPABASE_*` | `web/auth.py:_memstore`; resets on restart; guest auth |
| **Local Supabase** | Docker + CLI | real Postgres + Studio; apply `docs/supabase-schema.sql` |
| **Hosted Supabase** | prod | same schema; Google OAuth; RLS on every user-scoped table |

The same code path drives all three (`_db_writable()` gates the DB branch,
memstore is the fallback). Service-role key is **server-side only** — never the
browser path; the anon key is injected into served HTML and safe under RLS.

---

## 8. Deployment

- **Docker:** `docker compose run --rm agenticwhales` (Ollama profile available
  for local models).
- **Fly.io:** `fly.toml` is included; point `/readyz` at the health check.
- **Multi-worker:** `gunicorn web.server:app -k uvicorn.workers.UvicornWorker
  -w 4`. The advisory-lock leader election fans the autonomy plane out safely;
  `/readyz` returns 503 on stale leadership so rolling deploys just work.
- **Observability:** structured JSON logs with `correlation_id`/`user_id`,
  Prometheus `/metrics` (gate with `AGENTICWHALES_METRICS_TOKEN`), OTLP traces
  (one trace per recipe fire), append-only `audit_log`.

---

## 9. API-key checklist

| Need | Key(s) | Required? |
|---|---|---|
| Run a live debate | one provider key (e.g. `GOOGLE_API_KEY`) | **yes** (else backtest stub only) |
| Best diversity/quality | `GOOGLE_API_KEY` + `DEEPSEEK_API_KEY` + `ANTHROPIC_API_KEY` | recommended |
| Memory v2 real embeddings | `GOOGLE_API_KEY` | optional (hashing-trick fallback) |
| News/fundamentals fallback | `ALPHA_VANTAGE_API_KEY` | optional |
| Per-user persistence + OAuth | `AGENTICWHALES_SUPABASE_{URL,ANON_KEY,SERVICE_KEY}` | optional (guest mode otherwise) |
| Live streaming triggers | `ALPACA_API_KEY_ID` + `ALPACA_API_SECRET_KEY` (paper tier) | optional |

---

## 10. Verify end-to-end

```bash
curl -s localhost:8765/healthz                 # {"status":"ok"}
curl -s localhost:8765/readyz                  # {"ready":true,...}
curl -s localhost:8765/metrics | grep '^aw_'   # Prometheus counters
agenticwhales stream test --ticker AAPL --seconds 10   # 5+ trades in market hours
.venv/bin/pytest -q                            # full unit/E2E suite (1300+ tests, ~91% cov)
```

A green manual chain: create a `manual` recipe with `output_policy=paper_trade`
→ `recipe trigger-now <id>` → watch a `paper_order` land (or an `abstain`/
`risk_event` explaining why it didn't) → `agenticwhales paper status`.

---

## 11. Where to read next

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the deep system map (754 lines).
- **[NORTH_STAR.md](NORTH_STAR.md)** — honest current-state-vs-gaps assessment
  and the path to a genuinely intelligent agentic hedge fund.
- **[README.md](README.md)** — full env table, CLI reference, troubleshooting.
- **[docs/supabase-schema.sql](docs/supabase-schema.sql)** — the 30+ table
  schema and the atomic RPCs.
