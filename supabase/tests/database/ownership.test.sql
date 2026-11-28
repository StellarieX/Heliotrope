begin;

create extension if not exists pgtap with schema extensions;
set local search_path = public, extensions;
select no_plan();

insert into auth.users (id) values
    ('00000000-0000-4000-8000-000000000001'),
    ('00000000-0000-4000-8000-000000000002');

set local role authenticated;
set local "request.jwt.claim.sub" = '00000000-0000-4000-8000-000000000001';

select lives_ok($test$
    select complete_onboarding('first_user', 'First User', null, 'Student', 'Home', 2, 8,
        '[{"name":"Water heater","powerKw":2,"jobType":"THERMAL"}]');
$test$, 'Onboarding writes the profile and its initial load');
select is((select max_power_kw from profiles), 8::numeric, 'The owner can read capacity');
select is((select count(*) from jobs), 1::bigint, 'The owner can read their own load');
select lives_ok($test$
    select complete_onboarding('first_user', 'First User', null, 'Student', 'Home', 2, 8,
        '[{"name":"Water heater","powerKw":2,"jobType":"THERMAL"}]');
$test$, 'Onboarding can be retried');
select is((select count(*) from jobs), 1::bigint, 'An onboarding retry adds no duplicate loads');

select ok(patch_job((select id from jobs), '{"powerKw":2.5}'), 'The owner can patch a load');
select is((select spec ->> 'name' from jobs), 'Water heater', 'A patch preserves other load properties');
select is((select spec ->> 'powerKw' from jobs), '2.5', 'The changed load property is persisted');

set local "request.jwt.claim.sub" = '00000000-0000-4000-8000-000000000002';
select is((select count(*) from profiles), 0::bigint, 'The other account cannot read the private profile');
select is((select count(*) from jobs), 0::bigint, 'The other account cannot read the loads');
select throws_ok($test$
    insert into jobs (owner_id, spec) values ('00000000-0000-4000-8000-000000000001', '{"name":"Another job"}');
$test$, '42501', null, 'A load cannot be assigned to another account');
select throws_ok($test$
    select complete_onboarding('first_user', 'Other User', null, null, null, null, null, '[]');
$test$, '23505', null, 'Usernames are unique across accounts');
select throws_ok($test$
    select complete_onboarding('other_user', 'Other User', null, null, null, null, null, '[{"name":4}]');
$test$, '23514', null, 'An invalid load fails onboarding');
select is((select count(*) from profiles), 0::bigint, 'Invalid onboarding rolls back the profile too');

set local role anon;
select is((select display_name from profile_directory where username = 'first_user'),
    'First User', 'Public username lookup works when signed out');
select ok(not has_column_privilege(current_user, 'public.profile_directory', 'user_id', 'SELECT'),
    'Public profiles do not expose internal user IDs');
select ok(not has_table_privilege(current_user, 'public.profiles', 'SELECT'), 'Private profiles are unavailable when signed out');
select ok(not has_function_privilege(current_user, 'public.patch_job(text,jsonb)', 'EXECUTE'), 'Guests cannot patch loads');

reset role;
set local role authenticated;
set local "request.jwt.claim.sub" = '00000000-0000-4000-8000-000000000001';
update profiles set username = 'renamed_user' where id = auth.uid();
set local role anon;
select is((select count(username) from profile_directory where username = 'first_user'),
    0::bigint, 'Renaming releases the old public username');
select ok(exists(select username from profile_directory where username = 'renamed_user'),
    'Renaming updates the public profile');

reset role;
grant heliotrope_api to current_user;
set local role heliotrope_api;
insert into private.execution_schedules (schedule_id, owner_id, lifecycle, data)
values ('aabbccddeeff', '00000000-0000-4000-8000-000000000001', 'SCHEDULED',
    '{"schedule_id":"aabbccddeeff","lifecycle":"SCHEDULED"}');
reset role;
select ok(exists (select 1 from private.execution_schedules where schedule_id = 'aabbccddeeff'),
    'The limited backend role can save an execution plan');
select ok(not has_table_privilege('heliotrope_api', 'public.profiles', 'UPDATE'),
    'The backend database role cannot modify account profiles');

delete from auth.users where id = '00000000-0000-4000-8000-000000000001';
select is((select count(*) from profiles where username = 'renamed_user'), 0::bigint, 'Deleting an Auth user removes the profile');
select is((select count(*) from profile_directory where username = 'renamed_user'), 0::bigint, 'Deleting an Auth user releases the public username');
select is((select count(*) from jobs where owner_id = '00000000-0000-4000-8000-000000000001'), 0::bigint, 'Deleting an Auth user removes loads');
select is((select count(*) from private.execution_schedules where schedule_id = 'aabbccddeeff'), 0::bigint, 'Deleting an Auth user removes execution data');

select * from finish();
rollback;
