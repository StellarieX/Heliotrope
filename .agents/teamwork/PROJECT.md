# Project: Heliotrope Overhaul

## Architecture
Heliotrope is a clean energy scheduling and carbon-aware load orchestration platform.
- **Frontend**: Next.js 16 (App Router), React 19, Tailwind CSS 4, Firebase Client SDK (Firestore & Auth). Located in `app/` and `lib/`.
- **Backend**: FastAPI 0.141+, Pydantic 2, OR-Tools 9.15 (CP-SAT scheduler), HTTPX, Uvicorn. Located in `backend/app/`.
- **Security & Database**: Cloud Firestore (`firestore.rules`), client queries in `lib/username.ts` and `app/`.
- **Data Flow**:
  1. User authentication and job profiles stored in Firestore.
  2. Frontend normalizes jobs and queries FastAPI backend (`/api/v1/schedules/plan`, `/api/v1/coordination/schedule`, `/api/v1/carbon/forecast`).
  3. Backend CP-SAT optimizer computes optimal start times and power profiles subject to capacity limits, carbon intensity, and thermal comfort bands.
  4. Backend execution store records schedules, versions, events, and lifecycle state.
  5. Live session displayed and managed in `ExecutionPanel` on the frontend dashboard.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Public Profile Route Access | Allow any visitor to view claimed public profiles at `/[username]` without encountering Firestore permission errors. | M1 | R1 (Survey Exp 1) |
| 2 | Username Claim Ownership Security | Enforce in `firestore.rules` that `/usernames/{name}` claim docs cannot be created for others, overwritten, or deleted by unauthorized third parties. | M1 | R1 (Survey Exp 1) |
| 3 | Multi-User Coordination Panel | Integrate `BuildingChart` into dashboard to visualize aggregate power, flexible demand, and capacity limits via `coordinateBuilding()`. | M2 | R2 (Survey Exp 1) |
| 4 | Carbon Forecast Mode & Interval Controls | Support selecting forecast modes (`ACTUAL`, `EXPECTED`, `ROBUST`) on `CarbonChart` and dashboard, visualizing prediction intervals, and passing mode to live planning. | M2 | R2 (Survey Exp 1) |
| 5 | Thermal Comfort Boundaries & Scheduling | Add comfort boundary configuration (min/max temperature) for thermal appliances in the load form and schedule them in live planning rather than skipping. | M2 | R2 (Survey Exp 1) |
| 6 | Load Form Parameter Preservation | Preserve user-entered energy targets (`energyKwh`) and duration (`durationMin`) in generated scheduling payloads rather than replacing with static defaults. | M2 | R2 (Survey Exp 1) |
| 7 | Backend Execution State Persistence | Back `ExecutionStore` with durable SQLite storage (`sqlite3`) persisting schedule records, versions, and events across backend restarts. | M3 | R3 (Survey Exp 2) |
| 8 | Client Live Session Durability | Persist active schedule ID in `localStorage` and rehydrate live schedule and timeline on dashboard mount across browser reloads. | M3 | R3 (Survey Exp 2) |
| 9 | Live Grid Carbon Provider Adapter | Upgrade `ExternalProvider` to execute against live Electricity Maps API via `httpx` when configured, with clean fallback to synthetic duck curve when unconfigured. | M4 | R4 (Survey Exp 2) |
| 10 | Live NL Load Classification Adapter | Add live LLM/Gemini classification adapter in `load_intelligence.py` via `httpx`, falling back gracefully to `RuleBasedLoadClassifier` when unconfigured or on error. | M4 | R4 (Survey Exp 2) |
| 11 | Backend Hypothesis Dependency & Test Zero-Regression | Ensure `hypothesis>=6.100` is installed and verified so all 38 backend test files (`pytest backend/tests`) pass with zero regressions. | M4 | Survey Exp 3 |
| 12 | End-to-End Test Suite Verification | Validate 100% pass across Tiers 1-4 requirement-driven opaque-box tests, followed by Tier 5 adversarial coverage hardening. | M5 | Acceptance Criteria |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Security & Identity Guardrails | `firestore.rules`: Public profile read on `/users/{uid}`, granular claim authorization on `/usernames/{name}`. | none | DONE |
| M2 | UI Feature Completion & Form Integrity | `app/dashboard/`: `BuildingChart` integration, `CarbonChart` forecast controls, thermal comfort inputs & scheduling, preserve user kWh/min. | none | DONE |
| M3 | Execution State Persistence & Session Durability | `backend/app/services/execution_store.py`: SQLite durable store. `app/dashboard/page.tsx`: localStorage session retention & mount rehydration + tick polling (`POST /schedules/{id}/tick`). | none | DONE |
| M4 | Live External Provider Adapters | `backend/app/services/providers/external.py`: Live carbon adapter + fallback. `backend/app/services/load_intelligence.py`: Live LLM classifier + fallback. Hypothesis installation. | none | DONE |
| M5 | Final Milestone (E2E Pass & Adversarial Hardening) | Phase 1: 100% pass on E2E test suite (Tiers 1-4). Phase 2: Adversarial coverage hardening (Tier 5). | M1, M2, M3, M4 | PLANNED |

