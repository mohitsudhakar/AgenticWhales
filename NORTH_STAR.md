# AgenticWhales — North Star

> **The vision.** A *truly intelligent agentic hedge fund in your pocket*: an
> autonomous system that forms theses, sizes risk, executes, learns from every
> outcome, and compounds an edge — while coaching its operator out of their own
> worst instincts. Not a chatbot that talks about markets; a fund that *runs*.
>
> **This document is deliberately honest.** It grades the current code against
> that vision, separates what is *real* from what is *scaffolding*, and lays out
> the gaps that stand between "impressive demo" and "system with a measurable,
> durable edge." It is grounded in the code as it exists today, not in
> aspirational literature (for the research survey, see
> [AgenticWhales_Future.md](AgenticWhales_Future.md)).

---

## 1. What "intelligent" must mean here

A hedge fund is intelligent if it can demonstrate, out-of-sample and net of
costs, that it:

1. **Has an edge** — beats a naive benchmark (buy-and-hold, or a flat-coin
   prior) on risk-adjusted return, repeatably, on data it never trained on.
2. **Knows what it knows** — its stated probabilities are calibrated; its
   confidence tracks its accuracy.
3. **Sizes and survives** — it controls drawdown and tail risk at the
   *portfolio* level, not just per trade.
4. **Learns** — outcomes change future behavior in a way that improves the
   above metrics over time.
5. **Is trustworthy** — its decisions are attributable, auditable, and its
   track record is real, not backtested-into-existence.

Today AgenticWhales has *the skeleton of all five* and the *substance of none*.
That's not a criticism — it's an unusually complete skeleton. The work ahead is
turning each loop from "wired and plausible" into "validated and load-bearing."

---

## 2. Current state — what's real vs. scaffolding

| Subsystem | State | Honest read |
|---|---|---|
| Multi-agent debate graph | **Real** | Full LangGraph pipeline (analysts → researchers → trader → risk → PM), structured `PortfolioDecision` output, provider diversification, blind-first-round. Genuinely runs. |
| Structured decision contract | **Real** | `PortfolioDecision` (5-tier rating + scalars) is the trusted interface; the rest of the system keys off it, not prose. Strong design. |
| Classical quant analyst | **Real but toy** | Momentum/trend/Bollinger/vol-regime with sane weights — but these are textbook signals with hardcoded params and *no fitted edge*. It's an honesty check, not alpha. |
| Risk gate (`RiskGuard`) | **Real, per-trade only** | Position %, daily drawdown, slippage, kill-switch, shorts toggle. Clamps/blocks single orders. No portfolio-level risk. |
| Kelly sizing | **Real** | Fractional Kelly, capped, calibration-aware, honest abstain reasons. Solid. |
| Paper fill engine | **Real** | Long/short/cover/flip math, NAV, realized PnL, Postgres RPC + memstore mirror. Works. |
| Outcome resolver / Brier | **Real but thin** | Nightly resolution, Brier scoring, `decision_outcomes`. The loop closes — but on tiny N and a crude "hit = positive PnL" definition. |
| Calibration (Platt) | **Real, gated, unproven** | Fits after 30 outcomes, applies on opt-in. Mechanically correct; no evidence it improves live results yet. |
| Memory (FinMem + v2) | **Real, shallow** | Layered scoring + outcome-predictive embeddings. But embeddings default to a hashing trick without a key, and retrieval quality is unvalidated. |
| Behavioral detectors | **Real, heuristic** | Tilt/revenge/overconfidence/anchoring with fixed thresholds + a cooldown breaker. Useful signal, but rule-based and small-N, not the "95% NLP" of the vision. |
| Adaptive depth + prompt-eval | **Real, canary-only** | Depth escalation works; prompt-eval ships a flat-coin canary. No real candidate-prompt registry or A/B yet. |
| Autonomy (recipes/scheduler/streaming) | **Real** | Cron/interval/manual + Alpaca WS triggers + multi-timeframe fan-out, leader-elected. Production-shaped. |
| Backtest | **Real for mechanics only** | Deterministic stub LLM → validates sizing/risk/fills, **not** LLM alpha. The most important number (does the debate make money?) is *not* backtested. |
| Execution | **Paper only** | No real broker, no order routing, no microstructure, no slippage model beyond a flat bps. |
| Markets | **US equities + US crypto, single currency** | NYSE-resolved calendars; multi-market explicitly deferred. |
| Persistence / auth / cost / observability | **Real, production-grade** | Supabase + RLS, OAuth, cost middleware + budgets, Prometheus/OTLP/audit log. This is the most mature layer. |

