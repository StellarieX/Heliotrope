# Heliotrope

Named for the flower that turns to face the sun. Heliotrope shifts deferrable
electricity loads into low-carbon windows — always ready on time. Deadlines,
energy requirements, thermal comfort, and shared capacity are hard constraints;
carbon is the objective. Forecasts may change *which* schedule is preferred,
never whether one is feasible.

## Stack

- **Frontend:** Next.js 16, React 19, Tailwind CSS v4, TypeScript. Black
  minimal UI. Deployed on Vercel (`vercel.json` at root).
- **Auth + data:** Firebase Google sign-in only (no email/password), Firestore
  (`users/{uid}`, `users/{uid}/jobs/{id}`, public `usernames/{name}` registry;
  see `firestore.rules`). Singleton in `lib/firebase.ts:1`.
- **Backend:** Python 3.11, FastAPI + Pydantic, OR-Tools CP-SAT, pytest. Runs
  separately (default `:8000`). Title `Heliotrope Backend`
  (`backend/app/main.py:16`). No auth; CORS only
  (`backend/app/main.py:26`, `backend/app/core/config.py:20`).
- **Backend boundary:** the sole frontend-to-backend boundary is
  `lib/api/client.ts:17` (`BASE = NEXT_PUBLIC_BACKEND_URL ??
  http://localhost:8000`). No raw `fetch()` elsewhere.

## Routes

- `/` — landing + simulator (`app/page.tsx`).
- `/dashboard` — main app: loads, Live schedule panel, execution tracking
  (`app/dashboard/page.tsx`).
- `/account` — profile, username claim (`app/account/page.tsx`).
- `/{username}` — public profile; greedy route, resolves via `usernames/{name}`
  registry (`app/[username]/page.tsx`, `lib/username.ts`).

## Setup

See `SETUP.md` for full instructions. Quick start:

```bash
npm install
cp .env.example .env.local   # fill Firebase keys + backend URL
npm run dev                  # http://localhost:3000
```

```bash
python -m venv backend/.venv
backend/.venv/Scripts/python -m pip install -e "backend[dev]"  # or backend/pyproject.toml deps
backend/.venv/Scripts/python -m uvicorn app.main:app --app-dir backend --port 8000
```

Env template: `.env.example` (root). All frontend keys are `NEXT_PUBLIC_*`;
never put secrets there. Server-only keys (`JEV_API_KEY`,
`ELECTRICITY_MAPS_API_KEY`) live in the backend environment only.
Config: `backend/app/core/config.py:1`.

## API

21 endpoints under `/api/v1` plus `GET /` (see `backend/app/main.py:63`):

| Group | Endpoints |
|---|---|
| health | `GET /api/v1/health` |
| carbon | `GET /api/v1/carbon`, `POST /api/v1/carbon/forecast`, `/carbon/forecast/evaluate`, `/carbon/forecast/backtest` |
| loads | `POST /api/v1/loads/classify`, `POST /api/v1/loads/validate` |
| schedule | `POST /api/v1/schedule`, `POST /api/v1/schedule/compare` |
| coordination | `POST /api/v1/coordination/schedule`, `POST /api/v1/coordination/compare` |
| execution | `POST /api/v1/schedules/plan`, `POST /api/v1/schedules/plan-coordinated`, `GET /api/v1/schedules/{id}/state`, `GET /api/v1/schedules/{id}/history`, `POST /api/v1/schedules/{id}/events`, `POST /api/v1/schedules/{id}/replan`, `POST /api/v1/schedules/{id}/tick`, `POST /api/v1/schedules/{id}/telemetry`, `POST /api/v1/schedules/{id}/override`, `POST /api/v1/simulation/{id}/advance` |

Interactive docs when the backend runs: `http://localhost:8000/docs`.
Contracts mirrored in `lib/api/types.ts`. See `backend/README.md`.

## Tests

```bash
npm run lint && npm run build                              # frontend
npx tsc --noEmit                                           # typecheck (no script in package.json:5)
backend/.venv/Scripts/python -m pytest backend/tests -q    # backend unit/property (38 files)
backend/.venv/Scripts/python -m pytest tests/e2e -v        # e2e tiers
python tests/e2e/runner.py                                 # structured 4-tier summary
```

- `backend/tests`: 38 test files covering scheduling, carbon, forecast,
  loads, coordination, execution/simulation, tick, telemetry.
