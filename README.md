
# AgenticWhales: Multi-Agent Financial Intelligence

Multi-agent LLM debate over a ticker → structured decision → paper-trading sandbox → cognitive journal. Two web surfaces: **`/fund`** (the product — autonomy spine, risk guards, paper trading, journal) and **`/analyze`** (legacy power-user surface — one-shot analyses + batches). The same `agenticwhales` Python package + CLI works headless.

---

## Running the full stack

The whole product runs from **one process** — a FastAPI app that serves `/fund`, `/analyze`, the `/api/*` REST surface, WebSocket session streams, and embeds the scheduler + streaming worker + cron jobs. No separate workers, no message broker, no extra daemons.

### Prereqs

- Python **3.12+** (3.13 supported)
- `git`, `pip` (or `uv` / `conda`)
- **Optional but recommended:** Docker (for the local Supabase stack + `testcontainers`)
- At least one LLM provider key (Google Gemini is the default; DeepSeek/OpenAI/Anthropic also work)
- **Optional:** Alpaca paper-trading account for the live streaming worker

### One-time setup

```bash
git clone https://github.com/TauricResearch/TradingAgents.git
cd AgenticWhales

# Virtualenv — pick one
python3.12 -m venv .venv && source .venv/bin/activate
# … or conda: conda create -n agenticwhales python=3.13 && conda activate agenticwhales

# Install the package + the web extra (FastAPI, uvicorn, dotenv).
pip install -e '.[web]'

# Optional: integration tests against real Postgres (testcontainers + psycopg).
pip install -e '.[integration]'

# Drop your keys in .env (see "Environment variables" below).
# NOTE: `-n` (no-clobber) so this can NEVER overwrite an existing .env that
# already has your keys. If .env exists, this is a safe no-op — edit it directly.
cp -n .env.example .env
$EDITOR .env
```

### Start the server (every service in one command)

```bash
agenticwhales-web
# or, equivalently, when developing:
python -m web
```

Default bind is `127.0.0.1:8765`; override via `AGENTICWHALES_WEB_HOST` / `AGENTICWHALES_WEB_PORT`. The launch config in [.claude/launch.json](.claude/launch.json) pins port `8767` for the in-IDE preview, but the canonical port is 8765.

Open the app:

| URL | What it is |
|---|---|
| http://localhost:8765/ | 307-redirects to `/fund` |
| http://localhost:8765/fund | The product: hero session-launcher, theses, decisions, journal, backtest, risk |
| http://localhost:8765/analyze | Legacy power-user surface: one-shot analyses + batches with full model picker |
| http://localhost:8765/healthz | Liveness — always 200 if the process is up |
| http://localhost:8765/readyz | Readiness — 200 only when DB is reachable + leader heartbeat is fresh |
| http://localhost:8765/metrics | Prometheus exposition (gate with `AGENTICWHALES_METRICS_TOKEN`) |

### What runs inside the server process

The single uvicorn process boots all of these:

| Service | Where | Cadence |
|---|---|---|
| HTTP API + WebSocket session streams | [web/server.py](web/server.py) | always |
| Recipe scheduler (APScheduler, leader-elected via Postgres advisory lock) | [web/scheduler.py](web/scheduler.py) | always; per-recipe `cron` / `interval` / `manual` |
| Live streaming worker — Alpaca equity IEX + crypto US WS clients | [web/streaming_worker.py](web/streaming_worker.py) | leader-only; pumps every connected ticker |
| **Outcome resolver** — closes the loop on every paper order whose `expected_hold_days` has elapsed (writes `decision_outcomes`, scores Brier) | [agenticwhales/outcomes.py](agenticwhales/outcomes.py) | **nightly, 02:00 UTC** (job `outcome_resolver_nightly`) |
| **Prompt-eval harness** — canary flat-coin baseline vs the live PM | [agenticwhales/adaptive.py](agenticwhales/adaptive.py) | **weekly, Sundays 04:00 UTC** (job `prompt_eval_weekly`) |
| Behavioral findings detector | [agenticwhales/behavioral.py](agenticwhales/behavioral.py) | post-decision, fire-and-forget |
| Cost middleware (per-call attribution + global daily/monthly cap) | [agenticwhales/llm_clients/cost_middleware.py](agenticwhales/llm_clients/cost_middleware.py) | every LLM call |
| Prometheus metrics + structlog | [agenticwhales/observability.py](agenticwhales/observability.py) | always |

