-- Lightweight, privacy-safe product analytics for EssayPilot.
-- Apply after the existing schema and 20260812 feedback-loop migration.

create table if not exists public.analytics_events (
  event_id uuid primary key default gen_random_uuid(),
  user_id text not null check (
    user_id ~ '^[0-9a-f-]{36}$' or user_id ~ '^anon_[a-f0-9]{64}$'
  ),
  session_id uuid not null,
  run_id uuid references public.grading_runs(id) on delete set null,
  event_name text not null check (event_name in (
    'session_started', 'first_draft_submitted', 'report_generated',
    'report_generation_failed', 'report_viewed', 'tutorial_clicked',
    'problem_map_viewed', 'training_started', 'sentence_training_started',
    'sentence_training_completed', 'mistake_saved', 'archive_viewed',
    'second_draft_submitted', 'diff_viewed', 'dictionary_opened'
  )),
  occurred_at timestamptz not null default now(),
  metadata_json jsonb not null default '{}'::jsonb check (
    jsonb_typeof(metadata_json) = 'object' and pg_column_size(metadata_json) <= 2048
  ),
  dedupe_key text not null unique check (dedupe_key ~ '^[a-f0-9]{64}$')
);

create index if not exists analytics_events_occurred_at_idx
  on public.analytics_events(occurred_at);
create index if not exists analytics_events_name_occurred_idx
  on public.analytics_events(event_name, occurred_at);
create index if not exists analytics_events_user_occurred_idx
  on public.analytics_events(user_id, occurred_at);
create index if not exists analytics_events_session_idx
  on public.analytics_events(session_id);
create index if not exists analytics_events_run_idx
  on public.analytics_events(run_id) where run_id is not null;

alter table public.analytics_events enable row level security;
revoke all on public.analytics_events from public, anon, authenticated;

create or replace function public.record_analytics_event(
  p_event_id uuid,
  p_session_id uuid,
  p_run_id uuid,
  p_event_name text,
  p_metadata_json jsonb,
  p_dedupe_key text,
  p_anonymous_user_id text default null
) returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_authenticated_user uuid := auth.uid();
  v_user_id text;
  v_inserted integer := 0;
begin
  if p_event_id is null or p_session_id is null then
    raise exception 'Invalid analytics identifiers';
  end if;
  if p_event_name not in (
    'session_started', 'first_draft_submitted', 'report_generated',
    'report_generation_failed', 'report_viewed', 'tutorial_clicked',
    'problem_map_viewed', 'training_started', 'sentence_training_started',
    'sentence_training_completed', 'mistake_saved', 'archive_viewed',
    'second_draft_submitted', 'diff_viewed', 'dictionary_opened'
  ) then
    raise exception 'Invalid analytics event';
  end if;
  if p_dedupe_key !~ '^[a-f0-9]{64}$' then
    raise exception 'Invalid analytics dedupe key';
  end if;
  if p_metadata_json is null
     or jsonb_typeof(p_metadata_json) <> 'object'
     or pg_column_size(p_metadata_json) > 2048
     or (p_metadata_json - array[
       'cached', 'draft_number', 'entry_mode', 'failure_type',
       'item_index', 'source', 'task_kind'
     ]) <> '{}'::jsonb then
    raise exception 'Invalid analytics metadata';
  end if;

  if v_authenticated_user is not null then
    v_user_id := v_authenticated_user::text;
    if p_anonymous_user_id ~ '^anon_[a-f0-9]{64}$' then
      update public.analytics_events
      set user_id = v_user_id
      where user_id = p_anonymous_user_id;
    end if;
  elsif p_anonymous_user_id ~ '^anon_[a-f0-9]{64}$' then
    v_user_id := p_anonymous_user_id;
  else
    raise exception 'Anonymous analytics id required';
  end if;

  if p_run_id is not null and (
    v_authenticated_user is null or not exists (
      select 1 from public.grading_runs
      where id = p_run_id and user_id = v_authenticated_user
    )
  ) then
    p_run_id := null;
  end if;

  insert into public.analytics_events(
    event_id, user_id, session_id, run_id, event_name, metadata_json, dedupe_key
  ) values (
    p_event_id, v_user_id, p_session_id, p_run_id, p_event_name,
    p_metadata_json, p_dedupe_key
  ) on conflict (dedupe_key) do nothing;
  get diagnostics v_inserted = row_count;
  return v_inserted = 1;
