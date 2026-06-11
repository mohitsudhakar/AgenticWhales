-- ============================================================================
-- AgenticWhales — referral_attributions: first-touch growth attribution.
-- Idempotent: safe to run more than once. Paste into the Supabase SQL editor.
--
-- Attribution ONLY (which code brought this signup) — there are no rewards
-- and no billing coupling. One row per user, first touch wins.
-- ============================================================================

create table if not exists public.referral_attributions (
  user_id uuid primary key references auth.users(id) on delete cascade,
  code text not null default '',
  source text not null default '',
  created_at timestamptz not null default now()
);

create index if not exists referral_attributions_code_idx
  on public.referral_attributions (code);

alter table public.referral_attributions enable row level security;

-- Reads: own row only. Writes happen server-side via the service role.
drop policy if exists "referral_attributions: read own" on public.referral_attributions;
create policy "referral_attributions: read own"
  on public.referral_attributions for select
  using (auth.uid() = user_id);
