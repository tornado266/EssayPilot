-- EssayPilot decision analytics V2.
-- Additive and rollback-safe: legacy tables and RPCs remain available.

create extension if not exists pgcrypto;

create table if not exists public.analytics_events (
  event_id uuid primary key default gen_random_uuid(),
  user_id text not null check (
    user_id ~ '^[0-9a-f-]{36}$' or user_id ~ '^anon_[a-f0-9]{64}$'
  ),
  session_id uuid not null,
  attempt_id uuid,
  run_id uuid references public.grading_runs(id) on delete set null,
  event_name text not null,
  occurred_at timestamptz not null default now(),
  metadata_json jsonb not null default '{}'::jsonb check (
    jsonb_typeof(metadata_json) = 'object' and pg_column_size(metadata_json) <= 2048
  ),
  dedupe_key text not null unique check (dedupe_key ~ '^[a-f0-9]{64}$')
);

alter table public.analytics_events add column if not exists attempt_id uuid;
alter table public.analytics_events drop constraint if exists analytics_events_event_name_check;
alter table public.analytics_events add constraint analytics_events_event_name_check
check (event_name in (
  'session_started', 'login_completed', 'first_draft_submitted',
  'report_generated', 'report_generation_failed', 'report_viewed',
  'tutorial_clicked', 'problem_map_viewed', 'training_started',
  'sentence_training_started', 'sentence_training_completed', 'logic_training_completed', 'mistake_saved',
  'archive_viewed', 'second_draft_submitted', 'second_draft_generated',
  'second_draft_generation_failed', 'diff_viewed', 'dictionary_opened'
));

create index if not exists analytics_events_attempt_idx
  on public.analytics_events(attempt_id) where attempt_id is not null;
create index if not exists analytics_events_run_occurred_idx
  on public.analytics_events(run_id, occurred_at) where run_id is not null;
create index if not exists analytics_events_occurred_at_idx
  on public.analytics_events(occurred_at);
create index if not exists analytics_events_name_occurred_idx
  on public.analytics_events(event_name, occurred_at);
create index if not exists analytics_events_user_occurred_idx
  on public.analytics_events(user_id, occurred_at);

alter table public.analytics_events enable row level security;
revoke all on public.analytics_events from public, anon, authenticated;

create table if not exists public.product_feedback (
  feedback_id uuid primary key default gen_random_uuid(),
  user_id text not null check (
    user_id ~ '^[0-9a-f-]{36}$' or user_id ~ '^anon_[a-f0-9]{64}$'
  ),
  session_id uuid not null,
  attempt_id uuid,
  run_id uuid references public.grading_runs(id) on delete set null,
  touchpoint text not null check (touchpoint in ('report', 'training', 'second_draft')),
  helpful boolean not null,
  reason_codes text[] not null default '{}',
  occurred_at timestamptz not null default now(),
  dedupe_key text not null unique check (dedupe_key ~ '^[a-f0-9]{64}$'),
  check (reason_codes <@ array[
    'inaccurate', 'too_generic', 'unclear', 'not_actionable', 'too_slow',
    'too_long', 'difficulty_mismatch', 'progress_unclear', 'other'
  ]::text[]),
  check (
    (helpful and cardinality(reason_codes) = 0)
    or (not helpful and cardinality(reason_codes) between 1 and 3)
  )
);

create index if not exists product_feedback_occurred_idx
  on public.product_feedback(occurred_at);
create index if not exists product_feedback_touchpoint_occurred_idx
  on public.product_feedback(touchpoint, occurred_at);
create index if not exists product_feedback_run_idx
  on public.product_feedback(run_id) where run_id is not null;

alter table public.product_feedback enable row level security;
revoke all on public.product_feedback from public, anon, authenticated;