**One-line summary:** the *plumbing* (autonomy, persistence, cost, risk gates,
the learning-loop wiring) is genuinely strong; the *alpha and the proof of
alpha* do not yet exist.

---

## 3. The gaps that matter (ranked by leverage)

### G1 — No validated edge (the existential gap)
The system has never demonstrated, out-of-sample, that its decisions beat a
baseline. The backtest uses a stub LLM, so it tests sizing/risk plumbing, not
the debate. The Classical Analyst's signals are unfitted textbook formulas.
**Until there is a walk-forward backtest of the *real* LLM decision against
buy-and-hold and a flat-coin prior, "intelligent" is unproven.**

- Build a **replayable LLM backtest**: snapshot the as-of data, run the real
  graph at each historical date (cached/batched to control cost), size + fill,
  and produce a walk-forward equity curve with Sharpe, max drawdown, hit rate,
  and turnover.
- Compare against: buy-and-hold, equal-weight, flat-coin `p=0.5`, and the
  Classical Analyst alone. If the debate doesn't beat Classical-alone, the LLM
  cost isn't justified.

### G2 — Portfolio-level risk & construction is missing
Every decision is a single-name, single-shot call. There is no portfolio
optimizer, no correlation/covariance budgeting, no exposure netting across
positions, no rebalancing, no portfolio VaR or regime-aware de-risking. A
"hedge fund" that can't reason about the *book* isn't one.

- Add a **portfolio layer**: position-level → book-level aggregation, gross/net
  exposure limits, correlation-aware sizing (shrink correlated bets), and a
  rebalance loop. `RiskGuard` should evolve from per-trade clamp to
  book-level budget allocator.
- Add **regime detection** (vol/trend regime) that scales aggregate risk, not
  just the per-name vol multiplier the Classical Analyst already has.

### G3 — The learning loop is wired but not yet learning
Brier + calibration + memory + prompt-eval all exist, but on tiny N, crude
labels, and with no evidence they move live metrics. "Hit = positive PnL" is a
weak target; calibration is opt-in and unproven; memory retrieval is
unvalidated; prompt-eval is a canary.

- Define **better outcome labels** (risk-adjusted, benchmark-relative, not just
  sign-of-PnL).
- Run **calibration as a measured experiment**: does applying Platt improve
  out-of-sample Brier and realized PnL? Ship the answer, not just the
  mechanism.
- Promote prompt-eval from canary to a **real candidate-prompt registry** with
  shadow A/B and automatic promotion gated on out-of-sample Brier.
- Validate memory: does outcome-predictive retrieval actually improve next-call
  accuracy vs. no-memory? Measure it.

### G4 — Data fidelity caps the ceiling
yfinance + Alpha Vantage + stockstats is fine for a demo and terrible for
alpha: delayed/low-quality fundamentals, no point-in-time data (survivorship +
look-ahead risk beyond the as-of guard), no alt-data, no order-book/microstructure.
The social/news analysts read whatever loose feeds are wired.

- Move to **point-in-time, survivorship-bias-free** fundamentals and a real
  market-data vendor for the strategies that need it.
- Treat alt-data (the congress-trades / X-recs signals that already exist) as
  *features with measured IC*, not decoration.

### G5 — Multi-agent quality is asserted, not evaluated
Provider diversification, blind-first-round, and disagreement-injection are
sound *hypotheses* with one eval harness. But there's no per-agent quality
measurement (is the Bear actually finding risks? is the News analyst
hallucinating?), no hallucination detection on tool outputs, and the
"diversity reduces correlated failure" claim is largely unmeasured at scale.

- Add **agent-level evals**: graded rubrics per analyst, factuality checks
  against the tool data, and an ablation showing each agent's marginal
  contribution to decision quality.
- Add **hallucination guards**: cross-check PM scalars against the cited
  evidence; flag decisions whose thesis isn't supported by any tool output.

