-- EssayPilot V2: run once in the Supabase SQL editor.
create extension if not exists pgcrypto;

create table if not exists public.essays (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  task_type text not null default 'Task 2' check (task_type = 'Task 2'),
  question text not null,
  content text not null,
  content_hash text not null,
  word_count integer not null check (word_count >= 0),
  status text not null default 'graded' check (status in ('graded', 'training', 'completed')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
alter table public.essays add column if not exists content_hash text;
create unique index if not exists essays_user_content_hash_idx on public.essays(user_id, content_hash);

create table if not exists public.grading_runs (
  id uuid primary key default gen_random_uuid(),
  essay_id uuid not null references public.essays(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  overall_band numeric(2,1) not null check (overall_band between 0 and 9),
  criteria jsonb not null,
  report_json jsonb not null,
  report_markdown text not null,
  model text not null,
  prompt_version text not null,
  skill_version text not null,
  draft_role text not null default 'ordinary' check (draft_role in ('ordinary', 'first', 'second')),
  parent_run_id uuid references public.grading_runs(id) on delete set null,
  idempotency_key text,
  created_at timestamptz not null default now()
);
alter table public.grading_runs add column if not exists draft_role text not null default 'ordinary';
alter table public.grading_runs add column if not exists parent_run_id uuid references public.grading_runs(id) on delete set null;
alter table public.grading_runs add column if not exists idempotency_key text;
create unique index if not exists grading_runs_user_idempotency_key_idx
on public.grading_runs(user_id, idempotency_key);

create table if not exists public.practice_attempts (
  id uuid primary key default gen_random_uuid(),
  grading_run_id uuid not null references public.grading_runs(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  task_kind text not null check (task_kind in ('sentence', 'logic')),
  task_key_hash text check (task_key_hash ~ '^[a-f0-9]{64}$'),
  task_index integer not null,
  original_text text not null,
  submitted_text text not null,
  feedback text not null default '',
  revision_text text not null default '',
  status text not null default 'in_progress' check (status in ('in_progress', 'mastered')),
  error_tags text[] not null default '{}',
  training_action_id uuid,
  training_flow_id uuid,
  feedback_persisted_at timestamptz not null default now(),
  settled_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index if not exists practice_task_once_idx
on public.practice_attempts(user_id, grading_run_id, task_kind, task_key_hash);
create unique index if not exists practice_training_action_once_idx
on public.practice_attempts(training_action_id)
where training_action_id is not null;
create unique index if not exists practice_training_flow_once_idx
on public.practice_attempts(user_id, training_flow_id)
where training_flow_id is not null;

create table if not exists public.draft_revisions (
  id uuid primary key default gen_random_uuid(),
  essay_id uuid not null references public.essays(id) on delete cascade,
  grading_run_id uuid not null references public.grading_runs(id) on delete cascade,
  revised_grading_run_id uuid references public.grading_runs(id) on delete set null,
  user_id uuid not null references auth.users(id) on delete cascade,
  draft_number integer not null check (draft_number >= 2),
  content text not null,
  score_snapshot jsonb not null,
  report_json jsonb not null default '{}'::jsonb,
  report_markdown text not null default '',
  progress_report text not null,
  idempotency_key text,
  created_at timestamptz not null default now()
);
alter table public.draft_revisions add column if not exists revised_grading_run_id uuid references public.grading_runs(id) on delete set null;
alter table public.draft_revisions add column if not exists idempotency_key text;
create unique index if not exists draft_revisions_user_idempotency_key_idx
on public.draft_revisions(user_id, idempotency_key);

create table if not exists public.learning_items (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  grading_run_id uuid references public.grading_runs(id) on delete cascade,
  item_key text not null,
  item_type text not null check (item_type in ('error', 'expression')),
  category text not null,
  source_text text not null,
  target_text text not null default '',
  explanation text not null default '',
  origin text not null default 'report' check (origin in ('catalog', 'report')),
  topic_category text not null default 'society_family',
  function_category text not null default 'core_collocation',
  usage_note text not null default '',
  favorite boolean not null default false,
  status text not null default 'new' check (status in ('new', 'practicing', 'mastered')),
  review_count integer not null default 0 check (review_count >= 0),
  last_reviewed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(user_id, item_key)
);

create table if not exists public.expression_attempts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  learning_item_id uuid not null references public.learning_items(id) on delete cascade,
  submitted_sentence text not null,
  feedback_zh text not null,
  improved_sentence_en text not null,
  appropriate boolean not null default false,
  mastered boolean not null default false,
  model text not null,
  prompt_version text not null,
  created_at timestamptz not null default now()
);

alter table public.essays enable row level security;
alter table public.grading_runs enable row level security;
alter table public.practice_attempts enable row level security;
alter table public.draft_revisions enable row level security;
alter table public.learning_items enable row level security;
alter table public.expression_attempts enable row level security;

create or replace function public.set_updated_at() returns trigger
language plpgsql as $$ begin new.updated_at = now(); return new; end; $$;
drop trigger if exists essays_set_updated_at on public.essays;
create trigger essays_set_updated_at before update on public.essays
for each row execute function public.set_updated_at();
drop trigger if exists practice_set_updated_at on public.practice_attempts;
create trigger practice_set_updated_at before update on public.practice_attempts
for each row execute function public.set_updated_at();
drop trigger if exists learning_items_set_updated_at on public.learning_items;
create trigger learning_items_set_updated_at before update on public.learning_items
for each row execute function public.set_updated_at();

drop policy if exists "owners manage essays" on public.essays;
create policy "owners manage essays" on public.essays for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
drop policy if exists "owners manage grading runs" on public.grading_runs;
create policy "owners manage grading runs" on public.grading_runs for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
drop policy if exists "owners manage practice" on public.practice_attempts;
create policy "owners manage practice" on public.practice_attempts for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
drop policy if exists "owners manage revisions" on public.draft_revisions;
create policy "owners manage revisions" on public.draft_revisions for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
drop policy if exists "owners manage learning items" on public.learning_items;
create policy "owners manage learning items" on public.learning_items for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
drop policy if exists "owners manage expression attempts" on public.expression_attempts;
create policy "owners manage expression attempts" on public.expression_attempts for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

create or replace function public.save_grading_cycle(
  p_question text,
  p_essay text,
  p_word_count integer,
  p_content_hash text,
  p_overall_band numeric,
  p_criteria jsonb,
  p_report_json jsonb,
  p_report_markdown text,
  p_model text,
  p_prompt_version text,
  p_skill_version text
) returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_user uuid := auth.uid();
  v_essay uuid;
  v_run uuid;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  select e.id into v_essay
  from essays e
  where e.user_id = v_user and e.content_hash = p_content_hash
  limit 1;
  if v_essay is not null then
    select g.id into v_run
    from grading_runs g
    where g.essay_id = v_essay and g.prompt_version = p_prompt_version
    order by g.created_at desc limit 1;
  end if;
  if v_run is not null then
    return jsonb_build_object('essay_id', v_essay, 'grading_run_id', v_run, 'reused', true);
  end if;
  if v_essay is null then
    insert into essays(user_id, task_type, question, content, content_hash, word_count)
    values(v_user, 'Task 2', p_question, p_essay, p_content_hash, p_word_count)
    returning id into v_essay;
  end if;
  insert into grading_runs(essay_id, user_id, overall_band, criteria, report_json, report_markdown, model, prompt_version, skill_version)
  values(v_essay, v_user, p_overall_band, p_criteria, p_report_json, p_report_markdown, p_model, p_prompt_version, p_skill_version)
  returning id into v_run;
  return jsonb_build_object('essay_id', v_essay, 'grading_run_id', v_run);
end;
$$;

grant execute on function public.save_grading_cycle(text,text,integer,text,numeric,jsonb,jsonb,text,text,text,text) to authenticated;

create or replace function public.save_linked_grading_cycle(
  p_question text, p_essay text, p_word_count integer, p_content_hash text,
  p_overall_band numeric, p_criteria jsonb, p_report_json jsonb,
  p_report_markdown text, p_model text, p_prompt_version text,
  p_skill_version text, p_parent_run_id uuid, p_draft_role text
) returns jsonb
language plpgsql security invoker set search_path = public
as $$
declare
  v_user uuid := auth.uid();
  v_essay uuid;
  v_run uuid;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_draft_role not in ('first', 'second') then raise exception 'Invalid draft role'; end if;
  if not exists (select 1 from grading_runs where id = p_parent_run_id and user_id = v_user)
    then raise exception 'Parent grading run not found'; end if;
  select id into v_essay from essays where user_id = v_user and content_hash = p_content_hash limit 1;
  if v_essay is null then
    insert into essays(user_id, task_type, question, content, content_hash, word_count)
    values(v_user, 'Task 2', p_question, p_essay, p_content_hash, p_word_count)
    returning id into v_essay;
  end if;
  select id into v_run from grading_runs
  where user_id = v_user and essay_id = v_essay and prompt_version = p_prompt_version
    and parent_run_id = p_parent_run_id and draft_role = p_draft_role
  order by created_at desc limit 1;
  if v_run is not null then
    return jsonb_build_object('essay_id', v_essay, 'grading_run_id', v_run, 'reused', true);
  end if;
  update grading_runs set draft_role = 'first'
  where id = p_parent_run_id and user_id = v_user and draft_role = 'ordinary';
  insert into grading_runs(
    essay_id, user_id, overall_band, criteria, report_json, report_markdown,
    model, prompt_version, skill_version, draft_role, parent_run_id
  ) values (
    v_essay, v_user, p_overall_band, p_criteria, p_report_json, p_report_markdown,
    p_model, p_prompt_version, p_skill_version, p_draft_role, p_parent_run_id
  ) returning id into v_run;
  return jsonb_build_object('essay_id', v_essay, 'grading_run_id', v_run);
end;
$$;
grant execute on function public.save_linked_grading_cycle(text,text,integer,text,numeric,jsonb,jsonb,text,text,text,text,uuid,text) to authenticated;

create or replace function public.save_second_draft_result(
  p_grading_run_id uuid,
  p_flow_id uuid,
  p_question text,
  p_content text,
  p_word_count integer,
  p_content_hash text,
  p_overall_band numeric,
  p_criteria jsonb,
  p_report_json jsonb,
  p_report_markdown text,
  p_model text,
  p_prompt_version text,
  p_skill_version text,
  p_score_snapshot jsonb,
  p_progress_report text
) returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_original_essay_id uuid;
  v_second_essay_id uuid;
  v_revised_run_id uuid;
  v_revision_id uuid;
  v_existing_hash text;
  v_existing_revision_run_id uuid;
  v_action_status text;
  v_action_revised_run_id uuid;
  v_action_reserved_at timestamptz;
  v_action_expires_at timestamptz;
  v_revised_created_at timestamptz;
  v_run_key text := 'second-draft-run:' || p_grading_run_id::text;
  v_revision_key text := 'second-draft-revision:' || p_grading_run_id::text;
  v_reused boolean := false;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_grading_run_id is null or p_flow_id is null then
    raise exception 'Grading run id and flow id are required';
  end if;
  if p_content_hash is null or p_content_hash !~ '^[a-f0-9]{64}$' then
    raise exception 'Invalid content hash';
  end if;
  if p_content is null or btrim(p_content) = '' or p_word_count < 1 then
    raise exception 'Second draft content is required';
  end if;

  select a.status, a.revised_grading_run_id, a.reserved_at,
         a.reservation_expires_at
    into v_action_status, v_action_revised_run_id, v_action_reserved_at,
         v_action_expires_at
  from public.membership_second_draft_actions a
  join public.membership_run_accesses r on r.id = a.run_access_id
  where a.user_id = v_user
    and a.flow_id = p_flow_id
    and a.content_hash = p_content_hash
    and r.user_id = v_user
    and r.grading_run_id = p_grading_run_id
    and r.status = 'completed'
  for update of a;
  if not found then raise exception 'Matching second draft reservation required'; end if;

  select g.essay_id into v_original_essay_id
  from public.grading_runs g
  where g.id = p_grading_run_id
    and g.user_id = v_user
    and g.parent_run_id is null
    and g.draft_role in ('ordinary', 'first')
  for update;
  if not found then raise exception 'Original grading run not found'; end if;

  select g.id, g.essay_id, e.content_hash, g.created_at
    into v_revised_run_id, v_second_essay_id, v_existing_hash, v_revised_created_at
  from public.grading_runs g
  join public.essays e on e.id = g.essay_id
  where g.user_id = v_user
    and g.parent_run_id = p_grading_run_id
    and g.draft_role = 'second'
  order by (g.idempotency_key = v_run_key) desc nulls last, g.created_at desc
  limit 1;
  if v_revised_run_id is not null then
    if v_existing_hash <> p_content_hash then
      raise exception 'A different second draft already exists for this grading run';
    end if;
    if v_revised_created_at < v_action_reserved_at
      or v_revised_created_at > v_action_expires_at then
      raise exception 'Second draft result is outside its reservation';
    end if;
    if v_action_status = 'completed'
      and v_action_revised_run_id is distinct from v_revised_run_id then
      raise exception 'Completed reservation belongs to another second draft';
    end if;
    update public.grading_runs set idempotency_key = v_run_key
    where id = v_revised_run_id and idempotency_key is null;
    v_reused := true;
  else
    if v_action_status <> 'reserved' or v_action_expires_at <= now() then
      raise exception 'Second draft reservation expired before persistence';
    end if;
    select e.id into v_second_essay_id
    from public.essays e
    where e.user_id = v_user and e.content_hash = p_content_hash
    limit 1;
    if v_second_essay_id is null then
      insert into public.essays(
        user_id, task_type, question, content, content_hash, word_count
      ) values (
        v_user, 'Task 2', p_question, p_content, p_content_hash, p_word_count
      ) returning id into v_second_essay_id;
    end if;
    insert into public.grading_runs(
      essay_id, user_id, overall_band, criteria, report_json, report_markdown,
      model, prompt_version, skill_version, draft_role, parent_run_id,
      idempotency_key
    ) values (
      v_second_essay_id, v_user, p_overall_band, p_criteria, p_report_json,
      p_report_markdown, p_model, p_prompt_version, p_skill_version, 'second',
      p_grading_run_id, v_run_key
    ) returning id into v_revised_run_id;
  end if;

  update public.grading_runs set draft_role = 'first'
  where id = p_grading_run_id and user_id = v_user and draft_role = 'ordinary';

  select d.id, d.revised_grading_run_id
    into v_revision_id, v_existing_revision_run_id
  from public.draft_revisions d
  where d.user_id = v_user and d.grading_run_id = p_grading_run_id
    and d.draft_number = 2
  order by (d.idempotency_key = v_revision_key) desc nulls last, d.created_at desc
  limit 1;
  if v_revision_id is not null then
    if v_existing_revision_run_id is not null
      and v_existing_revision_run_id <> v_revised_run_id then
      raise exception 'Draft revision belongs to another revised grading run';
    end if;
    update public.draft_revisions
    set revised_grading_run_id = v_revised_run_id,
        content = p_content,
        score_snapshot = p_score_snapshot,
        report_json = '{}'::jsonb,
        report_markdown = '',
        progress_report = p_progress_report,
        idempotency_key = v_revision_key
    where id = v_revision_id;
  else
    insert into public.draft_revisions(
      essay_id, grading_run_id, revised_grading_run_id, user_id, draft_number,
      content, score_snapshot, report_json, report_markdown, progress_report,
      idempotency_key
    ) values (
      v_original_essay_id, p_grading_run_id, v_revised_run_id, v_user, 2,
      p_content, p_score_snapshot, '{}'::jsonb, '', p_progress_report,
      v_revision_key
    ) returning id into v_revision_id;
  end if;

  return jsonb_build_object(
    'essay_id', v_second_essay_id,
    'grading_run_id', v_revised_run_id,
    'draft_revision_id', v_revision_id,
    'reused', v_reused
  );
end;
$$;

revoke all on function public.save_second_draft_result(
  uuid,uuid,text,text,integer,text,numeric,jsonb,jsonb,text,text,text,text,jsonb,text
) from public, anon;
grant execute on function public.save_second_draft_result(
  uuid,uuid,text,text,integer,text,numeric,jsonb,jsonb,text,text,text,text,jsonb,text
) to authenticated;

grant select, insert, update, delete on public.essays, public.grading_runs, public.practice_attempts, public.draft_revisions to authenticated;
grant select, insert, update, delete on public.learning_items to authenticated;
grant select, insert, update, delete on public.expression_attempts to authenticated;

-- Anonymous public-beta funnel. The function returns counts only: no user ids,
-- email addresses, essay text, or reports leave the database.
create or replace function public.get_beta_funnel(p_since timestamptz)
returns jsonb
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  with first_runs as (
    select distinct on (g.user_id)
      g.user_id,
      g.id as grading_run_id,
      g.created_at as first_grading_at
    from public.grading_runs g
    where p_since is not null and g.created_at >= p_since
    order by g.user_id, g.created_at, g.id
  ),
  per_user as (
    select
      f.user_id,
      f.first_grading_at,
      (
        select min(p.updated_at)
        from public.practice_attempts p
        where p.user_id = f.user_id
          and p.grading_run_id = f.grading_run_id
          and p.task_kind = 'sentence'
          and p.status = 'mastered'
      ) as sentence_mastered_at,
      (
        select min(p.updated_at)
        from public.practice_attempts p
        where p.user_id = f.user_id
          and p.grading_run_id = f.grading_run_id
          and p.task_kind = 'logic'
          and p.status = 'mastered'
      ) as logic_mastered_at,
      (
        select min(d.created_at)
        from public.draft_revisions d
        where d.user_id = f.user_id
          and d.grading_run_id = f.grading_run_id
          and d.draft_number >= 2
      ) as second_draft_at
    from first_runs f
  ),
  daily_events as (
    select user_id, first_grading_at as occurred_at, 'first_grading'::text as stage from per_user
    union all
    select user_id, sentence_mastered_at, 'sentence_mastered' from per_user where sentence_mastered_at is not null
    union all
    select user_id, logic_mastered_at, 'logic_mastered' from per_user where logic_mastered_at is not null
    union all
    select user_id, greatest(sentence_mastered_at, logic_mastered_at), 'both_mastered'
    from per_user where sentence_mastered_at is not null and logic_mastered_at is not null
    union all
    select user_id, second_draft_at, 'second_draft' from per_user where second_draft_at is not null
  ),
  daily as (
    select
      occurred_at::date as day,
      count(distinct user_id) filter (where stage = 'first_grading') as first_grading_users,
      count(distinct user_id) filter (where stage = 'sentence_mastered') as sentence_mastered_users,
      count(distinct user_id) filter (where stage = 'logic_mastered') as logic_mastered_users,
      count(distinct user_id) filter (where stage = 'both_mastered') as both_mastered_users,
      count(distinct user_id) filter (where stage = 'second_draft') as second_draft_users
    from daily_events
    group by occurred_at::date
  )
  select jsonb_build_object(
    'since', p_since,
    'generated_at', now(),
    'first_grading_users', count(*),
    'sentence_mastered_users', count(*) filter (where sentence_mastered_at is not null),
    'logic_mastered_users', count(*) filter (where logic_mastered_at is not null),
    'both_mastered_users', count(*) filter (
      where sentence_mastered_at is not null and logic_mastered_at is not null
    ),
    'second_draft_users', count(*) filter (where second_draft_at is not null),
    'daily', coalesce(
      (select jsonb_agg(to_jsonb(daily) order by day) from daily),
      '[]'::jsonb
    )
  )
  from per_user;
$$;

revoke all on function public.get_beta_funnel(timestamptz) from public, anon, authenticated;
grant execute on function public.get_beta_funnel(timestamptz) to service_role;

-- EssayPilot guest trial and privacy-safe product lifecycle funnel.
-- Run once in the Supabase SQL editor before deploying the matching app version.

create table if not exists public.guest_trials (
    visitor_hash text primary key check (visitor_hash ~ '^[a-f0-9]{64}$'),
    status text not null check (status in ('reserved', 'used')),
    reservation_id uuid,
    reserved_at timestamptz,
    used_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.product_events (
    id bigint generated always as identity primary key,
    event_name text not null check (event_name in (
        'visitor_opened', 'login_completed', 'grading_started',
        'grading_completed', 'report_viewed', 'report_training_clicked',
        'second_draft_completed'
    )),
    visitor_hash text not null check (visitor_hash ~ '^[a-f0-9]{64}$'),
    user_id uuid references auth.users(id) on delete set null,
    flow_id uuid not null,
    occurred_at timestamptz not null default now(),
    unique (visitor_hash, event_name, flow_id)
);

alter table public.guest_trials enable row level security;
alter table public.product_events enable row level security;
revoke all on public.guest_trials, public.product_events from public, anon, authenticated;

create or replace function public.reserve_guest_trial(p_visitor_hash text, p_flow_id uuid)
returns jsonb language plpgsql security definer set search_path = public, pg_temp as $$
declare v_trial public.guest_trials%rowtype;
begin
  if p_visitor_hash !~ '^[a-f0-9]{64}$' then raise exception 'Invalid visitor'; end if;
  insert into public.guest_trials(visitor_hash, status, reservation_id, reserved_at)
  values (p_visitor_hash, 'reserved', p_flow_id, now())
  on conflict (visitor_hash) do update set
    status = 'reserved', reservation_id = p_flow_id, reserved_at = now(), updated_at = now()
  where guest_trials.status <> 'used'
    and (guest_trials.reservation_id = p_flow_id or guest_trials.reserved_at < now() - interval '20 minutes');
  select * into v_trial from public.guest_trials where visitor_hash = p_visitor_hash;
  return jsonb_build_object(
    'allowed', v_trial.status = 'reserved' and v_trial.reservation_id = p_flow_id,
    'status', v_trial.status
  );
end; $$;

create or replace function public.complete_guest_trial(p_visitor_hash text, p_flow_id uuid)
returns boolean language plpgsql security definer set search_path = public, pg_temp as $$
begin
  update public.guest_trials set status='used', used_at=now(), updated_at=now()
  where visitor_hash=p_visitor_hash and status='reserved' and reservation_id=p_flow_id;
  return found;
end; $$;

create or replace function public.release_guest_trial(p_visitor_hash text, p_flow_id uuid)
returns boolean language plpgsql security definer set search_path = public, pg_temp as $$
begin
  delete from public.guest_trials
  where visitor_hash=p_visitor_hash and status='reserved' and reservation_id=p_flow_id;
  return found;
end; $$;

create or replace function public.record_product_event(
  p_event_name text, p_visitor_hash text, p_flow_id uuid
) returns boolean language plpgsql security definer set search_path = public, pg_temp as $$
begin
  if p_event_name not in (
    'visitor_opened', 'login_completed', 'grading_started',
    'grading_completed', 'report_viewed', 'report_training_clicked',
    'second_draft_completed'
  ) or p_visitor_hash !~ '^[a-f0-9]{64}$' then
    raise exception 'Invalid product event';
  end if;
  insert into public.product_events(event_name, visitor_hash, user_id, flow_id)
  values (p_event_name, p_visitor_hash, auth.uid(), p_flow_id)
  on conflict (visitor_hash, event_name, flow_id) do nothing;
  return true;
end; $$;

create or replace function public.get_product_funnel(p_since timestamptz)
returns jsonb language sql stable security definer set search_path = public, pg_temp as $$
  with filtered as (
    select * from public.product_events where occurred_at >= p_since
  ), counts as (
    select
      count(distinct visitor_hash) filter (where event_name='visitor_opened') as visitors,
      count(distinct visitor_hash) filter (where event_name='login_completed') as logins,
      count(distinct flow_id) filter (where event_name='grading_started') as grading_starts,
      count(distinct flow_id) filter (where event_name='grading_completed') as grading_completions,
      count(distinct flow_id) filter (where event_name='report_viewed') as report_views,
      count(distinct flow_id) filter (where event_name='report_training_clicked') as training_clicks,
      count(distinct flow_id) filter (where event_name='second_draft_completed') as second_drafts
    from filtered
  )
  select jsonb_build_object(
    'since', p_since,
    'generated_at', now(),
    'visitors', visitors,
    'logins', logins,
    'grading_starts', grading_starts,
    'grading_completions', grading_completions,
    'report_views', report_views,
    'training_clicks', training_clicks,
    'second_drafts', second_drafts
  ) from counts;
$$;

revoke all on function public.reserve_guest_trial(text,uuid) from public;
revoke all on function public.complete_guest_trial(text,uuid) from public;
revoke all on function public.release_guest_trial(text,uuid) from public;
revoke all on function public.record_product_event(text,text,uuid) from public;
revoke all on function public.get_product_funnel(timestamptz) from public, anon, authenticated;
grant execute on function public.reserve_guest_trial(text,uuid) to anon, authenticated;
grant execute on function public.complete_guest_trial(text,uuid) to anon, authenticated;
grant execute on function public.release_guest_trial(text,uuid) to anon, authenticated;
grant execute on function public.record_product_event(text,text,uuid) to anon, authenticated;
grant execute on function public.get_product_funnel(timestamptz) to service_role;

-- EssayPilot guest-trial settlement idempotency.
-- Preserve flow ownership after completion or release so lost responses can be
-- retried safely without allowing a different flow to claim the same trial.

alter table public.guest_trials
  drop constraint if exists guest_trials_status_check;
alter table public.guest_trials
  add constraint guest_trials_status_check
  check (status in ('reserved', 'used', 'released'));

create or replace function public.reserve_guest_trial(p_visitor_hash text, p_flow_id uuid)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare v_trial public.guest_trials%rowtype;
begin
  if p_visitor_hash !~ '^[a-f0-9]{64}$' then raise exception 'Invalid visitor'; end if;
  insert into public.guest_trials(visitor_hash, status, reservation_id, reserved_at)
  values (p_visitor_hash, 'reserved', p_flow_id, now())
  on conflict (visitor_hash) do update set
    status = 'reserved', reservation_id = p_flow_id, reserved_at = now(),
    used_at = null, updated_at = now()
  where guest_trials.status = 'released'
    or (
      guest_trials.status = 'reserved'
      and (
        guest_trials.reservation_id = p_flow_id
        or guest_trials.reserved_at < now() - interval '20 minutes'
      )
    );
  select * into v_trial from public.guest_trials where visitor_hash = p_visitor_hash;
  return jsonb_build_object(
    'allowed', v_trial.status = 'reserved' and v_trial.reservation_id = p_flow_id,
    'status', v_trial.status
  );
end; $$;

create or replace function public.complete_guest_trial(p_visitor_hash text, p_flow_id uuid)
returns boolean language plpgsql security definer set search_path = pg_catalog, public as $$
begin
  update public.guest_trials set status='used', used_at=now(), updated_at=now()
  where visitor_hash=p_visitor_hash and status='reserved' and reservation_id=p_flow_id;
  if found then return true; end if;

  return exists (
    select 1 from public.guest_trials
    where visitor_hash=p_visitor_hash and status='used' and reservation_id=p_flow_id
  );
end; $$;

create or replace function public.release_guest_trial(p_visitor_hash text, p_flow_id uuid)
returns boolean language plpgsql security definer set search_path = pg_catalog, public as $$
begin
  update public.guest_trials set status='released', updated_at=now()
  where visitor_hash=p_visitor_hash and status='reserved' and reservation_id=p_flow_id;
  if found then return true; end if;

  return exists (
    select 1 from public.guest_trials
    where visitor_hash=p_visitor_hash and status='released' and reservation_id=p_flow_id
  );
end; $$;

-- EssayPilot founder membership access.
-- The manually reviewed CNY 7.50 founder pack lasts 30 days and unlocks
-- three first-draft learning cycles. Each claimed cycle includes up to three
-- sentence/logic AI reviews and one second-draft score/comparison.

create table if not exists public.memberships (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null unique references auth.users(id) on delete cascade,
  plan_code text not null default 'founder_pass_30d_3runs'
    check (plan_code = 'founder_pass_30d_3runs'),
  status text not null default 'active'
    check (status in ('active', 'revoked', 'refunded')),
  run_quota integer not null default 3 check (run_quota = 3),
  training_actions_per_run integer not null default 3
    check (training_actions_per_run = 3),
  second_drafts_per_run integer not null default 1
    check (second_drafts_per_run = 1),
  source text not null default 'manual'
    check (source in ('manual', 'complimentary')),
  grant_reference text,
  starts_at timestamptz not null default now(),
  expires_at timestamptz not null default (now() + interval '30 days'),
  revoked_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (expires_at = starts_at + interval '30 days'),
  check (
    (status = 'active' and revoked_at is null)
    or (status in ('revoked', 'refunded') and revoked_at is not null)
  )
);

create unique index if not exists memberships_grant_reference_idx
  on public.memberships(grant_reference)
  where grant_reference is not null;

create table if not exists public.membership_requests (
  id uuid primary key default gen_random_uuid(),
  request_code text not null unique,
  user_id uuid not null references auth.users(id) on delete cascade,
  plan_code text not null default 'founder_pass_30d_3runs'
    check (plan_code = 'founder_pass_30d_3runs'),
  amount_cny numeric(4,2) not null default 7.50 check (amount_cny = 7.50),
  currency text not null default 'CNY' check (currency = 'CNY'),
  payment_reference text not null unique
    check (
      payment_reference = btrim(payment_reference)
      and char_length(payment_reference) between 4 and 128
    ),
  paid_at timestamptz,
  note text not null default '' check (char_length(note) <= 500),
  status text not null default 'pending'
    check (status in ('pending', 'approved', 'rejected')),
  membership_id uuid references public.memberships(id) on delete set null,
  reviewed_at timestamptz,
  reviewed_by text check (
    reviewed_by is null or char_length(reviewed_by) between 1 and 128
  ),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (
    (status = 'pending' and membership_id is null and reviewed_at is null)
    or (status = 'approved' and membership_id is not null and reviewed_at is not null)
    or (status = 'rejected' and membership_id is null and reviewed_at is not null)
  )
);

alter table public.membership_requests
  add column if not exists reviewed_by text,
  add column if not exists amount_cny numeric(4,2) not null default 7.50
    check (amount_cny = 7.50),
  add column if not exists currency text not null default 'CNY'
    check (currency = 'CNY');

create unique index if not exists membership_requests_one_pending_user_idx
  on public.membership_requests(user_id)
  where status = 'pending';

create table if not exists public.membership_run_accesses (
  id uuid primary key default gen_random_uuid(),
  membership_id uuid not null references public.memberships(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  flow_id uuid not null,
  content_hash text not null check (content_hash ~ '^[a-f0-9]{64}$'),
  grading_run_id uuid references public.grading_runs(id) on delete restrict,
  status text not null default 'reserved'
    check (status in ('reserved', 'completed', 'released')),
  reserved_at timestamptz not null default now(),
  reservation_expires_at timestamptz not null default (now() + interval '30 minutes'),
  completed_at timestamptz,
  released_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(user_id, flow_id),
  unique(user_id, content_hash),
  check (reservation_expires_at > reserved_at),
  check (
    (status = 'reserved' and completed_at is null and released_at is null)
    or (status = 'completed' and completed_at is not null and released_at is null)
    or (status = 'released' and completed_at is null and released_at is not null)
  )
);

create unique index if not exists membership_run_accesses_run_idx
  on public.membership_run_accesses(user_id, grading_run_id)
  where grading_run_id is not null;

create index if not exists membership_run_accesses_usage_idx
  on public.membership_run_accesses(membership_id, status, reservation_expires_at);

create table if not exists public.membership_training_actions (
  id uuid primary key default gen_random_uuid(),
  run_access_id uuid not null
    references public.membership_run_accesses(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  flow_id uuid not null,
  task_kind text not null check (task_kind in ('sentence', 'logic')),
  task_key_hash text not null check (task_key_hash ~ '^[a-f0-9]{64}$'),
  status text not null default 'reserved'
    check (status in ('reserved', 'completed', 'released')),
  reserved_at timestamptz not null default now(),
  reservation_expires_at timestamptz not null default (now() + interval '30 minutes'),
  completed_at timestamptz,
  released_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(user_id, flow_id),
  unique(run_access_id, task_kind, task_key_hash),
  check (reservation_expires_at > reserved_at),
  check (
    (status = 'reserved' and completed_at is null and released_at is null)
    or (status = 'completed' and completed_at is not null and released_at is null)
    or (status = 'released' and completed_at is null and released_at is not null)
  )
);

create index if not exists membership_training_actions_usage_idx
  on public.membership_training_actions(run_access_id, status, reservation_expires_at);

-- A generated practice review is the durable proof for settling one reserved
-- training action.  The opaque task hash and flow bind that proof to exactly
-- one user-owned first-draft task without exposing the task text in the quota
-- tables.
alter table public.practice_attempts
  add column if not exists task_key_hash text,
  add column if not exists training_action_id uuid,
  add column if not exists training_flow_id uuid,
  add column if not exists feedback_persisted_at timestamptz not null default now(),
  add column if not exists settled_at timestamptz;

do $$
begin
  if not exists (
    select 1 from pg_catalog.pg_constraint
    where conname = 'practice_attempts_training_action_fkey'
      and conrelid = 'public.practice_attempts'::regclass
  ) then
    alter table public.practice_attempts
      add constraint practice_attempts_training_action_fkey
      foreign key (training_action_id)
      references public.membership_training_actions(id) on delete restrict;
  end if;
  if not exists (
    select 1 from pg_catalog.pg_constraint
    where conname = 'practice_attempts_feedback_proof_check'
      and conrelid = 'public.practice_attempts'::regclass
  ) then
    alter table public.practice_attempts
      add constraint practice_attempts_feedback_proof_check check (
        (training_action_id is null and training_flow_id is null)
        or (
          training_action_id is not null
          and training_flow_id is not null
          and task_key_hash ~ '^[a-f0-9]{64}$'
          and btrim(feedback) <> ''
        )
      );
  end if;
end;
$$;

drop index if exists public.practice_task_once_idx;
create unique index practice_task_once_idx
  on public.practice_attempts(user_id, grading_run_id, task_kind, task_key_hash);
create unique index if not exists practice_training_action_once_idx
  on public.practice_attempts(training_action_id)
  where training_action_id is not null;
create unique index if not exists practice_training_flow_once_idx
  on public.practice_attempts(user_id, training_flow_id)
  where training_flow_id is not null;

create table if not exists public.membership_second_draft_actions (
  id uuid primary key default gen_random_uuid(),
  run_access_id uuid not null unique
    references public.membership_run_accesses(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  flow_id uuid not null,
  content_hash text not null check (content_hash ~ '^[a-f0-9]{64}$'),
  revised_grading_run_id uuid references public.grading_runs(id) on delete restrict,
  status text not null default 'reserved'
    check (status in ('reserved', 'completed', 'released')),
  reserved_at timestamptz not null default now(),
  reservation_expires_at timestamptz not null default (now() + interval '30 minutes'),
  completed_at timestamptz,
  released_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(user_id, flow_id),
  check (reservation_expires_at > reserved_at),
  check (
    (status = 'reserved' and completed_at is null and released_at is null)
    or (status = 'completed' and completed_at is not null and released_at is null)
    or (status = 'released' and completed_at is null and released_at is not null)
  )
);

drop trigger if exists memberships_set_updated_at on public.memberships;
create trigger memberships_set_updated_at before update on public.memberships
for each row execute function public.set_updated_at();

drop trigger if exists membership_requests_set_updated_at on public.membership_requests;
create trigger membership_requests_set_updated_at before update on public.membership_requests
for each row execute function public.set_updated_at();

drop trigger if exists membership_run_accesses_set_updated_at
  on public.membership_run_accesses;
create trigger membership_run_accesses_set_updated_at
before update on public.membership_run_accesses
for each row execute function public.set_updated_at();

drop trigger if exists membership_training_actions_set_updated_at
  on public.membership_training_actions;
create trigger membership_training_actions_set_updated_at
before update on public.membership_training_actions
for each row execute function public.set_updated_at();

drop trigger if exists membership_second_draft_actions_set_updated_at
  on public.membership_second_draft_actions;
create trigger membership_second_draft_actions_set_updated_at
before update on public.membership_second_draft_actions
for each row execute function public.set_updated_at();

alter table public.memberships enable row level security;
alter table public.membership_requests enable row level security;
alter table public.membership_run_accesses enable row level security;
alter table public.membership_training_actions enable row level security;
alter table public.membership_second_draft_actions enable row level security;

-- SECURITY DEFINER functions below trust public objects, so application roles
-- must never be able to shadow them by creating objects in this schema.
revoke create on schema public from public, anon, authenticated;

revoke all on
  public.memberships,
  public.membership_requests,
  public.membership_run_accesses,
  public.membership_training_actions,
  public.membership_second_draft_actions
from public, anon, authenticated;

grant select on
  public.memberships,
  public.membership_requests,
  public.membership_run_accesses,
  public.membership_training_actions,
  public.membership_second_draft_actions
to authenticated;

grant select, insert, update, delete on
  public.memberships,
  public.membership_requests,
  public.membership_run_accesses,
  public.membership_training_actions,
  public.membership_second_draft_actions
to service_role;

drop policy if exists "owners view memberships" on public.memberships;
create policy "owners view memberships" on public.memberships
for select using (auth.uid() = user_id);

drop policy if exists "owners view membership requests" on public.membership_requests;
create policy "owners view membership requests" on public.membership_requests
for select using (auth.uid() = user_id);

drop policy if exists "owners view membership run accesses"
  on public.membership_run_accesses;
create policy "owners view membership run accesses"
on public.membership_run_accesses
for select using (auth.uid() = user_id);

drop policy if exists "owners view membership training actions"
  on public.membership_training_actions;
create policy "owners view membership training actions"
on public.membership_training_actions
for select using (auth.uid() = user_id);

drop policy if exists "owners view membership second draft actions"
  on public.membership_second_draft_actions;
create policy "owners view membership second draft actions"
on public.membership_second_draft_actions
for select using (auth.uid() = user_id);

-- Authenticated clients read their attempts through RLS, but all writes now go
-- through the binding RPC below so quota proof cannot race a reused lease.
revoke insert, update, delete on public.practice_attempts from authenticated;
grant select on public.practice_attempts to authenticated;

create or replace function public.get_my_membership_entitlement()
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_now timestamptz := now();
  v_membership public.memberships%rowtype;
  v_completed integer := 0;
  v_reserved integer := 0;
  v_active boolean := false;
  v_status text := 'none';
begin
  if v_user is null then raise exception 'Authentication required'; end if;

  select m.* into v_membership
  from public.memberships m
  where m.user_id = v_user;

  if not found then
    return jsonb_build_object(
      'membership_id', null, 'plan_code', 'founder_pass_30d_3runs',
      'active', false, 'status', 'none',
      'starts_at', null, 'expires_at', null,
      'run_quota', 3, 'runs_completed', 0,
      'runs_reserved', 0, 'runs_remaining', 0
    );
  end if;

  select
    count(*) filter (where a.status = 'completed'),
    count(*) filter (
      where a.status = 'reserved' and a.reservation_expires_at > v_now
    )
  into v_completed, v_reserved
  from public.membership_run_accesses a
  where a.membership_id = v_membership.id;

  v_active := v_membership.status = 'active'
    and v_membership.starts_at <= v_now
    and v_membership.expires_at > v_now;
  v_status := case
    when v_membership.status <> 'active' then v_membership.status
    when v_membership.starts_at > v_now then 'pending'
    when v_membership.expires_at <= v_now then 'expired'
    else 'active'
  end;

  return jsonb_build_object(
    'membership_id', v_membership.id,
    'plan_code', v_membership.plan_code,
    'active', v_active,
    'status', v_status,
    'starts_at', v_membership.starts_at,
    'expires_at', v_membership.expires_at,
    'run_quota', v_membership.run_quota,
    'runs_completed', v_completed,
    'runs_reserved', v_reserved,
    'runs_remaining', case
      when v_active then greatest(0, v_membership.run_quota - v_completed - v_reserved)
      else 0
    end
  );
end;
$$;

create or replace function public.create_membership_request(
  p_payment_reference text,
  p_paid_at text default '',
  p_note text default ''
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_reference text := btrim(coalesce(p_payment_reference, ''));
  v_note text := btrim(coalesce(p_note, ''));
  v_request public.membership_requests%rowtype;
  v_request_id uuid := gen_random_uuid();
  v_paid_at timestamptz;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  -- Serialize requests per account so two tabs cannot race past the pending
  -- request check and turn a harmless retry into a unique-constraint error.
  perform pg_advisory_xact_lock(hashtextextended(v_user::text, 0));
  if char_length(v_reference) not between 4 and 128 then
    raise exception 'Payment reference must contain 4 to 128 characters';
  end if;
  if char_length(v_note) > 500 then raise exception 'Note is too long'; end if;
  if btrim(coalesce(p_paid_at, '')) <> '' then
    begin
      if btrim(p_paid_at) ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' then
        v_paid_at := btrim(p_paid_at)::timestamptz;
      else
        v_paid_at := btrim(p_paid_at)::timestamp at time zone 'Asia/Shanghai';
      end if;
    exception when invalid_datetime_format or datetime_field_overflow then
      raise exception 'Invalid payment time';
    end;
  end if;
  if v_paid_at is not null and v_paid_at > now() + interval '10 minutes' then
    raise exception 'Payment time cannot be in the future';
  end if;
  if exists (select 1 from public.memberships m where m.user_id = v_user) then
    return jsonb_build_object('created', false, 'reason', 'membership_exists');
  end if;

  select r.* into v_request
  from public.membership_requests r
  where r.payment_reference = v_reference;
  if found then
    if v_request.user_id <> v_user then
      raise exception 'Payment reference already submitted';
    end if;
    return jsonb_build_object(
      'created', false, 'reason', 'already_submitted',
      'id', v_request.id, 'application_code', v_request.request_code,
      'status', v_request.status,
      'payment_reference', v_request.payment_reference,
      'submitted_at', v_request.created_at,
      'reviewed_at', v_request.reviewed_at
    );
  end if;

  select r.* into v_request
  from public.membership_requests r
  where r.user_id = v_user and r.status = 'pending'
  for update;
  if found then
    return jsonb_build_object(
      'created', false, 'reason', 'pending_request_exists',
      'id', v_request.id, 'application_code', v_request.request_code,
      'status', v_request.status,
      'payment_reference', v_request.payment_reference,
      'submitted_at', v_request.created_at,
      'reviewed_at', v_request.reviewed_at
    );
  end if;

  begin
    insert into public.membership_requests(
      id, request_code, user_id, payment_reference, paid_at, note
    ) values (
      v_request_id,
      'EP-' || upper(substr(replace(v_request_id::text, '-', ''), 1, 12)),
      v_user, v_reference, v_paid_at, v_note
    ) returning * into v_request;
  exception when unique_violation then
    -- A concurrent request can win after the pre-insert checks. Re-read the
    -- durable winner and return the same idempotent contract.
    select r.* into v_request
    from public.membership_requests r
    where r.payment_reference = v_reference
       or (r.user_id = v_user and r.status = 'pending')
    order by (r.payment_reference = v_reference) desc, r.created_at desc
    limit 1;
    if not found then raise; end if;
    if v_request.user_id <> v_user then
      raise exception 'Payment reference already submitted';
    end if;
    return jsonb_build_object(
      'created', false,
      'reason', case
        when v_request.payment_reference = v_reference
          then 'already_submitted'
        else 'pending_request_exists'
      end,
      'id', v_request.id, 'application_code', v_request.request_code,
      'status', v_request.status,
      'payment_reference', v_request.payment_reference,
      'submitted_at', v_request.created_at,
      'reviewed_at', v_request.reviewed_at
    );
  end;

  return jsonb_build_object(
    'created', true, 'reason', 'created',
    'id', v_request.id, 'application_code', v_request.request_code,
    'status', v_request.status,
    'payment_reference', v_request.payment_reference,
    'submitted_at', v_request.created_at,
    'reviewed_at', v_request.reviewed_at
  );
end;
$$;

create or replace function public.approve_membership_request(p_request_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_request public.membership_requests%rowtype;
  v_membership public.memberships%rowtype;
  v_reviewer text := coalesce(auth.uid()::text, auth.role());
begin
  if coalesce(auth.role(), '') <> 'service_role' then
    raise exception 'Service role required';
  end if;
  if p_request_id is null then raise exception 'Request id is required'; end if;

  select r.* into v_request
  from public.membership_requests r
  where r.id = p_request_id
  for update;
  if not found then
    return jsonb_build_object('approved', false, 'reason', 'request_not_found');
  end if;

  if v_request.status = 'approved' then
    return jsonb_build_object(
      'approved', true, 'reason', 'already_approved',
      'request_id', v_request.id, 'request_code', v_request.request_code,
      'membership_id', v_request.membership_id,
      'reviewed_by', v_request.reviewed_by
    );
  end if;
  if v_request.status <> 'pending' then
    return jsonb_build_object(
      'approved', false, 'reason', 'request_not_pending',
      'request_id', v_request.id, 'request_code', v_request.request_code,
      'status', v_request.status
    );
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.user_id = v_request.user_id
  for update;
  if found then
    if v_membership.grant_reference = v_request.payment_reference then
      update public.membership_requests
      set status = 'approved', membership_id = v_membership.id,
          reviewed_at = now(), reviewed_by = v_reviewer
      where id = v_request.id;
      return jsonb_build_object(
        'approved', true, 'reason', 'already_granted',
        'request_id', v_request.id, 'request_code', v_request.request_code,
        'membership_id', v_membership.id,
        'expires_at', v_membership.expires_at,
        'reviewed_by', v_reviewer
      );
    end if;
    return jsonb_build_object(
      'approved', false, 'reason', 'membership_exists',
      'request_id', v_request.id, 'request_code', v_request.request_code,
      'membership_id', v_membership.id
    );
  end if;

  insert into public.memberships(user_id, source, grant_reference)
  values(v_request.user_id, 'manual', v_request.payment_reference)
  returning * into v_membership;

  update public.membership_requests
  set status = 'approved', membership_id = v_membership.id,
      reviewed_at = now(), reviewed_by = v_reviewer
  where id = v_request.id;

  return jsonb_build_object(
    'approved', true, 'reason', 'approved',
    'request_id', v_request.id, 'request_code', v_request.request_code,
    'membership_id', v_membership.id,
    'plan_code', v_membership.plan_code,
    'starts_at', v_membership.starts_at,
    'expires_at', v_membership.expires_at,
    'run_quota', v_membership.run_quota,
    'reviewed_by', v_reviewer
  );
end;
$$;

create or replace function public.reserve_membership_run(
  p_flow_id uuid,
  p_content_hash text,
  p_grading_run_id uuid default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_now timestamptz := now();
  v_membership public.memberships%rowtype;
  v_access public.membership_run_accesses%rowtype;
  v_other_access public.membership_run_accesses%rowtype;
  v_cached_run_id uuid;
  v_run_hash text;
  v_content_hash text := p_content_hash;
  v_has_access boolean := false;
  v_completed_count integer := 0;
  v_active_reserved_count integer := 0;
  v_reconcilable_count integer := 0;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_flow_id is null then raise exception 'Flow id is required'; end if;
  if p_grading_run_id is null
    and (p_content_hash is null or p_content_hash !~ '^[a-f0-9]{64}$') then
    raise exception 'Invalid content hash';
  end if;

  if p_grading_run_id is null then
    select g.id into v_cached_run_id
    from public.grading_runs g
    join public.essays e on e.id = g.essay_id
    where g.user_id = v_user
      and e.content_hash = v_content_hash
      and coalesce(g.draft_role, 'ordinary') <> 'second'
    order by g.created_at desc
    limit 1;
  else
    select e.content_hash into v_run_hash
    from public.grading_runs g
    join public.essays e on e.id = g.essay_id
    where g.id = p_grading_run_id
      and g.user_id = v_user
      and coalesce(g.draft_role, 'ordinary') <> 'second';
    if not found then
      return jsonb_build_object(
        'allowed', false, 'cached', false, 'reason', 'grading_run_not_found'
      );
    end if;
    if p_content_hash ~ '^[a-f0-9]{64}$' and v_run_hash <> p_content_hash then
      raise exception 'Content hash does not match grading run';
    end if;
    v_content_hash := v_run_hash;
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.user_id = v_user
  for update;
  if not found then
    if v_cached_run_id is not null then
      return jsonb_build_object(
        'allowed', false, 'cached', true, 'reason', 'existing_result',
        'existing_run_id', v_cached_run_id, 'grading_run_id', v_cached_run_id,
        'runs_remaining', 0
      );
    end if;
    return jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'membership_required',
      'runs_remaining', 0
    );
  end if;

  -- Reconcile reports that were persisted inside their lease but whose
  -- completion response was lost. Do this before freeing stale leases or
  -- issuing a new slot, and never let reconciliation exceed the hard quota.
  select
    count(*) filter (where a.status = 'completed'),
    count(*) filter (
      where a.status = 'reserved' and a.reservation_expires_at > v_now
    )
  into v_completed_count, v_active_reserved_count
  from public.membership_run_accesses a
  where a.membership_id = v_membership.id;

  select count(*) into v_reconcilable_count
  from public.membership_run_accesses a
  where a.membership_id = v_membership.id
    and a.status = 'reserved'
    and a.reservation_expires_at <= v_now
    and (
      a.grading_run_id is not null
      or exists (
        select 1
        from public.grading_runs g
        join public.essays e on e.id = g.essay_id
        where g.user_id = a.user_id
          and e.user_id = a.user_id
          and e.content_hash = a.content_hash
          and coalesce(g.draft_role, 'ordinary') <> 'second'
          and g.created_at >= a.reserved_at
          and g.created_at <= a.reservation_expires_at
      )
    );

  if v_completed_count + v_active_reserved_count + v_reconcilable_count
      > v_membership.run_quota then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'allowed', false, 'cached', false,
      'reason', 'reconciliation_required'
    );
  end if;

  with resolved as (
    select
      a.id,
      coalesce(
        a.grading_run_id,
        (
          select g.id
          from public.grading_runs g
          join public.essays e on e.id = g.essay_id
          where g.user_id = a.user_id
            and e.user_id = a.user_id
            and e.content_hash = a.content_hash
            and coalesce(g.draft_role, 'ordinary') <> 'second'
            and g.created_at >= a.reserved_at
            and g.created_at <= a.reservation_expires_at
          order by g.created_at, g.id
          limit 1
        )
      ) as grading_run_id
    from public.membership_run_accesses a
    where a.membership_id = v_membership.id
      and a.status = 'reserved'
  )
  update public.membership_run_accesses a
  set grading_run_id = r.grading_run_id,
      status = 'completed', completed_at = v_now, released_at = null
  from resolved r
  where a.id = r.id and r.grading_run_id is not null;

  if v_cached_run_id is not null then
    select a.* into v_other_access
    from public.membership_run_accesses a
    where a.user_id = v_user and a.content_hash = v_content_hash
    for update;
    if found and v_other_access.status = 'completed' then
      return public.get_my_membership_entitlement() || jsonb_build_object(
        'allowed', false, 'cached', true, 'reason', 'existing_run_access',
        'reservation_id', v_other_access.id,
        'run_access_id', v_other_access.id,
        'flow_id', v_other_access.flow_id,
        'grading_run_id', v_other_access.grading_run_id,
        'existing_run_id', v_other_access.grading_run_id,
        'reservation_status', v_other_access.status
      );
    end if;
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'allowed', false, 'cached', true, 'reason', 'existing_result',
      'existing_run_id', v_cached_run_id, 'grading_run_id', v_cached_run_id
    );
  end if;

  if p_grading_run_id is not null then
    select a.* into v_other_access
    from public.membership_run_accesses a
    where a.user_id = v_user and a.grading_run_id = p_grading_run_id
    for update;
    if found and v_other_access.status = 'completed' then
      return public.get_my_membership_entitlement() || jsonb_build_object(
        'allowed', true, 'cached', true, 'reason', 'already_completed',
        'reservation_id', v_other_access.id,
        'run_access_id', v_other_access.id,
        'flow_id', v_other_access.flow_id,
        'grading_run_id', v_other_access.grading_run_id,
        'existing_run_id', v_other_access.grading_run_id,
        'reservation_status', v_other_access.status
      );
    elsif found and v_other_access.status = 'reserved'
      and v_other_access.reservation_expires_at > v_now then
      return public.get_my_membership_entitlement() || jsonb_build_object(
        'allowed', v_other_access.flow_id = p_flow_id,
        'cached', false,
        'reason', case
          when v_other_access.flow_id = p_flow_id then 'already_reserved'
          else 'reservation_conflict'
        end,
        'reservation_id', v_other_access.id,
        'flow_id', v_other_access.flow_id,
        'grading_run_id', p_grading_run_id,
        'reservation_status', v_other_access.status
      );
    end if;
  end if;

  select a.* into v_access
  from public.membership_run_accesses a
  where a.user_id = v_user and a.flow_id = p_flow_id
  for update;
  v_has_access := found;

  if v_has_access and v_access.content_hash <> v_content_hash then
    raise exception 'Flow id already belongs to another essay';
  end if;
  if v_has_access and p_grading_run_id is not null
    and v_access.grading_run_id is not null
    and v_access.grading_run_id <> p_grading_run_id then
    raise exception 'Flow id already belongs to another grading run';
  end if;

  if not v_has_access then
    select a.* into v_access
    from public.membership_run_accesses a
    where a.user_id = v_user and a.content_hash = v_content_hash
    for update;
    v_has_access := found;
    if v_has_access and v_access.status = 'completed' then
      return public.get_my_membership_entitlement() || jsonb_build_object(
        'allowed', false, 'cached', true, 'reason', 'existing_run_access',
        'reservation_id', v_access.id, 'run_access_id', v_access.id,
        'flow_id', v_access.flow_id,
        'grading_run_id', v_access.grading_run_id,
        'existing_run_id', v_access.grading_run_id,
        'reservation_status', v_access.status
      );
    elsif v_has_access and v_access.status = 'reserved'
      and v_access.reservation_expires_at > v_now then
      return public.get_my_membership_entitlement() || jsonb_build_object(
        'allowed', false, 'cached', false, 'reason', 'reservation_conflict',
        'reservation_id', v_access.id, 'flow_id', v_access.flow_id,
        'grading_run_id', v_access.grading_run_id,
        'reservation_status', v_access.status
      );
    end if;
  end if;

  if v_has_access and v_access.status = 'completed' then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'allowed', true, 'cached', true, 'reason', 'already_completed',
      'reservation_id', v_access.id, 'run_access_id', v_access.id,
      'flow_id', v_access.flow_id,
      'grading_run_id', v_access.grading_run_id,
      'existing_run_id', v_access.grading_run_id,
      'reservation_status', v_access.status
    );
  end if;
  if v_has_access and v_access.status = 'reserved'
    and v_access.reservation_expires_at > v_now then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'allowed', true, 'cached', false, 'reason', 'already_reserved',
      'reservation_id', v_access.id, 'flow_id', v_access.flow_id,
      'grading_run_id', v_access.grading_run_id,
      'reservation_status', v_access.status
    );
  end if;

  if v_membership.status <> 'active'
    or v_membership.starts_at > v_now
    or v_membership.expires_at <= v_now then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'membership_inactive'
    );
  end if;

  update public.membership_run_accesses
  set status = 'released', released_at = v_now
  where membership_id = v_membership.id
    and status = 'reserved'
    and reservation_expires_at <= v_now;

  if (
    select count(*)
    from public.membership_run_accesses a
    where a.membership_id = v_membership.id
      and (
        a.status = 'completed'
        or (a.status = 'reserved' and a.reservation_expires_at > v_now)
      )
  ) >= v_membership.run_quota then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'run_quota_exhausted'
    );
  end if;

  if v_has_access then
    update public.membership_run_accesses
    set flow_id = p_flow_id,
        grading_run_id = p_grading_run_id,
        status = 'reserved', reserved_at = v_now,
        reservation_expires_at = v_now + interval '30 minutes',
        completed_at = null, released_at = null
    where id = v_access.id
    returning * into v_access;
  else
    insert into public.membership_run_accesses(
      membership_id, user_id, flow_id, content_hash, grading_run_id,
      status, reserved_at, reservation_expires_at
    ) values (
      v_membership.id, v_user, p_flow_id, v_content_hash, p_grading_run_id,
      'reserved', v_now, v_now + interval '30 minutes'
    ) returning * into v_access;
  end if;

  return public.get_my_membership_entitlement() || jsonb_build_object(
    'allowed', true, 'cached', false, 'reason', 'reserved',
    'reservation_id', v_access.id, 'flow_id', v_access.flow_id,
    'grading_run_id', v_access.grading_run_id,
    'reservation_status', v_access.status
  );
end;
$$;

create or replace function public.complete_membership_run(
  p_flow_id uuid,
  p_grading_run_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_now timestamptz := now();
  v_membership public.memberships%rowtype;
  v_access public.membership_run_accesses%rowtype;
  v_existing public.membership_run_accesses%rowtype;
  v_run_hash text;
  v_run_created_at timestamptz;
  v_reconciled boolean := false;
  v_other_usage integer := 0;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_flow_id is null or p_grading_run_id is null then
    raise exception 'Flow id and grading run id are required';
  end if;

  select m.* into v_membership
  from public.memberships m where m.user_id = v_user for update;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'membership_required');
  end if;

  select a.* into v_access
  from public.membership_run_accesses a
  where a.user_id = v_user and a.flow_id = p_flow_id
  for update;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'reservation_not_found');
  end if;

  if v_access.status = 'completed' then
    if v_access.grading_run_id <> p_grading_run_id then
      raise exception 'Completed flow belongs to another grading run';
    end if;
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'completed', true, 'reason', 'already_completed',
      'run_access_id', v_access.id,
      'grading_run_id', v_access.grading_run_id
    );
  end if;

  select e.content_hash, g.created_at into v_run_hash, v_run_created_at
  from public.grading_runs g
  join public.essays e on e.id = g.essay_id
  where g.id = p_grading_run_id
    and g.user_id = v_user
    and coalesce(g.draft_role, 'ordinary') <> 'second';
  if not found then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'completed', false, 'reason', 'grading_run_not_found'
    );
  end if;
  if v_run_hash <> v_access.content_hash then
    raise exception 'Reserved essay does not match grading run';
  end if;
  if v_access.grading_run_id is not null
    and v_access.grading_run_id <> p_grading_run_id then
    raise exception 'Reserved flow belongs to another grading run';
  end if;
  if v_access.grading_run_id is null and (
    v_run_created_at < v_access.reserved_at
    or v_run_created_at > v_access.reservation_expires_at
  ) then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'completed', false, 'reason', 'grading_run_outside_reservation'
    );
  end if;

  -- Saving the report and settling its reservation are separate HTTP calls.
  -- If the report was durably created for this reservation, a retry must still
  -- charge the slot even after the 30-minute lease expired or was swept.
  if v_access.status = 'released'
    or v_access.reservation_expires_at <= v_now then
    v_reconciled := true;
  end if;

  select a.* into v_existing
  from public.membership_run_accesses a
  where a.user_id = v_user
    and a.grading_run_id = p_grading_run_id
    and a.id <> v_access.id
  for update;
  if found and v_existing.status = 'completed' then
    update public.membership_run_accesses
    set status = 'released', released_at = v_now
    where id = v_access.id;
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'completed', true, 'reason', 'existing_run_access',
      'run_access_id', v_existing.id,
      'grading_run_id', v_existing.grading_run_id
    );
  elsif found then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'completed', false, 'reason', 'reconciliation_required'
    );
  end if;

  select count(*) into v_other_usage
  from public.membership_run_accesses a
  where a.membership_id = v_membership.id
    and a.id <> v_access.id
    and (
      a.status = 'completed'
      or (a.status = 'reserved' and a.reservation_expires_at > v_now)
    );
  if v_other_usage >= v_membership.run_quota then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'completed', false, 'reason', 'reconciliation_required'
    );
  end if;

  update public.membership_run_accesses
  set grading_run_id = p_grading_run_id,
      status = 'completed', completed_at = v_now, released_at = null
  where id = v_access.id
  returning * into v_access;

  return public.get_my_membership_entitlement() || jsonb_build_object(
    'completed', true, 'reason', 'completed',
    'run_access_id', v_access.id,
    'grading_run_id', v_access.grading_run_id,
    'reconciled', v_reconciled
  );