end;
$$;

revoke all on function public.record_analytics_event(uuid,uuid,uuid,text,jsonb,text,text)
  from public;
grant execute on function public.record_analytics_event(uuid,uuid,uuid,text,jsonb,text,text)
  to anon, authenticated;

create or replace function public.get_analytics_dashboard(p_since timestamptz default null)
returns jsonb
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  with
  range_events as materialized (
    select * from public.analytics_events
    where p_since is null or occurred_at >= p_since
  ),
  first_seen as materialized (
    select user_id, min(occurred_at) as first_seen_at
    from public.analytics_events
    group by user_id
  ),
  event_usage as (
    select event_name, count(*) as event_count, count(distinct user_id) as user_count
    from range_events
    group by event_name
  ),
  draft_users as (
    select user_id, min(occurred_at) as first_draft_at
    from range_events
    where event_name = 'first_draft_submitted'
    group by user_id
  ),
  funnel_users as (
    select
      d.user_id,
      d.first_draft_at,
      report.report_at,
      training.training_at,
      completed.completed_at,
      second_draft.second_draft_at
    from draft_users d
    left join lateral (
      select min(e.occurred_at) as report_at
      from public.analytics_events e
      where e.user_id = d.user_id and e.event_name = 'report_viewed'
        and e.occurred_at >= d.first_draft_at
    ) report on true
    left join lateral (
      select min(e.occurred_at) as training_at
      from public.analytics_events e
      where e.user_id = d.user_id
        and e.event_name in ('training_started', 'sentence_training_started')
        and report.report_at is not null and e.occurred_at >= report.report_at
    ) training on true
    left join lateral (
      select min(e.occurred_at) as completed_at
      from public.analytics_events e
      where e.user_id = d.user_id and e.event_name = 'sentence_training_completed'
        and training.training_at is not null and e.occurred_at >= training.training_at
    ) completed on true
    left join lateral (
      select min(e.occurred_at) as second_draft_at
      from public.analytics_events e
      where e.user_id = d.user_id and e.event_name = 'second_draft_submitted'
        and training.training_at is not null and e.occurred_at >= training.training_at
    ) second_draft on true
  ),
  daily as (
    select
      timezone('Asia/Shanghai', occurred_at)::date as day,
      count(distinct user_id) as active_users,
      count(*) filter (where event_name = 'report_generated') as gradings
    from range_events
    group by timezone('Asia/Shanghai', occurred_at)::date
  ),
  cohorts as (
    select user_id, timezone('Asia/Shanghai', first_seen_at)::date as cohort_day
    from first_seen
    where p_since is null or first_seen_at >= p_since
  ),
  active_days as materialized (
    select distinct user_id, timezone('Asia/Shanghai', occurred_at)::date as active_day
    from public.analytics_events
  ),
  historical_runs as materialized (
    select g.* from public.grading_runs g
    where p_since is null or g.created_at >= p_since
  ),
  historical_first_seen as (
    select user_id, min(created_at) as first_grading_at
    from public.grading_runs group by user_id
  )
  select jsonb_build_object(
    'since', p_since,
    'generated_at', now(),
    'tracking_enabled_at', (select min(occurred_at) from public.analytics_events),
    'summary', jsonb_build_object(
      'unique_users', (select count(distinct user_id) from range_events),
      'new_users', (select count(*) from first_seen where p_since is null or first_seen_at >= p_since),
      'sessions', (select count(distinct session_id) from range_events),
      'first_drafts', (select count(*) from range_events where event_name = 'first_draft_submitted'),
      'successful_reports', (select count(*) from range_events where event_name = 'report_generated'),
      'failed_reports', (select count(*) from range_events where event_name = 'report_generation_failed')
    ),
    'event_usage', coalesce((
      select jsonb_agg(jsonb_build_object(
        'event_name', event_name, 'event_count', event_count, 'user_count', user_count
      ) order by event_name) from event_usage
    ), '[]'::jsonb),
    'funnel', jsonb_build_object(
      'first_draft_submitted', (select count(*) from funnel_users),
      'report_viewed', (select count(*) from funnel_users where report_at is not null),
      'training_started', (select count(*) from funnel_users where training_at is not null),
      'sentence_training_completed', (select count(*) from funnel_users where completed_at is not null),
      'second_draft_submitted', (select count(*) from funnel_users where second_draft_at is not null)
    ),
    'daily', coalesce((
      select jsonb_agg(to_jsonb(daily) order by day) from daily
    ), '[]'::jsonb),
    'retention', jsonb_build_object(
      'day_1', jsonb_build_object(
        'eligible_users', (select count(*) from cohorts where cohort_day <= timezone('Asia/Shanghai', now())::date - 1),
        'retained_users', (
          select count(*) from cohorts c
          where c.cohort_day <= timezone('Asia/Shanghai', now())::date - 1 and exists (
            select 1 from active_days a
            where a.user_id = c.user_id and a.active_day = c.cohort_day + 1
          )
        )
      ),
      'day_7', jsonb_build_object(
        'eligible_users', (select count(*) from cohorts where cohort_day <= timezone('Asia/Shanghai', now())::date - 7),
        'retained_users', (
          select count(*) from cohorts c
          where c.cohort_day <= timezone('Asia/Shanghai', now())::date - 7 and exists (
            select 1 from active_days a
            where a.user_id = c.user_id and a.active_day = c.cohort_day + 7
          )
        )
      )
    ),
    'historical', jsonb_build_object(
      'unique_users', (select count(distinct user_id) from historical_runs),
      'new_users', (
        select count(*) from historical_first_seen
        where p_since is null or first_grading_at >= p_since
      ),
      'grading_runs', (select count(*) from historical_runs),
      'first_drafts', (
        select count(*) from historical_runs
        where coalesce(draft_role, 'ordinary') <> 'second' and parent_run_id is null
      ),
      'successful_reports', (select count(*) from historical_runs),
      'training_started_users', (
        select count(distinct p.user_id) from public.practice_attempts p
        where p_since is null or p.created_at >= p_since
      ),
      'training_completed_users', (
        select count(distinct p.user_id) from public.practice_attempts p
        where p.status = 'mastered' and (p_since is null or p.updated_at >= p_since)
      ),
      'second_draft_users', (
        select count(distinct d.user_id) from public.draft_revisions d
        where p_since is null or d.created_at >= p_since
      ),
      'second_drafts', (
        select count(*) from public.draft_revisions d
        where p_since is null or d.created_at >= p_since
      )
    )
  );
