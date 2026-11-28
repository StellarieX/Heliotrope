create schema if not exists private;

create table public.profiles (
    id uuid primary key references auth.users(id) on delete cascade,
    username text unique,
    display_name text check (length(display_name) <= 80),
    photo_url text check (length(photo_url) <= 2048 and photo_url like 'https://%'),
    occupation text check (length(occupation) <= 40),
    place text check (length(place) <= 40),
    rooms integer check (rooms between 0 and 100000),
    max_power_kw numeric check (max_power_kw > 0 and max_power_kw <= 100000),
    onboarded boolean not null default false,
    constraint profiles_username_format check (
        username ~ '^[a-z0-9_]{3,20}$'
        and username not in (
            'dashboard', 'account', 'api', 'auth', 'admin', 'login', 'signin',
            'signout', 'settings', 'help', 'about', 'null', 'undefined'
        )
    )
);

alter table public.profiles enable row level security;
revoke all on public.profiles from public, anon, authenticated;
grant select, insert, update on public.profiles to authenticated;
create policy read_own_profile on public.profiles for select to authenticated
    using (id = (select auth.uid()));
create policy insert_own_profile on public.profiles for insert to authenticated
    with check (id = (select auth.uid()));
create policy update_own_profile on public.profiles for update to authenticated
    using (id = (select auth.uid())) with check (id = (select auth.uid()));

-- Only the three published fields are readable through the Data API. The FK
-- keeps deletion atomic with the private profile and Auth user.
create table public.profile_directory (
    user_id uuid primary key references public.profiles(id) on delete cascade,
    username text not null unique,
    display_name text,
    photo_url text
);

alter table public.profile_directory enable row level security;
revoke all on public.profile_directory from public, anon, authenticated;
grant select (username, display_name, photo_url) on public.profile_directory to anon, authenticated;
create policy public_profile_lookup on public.profile_directory for select to anon, authenticated
    using (true);

create function private.sync_profile_directory()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
    if new.username is null then
        delete from public.profile_directory where user_id = new.id;
    else
        insert into public.profile_directory (user_id, username, display_name, photo_url)
        values (new.id, new.username, new.display_name, new.photo_url)
        on conflict (user_id) do update set
            username = excluded.username,
            display_name = excluded.display_name,
            photo_url = excluded.photo_url;
    end if;
    return new;
end;
$$;

revoke execute on function private.sync_profile_directory() from public, anon, authenticated, service_role;
create trigger sync_profile_directory
    after insert or update of username, display_name, photo_url on public.profiles
    for each row execute function private.sync_profile_directory();

-- Keep the existing load payload so migration can preserve document IDs and
-- optional fields. Validate at the write boundary and index the owner listing.
create function private.valid_job_spec(value jsonb)
returns boolean
language sql
immutable strict
set search_path = ''
as $$
    select case when jsonb_typeof(value) is distinct from 'object' then false else
        value - array[
            'name', 'kind', 'shiftable', 'powerKw', 'readyBy', 'flexHours',
            'jobType', 'energyKwh', 'durationMin', 'tempMinC', 'tempMaxC',
            'tempTargetC', 'confidence', 'createdAt'
        ] = '{}'::jsonb
        and coalesce(jsonb_typeof(value -> 'name') = 'string' and length(trim(value ->> 'name')) between 1 and 80, false)
        and (not value ? 'shiftable' or jsonb_typeof(value -> 'shiftable') = 'boolean')
        and (not value ? 'jobType' or value ->> 'jobType' in ('FIXED', 'DEFERRABLE_ATOMIC', 'DEFERRABLE_INTERRUPTIBLE', 'THERMAL'))
        and not exists (
            select 1 from (values ('kind', 60), ('readyBy', 10), ('createdAt', 40)) as fields(field, max_length)
            where case jsonb_typeof(value -> fields.field)
                when 'string' then length(value ->> fields.field) > fields.max_length
                when 'null' then false
                else value ? fields.field
            end
        )
        and not exists (
            select 1 from (values
                ('powerKw', 0, 100000), ('flexHours', 0, 168), ('energyKwh', 0, 1000000),
                ('durationMin', 0, 10080), ('tempMinC', -50, 150), ('tempMaxC', -50, 150),
                ('tempTargetC', -50, 150), ('confidence', 0, 1)
            ) as fields(field, min_value, max_value)
            where case jsonb_typeof(value -> fields.field)
                when 'number' then (value ->> fields.field)::numeric not between fields.min_value and fields.max_value
                when 'null' then false
                else value ? fields.field
            end
        ) end;
$$;

revoke execute on function private.valid_job_spec(jsonb) from public, anon;
grant execute on function private.valid_job_spec(jsonb) to authenticated;

create table public.jobs (
    owner_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
    id text not null default gen_random_uuid()::text check (length(id) between 1 and 128),
    spec jsonb not null check (private.valid_job_spec(spec) is true),
    created_at timestamptz not null default now(),
    primary key (owner_id, id)
);