end;
$$;

create or replace function public.release_membership_run(p_flow_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_membership public.memberships%rowtype;
  v_access public.membership_run_accesses%rowtype;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_flow_id is null then raise exception 'Flow id is required'; end if;

  select m.* into v_membership
  from public.memberships m where m.user_id = v_user for update;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'membership_required');
  end if;

  select a.* into v_access
  from public.membership_run_accesses a
  where a.user_id = v_user and a.flow_id = p_flow_id
  for update;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'reservation_not_found');
  end if;
  if v_access.status = 'completed' then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'released', false, 'reason', 'already_completed',
      'run_access_id', v_access.id,
      'grading_run_id', v_access.grading_run_id
    );
  end if;
  if v_access.status = 'released' then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'released', true, 'reason', 'already_released'
    );
  end if;

  update public.membership_run_accesses
  set status = 'released', released_at = now()
  where id = v_access.id;
  return public.get_my_membership_entitlement() || jsonb_build_object(
    'released', true, 'reason', 'released'
  );
end;
$$;

create or replace function public.save_training_practice_attempt(
  p_grading_run_id uuid,
  p_action_id uuid,
  p_flow_id uuid,
  p_task_kind text,
  p_task_key_hash text,
  p_task_index integer,
  p_original_text text,
  p_submitted_text text,
  p_feedback text,
  p_revision_text text,
  p_mastered boolean,
  p_error_tags text[]
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_membership public.memberships%rowtype;
  v_access public.membership_run_accesses%rowtype;
  v_action public.membership_training_actions%rowtype;
  v_attempt public.practice_attempts%rowtype;
  v_action_id uuid := p_action_id;
  v_flow_id uuid := p_flow_id;
  v_owned_run uuid;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_grading_run_id is null then raise exception 'Grading run id is required'; end if;
  if p_task_kind not in ('sentence', 'logic') then raise exception 'Invalid task kind'; end if;
  if p_task_key_hash is null or p_task_key_hash !~ '^[a-f0-9]{64}$' then
    raise exception 'Invalid task key hash';
  end if;
  if p_task_index is null or p_task_index < 1 then raise exception 'Invalid task index'; end if;
  if btrim(coalesce(p_submitted_text, '')) = ''
    or btrim(coalesce(p_feedback, '')) = '' then
    raise exception 'Submitted text and feedback are required';
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.user_id = v_user
  for update;
  if not found then raise exception 'Membership required'; end if;

  select r.* into v_access
  from public.membership_run_accesses r
  where r.user_id = v_user
    and r.membership_id = v_membership.id
    and r.grading_run_id = p_grading_run_id
    and r.status = 'completed'
  for update;
  if not found then raise exception 'Completed run access required'; end if;

  select g.id into v_owned_run
  from public.grading_runs g
  where g.id = p_grading_run_id
    and g.user_id = v_user
    and coalesce(g.draft_role, 'ordinary') <> 'second';
  if not found then raise exception 'Owned first-draft run required'; end if;

  if v_action_id is null or v_flow_id is null then
    select p.training_action_id, p.training_flow_id
    into v_action_id, v_flow_id
    from public.practice_attempts p
    where p.user_id = v_user
      and p.grading_run_id = p_grading_run_id
      and p.task_kind = p_task_kind
      and p.task_key_hash = p_task_key_hash;
  end if;
  if v_action_id is null or v_flow_id is null then
    raise exception 'Training action proof is required';
  end if;

  select a.* into v_action
  from public.membership_training_actions a
  where a.id = v_action_id
    and a.run_access_id = v_access.id
    and a.user_id = v_user
    and a.flow_id = v_flow_id
    and a.task_kind = p_task_kind
    and a.task_key_hash = p_task_key_hash
  for update;
  if not found then raise exception 'Training action does not match this task'; end if;

  select p.* into v_attempt
  from public.practice_attempts p
  where p.user_id = v_user
    and p.grading_run_id = p_grading_run_id
    and p.task_kind = p_task_kind
    and p.task_key_hash = p_task_key_hash
  for update;
  if found and (
    v_attempt.training_action_id is distinct from v_action.id
    or v_attempt.training_flow_id is distinct from v_action.flow_id
  ) then
    raise exception 'Practice attempt belongs to another training action';
  end if;

  insert into public.practice_attempts as existing(
    grading_run_id, user_id, task_kind, task_key_hash, task_index,
    original_text, submitted_text, feedback, revision_text, status, error_tags,
    training_action_id, training_flow_id, feedback_persisted_at
  ) values (
    p_grading_run_id, v_user, p_task_kind, p_task_key_hash, p_task_index,
    p_original_text, p_submitted_text, p_feedback, coalesce(p_revision_text, ''),
    case when coalesce(p_mastered, false) then 'mastered' else 'in_progress' end,
    coalesce(p_error_tags, '{}'::text[]), v_action.id, v_action.flow_id, now()
  )
  on conflict (user_id, grading_run_id, task_kind, task_key_hash)
  do update set
    task_index = excluded.task_index,
    original_text = excluded.original_text,
    submitted_text = excluded.submitted_text,
    feedback = excluded.feedback,
    revision_text = excluded.revision_text,
    status = excluded.status,
    error_tags = excluded.error_tags,
    training_action_id = coalesce(
      existing.training_action_id, excluded.training_action_id
    ),
    training_flow_id = coalesce(
      existing.training_flow_id, excluded.training_flow_id
    ),
    feedback_persisted_at = coalesce(
      existing.feedback_persisted_at, excluded.feedback_persisted_at
    ),
    updated_at = now()
  returning * into v_attempt;

  return to_jsonb(v_attempt);
end;
$$;

create or replace function public.get_membership_run_access(p_grading_run_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_now timestamptz := now();
  v_access public.membership_run_accesses%rowtype;
  v_membership public.memberships%rowtype;
  v_active boolean := false;
  v_training_completed integer := 0;
  v_training_reserved integer := 0;
  v_second_completed boolean := false;
  v_second_reserved boolean := false;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_grading_run_id is null then raise exception 'Grading run id is required'; end if;

  select a.* into v_access
  from public.membership_run_accesses a
  where a.user_id = v_user
    and a.grading_run_id = p_grading_run_id
    and a.status = 'completed';
  if not found then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'allowed', false, 'reason', 'run_access_required',
      'history_readable', false,
      'grading_run_id', p_grading_run_id,
      'training_limit', 3, 'training_completed', 0,
      'training_reserved', 0, 'training_remaining', 0,
      'second_draft_completed', false, 'second_draft_reserved', false
    );
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.id = v_access.membership_id;
  v_active := found
    and v_membership.status = 'active'
    and v_membership.starts_at <= v_now
    and v_membership.expires_at > v_now;

  select
    count(*) filter (
      where a.status = 'completed'
        or exists (
          select 1
          from public.practice_attempts p
          join public.grading_runs g on g.id = p.grading_run_id
          where p.training_action_id = a.id
            and p.training_flow_id = a.flow_id
            and p.user_id = a.user_id
            and p.grading_run_id = v_access.grading_run_id
            and p.task_kind = a.task_kind
            and p.task_key_hash = a.task_key_hash
            and p.feedback_persisted_at is not null
            and btrim(p.feedback) <> ''
            and g.user_id = v_user
            and coalesce(g.draft_role, 'ordinary') <> 'second'
        )
    ),
    count(*) filter (
      where a.status = 'reserved' and a.reservation_expires_at > v_now
        and not exists (
          select 1
          from public.practice_attempts p
          join public.grading_runs g on g.id = p.grading_run_id
          where p.training_action_id = a.id
            and p.training_flow_id = a.flow_id
            and p.user_id = a.user_id
            and p.grading_run_id = v_access.grading_run_id
            and p.task_kind = a.task_kind
            and p.task_key_hash = a.task_key_hash
            and p.feedback_persisted_at is not null
            and btrim(p.feedback) <> ''
            and g.user_id = v_user
            and coalesce(g.draft_role, 'ordinary') <> 'second'
        )
    )
  into v_training_completed, v_training_reserved
  from public.membership_training_actions a
  where a.run_access_id = v_access.id;

  select
    exists (
      select 1 from public.membership_second_draft_actions a
      where a.run_access_id = v_access.id
        and (
          a.status = 'completed'
          or exists (
            select 1
            from public.draft_revisions d
            join public.grading_runs g on g.id = d.revised_grading_run_id
            join public.essays e on e.id = g.essay_id
            where d.user_id = v_user
              and d.grading_run_id = v_access.grading_run_id
              and d.draft_number = 2
              and g.user_id = v_user
              and g.parent_run_id = v_access.grading_run_id
              and g.draft_role = 'second'
              and e.user_id = v_user
              and e.content_hash = a.content_hash
              and g.created_at >= a.reserved_at
              and g.created_at <= a.reservation_expires_at
          )
        )
    ),
    exists (
      select 1 from public.membership_second_draft_actions a
      where a.run_access_id = v_access.id
        and a.status = 'reserved' and a.reservation_expires_at > v_now
        and not exists (
          select 1
          from public.draft_revisions d
          join public.grading_runs g on g.id = d.revised_grading_run_id
          join public.essays e on e.id = g.essay_id
          where d.user_id = v_user
            and d.grading_run_id = v_access.grading_run_id
            and d.draft_number = 2
            and g.user_id = v_user
            and g.parent_run_id = v_access.grading_run_id
            and g.draft_role = 'second'
            and e.user_id = v_user
            and e.content_hash = a.content_hash
            and g.created_at >= a.reserved_at
            and g.created_at <= a.reservation_expires_at
        )
    )
  into v_second_completed, v_second_reserved;

  return public.get_my_membership_entitlement() || jsonb_build_object(
    'allowed', v_active,
    'history_readable', true,
    'reason', case when v_active then 'access_granted' else 'membership_inactive' end,
    'run_access_id', v_access.id,
    'grading_run_id', v_access.grading_run_id,
    'training_limit', 3,
    'training_completed', v_training_completed,
    'training_reserved', v_training_reserved,
    'training_remaining', case
      when v_active
        then greatest(0, 3 - v_training_completed - v_training_reserved)
      else 0
    end,
    'second_draft_completed', v_second_completed,
    'second_draft_reserved', v_second_reserved
  );
end;
$$;

create or replace function public.reserve_training_action(
  p_grading_run_id uuid,
  p_flow_id uuid,
  p_task_kind text,
  p_task_key_hash text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_now timestamptz := now();
  v_access public.membership_run_accesses%rowtype;
  v_membership public.memberships%rowtype;
  v_action public.membership_training_actions%rowtype;
  v_has_action boolean := false;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_grading_run_id is null or p_flow_id is null then
    raise exception 'Grading run id and flow id are required';
  end if;
  if p_task_kind not in ('sentence', 'logic') then raise exception 'Invalid task kind'; end if;
  if p_task_key_hash is null or p_task_key_hash !~ '^[a-f0-9]{64}$' then
    raise exception 'Invalid task key hash';
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.user_id = v_user
  for update;
  if not found then
    return jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'membership_required',
      'training_remaining', 0
    );
  end if;

  select a.* into v_access
  from public.membership_run_accesses a
  where a.user_id = v_user
    and a.membership_id = v_membership.id
    and a.grading_run_id = p_grading_run_id
    and a.status = 'completed'
  for update;
  if not found then
    return jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'run_access_required',
      'training_remaining', 0
    );
  end if;

  select a.* into v_action
  from public.membership_training_actions a
  where a.user_id = v_user and a.flow_id = p_flow_id
  for update;
  v_has_action := found;
  if v_has_action and (
    v_action.run_access_id <> v_access.id
    or v_action.task_kind <> p_task_kind
    or v_action.task_key_hash <> p_task_key_hash
  ) then
    raise exception 'Flow id already belongs to another training task';
  end if;

  if not v_has_action then
    select a.* into v_action
    from public.membership_training_actions a
    where a.run_access_id = v_access.id
      and a.task_kind = p_task_kind
      and a.task_key_hash = p_task_key_hash
    for update;
    v_has_action := found;
  end if;

  -- A confirmed attempt wins over lease expiry.  Reconcile it before any
  -- membership/activity checks so a retry cannot reopen a model-backed task.
  if v_has_action and exists (
    select 1
    from public.practice_attempts p
    join public.grading_runs g on g.id = p.grading_run_id
    where p.training_action_id = v_action.id
      and p.training_flow_id = v_action.flow_id
      and p.user_id = v_user
      and p.grading_run_id = v_access.grading_run_id
      and p.task_kind = v_action.task_kind
      and p.task_key_hash = v_action.task_key_hash
      and p.feedback_persisted_at is not null
      and btrim(p.feedback) <> ''
      and g.user_id = v_user
      and coalesce(g.draft_role, 'ordinary') <> 'second'
  ) then
    update public.membership_training_actions
    set status = 'completed', completed_at = coalesce(completed_at, v_now),
        released_at = null
    where id = v_action.id;
    update public.practice_attempts
    set settled_at = coalesce(settled_at, v_now)
    where training_action_id = v_action.id
      and training_flow_id = v_action.flow_id;
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', false, 'cached', true, 'reason', 'already_completed',
      'action_id', v_action.id
    );
  end if;

  if v_has_action and v_action.status = 'completed' then
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', false, 'cached', true, 'reason', 'already_completed',
      'action_id', v_action.id
    );
  end if;
  if v_has_action and v_action.status = 'reserved'
    and v_action.reservation_expires_at > v_now then
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', v_action.flow_id = p_flow_id,
      'cached', false,
      'reason', case
        when v_action.flow_id = p_flow_id then 'already_reserved'
        else 'reservation_conflict'
      end,
      'action_id', v_action.id
    );
  end if;

  if v_membership.status <> 'active'
    or v_membership.starts_at > v_now
    or v_membership.expires_at <= v_now then
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'membership_inactive'
    );
  end if;

  update public.membership_training_actions
  set status = 'released', released_at = v_now
  where run_access_id = v_access.id
    and status = 'reserved'
    and reservation_expires_at <= v_now
    and not exists (
      select 1
      from public.practice_attempts p
      where p.training_action_id = membership_training_actions.id
        and p.training_flow_id = membership_training_actions.flow_id
        and p.user_id = membership_training_actions.user_id
        and p.grading_run_id = p_grading_run_id
        and p.task_kind = membership_training_actions.task_kind
        and p.task_key_hash = membership_training_actions.task_key_hash
        and p.feedback_persisted_at is not null
        and btrim(p.feedback) <> ''
    );

  if (
    select count(*)
    from public.membership_training_actions a
    where a.run_access_id = v_access.id
      and (
        a.status = 'completed'
        or (a.status = 'reserved' and a.reservation_expires_at > v_now)
        or exists (
          select 1
          from public.practice_attempts p
          join public.grading_runs g on g.id = p.grading_run_id
          where p.training_action_id = a.id
            and p.training_flow_id = a.flow_id
            and p.user_id = a.user_id
            and p.grading_run_id = v_access.grading_run_id
            and p.task_kind = a.task_kind
            and p.task_key_hash = a.task_key_hash
            and p.feedback_persisted_at is not null
            and btrim(p.feedback) <> ''
            and g.user_id = v_user
            and coalesce(g.draft_role, 'ordinary') <> 'second'
        )
      )
  ) >= v_membership.training_actions_per_run then
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'training_limit_reached'
    );
  end if;

  if v_has_action then
    update public.membership_training_actions
    set flow_id = p_flow_id, status = 'reserved', reserved_at = v_now,
        reservation_expires_at = v_now + interval '30 minutes',
        completed_at = null, released_at = null
    where id = v_action.id
    returning * into v_action;
  else
    insert into public.membership_training_actions(
      run_access_id, user_id, flow_id, task_kind, task_key_hash,
      status, reserved_at, reservation_expires_at
    ) values (
      v_access.id, v_user, p_flow_id, p_task_kind, p_task_key_hash,
      'reserved', v_now, v_now + interval '30 minutes'
    ) returning * into v_action;
  end if;

  return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
    'allowed', true, 'cached', false, 'reason', 'reserved',
    'action_id', v_action.id
  );