create or replace function public.record_analytics_event_v2(
  p_event_id uuid,
  p_session_id uuid,
  p_attempt_id uuid,
  p_run_id uuid,
  p_event_name text,
  p_metadata_json jsonb,
  p_dedupe_key text,
  p_anonymous_user_id text default null
) returns boolean
language plpgsql
security definer
set search_path = ''
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
    'session_started', 'login_completed', 'first_draft_submitted',
    'report_generated', 'report_generation_failed', 'report_viewed',
    'tutorial_clicked', 'problem_map_viewed', 'training_started',
    'sentence_training_started', 'sentence_training_completed', 'logic_training_completed', 'mistake_saved',
    'archive_viewed', 'second_draft_submitted', 'second_draft_generated',
    'second_draft_generation_failed', 'diff_viewed', 'dictionary_opened'
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
       'cached', 'draft_number', 'duration_ms', 'entry_mode', 'failure_type',
       'identity_type', 'item_index', 'source', 'task_kind'
     ]) <> '{}'::jsonb then
    raise exception 'Invalid analytics metadata';
  end if;
  if p_metadata_json ? 'duration_ms' and (
    jsonb_typeof(p_metadata_json -> 'duration_ms') <> 'number'
    or (p_metadata_json ->> 'duration_ms')::numeric < 0
    or (p_metadata_json ->> 'duration_ms')::numeric > 3600000
  ) then
    raise exception 'Invalid analytics duration';
  end if;
  if p_metadata_json ? 'identity_type'
     and p_metadata_json ->> 'identity_type' not in ('anonymous', 'authenticated') then
    raise exception 'Invalid analytics identity type';
  end if;
  if p_event_name in (
    'first_draft_submitted', 'report_generated', 'report_generation_failed',
    'second_draft_submitted', 'second_draft_generated', 'second_draft_generation_failed'
  ) and p_attempt_id is null then
    raise exception 'Attempt id required';
  end if;

  if v_authenticated_user is not null then
    v_user_id := v_authenticated_user::text;
    if p_anonymous_user_id ~ '^anon_[a-f0-9]{64}$' then
      update public.analytics_events
      set user_id = v_user_id
      where user_id = p_anonymous_user_id;
      update public.product_feedback
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
    event_id, user_id, session_id, attempt_id, run_id,
    event_name, metadata_json, dedupe_key
  ) values (
    p_event_id, v_user_id, p_session_id, p_attempt_id, p_run_id,
    p_event_name, p_metadata_json, p_dedupe_key
  ) on conflict (dedupe_key) do nothing;
  get diagnostics v_inserted = row_count;
  return v_inserted = 1;
end;
$$;

revoke all on function public.record_analytics_event_v2(
  uuid,uuid,uuid,uuid,text,jsonb,text,text
) from public;
grant execute on function public.record_analytics_event_v2(
  uuid,uuid,uuid,uuid,text,jsonb,text,text
) to anon, authenticated;

create or replace function public.record_product_feedback(
  p_feedback_id uuid,
  p_session_id uuid,
  p_attempt_id uuid,
  p_run_id uuid,
  p_touchpoint text,
  p_helpful boolean,
  p_reason_codes text[],
  p_dedupe_key text,
  p_anonymous_user_id text default null
) returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_authenticated_user uuid := auth.uid();
  v_user_id text;
  v_inserted integer := 0;
