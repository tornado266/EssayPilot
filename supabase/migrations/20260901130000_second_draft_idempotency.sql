-- One logical Draft 2 result is persisted once per original grading run.
-- Nullable keys let existing rows remain intact while all new writes gain a
-- database-enforced idempotency identity.
alter table public.grading_runs
  add column if not exists idempotency_key text;
alter table public.draft_revisions
  add column if not exists idempotency_key text;

create unique index if not exists grading_runs_user_idempotency_key_idx
  on public.grading_runs(user_id, idempotency_key);
create unique index if not exists draft_revisions_user_idempotency_key_idx
  on public.draft_revisions(user_id, idempotency_key);

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

  -- Locking the owner-scoped parent serializes concurrent retries for this
  -- essay even when they arrive before either request receives its response.
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
    update public.grading_runs
    set idempotency_key = v_run_key
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

  update public.grading_runs
  set draft_role = 'first'
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

-- Rollback: drop save_second_draft_result and the two idempotency indexes.
-- Keep nullable idempotency_key columns until no deployed client references them.
