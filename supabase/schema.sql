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
  created_at timestamptz not null default now()
);

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
  user_id uuid not null references auth.users(id) on delete cascade,
  draft_number integer not null check (draft_number >= 2),
  content text not null,
  score_snapshot jsonb not null,
  report_json jsonb not null default '{}'::jsonb,
  report_markdown text not null default '',
  progress_report text not null,
  created_at timestamptz not null default now()
);

alter table public.essays enable row level security;
alter table public.grading_runs enable row level security;
alter table public.practice_attempts enable row level security;
alter table public.draft_revisions enable row level security;

create or replace function public.set_updated_at() returns trigger
language plpgsql as $$ begin new.updated_at = now(); return new; end; $$;
drop trigger if exists essays_set_updated_at on public.essays;
create trigger essays_set_updated_at before update on public.essays
for each row execute function public.set_updated_at();
drop trigger if exists practice_set_updated_at on public.practice_attempts;
create trigger practice_set_updated_at before update on public.practice_attempts
for each row execute function public.set_updated_at();

drop policy if exists "owners manage essays" on public.essays;
create policy "owners manage essays" on public.essays for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
drop policy if exists "owners manage grading runs" on public.grading_runs;
create policy "owners manage grading runs" on public.grading_runs for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
drop policy if exists "owners manage practice" on public.practice_attempts;
create policy "owners manage practice" on public.practice_attempts for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
drop policy if exists "owners manage revisions" on public.draft_revisions;
create policy "owners manage revisions" on public.draft_revisions for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

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
  select e.id, g.id into v_essay, v_run
  from essays e join grading_runs g on g.essay_id = e.id
  where e.user_id = v_user and e.content_hash = p_content_hash
  order by g.created_at desc limit 1;
  if v_run is not null then
    return jsonb_build_object('essay_id', v_essay, 'grading_run_id', v_run, 'reused', true);
  end if;
  insert into essays(user_id, task_type, question, content, content_hash, word_count)
  values(v_user, 'Task 2', p_question, p_essay, p_content_hash, p_word_count)
  returning id into v_essay;
  insert into grading_runs(essay_id, user_id, overall_band, criteria, report_json, report_markdown, model, prompt_version, skill_version)
  values(v_essay, v_user, p_overall_band, p_criteria, p_report_json, p_report_markdown, p_model, p_prompt_version, p_skill_version)
  returning id into v_run;
  return jsonb_build_object('essay_id', v_essay, 'grading_run_id', v_run);
end;
$$;

grant execute on function public.save_grading_cycle(text,text,integer,text,numeric,jsonb,jsonb,text,text,text,text) to authenticated;
grant select, insert, update, delete on public.essays, public.grading_runs, public.practice_attempts, public.draft_revisions to authenticated;