begin
  if p_feedback_id is null or p_session_id is null then
    raise exception 'Invalid feedback identifiers';
  end if;
  if p_touchpoint not in ('report', 'training', 'second_draft') then
    raise exception 'Invalid feedback touchpoint';
  end if;
  if p_dedupe_key !~ '^[a-f0-9]{64}$' then
    raise exception 'Invalid feedback dedupe key';
  end if;
  p_reason_codes := coalesce(p_reason_codes, '{}'::text[]);
  if not (p_reason_codes <@ array[
    'inaccurate', 'too_generic', 'unclear', 'not_actionable', 'too_slow',
    'too_long', 'difficulty_mismatch', 'progress_unclear', 'other'
  ]::text[]) or cardinality(p_reason_codes) > 3
     or cardinality(p_reason_codes) <> (
       select count(distinct reason_code) from unnest(p_reason_codes) reason_code
     ) then
    raise exception 'Invalid feedback reasons';
  end if;
  if (p_helpful and cardinality(p_reason_codes) <> 0)
     or (not p_helpful and cardinality(p_reason_codes) not between 1 and 3) then
    raise exception 'Feedback reasons do not match helpfulness';
  end if;

  if v_authenticated_user is not null then
    v_user_id := v_authenticated_user::text;
    if p_anonymous_user_id ~ '^anon_[a-f0-9]{64}$' then
      update public.analytics_events
      set user_id = v_user_id
      where user_id = p_anonymous_user_id;
      update public.product_feedback
      set user_id = v_user_id
      where user_id = p_anonymous_user_id;
    end if;
  elsif p_anonymous_user_id ~ '^anon_[a-f0-9]{64}$' then
    v_user_id := p_anonymous_user_id;
  else
    raise exception 'Anonymous feedback id required';
  end if;

  if p_run_id is not null and (
    v_authenticated_user is null or not exists (
      select 1 from public.grading_runs
      where id = p_run_id and user_id = v_authenticated_user
    )
  ) then
    p_run_id := null;
  end if;

  insert into public.product_feedback(
    feedback_id, user_id, session_id, attempt_id, run_id,
    touchpoint, helpful, reason_codes, dedupe_key
  ) values (
    p_feedback_id, v_user_id, p_session_id, p_attempt_id, p_run_id,
    p_touchpoint, p_helpful, p_reason_codes, p_dedupe_key
  ) on conflict (dedupe_key) do nothing;
  get diagnostics v_inserted = row_count;
  return v_inserted = 1;
end;
$$;

revoke all on function public.record_product_feedback(
  uuid,uuid,uuid,uuid,text,boolean,text[],text,text
) from public;
grant execute on function public.record_product_feedback(
  uuid,uuid,uuid,uuid,text,boolean,text[],text,text
) to anon, authenticated;

create or replace function public.get_analytics_dashboard_v2(
  p_since timestamptz default null,
  p_until timestamptz default null
) returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_until timestamptz := least(coalesce(p_until, now()), now());
  v_result jsonb;
begin
if p_since is not null and p_since >= v_until then
  raise exception 'Analytics range must have since before until';
end if;

