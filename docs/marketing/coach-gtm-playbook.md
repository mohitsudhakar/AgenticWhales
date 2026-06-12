# Coach go-to-market playbook

> The operational half of the growth plan (the code half shipped as Phases 0–10:
> screenshot activation, trust pages, landing, learn pages, rules + digests,
> share card, public stats). Everything here is executable by one operator;
> nothing requires billing (deferred by decision, 2026-06-10).

## 1. Positioning

**One line:** *"What's your discipline tax?" — we quantify, in dollars, what your
own trading behavior already cost you. No predictions, read-only, educational.*

- We are NOT a journal (charts of your data), NOT a signal service (predictions),
  NOT advice. We are the **measurement layer for trading discipline**.
- The differentiator to repeat everywhere: **deterministic, falsifiable dollars** —
  same trades in, same numbers out, and every claim gets re-tested on the user's
  later trades (`/methodology` is the proof surface; link it constantly).
- Vocabulary discipline: "discipline tax", "behavioral leak", "historical
  attribution", "not detected forward". Never: "saved you", "will make you",
  "fixed", "edge", "alpha".

## 2. The creator-audit program (primary channel)

**Format:** a finance creator runs their REAL statement through the coach on
camera. High drama, fully honest, zero performance claims.

**Brief (send verbatim):**
- You'll export your trade history (we have guides: `/learn/<broker>-export-trade-history-csv`)
  or just screenshot it, run the audit live, and react to the number.
- You control what's shown: the share card is anonymized by default (score,
  period, top pattern); the $ figure renders only if you toggle it on.
- Hard rules (non-negotiable, they protect you too):
  1. Figures shown on screen come ONLY from the product's own share-card path —
     never hand-made graphics.
  2. No tickers/positions in the deliverable unless you choose to show your own
     screen; we never supply them.
  3. Past tense only — "this pattern cost me $X" is fine; "this tool will make
     you money" is banned and we'll ask for an edit.
  4. Disclose the partnership per FTC rules.
- Deliverables: 1 long-form video or stream segment + 1 share-card post with
  your `?ref=<yourcode>` link.

**Targeting:** 10–15 mid-size trading creators (50k–500k) who already do
"my P&L review" content — the audit is a natural episode. Day-trading FinTwit,
r/Daytrading-adjacent YouTube, options-recovery niches.

**Tracking:** every creator gets a referral code; `referral_attributions`
counts land on the admin dashboard. Pay per deliverable, not per signup
(no perverse incentives toward hype).

## 3. Launch sequence (tied to shipped phases)

| Step | Trigger | Action | Success gate (from the admin funnel) |
|---|---|---|---|
| Soft launch | landing + trust pages live (done) | Share `/` in 2–3 trading communities you're already in; ask for friction reports, not praise | median minutes-to-first-card < 5 |
| Share-loop launch | share card live (done) | First 3 creator audits go out; "What's your discipline tax?" framing everywhere | share rate > 5% of activated users |
| Retention story | digests live (done) | Content angle shifts to "my digest caught my revenge trade Tuesday"; push SnapTrade connect | D30 user-initiated retention > 20% |
| Proof flywheel | ≥10 traders audited | `/stats` goes into every pitch ("State of Retail Discipline"); quarterly refresh becomes a PR beat | n_traders growth week-over-week |

## 4. Moment-marketing calendar

- **January:** resolutions — "audit last year's discipline" (year card).
- **Feb–Apr (tax season):** the 1099/CSV is already exported — "while it's open,
  see your discipline tax." Learn pages catch this search traffic.
- **Volatility spikes / red weeks:** regret peaks; pre-written posts on revenge
  trading and the cooldown rule (educational tone, never opportunistic gloating).
- **December:** "Wrapped"-style year card push.
- **Quarterly:** refresh `/stats`, post the delta; review the 4 broker export
  guides for UI drift (update "Last reviewed" dates).

## 5. Loops that work without billing

- **Share loop:** leak card → ?ref code → landing → screenshot paste → their own
  card. The product is the ad.
- **Partner loop:** accountability partners see the streak page → "what is
  this?" → landing. (Partner emails carry the one-click stop link; never spam.)
- **Recognition, not rewards:** referral counts earn a "founding coach" badge in
  the digest — no monetary rewards until billing exists.

## 6. Compliance guardrails (apply to ALL external copy)

1. No performance promises, no forward-looking returns, no "beat the market".
2. Dollars are past-tense historical attribution from the user's own trades.
3. No buy/sell directives — run any new copy through the
   `pretrade.contains_directive` sweep (it's a unit test; paste copy into a test
   case if unsure) and the forbidden-phrases list in `tests/test_trust_pages.py`.
4. Benchmarks/percentile claims only when `cohort == "real"` (the product
   enforces this; marketing must not screenshot the demo as if real —
   the demo card is labeled "sample trader").
5. Public stats: only quote `/stats` numbers (already k-anonymous + rounded);
   never hand-compute aggregates from raw data.
6. Every artifact carries "Educational, not investment advice."

## 7. Metrics that matter (all on `/usage` already)

- Median minutes to first personal card (activation).
- Share rate (distinct sharers ÷ activated).
- D30 user-initiated retention (auto-sync audits excluded by design).
- Leak resolution rate ("not detected forward" ÷ resolved) — the proof metric.
- Referral counts by code (channel ROI).

## 8. Market validation without billing (the pricing smoke test)

Billing itself stays deferred, but willingness to pay is now **measured**, not
assumed — see [icp-and-pricing.md](icp-and-pricing.md) for the full thesis:

- `/pricing` shows real tiers (Audit free / Plus $9 founding / Pro $29
  founding) and captures free, non-binding founding-price reservations into
  the existing waitlist store (`source=pricing-<plan>`, `note=seg=<segment>`).
- The beachhead ICP is **prop-firm evaluation traders** ("your reset fees are
  your discipline tax, itemized"); the landing page's "Who it's for" section
  and the segment question on both forms track segment mix.
- Funnel events `pricing_viewed` → `founding_reserved` quantify conversion.
- Billing gets built when the validation gates in that doc are hit (e.g.
  ≥50 reservations), not before.

## 9. Explicitly out of scope until billing lands

Paid acquisition, monetary referral rewards, the Leak Guarantee and discipline-
dividend pricing mechanics (designed, parked), affiliate revenue shares, and
any payment processing or feature gating (a reservation never paywalls
anything that is free today).
