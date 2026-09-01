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
