# docs/INDEX — Heliotrope reference docs

Canonical docs live at repo root. This index points there
(`docs/` contains only this index to avoid duplication).

- `../ARCHITECTURE.md` — system architecture (frontend/backend/Firestore, data flow; source: `.agents/teamwork/PROJECT.md` Architecture + Interface Contracts).
- `../API.md` — frontend-backend API contracts (`/coordination/schedule`, `/carbon/forecast`, `/schedules/plan`, state/history).
- `../SETUP.md` — environment setup, test commands (`tests/e2e/runner.py`, `pytest backend/tests`, `npm run lint/build`).
- `../SECURITY.md` — Firestore rules ownership model + adversarial test inventory (Tier 5 candidates).
- `../DEPLOYMENT.md` — execution store durability (`HELIOTROPE_EXECUTION_DB`), provider keys (`ELECTRICITY_MAPS_API_KEY`, `GEMINI_API_KEY`/`JEV_API_KEY`), fallback behavior.

Related root docs: `TEST_INFRA.md`, `TEST_READY.md`, `handoff.md`.
Authoritative spec: `.agents/teamwork/PROJECT.md`.
