# Premium plan & features — requirements, design, implementation plan

*2026-06-12 · status: shipped with this document (see PR #19)*

The product showed an internal quota knob ("INTERMEDIATE") in the nav while
/pricing sold an unrelated taxonomy (Plus/Pro founding reservations) that
granted nothing. This spec unifies them: one plan ladder, real premium
features, and a user journey that meets people at the moment they hit a wall.

Billing remains **dark** (owner decision: validation-gated). Everything here
ships behind `AGENTICWHALES_BILLING_ENFORCED` (default `0`): plans, labels,
nudges and premium features are all *visible and usable* during the beta;
flipping the env var turns the gates on without a code change.

---

## 1. Plan ladder

| internal tier (`profiles.tier`) | plan | price (founding → launch) |
|---|---|---|
| `novice` | **Free** — "Know your leaks" | $0 |
| `intermediate` | **Plus** — "Fix them" | $9 → $19/mo |
| `master` | **Pro** — "Your trading back office" | $29 → $49/mo |

No schema change: the mapping lives in `web/entitlements.py`. UI never shows
the internal tier names again.

### Entitlements matrix (single source of truth: `web/entitlements.py`)

| key | Free | Plus | Pro |
|---|---|---|---|
| `briefs_per_day` | 3 | 50 | unlimited |
| `pretrade_per_day` | 5 | unlimited | unlimited |
| `max_active_rules` | 2 | unlimited | unlimited |
| `auto_sync` | — | ✓ | ✓ |
| `email_digest` / `partner` | — | ✓ | ✓ |
| `violation_alerts` | — | ✓ | ✓ |
| `counterfactual_curve` | — | ✓ | ✓ |
| `benchmark_percentile` | — | ✓ | ✓ |
| `standing_briefs` | — | — | ✓ |
| `eval_mode` | — | — | ✓ |

Enforcement: `entitlements.check(user_id, key)` consulted at the same
chokepoints that enforce quotas today. When `AGENTICWHALES_BILLING_ENFORCED=0`
every check passes but the *limit metadata* still flows to the UI so nudges
and "included in Plus" labels render. Brief quotas stay enforced as today
(they pre-date this work and are usage protection, not monetization).

---

## 2. Features

### 2.1 Counterfactual discipline curve (Plus)
**Requirement.** The single most visceral paid artifact: cumulative *actual*
P&L vs cumulative *disciplined* P&L (same trades under the size-cap + stop
rules), month by month. Honest framing: historical attribution, never a
forecast.

**Design.** `coach.discipline_curve(trips)` — deterministic, reuses the exact
per-trip arithmetic of `counterfactual_disciplined` (full-history median
notional so the curve reconciles with the headline number). Output:
`[{month, actual_cum, disciplined_cum}]`. Lands on the report dict as
`counterfactual_curve`; rendered as a dual-line SVG on /coach directly under
the leak cards, with past-tense copy and the standard tripwire sweep.

### 2.2 Prop-firm evaluation mode (Pro)
**Requirement.** The validated ICP (PR #18) pays $100+ per evaluation
attempt. Track an evaluation's constraints against the user's *actual*
realized P&L: profit target progress, daily-loss breaches, max-drawdown
proximity, days remaining. Scorekeeping only — the product never says "stop
trading" or "trade smaller" (no directives); it reports what happened and
where the thresholds are.

**Design.** `agenticwhales/prop_eval.py`: pure function
`evaluate(config, txns, today)` over daily realized P&L by exit date.
Presets: `ftmo_style` (profit target 10%, daily loss 5%, max drawdown 10%),
`topstep_style` (6% / 2% / 4%), `custom`. Storage: `coach_evals` (one active
eval per user, RLS read-own, service-role writes), covered by
`delete_coach_data` + the privacy meta-test. API: `GET/POST/DELETE
/api/coach/eval`. UI: an eval card on /coach with progress bars and a breach
history list.

### 2.3 Standing briefs (Pro)
**Requirement.** "Your analysts brief you every Monday on your watchlist" —
the back-office narrative made literal, on the existing scheduler.

**Design.** `coach_standing_briefs` table (id, user_id, tickers, cadence
`weekly`, active, last_run_at). Leader-gated cron Mondays 13:00 UTC (an hour
before the digest, so the digest can reference fresh briefs): for each active
row whose owner has the `standing_briefs` entitlement, fire one **brief-mode**
session per ticker via `runner.build_session` + `SessionRunner` — the same
path as the interactive desk, quota-exempt (like recipes), provider-gated.
Desk UI: a "Standing brief" card to set tickers + toggle; results appear in
normal history.

### 2.4 Violation alerts (Plus)
**Requirement.** When a sync/audit detects the user broke an adopted rule,
tell them without waiting for the Monday digest. Mirror, then conscience.

**Design.** `coach_prefs.email_alerts` boolean (default false, opt-in).
`_persist_rule_events` already computes *genuinely new* violations; when ≥1
new violation lands and the user opted in (+ entitlement + Resend
configured), send ONE minimized email per audit (counts + rule labels only —
dollars stay in-app, same as the digest posture; CAN-SPAM footer via
`email_service`). Never raises; in-app violations feed remains the source of
truth.

### 2.5 Leak Guarantee (copy, all paid plans)
If a quarter's audit doesn't surface quantified leaks ≥ that quarter's
subscription cost, the next month is free. Adjudicable from
`quarterly_discipline().quantified_leak` — deterministic, already shipped.
Pricing-page + methodology copy only (billing is dark); framed fee-to-fee,
never as a results promise.

### 2.6 Journey to pay
* Nav badge shows the **plan** ("Free plan") and links to /pricing.
* Nudges at friction moments, each linking /pricing with the relevant plan
  anchored: 3rd-rule activation (Free cap is 2), digest/alerts toggles
  (Plus), eval card + standing briefs (Pro), desk 429 (already routes).
  During beta each nudge says "included in Plus — free during the beta;
  reserve the founding price", so the smoke test measures intent at real
  walls instead of landing-page curiosity.
* `home.html` hero CTA becomes **"Join the Coach"** → Google sign-up for
  guests (the membership framing starts the relationship; signed-in users
  pass straight through to /coach).

---

## 3. Implementation plan (one PR-sized sequence, suite green at each step)

1. `web/entitlements.py` + `GET /api/account/plan` + tests.
2. `coach.discipline_curve` + report field + /coach chart + tests.
3. `prop_eval.py` + `coach_evals` storage/API/card + migration + tests.
4. `coach_standing_briefs` + cron + desk card + migration + tests.
5. `coach_prefs.email_alerts` + alert send on new violations + UI toggle + tests.
6. /pricing rebuild (3-tier matrix, Leak Guarantee, current-plan awareness,
   reserve flow kept) + nav plan chip + nudges + "Join the Coach".
7. Migrations folded into `docs/supabase-schema.sql` (drift tripwire enforces),
   `delete_coach_data` coverage, directive sweeps, full suite, preview pass.

### Migration
`docs/migrations/2026-06-12_premium_features.sql`: `coach_evals`,
`coach_standing_briefs`, `coach_prefs.email_alerts` — idempotent, folded into
the baseline in the same commit.

### Env
`AGENTICWHALES_BILLING_ENFORCED` (default `0`). No other new knobs; email
reuses `RESEND_API_KEY`/`AGENTICWHALES_EMAIL_FROM`.

### Design-law checklist (each feature tested against)
deterministic dollars · no buy/sell directives (sweep every generated string)
· past-tense attribution · read-only · dual-path storage via `web/auth.py`
helpers · `delete_coach_data` covers every new table · leader-gated crons ·
heavy work off the event loop.
