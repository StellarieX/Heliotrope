# Heliotrope

Heliotrope schedules household electrical loads around capacity and carbon signals. Its Next.js frontend manages profiles and loads; a FastAPI service runs scheduling, forecasts and execution tracking.

Describe an appliance in plain language, fill in the energy, duration or temperature details the planner needs, and choose a ready-by time. Heliotrope can compare ASAP, greedy and OR-Tools CP-SAT plans. Deadlines, energy delivery, thermal comfort and capacity are hard constraints. Carbon is the scheduling objective. The default hosted carbon source is a weather-derived estimate.

Supabase Auth handles Google sign-in. Postgres stores private profiles, loads, preferences and account-owned execution history. Active schedules follow the account across devices. Public profiles expose the chosen username, display name and photo.

## Local setup

Use Node 24, Python 3.11, Docker and Supabase CLI 3.4. Copy `.env.example` to `.env.local`, configure the Supabase project and backend, then run:

```sh
npm ci
npm run dev
cd backend
python -m pip install -e ".[dev]"
uvicorn app.main:app --reload
```

See [setup](docs/SETUP.md), [architecture](docs/ARCHITECTURE.md), [API](docs/API.md), [deployment and migration](docs/DEPLOYMENT.md), and [tests](docs/TEST_INFRA.md).

Scheduling is an estimate rather than a hardware control system. Execution progress reflects reported events; connect actual telemetry before relying on it for automation.
