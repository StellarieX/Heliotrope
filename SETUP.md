# Setup

## Prerequisites

- Node 20+, npm.
- Python 3.11+.
- A Firebase project with Google sign-in enabled (for auth/Firestore).
  The app runs without Firebase but auth and persistence are disabled
  (`lib/firebase.ts:9`).

## 1. Environment

```bash
cp .env.example .env.local   # fill Firebase keys + backend URL
```

| Variable | Where | Required | Notes |
|---|---|---|---|
| `NEXT_PUBLIC_FIREBASE_API_KEY` | frontend | for auth/DB | gates `isFirebaseConfigured()` |
| `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN` | frontend | for auth/DB | Firebase web config |
| `NEXT_PUBLIC_FIREBASE_PROJECT_ID` | frontend | for auth/DB | Firebase web config |
| `NEXT_PUBLIC_FIREBASE_APP_ID` | frontend | for auth/DB | Firebase web config |
| `NEXT_PUBLIC_BACKEND_URL` | frontend | no | defaults to `http://localhost:8000` (`lib/api/client.ts:17`) |
| `HELIOTROPE_ENV` | backend | no | default `development`; controls dev CORS (`backend/app/core/config.py:11`) |
| `PORT` | backend | no | default `8000` |
| `CORS_ALLOW_ORIGINS` | backend | prod only | comma-separated; dev allows loopback ports automatically |
| `CARBON_PROVIDER` | backend | no | default `synthetic` |
| `CARBON_CSV_PATH` | backend | for csv provider | path to carbon CSV |
| `JEV_API_KEY` | backend | no | legacy alias for `GEMINI_API_KEY`; `GEMINI_API_KEY` wins when both are set (`backend/app/core/config.py:33`) |
| `GEMINI_API_KEY` | backend | no | live Gemini classifier adapter via `httpx`; falls back to local rules when unset or on error |
| `ELECTRICITY_MAPS_API_KEY` | backend | no | live Electricity Maps adapter via `httpx` (zone `ELECTRICITY_MAPS_ZONE`, default `US-CAL-CISO`); falls back to synthetic signal when unset or on error |

Never prefix secrets with `NEXT_PUBLIC_` — those are bundled into browser JS.

## 2. Frontend

```bash
npm install
npm run dev      # http://localhost:3000
npm run build    # production build
npm run lint     # eslint
npx tsc --noEmit # typecheck (no npm script; typescript is a devDependency)
```

`package.json` scripts are `dev`/`build`/`start`/`lint` only.

## 3. Backend

```bash
python -m venv backend/.venv
backend/.venv/Scripts/python -m pip install -e "backend[dev]"
backend/.venv/Scripts/python -m uvicorn app.main:app --app-dir backend --port 8000
```

Docs: `http://localhost:8000/docs`. See `backend/README.md`.
On macOS/Linux replace `backend/.venv/Scripts/python` with
`backend/.venv/bin/python`.

## 4. Firebase

1. Create a project at console.firebase.google.com.
2. Enable Authentication > Google sign-in.
3. Create a Firestore database; deploy `firestore.rules`.
4. Register a web app; copy the four `NEXT_PUBLIC_FIREBASE_*` values into
   `.env.local`.

## 5. Tests

```bash
npm run lint && npm run build
npx tsc --noEmit
backend/.venv/Scripts/python -m pytest backend/tests -q   # backend unit/property (38 files)
backend/.venv/Scripts/python -m pytest tests/e2e -v       # e2e tiers
python tests/e2e/runner.py                                # structured 4-tier summary (120 tests)
```

E2E tiers (`tests/e2e/runner.py`): T1 features (50), T2 boundaries (50),
T3 combinations (15), T4 scenarios (5). Challenger suites outside the
runner run under the same `pytest tests/e2e` invocation.

## Troubleshooting

- UI says "backend unreachable": backend not running, wrong
  `NEXT_PUBLIC_BACKEND_URL`, or CORS blocked (check
  `backend/app/core/config.py` origins). `curl localhost:8000/api/v1/health`.
- Auth does nothing: `NEXT_PUBLIC_FIREBASE_API_KEY` unset.
- Schedules vanish on backend restart: NOT expected anymore — execution store is
  SQLite (`backend/data/heliotrope_execution.db` or `HELIOTROPE_EXECUTION_DB`).
  If IDs 404 after a restart, check the DB path/env and file permissions.
