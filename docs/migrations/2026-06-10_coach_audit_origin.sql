-- ============================================================================
-- AgenticWhales — coach_audits.origin: who initiated the audit.
-- Idempotent: safe to run more than once. Paste into the Supabase SQL editor.
--
-- Why: the nightly SnapTrade cron writes audits autonomously. Retention
-- metrics must distinguish user-initiated audits ('upload', 'sync_manual')
-- from the scheduler's ('sync_auto'), or D30 retention measures cron uptime
-- instead of users coming back.
-- ============================================================================

alter table public.coach_audits
  add column if not exists origin text not null default 'upload';

create index if not exists coach_audits_origin_idx
  on public.coach_audits (user_id, origin, created_at desc);