**Leader election:** the first uvicorn worker to claim a Postgres advisory lock on key `74737248` becomes the leader and runs the scheduler + crons + streaming worker. Others stay hot for HTTP and take over on heartbeat staleness within ~30s. With Supabase unconfigured (local dev) every worker thinks it's the leader — fine for single-process.

### Environment variables

Drop these into `.env`. `cp -n .env.example .env` (no-clobber — won't touch an existing `.env`) scaffolds the essentials; the table below is the full surface:

| Variable | Default | Purpose |
|---|---|---|
| **LLM provider keys** | | Set at least one; the default is Google. |
| `GOOGLE_API_KEY` | — | Gemini (recommended default — flash + pro both work) |
| `DEEPSEEK_API_KEY` | — | DeepSeek (good free-tier heterogeneity partner) |
| `OPENAI_API_KEY` | — | GPT-5.x |
| `ANTHROPIC_API_KEY` | — | Claude 4.x |
| `XAI_API_KEY` / `DASHSCOPE_API_KEY` / `ZHIPU_API_KEY` / `OPENROUTER_API_KEY` | — | Grok / Qwen / GLM / OpenRouter passthrough |
| `ALPHA_VANTAGE_API_KEY` | — | Optional; news + fundamentals fallback |
| **Default model selection** | | |
| `AGENTICWHALES_DEFAULT_PROVIDER` | `google` | Which provider the /fund hero defaults to |
| `AGENTICWHALES_DEFAULT_DEEP_MODEL` | `gemini-3.1-pro-preview` | Manager-grade model |
| `AGENTICWHALES_DEFAULT_QUICK_MODEL` | `gemini-3-flash-preview` | Analyst-grade model |
| `AGENTICWHALES_EMBEDDING_MODEL` | auto | Memory v2 embeddings; `text-embedding-004` when `GOOGLE_API_KEY` set, else hashing-trick |
| **Supabase (auth + per-user persistence)** | | Skip these for guest/local mode. |
| `AGENTICWHALES_SUPABASE_URL` | — | `https://<ref>.supabase.co` |
| `AGENTICWHALES_SUPABASE_ANON_KEY` | — | Public anon key — injected into the served HTML |
| `AGENTICWHALES_SUPABASE_SERVICE_KEY` | — | Service-role key (server-side ONLY; bypasses RLS) |
| **Alpaca streaming (Phase 3)** | | Both required for live WS. Use paper-tier keys. |
| `ALPACA_API_KEY_ID` | — | Key ID from https://app.alpaca.markets (paper trading) |
| `ALPACA_API_SECRET_KEY` | — | Secret key |
| **Cache + cost** | | |
| `AGENTICWHALES_CACHE_ENABLED` | `true` | Repeat-analysis cache (same ticker + date + config) |
| `AGENTICWHALES_CACHE_TTL_MINUTES` | `30` | TTL for the cache above |
| `AGENTICWHALES_PAPER_STARTING_CASH` | `100000` | Starting balance for a new paper account |
| **Server bind + observability** | | |
| `AGENTICWHALES_WEB_HOST` | `127.0.0.1` | Server bind host |
| `AGENTICWHALES_WEB_PORT` | `8765` | Server bind port |
| `AGENTICWHALES_LOG_LEVEL` | `INFO` | Log level (DEBUG / INFO / WARNING) |
| `AGENTICWHALES_LOG_FORMAT` | `json` | `json` or `text` |
| `AGENTICWHALES_METRICS_TOKEN` | — | If set, `/metrics` requires `?token=…` (Prometheus side-channel auth) |
| `AGENTICWHALES_HIGH_CARD_METRICS` | `0` | Turn on per-user-id labels (Prometheus cardinality risk) |
| **Misc** | | |
| `TRADINGAGENTS_WEB_HOST` / `TRADINGAGENTS_WEB_PORT` | — | Legacy aliases for `AGENTICWHALES_WEB_HOST/PORT`, still honoured |

### Database — where state lives

Three modes, all driven by the same code path:

**A. In-memory (default for local dev).** When `AGENTICWHALES_SUPABASE_*` are unset, every read/write goes to `web/auth.py:_memstore`. Resets on process restart. Perfect for testing the UI flow; no Postgres needed.

**B. Local Supabase via Docker.** Recommended for hands-on dev with real persistence:

```bash
# Install the Supabase CLI (https://supabase.com/docs/guides/cli)
brew install supabase/tap/supabase   # macOS

supabase init                        # one-time, in repo root
supabase start                       # boots Postgres + Studio at localhost:54323

# Apply the schema (30+ tables across Phase 1/2/3).
psql "$(supabase status -o json | jq -r .DB_URL)" -f docs/supabase-schema.sql

# Wire the env:
cat >> .env <<'EOF'
AGENTICWHALES_SUPABASE_URL=http://localhost:54321
AGENTICWHALES_SUPABASE_ANON_KEY=$(supabase status -o json | jq -r .ANON_KEY)
AGENTICWHALES_SUPABASE_SERVICE_KEY=$(supabase status -o json | jq -r .SERVICE_ROLE_KEY)
EOF
```

**C. Hosted Supabase.** Same shape — point the env at your project ref + paste the keys. Apply the schema once in **Studio → SQL Editor** (paste & run [docs/supabase-schema.sql](docs/supabase-schema.sql)). RLS is on by every user-scoped table.

### CLI — every subcommand

`agenticwhales` is also a Typer CLI. Each sub-app talks to the same backend the web UI uses (in-memory or Supabase, whichever is configured).

```bash
# One-shot analysis (interactive picker) — same flow as /analyze
agenticwhales analyze

# Recipes — recurring/manual theses
agenticwhales recipe create --name "Daily AAPL" --tickers AAPL \
    --analysts market,quant,news \
    --schedule-kind cron --schedule-expr "0 13 * * 1-5" \
    --provider google --quick gemini-3-flash-preview --deep gemini-3.1-pro-preview \
    --bull-model deepseek-v4 --bear-model gemini-3.1-pro-preview \
    --policy paper_trade --conviction-threshold 7 --daily-budget-usd 2.0
agenticwhales recipe list
agenticwhales recipe trigger-now <id>
agenticwhales recipe pause <id> | resume <id> | kill <id> | delete <id>

# Paper account
agenticwhales paper status
agenticwhales paper positions
agenticwhales paper orders --limit 20
agenticwhales paper risk-events --limit 20
agenticwhales paper kill-switch on|off

# Cost tracking
agenticwhales cost today
agenticwhales cost month
agenticwhales cost by-recipe

# Phase 3: historical backtest replay (yfinance OHLCV + deterministic stub LLM)
agenticwhales backtest run AAPL --from 2024-01-01 --to 2024-06-30 \
    --cash 100000 --kelly-cap 0.10 --out backtest-aapl-2024h1.json

# Phase 3: live Alpaca streaming dev aid (requires ALPACA_API_KEY_ID + SECRET)
agenticwhales stream test --ticker AAPL --seconds 30
agenticwhales stream test --ticker BTC-USD --seconds 30 --crypto
```

### Verifying everything works

```bash
# 1. Liveness + readiness
curl -s http://localhost:8765/healthz                       # {"status":"ok"}
curl -s http://localhost:8765/readyz                        # {"status":"ready","leader":true,...}

# 2. Prometheus scrape (counter names start with aw_*)
curl -s http://localhost:8765/metrics | grep '^aw_' | head

# 3. Alpaca WS smoke
agenticwhales stream test --ticker AAPL --seconds 10        # should print 5+ trades during market hours

# 4. Recipe → fire → paper-order chain (with in-memory mode)
curl -X POST http://localhost:8765/api/recipes -H "Content-Type: application/json" -d '{
  "name":"verify","tickers":["AAPL"],"analysts":["market","quant"],
  "llm_provider":"google","quick_model":"gemini-3-flash-preview",
  "deep_model":"gemini-3.1-pro-preview",
  "bull_model":"deepseek-v4","bear_model":"gemini-3.1-pro-preview",
  "schedule_kind":"manual","output_policy":"paper_trade",
  "conviction_threshold":7,"max_daily_token_cost_usd":2.0
}' | jq

# 5. Tests
.venv/bin/pytest -q                                         # 478+ unit/E2E tests
.venv/bin/pytest -m integration -q                          # 6 integration tests (real Postgres via testcontainers)
```

### Cron schedule reference

All cron jobs are registered in [web/scheduler.py](web/scheduler.py) and only execute on the elected leader. Each job re-checks `_is_leader` at fire time so leadership handoff mid-run can't double-fire.

| Job id | Cron (UTC) | Purpose |
|---|---|---|
| `prompt_eval_weekly` | `0 4 * * 0` (Sun 04:00) | Canary prompt-eval against live PM Brier |
| `outcome_resolver_nightly` | `0 2 * * *` (daily 02:00) | Resolve mature paper orders → `decision_outcomes` |

User-created recipes add their own jobs dynamically:
- Cron recipes: `CronTrigger.from_crontab(recipe.schedule_expr, timezone="UTC")`
- Interval recipes: `IntervalTrigger(seconds=...)`
- Manual recipes: no job — fired only via `agenticwhales recipe trigger-now <id>` or `/api/recipes/{id}/trigger-now`

### Observability + ops

- **Structured logs** — `structlog` emits JSON to stdout when `AGENTICWHALES_LOG_FORMAT=json` (default). Every log line carries `correlation_id` (the active `fire_id` or `session_id`) and `user_id`.
- **Prometheus metrics** — scrape `/metrics`. Key counters: `aw_recipe_fire_total{status}`, `aw_paper_order_total{side,status}`, `aw_risk_event_total{rule}`, `aw_llm_call_seconds{provider,model,agent}` (histogram), `aw_user_spend_today_usd{user_id}` (gated by `AGENTICWHALES_HIGH_CARD_METRICS=1`).
- **OpenTelemetry** — every recipe fire is one trace; OTLP exporter ready, configure with `OTEL_EXPORTER_OTLP_ENDPOINT`.
- **Audit log** — `public.audit_log` is append-only with `actor`, `action`, `target_user_id`, `metadata`. Streaming fires, scheduler leader changes, impersonations, and risk events all land here.

### Multi-worker / production

Run multiple uvicorn workers behind a load balancer; the advisory-lock leader election handles fan-out:

```bash
agenticwhales-web --workers 4                               # if using uvicorn directly
# or with gunicorn:
gunicorn web.server:app -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:8765
```

`/readyz` returns 503 if a worker thinks it's the leader but its heartbeat is stale — point your kubelet at it and rolling deploys just work. Test the failover behavior with `tests/integ/test_scheduler_leader.py` (when written; for now `test_scheduler_cron.py` covers the cron-registration logic).

### Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Missing Authorization bearer token` 401 | Supabase is configured but the browser isn't signed in | Click "Sign in with Google" in /fund or unset the `AGENTICWHALES_SUPABASE_*` env to switch to guest mode |
| `ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY not set` | Streaming worker is starting on the leader without Alpaca creds | Drop the two keys into `.env`, restart the server |
| `apscheduler not installed; recipe scheduler is disabled` | The `web` extra wasn't installed | `pip install -e '.[web]'` |
| `/metrics` returns 403 | `AGENTICWHALES_METRICS_TOKEN` is set | Pass `?token=…` matching the env value |
| Backtest returns "no OHLCV data" | `yfinance` rate-limit or non-trading-day window | Widen the date range; backtests refuse to peek past today (see [agenticwhales/asof.py](agenticwhales/asof.py)) |
| In-flight session never completes | LLM provider key invalid or budget cap hit | `curl /api/observability/cost/today` to verify spend; check structured logs for `BudgetExceeded` |
| Recipe-fire spam | A streaming `trigger_conditions` is too loose | Raise `streaming_max_fires_per_hour` cap on the recipe, or tighten the threshold |

---

### Auth & quota (Supabase)

The web UI gates new analyses behind a Supabase-backed sign-in (Google OAuth). Free **Novice** accounts get **3 instrument analyses per day** (resetting at 00:00 UTC); higher tiers (Intermediate, Master) are stubs until pricing is finalised.

To wire it up after creating your Supabase project:

1. **Run the migration** in `docs/supabase-schema.sql` (Supabase Studio → SQL Editor → paste & run). This creates `profiles`, `usage_daily`, RLS policies, and an atomic `increment_usage()` RPC.
2. **Enable Google as an OAuth provider**: Authentication → Providers → Google → on, paste your Google OAuth client ID + secret. Add your AgenticWhales URL (e.g. `http://localhost:8765/`) to *Redirect URLs*.
3. **Plug your project URL + anon key into the environment**:
    ```
    # .env (dev) or your prod env
    AGENTICWHALES_SUPABASE_URL=https://<your-ref>.supabase.co
    AGENTICWHALES_SUPABASE_ANON_KEY=eyJhbGciOi...   # the public "anon" key
    ```
    The web server reads these at request time and injects them into the served HTML before `supabase-client.js` evaluates — so dev/staging/prod just swap the env, no asset rebuild needed. The anon key is safe to expose to the browser as long as RLS is on (the migration takes care of that). **Never** put the `service_role` key in env vars consumed by the browser path.

If you skip step 3, the welcome modal degrades to a *guest mode* (per-browser localStorage cap; no cross-device tracking) so the app still works.

## News
- [2026-04] **AgenticWhales v0.2.4** released with structured-output agents (Research Manager, Trader, Portfolio Manager), LangGraph checkpoint resume, persistent decision log, DeepSeek/Qwen/GLM/Azure provider support, Docker, and a Windows UTF-8 encoding fix. See [CHANGELOG.md](CHANGELOG.md) for the full list.
- [2026-03] **AgenticWhales v0.2.3** released with multi-language support, GPT-5.4 family models, unified model catalog, backtesting date fidelity, and proxy support.
- [2026-03] **AgenticWhales v0.2.2** released with GPT-5.4/Gemini 3.1/Claude 4.6 model coverage, five-tier rating scale, OpenAI Responses API, Anthropic effort control, and cross-platform stability.
- [2026-02] **AgenticWhales v0.2.0** released with multi-provider LLM support (GPT-5.x, Gemini 3.x, Claude 4.x, Grok 4.x) and improved system architecture.
- [2026-01] **Trading-R1** [Technical Report](https://arxiv.org/abs/2509.11420) released, with [Terminal](https://github.com/TauricResearch/Trading-R1) expected to land soon.

<div align="center">
<a href="https://www.star-history.com/#TauricResearch/TradingAgents&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=TauricResearch/TradingAgents&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=TauricResearch/TradingAgents&type=Date" />
   <img alt="AgenticWhales Star History" src="https://api.star-history.com/svg?repos=TauricResearch/TradingAgents&type=Date" style="width: 80%; height: auto;" />
 </picture>
</a>
</div>

> 🎉 **AgenticWhales** is now live (built on top of the upstream TradingAgents framework). We have received numerous inquiries about the work, and we would like to express our thanks for the enthusiasm in our community.
>
> So we decided to fully open-source the framework. Looking forward to building impactful projects with you!

<div align="center">

🚀 [AgenticWhales](#agenticwhales-framework) | ⚡ [Installation & CLI](#installation-and-cli) | 🎬 [Demo](https://www.youtube.com/watch?v=90gr5lwjIho) | 📦 [Package Usage](#agenticwhales-package) | 🤝 [Contributing](#contributing) | 📄 [Citation](#citation)

</div>

## AgenticWhales Framework

AgenticWhales is a multi-agent trading framework that mirrors the dynamics of real-world trading firms. By deploying specialized LLM-powered agents: from fundamental analysts, sentiment experts, and technical analysts, to trader, risk management team, the platform collaboratively evaluates market conditions and informs trading decisions. Moreover, these agents engage in dynamic discussions to pinpoint the optimal strategy.

<p align="center">
  <img src="assets/schema.png" style="width: 100%; height: auto;">
</p>

> AgenticWhales framework is designed for research purposes. Trading performance may vary based on many factors, including the chosen backbone language models, model temperature, trading periods, the quality of data, and other non-deterministic factors. [It is not intended as financial, investment, or trading advice.](https://tauric.ai/disclaimer/)

Our framework decomposes complex trading tasks into specialized roles. This ensures the system achieves a robust, scalable approach to market analysis and decision-making.

### Analyst Team
- Fundamentals Analyst: Evaluates company financials and performance metrics, identifying intrinsic values and potential red flags.
- Sentiment Analyst: Analyzes social media and public sentiment using sentiment scoring algorithms to gauge short-term market mood.
- News Analyst: Monitors global news and macroeconomic indicators, interpreting the impact of events on market conditions.
- Technical Analyst: Utilizes technical indicators (like MACD and RSI) to detect trading patterns and forecast price movements.

<p align="center">
  <img src="assets/analyst.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

### Researcher Team
- Comprises both bullish and bearish researchers who critically assess the insights provided by the Analyst Team. Through structured debates, they balance potential gains against inherent risks.

<p align="center">
  <img src="assets/researcher.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### Trader Agent
- Composes reports from the analysts and researchers to make informed trading decisions. It determines the timing and magnitude of trades based on comprehensive market insights.

<p align="center">
  <img src="assets/trader.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### Risk Management and Portfolio Manager
- Continuously evaluates portfolio risk by assessing market volatility, liquidity, and other risk factors. The risk management team evaluates and adjusts trading strategies, providing assessment reports to the Portfolio Manager for final decision.
- The Portfolio Manager approves/rejects the transaction proposal. If approved, the order will be sent to the simulated exchange and executed.

<p align="center">
  <img src="assets/risk.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

## Installation and CLI

### Installation

Clone AgenticWhales:
```bash
git clone https://github.com/TauricResearch/TradingAgents.git
cd AgenticWhales
```

Create a virtual environment in any of your favorite environment managers:
```bash
conda create -n agenticwhales python=3.13
conda activate agenticwhales
```

Install the package and its dependencies:
```bash
pip install .
```

### Docker

Alternatively, run with Docker:
```bash
cp -n .env.example .env  # scaffold .env if missing (no-clobber), then add your API keys
docker compose run --rm agenticwhales
```

For local models with Ollama:
```bash
docker compose --profile ollama run --rm agenticwhales-ollama
```

### Required APIs

AgenticWhales supports multiple LLM providers. Set the API key for your chosen provider:

```bash
export OPENAI_API_KEY=...          # OpenAI (GPT)
export GOOGLE_API_KEY=...          # Google (Gemini)
export ANTHROPIC_API_KEY=...       # Anthropic (Claude)
export XAI_API_KEY=...             # xAI (Grok)
export DEEPSEEK_API_KEY=...        # DeepSeek
export DASHSCOPE_API_KEY=...       # Qwen (Alibaba DashScope)
export ZHIPU_API_KEY=...           # GLM (Zhipu)
export OPENROUTER_API_KEY=...      # OpenRouter
export ALPHA_VANTAGE_API_KEY=...   # Alpha Vantage
```

For enterprise providers (e.g. Azure OpenAI, AWS Bedrock), copy `.env.enterprise.example` to `.env.enterprise` and fill in your credentials.

For local models, configure Ollama with `llm_provider: "ollama"` in your config.

Alternatively, scaffold `.env` from the template and fill in your keys. The
`-n` (no-clobber) flag means this will **not** overwrite an existing `.env`:
```bash
cp -n .env.example .env
```

### CLI Usage

Launch the interactive CLI:
```bash
agenticwhales          # installed command
python -m cli.main     # alternative: run directly from source
```
You will see a screen where you can select your desired tickers, analysis date, LLM provider, research depth, and more.

<p align="center">
  <img src="assets/cli/cli_init.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

An interface will appear showing results as they load, letting you track the agent's progress as it runs.

<p align="center">
  <img src="assets/cli/cli_news.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

<p align="center">
  <img src="assets/cli/cli_transaction.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

### Web UI

The web UI exposes the same multi-agent pipeline through a browser-based dashboard with single-instrument analyses, multi-instrument *baskets*, a portfolio view, and live progress streaming over WebSocket.

Install the optional `web` extra (FastAPI + Uvicorn + python-dotenv):

```bash
pip install '.[web]'
# or with uv:
uv pip install '.[web]'
```

Start the server:

```bash
agenticwhales-web      # installed command
python -m web          # alternative: run directly from source
```

Then open <http://localhost:8080> (override with `AGENTICWHALES_WEB_HOST` / `AGENTICWHALES_WEB_PORT`).

You can cancel any in-flight analysis or basket from the session/basket header — the runner stops between graph steps and finalizes the run as `cancelled` without burning more tokens.

The auth + quota wiring (Supabase / Google OAuth) is described under [Auth & quota (Supabase)](#auth--quota-supabase) at the top. With Supabase unset, the welcome modal degrades to a per-browser guest mode so you can still try the app locally.

## AgenticWhales Package

### Implementation Details

We built AgenticWhales with LangGraph to ensure flexibility and modularity. The framework supports multiple LLM providers: OpenAI, Google, Anthropic, xAI, DeepSeek, Qwen (Alibaba DashScope), GLM (Zhipu), OpenRouter, Ollama for local models, and Azure OpenAI for enterprise.

### Python Usage

To use AgenticWhales inside your code, you can import the `agenticwhales` module and initialize a `AgenticWhalesGraph()` object. The `.propagate()` function will return a decision. You can run `main.py`, here's also a quick example:

```python
from agenticwhales.graph.trading_graph import AgenticWhalesGraph
from agenticwhales.default_config import DEFAULT_CONFIG

ta = AgenticWhalesGraph(debug=True, config=DEFAULT_CONFIG.copy())

# forward propagate
_, decision = ta.propagate("NVDA", "2026-01-15")
print(decision)
```

You can also adjust the default configuration to set your own choice of LLMs, debate rounds, etc.

```python
from agenticwhales.graph.trading_graph import AgenticWhalesGraph
from agenticwhales.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"        # openai, google, anthropic, xai, deepseek, qwen, glm, openrouter, ollama, azure
config["deep_think_llm"] = "gpt-5.4"     # Model for complex reasoning
config["quick_think_llm"] = "gpt-5.4-mini" # Model for quick tasks
config["max_debate_rounds"] = 2

ta = AgenticWhalesGraph(debug=True, config=config)
_, decision = ta.propagate("NVDA", "2026-01-15")
print(decision)
```

See `agenticwhales/default_config.py` for all configuration options.

## Persistence and Recovery

AgenticWhales persists two kinds of state across runs.

### Decision log

The decision log is always on. Each completed run appends its decision to `~/.tradingagents/memory/trading_memory.md`. On the next run for the same ticker, AgenticWhales fetches the realised return (raw and alpha vs SPY), generates a one-paragraph reflection, and injects the most recent same-ticker decisions plus recent cross-ticker lessons into the Portfolio Manager prompt, so each analysis carries forward what worked and what didn't.

Override the path with `AGENTICWHALES_MEMORY_LOG_PATH` (the legacy `TRADINGAGENTS_MEMORY_LOG_PATH` is still honoured as a fallback).

### Checkpoint resume

Checkpoint resume is opt-in via `--checkpoint`. When enabled, LangGraph saves state after each node so a crashed or interrupted run resumes from the last successful step instead of starting over. On a resume run you will see `Resuming from step N for <TICKER> on <date>` in the logs; on a new run you will see `Starting fresh`. Checkpoints are cleared automatically on successful completion.

Per-ticker SQLite databases live at `~/.tradingagents/cache/checkpoints/<TICKER>.db` (override the base with `AGENTICWHALES_CACHE_DIR`; legacy `TRADINGAGENTS_CACHE_DIR` is honoured as a fallback). Use `--clear-checkpoints` to reset all of them before a run.

```bash
agenticwhales analyze --checkpoint           # enable for this run
agenticwhales analyze --clear-checkpoints    # reset before running
```

```python
config = DEFAULT_CONFIG.copy()
config["checkpoint_enabled"] = True
ta = AgenticWhalesGraph(config=config)
_, decision = ta.propagate("NVDA", "2026-01-15")
```

