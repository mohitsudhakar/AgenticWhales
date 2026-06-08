# SnapTrade brokerage connect (implemented)

Chosen over Plaid for the connect feature because SnapTrade has stronger
**retail-brokerage trade coverage** (incl. Robinhood) and returns real
*investment activities* (buys/sells), which is what the coach needs. The Plaid
outline ([plaid-integration.md](plaid-integration.md)) stays as a reference/fallback.

Status: **wired end-to-end and tested offline.** It can't be exercised live in this
repo without SnapTrade credentials + a real brokerage link, but every seam is in
place and config-gated, so it lights up the moment credentials are set.

## How it works

```
UI "Connect brokerage"  ─▶ POST /api/snaptrade/connect ─▶ register user (once),
                            open SnapTrade Connection Portal (redirectURI)
user links broker in portal, returns to /coach
UI "Sync brokerage"     ─▶ POST /api/snaptrade/sync ────▶ /activities → normalize →
                            coach.audit_trades → persist → same dashboard + trend
```

- `agenticwhales/dataflows/snaptrade_client.py` — tiny signed HTTP client. The
  official SDK has a Python-3.12 typing bug, so we sign requests ourselves; the
  signing is a verbatim port of the SDK (`HMAC-SHA256` over a sorted, compact
  `{content, path, query}` JSON → base64 `Signature` header). Golden-value tested.
- `agenticwhales/dataflows/snaptrade_normalize.py` — SnapTrade activity →
  `Transaction` (robust ticker extraction across the nested shapes; keeps buy/sell,
  skips dividends/transfers/options for now).
- `web/snaptrade_api.py` — `status` / `connect` / `sync`, reusing `coach.audit_trades`
  + `_persist_audit` so the connected data flows into the exact same journey,
  dashboard, trend, and pre-trade history as an upload.
- `web/static/coach.html` — a "Connect brokerage" button that appears only when
  SnapTrade is configured; flips to "Sync brokerage" once linked.

## Setup (to go live)

1. **SnapTrade account** → get `clientId` + `consumerKey` (Sandbox first).
2. Env: `SNAPTRADE_CLIENT_ID`, `SNAPTRADE_CONSUMER_KEY`. (When unset, the endpoints
   report `configured: false` and the UI hides the button — no errors.)
3. Apply the new table from `docs/supabase-schema.sql`:
   ```sql
   create table if not exists public.snaptrade_users (
     user_id uuid primary key references auth.users(id) on delete cascade,
     st_user_id text not null, st_user_secret text not null,
     updated_at timestamptz not null default now());
   alter table public.snaptrade_users enable row level security;
   ```
4. In the SnapTrade dashboard, set the Connection Portal **redirect** to your
   `/coach` URL (the UI passes `customRedirect` too).
5. Sign in on `/coach` → **Connect brokerage** → link in the portal → **Sync brokerage**.

## Notes & caveats
- **Read-only.** We only register users, open the portal, and read accounts/activities.
  Never orders.
- **Secret handling.** `st_user_secret` is a long-lived read credential — RLS keeps it
  service-role-only; **encrypt it at rest** before production.
- **Normalization gaps.** FIFO round-trips are long-equity-only, so options legs are
  skipped (same limitation as CSV/PDF). Extend `reconstruct_round_trips` +
  `snaptrade_normalize` together when adding options.
- **Hands-free timeline updates (implemented).** Uploads and syncs both merge into
  `coach_trades` (deduped) and the dashboard charts month-by-month discipline from
  that union — so the user never re-uploads old data. A **nightly cron**
  (`_run_snaptrade_sync` in `web/scheduler.py`, 05:30 UTC, leader-only) walks every
  row in `snaptrade_users` and calls `snaptrade_api.sync_user(uid)` to fold in new
  activity automatically — no-op unless SnapTrade is configured. (SnapTrade webhooks
  could trigger an out-of-band resync too; not yet wired.)
- **Sync is synchronous** today (activities pull + audit). If large accounts make it
  slow, move it onto the same async job + SSE progress used by uploads.
- Verify Robinhood specifically in SnapTrade's supported-brokerage list for your
  region before promising it in the UI.
