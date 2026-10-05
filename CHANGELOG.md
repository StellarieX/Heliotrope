# Changelog

## M1 / M2 — completed

- Firestore rules (`firestore.rules`): public read on `users/{uid}` and `usernames/{name}`, owner-only writes, owner-only `jobs` subcollection; covered by 22 rules-challenge tests + 32 adversarial challenger tests.
- Auth + profile UI: Google `signInWithPopup`, `onAuthStateChanged` session, username claim flow backing `/[username]` pages.
- Job input form and dashboard wiring to the FastAPI backend.
- `CarbonChart`: ACTUAL / EXPECTED / ROBUST lambda charting.
- `BuildingChart`: building load visualization.
- Thermal comfort endpoint + UI, including the README thermal fix.
- Execution state API (`ExecutionStore`, versions/events/history) — **SQLite-backed is the current truth**: records, versions, and events persist in `backend/data/heliotrope_execution.db` (or `HELIOTROPE_EXECUTION_DB`) and survive restarts.

## Phase 7 — completed

- E2E tiers landed: T1 (50), T2 (50), T3 (15), T4 (5) via `tests/e2e/runner.py` + `test_e2e_requirements.py` bridge.
- Adversarial suites green (rules challenge + M1 challenger).
- Docs baseline: README thermal correction, teamwork `PROJECT.md` contracts (12 features, M1–M5).

## M3 — shipped (SQLite persistence)

- `ExecutionStore` backed with SQLite at `backend/data/heliotrope_execution.db` (records, versions, events survive restarts and load across instances; overridable via `HELIOTROPE_EXECUTION_DB`).
- Client session durability: active schedule ID in `localStorage`, rehydrated on dashboard mount; tick polling (`POST /schedules/{id}/tick`) for periodic auto-replan.

## M4 — shipped (live adapters with honest fallback)

- Electricity Maps carbon adapter (live `httpx` call when `ELECTRICITY_MAPS_API_KEY` set, zone `ELECTRICITY_MAPS_ZONE`, default `US-CAL-CISO`); synthetic fallback / 503 when unconfigured or upstream fails.
- Gemini load-classification adapter (live `httpx` call when `GEMINI_API_KEY` — legacy alias `JEV_API_KEY`, `GEMINI_API_KEY` wins — is set); falls back to rule-based classifier, recorded as an assumption, on any failure.
- Tick + telemetry endpoints (`POST /schedules/{id}/tick`, `POST /schedules/{id}/telemetry` with `MEASURED` vs `SIMULATED` source) and time-varying `capacity_profile_kw` on `/schedule`, coordination, and replan.

## M5 — shipped (Tier 5)

- Tier 5 hardening suite: `tests/e2e/test_tier5_hardening.py` (10 tests: thermal abuse, transition guards, over-capacity, open-access shape, meter/capacity-profile validation, unknown model, tick 404, empty-MOVE no-op), wired into `tests/e2e/runner.py` (`TIER5: 10/10`) + `test_tier5_pass` bridge.
