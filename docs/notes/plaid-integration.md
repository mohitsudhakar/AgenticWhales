# Plaid integration — getting transactions from Robinhood & other brokerages

Goal: replace manual CSV/PDF upload with a **read-only** brokerage connection that
auto-imports trade history, so the coach audit + discipline trend stay fresh
without the user exporting anything. This outline is Plaid-specific (see also
[broker-connect.md](broker-connect.md) for the build-vs-buy framing).

> **Honest caveat on Robinhood:** Plaid's **Investments** coverage is broad, but
> Robinhood support via Plaid has historically been limited/inconsistent (often
> balances-and-holdings, not always full *investment transactions*). Before
> committing, verify Robinhood in Plaid's institution coverage for the
> `investments_transactions` product. If it's thin, fall back to **SnapTrade**
> (stronger retail-brokerage trade coverage incl. Robinhood) or Robinhood's own
> account statement / CSV export. Architecturally everything below is the same —
> only the vendor adapter changes.

## The flow (Plaid Link → access_token → investment transactions)

```
Frontend (Plaid Link)          Backend (web/plaid_api.py)          Plaid
  open Link with link_token  ──▶ POST /api/plaid/link-token ──────▶ /link/token/create
  user picks broker + logs in
  onSuccess(public_token)    ──▶ POST /api/plaid/exchange ────────▶ /item/public_token/exchange
                                  store access_token (encrypted)
  (later / webhook)          ──▶ POST /api/plaid/sync ────────────▶ /investments/transactions/get
                                  normalize → coach.audit_trades → persist
```

## Steps

### 1. Account + config
- Create a Plaid account; get `client_id` + `secret` for **Sandbox → Development → Production**.
- Enable the **Investments** product. Env: `PLAID_CLIENT_ID`, `PLAID_SECRET`, `PLAID_ENV`.
- Add the `plaid` Python SDK (`uv add plaid-python`).

### 2. Create a Link token — `POST /api/plaid/link-token`
- Server calls `/link/token/create` with `products=["investments"]`, the user id,
  `country_codes`, and a `webhook` URL. Returns a short-lived `link_token`.
- Endpoint is per-user (`Depends(get_current_user_id)`) — connecting requires sign-in.

### 3. Open Plaid Link (frontend)
- Load `https://cdn.plaid.com/link/v2/stable/link-initialize.js`.
- `Plaid.create({ token: link_token, onSuccess })`. On success you get a
  `public_token` + institution metadata. Add a **"Connect your brokerage"** button
  beside "Upload CSV / PDF / image" in `coach.html`.

### 4. Exchange + store — `POST /api/plaid/exchange {public_token}`
- `/item/public_token/exchange` → `access_token` + `item_id`.
- Persist in a new table `plaid_items(user_id, item_id, institution, access_token_enc, created_at)`,
  RLS own-rows like `coach_audits`. **Encrypt `access_token` at rest** (app-level
  key, not just RLS) — it's a long-lived credential.

### 5. Sync transactions — `POST /api/plaid/sync`
- `/investments/transactions/get` with a date range; **paginate** on `total_investment_transactions`.
- The response has `investment_transactions` + a `securities` array. Build a
  `security_id → ticker_symbol` map, then normalize each transaction to our
  `Transaction` model (`agenticwhales/transactions/models.py`):
  - `date`, `type` (map Plaid `type`/`subtype` `buy`/`sell` → our `Buy`/`Sell`;
    skip `fee`/`transfer`/`cash`), `symbol` (resolved ticker), `quantity`,
    `price`, `amount`.
- Feed the normalized list straight into the **existing** pipeline:
  `coach.reconstruct_round_trips` → `coach.audit_trades(price_fetcher=...)` →
  `_persist_audit(user_id, report, txns)`. The journey, trend chart, and pre-trade
  history all light up with zero new UI logic.

### 6. Keep it fresh — webhooks + schedule
- Register the Link `webhook` for `INVESTMENTS_TRANSACTIONS` /
  `HISTORICAL_UPDATE` / `DEFAULT_UPDATE`. On webhook → re-run sync for that item.
- Optionally add a periodic resync via the existing scheduler (`web/scheduler.py`)
  so the discipline trend updates even without webhooks.

## Data-model notes
- **Securities resolution** is the fiddly part — always map via the `securities`
  array, not the transaction alone. Options/derivatives appear with non-equity
  security types; the current FIFO reconstruction is long-equity-only, so either
  filter them or extend `reconstruct_round_trips` (already flagged as a gap).
- Corporate actions (splits/dividends) come through as their own types — keep them
  out of round-trip reconstruction but they're fine for context.

## Security & compliance
- **Read-only.** Plaid Investments cannot place orders — keeps us firmly on the
  education/discipline side, never execution.
- Encrypt `access_token` at rest; never expose it to the browser. All Plaid calls
  are server-side with the service role.
- Add a "disconnect" path (`/item/remove`) and delete stored tokens on user request.

## Where it lands
- `web/plaid_api.py` — link-token / exchange / sync endpoints + webhook handler
  (mirror `web/coach_api.py`’s router + `app.include_router`).
- `agenticwhales/dataflows/plaid_normalize.py` — Plaid → `Transaction` mapping (unit-testable with fixtures).
- `docs/supabase-schema.sql` — `plaid_items` table.
- Reuses `coach.audit_trades`, `_persist_audit`, and the whole `/coach` journey.

## Effort
~1–1.5 weeks for a sandbox→dev MVP: Link → exchange → sync → audit → trend, plus the
normalization tests. Production needs Plaid's prod approval + token encryption review.
Test end-to-end against Plaid **Sandbox** (synthetic investments institutions) before
touching real credentials.
