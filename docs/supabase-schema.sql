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

-- ===========================================================================
-- Phase 1: Recipes + Paper-Trade + Risk Guards + Cost Spine + Observability
-- ===========================================================================
-- Every block is idempotent (create-if-not-exists / drop-then-create on
-- policies). Re-running the whole file is safe. Adopt Alembic later when
-- migration history needs to be tracked across environments.

-- ---------- recipes ----------
create table if not exists public.recipes (
  id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null,
  tickers text[] not null,
  exchange_code text not null default 'XNYS',
  analysts text[] not null,
  llm_provider text not null,
  quick_model text not null,
  deep_model text not null,
  bull_model text not null,
  bear_model text not null,
  research_depth integer not null default 1,
  output_language text not null default 'English',
  schedule_kind text not null check (schedule_kind in ('cron','interval','manual')),
  schedule_expr text,
  misfire_grace_seconds integer not null default 300,
  market_hours_only boolean not null default true,
  max_concurrent_tickers integer not null default 5,
  trigger_conditions jsonb,
  output_policy text not null default 'notify'
    check (output_policy in ('notify','paper_trade','alert_conviction','assist_only')),
  conviction_threshold integer not null default 7
    check (conviction_threshold between 1 and 10),
  max_daily_token_cost_usd numeric(10,4) not null default 5.0,
  consecutive_failures integer not null default 0,
  status text not null default 'active'
    check (status in ('active','paused','killed','failed')),
  last_run_at timestamptz,
  next_run_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists recipes_user_status_idx on public.recipes (user_id, status);
create index if not exists recipes_next_run_idx on public.recipes (status, next_run_at);
alter table public.recipes enable row level security;
drop policy if exists "recipes: read own" on public.recipes;
create policy "recipes: read own" on public.recipes for select using (auth.uid() = user_id);
drop policy if exists "recipes: insert own" on public.recipes;
create policy "recipes: insert own" on public.recipes for insert with check (auth.uid() = user_id);
drop policy if exists "recipes: update own" on public.recipes;
create policy "recipes: update own" on public.recipes for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
drop policy if exists "recipes: delete own" on public.recipes;
create policy "recipes: delete own" on public.recipes for delete using (auth.uid() = user_id);

-- Link sessions back to the recipe that spawned them (NULL for ad-hoc).
alter table public.sessions add column if not exists recipe_id text references public.recipes(id) on delete set null;
alter table public.sessions add column if not exists fire_id text;
create index if not exists sessions_recipe_idx on public.sessions (recipe_id, created_at desc);

-- ---------- paper_accounts ----------
create table if not exists public.paper_accounts (
  user_id uuid primary key references auth.users(id) on delete cascade,
  starting_cash numeric(20,8) not null default 100000,
  cash numeric(20,8) not null default 100000,
  short_collateral_reserved numeric(20,8) not null default 0,
  realized_pnl numeric(20,8) not null default 0,
  nav_open_today numeric(20,8),
  nav_open_today_date date,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
alter table public.paper_accounts enable row level security;
drop policy if exists "paper_accounts: read own" on public.paper_accounts;
create policy "paper_accounts: read own" on public.paper_accounts for select using (auth.uid() = user_id);
drop policy if exists "paper_accounts: insert own" on public.paper_accounts;
create policy "paper_accounts: insert own" on public.paper_accounts for insert with check (auth.uid() = user_id);
drop policy if exists "paper_accounts: update own" on public.paper_accounts;
create policy "paper_accounts: update own" on public.paper_accounts for update using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- ---------- paper_positions ----------
create table if not exists public.paper_positions (
  user_id uuid not null references auth.users(id) on delete cascade,
  ticker text not null,
  qty numeric(20,8) not null,
  avg_cost numeric(20,8) not null,
  last_price numeric(20,8),
  last_price_at timestamptz,
  updated_at timestamptz not null default now(),
  primary key (user_id, ticker)
);
create index if not exists paper_positions_user_idx on public.paper_positions (user_id);
alter table public.paper_positions enable row level security;
drop policy if exists "paper_positions: read own" on public.paper_positions;
create policy "paper_positions: read own" on public.paper_positions for select using (auth.uid() = user_id);
drop policy if exists "paper_positions: insert own" on public.paper_positions;
create policy "paper_positions: insert own" on public.paper_positions for insert with check (auth.uid() = user_id);
drop policy if exists "paper_positions: update own" on public.paper_positions;
create policy "paper_positions: update own" on public.paper_positions for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
drop policy if exists "paper_positions: delete own" on public.paper_positions;
create policy "paper_positions: delete own" on public.paper_positions for delete using (auth.uid() = user_id);

-- ---------- paper_orders ----------
create table if not exists public.paper_orders (
  id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  session_id text references public.sessions(id) on delete set null,
  recipe_id text references public.recipes(id) on delete set null,
  fire_id text not null,
  ticker text not null,
  side text not null check (side in ('buy','sell','short','cover')),
  qty numeric(20,8) not null,
  fill_price numeric(20,8) not null,
  slippage_bps integer not null default 0,
  gross_value numeric(20,8) not null,
  pm_rating text not null,
  conviction_score integer,
  expected_return_pct numeric(8,4),
  expected_volatility_pct numeric(8,4),
  prob_of_profit numeric(5,4),
  expected_hold_days integer,
  kelly_fraction numeric(8,6),
  status text not null check (status in ('filled','blocked','clamped')),
  created_at timestamptz not null default now()
);
create unique index if not exists paper_orders_idem on public.paper_orders (user_id, fire_id, ticker, side);
create index if not exists paper_orders_user_idx on public.paper_orders (user_id, created_at desc);
create index if not exists paper_orders_recipe_idx on public.paper_orders (recipe_id, created_at desc);
alter table public.paper_orders enable row level security;
drop policy if exists "paper_orders: read own" on public.paper_orders;
create policy "paper_orders: read own" on public.paper_orders for select using (auth.uid() = user_id);
drop policy if exists "paper_orders: insert own" on public.paper_orders;
create policy "paper_orders: insert own" on public.paper_orders for insert with check (auth.uid() = user_id);

-- ---------- conviction_scores ----------
create table if not exists public.conviction_scores (
  id bigserial primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  recipe_id text references public.recipes(id) on delete cascade,
  session_id text references public.sessions(id) on delete set null,
  ticker text not null,
  rating text not null,
  conviction_score integer not null check (conviction_score between 1 and 10),
  expected_return_pct numeric(8,4),
  expected_volatility_pct numeric(8,4),
  prob_of_profit numeric(5,4),
  recorded_at timestamptz not null default now()
);
create index if not exists conviction_recipe_idx on public.conviction_scores (recipe_id, recorded_at desc);
create index if not exists conviction_user_ticker_idx on public.conviction_scores (user_id, ticker, recorded_at desc);
alter table public.conviction_scores enable row level security;
drop policy if exists "conviction: read own" on public.conviction_scores;
create policy "conviction: read own" on public.conviction_scores for select using (auth.uid() = user_id);
drop policy if exists "conviction: insert own" on public.conviction_scores;
create policy "conviction: insert own" on public.conviction_scores for insert with check (auth.uid() = user_id);

-- ---------- risk_limits ----------
create table if not exists public.risk_limits (
  user_id uuid primary key references auth.users(id) on delete cascade,
  max_position_pct numeric(6,4) not null default 0.10,
  max_daily_drawdown_pct numeric(6,4) not null default 0.03,
  max_slippage_bps integer not null default 10,
  -- Deci-Kelly default until calibration data exists (Demis review D2)
  kelly_fraction_cap numeric(5,4) not null default 0.10,
  -- Adaptive reasoning depth threshold (Demis review D4); 0 disables
  adaptive_depth_variance_threshold numeric(5,4) not null default 0.30,
  daily_spend_cap_usd numeric(10,4) not null default 25.0,
  monthly_spend_cap_usd numeric(10,4) not null default 500.0,
  allow_shorts boolean not null default false,
  global_kill_switch boolean not null default false,
  updated_at timestamptz not null default now()
);
alter table public.risk_limits enable row level security;
drop policy if exists "risk_limits: read own" on public.risk_limits;
create policy "risk_limits: read own" on public.risk_limits for select using (auth.uid() = user_id);
drop policy if exists "risk_limits: insert own" on public.risk_limits;
create policy "risk_limits: insert own" on public.risk_limits for insert with check (auth.uid() = user_id);
drop policy if exists "risk_limits: update own" on public.risk_limits;
create policy "risk_limits: update own" on public.risk_limits for update using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- ---------- risk_events ----------
create table if not exists public.risk_events (
  id bigserial primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  recipe_id text references public.recipes(id) on delete cascade,
  session_id text references public.sessions(id) on delete set null,
  ticker text,
  rule text not null,
  details jsonb not null,
  created_at timestamptz not null default now()
);
create index if not exists risk_events_user_idx on public.risk_events (user_id, created_at desc);
alter table public.risk_events enable row level security;
drop policy if exists "risk_events: read own" on public.risk_events;
create policy "risk_events: read own" on public.risk_events for select using (auth.uid() = user_id);
drop policy if exists "risk_events: insert own" on public.risk_events;
create policy "risk_events: insert own" on public.risk_events for insert with check (auth.uid() = user_id);

-- ---------- recipe_usage ----------
create table if not exists public.recipe_usage (
  recipe_id text not null references public.recipes(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  usage_date date not null,
  token_cost_usd numeric(10,4) not null default 0,
  input_tokens bigint not null default 0,
  output_tokens bigint not null default 0,
  reasoning_tokens bigint not null default 0,
  run_count integer not null default 0,
  failure_count integer not null default 0,
  primary key (recipe_id, usage_date)
);
alter table public.recipe_usage enable row level security;
drop policy if exists "recipe_usage: read own" on public.recipe_usage;
create policy "recipe_usage: read own" on public.recipe_usage for select using (auth.uid() = user_id);

-- ---------- user_spend_daily (per-user spend cap denormalized) ----------
create table if not exists public.user_spend_daily (
  user_id uuid not null references auth.users(id) on delete cascade,
  usage_date date not null,
  total_cost_usd numeric(10,4) not null default 0,
  primary key (user_id, usage_date)
);
alter table public.user_spend_daily enable row level security;
drop policy if exists "user_spend_daily: read own" on public.user_spend_daily;
create policy "user_spend_daily: read own" on public.user_spend_daily for select using (auth.uid() = user_id);

-- ---------- llm_call_log (replayability — Sanjay S3) ----------
create table if not exists public.llm_call_log (
  id bigserial primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  session_id text references public.sessions(id) on delete cascade,
  agent_name text not null,
  provider text not null,
  model text not null,
  input_hash text not null,
  output_hash text,
  raw_payload_uri text,
  input_tokens integer,
  output_tokens integer,
  reasoning_tokens integer,
  cost_usd numeric(10,6),
  latency_ms integer,
  status text not null check (status in ('ok','timeout','error','retried','dead_letter')),
  error_message text,
  created_at timestamptz not null default now()
);
create index if not exists llm_call_session_idx on public.llm_call_log (session_id, created_at);
create index if not exists llm_call_user_day_idx on public.llm_call_log (user_id, created_at desc);
alter table public.llm_call_log enable row level security;
drop policy if exists "llm_call_log: read own" on public.llm_call_log;
create policy "llm_call_log: read own" on public.llm_call_log for select using (auth.uid() = user_id);

-- ---------- audit_log (impersonation + pricing changes; service-role only) ----------
create table if not exists public.audit_log (
  id bigserial primary key,
  actor text not null,
  action text not null,
  target_user_id uuid,
  target_resource text,
  metadata jsonb,
  created_at timestamptz not null default now()
);
create index if not exists audit_log_target_idx on public.audit_log (target_user_id, created_at desc);
alter table public.audit_log enable row level security;
-- No user-facing policies — service-role only.

-- ---------- llm_pricing (versioned, append-only) ----------
create table if not exists public.llm_pricing (
  id bigserial primary key,
  provider text not null,
  model text not null,
  input_per_1m_usd numeric(10,6) not null,
  output_per_1m_usd numeric(10,6) not null,
  cache_read_per_1m_usd numeric(10,6),
  reasoning_per_1m_usd numeric(10,6),
  effective_at timestamptz not null,
  source_url text,
  unique (provider, model, effective_at)
);
create index if not exists llm_pricing_lookup_idx on public.llm_pricing (provider, model, effective_at desc);
alter table public.llm_pricing enable row level security;
-- Read accessible to all authenticated users; writes service-role-only.
drop policy if exists "llm_pricing: read all" on public.llm_pricing;
create policy "llm_pricing: read all" on public.llm_pricing for select to authenticated using (true);

-- Seed pricing for currently-supported models (Phase 1 minimum set).
-- Source URLs are the provider's pricing page at the effective_at date.
insert into public.llm_pricing (provider, model, input_per_1m_usd, output_per_1m_usd, reasoning_per_1m_usd, effective_at, source_url)
values
  ('google',   'gemini-3-flash-preview',     0.075, 0.30,  null,  '2026-01-01', 'https://ai.google.dev/pricing'),
  ('google',   'gemini-3.1-pro-preview',     1.25,  10.00, null,  '2026-01-01', 'https://ai.google.dev/pricing'),
  ('deepseek', 'deepseek-v4',                0.27,  1.10,  null,  '2026-01-01', 'https://api-docs.deepseek.com/quick_start/pricing'),
  ('openai',   'gpt-5.4-mini',               0.15,  0.60,  null,  '2026-01-01', 'https://openai.com/api/pricing/'),
  ('openai',   'gpt-5.4',                    2.50,  10.00, null,  '2026-01-01', 'https://openai.com/api/pricing/'),
  ('anthropic','claude-4.6-haiku',           0.80,  4.00,  null,  '2026-01-01', 'https://docs.anthropic.com/pricing'),
  ('anthropic','claude-4.6-sonnet',          3.00,  15.00, null,  '2026-01-01', 'https://docs.anthropic.com/pricing')
on conflict (provider, model, effective_at) do nothing;

-- ---------- user_api_keys (envelope-encrypted; pgsodium required) ----------
-- Encryption helper functions live in Supabase Vault; for environments
-- without pgsodium, the ciphertext column stores base64-encoded plaintext.
-- The decision to upgrade to pgsodium is operational, not a schema concern.
create table if not exists public.user_api_keys (
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null,
  ciphertext text not null,
  last_used_at timestamptz,
  rotated_at timestamptz not null default now(),
  primary key (user_id, provider)
);
alter table public.user_api_keys enable row level security;
-- Reads return metadata only; never expose the ciphertext to the client.
drop policy if exists "user_api_keys: read meta own" on public.user_api_keys;
create policy "user_api_keys: read meta own" on public.user_api_keys for select using (auth.uid() = user_id);
drop policy if exists "user_api_keys: insert own" on public.user_api_keys;
create policy "user_api_keys: insert own" on public.user_api_keys for insert with check (auth.uid() = user_id);
drop policy if exists "user_api_keys: delete own" on public.user_api_keys;
create policy "user_api_keys: delete own" on public.user_api_keys for delete using (auth.uid() = user_id);

-- ---------- decision_outcomes (Demis D1 — substrate for Phase 2 learning loop) ----------
create table if not exists public.decision_outcomes (
  paper_order_id text primary key references public.paper_orders(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  ticker text not null,
  predicted_return_pct numeric(8,4),
  predicted_volatility_pct numeric(8,4),
  predicted_prob_of_profit numeric(5,4),
  predicted_hold_days integer,
  realized_return_pct numeric(8,4),
  realized_at timestamptz,
  hit boolean,
  brier_component numeric(10,6),
  resolved_at timestamptz not null default now()
);
create index if not exists decision_outcomes_user_idx on public.decision_outcomes (user_id, resolved_at desc);
alter table public.decision_outcomes enable row level security;
drop policy if exists "decision_outcomes: read own" on public.decision_outcomes;
create policy "decision_outcomes: read own" on public.decision_outcomes for select using (auth.uid() = user_id);

-- ---------- calibration_scores (Demis D2 — populated in Phase 2) ----------
create table if not exists public.calibration_scores (
  user_id uuid not null references auth.users(id) on delete cascade,
  regime text not null,
  window_days integer not null,
  brier_score numeric(10,6) not null,
  reliability_curve jsonb not null,
  computed_at timestamptz not null default now(),
  primary key (user_id, regime, window_days, computed_at)
);
alter table public.calibration_scores enable row level security;
drop policy if exists "calibration_scores: read own" on public.calibration_scores;
create policy "calibration_scores: read own" on public.calibration_scores for select using (auth.uid() = user_id);

-- ---------- scheduler_leader (Sanjay S1 — leader election) ----------
create table if not exists public.scheduler_leader (
  id integer primary key default 1 check (id = 1),
  worker_id text not null,
  heartbeat_at timestamptz not null default now()
);
-- Service-role only; no user policies.
alter table public.scheduler_leader enable row level security;

-- ===========================================================================
-- Phase 1.5: paper_place_order RPC (atomic, per-user-serialized)
-- ===========================================================================
-- The Phase-1 Python implementation does insert-order + upsert-position +
-- update-account as three separate PostgREST calls. A crash between calls
-- leaves the books desynced. This SECURITY DEFINER function runs the entire
-- sequence inside one transaction with a per-user advisory lock so concurrent
-- orders for the same user serialize cleanly. The Python wrapper in
-- `agenticwhales/paper.py::place_order` calls this RPC first and only falls
-- back to the Python flow if the function isn't installed (e.g. local dev
-- without the migration applied).

create or replace function public.paper_place_order(
  p_user_id              uuid,
  p_fire_id              text,
  p_recipe_id            text,
  p_session_id           text,
  p_ticker               text,
  p_side                 text,
  p_qty                  numeric,
  p_fill_price           numeric,
  p_slippage_bps         integer,
  p_pm_rating            text,
  p_conviction           integer,
  p_expected_return_pct  numeric,
  p_expected_volatility_pct numeric,
  p_prob_of_profit       numeric,
  p_expected_hold_days   integer,
  p_kelly_fraction       numeric,
  p_status               text
) returns jsonb
language plpgsql security definer set search_path = public as $$
declare
  v_order_id      text;
  v_existing      record;
  v_pos           record;
  v_account       record;
  v_cash          numeric;
  v_realized      numeric;
  v_short_coll    numeric;
  v_starting_cash numeric;
  v_new_qty       numeric;
  v_new_avg       numeric;
  v_cover_qty     numeric;
  v_sell_qty      numeric;
  v_gross         numeric;
begin
  -- Serialize per-user. The lock is xact-scoped so it auto-releases at commit.
  perform pg_advisory_xact_lock(hashtext(p_user_id::text));

  -- Idempotency: same (user, fire, ticker, side) → return the prior order.
  select id into v_order_id
    from paper_orders
    where user_id = p_user_id and fire_id = p_fire_id
      and ticker = p_ticker and side = p_side
    limit 1;
  if v_order_id is not null then
    return jsonb_build_object('order_id', v_order_id, 'idempotent', true);
  end if;

  v_order_id := encode(gen_random_bytes(12), 'hex');
  v_gross    := p_qty * p_fill_price;

  -- 1. Insert the order row.
  insert into paper_orders (
    id, user_id, session_id, recipe_id, fire_id, ticker, side, qty, fill_price,
    slippage_bps, gross_value, pm_rating, conviction_score,
    expected_return_pct, expected_volatility_pct, prob_of_profit,
    expected_hold_days, kelly_fraction, status
  ) values (
    v_order_id, p_user_id, p_session_id, p_recipe_id, p_fire_id,
    upper(p_ticker), p_side, p_qty, p_fill_price,
    p_slippage_bps, v_gross, p_pm_rating, p_conviction,
    p_expected_return_pct, p_expected_volatility_pct, p_prob_of_profit,
    p_expected_hold_days, p_kelly_fraction, p_status
  );

  -- 2. Apply the fill to positions + cash. Blocked orders end here.
  if p_status not in ('filled', 'clamped') then
    return jsonb_build_object('order_id', v_order_id, 'idempotent', false,
                              'status', p_status);
  end if;

  -- Load (or seed) the paper account row.
  select * into v_account from paper_accounts where user_id = p_user_id;
  if not found then
    insert into paper_accounts (user_id, starting_cash, cash, realized_pnl)
    values (p_user_id, 100000, 100000, 0)
    returning * into v_account;
  end if;
  v_cash       := v_account.cash;
  v_realized   := v_account.realized_pnl;
  v_short_coll := coalesce(v_account.short_collateral_reserved, 0);

  -- Load any existing position for this ticker.
  select * into v_pos
    from paper_positions
    where user_id = p_user_id and ticker = upper(p_ticker);

  if p_side = 'buy' then
    if found and v_pos.qty < 0 then
      -- Buying through a short: cover first, then open long with remainder.
      v_cover_qty := least(p_qty, abs(v_pos.qty));
      v_realized  := v_realized + (v_pos.avg_cost - p_fill_price) * v_cover_qty;
      v_short_coll := greatest(0, v_short_coll - v_cover_qty * v_pos.avg_cost);
      v_new_qty   := v_pos.qty + v_cover_qty + (p_qty - v_cover_qty);
      v_new_avg   := case when (p_qty - v_cover_qty) > 0 then p_fill_price else v_pos.avg_cost end;
    else
      v_new_qty := coalesce(v_pos.qty, 0) + p_qty;
      v_new_avg := case when v_new_qty > 0 then
        (coalesce(v_pos.qty, 0) * coalesce(v_pos.avg_cost, 0) + p_qty * p_fill_price) / v_new_qty
      else p_fill_price end;
    end if;
    v_cash := v_cash - p_qty * p_fill_price;

  elsif p_side = 'sell' then
    if not found or v_pos.qty <= 0 then
      -- No long to sell against; treat as no-op for safety.
      v_new_qty := coalesce(v_pos.qty, 0);
      v_new_avg := coalesce(v_pos.avg_cost, 0);
    else
      v_sell_qty := least(p_qty, v_pos.qty);
      v_realized := v_realized + (p_fill_price - v_pos.avg_cost) * v_sell_qty;
      v_cash     := v_cash + v_sell_qty * p_fill_price;
      v_new_qty  := v_pos.qty - v_sell_qty;
      v_new_avg  := case when v_new_qty > 0 then v_pos.avg_cost else 0 end;
    end if;

  elsif p_side = 'short' then
    declare
      v_total_short numeric := coalesce(abs(v_pos.qty), 0) + p_qty;
    begin
      v_new_avg := case when v_total_short > 0 then
        (coalesce(abs(v_pos.qty), 0) * coalesce(v_pos.avg_cost, 0) + p_qty * p_fill_price) / v_total_short
      else p_fill_price end;
      v_new_qty := -v_total_short;
      v_cash    := v_cash + p_qty * p_fill_price;       -- proceeds credited
      v_short_coll := v_short_coll + p_qty * p_fill_price; -- reserved as collateral
    end;

  elsif p_side = 'cover' then
    if not found or v_pos.qty >= 0 then
      v_new_qty := coalesce(v_pos.qty, 0);
      v_new_avg := coalesce(v_pos.avg_cost, 0);
    else
      v_cover_qty  := least(p_qty, abs(v_pos.qty));
      v_realized   := v_realized + (v_pos.avg_cost - p_fill_price) * v_cover_qty;
      v_cash       := v_cash - v_cover_qty * p_fill_price;
      v_short_coll := greatest(0, v_short_coll - v_cover_qty * v_pos.avg_cost);
      v_new_qty    := v_pos.qty + v_cover_qty;
      v_new_avg    := case when v_new_qty < 0 then v_pos.avg_cost else 0 end;
    end if;

  else
    raise exception 'unknown order side: %', p_side;
  end if;

  -- 3. Persist position + account.
  if abs(coalesce(v_new_qty, 0)) < 1e-9 then
    delete from paper_positions where user_id = p_user_id and ticker = upper(p_ticker);
  else
    insert into paper_positions (user_id, ticker, qty, avg_cost, last_price, last_price_at)
    values (p_user_id, upper(p_ticker), v_new_qty, v_new_avg, p_fill_price, now())
    on conflict (user_id, ticker) do update
    set qty = excluded.qty,
        avg_cost = excluded.avg_cost,
        last_price = excluded.last_price,
        last_price_at = excluded.last_price_at,
        updated_at = now();
  end if;

  update paper_accounts
     set cash = v_cash,
         realized_pnl = v_realized,
         short_collateral_reserved = v_short_coll,
         updated_at = now()
   where user_id = p_user_id;

  return jsonb_build_object('order_id', v_order_id, 'idempotent', false,
                            'status', p_status,
                            'cash_after', v_cash,
                            'realized_after', v_realized);
end; $$;

-- Service-role gets execute; regular authed users go through the application
-- layer (which calls this via the service-role REST client + impersonation
-- context). We keep the function callable by `authenticated` too so direct
-- supabase-js clients work in dev.
grant execute on function public.paper_place_order to authenticated, service_role;

-- ===========================================================================
-- Phase 2: Cognitive Trading Journal — schema additions
-- ===========================================================================

-- Journal entries: free-form notes attached to sessions / paper orders /
-- theses. `auto_draft` entries are created by the post-decision hook; the
-- user can edit + commit them. `kind='override_reason'` is used when the
-- user manually deviates from a paper recommendation.
create table if not exists public.journal_entries (
  id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  session_id text references public.sessions(id) on delete set null,
  paper_order_id text references public.paper_orders(id) on delete set null,
  thesis_id text references public.recipes(id) on delete set null,
  kind text not null check (kind in ('note','reflection','override_reason','auto_draft')),
  body text not null,
  sentiment_score integer,
  is_draft boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists journal_entries_user_idx on public.journal_entries (user_id, created_at desc);
create index if not exists journal_entries_session_idx on public.journal_entries (session_id);
alter table public.journal_entries enable row level security;
drop policy if exists "journal_entries: read own"   on public.journal_entries;
drop policy if exists "journal_entries: insert own" on public.journal_entries;
drop policy if exists "journal_entries: update own" on public.journal_entries;
drop policy if exists "journal_entries: delete own" on public.journal_entries;
create policy "journal_entries: read own"   on public.journal_entries for select using (auth.uid() = user_id);
create policy "journal_entries: insert own" on public.journal_entries for insert with check (auth.uid() = user_id);
create policy "journal_entries: update own" on public.journal_entries for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "journal_entries: delete own" on public.journal_entries for delete using (auth.uid() = user_id);

-- Calibration head: Platt-scaling params per user × regime, opt-in.
create table if not exists public.calibration_models (
  user_id uuid not null references auth.users(id) on delete cascade,
  regime text not null,
  a numeric(10,6) not null,
  b numeric(10,6) not null,
  n_samples integer not null,
  brier_before numeric(8,6),
  brier_after numeric(8,6),
  applied boolean not null default false,
  fitted_at timestamptz not null default now(),
  primary key (user_id, regime, fitted_at)
);
alter table public.calibration_models enable row level security;
drop policy if exists "calibration_models: read own" on public.calibration_models;
create policy "calibration_models: read own" on public.calibration_models for select using (auth.uid() = user_id);
drop policy if exists "calibration_models: update own" on public.calibration_models;
create policy "calibration_models: update own" on public.calibration_models for update using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- Disagreement log: per-fire similarity between Bull and Bear outputs.
create table if not exists public.disagreement_log (
  id bigserial primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  recipe_id text references public.recipes(id) on delete cascade,
  session_id text references public.sessions(id) on delete cascade,
  bull_model text,
  bear_model text,
  similarity numeric(5,4) not null,
  rating_agreement boolean,
  recorded_at timestamptz not null default now()
);
create index if not exists disagreement_log_recipe_idx on public.disagreement_log (recipe_id, recorded_at desc);
alter table public.disagreement_log enable row level security;
drop policy if exists "disagreement_log: read own" on public.disagreement_log;
create policy "disagreement_log: read own" on public.disagreement_log for select using (auth.uid() = user_id);

-- Behavioral findings: tilt / revenge / anchoring / overconfidence detections.
create table if not exists public.behavioral_findings (
  id bigserial primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  pattern text not null check (pattern in ('tilt','revenge','anchoring','overconfidence')),
  severity numeric(3,2) not null,
  evidence jsonb not null,
  acknowledged boolean not null default false,
  dismissed boolean not null default false,
  created_at timestamptz not null default now()
);
create index if not exists behavioral_findings_user_idx on public.behavioral_findings (user_id, created_at desc);
alter table public.behavioral_findings enable row level security;
drop policy if exists "behavioral_findings: read own" on public.behavioral_findings;
create policy "behavioral_findings: read own" on public.behavioral_findings for select using (auth.uid() = user_id);
drop policy if exists "behavioral_findings: update own" on public.behavioral_findings;
create policy "behavioral_findings: update own" on public.behavioral_findings for update using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- Prompt evaluation harness — weekly A/B against the user's own data.
create table if not exists public.prompt_evals (
  id bigserial primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  variant text not null,
  baseline_brier numeric(8,6) not null,
  variant_brier numeric(8,6) not null,
  n_samples integer not null,
  promoted boolean not null default false,
  evaluated_at timestamptz not null default now()
);
alter table public.prompt_evals enable row level security;
drop policy if exists "prompt_evals: read own" on public.prompt_evals;
create policy "prompt_evals: read own" on public.prompt_evals for select using (auth.uid() = user_id);

-- Memory embeddings — drop-in replacement for Jaccard retrieval. `vector` is
-- a pgvector column if the extension is installed; otherwise we store the raw
-- bytes in a bytea fallback column and treat the dim as advisory.
-- (Operators can run `create extension if not exists vector;` in their
-- project before applying this block if they want native vector indexing.)
create table if not exists public.memory_embeddings (
  entry_id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  source text not null check (source in ('journal','paper_order','memory_log')),
  model_id text not null,
  vector_bytes bytea,                                     -- fallback raw float32[] storage
  created_at timestamptz not null default now()
);
create index if not exists memory_embeddings_user_idx on public.memory_embeddings (user_id, source);
alter table public.memory_embeddings enable row level security;
drop policy if exists "memory_embeddings: read own" on public.memory_embeddings;
create policy "memory_embeddings: read own" on public.memory_embeddings for select using (auth.uid() = user_id);
drop policy if exists "memory_embeddings: insert own" on public.memory_embeddings;
create policy "memory_embeddings: insert own" on public.memory_embeddings for insert with check (auth.uid() = user_id);

-- Add auto-inject-classical flag to recipes (Phase 2.6/2.7).
alter table public.recipes
  add column if not exists auto_inject_classical boolean not null default false;

-- Behavioral cooldown opt-in (Phase 2.5). When true, a tilt/revenge finding
-- created in the last 60 min blocks the next paper order with a tilt_cooldown
-- risk_event. Detection always runs; only the *action* (blocking) is opt-in.
alter table public.risk_limits
  add column if not exists behavioral_cooldown boolean not null default false;

-- ============================================================================
-- Phase 3 — Streaming + multi-timeframe + backtest
-- ============================================================================

-- Backtest runs — one row per `agenticwhales backtest run ...` invocation.
create table if not exists public.backtest_runs (
  id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  recipe_id text references public.recipes(id) on delete set null,
  ticker text not null,
  from_date date not null,
  to_date date not null,
  starting_cash numeric(20,8) not null,
  final_nav numeric(20,8),
  total_decisions integer not null default 0,
  closed_trades integer not null default 0,
  hit_rate numeric(6,4),
  brier numeric(8,6),
  max_drawdown_pct numeric(6,4),
  equity_curve jsonb,
  status text not null check (status in ('queued','running','done','failed')),
  created_at timestamptz not null default now()
);
create index if not exists backtest_runs_user_idx on public.backtest_runs (user_id, created_at desc);
alter table public.backtest_runs enable row level security;
drop policy if exists "backtest_runs: read own" on public.backtest_runs;
create policy "backtest_runs: read own" on public.backtest_runs for select using (auth.uid() = user_id);
drop policy if exists "backtest_runs: insert own" on public.backtest_runs;
create policy "backtest_runs: insert own" on public.backtest_runs for insert with check (auth.uid() = user_id);

-- Per-decision rows inside a backtest run — used for chart drilldowns.
create table if not exists public.backtest_decisions (
  id bigserial primary key,
  run_id text not null references public.backtest_runs(id) on delete cascade,
  as_of_date date not null,
  ticker text not null,
  rating text,
  predicted_return_pct numeric(8,4),
  predicted_prob numeric(5,4),
  realized_return_pct numeric(8,4),
  hit boolean,
  reason text,
  recorded_at timestamptz not null default now()
);
create index if not exists backtest_decisions_run_idx on public.backtest_decisions (run_id, as_of_date);

-- Streaming events cache — dedup buffer for the WS feed. The streaming worker
-- writes here when an event triggers a fire so we can replay / audit later.
create table if not exists public.streaming_events (
  id bigserial primary key,
  source text not null,
  symbol text not null,
  kind text not null check (kind in ('quote','trade','bar','news')),
  payload jsonb,
  received_at timestamptz not null default now()
);
create index if not exists streaming_events_recent_idx on public.streaming_events (symbol, received_at desc);

-- Per-recipe Phase 3 columns. `timeframes` enumerates the multi-TF set the
-- DAG fan-out runs; default `{1d}` preserves Phase 1/2 behavior. The streaming
-- rate-limit is per-recipe, default 6 fires/hr.
alter table public.recipes
  add column if not exists timeframes text[] not null default array['1d']::text[],
  add column if not exists streaming_max_fires_per_hour integer not null default 6;

-- Conviction decay tuning — half-life knob per row so a long-horizon thesis
-- can be configured to decay slower than a day-trade thesis. Default 5 days.
alter table public.conviction_scores
  add column if not exists decay_half_life_days integer not null default 5;

-- ===========================================================================
-- PR-2: Compliance attestation (Sundar review #2)
-- ===========================================================================
-- Replaces the prior client-side modal pattern with a server-enforced gate.
-- Every new session and every paper order must reference a non-revoked
-- attestation row owned by the same user. The disclaimer text is versioned
-- so we can ship updates without invalidating prior attestations until the
-- next user action.

create table if not exists public.compliance_attestations (
  id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  -- Semantic version of the disclaimer text the user agreed to. Bumped
  -- whenever the disclaimer is materially revised. Old rows stay valid
  -- for their respective versions; a new session will require a fresh
  -- attestation when the active version no longer matches.
  version text not null,
  -- Which clauses the user explicitly acknowledged. Three booleans rather
  -- than a single "agreed" flag so we can audit *what* they agreed to.
  ack_paper_only boolean not null,
  ack_not_advice boolean not null,
  ack_jurisdiction boolean not null,
  -- The literal disclaimer text at the time of the attestation. Stored
  -- verbatim so we never have to reconstruct what they actually saw.
  disclaimer_text text not null,
  -- Optional jurisdiction code (ISO 3166-1 alpha-2). Blank when the user
  -- declines to provide one; the server-side blocklist still applies.
  jurisdiction text,
  created_at timestamptz not null default now(),
  -- Revoked rows can no longer be used as the FK target for new sessions.
  -- The history is preserved (we never delete).
  revoked_at timestamptz
);
create index if not exists compliance_attestations_user_idx
  on public.compliance_attestations (user_id, created_at desc);

alter table public.compliance_attestations enable row level security;
drop policy if exists "compliance: read own" on public.compliance_attestations;
create policy "compliance: read own"
  on public.compliance_attestations for select
  using (auth.uid() = user_id);
drop policy if exists "compliance: insert own" on public.compliance_attestations;
create policy "compliance: insert own"
  on public.compliance_attestations for insert
  with check (auth.uid() = user_id);
-- Updates only allowed to set revoked_at (clients can mark their own
-- attestation revoked). Service-role bypasses RLS so the server can revoke
-- on disclaimer-version bumps.
drop policy if exists "compliance: update own" on public.compliance_attestations;
create policy "compliance: update own"
  on public.compliance_attestations for update
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- Wire the FK onto sessions and paper_orders. Both are nullable until the
-- backfill cron has run; once it has, application code refuses to create
-- new rows without a non-null attestation_id.
alter table public.sessions
  add column if not exists compliance_attestation_id text
    references public.compliance_attestations(id) on delete set null;
create index if not exists sessions_compliance_idx
  on public.sessions (compliance_attestation_id);

alter table public.paper_orders
  add column if not exists compliance_attestation_id text
    references public.compliance_attestations(id) on delete set null;
create index if not exists paper_orders_compliance_idx
  on public.paper_orders (compliance_attestation_id);

-- The active disclaimer version the application enforces. Single-row
-- table queried at session-create time. Updating this row triggers the
-- next session-create request to require a fresh attestation.
create table if not exists public.compliance_active_version (
  -- Singleton row pinned by id=1.
  id integer primary key check (id = 1),
  version text not null,
  effective_at timestamptz not null default now()
);
insert into public.compliance_active_version (id, version)
  values (1, 'v1.0')
  on conflict (id) do nothing;

-- Helper: returns true if the user has a non-revoked attestation matching
-- the currently-active version. Used by application code so the version
-- pin is the single source of truth.
create or replace function public.has_active_attestation(p_user uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1
    from public.compliance_attestations ca,
         public.compliance_active_version cav
    where ca.user_id = p_user
      and ca.version = cav.version
      and ca.revoked_at is null
      and ca.ack_paper_only
      and ca.ack_not_advice
      and ca.ack_jurisdiction
  );
$$;
grant execute on function public.has_active_attestation(uuid) to authenticated;