## Interface Contracts
### Frontend ↔ Firestore
- `/users/{uid}`: Public read permitted (`allow read: if true;`), write restricted to owner (`request.auth.uid == uid`).
- `/users/{uid}/jobs/{jobId}`: Read and write restricted to owner (`request.auth.uid == uid`).
- `/usernames/{name}`: Public read permitted. Create allowed if `request.auth != null && request.resource.data.uid == request.auth.uid`. Update allowed if `resource.data.uid == request.auth.uid && request.resource.data.uid == request.auth.uid`. Delete allowed if `resource.data.uid == request.auth.uid`.

### Frontend ↔ Backend API
- `POST /api/v1/coordination/schedule`: Accepts `{ buildings: [...], capacity_kw: number }`, returns `CoordinationResult` with `aggregate_profile: CoordinationAggregatePoint[]`.
- `POST /api/v1/carbon/forecast`: Accepts `CarbonForecastRequest`, returns `CarbonForecast` containing `points: CarbonForecastPoint[]` (`predicted_gco2_per_kwh`, `lower_gco2_per_kwh`, `upper_gco2_per_kwh`).
- `POST /api/v1/schedules/plan`: Accepts `SchedulerInput` with `jobs: LoadSpec[]` (preserving user `energy_required_kwh`, `duration_minutes`, and `thermal: ThermalSpec` for thermal loads), returns `ScheduleResponse`.
- `GET /api/v1/schedules/{id}/state` & `GET /api/v1/schedules/{id}/history`: Returns `ExecutionState` and `ScheduleHistory`.

### Backend Execution Store Contract
- `ExecutionStore`:
  - `create(scheduler_input, result, reason) -> ScheduleRecord`: Inserts record into SQLite (`backend/data/heliotrope_execution.db`) and caches in memory.
  - `get(schedule_id) -> ScheduleRecord | None`: Retrieves from cache or loads from SQLite via `ScheduleRecord.model_validate_json()`.
  - `append_version(schedule_id, ...)`: Updates record and commits to SQLite.
  - `record_event(schedule_id, ...)`: Appends event and commits to SQLite.
  - `save(record: ScheduleRecord)`: Persists modified record to SQLite.

### Backend External Provider Contracts
- `ExternalProvider`:
  - Uses `ELECTRICITY_MAPS_API_KEY` (and optional `ELECTRICITY_MAPS_ZONE`, default `US-CAL-CISO`).
  - When configured: HTTP GET to Electricity Maps API, parses to `list[CarbonPoint]`.
  - When unconfigured or on network error: Falls back cleanly to `SyntheticDuckCurveProvider.get_signal()`, unless strict contract test expects `ProviderNotConfigured`.
- `LoadIntelligenceProvider`:
  - Uses `GEMINI_API_KEY` or `JEV_API_KEY` (`GEMINI_API_KEY` wins when both are set; `JEV_API_KEY` is the legacy alias — see `backend/app/core/config.py:29-34`).
  - When configured: HTTP POST to LLM API requesting structured JSON classification.
  - When unconfigured or on network/API failure: Falls back cleanly to `RuleBasedLoadClassifier.classify()`.

## Code Layout
- Frontend: `app/` (Next.js pages and dashboard components), `lib/` (API client, types, Firestore utilities).
- Backend: `backend/app/` (routes, domain models, services, schedulers, core), `backend/tests/` (unit and integration tests).
- Rules: `firestore.rules` (Firestore security rules).
- State/Metadata: `.agents/teamwork/` (orchestrator and subagent coordination files).
