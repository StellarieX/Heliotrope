# Deployment

## Frontend (Vercel)

- Framework: Next.js 16.3.8 (`vercel.json`: `framework: nextjs`, build `npm run build`, install `npm install`). `next.config.ts` is empty; PostCSS uses Tailwind v4 (`@tailwindcss/postcss`).
- Steps:
  1. `npm install`
  2. `npm run lint` and `npx tsc --noEmit` (no dedicated test/typecheck script exists)
  3. `npm run build`
  4. Connect repo to Vercel or run `vercel --prod`. No extra output directory config needed.
- Notes: `public/*.svg` are dead stock assets and `favicon.ico` is generic; replace before a public launch if branding matters.

## Backend (FastAPI, currently unhosted)

- Local: from `backend/`, `uvicorn app.main:app --port 8000` (frontend expects `:8000`).
- `backend/Dockerfile` exists (`python:3.11-slim`, installs from `backend/requirements.lock`, serves `uvicorn app.main:app --host 0.0.0.0 --port $PORT`). To host (Render / Fly / Cloud Run / VM):
  1. Build with `backend/Dockerfile` (or `pip install -e .` + `uvicorn app.main:app --host 0.0.0.0 --port $PORT`).
  2. Set `CORS_ALLOW_ORIGINS=https://<vercel-app>.vercel.app` — without this, production browsers are blocked because only the localhost regex is allowed by default.
- Do not expose publicly without auth (see `SECURITY.md`: no Firebase token verification, caller-supplied `Participant.id`).

## Firestore rules deploy

- Source of truth: root `firestore.rules` (25 lines).
- Deploy: `firebase deploy --only firestore:rules` (requires Firebase CLI + `firebase.json` / project selection). Re-run the rules challenge suites after edits:
  - `pytest tests/test_firestore_rules_challenge.py`
  - `pytest tests/e2e/test_m1_adversarial_challenger.py`

## Environment variables

| Variable | Where | Required | Notes |
|---|---|---|---|
| `CORS_ALLOW_ORIGINS` | backend | prod only | Comma-separated origins, e.g. `https://app.vercel.app`. Empty in dev. |
| `NEXT_PUBLIC_BACKEND_URL` | frontend | prod only | Point at hosted backend URL; defaults to localhost in dev (`lib/api/client.ts:17`). |
| Firebase web config | frontend | yes | API key / project ID in client; lock down via console authorized domains. |

## Persistence / ops warning

- `ExecutionStore` is SQLite-backed (`backend/data/heliotrope_execution.db`,
  overridable via `HELIOTROPE_EXECUTION_DB`, in-memory read-through cache):
  schedule IDs, versions, and events survive backend restarts. Mount/persist
  the DB file as a volume in hosted deploys — an ephemeral container
  filesystem still loses it, and multiple instances should share one DB file.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| CORS error in prod | `CORS_ALLOW_ORIGINS` unset | Set env to Vercel URL, redeploy backend |
| 404 on known schedule ID | backend restarted with an ephemeral/lost DB file | Re-create schedule; ensure the SQLite file (`HELIOTROPE_EXECUTION_DB`) is on a persisted volume |
| `/[username]` 404 | missing `usernames/{name}` claim or `users/{uid}` doc | Re-claim username while signed in |
| Vercel build fail | lint/TS error | Run `npm run lint`, `npx tsc --noEmit` locally first |
