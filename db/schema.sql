
create table if not exists stores (
    id          uuid primary key default gen_random_uuid(),
    name        text not null unique
);

create table if not exists products (
    id          uuid primary key default gen_random_uuid(),
    store_id    uuid references stores(id) on delete cascade,
    name        text not null,
    category    text,
    unit        text,
    store_name  text, 
    constraint products_store_name_idx unique (store_id, name)
);

create table if not exists offers (
    id             uuid primary key default gen_random_uuid(),
    product_id     uuid references products(id) on delete cascade,
    current_price  numeric not null,
    was_price      numeric,
    start_date     timestamptz default now(),
    end_date       timestamptz,
    is_discount    boolean default true
);


create table if not exists chat_sessions (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid references auth.users(id) on delete cascade,  
    name        text not null,                                      
    content     text,                          
    messages    jsonb default '[]'::jsonb,     
    intent      jsonb default '{}'::jsonb,     
    saved_at    timestamptz default now(),
    constraint chat_sessions_user_id_name_key unique (user_id, name)
);

alter table chat_sessions add column if not exists user_id uuid references auth.users(id) on delete cascade;

do $$ begin
    alter table chat_sessions drop constraint if exists chat_sessions_name_key;
exception when others then null; end $$;
do $$ begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'chat_sessions_user_id_name_key' and conrelid = 'chat_sessions'::regclass
    ) then
        alter table chat_sessions add constraint chat_sessions_user_id_name_key unique (user_id, name);
    end if;
exception when others then null; end $$;

alter table chat_sessions enable row level security;

drop policy if exists "Users can view own sessions" on chat_sessions;
create policy "Users can view own sessions"
    on chat_sessions for select
    using (auth.uid() = user_id);

drop policy if exists "Users can insert own sessions" on chat_sessions;
create policy "Users can insert own sessions"
    on chat_sessions for insert
    with check (auth.uid() = user_id);

drop policy if exists "Users can update own sessions" on chat_sessions;
create policy "Users can update own sessions"
    on chat_sessions for update
    using (auth.uid() = user_id);

drop policy if exists "Users can delete own sessions" on chat_sessions;
create policy "Users can delete own sessions"
    on chat_sessions for delete
    using (auth.uid() = user_id);


create table if not exists user_profiles (
    id                    uuid primary key,      -- mirrors auth.users.id
    email                 text not null unique,
    full_name             text not null default '',
    onboarding_completed  boolean default false,
    created_at            timestamptz default now(),
    updated_at            timestamptz default now()
);

alter table user_profiles
    add column if not exists onboarding_completed boolean default false;

alter table user_profiles enable row level security;

drop policy if exists "Users can view own profile" on user_profiles;
create policy "Users can view own profile"
    on user_profiles for select
    using (auth.uid() = id);

drop policy if exists "Users can insert own profile" on user_profiles;
create policy "Users can insert own profile"
    on user_profiles for insert
    with check (auth.uid() = id);

drop policy if exists "Users can update own profile" on user_profiles;
create policy "Users can update own profile"
    on user_profiles for update
    using (auth.uid() = id);


-- =============================================================================
-- VIEW: public.user_login_details
-- Joins auth.users with user_profiles so admins can see all registered users
-- in the Supabase Table Editor (Views section).
-- =============================================================================

drop view if exists public.user_login_details;

create view public.user_login_details as
select
    u.id,
    u.email,
    coalesce(p.full_name, u.raw_user_meta_data->>'full_name', '') as full_name,
    u.created_at          as registered_at,
    u.last_sign_in_at,
    u.email_confirmed_at,
    case when u.email_confirmed_at is not null then 'Verified' else 'Unverified' end as email_status,
    coalesce(p.onboarding_completed, false) as onboarding_completed,
    p.updated_at          as profile_updated_at
from auth.users u
left join public.user_profiles p on p.id = u.id
order by u.created_at desc;

grant select on public.user_login_details to authenticated;
grant select on public.user_login_details to service_role;
grant select on public.user_login_details to anon;