end;
$$;

create or replace function public.complete_training_action(p_flow_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_now timestamptz := now();
  v_membership public.memberships%rowtype;
  v_action public.membership_training_actions%rowtype;
  v_access public.membership_run_accesses%rowtype;
  v_attempt public.practice_attempts%rowtype;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_flow_id is null then raise exception 'Flow id is required'; end if;

  select m.* into v_membership
  from public.memberships m
  where m.user_id = v_user
  for update;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'membership_required');
  end if;

  select a.* into v_action
  from public.membership_training_actions a
  where a.user_id = v_user and a.flow_id = p_flow_id;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'reservation_not_found');
  end if;
  select r.* into v_access
  from public.membership_run_accesses r
  where r.id = v_action.run_access_id
    and r.user_id = v_user
    and r.membership_id = v_membership.id
  for update;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'run_access_required');
  end if;
  select a.* into v_action
  from public.membership_training_actions a
  where a.id = v_action.id
  for update;

  if v_action.status = 'completed' then
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'completed', true, 'reason', 'already_completed', 'action_id', v_action.id
    );
  end if;

  -- Complete only after the model result is durably bound to this exact
  -- user/run/task/action/flow.  The proof permits safe late settlement after a
  -- timeout, expiry, or an earlier response that never reached the client.
  select p.* into v_attempt
  from public.practice_attempts p
  join public.grading_runs g on g.id = p.grading_run_id
  where p.training_action_id = v_action.id
    and p.training_flow_id = p_flow_id
    and p.user_id = v_user
    and p.grading_run_id = v_access.grading_run_id
    and p.task_kind = v_action.task_kind
    and p.task_key_hash = v_action.task_key_hash
    and p.feedback_persisted_at is not null
    and btrim(p.feedback) <> ''
    and g.user_id = v_user
    and coalesce(g.draft_role, 'ordinary') <> 'second'
  for update of p;
  if found then
    update public.membership_training_actions
    set status = 'completed', completed_at = coalesce(completed_at, v_now),
        released_at = null
    where id = v_action.id;
    update public.practice_attempts
    set settled_at = coalesce(settled_at, v_now)
    where id = v_attempt.id;
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'completed', true,
      'reason', case
        when v_action.status = 'released'
          or v_action.reservation_expires_at <= v_now then 'reconciled'
        else 'completed'
      end,
      'action_id', v_action.id,
      'practice_attempt_id', v_attempt.id
    );
  end if;

  if v_action.status = 'released' then
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'completed', false, 'reason', 'already_released', 'action_id', v_action.id
    );
  end if;
  if v_action.reservation_expires_at <= v_now then
    update public.membership_training_actions
    set status = 'released', released_at = v_now
    where id = v_action.id;
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'completed', false, 'reason', 'reservation_expired', 'action_id', v_action.id
    );
  end if;

  return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
    'completed', false, 'reason', 'practice_attempt_required',
    'action_id', v_action.id
  );
