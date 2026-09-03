-- EssayPilot automatic membership activation.
-- A submitted payment reference is treated as sufficient confirmation. The
-- request row and its 30-day, three-essay membership are created atomically.

create or replace function public.auto_activate_membership_request()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_now timestamptz := now();
  v_membership public.memberships%rowtype;
begin
  if new.status <> 'pending' or new.membership_id is not null then
    return new;
  end if;

  select m.* into v_membership
  from public.memberships m
  where m.grant_reference = new.payment_reference
  for update;

  if found then
    if v_membership.user_id <> new.user_id then
      raise exception 'Payment reference belongs to another user';
    end if;
  else
    insert into public.memberships(
      user_id, plan_code, source, grant_reference, starts_at, expires_at
    ) values (
      new.user_id, new.plan_code, 'manual', new.payment_reference,
      v_now, v_now + interval '30 days'
    ) returning * into v_membership;
  end if;

  new.status := 'approved';
  new.membership_id := v_membership.id;
  new.reviewed_at := v_now;
  new.reviewed_by := 'automatic';
  return new;
end;
$$;

revoke all on function public.auto_activate_membership_request()
  from public, anon, authenticated;

drop trigger if exists membership_requests_auto_activate
  on public.membership_requests;
create trigger membership_requests_auto_activate
before insert or update on public.membership_requests
for each row execute function public.auto_activate_membership_request();

-- Activate requests that were still waiting when this migration was applied.
update public.membership_requests
set updated_at = now()
where status = 'pending';
