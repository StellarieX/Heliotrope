# Test infrastructure

Run the checks from the repository root:

```sh
npm run lint
npx tsc --noEmit
npm run build
python -m pytest backend/tests -q
python -m pytest tests/e2e -q -m 'not live'
supabase test db
```

Start the local Supabase stack before running `supabase test db`. The Auth-and-execution integration test uses the same local project. Provide `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SECRET_KEY`, and `DATABASE_URL` from `supabase status`, then run `python -m pytest tests/integration -q`. It creates isolated test accounts and checks profile visibility, plan ownership, failed replacement rollback, restart persistence, and account deletion. The importer tests cover mapping and skipped ownership using fixtures.

The `tests/e2e` suite includes historical scheduling and normalization scenarios. The retired Firestore rules simulations do not verify PostgreSQL authorization; use the SQL policy tests for that boundary. Live deployment tests require an explicitly configured endpoint and should only target an authorized environment.