end;
$$;

create or replace function public.release_training_action(p_flow_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_now timestamptz := now();
  v_membership public.memberships%rowtype;
  v_action public.membership_training_actions%rowtype;
  v_access public.membership_run_accesses%rowtype;
  v_attempt public.practice_attempts%rowtype;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_flow_id is null then raise exception 'Flow id is required'; end if;

  select m.* into v_membership
  from public.memberships m
  where m.user_id = v_user
  for update;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'membership_required');
  end if;

  select a.* into v_action
  from public.membership_training_actions a
  where a.user_id = v_user and a.flow_id = p_flow_id;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'reservation_not_found');
  end if;
  select r.* into v_access
  from public.membership_run_accesses r
  where r.id = v_action.run_access_id
    and r.user_id = v_user
    and r.membership_id = v_membership.id
  for update;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'run_access_required');
  end if;
  select a.* into v_action
  from public.membership_training_actions a
  where a.id = v_action.id
  for update;

  if v_action.status = 'completed' then
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'released', false, 'reason', 'already_completed', 'action_id', v_action.id
    );
  end if;

  select p.* into v_attempt
  from public.practice_attempts p
  join public.grading_runs g on g.id = p.grading_run_id
  where p.training_action_id = v_action.id
    and p.training_flow_id = p_flow_id
    and p.user_id = v_user
    and p.grading_run_id = v_access.grading_run_id
    and p.task_kind = v_action.task_kind
    and p.task_key_hash = v_action.task_key_hash
    and p.feedback_persisted_at is not null
    and btrim(p.feedback) <> ''
    and g.user_id = v_user
    and coalesce(g.draft_role, 'ordinary') <> 'second'
  for update of p;
  if found then
    update public.membership_training_actions
    set status = 'completed', completed_at = coalesce(completed_at, v_now),
        released_at = null
    where id = v_action.id;
    update public.practice_attempts
    set settled_at = coalesce(settled_at, v_now)
    where id = v_attempt.id;
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'released', false, 'reason', 'feedback_persisted',
      'action_id', v_action.id, 'practice_attempt_id', v_attempt.id
    );
  end if;
  if v_action.status = 'released' then
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'released', true, 'reason', 'already_released', 'action_id', v_action.id
    );
  end if;

  update public.membership_training_actions
  set status = 'released', released_at = now()
  where id = v_action.id;
  return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
    'released', true, 'reason', 'released', 'action_id', v_action.id
  );
