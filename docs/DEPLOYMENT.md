# Deployment and legacy migration

Configure the Google provider for the target Supabase project. In Google Cloud, authorize `https://<project-ref>.supabase.co/auth/v1/callback`. In Supabase Auth, set the frontend site URL and allow the frontend's `/auth/callback` URL. Apply the SQL migrations and verify row-level security before serving traffic.

Set `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` on the frontend. Set `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SECRET_KEY`, and `DATABASE_URL` on the backend. Keep the secret and database URL server-side. The SQL migration defines the `heliotrope_api` database role with the execution permissions the backend requires. Give a dedicated database login membership in that role and put its TLS-enabled connection string in `DATABASE_URL`.

For the Vercel same-origin rewrite, set `BACKEND_URL` and leave `NEXT_PUBLIC_BACKEND_URL` unset. Configure exact CORS origins if calling the backend cross-origin. Render's Blueprint lists required backend variables.

## Importing historical data

Back up the Firebase export and SQLite database, and take a restorable PostgreSQL backup. Import or create Supabase Auth accounts separately, verify their identities, and explicitly map each Firebase UID to its Supabase UUID. Verify a schedule's ownership separately because the previous store carried no account identifier. Freeze writes during cutover and rehearse on an isolated database first. Fold `users/{uid}/settings/prefs.maxPowerKw` into `profile.maxPowerKw` in the normalized export.

The importer accepts normalized JSON rather than a vendor-specific export. Convert the Firestore export into this shape, preserving document IDs and job values. Export SQLite `schedules` rows into the `schedules` array; `data` may be a parsed object or its original JSON string.

```json
{"users":{"firebase-uid":{"profile":{"username":"alice","displayName":"Alice","photoURL":null,"occupation":null,"place":null,"rooms":2,"maxPowerKw":5,"onboarded":true},"jobs":{"original-job-id":{"name":"Washer","createdAt":"2026-01-01T00:00:00Z"}}}},"schedules":[]}
```

Provide a second JSON file with explicit mappings. Each schedule maps to a Firebase UID already present in `users`; omit it when ownership cannot be established. Unmapped users and schedules are skipped and reported. Do not upload either file to the repository.

```json
{"users":{"firebase-uid":"00000000-0000-0000-0000-000000000001"},"schedules":{}}
```

From `backend`, run `python scripts/import_legacy.py export.json mapping.json` for a dry run. Review skipped records and identity mappings. Set `DATABASE_URL` for the intended database and add `--apply` to execute. The importer checks that mapped target accounts exist, inserts without overwriting, and commits all eligible records in one transaction; a constraint or conflict rolls back the transaction. A dry run validates input and reports counts but does not connect to the target or detect database conflicts. Account verification and source conversion are operator responsibilities.

For rollback, stop writes and restore the pre-cutover database backup; retain the original Firebase and SQLite data until reconciliation is complete. Restoring a backup also removes subsequent writes, so plan the cutover window accordingly. No live migration has been performed by this repository.

The production backend Dockerfile installs the pinned `backend/requirements.lock` on Python 3.11. Build the frontend with Node 24 and `npm ci` using `package-lock.json`. Configure deployment credentials in the owner's hosting accounts; no remote resources are changed by these instructions.
