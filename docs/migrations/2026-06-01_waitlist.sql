-- ============================================================================
-- AgenticWhales — waitlist signups table.
-- Idempotent: safe to run more than once. Paste into the Supabase SQL editor
-- (Dashboard → SQL → New query → Run).
--
-- Why: the public landing page's "Join the waitlist" CTA POSTs to
--   /api/waitlist, which persists via web.auth.save_waitlist_signup ->
--   public.waitlist_signups. Without this table the server falls back to the
--   in-memory store (PGRST205 in logs) and signups are lost on restart.
--
-- Browse / export the signups in Dashboard → Table editor → waitlist_signups
-- (a spreadsheet view with CSV download), or via GET /api/waitlist/export.csv
-- as the admin.
-- ============================================================================

create table if not exists public.waitlist_signups (
  id text primary key,
  email text not null unique,
  name text not null default '',
  company text not null default '',
  note text not null default '',
  source text not null default 'landing',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists waitlist_signups_created_idx
  on public.waitlist_signups (created_at desc);

-- RLS on, with NO policies: this table is written ONLY by the server using the
-- service-role key (which bypasses RLS). Anonymous landing-page visitors never
-- touch Postgres directly — they POST to /api/waitlist — so the public
-- (anon/authenticated) roles must have no read or write access to the raw
-- emails. "RLS enabled + zero policies" = deny-all for non-service roles.
alter table public.waitlist_signups enable row level security;

-- ---------------------------------------------------------------------------
-- Verify (optional):
--   select to_regclass('public.waitlist_signups');   -- should be non-null
--   select count(*) from public.waitlist_signups;
-- ---------------------------------------------------------------------------