end;
$$;

create or replace function public.reserve_second_draft_action(
  p_grading_run_id uuid,
  p_flow_id uuid,
  p_content_hash text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_now timestamptz := now();
  v_access public.membership_run_accesses%rowtype;
  v_membership public.memberships%rowtype;
  v_action public.membership_second_draft_actions%rowtype;
  v_existing_revision uuid;
  v_existing_revised_run_id uuid;
  v_existing_run_created_at timestamptz;
  v_has_action boolean := false;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_grading_run_id is null or p_flow_id is null then
    raise exception 'Grading run id and flow id are required';
  end if;
  if p_content_hash is null or p_content_hash !~ '^[a-f0-9]{64}$' then
    raise exception 'Invalid content hash';
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.user_id = v_user
  for update;
  if not found then
    return jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'membership_required'
    );
  end if;

  select a.* into v_access
  from public.membership_run_accesses a
  where a.user_id = v_user
    and a.membership_id = v_membership.id
    and a.grading_run_id = p_grading_run_id
    and a.status = 'completed'
  for update;
  if not found then
    return jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'run_access_required'
    );
  end if;

  select d.id, d.revised_grading_run_id
  into v_existing_revision, v_existing_revised_run_id
  from public.draft_revisions d
  where d.user_id = v_user and d.grading_run_id = p_grading_run_id
  order by d.created_at desc limit 1;
  if v_existing_revision is not null then
    select a.* into v_action
    from public.membership_second_draft_actions a
    where a.run_access_id = v_access.id
    for update;
    v_has_action := found;
    if v_has_action
      and v_action.status <> 'completed'
      and v_existing_revised_run_id is not null then
      select g.created_at into v_existing_run_created_at
      from public.grading_runs g
      join public.essays e on e.id = g.essay_id
      where g.id = v_existing_revised_run_id
        and g.user_id = v_user
        and e.user_id = v_user
        and e.content_hash = v_action.content_hash
        and g.parent_run_id = v_access.grading_run_id
        and g.draft_role = 'second';
      if found
        and v_existing_run_created_at >= v_action.reserved_at
        and v_existing_run_created_at <= v_action.reservation_expires_at then
        update public.membership_second_draft_actions
        set status = 'completed', completed_at = v_now, released_at = null,
            revised_grading_run_id = v_existing_revised_run_id
        where id = v_action.id
        returning * into v_action;
      end if;
    end if;
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', false, 'cached', true, 'reason', 'existing_result',
      'existing_revision_id', v_existing_revision,
      'action_id', case when v_has_action then v_action.id else null end,
      'revised_grading_run_id', v_existing_revised_run_id
    );
  end if;

  select a.* into v_action
  from public.membership_second_draft_actions a
  where a.run_access_id = v_access.id
  for update;
  if found and v_action.status = 'completed' then
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', false, 'cached', true, 'reason', 'second_draft_completed',
      'action_id', v_action.id,
      'revised_grading_run_id', v_action.revised_grading_run_id
    );
  elsif found and v_action.status = 'reserved'
    and v_action.reservation_expires_at > v_now then
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', v_action.flow_id = p_flow_id,
      'cached', false,
      'reason', case
        when v_action.flow_id = p_flow_id then 'already_reserved'
        else 'reservation_conflict'
      end,
      'action_id', v_action.id
    );
  end if;

  if v_membership.status <> 'active'
    or v_membership.starts_at > v_now
    or v_membership.expires_at <= v_now then
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'membership_inactive'
    );
  end if;

  if found then
    update public.membership_second_draft_actions
    set flow_id = p_flow_id, content_hash = p_content_hash,
        revised_grading_run_id = null,
        status = 'reserved', reserved_at = v_now,
        reservation_expires_at = v_now + interval '30 minutes',
        completed_at = null, released_at = null
    where id = v_action.id
    returning * into v_action;
  else
    insert into public.membership_second_draft_actions(
      run_access_id, user_id, flow_id, content_hash,
      status, reserved_at, reservation_expires_at
    ) values (
      v_access.id, v_user, p_flow_id, p_content_hash,
      'reserved', v_now, v_now + interval '30 minutes'
    ) returning * into v_action;
  end if;

  return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
    'allowed', true, 'cached', false, 'reason', 'reserved',
    'action_id', v_action.id
  );
end;
$$;

create or replace function public.complete_second_draft_action(
  p_flow_id uuid,
  p_revised_grading_run_id uuid default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_now timestamptz := now();
  v_membership public.memberships%rowtype;
  v_action public.membership_second_draft_actions%rowtype;
  v_access public.membership_run_accesses%rowtype;
  v_revised_created_at timestamptz;
  v_reconciled boolean := false;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_flow_id is null then raise exception 'Flow id is required'; end if;
  if p_revised_grading_run_id is null then
    return jsonb_build_object(
      'completed', false, 'reason', 'revised_grading_run_required'
    );
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.user_id = v_user
  for update;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'membership_required');
  end if;

  select a.* into v_action
  from public.membership_second_draft_actions a
  where a.user_id = v_user and a.flow_id = p_flow_id;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'reservation_not_found');
  end if;
  select r.* into v_access
  from public.membership_run_accesses r
  where r.id = v_action.run_access_id
    and r.user_id = v_user
    and r.membership_id = v_membership.id
  for update;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'run_access_required');
  end if;
  select a.* into v_action
  from public.membership_second_draft_actions a
  where a.id = v_action.id
  for update;

  if v_action.status = 'completed' then
    if v_action.revised_grading_run_id <> p_revised_grading_run_id then
      raise exception 'Completed flow belongs to another revised grading run';
    end if;
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'completed', true, 'reason', 'already_completed',
      'action_id', v_action.id,
      'revised_grading_run_id', v_action.revised_grading_run_id
    );
  end if;

  select g.created_at into v_revised_created_at
  from public.grading_runs g
    join public.essays e on e.id = g.essay_id
  where g.id = p_revised_grading_run_id
    and g.user_id = v_user
    and e.user_id = v_user
    and e.content_hash = v_action.content_hash
    and g.parent_run_id = v_access.grading_run_id
    and g.draft_role = 'second';
  if not found then
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'completed', false, 'reason', 'revised_grading_run_not_found',
      'action_id', v_action.id
    );
  end if;
  if v_revised_created_at < v_action.reserved_at
    or v_revised_created_at > v_action.reservation_expires_at then
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'completed', false, 'reason', 'revised_run_outside_reservation',
      'action_id', v_action.id
    );
  end if;

  if v_action.status = 'released'
    or v_action.reservation_expires_at <= v_now then
    v_reconciled := true;
  end if;

  update public.membership_second_draft_actions
  set status = 'completed', completed_at = v_now,
      revised_grading_run_id = p_revised_grading_run_id,
      released_at = null
  where id = v_action.id;
  return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
    'completed', true, 'reason', 'completed',
    'action_id', v_action.id,
    'revised_grading_run_id', p_revised_grading_run_id,
    'reconciled', v_reconciled
  );
end;
$$;

create or replace function public.release_second_draft_action(p_flow_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_membership public.memberships%rowtype;
  v_action public.membership_second_draft_actions%rowtype;
  v_access public.membership_run_accesses%rowtype;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_flow_id is null then raise exception 'Flow id is required'; end if;

  select m.* into v_membership
  from public.memberships m
  where m.user_id = v_user
  for update;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'membership_required');
  end if;

  select a.* into v_action
  from public.membership_second_draft_actions a
  where a.user_id = v_user and a.flow_id = p_flow_id;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'reservation_not_found');
  end if;
  select r.* into v_access
  from public.membership_run_accesses r
  where r.id = v_action.run_access_id
    and r.user_id = v_user
    and r.membership_id = v_membership.id
  for update;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'run_access_required');
  end if;
  select a.* into v_action
  from public.membership_second_draft_actions a
  where a.id = v_action.id
  for update;

  if v_action.status = 'completed' then
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'released', false, 'reason', 'already_completed', 'action_id', v_action.id
    );
  end if;
  if v_action.status = 'released' then
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'released', true, 'reason', 'already_released', 'action_id', v_action.id
    );
  end if;

  update public.membership_second_draft_actions
  set status = 'released', released_at = now()
  where id = v_action.id;
  return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
    'released', true, 'reason', 'released', 'action_id', v_action.id
  );
end;
$$;

revoke all on function public.get_my_membership_entitlement()
  from public, anon;
revoke all on function public.create_membership_request(text,text,text)
  from public, anon;
revoke all on function public.approve_membership_request(uuid)
  from public, anon, authenticated;
revoke all on function public.reserve_membership_run(uuid,text,uuid)
  from public, anon;
revoke all on function public.complete_membership_run(uuid,uuid)
  from public, anon;
revoke all on function public.release_membership_run(uuid)
  from public, anon;
revoke all on function public.get_membership_run_access(uuid)
  from public, anon;
revoke all on function public.save_training_practice_attempt(uuid,uuid,uuid,text,text,integer,text,text,text,text,boolean,text[])
  from public, anon;
revoke all on function public.reserve_training_action(uuid,uuid,text,text)
  from public, anon;
revoke all on function public.complete_training_action(uuid)
  from public, anon;
revoke all on function public.release_training_action(uuid)
  from public, anon;
revoke all on function public.reserve_second_draft_action(uuid,uuid,text)
  from public, anon;
revoke all on function public.complete_second_draft_action(uuid,uuid)
  from public, anon;
revoke all on function public.release_second_draft_action(uuid)
  from public, anon;

grant execute on function public.get_my_membership_entitlement()
  to authenticated;
grant execute on function public.create_membership_request(text,text,text)
  to authenticated;
grant execute on function public.approve_membership_request(uuid)
  to service_role;
grant execute on function public.reserve_membership_run(uuid,text,uuid)
  to authenticated;
grant execute on function public.complete_membership_run(uuid,uuid)
  to authenticated;
grant execute on function public.release_membership_run(uuid)
  to authenticated;
grant execute on function public.get_membership_run_access(uuid)
  to authenticated;
grant execute on function public.save_training_practice_attempt(uuid,uuid,uuid,text,text,integer,text,text,text,text,boolean,text[])
  to authenticated;
grant execute on function public.reserve_training_action(uuid,uuid,text,text)
  to authenticated;
grant execute on function public.complete_training_action(uuid)
  to authenticated;
grant execute on function public.release_training_action(uuid)
  to authenticated;
grant execute on function public.reserve_second_draft_action(uuid,uuid,text)
  to authenticated;
grant execute on function public.complete_second_draft_action(uuid,uuid)
  to authenticated;
grant execute on function public.release_second_draft_action(uuid)
  to authenticated;

-- End EssayPilot founder membership access.


-- The remainder mirrors the additive renewal-pack migration so a new project
-- initialized from this schema has the same current objects.
-- EssayPilot repeatable manual-review packs.
--
-- The first approved pack costs CNY 7.50. Every later pack costs CNY 9.90.
-- Each approval creates a separate, immutable 30-day membership pack with
-- three essay cycles. Historical runs therefore remain bound to the pack that
-- paid for them, and a later purchase cannot reactivate an expired run.

alter table public.memberships
  drop constraint if exists memberships_user_id_key,
  drop constraint if exists memberships_plan_code_check;

alter table public.memberships
  add constraint memberships_plan_code_check check (
    plan_code in ('founder_pass_30d_3runs', 'renewal_pass_30d_3runs')
  );

create index if not exists memberships_user_pack_order_idx
  on public.memberships(user_id, starts_at desc, created_at desc);

alter table public.membership_requests
  drop constraint if exists membership_requests_plan_code_check,
  drop constraint if exists membership_requests_amount_cny_check,
  drop constraint if exists membership_requests_plan_price_check;

alter table public.membership_requests
  add constraint membership_requests_plan_code_check check (
    plan_code in ('founder_pass_30d_3runs', 'renewal_pass_30d_3runs')
  ),
  add constraint membership_requests_plan_price_check check (
    (plan_code = 'founder_pass_30d_3runs' and amount_cny = 7.50)
    or (plan_code = 'renewal_pass_30d_3runs' and amount_cny = 9.90)
  );

create or replace function public.get_my_membership_entitlement()
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_now timestamptz := now();
  v_membership public.memberships%rowtype;
  v_completed integer := 0;
  v_reserved integer := 0;
  v_purchase_count integer := 0;
  v_active boolean := false;
  v_can_purchase boolean := false;
  v_status text := 'none';
  v_next_plan text := 'founder_pass_30d_3runs';
  v_next_amount numeric(4,2) := 7.50;
begin
  if v_user is null then raise exception 'Authentication required'; end if;

  select count(*) into v_purchase_count
  from public.memberships m
  where m.user_id = v_user;
  if v_purchase_count > 0 then
    v_next_plan := 'renewal_pass_30d_3runs';
    v_next_amount := 9.90;
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.user_id = v_user
  -- The latest pack is authoritative.  In particular, never fall back to an
  -- older exhausted-but-unexpired pack after a newer pack was refunded or
  -- revoked, because that would advertise a renewal the server must reject.
  order by m.starts_at desc, m.created_at desc
  limit 1;

  if not found then
    return jsonb_build_object(
      'membership_id', null, 'plan_code', 'founder_pass_30d_3runs',
      'active', false, 'status', 'none',
      'starts_at', null, 'expires_at', null,
      'run_quota', 3, 'runs_completed', 0,
      'runs_reserved', 0, 'runs_remaining', 0,
      'purchase_count', 0, 'has_previous_purchase', false,
      'next_plan_code', v_next_plan,
      'next_amount_cny', v_next_amount,
      'can_purchase', true
    );
  end if;

  select
    count(*) filter (where a.status = 'completed'),
    count(*) filter (
      where a.status = 'reserved' and a.reservation_expires_at > v_now
    )
  into v_completed, v_reserved
  from public.membership_run_accesses a
  where a.membership_id = v_membership.id;

  v_active := v_membership.status = 'active'
    and v_membership.starts_at <= v_now
    and v_membership.expires_at > v_now;
  v_status := case
    when v_membership.status <> 'active' then v_membership.status
    when v_membership.starts_at > v_now then 'pending'
    when v_membership.expires_at <= v_now then 'expired'
    else 'active'
  end;
  v_can_purchase := v_membership.status = 'active'
    and v_membership.starts_at <= v_now
    and (
      v_membership.expires_at <= v_now
      or (v_completed >= v_membership.run_quota and v_reserved = 0)
    );

  return jsonb_build_object(
    'membership_id', v_membership.id,
    'plan_code', v_membership.plan_code,
    'active', v_active,
    'status', v_status,
    'starts_at', v_membership.starts_at,
    'expires_at', v_membership.expires_at,
    'run_quota', v_membership.run_quota,
    'runs_completed', v_completed,
    'runs_reserved', v_reserved,
    'runs_remaining', case
      when v_active then greatest(
        0, v_membership.run_quota - v_completed - v_reserved
      )
      else 0
    end,
    'purchase_count', v_purchase_count,
    'has_previous_purchase', v_purchase_count > 0,
    'next_plan_code', v_next_plan,
    'next_amount_cny', v_next_amount,
    'can_purchase', v_can_purchase
  );
