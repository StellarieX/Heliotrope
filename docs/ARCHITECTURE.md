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
| `app/page.tsx` (`/`) + `app/LivePlan.tsx` | Landing with a live demo: real carbon signal (`getCarbonSignal`) and an ASAP-vs-CP-SAT comparison (`compareSchedulers`) on example loads, recomputed as the flexibility slider moves. No hand-drawn numbers. |
| `app/dashboard/page.tsx` (`/dashboard`) | Auth-gated CRUD on `users/{uid}/jobs`, jevRank, `CarbonChart`, `BuildingChart`, `ExecutionPanel` (plan/state/history/events/replan; progress is user-reported), `Onboarding`. Shared logic: `lib/loads/specs.ts` (load -> LoadSpec; latest finish = ready-by + flexibility; missing energy/duration is asked for, never assumed) and `lib/loads/useClassification.ts` (backend classification, AI or rules). |
| `app/account/page.tsx` (`/account`) | Profile edit (`Profile{username,occupation,place,rooms,onboarded}`). |
| `app/[username]/page.tsx` (`/[username]`) | Public profile: `usernames/{name}` -> `users/{uid}` -> public fields + jobs summary. |

`lib/api/client.ts` is the single fetch boundary: `BASE` trims trailing slashes on
`NEXT_PUBLIC_BACKEND_URL`; all calls use `cache: "no-store"` with a 30s
`AbortSignal.timeout`. Schedule-id paths go through an empty-id guard +
`encodeURIComponent`. `toHttpError` handles FastAPI 422 JSON-array `detail`,
plain-text/HTML bodies, and empty bodies; non-JSON success bodies throw.
`lib/firebase.ts` requires all four `NEXT_PUBLIC_FIREBASE_*` vars and guards
`window` for SSR. `lib/username.ts` holds `RESERVED_USERNAMES` and the shared
`validUsername` used by both `app/[username]` and `app/account`. Dashboard:
hoisted coordination default, optimistic `removeJob` with restore on failure,
unmount-cancelled effects, `aria-label`s, Onboarding re-validation.
`lib/api/types.ts` mirrors `backend/app/domain` (`LoadSpec`, `Classification`,
`FeasibilityReport`, `ExecutionState`, `CoordinationResult`, carbon types).
`null` in `LoadSpec` means UNKNOWN, never zero.

## Backend

`backend/app/main.py` mounts 7 routers under `/api/v1`: `health`, `schedule`, `carbon`, `forecast`, `loads`, `coordination`, `execution`. CORS: credentialed loopback regex in non-production plus explicit origins; a configured `"*"` gets a separate credential-less middleware (credentials + wildcard is rejected by browsers). Handlers: `RequestValidationError` -> 422 `{detail, code: "invalid_request", message}`; `ResponseValidationError` -> 500 `internal_error`; `StarletteHTTPException` -> same status with `not_found` for 404; generic `Exception` -> 500 `internal_error`. All route errors include `message`.

Layering:

```
routes/ -> services/ -> domain/
  schedule.py      scheduler_service, scheduler_normalizer, carbon_service,
                   forecast_service, schedule_realization
  carbon.py        carbon_service -> providers (weather proxy [live, keyless], csv, external [Electricity Maps history], synthetic [test curve])
  forecast.py      forecast_service, forecast_backtest
  loads.py         load_intelligence (rule-based, live/offline) + classification,
                   load_normalizer, core/feasibility (validator)
  coordination.py  coordinator (+ coordinated_cpsat) over real optimizer output
  execution.py     execution_store + execution_events + receding + simulator
```

Domain (`backend/app/domain/`): `jobs`, `loads` (`LoadSpec`, `None` = unknown), `horizon`, `carbon`, `forecasting` (`ForecastMode` ACTUAL/EXPECTED/ROBUST), `scheduling`, `coordination`, `execution` (lifecycle), `thermal`, `scaling`.

