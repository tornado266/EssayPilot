-- EssayPilot topic expression library. Safe to run more than once.
alter table public.learning_items alter column grading_run_id drop not null;
alter table public.learning_items add column if not exists origin text not null default 'report';
alter table public.learning_items add column if not exists topic_category text not null default 'society_family';
alter table public.learning_items add column if not exists function_category text not null default 'core_collocation';
alter table public.learning_items add column if not exists usage_note text not null default '';
alter table public.learning_items add column if not exists favorite boolean not null default false;

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

alter table public.expression_attempts enable row level security;
drop policy if exists "owners manage expression attempts" on public.expression_attempts;
create policy "owners manage expression attempts" on public.expression_attempts
for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

grant select, insert, update, delete on public.expression_attempts to authenticated;
