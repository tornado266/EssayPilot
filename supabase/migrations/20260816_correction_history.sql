-- Linked correction history without duplicating second-draft reports.
alter table public.grading_runs
  add column if not exists draft_role text not null default 'ordinary';
alter table public.grading_runs
  add column if not exists parent_run_id uuid references public.grading_runs(id) on delete set null;
alter table public.grading_runs drop constraint if exists grading_runs_draft_role_check;
alter table public.grading_runs
  add constraint grading_runs_draft_role_check
  check (draft_role in ('ordinary', 'first', 'second'));

alter table public.draft_revisions
  add column if not exists revised_grading_run_id uuid references public.grading_runs(id) on delete set null;

create index if not exists grading_runs_user_created_idx
  on public.grading_runs(user_id, created_at desc);
create index if not exists grading_runs_parent_idx
  on public.grading_runs(user_id, parent_run_id) where parent_run_id is not null;

create or replace function public.save_linked_grading_cycle(
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
  p_skill_version text,
  p_parent_run_id uuid,
  p_draft_role text
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
  if p_draft_role not in ('first', 'second') then raise exception 'Invalid draft role'; end if;
  if not exists (
    select 1 from grading_runs where id = p_parent_run_id and user_id = v_user
  ) then raise exception 'Parent grading run not found'; end if;

  select id into v_essay from essays
  where user_id = v_user and content_hash = p_content_hash limit 1;
  if v_essay is null then
    insert into essays(user_id, task_type, question, content, content_hash, word_count)
    values(v_user, 'Task 2', p_question, p_essay, p_content_hash, p_word_count)
    returning id into v_essay;
  end if;

  select id into v_run from grading_runs
  where user_id = v_user and essay_id = v_essay
    and prompt_version = p_prompt_version
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

grant execute on function public.save_linked_grading_cycle(
  text,text,integer,text,numeric,jsonb,jsonb,text,text,text,text,uuid,text
) to authenticated;

-- Rollback: drop the function/indexes, then drop revised_grading_run_id,
-- parent_run_id, and draft_role after confirming no new linked rows are needed.
