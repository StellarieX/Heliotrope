# Heliotrope

Heliotrope shifts deferrable electricity loads into low-carbon windows — always ready on time. Deadlines, energy needs, thermal comfort, and shared capacity are hard constraints; carbon is the objective. Forecasts may change *which* schedule is preferred, never whether one is feasible.

Named for the flower that turns to face the sun.

## How it works

Three planes, two boundaries:

```
Next.js app  --Firestore SDK-->  Firestore (auth, loads, usernames)
     |
     +--HTTPS, no token-->  FastAPI backend (pure compute)
```

- **Frontend** (`app/`, `lib/`) owns identity, load CRUD, and visualization. It talks to Firestore directly (`lib/firebase.ts`) and to the backend only through `lib/api/client.ts`.
- **Backend** (`backend/app/`) is stateless compute: carbon signals, forecasting, load classification, scheduling, multi-user coordination, execution tracking. The only durable state is SQLite-backed execution records.
- **Firestore** (`firestore.rules`) stores `users/{uid}`, `users/{uid}/jobs/{id}`, and the public `usernames/{name}` registry behind Google sign-in.

A load's journey: describe it in plain words → `/loads/classify` turns it into a typed `LoadSpec` (FIXED, ATOMIC, INTERRUPTIBLE, THERMAL) → `/schedules/plan` solves it with CP-SAT against the carbon signal → track it through events, replans, and overrides → compare planned vs realized CO₂. Multiple tenants coordinate under one shared capacity via `/coordination/schedule`.

## Architecture in brief

- **Solvers:** ASAP (carbon-blind baseline), greedy (cheapest-first heuristic), CP-SAT (exact OR-Tools model, integer-scaled, independently validated). `INFEASIBLE` is a 200 response, never a silent guess. See `docs/ARCHITECTURE.md`.
- **Carbon:** synthetic duck-curve by default, CSV upload, or live Electricity Maps. Forecasts come in EXPECTED or ROBUST (`predicted + λ·(upper − predicted)`) modes. See `docs/API.md`.
- **Execution:** versioned schedules in SQLite, rolling-horizon replans with frozen past, event/override guardrails, client-driven tick for periodic replan, meter telemetry (`MEASURED`) alongside deterministic simulation (`SIMULATED`).
- **Honesty policy:** synthetic or simulated data is always labeled; unknown values stay `null`, never zero; unconfigured providers refuse instead of inventing output.

## Run it

```bash
npm install
cp .env.example .env.local   # fill Firebase keys + backend URL
npm run dev                  # http://localhost:3000
```

```bash
python -m venv backend/.venv
backend/.venv/Scripts/python -m pip install -e "./backend[dev]"
backend/.venv/Scripts/python -m uvicorn app.main:app --app-dir backend --port 8000
```

Full environment table: `docs/SETUP.md`. API reference: `docs/API.md`. Deployment: `docs/DEPLOYMENT.md`.

## Verify it

```bash
npm run lint && npx tsc --noEmit && npm run build
python -m pytest backend/tests -q     # incl. hypothesis property tests
python tests/e2e/runner.py            # 120 tiered + 10 Tier-5 hardening tests
```

## Limitations

- Backend has no auth; anyone with the URL can call it. CORS is the only browser-side gate.
- No live device control — execution is tracked via events, meter readings, and simulation.
- Time-varying capacity is supported per-slot via `capacity_profile_kw`; the UI plans with a scalar default.
- Backend is not hosted; Vercel serves the frontend only (`NEXT_PUBLIC_BACKEND_URL` must point at a backend).

## License

MIT — see `LICENSE`. Security model: `SECURITY.md`. Contributing: `CONTRIBUTING.md`.
