# Deployment

End-to-end recipe: Next.js frontend on Vercel + FastAPI backend in Docker.
Two supported frontend-to-backend modes: **same-origin** (recommended) and
**direct**.

## Modes

| Mode | `NEXT_PUBLIC_BACKEND_URL` | `BACKEND_URL` | CORS needed? |
|---|---|---|---|
| Same-origin (recommended) | `""` (empty) | Set, server-only, to the backend URL | No — browser calls `/api/v1/*`, Next.js rewrites server-side to `BACKEND_URL` |
| Direct | Backend URL (e.g. `http://localhost:8000` in dev) | Unset | Yes — browser calls the backend cross-origin |

`next.config.ts`: `RAW_BACKEND_URL = BACKEND_URL || NEXT_PUBLIC_BACKEND_URL || ""`,
trimmed of trailing slashes. No rewrite is registered when the result is empty,
so an unset backend never proxies to localhost in production. `vercel.json`:
`framework: nextjs`, `regions: ["iad1"]`, `Cache-Control: no-store` on
`/api/v1/*`.

## Frontend (Vercel)

1. Connect the repo to Vercel (build `npm run build`, install `npm install`).
2. Production environment variables (Vercel dashboard, per environment):
   - Do **not** set `NEXT_PUBLIC_BACKEND_URL` (Vercel cannot store an empty value; unset = same-origin mode in a production build).
   - `BACKEND_URL` = `https://<your-backend-host>` (server-only; no `NEXT_PUBLIC_` prefix).
   - Four `NEXT_PUBLIC_FIREBASE_*` keys.
   - `HELIOTROPE_ENV=production` is not a frontend var; set it on the backend.
3. Local dev: leave `NEXT_PUBLIC_BACKEND_URL=http://localhost:8000` in `.env.local`;
   the bundle calls the backend directly and no rewrite is needed.

## Backend on Render (demo hosting)

