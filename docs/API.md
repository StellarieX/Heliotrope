# Heliotrope API

Base URL: `process.env.NEXT_PUBLIC_BACKEND_URL` (default `http://localhost:8000`). All paths below are prefixed with `/api/v1` except `GET /`. No auth, no token. Interactive docs at `GET /docs`.

Error shape: `{detail, code, message}` where code is one of `invalid_scheduler`, `unknown_model`, `invalid_request`, `invalid_transition`, `override_rejected`, `provider_unavailable`, `not_found`, `internal_error`, `http_error`. Validation failures (FastAPI request models) are 422 `invalid_request`; `detail` preserves the pydantic error list (arrays for 422s — clients must not assume a string). Response-schema failures are 500 `internal_error`; framework 404s are `not_found`. `INFEASIBLE` results are HTTP 200 with `status: "INFEASIBLE"`.

| # | Method + Path | Request | Response | Errors |
|---|---|---|---|---|
| 1 | `GET /` | — | `{service, docs}` | — |
| 2 | `GET /api/v1/health` | — | `{status: "ok", service, env}` | — |
| 3 | `GET /api/v1/carbon?start&end&resolution_minutes&provider` | query: `start`, `end` (tz-aware ISO-8601; naive datetimes are 422 `invalid_request`), `resolution_minutes` (one of 5/15/30/60, default 15), `provider` (default `synthetic`) | `CarbonSignalResponse{start, end, resolution_minutes, signal_type: MARGINAL\|AVERAGE\|PROXY\|SYNTHETIC, source, points[{timestamp, carbon_intensity_gco2_per_kwh}], quality{complete, missing_points, interpolated_points, source, signal_type, is_forecast}}` | 422 `invalid_request`, 503 `provider_unavailable` |
| 4 | `POST /api/v1/carbon/forecast` | `{start, end, resolution_minutes?, model: "seasonal"\|"persistence", lookback_days?, coverage?, history_days?}` (tz-aware ISO) | `{signal_type: "FORECAST", resolution_minutes, points[{timestamp, predicted_gco2_per_kwh, lower_gco2_per_kwh, upper_gco2_per_kwh}], provenance{model, generated_at, training_window_start/end, training_points, source_signal, source_signal_type, horizon_start/end, resolution_minutes, interval_nominal_coverage, uncertainty_method, configuration?}}` | 400 `unknown_model`, 422 `invalid_request` |
| 5 | `POST /api/v1/carbon/forecast/evaluate` | `{forecast? (inline forecast object), start?, end?, resolution_minutes?, model?, lookback_days?, coverage?, actual: [{timestamp, gco2_per_kwh}]}` — inline `forecast` or `start`+`end` required | `{mae, rmse, coverage, interval_width, ...}` (evaluation metrics dict) | 422 `invalid_request` |
| 6 | `POST /api/v1/carbon/forecast/backtest` | `{history? [{timestamp, gco2_per_kwh}], model?, horizon_hours? (≤168), step_hours? (≤168), resolution_minutes?, lookback_days?, coverage?, max_steps? (≤32), max_history_days? (≤60), variant?, compare_models?}` (defaults to deterministic SYNTHETIC history; bounded) | backtest result dict (per-step metrics + runtime; `compare_models: true` returns both baselines) | 400 `unknown_model`, 422 `invalid_request` |
| 7 | `POST /api/v1/loads/classify` (`provider` is the engine that actually answered: `jev` only when Jev classified this request, else `rule_based`) | `LoadRequest{name, power_kw?, max_power_kw?, duration_minutes?, energy_required_kwh?, min_chunk_minutes?, release_wall?, deadline_wall?, timezone?, job_type?}` | `{provider, classification{name, input, category, job_type, shiftable, confidence, ambiguous, reason, matched_rule, alternatives, required_fields, thermal_example, assumptions}, confidence, ambiguous, assumptions, normalized_load_spec: LoadSpec, feasibility: FeasibilityReport}` | 422 `invalid_request`, 503 `provider_unavailable` |
| 8 | `POST /api/v1/loads/validate` | `LoadSpec` (+ optional `power_profile: number[]` validation aid) | `{feasible, checks_run, errors, warnings, semantics{load_type, job_type, shiftable, decision_variable, primary_requirement, constraints, notes}, explanation, summary, metric_inputs}` — `semantics` exposes both `job_type` and `load_type` | 422 `invalid_request` |
| 8b | `POST /api/v1/loads/prioritize` | `{loads: [{id, name, kind?, power_kw (0..1000), hours_until_ready (0..168), flex_hours?}]}` (1 to 25 loads) | `{provider: "jev"\|"mixed"\|"heuristic", items: [{id, score 0..100, band, reason, source, importance?, importance_label?, importance_confidence?}] (most pressing first), notes[]}`. Jev rates how essential each appliance is (Score, confidence-gated at 0.5); time pressure, size and flexibility are computed here. All loads go to Jev in ONE request. `provider` is `jev` only when every load was Jev-scored, `mixed` when only some were, `heuristic` when none were (unconfigured, unreachable, or answers below the confidence gate); `notes` says why. After an outage Jev is skipped for 30 s so the fallback is immediate | 422 `invalid_request` |
| 9 | `POST /api/v1/schedule` | `{jobs: LoadSpec[] (≥1), capacity_kw (>0), capacity_profile_kw?: number[] (per-slot kW, len == horizon slots; overrides scalar per slot), scheduler: "ASAP"\|"GREEDY"\|"CPSAT" (case-insensitive), objective?, horizon?, tariff?, solver_config?, carbon_provider?, carbon_start?, carbon_end?, carbon_resolution_minutes?, carbon?: {mode, forecast_model, forecast_mode: ACTUAL\|EXPECTED\|ROBUST, risk_weight, deadline_buffer_minutes, lookback_days, coverage, history_days, actual_signal?}, explain?}` | `SchedulerResult` + `warnings[]` + `forecast{mode, ...provenance}` + optional `realized{realized_co2_kg, forecast_expected_co2_kg, forecast_error_kg, ...}`; `status` FEASIBLE/OPTIMAL or INFEASIBLE (200) | 400 `invalid_scheduler`, 422 `invalid_request`, 503 `provider_unavailable` |
| 10 | `POST /api/v1/schedule/compare` | same as #9 + `schedulers?: string[]` | `{results: {ASAP, GREEDY, CPSAT} SchedulerResult, warnings[]}` | same as #9 |
| 11 | `POST /api/v1/coordination/schedule` | `CoordinationRequest{jobs: LoadSpec[] (with participant_id), capacity_kw, capacity_profile_kw?: number[] (per-slot kW, len == horizon slots; overrides scalar per slot), coordination_mode?, fairness_mode?, horizon?, carbon_provider?, carbon_start?, carbon_end?, carbon_resolution_minutes?, ...}` — mismatched profile length is 422 `invalid_request` | `CoordinationResult{status, coordination_mode, participants[{participant_id, inconvenience_score, delay_minutes, jobs_shifted, job_count, co2_kg}], jobs[{participant_id, job_id, name, scheduled_start/end, energy_kwh, power_kw, delay_minutes, carbon_kg, reason}], aggregate_profile[] (capacity_kw is per-slot when a profile is given), congestion_profile[], metrics{total_energy_kwh, total_co2_kg, peak_kw, capacity_violations, total_delay_minutes, worst_inconvenience, participant_count, job_count, solve_time_ms}, fairness_mode, solver_status, reason, signal_provenance}` | 422 `invalid_request`, 503 `provider_unavailable` |
| 12 | `POST /api/v1/coordination/compare` | same as #11 | `{independent: CoordinationResult, coordinated: CoordinationResult}` | same as #11 |
| — | Anti-herding pool (#9, #10, #13, replans) | `share_pool?: bool` (default `false` on #9/#10, `true` on #13), `pool_exclude_schedule_id?` (#9/#10), `replaces_schedule_id?` (#13: cancels the caller's previous plan first) | `pool{applied, active_schedules, peak_pooled_kw, beta}`: aggregate kW only, never ids or names. Other active plans' load raises each slot's objective carbon by a logistic factor in `[1, 1 + POOL_BETA]`; deadlines, capacity and reported CO₂ are unchanged | — |
| 12b | `GET /api/v1/pool/stats?start&end&resolution_minutes` | query: `start`, `end` (tz-aware ISO-8601; naive is 422 `invalid_request`; default now floored to 15 min through +24 h; window at most 7 days), `resolution_minutes` (5/15/30/60, default 15) | `{generated_at, start, end, resolution_minutes, active_schedules, active_loads, total_planned_kwh, peak_kw, peak_at (null when no load), average_kw (mean over slots that carry load, 0 if none), peak_to_average (null when no load; 1.0 is flat, higher is more crowded), slots[{timestamp, kw}] (every slot, zeros included), pool{enabled, beta, ref_kw, scale_kw}}`. The planned load of all live, unfinished schedules in the window (the same pool that tilts new plans away from crowded slots), summed per slot. Aggregates only: no schedule ids, job ids, names or per-user rows; nothing is cached per user. `active_schedules` / `active_loads` count plans / non-finished jobs with load in the window. `start`/`end` echo the slot grid actually used. Planned, not metered | 422 `invalid_request` |
| 13 | `POST /api/v1/schedules/plan` | #9 shape + `execution?: {policy: MANUAL\|PERIODIC\|EVENT_DRIVEN\|HYBRID, horizon_minutes?, reoptimization_interval_minutes?, commitment_window_minutes?, min_shift_minutes?, change_penalty_weight?, improvement_threshold_percent?}` | `ExecutionState{schedule_id (12 hex), lifecycle, version: 1, solver_status, jobs[{job_id, participant_id, status, scheduled_start/end, energy_delivered_kwh, expected_energy_kwh, note}]}` + `warnings` | same as #9 |
| 14 | `POST /api/v1/schedules/plan-coordinated` | #11 shape + `execution?` (as #13) | `ExecutionState` + `coordination: CoordinationResult`; INFEASIBLE building returns the coordination result (200) without creating a record | 422 `invalid_request`, 503 `provider_unavailable` |
| 15 | `GET /api/v1/schedules/{id}/state` | path `schedule_id` | `ExecutionState` (live truth): `{schedule_id, lifecycle, version, solver_status, jobs[{job_id, participant_id, status, scheduled_start, scheduled_end, deadline_at (end of the job's last allowed slot), energy_delivered_kwh, expected_energy_kwh, note, source}]}` | 404 `not_found` |
| 16 | `GET /api/v1/schedules/{id}/history` | path `schedule_id` | `{schedule_id, lifecycle, versions[{version, created_at, reason, solver_status, carbon_estimate_kg, peak_kw, changed_jobs[{job_id, previous_start, new_start, previous_end, new_end, change_minutes, reason}]}]}` | 404 `not_found` |
| 17 | `POST /api/v1/schedules/{id}/events` | `{event_type: CARBON_FORECAST_UPDATED\|JOB_ADDED\|JOB_REMOVED\|JOB_STARTED\|JOB_COMPLETED\|JOB_MISSED\|JOB_FAILED\|JOB_PAUSED\|JOB_RESUMED\|CAPACITY_CHANGED\|USER_OVERRIDE\|CLOCK_ADVANCED, timestamp?, job_id?, participant_id?, payload? (energy_delivered_kwh, delivered_slots)}` | `{state: ExecutionState, replan_advised, notes[], replanned? (auto-replan under EVENT_DRIVEN/HYBRID)}` | 404 `not_found`, 422 `invalid_transition` |
| 18 | `POST /api/v1/schedules/{id}/replan` | `{now?, reason?, capacity_kw?, capacity_profile_kw?: number[] (per-slot kW, len == horizon slots; overrides scalar per slot; scalar-only clears a previous profile), added_jobs?: LoadSpec[], removed_job_ids?: string[]}` | `{replanned, version?, changes?, notes[], frozen_lifted?, status?, error?, state: ExecutionState}` | 404 `not_found`, 422 `invalid_request` |
| 19 | `POST /api/v1/schedules/{id}/override` | `{job_id, command: START_NOW\|PAUSE\|CANCEL\|MOVE\|RUN_ASAP, new_release_at?, new_deadline_at?}` | `{accepted: true, explanation, replanned?, ..., state: ExecutionState}`. PAUSE and CANCEL are recorded without a replan. START_NOW and RUN_ASAP pin the job to start at once: its window closes right after the shortest run that delivers it (not running thermal loads). MOVE needs a window that actually changes (a `new_deadline_at` past the current slot and inside the horizon); otherwise 422. A rejected window leaves the schedule untouched. A MISSED job can be restarted (START_NOW). | 404 `not_found`, 422 `override_rejected` / `invalid_transition` |
| 20 | `POST /api/v1/simulation/{id}/advance` (developer/test tool; the app does not call it) | `{to_time, script?: [], carbon_actual?: []}` | `{simulated: true, trace, state: ExecutionState, metrics{planned_energy_kwh, actual_energy_kwh, planned_co2_kg, realized_co2_kg, job_counts, versions}}` | 404 `not_found` |
| 21 | `POST /api/v1/schedules/{id}/tick` | `{now?}` (daemon calls every N min; no in-process cron) | `{ticked: true, now, periodic_due, replan_advised, replanned?, version?, changes?, notes[], state: ExecutionState}` — records `CLOCK_ADVANCED`, marks overdue starts `MISSED`, auto-replans with reason `PERIODIC` when policy is `PERIODIC`/`HYBRID` and `reoptimization_interval_minutes` elapsed since `last_replan_at` (else version `created_at`); `MANUAL` never auto-replans | 404 `not_found` |
| 22 | `POST /api/v1/schedules/{id}/telemetry` | `{job_id, timestamp?, energy_kwh? (≥0), power_kw? (≥0), source?}` (at least one of `energy_kwh`/`power_kw`; real meter reading, not simulation) | `{accepted: true, source: "MEASURED", event_types[], completed, notes[], replan_advised, state: ExecutionState}` — emits `JOB_STARTED` (+`JOB_COMPLETED` when cumulative energy meets the expected target) via the existing event path; job `source` in state is `MEASURED`, `SIMULATED` otherwise | 404 `not_found`, 422 `invalid_request` / `invalid_transition` |

## Examples

Health:

```bash
curl http://localhost:8000/api/v1/health
# {"status":"ok","service":"heliotrope-backend","env":"..."}
```

Carbon signal:

```bash
curl "http://localhost:8000/api/v1/carbon?start=2026-10-01T00:00:00%2B00:00&end=2026-10-02T00:00:00%2B00:00&resolution_minutes=15&provider=synthetic"
```

Carbon forecast:

```bash
curl -X POST http://localhost:8000/api/v1/carbon/forecast \
  -H 'Content-Type: application/json' \
  -d '{"start":"2026-10-05T00:00:00+00:00","end":"2026-10-06T00:00:00+00:00","resolution_minutes":15,"model":"seasonal","lookback_days":14,"coverage":0.9,"history_days":14}'
```

Classify a load:

```bash
curl -X POST http://localhost:8000/api/v1/loads/classify \
  -H 'Content-Type: application/json' \
  -d '{"name":"run dishwasher tonight"}'
# {"provider":"...","classification":{...},"normalized_load_spec":{...},"feasibility":{...}}
```

Plan (live schedule):

```bash
curl -X POST http://localhost:8000/api/v1/schedules/plan \
  -H 'Content-Type: application/json' \
  -d '{"jobs":[{...LoadSpec...}],"capacity_kw":5,"scheduler":"CPSAT","execution":{"policy":"HYBRID"}}'
# {"schedule_id":"...","lifecycle":"SCHEDULED","version":1,"solver_status":"...","jobs":[...]}
```

State / history / events:

```bash
curl http://localhost:8000/api/v1/schedules/<id>/state
curl http://localhost:8000/api/v1/schedules/<id>/history
curl -X POST http://localhost:8000/api/v1/schedules/<id>/events \
  -H 'Content-Type: application/json' \
  -d '{"event_type":"JOB_STARTED","job_id":"dishwasher-1","timestamp":"2026-10-05T20:00:00+00:00"}'
```

Coordination:

```bash
curl -X POST http://localhost:8000/api/v1/coordination/schedule \
  -H 'Content-Type: application/json' \
  -d '{"jobs":[{...LoadSpec with participant_id...}],"capacity_kw":10,"coordination_mode":"COORDINATED"}'
```

Notes: `LoadSpec` unknown fields are `null` (never 0). Forecast `coverage` is in (0,1) exclusive — boundaries are 422. `resolution_minutes` must be one of 5/15/30/60. Schedule `carbon_start`/`carbon_end` must be both-or-neither; naive datetimes are 422. Frontend calls wrap these via `lib/api/client.ts` (`getHealth`, `getCarbonSignal`, `getCarbonForecast`, `classifyLoad`, `validateLoad`, `scheduleJobs`, `planSchedule`, `getScheduleState`, `getScheduleHistory`, `postScheduleEvent`, `replanSchedule`, `advanceSimulation`, `coordinateBuilding`, `compareCoordination`).
