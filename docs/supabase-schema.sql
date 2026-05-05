-- AgenticWhales — Supabase schema
--
-- Run this in your Supabase project (SQL editor or `supabase db push`).
-- It creates two tables, RLS policies, and an atomic `increment_usage` RPC.
--
--   profiles      — one row per Auth user (id = auth.users.id), holds the
--                   chosen display name and the user's tier.
--   usage_daily   — (user_id, day) → count. Powers the per-day quota.
--
-- All policies are written so that a user can ONLY read/write their own row.

-- ---------- profiles ----------
create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  username text not null default 'trader',
  tier text not null default 'novice'
    check (tier in ('novice', 'intermediate', 'master')),
  created_at timestamptz not null default now()
);

alter table public.profiles enable row level security;

drop policy if exists "profiles: read own" on public.profiles;
create policy "profiles: read own"
  on public.profiles for select
  using (auth.uid() = id);

drop policy if exists "profiles: insert own" on public.profiles;
create policy "profiles: insert own"
  on public.profiles for insert
  with check (auth.uid() = id);

drop policy if exists "profiles: update own" on public.profiles;
create policy "profiles: update own"
  on public.profiles for update
  using (auth.uid() = id)
  with check (auth.uid() = id);

-- ---------- usage_daily ----------
create table if not exists public.usage_daily (
  user_id uuid not null references auth.users(id) on delete cascade,
  day date not null,
  count integer not null default 0 check (count >= 0),
  updated_at timestamptz not null default now(),
  primary key (user_id, day)
);

alter table public.usage_daily enable row level security;

drop policy if exists "usage: read own" on public.usage_daily;
create policy "usage: read own"
  on public.usage_daily for select
  using (auth.uid() = user_id);

drop policy if exists "usage: insert own" on public.usage_daily;
create policy "usage: insert own"
  on public.usage_daily for insert
  with check (auth.uid() = user_id);

drop policy if exists "usage: update own" on public.usage_daily;
create policy "usage: update own"
  on public.usage_daily for update
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- ---------- atomic increment RPC ----------
-- Used by the web client when an analysis is created. Returns the new count
-- for today. Day key is computed in UTC so quotas roll at 00:00 UTC.
create or replace function public.increment_usage()
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  uid uuid := auth.uid();
  today date := (now() at time zone 'utc')::date;
  new_count integer;
begin
  if uid is null then
    raise exception 'not authenticated';
  end if;

  insert into public.usage_daily (user_id, day, count, updated_at)
  values (uid, today, 1, now())
  on conflict (user_id, day)
  do update set count = public.usage_daily.count + 1, updated_at = now()
  returning count into new_count;

  return new_count;
end;
$$;

grant execute on function public.increment_usage() to authenticated;

-- ---------- sessions ----------
-- Per-user index of analysis sessions. The full payload (agent reports,
-- messages, stats) lives on the AgenticWhales server's disk; this table
-- tracks what each user has run so the sidebar can list it.
create table if not exists public.sessions (
  id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  ticker text,
  analysis_date text,
  status text,
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  -- Denormalised stats — kept alongside the jsonb data column so they can be
  -- queried without a JSON path expression, indexed, and aggregated cheaply.
  tokens_in integer not null default 0,
  tokens_out integer not null default 0,
  tokens_total integer generated always as (tokens_in + tokens_out) stored,
  llm_calls integer not null default 0,
  tool_calls integer not null default 0,
  quick_model text,
  deep_model text,
  data jsonb
);
-- Idempotent ALTER for projects that created `sessions` before these columns
-- existed. Postgres ignores any column that's already there.
alter table public.sessions add column if not exists tokens_in integer not null default 0;
alter table public.sessions add column if not exists tokens_out integer not null default 0;
alter table public.sessions add column if not exists tokens_total integer generated always as (tokens_in + tokens_out) stored;
alter table public.sessions add column if not exists llm_calls integer not null default 0;
alter table public.sessions add column if not exists tool_calls integer not null default 0;
alter table public.sessions add column if not exists quick_model text;
alter table public.sessions add column if not exists deep_model text;
create index if not exists sessions_user_id_idx on public.sessions (user_id, created_at desc);

alter table public.sessions enable row level security;

drop policy if exists "sessions: read own" on public.sessions;
create policy "sessions: read own"
  on public.sessions for select
  using (auth.uid() = user_id);

drop policy if exists "sessions: insert own" on public.sessions;
create policy "sessions: insert own"
  on public.sessions for insert
  with check (auth.uid() = user_id);

drop policy if exists "sessions: update own" on public.sessions;
create policy "sessions: update own"
  on public.sessions for update
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

drop policy if exists "sessions: delete own" on public.sessions;
create policy "sessions: delete own"
  on public.sessions for delete
  using (auth.uid() = user_id);

-- ---------- batches ----------
create table if not exists public.batches (
  id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  analysis_date text,
  status text,
  ticker_count integer,
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  tokens_in integer not null default 0,
  tokens_out integer not null default 0,
  tokens_total integer generated always as (tokens_in + tokens_out) stored,
  llm_calls integer not null default 0,
  tool_calls integer not null default 0,
  quick_model text,
  deep_model text,
  data jsonb
);
alter table public.batches add column if not exists tokens_in integer not null default 0;
alter table public.batches add column if not exists tokens_out integer not null default 0;
alter table public.batches add column if not exists tokens_total integer generated always as (tokens_in + tokens_out) stored;
alter table public.batches add column if not exists llm_calls integer not null default 0;
alter table public.batches add column if not exists tool_calls integer not null default 0;
alter table public.batches add column if not exists quick_model text;
alter table public.batches add column if not exists deep_model text;
create index if not exists batches_user_id_idx on public.batches (user_id, created_at desc);

alter table public.batches enable row level security;

drop policy if exists "batches: read own" on public.batches;
create policy "batches: read own"
  on public.batches for select
  using (auth.uid() = user_id);

drop policy if exists "batches: insert own" on public.batches;
create policy "batches: insert own"
  on public.batches for insert
  with check (auth.uid() = user_id);

drop policy if exists "batches: update own" on public.batches;
create policy "batches: update own"
  on public.batches for update
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

drop policy if exists "batches: delete own" on public.batches;
create policy "batches: delete own"
  on public.batches for delete
  using (auth.uid() = user_id);
