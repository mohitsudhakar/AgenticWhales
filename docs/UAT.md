# User Acceptance Testing — AgenticWhales

A manual walkthrough of every Phase 1–3 user-felt deliverable. Each scenario
has setup, action, expected result, and a verification step. Tick the boxes
as you go.

---

## 0. Prereqs

Before you start:

- [ ] `pip install -e '.[web]'` succeeded
- [ ] `.env` has at least `GOOGLE_API_KEY` (or another LLM key)
- [ ] `.env` has `ALPACA_API_KEY_ID` + `ALPACA_API_SECRET_KEY` for Phase 3 streaming UAT
- [ ] `agenticwhales-web` started without errors
- [ ] http://localhost:8765/healthz returns 200
- [ ] http://localhost:8765/readyz returns 200

Run the unit suite first — if any test fails, stop and fix that before touching the UI:

```bash
.venv/bin/pytest -q
# expect: 547+ passed, 1 skipped, 6 deselected
```

---

## A. Phase 1 — Autonomy spine

### A1. Web app boots, /fund is the canonical home

**Action.** Open http://localhost:8765/. Sign in via the landing page (or
configure Supabase to skip the gate for guest mode), then arrive at /fund.

**Expected.**
- Landing page shows Privacy/Terms + Google sign-in.
- After sign-in (or as guest), `/fund` loads with the sidebar (Overview, Theses, Decisions, Journal, Risk events, Insights, Backtest, Risk controls) and the **🐋 Analyze a ticker** hero card visible on Overview.
- Compliance modal appears once per browser; "I have read and acknowledge" → "Continue to /fund" dismisses it.

**Verify.** `localStorage.getItem('aw_compliance_acked') === '1'` after acknowledging.

### A2. Hero session — live multi-agent debate (the well-lit path)

**Action.**
1. On Overview, type `AAPL` (or your favorite ticker) into the hero ticker box.
2. Click **Start analysis**.

**Expected.**
- Hero swaps to *running* state with a spinner: "Spinning up agents…"
- An ordered list of 11 agents appears (Market Analyst, Quant, News, Bull Researcher, Bear Researcher, Research Manager, Trader, three Risk analysts, Portfolio Manager).
- Each row's status icon transitions: **○** PENDING → **●** IN_PROGRESS (spinning) → **✓** COMPLETED.
- Within ~60 seconds, each completed row gets a one-line preview of the agent's output.
- After ~120 seconds, the hero swaps to *complete* state with the final decision panel: rating (Buy/Overweight/Hold/Underweight/Sell) in color, expected return, P(profit), expected hold, conviction; an expandable "Investment thesis"; and three CTAs.

