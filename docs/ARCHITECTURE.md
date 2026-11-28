# Architecture

The Next.js client uses Supabase Auth and the Supabase browser client for profiles and saved jobs. PostgreSQL row-level security restricts private rows to their owner. A separate `profile_directory` projection exposes only public profile fields. FastAPI verifies bearer tokens for account and execution operations and stores execution schedules in a private schema, scoped to the authenticated owner. The scheduling engines, carbon providers and normalizers remain in `backend/app`.

Execution state is shared through PostgreSQL across service instances. The backend must use server-only credentials; the browser receives only the publishable key. The database migration is in `supabase/migrations`.
