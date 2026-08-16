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
  created_at timestamptz not null default now()
);
alter table public.grading_runs add column if not exists draft_role text not null default 'ordinary';
alter table public.grading_runs add column if not exists parent_run_id uuid references public.grading_runs(id) on delete set null;

create table if not exists public.practice_attempts (
  id uuid primary key default gen_random_uuid(),
  grading_run_id uuid not null references public.grading_runs(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  task_kind text not null check (task_kind in ('sentence', 'logic')),
  task_index integer not null,
  original_text text not null,
  submitted_text text not null,
  feedback text not null default '',
  revision_text text not null default '',
  status text not null default 'in_progress' check (status in ('in_progress', 'mastered')),
  error_tags text[] not null default '{}',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index if not exists practice_task_once_idx
on public.practice_attempts(user_id, grading_run_id, task_kind, task_index);

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
  created_at timestamptz not null default now()
);
alter table public.draft_revisions add column if not exists revised_grading_run_id uuid references public.grading_runs(id) on delete set null;

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

-- Guest trial and product-funnel objects are maintained in
-- migrations/20260812_feedback_loop_funnel.sql so existing deployments can
-- apply the change without recreating private learning tables.