end;
$$;

create or replace function public.reserve_membership_run(
  p_flow_id uuid,
  p_content_hash text,
  p_grading_run_id uuid default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_now timestamptz := now();
  v_membership public.memberships%rowtype;
  v_access public.membership_run_accesses%rowtype;
  v_other_access public.membership_run_accesses%rowtype;
  v_cached_run_id uuid;
  v_proof_run_id uuid;
  v_run_hash text;
  v_run_created_at timestamptz;
  v_reconcile_result jsonb;
  v_content_hash text := p_content_hash;
  v_has_access boolean := false;
  v_completed_count integer := 0;
  v_active_reserved_count integer := 0;
  v_reconcilable_count integer := 0;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_flow_id is null then raise exception 'Flow id is required'; end if;
  if p_grading_run_id is null
    and (p_content_hash is null or p_content_hash !~ '^[a-f0-9]{64}$') then
    raise exception 'Invalid content hash';
  end if;

  if p_grading_run_id is null then
    select g.id, g.created_at into v_cached_run_id, v_run_created_at
    from public.grading_runs g
    join public.essays e on e.id = g.essay_id
    where g.user_id = v_user
      and e.content_hash = v_content_hash
      and coalesce(g.draft_role, 'ordinary') <> 'second'
    order by g.created_at desc
    limit 1;
  else
    select e.content_hash, g.created_at into v_run_hash, v_run_created_at
    from public.grading_runs g
    join public.essays e on e.id = g.essay_id
    where g.id = p_grading_run_id
      and g.user_id = v_user
      and coalesce(g.draft_role, 'ordinary') <> 'second';
    if not found then
      return jsonb_build_object(
        'allowed', false, 'cached', false, 'reason', 'grading_run_not_found'
      );
    end if;
    if p_content_hash ~ '^[a-f0-9]{64}$' and v_run_hash <> p_content_hash then
      raise exception 'Content hash does not match grading run';
    end if;
    v_content_hash := v_run_hash;
  end if;

  -- A persisted report always settles against the pack that reserved it. A
  -- later pack may reuse only a genuinely released/expired reservation that
  -- has no bound report proof.
  v_proof_run_id := coalesce(p_grading_run_id, v_cached_run_id);
  if v_proof_run_id is not null then
    select a.* into v_other_access
    from public.membership_run_accesses a
    where a.user_id = v_user
      and (
        a.grading_run_id = v_proof_run_id
        or (
          a.grading_run_id is null
          and a.content_hash = v_content_hash
          and v_run_created_at >= a.reserved_at
          and v_run_created_at <= a.reservation_expires_at
        )
      )
    order by
      case when a.status = 'completed' then 0 else 1 end,
      a.created_at desc
    limit 1;
    if found and v_other_access.status = 'completed' then
      return public.get_my_membership_entitlement() || jsonb_build_object(
        'allowed', true, 'cached', true, 'reason', 'already_completed',
        'reservation_id', v_other_access.id,
        'run_access_id', v_other_access.id,
        'flow_id', v_other_access.flow_id,
        'grading_run_id', v_proof_run_id,
        'existing_run_id', v_proof_run_id,
        'reservation_status', v_other_access.status
      );
    elsif found then
      v_reconcile_result := public.complete_membership_run(
        v_other_access.flow_id, v_proof_run_id
      );
      if coalesce((v_reconcile_result ->> 'completed')::boolean, false) then
        return v_reconcile_result || jsonb_build_object(
          'allowed', true, 'cached', true, 'reason', 'already_completed',
          'reservation_id', v_other_access.id,
          'run_access_id', v_other_access.id,
          'flow_id', v_other_access.flow_id,
          'grading_run_id', v_proof_run_id,
          'existing_run_id', v_proof_run_id,
          'reservation_status', 'completed'
        );
      end if;
      return v_reconcile_result || jsonb_build_object(
        'allowed', false, 'cached', true,
        'reason', 'reconciliation_required',
        'reservation_id', v_other_access.id,
        'flow_id', v_other_access.flow_id,
        'grading_run_id', v_proof_run_id,
        'existing_run_id', v_proof_run_id
      );
    end if;
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.user_id = v_user
  order by
    case when m.status = 'active'
      and m.starts_at <= v_now and m.expires_at > v_now then 0 else 1 end,
    m.starts_at desc,
    m.created_at desc
  limit 1
  for update;
  if not found then
    if v_cached_run_id is not null then
      return jsonb_build_object(
        'allowed', false, 'cached', true, 'reason', 'existing_result',
        'existing_run_id', v_cached_run_id, 'grading_run_id', v_cached_run_id,
        'runs_remaining', 0
      );
    end if;
    return jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'membership_required',
      'runs_remaining', 0
    );
  end if;

  -- Reconcile reports that were persisted inside their lease but whose
  -- completion response was lost. Do this before freeing stale leases or
  -- issuing a new slot, and never let reconciliation exceed the hard quota.
  select
    count(*) filter (where a.status = 'completed'),
    count(*) filter (
      where a.status = 'reserved' and a.reservation_expires_at > v_now
    )
  into v_completed_count, v_active_reserved_count
  from public.membership_run_accesses a
  where a.membership_id = v_membership.id;

  select count(*) into v_reconcilable_count
  from public.membership_run_accesses a
  where a.membership_id = v_membership.id
    and a.status = 'reserved'
    and a.reservation_expires_at <= v_now
    and (
      a.grading_run_id is not null
      or exists (
        select 1
        from public.grading_runs g
        join public.essays e on e.id = g.essay_id
        where g.user_id = a.user_id
          and e.user_id = a.user_id
          and e.content_hash = a.content_hash
          and coalesce(g.draft_role, 'ordinary') <> 'second'
          and g.created_at >= a.reserved_at
          and g.created_at <= a.reservation_expires_at
      )
    );

  if v_completed_count + v_active_reserved_count + v_reconcilable_count
      > v_membership.run_quota then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'allowed', false, 'cached', false,
      'reason', 'reconciliation_required'
    );
  end if;

  with resolved as (
    select
      a.id,
      coalesce(
        a.grading_run_id,
        (
          select g.id
          from public.grading_runs g
          join public.essays e on e.id = g.essay_id
          where g.user_id = a.user_id
            and e.user_id = a.user_id
            and e.content_hash = a.content_hash
            and coalesce(g.draft_role, 'ordinary') <> 'second'
            and g.created_at >= a.reserved_at
            and g.created_at <= a.reservation_expires_at
          order by g.created_at, g.id
          limit 1
        )
      ) as grading_run_id
    from public.membership_run_accesses a
    where a.membership_id = v_membership.id
      and a.status = 'reserved'
  )
  update public.membership_run_accesses a
  set grading_run_id = r.grading_run_id,
      status = 'completed', completed_at = v_now, released_at = null
  from resolved r
  where a.id = r.id and r.grading_run_id is not null;

  if v_cached_run_id is not null then
    select a.* into v_other_access
    from public.membership_run_accesses a
    where a.user_id = v_user and a.content_hash = v_content_hash
    for update;
    if found and v_other_access.status = 'completed' then
      return public.get_my_membership_entitlement() || jsonb_build_object(
        'allowed', false, 'cached', true, 'reason', 'existing_run_access',
        'reservation_id', v_other_access.id,
        'run_access_id', v_other_access.id,
        'flow_id', v_other_access.flow_id,
        'grading_run_id', v_other_access.grading_run_id,
        'existing_run_id', v_other_access.grading_run_id,
        'reservation_status', v_other_access.status
      );
    end if;
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'allowed', false, 'cached', true, 'reason', 'existing_result',
      'existing_run_id', v_cached_run_id, 'grading_run_id', v_cached_run_id
    );
  end if;

  if p_grading_run_id is not null then
    select a.* into v_other_access
    from public.membership_run_accesses a
    where a.user_id = v_user and a.grading_run_id = p_grading_run_id
    for update;
    if found and v_other_access.status = 'completed' then
      return public.get_my_membership_entitlement() || jsonb_build_object(
        'allowed', true, 'cached', true, 'reason', 'already_completed',
        'reservation_id', v_other_access.id,
        'run_access_id', v_other_access.id,
        'flow_id', v_other_access.flow_id,
        'grading_run_id', v_other_access.grading_run_id,
        'existing_run_id', v_other_access.grading_run_id,
        'reservation_status', v_other_access.status
      );
    elsif found and v_other_access.status = 'reserved'
      and v_other_access.reservation_expires_at > v_now then
      return public.get_my_membership_entitlement() || jsonb_build_object(
        'allowed', v_other_access.flow_id = p_flow_id,
        'cached', false,
        'reason', case
          when v_other_access.flow_id = p_flow_id then 'already_reserved'
          else 'reservation_conflict'
        end,
        'reservation_id', v_other_access.id,
        'flow_id', v_other_access.flow_id,
        'grading_run_id', p_grading_run_id,
        'reservation_status', v_other_access.status
      );
    end if;
  end if;

  select a.* into v_access
  from public.membership_run_accesses a
  where a.user_id = v_user and a.flow_id = p_flow_id
  for update;
  v_has_access := found;

  if v_has_access and v_access.content_hash <> v_content_hash then
    raise exception 'Flow id already belongs to another essay';
  end if;
  if v_has_access and p_grading_run_id is not null
    and v_access.grading_run_id is not null
    and v_access.grading_run_id <> p_grading_run_id then
    raise exception 'Flow id already belongs to another grading run';
  end if;

  if not v_has_access then
    select a.* into v_access
    from public.membership_run_accesses a
    where a.user_id = v_user and a.content_hash = v_content_hash
    for update;
    v_has_access := found;
    if v_has_access and v_access.status = 'completed' then
      return public.get_my_membership_entitlement() || jsonb_build_object(
        'allowed', false, 'cached', true, 'reason', 'existing_run_access',
        'reservation_id', v_access.id, 'run_access_id', v_access.id,
        'flow_id', v_access.flow_id,
        'grading_run_id', v_access.grading_run_id,
        'existing_run_id', v_access.grading_run_id,
        'reservation_status', v_access.status
      );
    end if;
  end if;

  -- A refund/revocation invalidates an unproved lease immediately. Persisted
  -- reports were reconciled above and remain chargeable to their original
  -- pack; this branch only prevents a new model call after access was stopped.
  if v_has_access and v_access.status = 'reserved'
    and v_access.reservation_expires_at > v_now
    and exists (
      select 1 from public.memberships m
      where m.id = v_access.membership_id
        and m.status in ('revoked', 'refunded')
    ) then
    update public.membership_run_accesses
    set status = 'released', released_at = v_now
    where id = v_access.id
    returning * into v_access;
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'membership_inactive',
      'reservation_id', v_access.id, 'flow_id', v_access.flow_id,
      'reservation_status', v_access.status
    );
  end if;

  if v_has_access and v_access.status = 'completed' then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'allowed', true, 'cached', true, 'reason', 'already_completed',
      'reservation_id', v_access.id, 'run_access_id', v_access.id,
      'flow_id', v_access.flow_id,
      'grading_run_id', v_access.grading_run_id,
      'existing_run_id', v_access.grading_run_id,
      'reservation_status', v_access.status
    );
  end if;
  if v_has_access and v_access.status = 'reserved'
    and v_access.reservation_expires_at > v_now then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'allowed', v_access.flow_id = p_flow_id,
      'cached', false,
      'reason', case
        when v_access.flow_id = p_flow_id then 'already_reserved'
        else 'reservation_conflict'
      end,
      'reservation_id', v_access.id, 'flow_id', v_access.flow_id,
      'grading_run_id', v_access.grading_run_id,
      'reservation_status', v_access.status
    );
  end if;

  if v_membership.status <> 'active'
    or v_membership.starts_at > v_now
    or v_membership.expires_at <= v_now then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'membership_inactive'
    );
  end if;

  update public.membership_run_accesses
  set status = 'released', released_at = v_now
  where membership_id = v_membership.id
    and status = 'reserved'
    and reservation_expires_at <= v_now;

  if (
    select count(*)
    from public.membership_run_accesses a
    where a.membership_id = v_membership.id
      and (
        a.status = 'completed'
        or (a.status = 'reserved' and a.reservation_expires_at > v_now)
      )
  ) >= v_membership.run_quota then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'run_quota_exhausted'
    );
  end if;

  if v_has_access then
    update public.membership_run_accesses
    set membership_id = v_membership.id,
        flow_id = p_flow_id,
        grading_run_id = p_grading_run_id,
        status = 'reserved', reserved_at = v_now,
        reservation_expires_at = v_now + interval '30 minutes',
        completed_at = null, released_at = null
    where id = v_access.id
    returning * into v_access;
  else
    insert into public.membership_run_accesses(
      membership_id, user_id, flow_id, content_hash, grading_run_id,
      status, reserved_at, reservation_expires_at
    ) values (
      v_membership.id, v_user, p_flow_id, v_content_hash, p_grading_run_id,
      'reserved', v_now, v_now + interval '30 minutes'
    ) returning * into v_access;
  end if;

  return public.get_my_membership_entitlement() || jsonb_build_object(
    'allowed', true, 'cached', false, 'reason', 'reserved',
    'reservation_id', v_access.id, 'flow_id', v_access.flow_id,
    'grading_run_id', v_access.grading_run_id,
    'reservation_status', v_access.status
  );
end;
$$;

create or replace function public.complete_membership_run(
  p_flow_id uuid,
  p_grading_run_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_now timestamptz := now();
  v_membership public.memberships%rowtype;
  v_access public.membership_run_accesses%rowtype;
  v_existing public.membership_run_accesses%rowtype;
  v_run_hash text;
  v_run_created_at timestamptz;
  v_reconciled boolean := false;
  v_other_usage integer := 0;
  v_membership_id uuid;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_flow_id is null or p_grading_run_id is null then
    raise exception 'Flow id and grading run id are required';
  end if;

  select a.membership_id into v_membership_id
  from public.membership_run_accesses a
  where a.user_id = v_user and a.flow_id = p_flow_id;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'reservation_not_found');
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.id = v_membership_id and m.user_id = v_user
  for update;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'membership_required');
  end if;

  select a.* into v_access
  from public.membership_run_accesses a
  where a.user_id = v_user
    and a.flow_id = p_flow_id
    and a.membership_id = v_membership.id
  for update;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'reservation_not_found');
  end if;

  if v_access.status = 'completed' then
    if v_access.grading_run_id <> p_grading_run_id then
      raise exception 'Completed flow belongs to another grading run';
    end if;
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'completed', true, 'reason', 'already_completed',
      'run_access_id', v_access.id,
      'grading_run_id', v_access.grading_run_id
    );
  end if;

  select e.content_hash, g.created_at into v_run_hash, v_run_created_at
  from public.grading_runs g
  join public.essays e on e.id = g.essay_id
  where g.id = p_grading_run_id
    and g.user_id = v_user
    and coalesce(g.draft_role, 'ordinary') <> 'second';
  if not found then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'completed', false, 'reason', 'grading_run_not_found'
    );
  end if;
  if v_run_hash <> v_access.content_hash then
    raise exception 'Reserved essay does not match grading run';
  end if;
  if v_access.grading_run_id is not null
    and v_access.grading_run_id <> p_grading_run_id then
    raise exception 'Reserved flow belongs to another grading run';
  end if;
  if v_access.grading_run_id is null and (
    v_run_created_at < v_access.reserved_at
    or v_run_created_at > v_access.reservation_expires_at
  ) then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'completed', false, 'reason', 'grading_run_outside_reservation'
    );
  end if;

  -- Saving the report and settling its reservation are separate HTTP calls.
  -- If the report was durably created for this reservation, a retry must still
  -- charge the slot even after the 30-minute lease expired or was swept.
  if v_access.status = 'released'
    or v_access.reservation_expires_at <= v_now then
    v_reconciled := true;
  end if;

  select a.* into v_existing
  from public.membership_run_accesses a
  where a.user_id = v_user
    and a.grading_run_id = p_grading_run_id
    and a.id <> v_access.id
  for update;
  if found and v_existing.status = 'completed' then
    update public.membership_run_accesses
    set status = 'released', released_at = v_now
    where id = v_access.id;
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'completed', true, 'reason', 'existing_run_access',
      'run_access_id', v_existing.id,
      'grading_run_id', v_existing.grading_run_id
    );
  elsif found then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'completed', false, 'reason', 'reconciliation_required'
    );
  end if;

  select count(*) into v_other_usage
  from public.membership_run_accesses a
  where a.membership_id = v_membership.id
    and a.id <> v_access.id
    and (
      a.status = 'completed'
      or (a.status = 'reserved' and a.reservation_expires_at > v_now)
    );
  if v_other_usage >= v_membership.run_quota then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'completed', false, 'reason', 'reconciliation_required'
    );
  end if;

  update public.membership_run_accesses
  set grading_run_id = p_grading_run_id,
      status = 'completed', completed_at = v_now, released_at = null
  where id = v_access.id
  returning * into v_access;

  return public.get_my_membership_entitlement() || jsonb_build_object(
    'completed', true, 'reason', 'completed',
    'run_access_id', v_access.id,
    'grading_run_id', v_access.grading_run_id,
    'reconciled', v_reconciled
  );
end;
$$;

create or replace function public.release_membership_run(p_flow_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_membership public.memberships%rowtype;
  v_access public.membership_run_accesses%rowtype;
  v_membership_id uuid;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_flow_id is null then raise exception 'Flow id is required'; end if;

  select a.membership_id into v_membership_id
  from public.membership_run_accesses a
  where a.user_id = v_user and a.flow_id = p_flow_id;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'reservation_not_found');
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.id = v_membership_id and m.user_id = v_user
  for update;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'membership_required');
  end if;

  select a.* into v_access
  from public.membership_run_accesses a
  where a.user_id = v_user
    and a.flow_id = p_flow_id
    and a.membership_id = v_membership.id
  for update;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'reservation_not_found');
  end if;
  if v_access.status = 'completed' then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'released', false, 'reason', 'already_completed',
      'run_access_id', v_access.id,
      'grading_run_id', v_access.grading_run_id
    );
  end if;
  if v_access.status = 'released' then
    return public.get_my_membership_entitlement() || jsonb_build_object(
      'released', true, 'reason', 'already_released'
    );
  end if;

  update public.membership_run_accesses
  set status = 'released', released_at = now()
  where id = v_access.id;
  return public.get_my_membership_entitlement() || jsonb_build_object(
    'released', true, 'reason', 'released'
  );
end;
$$;

