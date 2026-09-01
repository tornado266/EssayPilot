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
