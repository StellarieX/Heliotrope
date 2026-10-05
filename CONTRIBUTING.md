# Contributing

## Setup

See `README.md` for full stack setup (Next.js 16 / React 19 / TypeScript strict ES2017 frontend; FastAPI / Pydantic / OR-Tools backend; pytest / hypothesis tests). Quick start:

- Frontend: `npm install && npm run dev`
- Backend: from `backend/`, `pip install -e ".[dev]"` then `uvicorn app.main:app --port 8000`
- E2E runner + bridge: `tests/e2e/runner.py`, `backend/tests/test_e2e_requirements.py`
- Spec: 12 features across milestones M1–M5 with interface contracts — see `CHANGELOG.md` for shipped status.

## Code style

- Colocate components with their routes; shared logic goes in `lib/`.
- `lib/api/client.ts` is the sole frontend-to-backend boundary. Do not add raw `fetch` calls to the API elsewhere; extend the client and types in `lib/api/types.ts`.
- TypeScript: strict, target ES2017, path alias `@/*`. No `any` without justification.
- Python: FastAPI + Pydantic models, `backend/app/services/` for domain logic.
- Keep `firestore.rules` (25 lines) minimal; any rule change needs new challenge tests.

## Honesty policy (required in code and PRs)

- Label synthetic or simulated data explicitly (`SYNTHETIC` / `simulation`).
- Use `None`, not `0`, for unknown numeric results.
- Return explicit `INFEASIBLE` / error states instead of silently degrading.
- Never fabricate measurements, savings, or model outputs in UI, fixtures, or docs.

## Tests

| Tier | Size | File | Focus |
|---|---|---|---|
| T1 | 50 | `tests/e2e/test_tier1_features.py` | per-feature happy paths |
| T2 | 50 | `tests/e2e/test_tier2_boundaries.py` | boundaries, 404s, round-trips |
| T3 | 15 | `tests/e2e/test_tier3_combinations.py` | cross-feature combinations |
| T4 | 5 | `tests/e2e/test_tier4_scenarios.py` | end-to-end scenarios |
| Rules | 22 | `tests/test_firestore_rules_challenge.py` | Firestore allow/deny matrix |
| Adversarial | 32 | `tests/e2e/test_m1_adversarial_challenger.py` | hijack, deletion, spoofed UID, prompt-injection |

Run:

- `pytest backend/tests/` — backend unit tests
- `pytest tests/ -x -q` — full E2E tiers (needs backend importable; `clean_execution_store` in `tests/e2e/conftest.py` builds a default `ExecutionStore()` on the shared default SQLite path — per-test isolated DB files are NOT wired, see `TEST_INFRA.md` correction)
- `pytest tests/test_firestore_rules_challenge.py tests/e2e/test_m1_adversarial_challenger.py` — security suites
- Property tests (`backend/tests/test_properties.py`) need `pip install "hypothesis>=6.100"`; `.hypothesis/` cache dir is gitignored.

## PR checklist

- [ ] `npm run lint` passes
- [ ] `npx tsc --noEmit` passes
- [ ] `npm run build` passes
- [ ] `pytest backend/tests/ tests/` passes (or note which tier was run and why)
- [ ] Honesty policy followed (no unlabeled synthetic data, no `0`-for-unknown)
- [ ] Firestore rule changes include challenge-test updates
- [ ] Docs updated (`README.md` / `DEPLOYMENT.md` / `CHANGELOG.md` if behavior changed)