create index jobs_owner_created_idx on public.jobs (owner_id, created_at, id);
alter table public.jobs enable row level security;
revoke all on public.jobs from public, anon, authenticated;
grant select, insert, delete on public.jobs to authenticated;
grant update (spec) on public.jobs to authenticated;
create policy read_own_jobs on public.jobs for select to authenticated
    using (owner_id = (select auth.uid()));
create policy insert_own_jobs on public.jobs for insert to authenticated
    with check (owner_id = (select auth.uid()));
create policy update_own_jobs on public.jobs for update to authenticated
    using (owner_id = (select auth.uid())) with check (owner_id = (select auth.uid()));
create policy delete_own_jobs on public.jobs for delete to authenticated
    using (owner_id = (select auth.uid()));

create function public.patch_job(p_job_id text, p_patch jsonb)
returns boolean
language plpgsql
set search_path = ''
as $$
begin
    if jsonb_typeof(p_patch) is distinct from 'object' then
        raise exception 'A load patch must be an object' using errcode = '22023';
    end if;
    update public.jobs set spec = spec || p_patch
    where owner_id = (select auth.uid()) and id = p_job_id;
    return found;
end;
$$;

create function public.complete_onboarding(
    p_username text, p_display_name text, p_photo_url text, p_occupation text,
    p_place text, p_rooms integer, p_max_power_kw numeric, p_jobs jsonb
)
returns void
language plpgsql
set search_path = ''
as $$
declare
    account_id uuid := auth.uid();
begin
    if account_id is null then
        raise exception 'Sign in before completing onboarding' using errcode = '42501';
    end if;
    if p_username is null or jsonb_typeof(p_jobs) is distinct from 'array' then
        raise exception 'A username and a load list are required' using errcode = '22023';
    end if;
    if jsonb_array_length(p_jobs) > 20 then
        raise exception 'Onboarding supports up to 20 loads' using errcode = '22023';
    end if;

    insert into public.profiles (id, username, display_name, photo_url, occupation, place, rooms, max_power_kw, onboarded)
    values (account_id, p_username, p_display_name, p_photo_url, p_occupation, p_place, p_rooms, p_max_power_kw, true)
    on conflict (id) do update set
        username = excluded.username,
        display_name = excluded.display_name,
        photo_url = excluded.photo_url,
        occupation = excluded.occupation,
        place = excluded.place,
        rooms = excluded.rooms,
        max_power_kw = coalesce(excluded.max_power_kw, profiles.max_power_kw),
        onboarded = true
    where not profiles.onboarded;

    -- The unique profile row serializes concurrent retries from the same account.
    if found then
        insert into public.jobs (owner_id, spec)
        select account_id, job.value from jsonb_array_elements(p_jobs) as job(value);
    end if;
end;
$$;

revoke execute on function public.patch_job(text, jsonb) from public, anon;
revoke execute on function public.complete_onboarding(text, text, text, text, text, integer, numeric, jsonb) from public, anon;
grant execute on function public.patch_job(text, jsonb) to authenticated;
grant execute on function public.complete_onboarding(text, text, text, text, text, integer, numeric, jsonb) to authenticated;

create table private.execution_schedules (
    schedule_id text primary key check (schedule_id ~ '^[a-zA-Z0-9_-]{12,64}$'),
    owner_id uuid not null references auth.users(id) on delete cascade,
    lifecycle text not null check (lifecycle in (
        'DRAFT', 'SCHEDULED', 'ACTIVE', 'COMPLETED', 'PARTIALLY_COMPLETED', 'CANCELLED', 'FAILED', 'STALE'
    )),
    data jsonb not null,
    revision bigint not null default 1 check (revision > 0),
    updated_at timestamptz not null default now(),
    check ((jsonb_typeof(data) = 'object' and data ->> 'schedule_id' = schedule_id and data ->> 'lifecycle' = lifecycle) is true)
);

create index execution_owner_updated_idx on private.execution_schedules (owner_id, updated_at desc, schedule_id);
create index execution_live_idx on private.execution_schedules (lifecycle, updated_at desc)
    where lifecycle in ('DRAFT', 'SCHEDULED', 'ACTIVE');
alter table private.execution_schedules enable row level security;
revoke all on private.execution_schedules from public, anon, authenticated, service_role;

-- Grant this group to the server's database login. Keep its password and DSN
-- outside migrations. Auth session access is only needed to confirm deletion.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'heliotrope_api') then
        create role heliotrope_api nologin;
    end if;
end;
$$;

grant usage on schema private, auth to heliotrope_api;
grant select, insert, update on private.execution_schedules to heliotrope_api;
grant select (id, user_id) on auth.sessions to heliotrope_api;
create policy execution_api_access on private.execution_schedules for all to heliotrope_api
    using (true) with check (true);
