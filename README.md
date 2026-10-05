# Heliotrope

Shift deferrable electricity loads into low-carbon windows — always ready on time.

Heliotrope is a carbon-aware scheduler. You describe your loads (EV charging, water heating, laundry, cooling), give each a deadline, and Heliotrope places them where grid carbon is lowest while guaranteeing every deadline, energy requirement, thermal comfort band, and shared capacity limit. Deadlines and physics are hard constraints; carbon is the objective. Forecasts may change *which* schedule is preferred, never whether one is feasible.

## Features

- **Plain-words load intake** — `/loads/classify` turns "geyser at night" into a typed `LoadSpec` (FIXED, ATOMIC, INTERRUPTIBLE, THERMAL) with confidence, ambiguity flags, and recorded assumptions.
- **Three solvers** — ASAP (carbon-blind baseline), greedy (cheapest-first heuristic), and exact OR-Tools CP-SAT. Integer-scaled arithmetic with overflow guards and an independent post-solve validator; `INFEASIBLE` is an explicit 200 response, never a silent guess.
- **Carbon-aware planning** — synthetic duck-curve by default, CSV upload, or live Electricity Maps. Forecasts in EXPECTED or ROBUST (`predicted + λ·(upper − predicted)`) modes, with planned-vs-realized CO₂ scoring.
- **Multi-tenant coordination** — joint scheduling under one shared capacity with congestion pricing and AVG/MAX fairness, plus independent-vs-coordinated comparison.
- **Live execution tracking** — versioned schedules in SQLite, rolling-horizon replans with frozen past, event/override guardrails, client-driven tick for periodic replan, and meter telemetry (`MEASURED`) alongside deterministic simulation (`SIMULATED`).
- **Honesty by design** — synthetic or simulated data is always labeled; unknown values stay `null`, never zero; unconfigured providers refuse instead of inventing output.

## How it works

Three planes, two boundaries:

```
Next.js app  --Firestore SDK-->  Firestore (auth, loads, usernames)
     |
     +--HTTPS, no token-->  FastAPI backend (pure compute)
```

A load's journey:

1. Describe it in the dashboard → saved to `users/{uid}/jobs/{id}` in Firestore.
2. `/loads/classify` returns its type, category, and the exact fields still needed.
3. `/schedules/plan` solves it with CP-SAT against the carbon signal → version 1.
4. Track it: `JOB_STARTED` / `COMPLETED` / `FAILED` events, manual or periodic replans (`tick`), guarded overrides (`START_NOW`, `PAUSE`, `CANCEL`, `MOVE`, `RUN_ASAP`).
5. Compare planned vs realized CO₂; coordinate whole buildings under one capacity cap.

The frontend (`app/`, `lib/`) owns identity, load CRUD, and charts. It reaches Firestore directly (`lib/firebase.ts`) and touches the backend only through `lib/api/client.ts`. The backend (`backend/app/`) is stateless compute — the only durable state is SQLite execution records. See `docs/ARCHITECTURE.md`.

## Tech stack

| Layer | Choices |
|---|---|
| Frontend | Next.js 16, React 19, Tailwind CSS v4, TypeScript, Firebase Auth (Google) + Firestore, Vercel hosting |
| Backend | Python 3.11, FastAPI + Pydantic, OR-Tools CP-SAT, SQLite execution store, pytest + hypothesis |
| Carbon | Synthetic provider (default), CSV provider, Electricity Maps adapter (live, key-gated) |
| Intelligence | Rule-based classifier (live), Gemini adapter (live, key-gated, rule-based fallback) |

## Quickstart

Prerequisites: Node 20+, Python 3.11+, a Firebase project with Google sign-in enabled.

```bash
npm install
cp .env.example .env.local   # fill Firebase keys + backend URL
npm run dev                  # http://localhost:3000
```

```bash
python -m venv backend/.venv
backend/.venv/Scripts/python -m pip install -e "./backend[dev]"
backend/.venv/Scripts/python -m uvicorn app.main:app --app-dir backend --port 8000
# API docs: http://localhost:8000/docs
```

