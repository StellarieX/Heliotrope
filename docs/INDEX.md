# docs — Heliotrope reference docs

- `ARCHITECTURE.md` — system architecture (frontend/backend/Firestore, data flow).
- `API.md` — frontend-backend API contracts (`/coordination/schedule`, `/carbon/forecast`, `/schedules/plan`, state/history).
- `SETUP.md` — environment setup, test commands (`tests/e2e/runner.py`, `pytest backend/tests`, `npm run lint/build`).
- `DEPLOYMENT.md` — execution store durability (`HELIOTROPE_EXECUTION_DB`), provider keys (`ELECTRICITY_MAPS_API_KEY`, `GEMINI_API_KEY`/`JEV_API_KEY`), fallback behavior.
- `TEST_INFRA.md` — E2E test infrastructure (tiers, runner, isolation limits).

Kept at repo root per convention: `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`.