**Verify.** Three buttons visible on the complete state:
- **Save as recurring thesis +** (gradient primary)
- **Analyze another →** (ghost)
- **Open full debate ↗** (ghost; links to /analyze#session/<sid>)

### A3. Save-as-recurring thesis

**Action.** On the complete state, click **Save as recurring thesis**.

**Expected.**
- Status text changes to "Saved as recurring thesis. Opening Theses…"
- After ~500ms, the **Theses** tab is selected.
- The new recipe is highlighted with a brief flash (~2s).
- Recipe name: `<TICKER> — saved from analysis`.

**Verify.** In **Theses**, the new recipe shows your ticker, status=active, Bull=Google deep model, Bear=DeepSeek deep model (or whichever heterogeneous pair the saver found). Click **↻ Refresh** if it doesn't appear immediately.

### A4. Recipe scheduler — manual trigger

**Action.** On the new recipe row, click **Trigger now**.

**Expected.**
- A new session ID is returned.
- The session's status moves through `pending → running → completed`.
- The recipe's `last_run_at` updates.
- If the recipe's output_policy is `paper_trade`, a paper order is placed (visible under **Decisions → Recent orders**).
- If output_policy is `notify` (the save-default), no order — just a conviction score row visible under **Decisions → Conviction over time**.

**Verify (CLI mirror).**
```bash
agenticwhales recipe list       # shows your recipe with last_run_at populated
agenticwhales paper status      # NAV unchanged, realized_pnl unchanged
```

### A5. Risk controls — kill switch + spend cap

**Action.**
1. Go to **Risk controls** → flip the **Kill switch** ON.
2. Trigger-now the recipe again.

**Expected.** The fire is blocked. **Risk events** shows a row with rule=`kill_switch`. No new paper orders.

**Verify.**
```bash
curl http://localhost:8765/api/risk/events?limit=5 | jq '.[0].rule'
# "kill_switch"
```

Then flip kill switch OFF before continuing.

### A6. Cost spine — daily spend cap

**Action.**
1. **Risk controls** → set **Daily spend cap (USD)** to `0.01`. Save.
2. Trigger-now the recipe.

**Expected.** Fire fails with `BudgetExceeded` (visible in server logs / risk_events with rule=`budget`). No tokens spent past cap.

**Verify.**
```bash
curl http://localhost:8765/api/risk/events?limit=5 | jq '.[0].rule'
# "budget"
```

Reset cap back to `25.0` afterward.

### A7. Health + observability endpoints

**Action.**
```bash
curl -s http://localhost:8765/healthz                                 # liveness
curl -s http://localhost:8765/readyz                                  # readiness
curl -s http://localhost:8765/metrics | grep '^aw_'                   # Prometheus
```

**Expected.**
- `/healthz` → `{"status":"ok"}` (200)
- `/readyz` → `{"status":"ready", "leader": true, ...}` (200)
- `/metrics` returns Prometheus exposition with counters starting `aw_*`.

---

## B. Phase 2 — Cognitive journal + calibration

### B1. Journal auto-draft after a fire

**Action.** After Action A4 completes, navigate to **Journal**.

**Expected.** A draft entry with kind=`auto_draft` is in the timeline, body referencing the recipe's ticker + the PM's rating.

**Verify.** Click the entry — body should contain the executive summary or investment thesis.

### B2. Manual journal entry

**Action.** **Journal** → **New entry** → write a note → Save.

**Expected.** Entry appears in Timeline with kind=`note`.

### B3. Ask the fund — 10 templated questions

**Action.** On **Overview**, click any **Ask the fund** template button.

**Expected.** Markdown answer renders within ~1s, citing your own data. With no data, you'll see a "no data yet" message — that's correct.

Try a few:
- "📅 Which day of the week am I losing money?"
- "🥇 Which thesis is my most / least profitable?"
- "🎯 How calibrated are my probability estimates?"

### B4. Calibration head

**Action.** Run several theses (or trigger an existing recipe several times) to accumulate ≥30 resolved outcomes, then click **Resolve outcomes** on Overview. Then visit **Insights**.

**Expected.**
- **Calibration card** on Overview shows current Brier + n samples.
- **Insights** has a Calibration card with a reliability-curve preview + an opt-in toggle.
- After opt-in, future sizing decisions use the Platt-scaled probability instead of raw.

### B5. Behavioral findings detector

**Action.** Trigger a few losing trades + a same-day journal entry. Click **Re-scan** on the Overview behavioral card.

**Expected.** If patterns match (tilt / revenge / anchoring / overconfidence), findings appear with severity + evidence. Each finding can be acknowledged or dismissed.

### B6. Disagreement index

**Action.** Run a recipe where Bull/Bear models are heterogeneous (default for saved-as-recurring). View **Insights**.

**Expected.** Each thesis shows a similarity score (0–1) between Bull and Bear outputs. <0.4 = high disagreement; >0.9 = correlated ensembling.

### B7. Classical analyst (non-LLM)

**Action.** Create a recipe with **Auto-inject classical** enabled (Advanced options). When Bull/Bear similarity > 0.9, the Classical analyst (RSI + SMA + Bollinger composite) joins as a tiebreaker.

**Expected.** Visible in the agent feed during the run; its `PortfolioDecision` appears in the Decisions tab alongside the LLM-generated ones.

### B8. Ablation report

**Action.** **Decisions → click "Why?" on any closed order**.

**Expected.** A modal shows per-analyst contribution: how much each section's narrative drove the final decision (citation-proxy ablation).

### B9. Prompt-eval harness

**Action.** Run the cron job manually (it normally fires Sundays 04:00 UTC):

```bash
.venv/bin/python -c "
from web.scheduler import RecipeScheduler
s = RecipeScheduler()
s._is_leader = True
s._run_prompt_evals()
"
```

**Expected.** A `prompt_evals` row is written for each user with ≥10 resolved outcomes. The canary `calibrated-v1` variant emits a flat 0.5 probability — promoted if the live PM's Brier > 0.5 (i.e., worse than chance).

---

## C. Phase 3 — Streaming + multi-TF + backtest

### C1. Backtest replay (CLI)

**Action.**
```bash
agenticwhales backtest run AAPL --from 2024-01-01 --to 2024-06-30 \
    --out /tmp/backtest-aapl-2024h1.json
```

**Expected.**
- Console table shows: Final NAV, Growth %, decisions made, trades closed, hit rate, Brier, max drawdown.
- A second table shows last 10 trades.
- `/tmp/backtest-aapl-2024h1.json` contains full equity curve + trades.

### C2. Backtest replay (UI)

**Action.** **Backtest** tab → Ticker AAPL, From 2024-01-01, To 2024-06-30 → **Run**.

**Expected.**
- "Done. N decisions, M trades." status appears within ~5s.
- KPI grid shows 6 metrics.
- Green equity-curve SVG renders.
- Trades table populates with last 10.

### C3. As-of-date look-ahead guard

**Action.** Inside a Python REPL:

```python
from agenticwhales.asof import as_of_date, current_as_of
from agenticwhales.backtest import _load_history
import datetime as dt

with as_of_date("2024-06-01"):
    # Should refuse to fetch past June 1 even if asked.
    df = _load_history("AAPL", dt.date(2024, 1, 1), dt.date(2024, 12, 31))
    assert df.index.max().date() <= dt.date(2024, 6, 1)
```

**Expected.** Assertion passes; the loader silently truncates the end date to the as-of bound.

### C4. Alpaca streaming — smoke test

**Action.**
```bash
agenticwhales stream test --ticker AAPL --seconds 10
```

**Expected during US market hours.** 5+ live trade events print in a Rich live table:
```
[1] trade AAPL price=298.47 size=40
[2] trade AAPL price=298.47 size=40
…
Received 5 event(s) in 10s.
```

**Outside market hours.** 0 events, exit code 0, message: "No events. Common causes: market closed…"

**With bad creds.** Exit code 1, "Auth failure". With missing creds: exit code 2.

### C5. Trigger conditions — event-driven recipe fires

**Action.**
1. **Theses** → **New thesis** → enable Advanced → fill the recipe.
2. POST a trigger condition manually via API (the UI doesn't expose this yet):

```bash
RID=<your recipe id>
curl -X PUT http://localhost:8765/api/recipes/$RID \
    -H "Content-Type: application/json" \
    -d '{"trigger_conditions": {"kind": "price_move", "threshold_pct": 0.005, "direction": "either"}}'
```
3. Watch **Risk events** or the **📡 Streaming fires** card on Overview.

**Expected during market hours.** Within minutes, the recipe fires when AAPL moves >0.5%. The Streaming-fires card shows a row with timestamp, ticker, recipe name, reason ("price moved +0.52%").

### C6. Multi-TF fan-out

**Action.** Update a recipe to have multiple timeframes:

```bash
RID=<your recipe id>
curl -X PUT http://localhost:8765/api/recipes/$RID \
    -H "Content-Type: application/json" \
    -d '{"timeframes": ["1h", "1d"]}'
```

Trigger-now. Wait for completion (~2–4 minutes — N timeframes run sequentially).

**Expected.**
- N sessions are created (one per timeframe), each with `session.timeframe` stamped.
- The lead session (the first one) carries `pm_decision` = the **merged** decision from `merge_decisions(...)`, plus `multitf_decisions` containing each per-TF PortfolioDecision.
- A `disagreement_log` row is written with similarity ∈ [0, 1].
- Downstream paper-order placement uses the merged decision.

**Verify.**
```bash
curl http://localhost:8765/api/disagreement?limit=5 | jq '.[0]'
```

**Honest caveat.** v1 multi-TF runs all timeframes against the same daily OHLCV data — the per-TF decisions will often be identical, similarity = 1.0. Genuine per-timeframe data fetching (intraday OHLCV per TF) is Phase 3.5.

### C7. Conviction decay chart

**Action.** With at least 3–5 conviction scores accumulated (each recipe fire writes one), visit **Decisions**.

**Expected.**
- The "Conviction over time" card shows ticker chips and a 200-px SVG sparkline.
- Solid green line = decayed score (5-day half-life by default).
- Dotted blue line = raw score.
- Adjusting the half-life input redraws the decayed line immediately.
- Clicking a ticker chip filters the chart to that ticker only.

### C8. Streaming-fires panel

**Action.** **Overview** → **📡 Streaming fires** card. After C5 has fired at least once, click ↻ Refresh.

**Expected.** Rows show timestamp (UTC), ticker, recipe name, reason. Up to 20 most-recent fires.

### C9. Outcome resolver nightly cron

**Action.** Wait until 02:00 UTC, or invoke manually:

```bash
.venv/bin/python -c "
from web.scheduler import RecipeScheduler
s = RecipeScheduler()
s._is_leader = True
s._run_outcome_resolver()
"
```

**Expected.** Every paper order whose `expected_hold_days` has elapsed gets resolved: realized return computed, hit flag set, Brier component scored, row written to `decision_outcomes`.

**Verify.**
```bash
curl http://localhost:8765/api/paper/outcomes?limit=5 | jq
```

---

## D. Integration tests (real Postgres)

The unit suite uses an in-memory fallback. For full confidence in the storage
layer + RLS + the `paper_place_order` SECURITY DEFINER RPC:

```bash
pip install -e '.[integration]'
.venv/bin/pytest -m integration -q
```

**Expected.** 6 tests pass against a fresh `postgres:16-alpine` testcontainer. Requires Docker daemon running.

---

## E. End-of-UAT sanity check

Before declaring UAT passed:

- [ ] `pytest -q` shows **547 passed, 1 skipped, 6 deselected** (or higher).
- [ ] `pytest -m integration -q` shows 6 passed.
- [ ] `curl /healthz` → 200, `curl /readyz` → 200.
- [ ] `curl /metrics | grep aw_recipe_fire_total` shows a counter that incremented during your test runs.
- [ ] At least one full hero session completed without console errors in the browser.
- [ ] At least one backtest replay finished and rendered the equity curve.
- [ ] At least one Alpaca `stream test` returned live events during market hours.

If any of those fail, file a bug before shipping.

---

## F. Known caveats (intentional)

- **`/` is a landing page**, not a 307 redirect — sign-in gate.
- **Multi-TF v1** runs all timeframes against daily data; intraday per-TF
  fetching is Phase 3.5.
- **Alpaca streaming on free IEX feed** is delayed and only covers a subset of NMS prints; OTC and futures need a paid market-data subscription.
- **Backtest live mode** (real LLM in the replay loop) is not wired —
  the stub momentum generator exercises the wiring; full live replay is
  Phase 3.5.
- **No JS test runner** — the hero state machine is covered by the manual
  UAT path here + the HTTP-level tests for the surrounding endpoints.
