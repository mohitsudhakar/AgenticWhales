# Broker connect — deferred (note for later)

**Status:** not started — deliberately deferred. CSV/PDF/image upload + OCR covers
onboarding for now. Revisit when manual upload friction becomes the top conversion
blocker (i.e. users start but don't finish because exporting a statement is annoying).

## Why it's the eventual unlock
The behavioral coach + pre-trade check need the user's trade history. Today that
comes from a manual upload. A read-only brokerage connection would:
- auto-import history (no export step) → much higher activation,
- keep the discipline-score trend fresh automatically (the retention loop),
- enable a real *pre-trade* surface tied to the user's live positions/equity.

## Build-vs-buy (lean buy)
- **SnapTrade** — purpose-built for retail brokerage read/trade access (Robinhood,
  Webull, Schwab, IBKR, Coinbase, …). Read-only scopes cover positions + activity,
  which is exactly what the coach needs. Likely the fastest path.
- **Plaid Investments** — broad coverage, holdings + investment *transactions*, but
  transaction granularity/latency varies by institution; better for balances than
  for round-trip reconstruction.
- Direct broker OAuth (Robinhood/IBKR) — most control, most maintenance. Avoid until
  a single broker dominates the user base.

## Scope when we do it
1. `web/broker_api.py` — connect (OAuth/SDK link), list accounts, pull activity →
   normalize into the existing `Transaction` model (reuse `coach.reconstruct_round_trips`).
2. Persist the connection + a periodic sync (reuse the scheduler) to refresh
   `coach_audits` so the trend updates without re-upload.
3. Compliance: **read-only scopes only.** No order placement (keeps us clearly on the
   education/discipline side, not advice/execution).
4. UI: a "Connect your brokerage" button beside "Upload CSV / PDF / image".

## Estimate
~1–2 weeks for a SnapTrade read-only MVP (connect → import → audit → trend),
gated on a vendor account + their sandbox.
