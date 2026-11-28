# Setup

Install Node 24, Python 3.11, Docker and Supabase CLI 3.4. Start the local Supabase stack with `supabase start` and apply migrations with `supabase db reset` **only on a disposable local database**. Obtain its URL and keys from `supabase status`. Enable Google authentication and configure its callback URLs for the environment where sign-in is tested.

Copy `.env.example` to `.env.local` for the frontend and set the two `NEXT_PUBLIC_SUPABASE_*` values. Set `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SECRET_KEY`, and `DATABASE_URL` in the backend process environment. Keep the secret key and database URL out of frontend variables and version control.

Run `npm ci && npm run dev` for the frontend. In `backend`, run `python -m pip install -e ".[dev]"` and `uvicorn app.main:app --reload`. The local frontend defaults to `http://localhost:8000` for the API.
