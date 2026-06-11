-- ============================================================================
-- AgenticWhales — coach_rules + coach_rule_events: the enforcement loop.
-- Idempotent: safe to run more than once. Paste into the Supabase SQL editor.
--
-- coach_rules: user-adopted process constraints seeded from coach_findings
-- (one per leak kind + rule kind). Statuses: suggested -> active -> paused.
-- coach_rule_events: deterministic violation records — the event id is a hash
-- of (user | rule | trade identity), so re-auditing merged history can never
-- double-count a violation.
-- ============================================================================

create table if not exists public.coach_rules (
  id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  leak_key text not null,
  rule_kind text not null,
  label text not null default '',
  description text not null default '',
  params jsonb not null default '{}'::jsonb,
  checkable_from_fills boolean not null default false,
  status text not null default 'suggested',   -- suggested | active | paused
  source_finding_id text not null default '',
  adopted_at timestamptz,
  updated_at timestamptz
);

create index if not exists coach_rules_user_idx
  on public.coach_rules (user_id, status);

create table if not exists public.coach_rule_events (
  id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  rule_id text not null,
  rule_kind text not null,
  leak_key text not null default '',
  trade_key text not null default '',
  symbol text not null default '',
  occurred_on text not null default '',       -- entry date of the violating trade
  dollars double precision not null default 0, -- realized P&L of the violating trade(s)
  evidence text not null default ''
);

create index if not exists coach_rule_events_user_idx
  on public.coach_rule_events (user_id, occurred_on desc);

alter table public.coach_rules enable row level security;
alter table public.coach_rule_events enable row level security;

drop policy if exists "coach_rules: read own" on public.coach_rules;
create policy "coach_rules: read own"
  on public.coach_rules for select using (auth.uid() = user_id);

drop policy if exists "coach_rule_events: read own" on public.coach_rule_events;
create policy "coach_rule_events: read own"
  on public.coach_rule_events for select using (auth.uid() = user_id);