with
bounds as (
  select
    p_since as since_at,
    v_until as until_at,
    case when p_since is null then null
         else p_since - (v_until - p_since)
    end as previous_since_at
),
current_events as materialized (
  select e.*
  from public.analytics_events e cross join bounds b
  where (b.since_at is null or e.occurred_at >= b.since_at)
    and e.occurred_at < b.until_at
),
previous_events as materialized (
  select e.*
  from public.analytics_events e cross join bounds b
  where b.since_at is not null
    and e.occurred_at >= b.previous_since_at
    and e.occurred_at < b.since_at
),
first_seen as materialized (
  select user_id, min(occurred_at) as first_seen_at
  from public.analytics_events
  group by user_id
),
current_summary as (
  select jsonb_build_object(
    'active_users', count(distinct user_id),
    'authenticated_active_users', count(distinct user_id) filter (where user_id !~ '^anon_'),
    'anonymous_active_visitors', count(distinct user_id) filter (where user_id ~ '^anon_'),
    'new_tracked_users', (
      select count(*) from first_seen f cross join bounds b
      where (b.since_at is null or f.first_seen_at >= b.since_at)
        and f.first_seen_at < b.until_at
    ),
    'sessions', count(distinct session_id),
    'registered_users_total', (
      select count(*) from auth.users u cross join bounds b where u.created_at < b.until_at
    ),
    'registered_users_new', (
      select count(*) from auth.users u cross join bounds b
      where (b.since_at is null or u.created_at >= b.since_at) and u.created_at < b.until_at
    ),
    'first_draft_attempts', count(distinct attempt_id) filter (where event_name = 'first_draft_submitted'),
    'report_successes', count(distinct attempt_id) filter (where event_name = 'report_generated'),
    'report_failures', count(distinct attempt_id) filter (where event_name = 'report_generation_failed'),
    'second_draft_attempts', count(distinct attempt_id) filter (where event_name = 'second_draft_submitted'),
    'second_draft_successes', count(distinct attempt_id) filter (where event_name = 'second_draft_generated'),
    'second_draft_failures', count(distinct attempt_id) filter (where event_name = 'second_draft_generation_failed')
  ) as value
  from current_events
),
previous_summary as (
  select jsonb_build_object(
    'active_users', count(distinct user_id),
    'authenticated_active_users', count(distinct user_id) filter (where user_id !~ '^anon_'),
    'anonymous_active_visitors', count(distinct user_id) filter (where user_id ~ '^anon_'),
    'sessions', count(distinct session_id),
    'registered_users_total', (
      select count(*) from auth.users u cross join bounds b
      where b.since_at is not null and u.created_at < b.since_at
    ),
    'registered_users_new', (
      select count(*) from auth.users u cross join bounds b
      where b.since_at is not null and u.created_at >= b.previous_since_at and u.created_at < b.since_at
    ),
    'first_draft_attempts', count(distinct attempt_id) filter (where event_name = 'first_draft_submitted'),
    'report_successes', count(distinct attempt_id) filter (where event_name = 'report_generated'),
    'report_failures', count(distinct attempt_id) filter (where event_name = 'report_generation_failed'),
    'second_draft_attempts', count(distinct attempt_id) filter (where event_name = 'second_draft_submitted'),
    'second_draft_successes', count(distinct attempt_id) filter (where event_name = 'second_draft_generated'),
    'second_draft_failures', count(distinct attempt_id) filter (where event_name = 'second_draft_generation_failed')
  ) as value
  from previous_events
),
experience_visits as materialized (
  select user_id, session_id, min(occurred_at) as visited_at
  from current_events
  where event_name = 'session_started'
  group by user_id, session_id
),
experience_paths as (
  select
    v.user_id,
    v.session_id,
    v.visited_at,
    submitted.attempt_id,
    submitted.submitted_at,
    generated.generated_at,
    viewed.viewed_at,
    viewed.identity_type as view_identity_type,
    logged_in.login_at
  from experience_visits v
  left join lateral (
    select e.attempt_id, min(e.occurred_at) as submitted_at
    from current_events e
    where e.user_id = v.user_id
      and e.session_id = v.session_id
      and e.event_name = 'first_draft_submitted'
      and e.attempt_id is not null
      and e.occurred_at >= v.visited_at
    group by e.attempt_id
    order by submitted_at, e.attempt_id
    limit 1
  ) submitted on true
  left join lateral (
    select min(e.occurred_at) as generated_at
    from current_events e
    where e.user_id = v.user_id
      and e.session_id = v.session_id
      and e.attempt_id = submitted.attempt_id
      and e.event_name = 'report_generated'
      and e.occurred_at >= submitted.submitted_at
  ) generated on true
  left join lateral (
    select e.occurred_at as viewed_at,
           e.metadata_json ->> 'identity_type' as identity_type
    from current_events e
    where e.user_id = v.user_id
      and e.session_id = v.session_id
      and e.attempt_id = submitted.attempt_id
      and e.event_name = 'report_viewed'
      and e.occurred_at >= generated.generated_at
    order by e.occurred_at
    limit 1
  ) viewed on true
  left join lateral (
    select min(e.occurred_at) as login_at
    from current_events e
    where e.user_id = v.user_id
      and e.session_id = v.session_id
      and e.attempt_id = submitted.attempt_id
      and e.event_name = 'login_completed'
      and e.occurred_at >= viewed.viewed_at
  ) logged_in on true
),
experience_counts as (
  select
    count(distinct user_id) as visited,
    count(distinct user_id) filter (where submitted_at is not null) as submitted,
    count(distinct user_id) filter (where generated_at is not null) as generated,
    count(distinct user_id) filter (where viewed_at is not null) as viewed
  from experience_paths
),
guest_report_login_counts as (
  select
    count(distinct user_id) filter (
      where viewed_at is not null and view_identity_type = 'anonymous'
    ) as eligible_users,
    count(distinct user_id) filter (
      where viewed_at is not null and view_identity_type = 'anonymous' and login_at is not null
    ) as converted_users
  from experience_paths
),
learning_reports as materialized (
  select user_id, run_id, min(occurred_at) as report_at
  from current_events
  where run_id is not null and event_name = 'report_viewed'
  group by user_id, run_id
),
learning_paths as (
  select
    r.user_id,
    r.run_id,
    r.report_at,
    training.training_at,
    completed.completed_at,
    second_draft.second_draft_at,
    diff.diff_at
  from learning_reports r
  left join lateral (
    select min(e.occurred_at) as training_at
    from current_events e
    where e.user_id = r.user_id
      and e.run_id = r.run_id
      and e.event_name in ('training_started', 'sentence_training_started')
      and e.occurred_at >= r.report_at
  ) training on true
  left join lateral (
    select min(e.occurred_at) as completed_at
    from current_events e
    where e.user_id = r.user_id
      and e.run_id = r.run_id
      and e.event_name in ('sentence_training_completed', 'logic_training_completed')
      and e.occurred_at >= training.training_at
  ) completed on true
  left join lateral (
    select min(e.occurred_at) as second_draft_at
    from current_events e
    where e.user_id = r.user_id
      and e.run_id = r.run_id
      and e.event_name = 'second_draft_generated'
      and e.occurred_at >= completed.completed_at
  ) second_draft on true
  left join lateral (
    select min(e.occurred_at) as diff_at
    from current_events e
    where e.user_id = r.user_id
      and e.run_id = r.run_id
      and e.event_name = 'diff_viewed'
      and e.occurred_at >= second_draft.second_draft_at
  ) diff on true
),
learning_counts as (
  select
    count(distinct user_id) as report_viewed,
    count(distinct user_id) filter (where training_at is not null) as training_started,
    count(distinct user_id) filter (where completed_at is not null) as training_completed,
    count(distinct user_id) filter (where second_draft_at is not null) as second_draft_generated,
    count(distinct user_id) filter (where diff_at is not null) as diff_viewed
  from learning_paths
),
report_failure_types as (
  select coalesce(nullif(metadata_json ->> 'failure_type', ''), 'unknown') as failure_type,
         count(distinct attempt_id) as count
  from current_events
  where event_name = 'report_generation_failed' and attempt_id is not null
  group by 1
  having count(distinct attempt_id) >= 5
  order by count desc, failure_type
),
second_failure_types as (
  select coalesce(nullif(metadata_json ->> 'failure_type', ''), 'unknown') as failure_type,
         count(distinct attempt_id) as count
  from current_events
  where event_name = 'second_draft_generation_failed' and attempt_id is not null
  group by 1
  having count(distinct attempt_id) >= 5
  order by count desc, failure_type
),
report_durations as (
  select (metadata_json ->> 'duration_ms')::numeric as duration_ms
  from current_events
  where event_name in ('report_generated', 'report_generation_failed')
    and attempt_id is not null
    and jsonb_typeof(metadata_json -> 'duration_ms') = 'number'
),
second_durations as (
  select (metadata_json ->> 'duration_ms')::numeric as duration_ms
  from current_events
  where event_name in ('second_draft_generated', 'second_draft_generation_failed')
    and attempt_id is not null
    and jsonb_typeof(metadata_json -> 'duration_ms') = 'number'
),
draft_deltas as materialized (
  select distinct on (d.user_id)
    d.user_id,
    (d.score_snapshot ->> 'Overall Band')::numeric - g.overall_band as band_delta
  from public.draft_revisions d
  join public.grading_runs g on g.id = d.grading_run_id
  cross join bounds b
  where (b.since_at is null or d.created_at >= b.since_at)
    and d.created_at < b.until_at
    and jsonb_typeof(d.score_snapshot -> 'Overall Band') = 'number'
  order by d.user_id, d.created_at desc, d.id desc
),
current_feedback as materialized (
  select f.*
  from public.product_feedback f cross join bounds b
  where (b.since_at is null or f.occurred_at >= b.since_at)
    and f.occurred_at < b.until_at
),
feedback_reasons as (
  select f.touchpoint, reason_code, count(*) as count
  from current_feedback f cross join lateral unnest(f.reason_codes) reason_code
  group by f.touchpoint, reason_code
  having count(*) >= 5
),
feedback_touchpoints(touchpoint, denominator) as (
  values
    ('report'::text, (
      select count(distinct coalesce(attempt_id::text, run_id::text, user_id || ':' || session_id::text))
      from current_events where event_name = 'report_viewed'
    )),
    ('training'::text, (
      select count(distinct coalesce(run_id::text, attempt_id::text, user_id || ':' || session_id::text))
      from current_events where event_name in ('sentence_training_completed', 'logic_training_completed')
    )),
    ('second_draft'::text, (
      select count(distinct coalesce(attempt_id::text, run_id::text, user_id || ':' || session_id::text))
      from current_events where event_name = 'diff_viewed'
    ))
),
feedback_rows as (
  select
    t.touchpoint,
    count(f.feedback_id) as responses,
    count(distinct f.user_id) as respondent_users,
    count(f.feedback_id) filter (where f.helpful) as helpful,
    count(f.feedback_id) filter (where not f.helpful) as unhelpful,
    t.denominator as eligible_users,
    coalesce((
      select jsonb_agg(jsonb_build_object(
        'reason_code', r.reason_code, 'count', r.count
      ) order by r.count desc, r.reason_code)
      from feedback_reasons r where r.touchpoint = t.touchpoint
    ), '[]'::jsonb) as reason_counts
  from feedback_touchpoints t
  left join current_feedback f on f.touchpoint = t.touchpoint
  group by t.touchpoint, t.denominator
),
current_runs as materialized (
  select g.*
  from public.grading_runs g cross join bounds b
  where (b.since_at is null or g.created_at >= b.since_at)
    and g.created_at < b.until_at
    and coalesce(g.draft_role, 'ordinary') <> 'second'
    and g.parent_run_id is null
),
need_criteria as (
  select report_json -> 'priorities' -> 0 ->> 'criterion' as key, count(*) as count
  from current_runs
  where nullif(report_json -> 'priorities' -> 0 ->> 'criterion', '') is not null
  group by 1 having count(*) >= 5 order by count desc, key
),
need_actions as (
  select report_json -> 'priorities' -> 0 ->> 'action_type' as key, count(*) as count
  from current_runs
  where nullif(report_json -> 'priorities' -> 0 ->> 'action_type', '') is not null
  group by 1 having count(*) >= 5 order by count desc, key
),
need_topics as (
  select report_json ->> 'essay_topic_category' as key, count(*) as count
  from current_runs
  where nullif(report_json ->> 'essay_topic_category', '') is not null
  group by 1 having count(*) >= 5 order by count desc, key
),
daily as (
  select
    timezone('Asia/Shanghai', occurred_at)::date as day,
    count(distinct user_id) as active_users,
    count(distinct attempt_id) filter (where event_name = 'report_generated') as reports,
    count(distinct attempt_id) filter (where event_name = 'report_generation_failed') as failures
  from current_events
  group by timezone('Asia/Shanghai', occurred_at)::date
),
cohorts as (
  select f.user_id, timezone('Asia/Shanghai', f.first_seen_at)::date as cohort_day
  from first_seen f cross join bounds b
  where (b.since_at is null or f.first_seen_at >= b.since_at)
    and f.first_seen_at < b.until_at
),
active_days as materialized (
  select distinct e.user_id, timezone('Asia/Shanghai', e.occurred_at)::date as active_day
  from public.analytics_events e cross join bounds b
  where e.occurred_at < b.until_at
),
historical_first_seen as (
  select user_id, min(created_at) as first_grading_at
  from public.grading_runs group by user_id
)
select jsonb_build_object(
  'schema_version', 2,
  'since', (select since_at from bounds),
  'until', (select until_at from bounds),
  'generated_at', now(),
  'tracking_enabled_at', (select min(occurred_at) from public.analytics_events),
  'attempt_tracking_enabled_at', (
    select min(occurred_at) from public.analytics_events where attempt_id is not null
  ),
  'summary', (select value from current_summary),
  'previous_summary', case when p_since is null then null else (select value from previous_summary) end,
  'experience_funnel', (
    select jsonb_build_array(
      jsonb_build_object('stage', 'session_started', 'label', '访问', 'users', visited),
      jsonb_build_object('stage', 'first_draft_submitted', 'label', '提交初稿', 'users', submitted),
      jsonb_build_object('stage', 'report_generated', 'label', '生成报告', 'users', generated),
      jsonb_build_object('stage', 'report_viewed', 'label', '查看报告', 'users', viewed)
    ) from experience_counts
  ),
  'guest_report_login', (
    select jsonb_build_object(
      'eligible_users', eligible_users,
      'converted_users', converted_users
    ) from guest_report_login_counts
  ),
  'learning_funnel', (
    select jsonb_build_array(
      jsonb_build_object('stage', 'report_viewed', 'label', '查看报告', 'users', report_viewed),
      jsonb_build_object('stage', 'training_started', 'label', '进入训练', 'users', training_started),
      jsonb_build_object('stage', 'training_completed', 'label', '完成至少一项训练', 'users', training_completed),
      jsonb_build_object('stage', 'second_draft_generated', 'label', '生成二稿', 'users', second_draft_generated),
      jsonb_build_object('stage', 'diff_viewed', 'label', '查看两稿对比', 'users', diff_viewed)
    ) from learning_counts
  ),
  'quality', jsonb_build_object(
    'report', jsonb_build_object(
      'attempts', (select count(distinct attempt_id) from current_events where event_name = 'first_draft_submitted'),
      'successes', (select count(distinct attempt_id) from current_events where event_name = 'report_generated'),
      'failures', (select count(distinct attempt_id) from current_events where event_name = 'report_generation_failed'),
      'p50_duration_ms', (select percentile_disc(0.50) within group (order by duration_ms) from report_durations),
      'p95_duration_ms', (select percentile_disc(0.95) within group (order by duration_ms) from report_durations),
      'failure_types', coalesce((
        select jsonb_agg(to_jsonb(r) order by r.count desc, r.failure_type)
        from report_failure_types r
      ), '[]'::jsonb)
    ),
    'second_draft', jsonb_build_object(
      'attempts', (select count(distinct attempt_id) from current_events where event_name = 'second_draft_submitted'),
      'successes', (select count(distinct attempt_id) from current_events where event_name = 'second_draft_generated'),
      'failures', (select count(distinct attempt_id) from current_events where event_name = 'second_draft_generation_failed'),
      'p50_duration_ms', (select percentile_disc(0.50) within group (order by duration_ms) from second_durations),
      'p95_duration_ms', (select percentile_disc(0.95) within group (order by duration_ms) from second_durations),
      'failure_types', coalesce((
        select jsonb_agg(to_jsonb(r) order by r.count desc, r.failure_type)
        from second_failure_types r
      ), '[]'::jsonb)
    ),
    'training', jsonb_build_object(
      'started_users', (select training_started from learning_counts),
      'completed_users', (select training_completed from learning_counts)
    ),
    'draft_outcomes', jsonb_build_object(
      'eligible_users', (select count(distinct user_id) from draft_deltas),
      'improved_users', (select count(distinct user_id) from draft_deltas where band_delta > 0),
      'unchanged_users', (select count(distinct user_id) from draft_deltas where band_delta = 0),
      'declined_users', (select count(distinct user_id) from draft_deltas where band_delta < 0),
      'average_band_delta', (select round(avg(band_delta), 2) from draft_deltas)
    )
  ),
  'feedback', coalesce((
    select jsonb_agg(jsonb_build_object(
      'touchpoint', touchpoint,
      'responses', responses,
      'respondent_users', respondent_users,
      'helpful', helpful,
      'unhelpful', unhelpful,
      'eligible_users', eligible_users,
      'reason_counts', reason_counts
    ) order by touchpoint) from feedback_rows
  ), '[]'::jsonb),
  'learning_needs', jsonb_build_object(
    'criteria', coalesce((select jsonb_agg(to_jsonb(n) order by n.count desc, n.key) from need_criteria n), '[]'::jsonb),
    'action_types', coalesce((select jsonb_agg(to_jsonb(n) order by n.count desc, n.key) from need_actions n), '[]'::jsonb),
    'topics', coalesce((select jsonb_agg(to_jsonb(n) order by n.count desc, n.key) from need_topics n), '[]'::jsonb)
  ),
  'daily', coalesce((select jsonb_agg(to_jsonb(daily) order by day) from daily), '[]'::jsonb),
  'retention', jsonb_build_object(
    'day_1', jsonb_build_object(
      'eligible_users', (
        select count(*) from cohorts c cross join bounds b
        where c.cohort_day + 1 < timezone('Asia/Shanghai', b.until_at)::date
      ),
      'retained_users', (
        select count(*) from cohorts c cross join bounds b
        where c.cohort_day + 1 < timezone('Asia/Shanghai', b.until_at)::date
          and exists (
            select 1 from active_days a
            where a.user_id = c.user_id and a.active_day = c.cohort_day + 1
          )
      )
    ),
    'day_7', jsonb_build_object(
      'eligible_users', (
        select count(*) from cohorts c cross join bounds b
        where c.cohort_day + 7 < timezone('Asia/Shanghai', b.until_at)::date
      ),
      'retained_users', (
        select count(*) from cohorts c cross join bounds b
        where c.cohort_day + 7 < timezone('Asia/Shanghai', b.until_at)::date
          and exists (
            select 1 from active_days a
            where a.user_id = c.user_id and a.active_day = c.cohort_day + 7
          )
      )
    )
  ),
  'historical', jsonb_build_object(
    'unique_users', (select count(distinct user_id) from current_runs),
    'new_users', (
      select count(*) from historical_first_seen f cross join bounds b
      where (b.since_at is null or f.first_grading_at >= b.since_at)
        and f.first_grading_at < b.until_at
    ),
    'successful_reports', (select count(*) from current_runs),
    'training_started_users', (
      select count(distinct p.user_id) from public.practice_attempts p cross join bounds b
      where (b.since_at is null or p.created_at >= b.since_at) and p.created_at < b.until_at
    ),
    'training_completed_users', (
      select count(distinct p.user_id) from public.practice_attempts p cross join bounds b
      where p.status = 'mastered'
        and (b.since_at is null or p.updated_at >= b.since_at) and p.updated_at < b.until_at
    ),
    'second_draft_users', (
      select count(distinct d.user_id) from public.draft_revisions d cross join bounds b
      where (b.since_at is null or d.created_at >= b.since_at) and d.created_at < b.until_at
    )
  ),
  'data_quality', jsonb_build_object(
    'events_total', (select count(*) from current_events),
    'attempt_linked_events', (select count(*) from current_events where attempt_id is not null),
    'events_without_attempt_id', (select count(*) from current_events where attempt_id is null),
    'missing_attempt_outcomes', (
      select count(*) from current_events
      where event_name in (
        'first_draft_submitted', 'report_generated', 'report_generation_failed',
        'second_draft_submitted', 'second_draft_generated', 'second_draft_generation_failed'
      ) and attempt_id is null
    ),
    'feedback_responses', (select count(*) from current_feedback)
  )
) into v_result;
return v_result;
end;
$$;

revoke all on function public.get_analytics_dashboard_v2(timestamptz,timestamptz)
  from public, anon, authenticated;
grant execute on function public.get_analytics_dashboard_v2(timestamptz,timestamptz)
  to service_role;