Services: `carbon_service`, `providers/synthetic` (CSV) + `external` (stub), `carbon_accounting` (single scoring path; `co2_cost` may be `None`), `scheduler_normalizer`, `scheduler_service`, `schedulers` (ASAP/GREEDY/CPSAT), `validator`, `forecast*`, `classification`, `load_normalizer` (naive datetimes -> UTC with warning; inverted windows raise), `load_intelligence` (rule-based, live; LLM results below 0.6 confidence are `ambiguous: True`), `firestore_normalizer` (only Firestore touchpoint, pure function; warns on naive datetimes, sanitizes non-numeric energy), `coordinator`, `coordinated_cpsat`, `execution_store` (SQLite + dict cache, `Lock` + WAL + 30s timeout), `execution_events` (forward-only, validated energy), `receding` (rolling horizon), `simulator` (deterministic, labeled simulation; pause/resume keeps energy correct), `carbon_service` (provenance pruned, 512-entry cap, locked), `meter_provider` (locked, naive timestamps -> UTC).

Provider hardening: CSV rejects non-finite values, caps files at 5MB / 100k rows, and requires a regular file; external rejects NaN and retries once on `TransportError`; synthetic builds phases in `__init__`; backtest reuses signal provenance.

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

Solver honesty and speed: a CP-SAT result is `OPTIMAL` only when optimality was proved; one that stopped inside `relative_gap_limit` is `FEASIBLE` and carries `solver.relative_gap` / `optimality_gap`. `SOLVER_RELATIVE_GAP` sets a deployment-wide tolerance (default 0 = strict). Replans pass the previous schedule to CP-SAT as advisory hints (`SchedulerInput.hints`, keyed by timestamp, excluded from serialization, `repair_hint` on): hints cannot change the optimum, only where the search starts. Measured on a household with a water heater, hints alone and extra workers did not help (the bottleneck is the optimality proof, not the search), while a 0.01% tolerance cut solve time from 12s to 0.17s; see `backend/bench_solver.py`.

Scheduler internals: base `_simulate_thermal` steps idle slots; `place_thermal_control` is contention-aware; `allocate_interruptible` takes a contiguous prefix with min-chunk repair; CP-SAT uses a non-negative delay term, forbids tail-start, and maps timeout to `UNKNOWN`/`INTERNAL_ERROR` with `_last_solver` reset; deadline-at-horizon-end is handled; robust carbon is clamped `>= 0`.

Timezone rule: naive datetimes are rejected with 422. Schedule `carbon_start`/`carbon_end` must be both-or-neither; a `Z` suffix is coerced to an offset. Finite checks (`math.isfinite`) apply to capacity, `resolution_minutes` (allowed set `5|15|30|60`), `LoadSpec`/`Job`/`PlacedJob` numerics, and `PlacedJob` requires `end >= start`.

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
| `users/{uid}` | public read, owner write | `Profile{username, displayName, photoURL, occupation, place, rooms, onboarded}` (no email; allow-listed by the rules) |
| `users/{uid}/jobs/{jobId}` | owner-only | `DashboardJob{+jobType, energyKwh, durationMin, tempMinC, tempMaxC, confidence}` |
| `usernames/{name}` | public read, owner-uid-gated write | `{uid}` pointer for `/[username]` resolution |

## Honesty invariants

- `None`/`null` is unknown, never 0; Heliotrope does not guess physical values.
- Carbon output is always labelled by `signal_type`: the weather provider is `PROXY` (an estimate from real weather), the test curve `SYNTHETIC`. A forecast signal is `FORECAST` (or stays `SYNTHETIC` when the history it learned from was synthetic). In `carbon.mode: FORECAST` the solver minimizes the objective carbon (point forecast for EXPECTED, `forecast + risk_weight*(upper-forecast)` for ROBUST) while reported CO2 and `co2_saved_*` are computed on the predicted series; `metrics.co2_basis`, `signal.basis` and `forecast.co2_basis` say `FORECAST` (versus `OBSERVED`), and `carbon.actual_signal` yields realized CO2 in `realized`. Forecast history is read in windows under `CARBON_MAX_RANGE_DAYS`; if the provider is down it falls back to a labelled synthetic history. The `/simulation` endpoint is a developer/test tool and is not used by the app; its responses carry `simulated: true`.
- `INFEASIBLE` is HTTP 200 with `status: "INFEASIBLE"` + reason (valid answer, not malformed input).
- Error codes: 400 `invalid_scheduler` / `unknown_model`, 422 `invalid_request` / `invalid_transition` / `override_rejected`, 503 `provider_unavailable`, 404 `not_found` (unknown `schedule_id`).
- Carbon accounting is single-pathed (`carbon_accounting`); forecast uncertainty changes preference, not feasibility; realized CO2 is scored against actuals, never the forecast.