### G6 — Execution is a paper toy
No real broker integration, no order-routing logic (TWAP/VWAP/limit vs market),
no realistic slippage/impact model, no fills against an order book. The flat
`max_slippage_bps` is a placeholder.

- Build an **execution simulator** with a real microstructure/slippage model
  *before* any live broker — backtests and paper trading should pay realistic
  costs, or the equity curve lies.
- Then, behind a hard custody/compliance boundary, a real (paper-first) broker
  adapter.

### G7 — The behavioral partner is a rules engine, not a coach
The four detectors are a good start but are fixed-threshold heuristics on small
samples. The vision's "NLP behavioral coach that quantifies the dollar cost of
your tilt" needs embeddings over the user's notes, sequence modeling, and
P&L-attributed bias quantification.

- Layer **NLP on the journal**: sentiment/emotion embeddings, cluster trades
  into setups, and attribute drawdown to behavioral patterns with a quantified
  "this bias costs you X% expectancy."

### G8 — Trust, attribution, compliance, custody
There is an audit log and compliance attestation, but no live track record, no
performance attribution (which agent/signal made the money?), no explainability
beyond prose, and no custody/regulatory framework for real capital.

- **Track record as a first-class artifact**: immutable, signed, attributable.
- **Attribution**: decompose returns by signal, agent, and regime.
- The custody/compliance/identity story (the vision's ERC-8004 angle) is a
  far-horizon concern — but the *attestation + audit + RLS* groundwork already
  exists to build on.

---

## 4. Staged roadmap

**Horizon 1 — Prove or kill the edge (the only thing that matters first).**
- Replayable real-LLM walk-forward backtest (G1).
- Realistic slippage/impact in backtest + paper (G6, partial).
- Calibration-as-experiment: measured Brier/PnL impact (G3, partial).
- Outputs: an equity curve vs. four baselines, a calibration (reliability)
  curve, and a go/no-go on "the debate beats Classical-alone."

**Horizon 2 — Make it a fund, not a stock-picker.**
- Portfolio construction + book-level risk + rebalancing (G2).
- Regime-aware aggregate de-risking (G2).
- Better outcome labels + memory validation (G3).
- Per-agent evals + hallucination guards (G5).

**Horizon 3 — Compounding intelligence + trust.**
- Candidate-prompt registry with shadow A/B and auto-promotion (G3).
- Point-in-time data + measured alt-data IC (G4).
- NLP behavioral coach with P&L-attributed bias cost (G7).
- Live track record + return attribution (G8).

**Horizon 4 — Real capital (gated on everything above).**
- Execution simulator → paper broker → (behind custody/compliance) live broker.
- Multi-market, multi-currency.
- Identity/reputation/custody framework.

---

## 5. How we'll know it's working (success metrics)

Replace vibes with a dashboard the system reports on itself:

| Dimension | Metric | Bar |
|---|---|---|
| Edge | Walk-forward Sharpe vs. buy-and-hold & flat-coin | beats both, out-of-sample |
| Edge | Information ratio vs. benchmark | > 0.5 and stable |
| Calibration | Expected Calibration Error (ECE) on resolved outcomes | < 0.05, improving |
| Knows-what-it-knows | Brier vs. flat-coin canary | strictly better, persistently |
| Survival | Max drawdown vs. benchmark | materially lower |
| Learning | Δ out-of-sample Brier after calibration/memory on | negative (improvement), measured |
| Cost discipline | $ per decision vs. realized edge | edge ≫ cost |
| Agent value | Marginal decision-quality lift per agent (ablation) | each agent pays for itself |
| Trust | % decisions whose thesis is evidence-supported | → 100%, hallucination-flagged otherwise |
| Behavioral | Quantified $ cost of detected biases | reported and trending down |

---

## 6. The honest bottom line

AgenticWhales today is an **exceptionally well-built decision and autonomy
platform** with a complete learning-loop *architecture* and production-grade
ops — and **no demonstrated edge, no portfolio brain, and no proof its learning
loop learns**. The single highest-leverage move is **G1: a real walk-forward
backtest of the actual LLM debate.** Everything else — portfolio construction,
better data, the behavioral coach, real execution — is worth building only once
that backtest says the brain is worth feeding. Build the scoreboard first; let
it tell you which gaps to close.
