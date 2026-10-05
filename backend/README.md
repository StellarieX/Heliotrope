# Heliotrope Backend

FastAPI service: scheduling (ASAP/greedy/CP-SAT), carbon signals, forecasting,
load classification/validation, multi-user coordination, execution +
simulation. Title `Heliotrope Backend` (`app/main.py:16`).

## Run

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # from backend/
.venv/Scripts/python -m uvicorn app.main:app --app-dir backend --port 8000
```

Docs: `http://localhost:8000/docs`.

## Config

All from environment (`app/core/config.py:1`):

| Variable | Default | Notes |
|---|---|---|
| `HELIOTROPE_ENV` | `development` | non-production allows loopback CORS ports |
| `PORT` | `8000` | |
| `CORS_ALLOW_ORIGINS` | empty | comma-separated; required in production |
| `CARBON_PROVIDER` | `synthetic` | synthetic seed 7 unless overridden |
| `CARBON_CSV_PATH` | empty | required for csv provider |
| `CARBON_MAX_RANGE_DAYS` / `CARBON_CACHE_TTL_S` / `CARBON_SYNTHETIC_SEED` | `7` / `300` / `7` | tuning |
| `LOAD_INTELLIGENCE_PROVIDER` | `rule_based` | `jev` honored only with a verified contract |
| `JEV_API_KEY` / `ELECTRICITY_MAPS_API_KEY` | unset | reserved, never sent to the browser |

## Endpoints

21 endpoints under `/api/v1` (routers in `app/main.py:63`):

- `GET /health`
- `GET /carbon`, `POST /carbon/forecast`, `/carbon/forecast/evaluate`, `/carbon/forecast/backtest`
- `POST /loads/classify`, `POST /loads/validate`
- `POST /schedule`, `POST /schedule/compare`
- `POST /coordination/schedule`, `POST /coordination/compare`
- `POST /schedules/plan`, `POST /schedules/plan-coordinated`,
  `GET /schedules/{id}/state`, `GET /schedules/{id}/history`,
  `POST /schedules/{id}/events`, `POST /schedules/{id}/replan`,
  `POST /schedules/{id}/tick`, `POST /schedules/{id}/telemetry`,
  `POST /schedules/{id}/override`, `POST /simulation/{id}/advance`

Time-driven scheduling: no in-process cron. A client daemon calls
`POST /schedules/{id}/tick {now}` every N minutes; the tick records
`CLOCK_ADVANCED`, marks overdue starts `MISSED`, and auto-replans via the
same replan path with reason `PERIODIC` when the policy is
`PERIODIC`/`HYBRID` and `reoptimization_interval_minutes` has elapsed since
`last_replan_at` (else the current version's `created_at`). `MANUAL` never
auto-replans.

Real telemetry: `POST /schedules/{id}/telemetry {job_id, timestamp?,
energy_kwh? (≥0), power_kw? (≥0)}` ingests validated meter readings
(`app/services/meter_provider.py:1`) alongside the deterministic simulator
(`SIMULATED`). Readings reuse the event path (`JOB_STARTED`/`JOB_COMPLETED`
+ `energy_delivered_kwh`); jobs with measured data report `source:
"MEASURED"` in state, others `"SIMULATED"`. No MQTT/OCPP/Modbus driver is
bundled yet — drivers push through this endpoint.

No auth; CORS only. Invalid bodies return 422 with `code:
invalid_request` (`app/main.py:39`).

Time-varying shared capacity: `SharedResource.capacity_profile_kw` (per-slot
kW, length == horizon slots) overrides the scalar `capacity_kw` per slot; the
scalar remains the default. Enforced as a hard per-slot constraint in the
coordinated CP-SAT model, the validator, and the simulator's capacity check;
a length mismatch is 422 `invalid_request`. The replan body accepts the same
`capacity_profile_kw` (scalar-only replan clears a previous profile).

## Tests

```bash
.venv/Scripts/python -m pytest tests -q   # from backend/ (38 files, configured in pyproject.toml:17)
```

## Warning

The execution store is SQLite-backed (`backend/data/heliotrope_execution.db`,
overridable via `HELIOTROPE_EXECUTION_DB`, with an in-memory read-through
cache): planned schedules, versions, and simulation state survive restarts.
Mount/persist the DB file as a volume in hosted deploys; do not treat an
ephemeral container filesystem as durable storage.