$$;

revoke all on function public.get_analytics_dashboard(timestamptz)
  from public, anon, authenticated;
grant execute on function public.get_analytics_dashboard(timestamptz)
  to service_role;

-- Reliably map the old minimal lifecycle events if that migration was installed.
do $$
begin
  if to_regclass('public.product_events') is not null then
    insert into public.analytics_events(
      user_id, session_id, event_name, occurred_at, metadata_json, dedupe_key
    )
    select
      coalesce(user_id::text, 'anon_' || visitor_hash),
      flow_id,
      case event_name
        when 'visitor_opened' then 'session_started'
        when 'grading_started' then 'first_draft_submitted'
        when 'grading_completed' then 'report_generated'
        when 'report_viewed' then 'report_viewed'
        when 'report_training_clicked' then 'training_started'
        when 'second_draft_completed' then 'second_draft_submitted'
      end,
      occurred_at,
      '{"source":"legacy_product_events"}'::jsonb,
      encode(digest('legacy-product-event:' || id::text, 'sha256'), 'hex')
    from public.product_events
    where event_name in (
      'visitor_opened', 'grading_started', 'grading_completed',
      'report_viewed', 'report_training_clicked', 'second_draft_completed'
    )
    on conflict (dedupe_key) do nothing;
  end if;
end;
$$;
