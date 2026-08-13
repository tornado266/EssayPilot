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

