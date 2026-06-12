-- ============================================================================
-- AgenticWhales — premium features: eval tracker, standing briefs, alerts.
-- Idempotent: safe to run more than once. Paste into the Supabase SQL editor.
--
-- coach_evals: one prop-firm evaluation tracker per user (Pro). Deterministic
-- scorekeeping over the user's own realized P&L — thresholds only; the
-- product never instructs anyone to trade or stop trading.
-- coach_standing_briefs: one standing-brief config per user (Pro) — tickers
-- the weekly cron briefs them on, in brief mode (no trading tail).
-- coach_prefs.email_alerts: opt-in email when a sync detects new rule
-- violations (Plus). Minimized body (counts + rule labels; dollars in-app).
-- ============================================================================

create table if not exists public.coach_evals (
  user_id uuid primary key references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  updated_at timestamptz,
  preset text not null default 'custom',
  account_size double precision not null default 0,
  start_date text not null default '',
  end_date text,
  profit_target double precision not null default 0,
  daily_loss_limit double precision not null default 0,
  max_drawdown double precision not null default 0
);
alter table public.coach_evals enable row level security;
drop policy if exists "coach_evals: read own" on public.coach_evals;
create policy "coach_evals: read own"
  on public.coach_evals for select using (auth.uid() = user_id);

create table if not exists public.coach_standing_briefs (
  user_id uuid primary key references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  updated_at timestamptz,
  tickers jsonb not null default '[]'::jsonb,
  cadence text not null default 'weekly',
  active boolean not null default true,
  last_run_at timestamptz
);
create index if not exists coach_standing_briefs_active_idx
  on public.coach_standing_briefs (active);
alter table public.coach_standing_briefs enable row level security;
drop policy if exists "coach_standing_briefs: read own" on public.coach_standing_briefs;
create policy "coach_standing_briefs: read own"
  on public.coach_standing_briefs for select using (auth.uid() = user_id);

alter table public.coach_prefs
  add column if not exists email_alerts boolean not null default false;