create or replace function public.save_training_practice_attempt(
  p_grading_run_id uuid,
  p_action_id uuid,
  p_flow_id uuid,
  p_task_kind text,
  p_task_key_hash text,
  p_task_index integer,
  p_original_text text,
  p_submitted_text text,
  p_feedback text,
  p_revision_text text,
  p_mastered boolean,
  p_error_tags text[]
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_membership public.memberships%rowtype;
  v_access public.membership_run_accesses%rowtype;
  v_action public.membership_training_actions%rowtype;
  v_attempt public.practice_attempts%rowtype;
  v_action_id uuid := p_action_id;
  v_flow_id uuid := p_flow_id;
  v_owned_run uuid;
  v_membership_id uuid;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_grading_run_id is null then raise exception 'Grading run id is required'; end if;
  if p_task_kind not in ('sentence', 'logic') then raise exception 'Invalid task kind'; end if;
  if p_task_key_hash is null or p_task_key_hash !~ '^[a-f0-9]{64}$' then
    raise exception 'Invalid task key hash';
  end if;
  if p_task_index is null or p_task_index < 1 then raise exception 'Invalid task index'; end if;
  if btrim(coalesce(p_submitted_text, '')) = ''
    or btrim(coalesce(p_feedback, '')) = '' then
    raise exception 'Submitted text and feedback are required';
  end if;

  select r.membership_id into v_membership_id
  from public.membership_run_accesses r
  where r.user_id = v_user
    and r.grading_run_id = p_grading_run_id
    and r.status = 'completed';
  if not found then raise exception 'Completed run access required'; end if;

  select m.* into v_membership
  from public.memberships m
  where m.id = v_membership_id and m.user_id = v_user
  for update;
  if not found then raise exception 'Membership required'; end if;

  select r.* into v_access
  from public.membership_run_accesses r
  where r.user_id = v_user
    and r.membership_id = v_membership.id
    and r.grading_run_id = p_grading_run_id
    and r.status = 'completed'
  for update;
  if not found then raise exception 'Completed run access required'; end if;

  select g.id into v_owned_run
  from public.grading_runs g
  where g.id = p_grading_run_id
    and g.user_id = v_user
    and coalesce(g.draft_role, 'ordinary') <> 'second';
  if not found then raise exception 'Owned first-draft run required'; end if;

  if v_action_id is null or v_flow_id is null then
    select p.training_action_id, p.training_flow_id
    into v_action_id, v_flow_id
    from public.practice_attempts p
    where p.user_id = v_user
      and p.grading_run_id = p_grading_run_id
      and p.task_kind = p_task_kind
      and p.task_key_hash = p_task_key_hash;
  end if;
  if v_action_id is null or v_flow_id is null then
    raise exception 'Training action proof is required';
  end if;

  select a.* into v_action
  from public.membership_training_actions a
  where a.id = v_action_id
    and a.run_access_id = v_access.id
    and a.user_id = v_user
    and a.flow_id = v_flow_id
    and a.task_kind = p_task_kind
    and a.task_key_hash = p_task_key_hash
  for update;
  if not found then raise exception 'Training action does not match this task'; end if;

  select p.* into v_attempt
  from public.practice_attempts p
  where p.user_id = v_user
    and p.grading_run_id = p_grading_run_id
    and p.task_kind = p_task_kind
    and p.task_key_hash = p_task_key_hash
  for update;
  if found and (
    v_attempt.training_action_id is distinct from v_action.id
    or v_attempt.training_flow_id is distinct from v_action.flow_id
  ) then
    raise exception 'Practice attempt belongs to another training action';
  end if;

  insert into public.practice_attempts as existing(
    grading_run_id, user_id, task_kind, task_key_hash, task_index,
    original_text, submitted_text, feedback, revision_text, status, error_tags,
    training_action_id, training_flow_id, feedback_persisted_at
  ) values (
    p_grading_run_id, v_user, p_task_kind, p_task_key_hash, p_task_index,
    p_original_text, p_submitted_text, p_feedback, coalesce(p_revision_text, ''),
    case when coalesce(p_mastered, false) then 'mastered' else 'in_progress' end,
    coalesce(p_error_tags, '{}'::text[]), v_action.id, v_action.flow_id, now()
  )
  on conflict (user_id, grading_run_id, task_kind, task_key_hash)
  do update set
    task_index = excluded.task_index,
    original_text = excluded.original_text,
    submitted_text = excluded.submitted_text,
    feedback = excluded.feedback,
    revision_text = excluded.revision_text,
    status = excluded.status,
    error_tags = excluded.error_tags,
    training_action_id = coalesce(
      existing.training_action_id, excluded.training_action_id
    ),
    training_flow_id = coalesce(
      existing.training_flow_id, excluded.training_flow_id
    ),
    feedback_persisted_at = coalesce(
      existing.feedback_persisted_at, excluded.feedback_persisted_at
    ),
    updated_at = now()
  returning * into v_attempt;

  return to_jsonb(v_attempt);
end;
$$;

create or replace function public.reserve_training_action(
  p_grading_run_id uuid,
  p_flow_id uuid,
  p_task_kind text,
  p_task_key_hash text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_now timestamptz := now();
  v_access public.membership_run_accesses%rowtype;
  v_membership public.memberships%rowtype;
  v_action public.membership_training_actions%rowtype;
  v_has_action boolean := false;
  v_membership_id uuid;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_grading_run_id is null or p_flow_id is null then
    raise exception 'Grading run id and flow id are required';
  end if;
  if p_task_kind not in ('sentence', 'logic') then raise exception 'Invalid task kind'; end if;
  if p_task_key_hash is null or p_task_key_hash !~ '^[a-f0-9]{64}$' then
    raise exception 'Invalid task key hash';
  end if;

  select a.membership_id into v_membership_id
  from public.membership_run_accesses a
  where a.user_id = v_user
    and a.grading_run_id = p_grading_run_id
    and a.status = 'completed';
  if not found then
    return jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'run_access_required',
      'training_remaining', 0
    );
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.id = v_membership_id and m.user_id = v_user
  for update;
  if not found then
    return jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'membership_required',
      'training_remaining', 0
    );
  end if;

  select a.* into v_access
  from public.membership_run_accesses a
  where a.user_id = v_user
    and a.membership_id = v_membership.id
    and a.grading_run_id = p_grading_run_id
    and a.status = 'completed'
  for update;
  if not found then
    return jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'run_access_required',
      'training_remaining', 0
    );
  end if;

  select a.* into v_action
  from public.membership_training_actions a
  where a.user_id = v_user and a.flow_id = p_flow_id
  for update;
  v_has_action := found;
  if v_has_action and (
    v_action.run_access_id <> v_access.id
    or v_action.task_kind <> p_task_kind
    or v_action.task_key_hash <> p_task_key_hash
  ) then
    raise exception 'Flow id already belongs to another training task';
  end if;

  if not v_has_action then
    select a.* into v_action
    from public.membership_training_actions a
    where a.run_access_id = v_access.id
      and a.task_kind = p_task_kind
      and a.task_key_hash = p_task_key_hash
    for update;
    v_has_action := found;
  end if;

  -- A confirmed attempt wins over lease expiry.  Reconcile it before any
  -- membership/activity checks so a retry cannot reopen a model-backed task.
  if v_has_action and exists (
    select 1
    from public.practice_attempts p
    join public.grading_runs g on g.id = p.grading_run_id
    where p.training_action_id = v_action.id
      and p.training_flow_id = v_action.flow_id
      and p.user_id = v_user
      and p.grading_run_id = v_access.grading_run_id
      and p.task_kind = v_action.task_kind
      and p.task_key_hash = v_action.task_key_hash
      and p.feedback_persisted_at is not null
      and btrim(p.feedback) <> ''
      and g.user_id = v_user
      and coalesce(g.draft_role, 'ordinary') <> 'second'
  ) then
    update public.membership_training_actions
    set status = 'completed', completed_at = coalesce(completed_at, v_now),
        released_at = null
    where id = v_action.id;
    update public.practice_attempts
    set settled_at = coalesce(settled_at, v_now)
    where training_action_id = v_action.id
      and training_flow_id = v_action.flow_id;
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', false, 'cached', true, 'reason', 'already_completed',
      'action_id', v_action.id
    );
  end if;

  if v_has_action and v_action.status = 'completed' then
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', false, 'cached', true, 'reason', 'already_completed',
      'action_id', v_action.id
    );
  end if;
  if v_has_action and v_action.status = 'reserved'
    and v_action.reservation_expires_at > v_now
    and v_membership.status in ('revoked', 'refunded') then
    update public.membership_training_actions
    set status = 'released', released_at = v_now
    where id = v_action.id
    returning * into v_action;
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'membership_inactive',
      'action_id', v_action.id
    );
  end if;
  if v_has_action and v_action.status = 'reserved'
    and v_action.reservation_expires_at > v_now then
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', v_action.flow_id = p_flow_id,
      'cached', false,
      'reason', case
        when v_action.flow_id = p_flow_id then 'already_reserved'
        else 'reservation_conflict'
      end,
      'action_id', v_action.id
    );
  end if;

  if v_membership.status <> 'active'
    or v_membership.starts_at > v_now
    or v_membership.expires_at <= v_now then
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'membership_inactive'
    );
  end if;

  update public.membership_training_actions
  set status = 'released', released_at = v_now
  where run_access_id = v_access.id
    and status = 'reserved'
    and reservation_expires_at <= v_now
    and not exists (
      select 1
      from public.practice_attempts p
      where p.training_action_id = membership_training_actions.id
        and p.training_flow_id = membership_training_actions.flow_id
        and p.user_id = membership_training_actions.user_id
        and p.grading_run_id = p_grading_run_id
        and p.task_kind = membership_training_actions.task_kind
        and p.task_key_hash = membership_training_actions.task_key_hash
        and p.feedback_persisted_at is not null
        and btrim(p.feedback) <> ''
    );

  if (
    select count(*)
    from public.membership_training_actions a
    where a.run_access_id = v_access.id
      and (
        a.status = 'completed'
        or (a.status = 'reserved' and a.reservation_expires_at > v_now)
        or exists (
          select 1
          from public.practice_attempts p
          join public.grading_runs g on g.id = p.grading_run_id
          where p.training_action_id = a.id
            and p.training_flow_id = a.flow_id
            and p.user_id = a.user_id
            and p.grading_run_id = v_access.grading_run_id
            and p.task_kind = a.task_kind
            and p.task_key_hash = a.task_key_hash
            and p.feedback_persisted_at is not null
            and btrim(p.feedback) <> ''
            and g.user_id = v_user
            and coalesce(g.draft_role, 'ordinary') <> 'second'
        )
      )
  ) >= v_membership.training_actions_per_run then
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'training_limit_reached'
    );
  end if;

  if v_has_action then
    update public.membership_training_actions
    set flow_id = p_flow_id, status = 'reserved', reserved_at = v_now,
        reservation_expires_at = v_now + interval '30 minutes',
        completed_at = null, released_at = null
    where id = v_action.id
    returning * into v_action;
  else
    insert into public.membership_training_actions(
      run_access_id, user_id, flow_id, task_kind, task_key_hash,
      status, reserved_at, reservation_expires_at
    ) values (
      v_access.id, v_user, p_flow_id, p_task_kind, p_task_key_hash,
      'reserved', v_now, v_now + interval '30 minutes'
    ) returning * into v_action;
  end if;

  return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
    'allowed', true, 'cached', false, 'reason', 'reserved',
    'action_id', v_action.id
  );
end;
$$;

create or replace function public.complete_training_action(p_flow_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_now timestamptz := now();
  v_membership public.memberships%rowtype;
  v_action public.membership_training_actions%rowtype;
  v_access public.membership_run_accesses%rowtype;
  v_membership_id uuid;
  v_attempt public.practice_attempts%rowtype;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_flow_id is null then raise exception 'Flow id is required'; end if;

  select r.membership_id into v_membership_id
  from public.membership_training_actions a
  join public.membership_run_accesses r on r.id = a.run_access_id
  where a.user_id = v_user
    and a.flow_id = p_flow_id
    and r.user_id = v_user;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'reservation_not_found');
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.id = v_membership_id and m.user_id = v_user
  for update;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'membership_required');
  end if;

  select a.* into v_action
  from public.membership_training_actions a
  where a.user_id = v_user and a.flow_id = p_flow_id;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'reservation_not_found');
  end if;
  select r.* into v_access
  from public.membership_run_accesses r
  where r.id = v_action.run_access_id
    and r.user_id = v_user
    and r.membership_id = v_membership.id
  for update;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'run_access_required');
  end if;
  select a.* into v_action
  from public.membership_training_actions a
  where a.id = v_action.id
  for update;

  if v_action.status = 'completed' then
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'completed', true, 'reason', 'already_completed', 'action_id', v_action.id
    );
  end if;

  -- Complete only after the model result is durably bound to this exact
  -- user/run/task/action/flow.  The proof permits safe late settlement after a
  -- timeout, expiry, or an earlier response that never reached the client.
  select p.* into v_attempt
  from public.practice_attempts p
  join public.grading_runs g on g.id = p.grading_run_id
  where p.training_action_id = v_action.id
    and p.training_flow_id = p_flow_id
    and p.user_id = v_user
    and p.grading_run_id = v_access.grading_run_id
    and p.task_kind = v_action.task_kind
    and p.task_key_hash = v_action.task_key_hash
    and p.feedback_persisted_at is not null
    and btrim(p.feedback) <> ''
    and g.user_id = v_user
    and coalesce(g.draft_role, 'ordinary') <> 'second'
  for update of p;
  if found then
    update public.membership_training_actions
    set status = 'completed', completed_at = coalesce(completed_at, v_now),
        released_at = null
    where id = v_action.id;
    update public.practice_attempts
    set settled_at = coalesce(settled_at, v_now)
    where id = v_attempt.id;
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'completed', true,
      'reason', case
        when v_action.status = 'released'
          or v_action.reservation_expires_at <= v_now then 'reconciled'
        else 'completed'
      end,
      'action_id', v_action.id,
      'practice_attempt_id', v_attempt.id
    );
  end if;

  if v_action.status = 'released' then
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'completed', false, 'reason', 'already_released', 'action_id', v_action.id
    );
  end if;
  if v_action.reservation_expires_at <= v_now then
    update public.membership_training_actions
    set status = 'released', released_at = v_now
    where id = v_action.id;
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'completed', false, 'reason', 'reservation_expired', 'action_id', v_action.id
    );
  end if;

  return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
    'completed', false, 'reason', 'practice_attempt_required',
    'action_id', v_action.id
  );
end;
$$;

create or replace function public.release_training_action(p_flow_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_now timestamptz := now();
  v_membership public.memberships%rowtype;
  v_action public.membership_training_actions%rowtype;
  v_access public.membership_run_accesses%rowtype;
  v_membership_id uuid;
  v_attempt public.practice_attempts%rowtype;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_flow_id is null then raise exception 'Flow id is required'; end if;

  select r.membership_id into v_membership_id
  from public.membership_training_actions a
  join public.membership_run_accesses r on r.id = a.run_access_id
  where a.user_id = v_user
    and a.flow_id = p_flow_id
    and r.user_id = v_user;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'reservation_not_found');
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.id = v_membership_id and m.user_id = v_user
  for update;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'membership_required');
  end if;

  select a.* into v_action
  from public.membership_training_actions a
  where a.user_id = v_user and a.flow_id = p_flow_id;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'reservation_not_found');
  end if;
  select r.* into v_access
  from public.membership_run_accesses r
  where r.id = v_action.run_access_id
    and r.user_id = v_user
    and r.membership_id = v_membership.id
  for update;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'run_access_required');
  end if;
  select a.* into v_action
  from public.membership_training_actions a
  where a.id = v_action.id
  for update;

  if v_action.status = 'completed' then
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'released', false, 'reason', 'already_completed', 'action_id', v_action.id
    );
  end if;

  select p.* into v_attempt
  from public.practice_attempts p
  join public.grading_runs g on g.id = p.grading_run_id
  where p.training_action_id = v_action.id
    and p.training_flow_id = p_flow_id
    and p.user_id = v_user
    and p.grading_run_id = v_access.grading_run_id
    and p.task_kind = v_action.task_kind
    and p.task_key_hash = v_action.task_key_hash
    and p.feedback_persisted_at is not null
    and btrim(p.feedback) <> ''
    and g.user_id = v_user
    and coalesce(g.draft_role, 'ordinary') <> 'second'
  for update of p;
  if found then
    update public.membership_training_actions
    set status = 'completed', completed_at = coalesce(completed_at, v_now),
        released_at = null
    where id = v_action.id;
    update public.practice_attempts
    set settled_at = coalesce(settled_at, v_now)
    where id = v_attempt.id;
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'released', false, 'reason', 'feedback_persisted',
      'action_id', v_action.id, 'practice_attempt_id', v_attempt.id
    );
  end if;
  if v_action.status = 'released' then
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'released', true, 'reason', 'already_released', 'action_id', v_action.id
    );
  end if;

  update public.membership_training_actions
  set status = 'released', released_at = now()
  where id = v_action.id;
  return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
    'released', true, 'reason', 'released', 'action_id', v_action.id
  );
end;
$$;

