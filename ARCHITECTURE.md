# Heliotrope Architecture

## System overview

```
+----------------+      HTTPS/fetch       +------------------+      signal      +-----------+
| Next.js (app/) |  NEXT_PUBLIC_BACKEND_URL | FastAPI (:8000)  |  (synthetic /  | Firestore |
|  /             | -----------------------> | /api/v1/*        | -- external*-> | users/*   |
|  /dashboard    |  no token, unauthenticated| 7 routers        |                | usernames/*|
|  /account      |                          | stateless except |                +-----------+
|  /[username]   | <----------------------- | ExecutionStore   |   * external providers are live
+----------------+      JSON                 +------------------+     adapters w/ honest fallback
         | Firestore SDK direct (Auth + jobs CRUD)                            (synthetic/rules default
         +------------------------------------------------------> Firestore   when unconfigured)
```

Three planes:

1. **Frontend (Next.js, `app/`, `lib/`)** — owns identity, onboarding, job CRUD, charts. Talks to backend only via `lib/api/client.ts`. Talks to Firestore directly via `lib/firebase.ts`.
2. **Backend (FastAPI, `backend/app/`)** — pure compute: carbon, forecast, load intelligence, scheduling, coordination, execution/simulation. No auth. Stateless except `ExecutionStore` (SQLite, `backend/data/heliotrope_execution.db` or `HELIOTROPE_EXECUTION_DB`, 12-hex-char `schedule_id`, in-memory read-through cache).
3. **Firestore** — user profiles, dashboard jobs, public username index. Never touched by the backend except through the pure function `firestore_normalizer` (frontend converts `DashboardJob` -> `LoadSpec` via `lib/api/normalize.ts` before calling the backend).

## Frontend

| Route | Purpose |
|---|---|
| `app/page.tsx` (`/`) | Landing + simulator (calls `scheduleJobs`, `getCarbonSignal`/`getCarbonForecast`, `classifyLoad`). |
| `app/dashboard/page.tsx` (`/dashboard`) | Auth-gated CRUD on `users/{uid}/jobs`, jevRank, `CarbonChart`, `BuildingChart`, `ExecutionPanel` (plan/state/history/events/replan/simulate), `Onboarding`. |
| `app/account/page.tsx` (`/account`) | Profile edit (`Profile{username,occupation,place,rooms,onboarded}`). |
| `app/[username]/page.tsx` (`/[username]`) | Public profile: `usernames/{name}` -> `users/{uid}` -> public fields + jobs summary. |

`lib/api/client.ts` is the single fetch boundary. `lib/api/types.ts` mirrors `backend/app/domain` (`LoadSpec`, `Classification`, `FeasibilityReport`, `ExecutionState`, `CoordinationResult`, carbon types). `null` in `LoadSpec` means UNKNOWN, never zero.

## Backend

`backend/app/main.py` mounts 7 routers under `/api/v1`: `health`, `schedule`, `carbon`, `forecast`, `loads`, `coordination`, `execution`. CORS allows localhost + configured origins. `RequestValidationError` is normalized to `{detail, code: "invalid_request", message}` with HTTP 422.

Layering:

```
routes/ -> services/ -> domain/
  schedule.py      scheduler_service, scheduler_normalizer, carbon_service,
                   forecast_service, schedule_realization
  carbon.py        carbon_service -> providers (synthetic CSV default, Electricity Maps live adapter w/ fallback)
  forecast.py      forecast_service, forecast_backtest
  loads.py         load_intelligence (rule-based, live/offline) + classification,
                   load_normalizer, core/feasibility (validator)
  coordination.py  coordinator (+ coordinated_cpsat) over real optimizer output
  execution.py     execution_store + execution_events + receding + simulator
```

Domain (`backend/app/domain/`): `jobs`, `loads` (`LoadSpec`, `None` = unknown), `horizon`, `carbon`, `forecasting` (`ForecastMode` ACTUAL/EXPECTED/ROBUST), `scheduling`, `coordination`, `execution` (lifecycle), `thermal`, `scaling`.

Services: `carbon_service`, `providers/synthetic` (CSV) + `external` (stub), `carbon_accounting` (single scoring path), `scheduler_normalizer`, `scheduler_service`, `schedulers` (ASAP/GREEDY/CPSAT), `validator`, `forecast*`, `classification`, `load_normalizer`, `load_intelligence` (rule-based, live), `firestore_normalizer` (only Firestore touchpoint, pure function), `coordinator`, `coordinated_cpsat`, `execution_store` (SQLite + dict cache), `execution_events` (forward-only), `receding` (rolling horizon), `simulator` (deterministic, labeled simulation).

## Data flows

### Single-user: onboarding -> jobs -> classify -> plan -> execute

