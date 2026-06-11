-- ============================================================================
-- AgenticWhales — coach_prefs + coach_digests + coach_partners.
-- Idempotent: safe to run more than once. Paste into the Supabase SQL editor.
--
-- coach_prefs: explicit OPT-IN email preferences (digest emails are off by
-- default; one-click unsubscribe token).
-- coach_digests: one weekly in-app digest per user per week (pk
-- 'user_id|week_start' = idempotent cron). Email bodies carry counts only;
-- the payload here is the in-app source of truth.
-- coach_partners: accountability partners — double-opt-in, compliance-only
-- view (rule status; never P&L, symbols, or dollars).
-- ============================================================================

create table if not exists public.coach_prefs (
  user_id uuid primary key references auth.users(id) on delete cascade,
  email_digest boolean not null default false,
  digest_email text not null default '',
  unsubscribe_token text not null default '',
  updated_at timestamptz
);

create index if not exists coach_prefs_token_idx
  on public.coach_prefs (unsubscribe_token);

create table if not exists public.coach_digests (
  id text primary key,                       -- '<user_id>|<week_start>'
  user_id uuid not null references auth.users(id) on delete cascade,
  week_start text not null,                  -- ISO Monday of the summarized week
  payload jsonb not null default '{}'::jsonb,
  emailed boolean not null default false,
  created_at timestamptz not null default now()
);

create index if not exists coach_digests_user_idx
  on public.coach_digests (user_id, week_start desc);

create table if not exists public.coach_partners (
  id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  partner_email text not null default '',
  status text not null default 'invited',    -- invited | active | revoked
  view_token text not null default '',
  invited_at timestamptz not null default now(),
  confirmed_at timestamptz,
  revoked_at timestamptz
);

create index if not exists coach_partners_user_idx
  on public.coach_partners (user_id, status);

alter table public.coach_prefs enable row level security;
alter table public.coach_digests enable row level security;
alter table public.coach_partners enable row level security;

drop policy if exists "coach_prefs: read own" on public.coach_prefs;
create policy "coach_prefs: read own"
  on public.coach_prefs for select using (auth.uid() = user_id);

drop policy if exists "coach_digests: read own" on public.coach_digests;
create policy "coach_digests: read own"
  on public.coach_digests for select using (auth.uid() = user_id);

drop policy if exists "coach_partners: read own" on public.coach_partners;
create policy "coach_partners: read own"
  on public.coach_partners for select using (auth.uid() = user_id);
-- Partner views are served server-side via the service role after a token
-- check — partners have no Supabase identity, so no anon policy exists.
