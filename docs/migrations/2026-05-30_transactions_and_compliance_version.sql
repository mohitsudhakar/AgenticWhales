-- ============================================================================
-- AgenticWhales — apply the two tables missing from the live Supabase project.
-- Idempotent: safe to run more than once. Paste into the Supabase SQL editor
-- (Dashboard → SQL → New query → Run) for project mumozmnbafzwgmgcfpkz.
--
-- Why: server logs showed
--   PGRST205 "Could not find the table 'public.compliance_active_version'"
-- and the new Robinhood-journal persistence needs public.transactions.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1) compliance_active_version — singleton pinning the enforced disclaimer
--    version. auth.active_compliance_version() reads id=1; without it the
--    server falls back to the hard-coded "v1.0".
-- ---------------------------------------------------------------------------
create table if not exists public.compliance_active_version (
  id integer primary key check (id = 1),
  version text not null,
  effective_at timestamptz not null default now()
);

insert into public.compliance_active_version (id, version)
values (1, 'v1.0')
on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- 2) transactions — uploaded brokerage statement rows (Robinhood etc.).
--    Saved when a signed-in user uploads a CSV on the Trade History tab so the
--    cognitive journal can analyze behaviour across sessions. Grouped by an
--    upload `batch_id` so re-uploads don't silently merge.
-- ---------------------------------------------------------------------------
create table if not exists public.transactions (
  id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  batch_id text not null,
  source text not null default 'csv_upload',
  txn_date text not null default '',
  type text not null default 'Other',
  symbol text not null default '',
  description text not null default '',
  quantity double precision not null default 0,
  price double precision not null default 0,
  amount double precision not null default 0,
  created_at timestamptz not null default now()
);

create index if not exists transactions_user_idx
  on public.transactions (user_id, created_at desc);
create index if not exists transactions_batch_idx
  on public.transactions (user_id, batch_id);

alter table public.transactions enable row level security;

drop policy if exists "transactions: read own" on public.transactions;
create policy "transactions: read own"
  on public.transactions for select
  using (auth.uid() = user_id);

drop policy if exists "transactions: insert own" on public.transactions;
create policy "transactions: insert own"
  on public.transactions for insert
  with check (auth.uid() = user_id);

drop policy if exists "transactions: delete own" on public.transactions;
create policy "transactions: delete own"
  on public.transactions for delete
  using (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- Verify (optional): both should return one row each.
--   select * from public.compliance_active_version;
--   select to_regclass('public.transactions');
-- ---------------------------------------------------------------------------