Full environment table and troubleshooting: `docs/SETUP.md`.

## Configuration

| Variable | Where | Purpose |
|---|---|---|
| `NEXT_PUBLIC_FIREBASE_API_KEY` (+ `_AUTH_DOMAIN`, `_PROJECT_ID`, `_APP_ID`) | Frontend (`.env.local`) | Firebase web config; all four required or the app runs unconfigured |
| `NEXT_PUBLIC_BACKEND_URL` | Frontend | Backend base URL (`http://localhost:8000` in dev); empty string in Vercel production for same-origin mode |
| `BACKEND_URL` | Frontend server-only (Vercel dashboard) | Rewrite target proxying `/api/v1/*` to the FastAPI backend |
| `ELECTRICITY_MAPS_API_KEY` / `ELECTRICITY_MAPS_ZONE` | Backend env | Live carbon signal (default zone `US-CAL-CISO`); synthetic when unset |
| `GEMINI_API_KEY` (legacy alias `JEV_API_KEY`) | Backend env | Live load classification; rule-based when unset |
| `CORS_ALLOW_ORIGINS` | Backend env | Allowed browser origins for direct cross-origin mode; unneeded in same-origin mode |
| `LOG_LEVEL` / `CARBON_*` / `LOAD_INTELLIGENCE_PROVIDER` / `DEFAULT_LOAD_TIMEZONE` | Backend env | `info` / `synthetic`, `7/300/7` tuning / `rule_based` / `UTC` |
| `HELIOTROPE_EXECUTION_DB` | Backend env | SQLite path override (default `backend/data/heliotrope_execution.db`) |

Deploy recipe (Vercel frontend + Docker backend on Render): `docs/DEPLOYMENT.md`. To run everything locally with no Firebase project, see the emulator section in `docs/SETUP.md`.

## API overview

21 endpoints under `/api/v1` (plus `GET /`): health, carbon signal, carbon forecast/evaluate/backtest, load classify/validate, schedule/compare, coordination schedule/compare, schedule plan/state/history/events/replan/override/tick/telemetry, simulation advance. Full contracts with examples: `docs/API.md`.

## Testing

```bash
npm run lint && npx tsc --noEmit && npm run build   # build passes (5 routes)
python -m pytest backend/tests -q     # unit + property (4694)
python tests/e2e/runner.py            # 162 e2e tests (tiers + hardening + live-gated)
# real rules tests in the Firestore emulator (Java 21+): see tests/rules/rules.test.mjs
```

Infra details and isolation limits: `docs/TEST_INFRA.md`.

## Project structure

```
app/                  Next.js routes: / (landing), /dashboard, /account, /{username}
lib/                  firebase singleton, api client (sole backend boundary), ranking, username claims
backend/app/          FastAPI: api/routes, domain models, services (schedulers, carbon, forecast, execution)
backend/tests/        38 pytest files incl. property tests
tests/e2e/            4-tier runner + Tier-5 hardening + rules challengers
docs/                 architecture, API, setup, deployment, test reports
firestore.rules       public reads, owner-only writes, validated profile fields
firebase.json         rules + local emulator config
render.yaml           backend blueprint (Render, Docker)
```

## Limitations

- Backend has no auth; anyone with the URL can call it. CORS is the only browser-side gate. Do not expose publicly without auth (see `SECURITY.md`).
- Naive datetimes are rejected with 422; callers must send tz-aware ISO-8601.
- Error `detail` may be a string or an array (FastAPI 422 lists); clients must handle both.
- No live device control — execution is tracked via events, meter readings, and simulation.
- The UI plans with a scalar capacity default; per-slot `capacity_profile_kw` is API-level.
- Single SQLite file: multiple backend instances must share one DB file.
- Unconfigured providers refuse or fall back instead of inventing output; CSV inputs are capped (5MB / 100k rows).

## Contributing & license

PR checklist and honesty policy: `CONTRIBUTING.md`. Security model: `SECURITY.md`. MIT — see `LICENSE`.
