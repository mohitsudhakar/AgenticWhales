-- ============================================================================
-- AgenticWhales — coach_findings: the forward-validation seam for the coach.
-- Idempotent: safe to run more than once. Paste into the Supabase SQL editor.
--
-- Why (2026-06-08 executive critique, D1): the coach's central claim is
-- "this bias cost you $X, and this rule fixes it". That claim must stay
-- falsifiable. Each emitted finding is persisted with a stable leak_key and
-- the end of the trade window it was computed on; once the user has enough
-- LATER trades, the server re-runs the same detector on the forward slice and
-- records whether the behavior persisted. One OPEN row per (user, leak kind);
-- resolution can immediately open a fresh row, building a longitudinal chain.
-- ============================================================================

create table if not exists public.coach_findings (
  id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  leak_key text not null,                 -- stable kind id, e.g. 'revenge_trading'
  name text not null,                     -- human-facing leak name at emission time
  severity text not null default 'low',
  dollars double precision not null default 0,   -- historical attribution ($, past tense)
  fix text not null default '',           -- the prescribed rule
  window_end text not null default '',    -- last trade date the finding was computed on
  -- forward validation (null until resolved):
  resolved_at timestamptz,
  persisted boolean,                      -- did the leak show up again in later trades?
  forward_dollars double precision,       -- its cost on the forward slice
  n_forward_trades integer
);

create index if not exists coach_findings_user_idx
  on public.coach_findings (user_id, created_at desc);
create index if not exists coach_findings_open_idx
  on public.coach_findings (user_id, leak_key) where resolved_at is null;

alter table public.coach_findings enable row level security;

-- Reads: a user sees only their own findings. Writes happen server-side via
-- the service role (which bypasses RLS) — same posture as coach_audits.
drop policy if exists "coach_findings: read own" on public.coach_findings;
create policy "coach_findings: read own"
  on public.coach_findings for select
  using (auth.uid() = user_id);

-- Verify (optional):
--   select to_regclass('public.coach_findings');