create or replace function public.reserve_second_draft_action(
  p_grading_run_id uuid,
  p_flow_id uuid,
  p_content_hash text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_now timestamptz := now();
  v_access public.membership_run_accesses%rowtype;
  v_membership public.memberships%rowtype;
  v_action public.membership_second_draft_actions%rowtype;
  v_existing_revision uuid;
  v_existing_revised_run_id uuid;
  v_existing_run_created_at timestamptz;
  v_has_action boolean := false;
  v_membership_id uuid;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_grading_run_id is null or p_flow_id is null then
    raise exception 'Grading run id and flow id are required';
  end if;
  if p_content_hash is null or p_content_hash !~ '^[a-f0-9]{64}$' then
    raise exception 'Invalid content hash';
  end if;

  select a.membership_id into v_membership_id
  from public.membership_run_accesses a
  where a.user_id = v_user
    and a.grading_run_id = p_grading_run_id
    and a.status = 'completed';
  if not found then
    return jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'run_access_required'
    );
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.id = v_membership_id and m.user_id = v_user
  for update;
  if not found then
    return jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'membership_required'
    );
  end if;

  select a.* into v_access
  from public.membership_run_accesses a
  where a.user_id = v_user
    and a.membership_id = v_membership.id
    and a.grading_run_id = p_grading_run_id
    and a.status = 'completed'
  for update;
  if not found then
    return jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'run_access_required'
    );
  end if;

  select d.id, d.revised_grading_run_id
  into v_existing_revision, v_existing_revised_run_id
  from public.draft_revisions d
  where d.user_id = v_user and d.grading_run_id = p_grading_run_id
  order by d.created_at desc limit 1;
  if v_existing_revision is not null then
    select a.* into v_action
    from public.membership_second_draft_actions a
    where a.run_access_id = v_access.id
    for update;
    v_has_action := found;
    if v_has_action
      and v_action.status <> 'completed'
      and v_existing_revised_run_id is not null then
      select g.created_at into v_existing_run_created_at
      from public.grading_runs g
      join public.essays e on e.id = g.essay_id
      where g.id = v_existing_revised_run_id
        and g.user_id = v_user
        and e.user_id = v_user
        and e.content_hash = v_action.content_hash
        and g.parent_run_id = v_access.grading_run_id
        and g.draft_role = 'second';
      if found
        and v_existing_run_created_at >= v_action.reserved_at
        and v_existing_run_created_at <= v_action.reservation_expires_at then
        update public.membership_second_draft_actions
        set status = 'completed', completed_at = v_now, released_at = null,
            revised_grading_run_id = v_existing_revised_run_id
        where id = v_action.id
        returning * into v_action;
      end if;
    end if;
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', false, 'cached', true, 'reason', 'existing_result',
      'existing_revision_id', v_existing_revision,
      'action_id', case when v_has_action then v_action.id else null end,
      'revised_grading_run_id', v_existing_revised_run_id
    );
  end if;

  select a.* into v_action
  from public.membership_second_draft_actions a
  where a.run_access_id = v_access.id
  for update;
  if found and v_action.status = 'completed' then
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', false, 'cached', true, 'reason', 'second_draft_completed',
      'action_id', v_action.id,
      'revised_grading_run_id', v_action.revised_grading_run_id
    );
  elsif found and v_action.status = 'reserved'
    and v_action.reservation_expires_at > v_now
    and v_membership.status in ('revoked', 'refunded') then
    update public.membership_second_draft_actions
    set status = 'released', released_at = v_now
    where id = v_action.id
    returning * into v_action;
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'membership_inactive',
      'action_id', v_action.id
    );
  elsif found and v_action.status = 'reserved'
    and v_action.reservation_expires_at > v_now then
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', v_action.flow_id = p_flow_id,
      'cached', false,
      'reason', case
        when v_action.flow_id = p_flow_id then 'already_reserved'
        else 'reservation_conflict'
      end,
      'action_id', v_action.id
    );
  end if;

  if v_membership.status <> 'active'
    or v_membership.starts_at > v_now
    or v_membership.expires_at <= v_now then
    return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
      'allowed', false, 'cached', false, 'reason', 'membership_inactive'
    );
  end if;

  if found then
    update public.membership_second_draft_actions
    set flow_id = p_flow_id, content_hash = p_content_hash,
        revised_grading_run_id = null,
        status = 'reserved', reserved_at = v_now,
        reservation_expires_at = v_now + interval '30 minutes',
        completed_at = null, released_at = null
    where id = v_action.id
    returning * into v_action;
  else
    insert into public.membership_second_draft_actions(
      run_access_id, user_id, flow_id, content_hash,
      status, reserved_at, reservation_expires_at
    ) values (
      v_access.id, v_user, p_flow_id, p_content_hash,
      'reserved', v_now, v_now + interval '30 minutes'
    ) returning * into v_action;
  end if;

  return public.get_membership_run_access(p_grading_run_id) || jsonb_build_object(
    'allowed', true, 'cached', false, 'reason', 'reserved',
    'action_id', v_action.id
  );
end;
$$;

create or replace function public.complete_second_draft_action(
  p_flow_id uuid,
  p_revised_grading_run_id uuid default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_now timestamptz := now();
  v_membership public.memberships%rowtype;
  v_action public.membership_second_draft_actions%rowtype;
  v_access public.membership_run_accesses%rowtype;
  v_membership_id uuid;
  v_revised_created_at timestamptz;
  v_reconciled boolean := false;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_flow_id is null then raise exception 'Flow id is required'; end if;
  if p_revised_grading_run_id is null then
    return jsonb_build_object(
      'completed', false, 'reason', 'revised_grading_run_required'
    );
  end if;

  select r.membership_id into v_membership_id
  from public.membership_second_draft_actions a
  join public.membership_run_accesses r on r.id = a.run_access_id
  where a.user_id = v_user
    and a.flow_id = p_flow_id
    and r.user_id = v_user;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'reservation_not_found');
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.id = v_membership_id and m.user_id = v_user
  for update;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'membership_required');
  end if;

  select a.* into v_action
  from public.membership_second_draft_actions a
  where a.user_id = v_user and a.flow_id = p_flow_id;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'reservation_not_found');
  end if;
  select r.* into v_access
  from public.membership_run_accesses r
  where r.id = v_action.run_access_id
    and r.user_id = v_user
    and r.membership_id = v_membership.id
  for update;
  if not found then
    return jsonb_build_object('completed', false, 'reason', 'run_access_required');
  end if;
  select a.* into v_action
  from public.membership_second_draft_actions a
  where a.id = v_action.id
  for update;

  if v_action.status = 'completed' then
    if v_action.revised_grading_run_id <> p_revised_grading_run_id then
      raise exception 'Completed flow belongs to another revised grading run';
    end if;
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'completed', true, 'reason', 'already_completed',
      'action_id', v_action.id,
      'revised_grading_run_id', v_action.revised_grading_run_id
    );
  end if;

  select g.created_at into v_revised_created_at
  from public.grading_runs g
    join public.essays e on e.id = g.essay_id
  where g.id = p_revised_grading_run_id
    and g.user_id = v_user
    and e.user_id = v_user
    and e.content_hash = v_action.content_hash
    and g.parent_run_id = v_access.grading_run_id
    and g.draft_role = 'second';
  if not found then
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'completed', false, 'reason', 'revised_grading_run_not_found',
      'action_id', v_action.id
    );
  end if;
  if v_revised_created_at < v_action.reserved_at
    or v_revised_created_at > v_action.reservation_expires_at then
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'completed', false, 'reason', 'revised_run_outside_reservation',
      'action_id', v_action.id
    );
  end if;

  if v_action.status = 'released'
    or v_action.reservation_expires_at <= v_now then
    v_reconciled := true;
  end if;

  update public.membership_second_draft_actions
  set status = 'completed', completed_at = v_now,
      revised_grading_run_id = p_revised_grading_run_id,
      released_at = null
  where id = v_action.id;
  return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
    'completed', true, 'reason', 'completed',
    'action_id', v_action.id,
    'revised_grading_run_id', p_revised_grading_run_id,
    'reconciled', v_reconciled
  );
end;
$$;

create or replace function public.release_second_draft_action(p_flow_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_membership public.memberships%rowtype;
  v_action public.membership_second_draft_actions%rowtype;
  v_access public.membership_run_accesses%rowtype;
  v_membership_id uuid;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  if p_flow_id is null then raise exception 'Flow id is required'; end if;

  select r.membership_id into v_membership_id
  from public.membership_second_draft_actions a
  join public.membership_run_accesses r on r.id = a.run_access_id
  where a.user_id = v_user
    and a.flow_id = p_flow_id
    and r.user_id = v_user;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'reservation_not_found');
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.id = v_membership_id and m.user_id = v_user
  for update;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'membership_required');
  end if;

  select a.* into v_action
  from public.membership_second_draft_actions a
  where a.user_id = v_user and a.flow_id = p_flow_id;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'reservation_not_found');
  end if;
  select r.* into v_access
  from public.membership_run_accesses r
  where r.id = v_action.run_access_id
    and r.user_id = v_user
    and r.membership_id = v_membership.id
  for update;
  if not found then
    return jsonb_build_object('released', false, 'reason', 'run_access_required');
  end if;
  select a.* into v_action
  from public.membership_second_draft_actions a
  where a.id = v_action.id
  for update;

  if v_action.status = 'completed' then
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'released', false, 'reason', 'already_completed', 'action_id', v_action.id
    );
  end if;
  if v_action.status = 'released' then
    return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
      'released', true, 'reason', 'already_released', 'action_id', v_action.id
    );
  end if;

  update public.membership_second_draft_actions
  set status = 'released', released_at = now()
  where id = v_action.id;
  return public.get_membership_run_access(v_access.grading_run_id) || jsonb_build_object(
    'released', true, 'reason', 'released', 'action_id', v_action.id
  );
end;
$$;

create or replace function public.create_membership_request(
  p_payment_reference text,
  p_paid_at text default '',
  p_note text default ''
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user uuid := auth.uid();
  v_now timestamptz := now();
  v_reference text := btrim(coalesce(p_payment_reference, ''));
  v_note text := btrim(coalesce(p_note, ''));
  v_request public.membership_requests%rowtype;
  v_membership public.memberships%rowtype;
  v_request_id uuid := gen_random_uuid();
  v_paid_at timestamptz;
  v_purchase_count integer := 0;
  v_completed integer := 0;
  v_reserved integer := 0;
  v_plan text := 'founder_pass_30d_3runs';
  v_amount numeric(4,2) := 7.50;
begin
  if v_user is null then raise exception 'Authentication required'; end if;
  perform pg_advisory_xact_lock(hashtextextended(v_user::text, 0));
  if char_length(v_reference) not between 4 and 128 then
    raise exception 'Payment reference must contain 4 to 128 characters';
  end if;
  if char_length(v_note) > 500 then raise exception 'Note is too long'; end if;
  if btrim(coalesce(p_paid_at, '')) <> '' then
    begin
      if btrim(p_paid_at) ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' then
        v_paid_at := btrim(p_paid_at)::timestamptz;
      else
        v_paid_at := btrim(p_paid_at)::timestamp at time zone 'Asia/Shanghai';
      end if;
    exception when invalid_datetime_format or datetime_field_overflow then
      raise exception 'Invalid payment time';
    end;
  end if;
  if v_paid_at is not null and v_paid_at > v_now + interval '10 minutes' then
    raise exception 'Payment time cannot be in the future';
  end if;

  select r.* into v_request
  from public.membership_requests r
  where r.payment_reference = v_reference;
  if found then
    if v_request.user_id <> v_user then
      raise exception 'Payment reference already submitted';
    end if;
    return jsonb_build_object(
      'created', false, 'reason', 'already_submitted',
      'id', v_request.id, 'application_code', v_request.request_code,
      'status', v_request.status,
      'plan_code', v_request.plan_code,
      'amount_cny', v_request.amount_cny,
      'currency', v_request.currency,
      'payment_reference', v_request.payment_reference,
      'submitted_at', v_request.created_at,
      'reviewed_at', v_request.reviewed_at
    );
  end if;

  select r.* into v_request
  from public.membership_requests r
  where r.user_id = v_user and r.status = 'pending'
  for update;
  if found then
    return jsonb_build_object(
      'created', false, 'reason', 'pending_request_exists',
      'id', v_request.id, 'application_code', v_request.request_code,
      'status', v_request.status,
      'plan_code', v_request.plan_code,
      'amount_cny', v_request.amount_cny,
      'currency', v_request.currency,
      'payment_reference', v_request.payment_reference,
      'submitted_at', v_request.created_at,
      'reviewed_at', v_request.reviewed_at
    );
  end if;

  select count(*) into v_purchase_count
  from public.memberships m
  where m.user_id = v_user;
  if v_purchase_count > 0 then
    v_plan := 'renewal_pass_30d_3runs';
    v_amount := 9.90;
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.user_id = v_user
  order by m.starts_at desc, m.created_at desc
  limit 1
  for update;
  if found and v_membership.status in ('revoked', 'refunded') then
    return jsonb_build_object(
      'created', false, 'reason', 'purchase_not_allowed',
      'plan_code', v_plan, 'amount_cny', v_amount, 'currency', 'CNY'
    );
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.user_id = v_user
    and m.status = 'active'
    and m.starts_at <= v_now
    and m.expires_at > v_now
  order by m.starts_at desc, m.created_at desc
  limit 1
  for update;
  if found then
    select
      count(*) filter (where a.status = 'completed'),
      count(*) filter (
        where a.status = 'reserved' and a.reservation_expires_at > v_now
      )
    into v_completed, v_reserved
    from public.membership_run_accesses a
    where a.membership_id = v_membership.id;
    if v_completed < v_membership.run_quota or v_reserved > 0 then
      return jsonb_build_object(
        'created', false, 'reason', 'active_membership',
        'plan_code', v_plan, 'amount_cny', v_amount, 'currency', 'CNY',
        'runs_remaining', greatest(
          0, v_membership.run_quota - v_completed - v_reserved
        )
      );
    end if;
  end if;

  begin
    insert into public.membership_requests(
      id, request_code, user_id, plan_code, amount_cny,
      payment_reference, paid_at, note
    ) values (
      v_request_id,
      'EP-' || upper(substr(replace(v_request_id::text, '-', ''), 1, 12)),
      v_user, v_plan, v_amount, v_reference, v_paid_at, v_note
    ) returning * into v_request;
  exception when unique_violation then
    select r.* into v_request
    from public.membership_requests r
    where r.payment_reference = v_reference
       or (r.user_id = v_user and r.status = 'pending')
    order by (r.payment_reference = v_reference) desc, r.created_at desc
    limit 1;
    if not found then raise; end if;
    if v_request.user_id <> v_user then
      raise exception 'Payment reference already submitted';
    end if;
    return jsonb_build_object(
      'created', false,
      'reason', case
        when v_request.payment_reference = v_reference
          then 'already_submitted'
        else 'pending_request_exists'
      end,
      'id', v_request.id, 'application_code', v_request.request_code,
      'status', v_request.status,
      'plan_code', v_request.plan_code,
      'amount_cny', v_request.amount_cny,
      'currency', v_request.currency,
      'payment_reference', v_request.payment_reference,
      'submitted_at', v_request.created_at,
      'reviewed_at', v_request.reviewed_at
    );
  end;

  return jsonb_build_object(
    'created', true, 'reason', 'created',
    'id', v_request.id, 'application_code', v_request.request_code,
    'status', v_request.status,
    'plan_code', v_request.plan_code,
    'amount_cny', v_request.amount_cny,
    'currency', v_request.currency,
    'payment_reference', v_request.payment_reference,
    'submitted_at', v_request.created_at,
    'reviewed_at', v_request.reviewed_at
  );
end;
$$;

create or replace function public.approve_membership_request(p_request_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_now timestamptz := now();
  v_request public.membership_requests%rowtype;
  v_membership public.memberships%rowtype;
  v_existing public.memberships%rowtype;
  v_reviewer text := coalesce(auth.uid()::text, auth.role());
  v_purchase_count integer := 0;
  v_completed integer := 0;
  v_reserved integer := 0;
  v_expected_plan text;
  v_expected_amount numeric(4,2);
begin
  if coalesce(auth.role(), '') <> 'service_role' then
    raise exception 'Service role required';
  end if;
  if p_request_id is null then raise exception 'Request id is required'; end if;

  -- Read the owner first, then take the same per-user advisory lock used by
  -- request creation.  Locking the request row first would invert that order
  -- and could deadlock with a simultaneous create request.
  select r.* into v_request
  from public.membership_requests r
  where r.id = p_request_id;
  if not found then
    return jsonb_build_object('approved', false, 'reason', 'request_not_found');
  end if;

  perform pg_advisory_xact_lock(
    hashtextextended(v_request.user_id::text, 0)
  );
  select r.* into v_request
  from public.membership_requests r
  where r.id = p_request_id
  for update;
  if not found then
    return jsonb_build_object('approved', false, 'reason', 'request_not_found');
  end if;
  if v_request.status = 'approved' then
    return jsonb_build_object(
      'approved', true, 'reason', 'already_approved',
      'request_id', v_request.id, 'request_code', v_request.request_code,
      'membership_id', v_request.membership_id,
      'plan_code', v_request.plan_code,
      'amount_cny', v_request.amount_cny,
      'reviewed_by', v_request.reviewed_by
    );
  end if;
  if v_request.status <> 'pending' then
    return jsonb_build_object(
      'approved', false, 'reason', 'request_not_pending',
      'request_id', v_request.id, 'request_code', v_request.request_code,
      'status', v_request.status
    );
  end if;

  select m.* into v_existing
  from public.memberships m
  where m.grant_reference = v_request.payment_reference
  for update;
  if found then
    if v_existing.user_id <> v_request.user_id then
      raise exception 'Payment reference belongs to another user';
    end if;
    update public.membership_requests
    set status = 'approved', membership_id = v_existing.id,
        reviewed_at = v_now, reviewed_by = v_reviewer
    where id = v_request.id;
    return jsonb_build_object(
      'approved', true, 'reason', 'already_granted',
      'request_id', v_request.id, 'request_code', v_request.request_code,
      'membership_id', v_existing.id,
      'plan_code', v_request.plan_code,
      'amount_cny', v_request.amount_cny,
      'expires_at', v_existing.expires_at,
      'reviewed_by', v_reviewer
    );
  end if;

  select count(*) into v_purchase_count
  from public.memberships m
  where m.user_id = v_request.user_id;
  if v_purchase_count = 0 then
    v_expected_plan := 'founder_pass_30d_3runs';
    v_expected_amount := 7.50;
  else
    v_expected_plan := 'renewal_pass_30d_3runs';
    v_expected_amount := 9.90;
  end if;
  if v_request.plan_code <> v_expected_plan
    or v_request.amount_cny <> v_expected_amount then
    return jsonb_build_object(
      'approved', false, 'reason', 'request_plan_mismatch',
      'request_id', v_request.id, 'request_code', v_request.request_code
    );
  end if;

  select m.* into v_existing
  from public.memberships m
  where m.user_id = v_request.user_id
  order by m.starts_at desc, m.created_at desc
  limit 1
  for update;
  if found and v_existing.status in ('revoked', 'refunded') then
    return jsonb_build_object(
      'approved', false, 'reason', 'purchase_not_allowed',
      'request_id', v_request.id, 'request_code', v_request.request_code
    );
  end if;

  select m.* into v_existing
  from public.memberships m
  where m.user_id = v_request.user_id
    and m.status = 'active'
    and m.starts_at <= v_now
    and m.expires_at > v_now
  order by m.starts_at desc, m.created_at desc
  limit 1
  for update;
  if found then
    select
      count(*) filter (where a.status = 'completed'),
      count(*) filter (
        where a.status = 'reserved' and a.reservation_expires_at > v_now
      )
    into v_completed, v_reserved
    from public.membership_run_accesses a
    where a.membership_id = v_existing.id;
    if v_completed < v_existing.run_quota or v_reserved > 0 then
      return jsonb_build_object(
        'approved', false, 'reason', 'active_membership',
        'request_id', v_request.id, 'request_code', v_request.request_code,
        'runs_remaining', greatest(
          0, v_existing.run_quota - v_completed - v_reserved
        )
      );
    end if;
  end if;

  insert into public.memberships(
    user_id, plan_code, source, grant_reference, starts_at, expires_at
  ) values (
    v_request.user_id, v_request.plan_code, 'manual',
    v_request.payment_reference, v_now, v_now + interval '30 days'
  ) returning * into v_membership;

  update public.membership_requests
  set status = 'approved', membership_id = v_membership.id,
      reviewed_at = v_now, reviewed_by = v_reviewer
  where id = v_request.id;

  return jsonb_build_object(
    'approved', true, 'reason', 'approved',
    'request_id', v_request.id, 'request_code', v_request.request_code,
    'membership_id', v_membership.id,
    'plan_code', v_membership.plan_code,
    'amount_cny', v_request.amount_cny,
    'starts_at', v_membership.starts_at,
    'expires_at', v_membership.expires_at,
    'run_quota', v_membership.run_quota,
    'reviewed_by', v_reviewer
  );
end;
$$;

revoke all on function public.get_my_membership_entitlement()
  from public, anon, authenticated;
revoke all on function public.create_membership_request(text,text,text)
  from public, anon, authenticated;
revoke all on function public.approve_membership_request(uuid)
  from public, anon, authenticated;

grant execute on function public.get_my_membership_entitlement()
  to authenticated;
grant execute on function public.create_membership_request(text,text,text)
  to authenticated;
grant execute on function public.approve_membership_request(uuid)
  to service_role;

-- End EssayPilot repeatable manual-review packs.


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
