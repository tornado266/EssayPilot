begin;

create extension if not exists pgtap with schema extensions;
set local search_path = public, extensions;

select plan(23);

select has_table('public', 'analytics_events', 'analytics event table exists');
select has_table('public', 'product_feedback', 'structured feedback table exists');
select ok(
  (select relrowsecurity from pg_class where oid = 'public.analytics_events'::regclass),
  'analytics events have RLS enabled'
);
select ok(
  (select relrowsecurity from pg_class where oid = 'public.product_feedback'::regclass),
  'product feedback has RLS enabled'
);
select ok(not has_table_privilege('anon', 'public.analytics_events', 'select'), 'anon cannot read events');
select ok(not has_table_privilege('authenticated', 'public.analytics_events', 'select'), 'authenticated cannot read events');
select ok(not has_table_privilege('anon', 'public.product_feedback', 'select'), 'anon cannot read feedback');
select ok(has_function_privilege(
  'anon',
  'public.record_analytics_event_v2(uuid,uuid,uuid,uuid,text,jsonb,text,text)',
  'execute'
), 'anon can use the narrow event writer');
select ok(has_function_privilege(
  'authenticated',
  'public.record_product_feedback(uuid,uuid,uuid,uuid,text,boolean,text[],text,text)',
  'execute'
), 'authenticated can use the narrow feedback writer');
select ok(has_function_privilege(
  'service_role',
  'public.get_analytics_dashboard_v2(timestamptz,timestamptz)',
  'execute'
), 'service role can read aggregates');
select ok(not has_function_privilege(
  'anon',
  'public.get_analytics_dashboard_v2(timestamptz,timestamptz)',
  'execute'
), 'anon cannot read aggregates');

insert into auth.users(id, aud, role, email, created_at, updated_at)
values (
  '10000000-0000-0000-0000-000000000001',
  'authenticated',
  'authenticated',
  'analytics-test@example.invalid',
  now() - interval '30 minutes',
  now() - interval '30 minutes'
);

insert into public.analytics_events(
  event_id, user_id, session_id, attempt_id, event_name,
  occurred_at, metadata_json, dedupe_key
) values
  ('20000000-0000-0000-0000-000000000001', 'anon_' || repeat('a', 64), '30000000-0000-0000-0000-000000000001', null, 'session_started', now() - interval '50 minutes', '{"identity_type":"anonymous"}', repeat('1', 64)),
  ('20000000-0000-0000-0000-000000000002', 'anon_' || repeat('a', 64), '30000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001', 'first_draft_submitted', now() - interval '45 minutes', '{"identity_type":"anonymous"}', repeat('2', 64)),
  ('20000000-0000-0000-0000-000000000003', 'anon_' || repeat('a', 64), '30000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001', 'report_generated', now() - interval '40 minutes', '{"identity_type":"anonymous","duration_ms":1200}', repeat('3', 64)),
  ('20000000-0000-0000-0000-000000000004', 'anon_' || repeat('a', 64), '30000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001', 'report_viewed', now() - interval '35 minutes', '{"identity_type":"anonymous"}', repeat('4', 64));

insert into public.product_feedback(
  feedback_id, user_id, session_id, attempt_id, touchpoint,
  helpful, reason_codes, occurred_at, dedupe_key
) values (
  '50000000-0000-0000-0000-000000000001',
  'anon_' || repeat('a', 64),
  '30000000-0000-0000-0000-000000000001',
  '40000000-0000-0000-0000-000000000001',
  'report', false, array['unclear'], now() - interval '30 minutes', repeat('5', 64)
);

select cmp_ok(
  (public.get_analytics_dashboard_v2(now() - interval '2 hours', now()) -> 'summary' ->> 'registered_users_new')::integer,
  '>=', 1,
  'registered account aggregation includes the new auth account'
);
select is(
  (public.get_analytics_dashboard_v2(now() - interval '2 hours', now()) -> 'experience_funnel' -> 0 ->> 'users')::integer,
  1,
  'experience funnel counts the anchored visit'
);
select is(
  (public.get_analytics_dashboard_v2(now() - interval '2 hours', now()) -> 'experience_funnel' -> 1 ->> 'users')::integer,
  1,
  'experience funnel links the same-session submission'
);
select is(
  (public.get_analytics_dashboard_v2(now() - interval '2 hours', now()) -> 'experience_funnel' -> 2 ->> 'users')::integer,
  1,
  'experience funnel links generation by attempt id'
);
select is(
  (public.get_analytics_dashboard_v2(now() - interval '2 hours', now()) -> 'experience_funnel' -> 3 ->> 'users')::integer,
  1,
  'experience funnel links report view by attempt id'
);
select is(
  (public.get_analytics_dashboard_v2(now() - interval '2 hours', now()) -> 'guest_report_login' ->> 'eligible_users')::integer,
  1,
  'guest report login denominator uses event-time identity'
);
select is(
  (public.get_analytics_dashboard_v2(now() - interval '2 hours', now()) -> 'guest_report_login' ->> 'converted_users')::integer,
  0,
  'guest report login conversion is initially zero'
);
select is(
  (public.get_analytics_dashboard_v2(now() - interval '2 hours', now()) -> 'feedback' -> 0 ->> 'responses')::integer,
  1,
  'feedback aggregate counts the response'
);
select is(
  jsonb_array_length(public.get_analytics_dashboard_v2(now() - interval '2 hours', now()) -> 'feedback' -> 0 -> 'reason_counts'),
  0,
  'feedback reason distribution is hidden below five samples'
);

select set_config('request.jwt.claim.sub', '10000000-0000-0000-0000-000000000001', true);
select ok(public.record_analytics_event_v2(
  '20000000-0000-0000-0000-000000000005',
  '30000000-0000-0000-0000-000000000001',
  '40000000-0000-0000-0000-000000000001',
  null,
  'login_completed',
  '{"identity_type":"authenticated"}'::jsonb,
  repeat('f', 64),
  'anon_' || repeat('a', 64)
), 'login event is accepted and triggers anonymous identity merge');
select is(
  (select count(*)::integer from public.analytics_events where user_id = 'anon_' || repeat('a', 64)),
  0,
  'anonymous event rows are merged after login'
);
select is(
  (select count(*)::integer from public.analytics_events where user_id = '10000000-0000-0000-0000-000000000001'),
  5,
  'merged event rows belong to the authenticated pseudonymous id'
);

select * from finish();
rollback;
