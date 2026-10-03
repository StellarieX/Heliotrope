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
  see `firestore.rules`).
- **Backend:** Python 3.11, FastAPI + Pydantic, OR-Tools CP-SAT, pytest. Runs
  separately (default `:8000`).

## Run frontend

```bash
npm install
cp .env.example .env.local   # fill Firebase keys
npm run dev                  # http://localhost:3000
```

## Run backend

```bash
python -m venv backend/.venv
backend/.venv/Scripts/python -m pip install fastapi uvicorn pydantic httpx pytest hypothesis ortools
backend/.venv/Scripts/python -m uvicorn app.main:app --app-dir backend --port 8000
```

## Run tests

```bash
npm run lint && npm run build                              # frontend
backend/.venv/Scripts/python -m pytest backend/tests -q    # backend (~4600 tests)
```

## What works (through Phase 7)

- Landing, Google auth, onboarding, dashboard, account, public `/{username}` pages.
- Load classification (local rules + backend `/loads/classify` with
  confidence/ambiguity/assumptions); always-on loads filtered, never scheduled.
- Schedulers: ASAP baseline, greedy, CP-SAT (integer-scaled), plus forecast
  EXPECTED/ROBUST modes, comparison endpoint, per-job CO₂ explanations.
- Multi-user coordination under shared capacity with congestion + AVG/MAX
  fairness objectives and independent-vs-coordinated comparison.
- Execution layer: versioned schedules, rolling-horizon replans with frozen
  past, event/override handling with guardrails, deterministic simulator
  (labeled simulation, never telemetry), planned-vs-realized metrics.
- Dashboard "Live schedule" panel plans from your loads via the backend and
  tracks execution truth. The "Rank" button is a local priority heuristic
  only — not the optimizer.

## Known limitations

- Thermal loads need a comfort band the dashboard cannot know; they are
  skipped in live planning with an explanation.
- Time-varying shared capacity is rejected (constant only).
- External carbon/vendor APIs are adapter boundaries, not live integrations.
- **JEV status:** a `JevLoadIntelligence` boundary and server-side
  `JEV_API_KEY` config exist, but no verified upstream contract does — the
  classifier always uses local rules and refuses rather than invents.
- Execution store is in-memory (no DB persistence yet).
- Backend is not hosted; Vercel serves the frontend only.

## Data honesty policy

Synthetic/demo data is always labeled (`SYNTHETIC`, "model projection",
"simulation"). Uncomputed metrics stay `None`, never zero. Unimplemented or
infeasible work returns explicit errors/INFEASIBLE, never fabricated output.