1. Onboarding writes `Profile` to `users/{uid}` (`onboarded: true`).
2. Dashboard writes `DashboardJob{+jobType, energyKwh, durationMin, tempMinC/tempMaxC, confidence}` to `users/{uid}/jobs/{jobId}`.
3. `POST /loads/classify` (free text -> canonical `LoadSpec` + feasibility) then `POST /loads/validate` (fully specified spec -> verdict). Classification never schedules.
4. `POST /schedule` (one-shot, returns `SchedulerResult` + `warnings` + `forecast` summary + optional `realized` scoring) or `POST /schedules/plan` (persists a `ScheduleRecord` v1, lifecycle `SCHEDULED`, returns `ExecutionState`).
5. Live loop on `schedule_id`: `GET state`, `GET history`, `POST events` (may auto-replan under EVENT_DRIVEN/HYBRID), `POST replan` (remaining requirements only, history preserved), `POST tick {now}` (daemon-called; records `CLOCK_ADVANCED`, marks overdue starts `MISSED`, auto-replans with reason `PERIODIC` under PERIODIC/HYBRID), `POST telemetry` (validated meter readings, `MEASURED` source vs `SIMULATED`), `POST override` (guarded: START_NOW/PAUSE/CANCEL/MOVE/RUN_ASAP), `POST /simulation/{id}/advance` (deterministic trace, never telemetry).

### Coordination (multi-user / building)

`POST /coordination/schedule` runs `MultiUserCoordinator.coordinate` over one shared carbon signal and capacity: per-participant schedules + `aggregate_profile` + `congestion_profile` + `metrics` + `fairness_mode`. `POST /coordination/compare` runs INDEPENDENT vs COORDINATED on the same input (both real runs). `POST /schedules/plan-coordinated` persists the coordinated result as an executable `ScheduleRecord` (`context.kind: "coordinated"`).

## Scheduler modes

| Name | Behavior |
|---|---|
| `ASAP` | Earliest-feasible placement, no carbon optimization. |
| `GREEDY` | Cheapest-slot-first heuristic. |
| `CPSAT` | Constraint-programming optimum under `ObjectiveWeights` (+ optional `TimeOfUseTariff`, `ForecastConfig`). |

Unknown name -> 400 `invalid_scheduler`, never substituted. `POST /schedule/compare` runs ASAP/GREEDY/CPSAT (or a requested subset) over unchanged input.

## Forecast modes (`ForecastMode`, `carbon` block on `POST /schedule`)

Absent block = ACTUAL (observed signal, Phase-4 behavior). `mode: FORECAST` builds a forecast and optimizes against it:

| Mode | Meaning |
|---|---|
| `ACTUAL` | Observed signal. |
| `EXPECTED` | Point forecast. |
| `ROBUST` | Upper prediction bound weighted by `risk_weight` (0.0 = point, 1.0 = bound). |

Forecast affects only which feasible schedule is preferred, never feasibility. `deadline_buffer_minutes` is an explicit safety margin. Caller-supplied `actual_signal` triggers a `realized` section scoring the plan against actuals.

## Execution lifecycle

`ScheduleLifecycle`: DRAFT -> SCHEDULED -> ACTIVE -> COMPLETED / PARTIALLY_COMPLETED; any -> CANCELLED; FAILED on unschedulable turn. `JobStatus`: PENDING -> READY -> RUNNING -> COMPLETED, with PAUSED / MISSED / FAILED / CANCELLED branches. `ScheduleEventType` (12 types: carbon updated, job added/removed/started/completed/missed/failed/paused/resumed, capacity changed, user override, clock advanced) is forward-only: events append, versions append (`append_version` only when something changed), replans solve over remaining future via `RecedingHorizon` and never rewrite history. `ReschedulePolicy`: MANUAL / PERIODIC / EVENT_DRIVEN / HYBRID (default). `RescheduleReason`: 11 values (carbon changed, load added/removed, job delayed/failed, capacity change, user override, missed start, system recovery, periodic, manual). `OverrideCommand`: START_NOW / PAUSE / CANCEL / MOVE / RUN_ASAP; rejected overrides are 422 `override_rejected`, illegal transitions 422 `invalid_transition`.

## Firestore schema

| Path | Access | Shape |
|---|---|---|
| `users/{uid}` | public read, owner write | `Profile{username, occupation, place, rooms, onboarded}` |
| `users/{uid}/jobs/{jobId}` | owner-only | `DashboardJob{+jobType, energyKwh, durationMin, tempMinC, tempMaxC, confidence}` |
| `usernames/{name}` | public read, owner-uid-gated write | `{uid}` pointer for `/[username]` resolution |

## Honesty invariants

- `None`/`null` is unknown, never 0; Heliotrope does not guess physical values.
- Synthetic carbon/forecast output is always labeled `SYNTHETIC`/`signal_type`, never grid data. Simulation responses carry `simulated: true`.
- `INFEASIBLE` is HTTP 200 with `status: "INFEASIBLE"` + reason (valid answer, not malformed input).
- Error codes: 400 `invalid_scheduler` / `unknown_model`, 422 `invalid_request` / `invalid_transition` / `override_rejected`, 503 `provider_unavailable`, 404 `not_found` (unknown `schedule_id`).
- Carbon accounting is single-pathed (`carbon_accounting`); forecast uncertainty changes preference, not feasibility; realized CO2 is scored against actuals, never the forecast.
