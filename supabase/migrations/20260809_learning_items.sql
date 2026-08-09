-- EssayPilot Task 2 learning assets migration.
-- Run once in the Supabase SQL Editor after the original schema.sql.

create table if not exists public.learning_items (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    grading_run_id uuid not null references public.grading_runs(id) on delete cascade,
    item_key text not null,
    item_type text not null check (item_type in ('error', 'expression')),
    category text not null,
    source_text text not null default '',
    target_text text not null default '',
    explanation text not null default '',
    status text not null default 'new' check (status in ('new', 'practicing', 'mastered')),
    review_count integer not null default 0 check (review_count >= 0),
    last_reviewed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, item_key)
);

alter table public.learning_items enable row level security;

drop trigger if exists learning_items_set_updated_at on public.learning_items;
create trigger learning_items_set_updated_at
before update on public.learning_items
for each row execute function public.set_updated_at();

drop policy if exists learning_items_owner_all on public.learning_items;
create policy learning_items_owner_all on public.learning_items
for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

grant select, insert, update, delete on public.learning_items to authenticated;