- `tests/e2e`: 120 tests in the runner (`tests/e2e/runner.py:30`) —
  T1 features (50), T2 boundaries (50), T3 combinations (15), T4 scenarios (5).
  Adversarial/challenger suites (e.g. `test_m1_adversarial_challenger.py`)
  sit outside the runner.
- `package.json:5` scripts are `dev`/`build`/`start`/`lint` only — no
  test/typecheck script.

## What works (through Phase 7)

- Landing, Google auth, onboarding, dashboard, account, public `/{username}` pages.
- Load classification (local rules + backend `/loads/classify` with
  confidence/ambiguity/assumptions); always-on loads filtered, never scheduled.
- Schedulers: ASAP baseline, greedy, CP-SAT (integer-scaled), plus forecast
  EXPECTED/ROBUST modes, comparison endpoint, per-job CO₂ explanations.
- Thermal loads supported via comfort-band `ThermalSpec`
  (`tempMinC`/`tempMaxC`); the dashboard collects the band at load entry, so
  thermal jobs plan normally instead of being skipped.
- Multi-user coordination under shared capacity with congestion + AVG/MAX
  fairness objectives and independent-vs-coordinated comparison.
- Execution layer: versioned schedules, rolling-horizon replans with frozen
  past, event/override handling with guardrails, deterministic simulator
  (labeled simulation, never telemetry), planned-vs-realized metrics.
- Dashboard "Live schedule" panel plans from your loads via the backend and
  tracks execution truth. The "Rank" button is a local priority heuristic
  (`lib/prioritize.ts`) only — not the optimizer.

## Phase 7 vs M1–M5

Older docs refer to milestones M1–M5; current code is organized as Phases
2–7. Rough mapping:

| Phase | Scope | Old milestone |
|---|---|---|
| Phase 2 | Carbon intelligence (`/carbon`) | M1 |
| Phase 3 | Load intelligence (`/loads/classify`, `/loads/validate`) | M2 |
| Phase 4 | Optimization engines (`/schedule`, `/schedule/compare`) | M3 |
| Phase 5 | Forecasting (`/carbon/forecast*`) | M4 |
| Phase 6 | Coordination (`/coordination/*`) | M5 |
| Phase 7 | Execution + simulation (`/schedules/*`, `/simulation/*`) | post-M5 |

Where a doc still says "M1–M5", read it as "Phases 2–6 plus execution".

## Data honesty policy

Synthetic/demo data is always labeled (`SYNTHETIC`, "model projection",
"simulation"). Uncomputed metrics stay `None`, never zero. Unimplemented or
infeasible work returns explicit errors/INFEASIBLE, never fabricated output.
Rule-based load intelligence is live; the Gemini/Jev adapter
(`GEMINI_API_KEY`, legacy alias `JEV_API_KEY`) and the Electricity Maps
carbon adapter (`ELECTRICITY_MAPS_API_KEY`) are live with honest fallback —
configured they call upstream via `httpx`, unconfigured or on upstream
failure they fall back to local rules / synthetic signal (or 503 for an
explicitly requested external provider).

## Known limitations

- Time-varying shared capacity is supported via `capacity_profile_kw`
  (per-slot kW, length == horizon slots; overrides scalar per slot).
- External carbon/vendor APIs are live adapters with honest fallback
  (Electricity Maps + Gemini via `httpx`; synthetic/rules fallback when
  unconfigured).
  Default carbon provider is synthetic (seed 7,
  `backend/app/core/config.py:32`).
- **Jev/Gemini status:** a `JevLoadIntelligence` live adapter plus
  server-side `GEMINI_API_KEY` config (`JEV_API_KEY` honored as legacy
  alias, `GEMINI_API_KEY` wins) exist: with a key it attempts one
  structured Gemini call and strictly validates the JSON, with any failure
  falling back to local rules (recorded as an assumption) rather than
  inventing output.
- Execution store is SQLite (`ExecutionStore`, `backend/data/heliotrope_execution.db`
  or `HELIOTROPE_EXECUTION_DB`, in-memory read-through cache);
  schedules survive backend restarts. Back up / mount the DB file as a
  volume in hosted deploys.
- Backend is not hosted; Vercel serves the frontend only. Set
  `NEXT_PUBLIC_BACKEND_URL` to a reachable backend or the UI reports
  "backend unreachable".
- No backend auth; anyone with the URL can call all endpoints. CORS is the
  only browser-side restriction.