`render.yaml` at the repo root is a Render Blueprint for the Docker image
below: Render dashboard → New → Blueprint → this repo. Then, in Vercel,
set `BACKEND_URL=https://<service>.onrender.com` and
leave `NEXT_PUBLIC_BACKEND_URL` unset, and redeploy (env changes only apply to new builds). Free-plan caveats: the
service sleeps after ~15 min idle (first request takes ~50s, longer than the
client's 30s timeout), so open `/api/v1/health` before a demo. There is no disk,
so execution records reset on restart; the dashboard drops a lost schedule and
asks you to plan again.

## Backend (Docker)

Build from the **repo root** (paths in `backend/Dockerfile` are root-relative):

```bash
docker build -f backend/Dockerfile -t heliotrope-backend .
docker run -p 8000:8000 \
  -e HELIOTROPE_ENV=production \
  -e CORS_ALLOW_ORIGINS=https://<your-app>.vercel.app \
  -v heliotrope-data:/srv/data \
  heliotrope-backend
```

Image notes: `python:3.11-slim`, installs from `backend/requirements.lock`
(runtime-only; dev tools stay out), `PYTHONDONTWRITEBYTECODE=1` /
`PYTHONUNBUFFERED=1`, runs as non-root `appuser`, `HEALTHCHECK` on
`GET /api/v1/health`.

Environment:

| Variable | Required | Notes |
|---|---|---|
| `HELIOTROPE_ENV` | no (`development`) | `production` disables the loopback dev CORS rule |
| `PORT` | no (`8000`) | Fail-fast: non-integer values raise at import |
| `LOG_LEVEL` | no (`info`) | |
| `CORS_ALLOW_ORIGINS` | only for direct cross-origin mode | Comma-separated, slashes stripped; `"*"` gets a credential-less middleware (credentials + wildcard is rejected by browsers) |
| `CARBON_PROVIDER` | **set to `weather` in production** | Library default is `synthetic` (deterministic, for tests); `render.yaml` sets `weather` |
| `CARBON_LAT` / `CARBON_LON` / `CARBON_UTC_OFFSET_HOURS` | no | Site for the weather proxy; defaults to Bhopal, India (`23.2599`, `77.4126`, `5.5`) |
| `CARBON_WEATHER_BASE_GCO2` / `CARBON_WEATHER_SOLAR_SHARE` / `CARBON_WEATHER_WIND_SHARE` | no | Proxy calibration: intensity with no renewables (`700`) and the largest share full sun (`0.20`) / wind (`0.10`) can displace |
| `CARBON_CSV_PATH` / `CARBON_MAX_RANGE_DAYS` / `CARBON_CACHE_TTL_S` / `CARBON_SYNTHETIC_SEED` | no | Defaults empty / `7` / `300` / `7` |
| `LOAD_INTELLIGENCE_PROVIDER` | `auto` in `render.yaml` | `auto`/`jev`: Jev AI classifier when a key is set, else rules. `rule_based` never calls the model |
| `DEFAULT_LOAD_TIMEZONE` | no (`UTC`) | |
| `ELECTRICITY_MAPS_API_KEY` / `ELECTRICITY_MAPS_ZONE` | no | Only for `CARBON_PROVIDER=external`; that adapter returns the past 24h, so it cannot drive planning on its own |
| `GEMINI_API_KEY` (`JEV_API_KEY` legacy alias, `GEMINI_API_KEY` wins) | for Jev | Paste your existing Jev key here (set on **Render**, not Vercel). Falls back to the rules per request on any failure |
| `GEMINI_MODEL` / `GEMINI_MAX_CALLS_PER_MIN` / `GEMINI_BASE_URL` | no | Default `gemini-2.5-flash` / `30` model calls per minute for the whole process (the API is public; over the cap the rules answer) / Google endpoint. If classifications show "rules" with a Gemini error, check the model name first |

`config.py` auto-trusts Vercel's own `VERCEL_URL` /
`VERCEL_PROJECT_PRODUCTION_URL` hosts, so per-deployment URLs do not need manual
`CORS_ALLOW_ORIGINS` entries when the backend runs on Vercel. Explicit entries
always win.

## SQLite volume

`ExecutionStore` is SQLite (`backend/data/heliotrope_execution.db`, overridable
via `HELIOTROPE_EXECUTION_DB`; writes serialized under a `Lock`, WAL mode,
30s busy timeout). Mount/persist the DB file as a volume in hosted deploys —
an ephemeral container filesystem loses it, and multiple instances must share
one DB file. A lost DB file surfaces as 404 `not_found` on known schedule IDs.

## Firestore rules deploy

- Source of truth: root `firestore.rules` (includes a `uid is string` guard on
  claim creation). No recursive-wildcard catch-all by design; unmatched
  collections are default-deny.
- Deploy: `firebase deploy --only firestore:rules` (root `firebase.json` already
  points at `firestore.rules`; pick the project with `firebase use <id>`). **Deploy
  the rules before the new frontend**: the new onboarding no longer stores emails, and
  the stricter rules reject any profile write that still contains one. Existing profiles
  are cleaned automatically when their owner next opens the app.
- Test against the real rules engine (needs Java 21+), then the Python models:
  - `npx firebase emulators:exec --only firestore --project demo-heliotrope "node tests/rules/rules.test.mjs"` (see the file header)
  - `pytest tests/test_firestore_rules_challenge.py`
  - `pytest tests/e2e/test_m1_adversarial_challenger.py`

## Live E2E check

`tests/e2e` supports `HELIOTROPE_E2E_BASE_URL` for a live deploy check plus a
`live`-marked test. CI (`.github/workflows/ci.yml`): `frontend` (lint + tsc +
build, pinned Node via `.nvmrc`), `backend` (`py_compile` on `main.py` +
`config.py`, then `pytest backend/tests`, pinned Python via `.python-version`),
`e2e-and-rules` (hermetic `tests/e2e -k "not live"` + rules challenge).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| CORS error in prod (direct mode) | `CORS_ALLOW_ORIGINS` unset | Set to the Vercel URL, redeploy backend; or switch to same-origin mode (empty `NEXT_PUBLIC_BACKEND_URL` + `BACKEND_URL`) |
| Backend calls go to localhost in prod | `BACKEND_URL` unset while rewrite expected | Set server-only `BACKEND_URL` in Vercel dashboard |
| 404 on known schedule ID | Ephemeral/lost SQLite file | Re-create schedule; put `HELIOTROPE_EXECUTION_DB` on a persisted volume |
| `/[username]` 404 | Missing `usernames/{name}` claim or `users/{uid}` doc | Re-claim username while signed in |
| Vercel build fail | Lint/TS error | Run `npm run lint`, `npx tsc --noEmit` locally first |
| Backend boot crash `ValueError: Invalid PORT` | Non-integer `PORT` | Set `PORT` to an integer |
| `public/*.svg` branding | Dead stock assets, generic `favicon.ico` | Replace before a public launch |
