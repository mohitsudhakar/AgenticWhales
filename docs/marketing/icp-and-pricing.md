# ICP & pricing thesis — the market-validation tranche

> Companion to the [GTM playbook](coach-gtm-playbook.md). That doc says how to
> reach traders; this one says **which traders to reach first and how we find
> out whether they'll pay** — without building billing. The instrument is the
> `/pricing` page (founding-price reservations into the existing waitlist
> store) plus segment capture on the landing page.

## 1. The beachhead: prop-firm evaluation traders

Generic "active retail traders" is everyone and no one. The first market is
**traders attempting funded-account evaluations** (Topstep, Apex, FTMO-style
firms). Why they are the sharpest wedge:

1. **They already pay, repeatedly.** Evaluations and reset fees run $50–$300
   a pop, and resets recur. Willingness to pay for *anything* that reduces
   resets is pre-proven — no consumer-subscription leap of faith required.
2. **Their failure mode is our exact product.** Evaluations are lost to rule
   breaches — daily-loss caps and trailing drawdowns hit on tilt — far more
   often than to a bad thesis. The audit measures precisely that, from their
   own fills, in dollars, past tense.
3. **The message is dollar-for-dollar.** "Your reset fees are your discipline
   tax, itemized." No abstraction needed.
4. **Distribution matches our channel.** Tight Discords, r/Futures-adjacent
   YouTube, "payout proof" creators — the existing creator-audit program
   (real statement, on camera) is a native episode format there.
5. **Compliance-friendly.** Coaching someone to stay inside a firm's published
   risk rules is much further from investment advice than P&L improvement
   claims. Same guardrails apply; the framing is naturally past-tense and
   rules-based.

**Secondary segments** (served by the same product, tracked separately):
- Day/swing traders in their own account (the original ICP).
- Options traders — oversizing and doubling down after a loss show up in
  fills regardless of instrument.

We do not build segment-specific features yet. We *measure* segment mix and
conversion first; the segment question rides along on waitlist signups and
founding reservations (`note: seg=<segment>`).

## 2. Pricing thesis (smoke test, not billing)

Billing stays out of scope (decision 2026-06-10). The `/pricing` page is a
**measurement instrument**: real tiers, real prices, and a "reserve the
founding price" CTA that costs nothing and commits to nothing. Reservations
land in the existing waitlist store with `source=pricing-<plan>` so the
admin CSV export is the analysis surface — no new tables, no new credentials.

| Tier | Founding / later | What it maps to (already shipped) |
|---|---|---|
| **Audit** — free | $0 | Screenshot/CSV/PDF audits (3/day), demo, anonymized share card. Free forever — it IS the ad. |
| **Plus** | $9/mo founding · $19 later | Saved history + discipline trend, rule book with forward re-testing, weekly digest, accountability partner. |
| **Pro** | $29/mo founding · $49 later | Plus + read-only brokerage auto-sync, pre-trade check against full history, priority parsing, quarterly deep-dive. |

Pricing anchors: one evaluation reset fee exceeds a year of Plus at the
founding price — the comparison is fee-vs-fee, never a performance promise.

**Honesty rules for the page (enforced by tests):**
- Everything stays free during the beta; the page says so plainly.
- Reserving is free, non-binding, and described as such — it locks the
  founding price for the first 12 months once billing launches and tells us
  which plan to build first.
- All copy passes the forbidden-phrases sweep; the standard "Educational,
  not investment advice" footer appears.

## 3. Validation gates (when do we actually build billing?)

Decide on evidence, not vibes. Review monthly via the waitlist CSV export and
the `pricing_viewed` / `founding_reserved` funnel metrics:

| Gate | Threshold | Action when hit |
|---|---|---|
| Demand exists | ≥50 founding reservations total | Prioritize billing build (Stripe), grandfather reservations |
| Plan fit | one plan ≥60% of reservations | Build that plan first; park the other |
| Segment fit | one segment ≥50% of reservations | Lead all copy + creator targeting with that segment |
| No demand | <15 reservations after 60 days of ≥500 pricing views | Re-test price points or reframe tiers before writing any billing code |

Funnel definition: `pricing_viewed` → `founding_reserved` (both in the coach
events allowlist; Prometheus counters work for guests, durable rows for
signed-in users). Reservation conversion ≥3% of pricing views is the healthy
benchmark for a pre-launch smoke test.

## 4. What this tranche deliberately does NOT do

- No payment processing, no checkout, no card capture.
- No feature gating — nothing that is free today becomes paywalled.
- No per-segment product forks; segments are a tracking dimension only.
- No public claims about pass rates, profitability, or outcomes — the
  evaluation-trader copy stays inside the same compliance guardrails as
  everything else (GTM playbook §6).
