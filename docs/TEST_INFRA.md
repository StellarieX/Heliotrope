# TEST_INFRA — Heliotrope E2E Test Infrastructure

## Overview
This document defines the End-to-End (E2E) testing infrastructure for the Heliotrope platform. It establishes the testing methodology, tier architecture, directory layout, execution commands, and verification criteria across the 10 core E2E features (Features 1 through 10, requirements R1–R4).

> Scope note (2026-10-05): `PROJECT.md` lists 12 features total. F11 (Hypothesis zero-regression, M4) and F12 (E2E verification incl. Tier 5, M5) are out of scope for Tiers 1–4 and tracked separately. See Tier 5 placeholder below.

---

## Testing Principles & Integrity Guardrails
1. **Opaque-Box Verification**: Tests validate observable contracts, API payloads, status codes, schemas, and persistence behavior from the caller's perspective rather than asserting internal implementation details.
2. **Authoritative Specification Sources**:
   - Requirements (R1 Security & Identity, R2 UI & Form Integrity, R3 Persistence & Session Durability, R4 Live External Adapters).
   - Technical specifications & interface contracts (Features 1 through 10 in E2E scope; F11 Hypothesis zero-regression and F12 E2E/Tier 5 tracked separately).
3. **No Facade Tests**: Every test exercises real logic, real domain models, real schedulers (or solver pipelines), real HTTP routes, real security rules logic, and real serialization/persistence mechanics.
4. **Progressive Testability & Hermetic Isolation**: Tests set up their own temporary test data and clean environment overrides. Isolation limits (verified 2026-10-05 in `tests/e2e/conftest.py`): `client` fixture is session-scoped `TestClient` (shared app instance); `clean_execution_store` is function-scoped `ExecutionStore()` but defaults to the shared `backend/data/heliotrope_execution.db` path unless overridden — full per-test SQLite isolation is NOT yet wired. E2E persistence assertions run against the real store code path but do not prove cross-restart durability on an isolated DB file.

---

## 4-Tier Test Architecture

```
+-------------------------------------------------------------------------+
|                  TIER 4: Real-World Application Scenarios               |
|            (Realistic multi-step diurnal & multi-tenant workflows)      |
+-------------------------------------------------------------------------+
|                TIER 3: Cross-Feature Combinations                       |
|           (Pairwise interactions, cross-subsystem state pipelines)       |
+-------------------------------------------------------------------------+
|                TIER 2: Boundary & Corner Cases                          |
|         (>=5 per feature: nulls, overflows, clock drifts, bad tokens)   |
+-------------------------------------------------------------------------+
|                TIER 1: Feature Coverage (Core Functional)               |
|             (>=5 per feature: happy paths, baseline contracts)          |
+-------------------------------------------------------------------------+
```

### Tier 1: Feature Coverage (50 Tests)
Provides baseline functional coverage for each of the 10 core features (>= 5 test cases per feature):
- **Feature 1: Public Profile Route Access** (F1.1 – F1.5): Public read access, missing user handling, claim resolution, valid profile schema, case-insensitive normalization.
- **Feature 2: Username Claim Ownership Security** (F2.1 – F2.5): Owner claim creation, unauthenticated rejection, cross-user claim rejection, owner claim updates, non-owner update/deletion prevention.
- **Feature 3: Multi-User Coordination Panel** (F3.1 – F3.5): Multi-building load coordination, aggregate power profiles, flexible demand curves, capacity limits compliance, coordination comparison.
- **Feature 4: Carbon Forecast Mode & Interval Controls** (F4.1 – F4.5): ACTUAL mode, EXPECTED mode with prediction intervals, ROBUST mode with risk adjustment, schedule optimization with forecast mode, horizon forecast verification.
- **Feature 5: Thermal Comfort Boundaries & Scheduling** (F5.1 – F5.5): Thermal load spec creation, temperature comfort band adherence, thermal job scheduling (not skipped), thermal + atomic joint scheduling, dynamic temperature progression.
- **Feature 6: Load Form Parameter Preservation** (F6.1 – F6.5): Custom energy kWh preservation, custom duration minutes preservation, sub-hour fractional duration preservation, omission fallback behavior, assumption strings suppression.
- **Feature 7: Backend Execution State Persistence** (F7.1 – F7.5): Schedule record creation in store, retrieval from store, event recording, version progression, reload from SQLite storage across restart.
- **Feature 8: Client Live Session Durability** (F8.1 – F8.5): LocalStorage active ID storage, mount rehydration, missing/404 schedule cache eviction, active ID switching, timeline reconstruction from persisted state.
- **Feature 9: Live Grid Carbon Provider Adapter** (F9.1 – F9.5): Live Electricity Maps API call parsing, synthetic fallback when unconfigured, network timeout graceful fallback, zone code configuration, carbon signal schema validation.
- **Feature 10: Live NL Load Classification Adapter** (F10.1 – F10.5): Live LLM classification parsing, rule-based fallback when unconfigured, malformed JSON fallback, complex appliance description parsing, classification integration with load normalization.

### Tier 2: Boundary & Corner Cases (50 Tests)
Tests edge cases, boundary limits, extremal inputs, and security constraints (>= 5 test cases per feature):
- **Feature 1**: Empty username string, Unicode/punctuation in username, unauthenticated read with special characters, deleted user dangling claim, extremely long usernames.
- **Feature 2**: Malicious UID spoofing in payload, empty auth token, concurrent claim collision, casing collision, delete non-existent username claim.
- **Feature 3**: Zero flexible load capacity, capacity equals aggregate baseline, 100+ buildings stress test, overlapping deadlines with zero slack, negative capacity validation.
- **Feature 4**: Zero lookback days, coverage probability boundary (0.01 and 0.99), inverted carbon signals, extreme risk weight (10.0), missing historical carbon points.
- **Feature 5**: Inverted comfort band (min > max), initial temperature outside band, zero ambient loss rate, extreme heating power (100 kW), tiny comfort band (0.1°C tolerance).
- **Feature 6**: Extreme energy values (0.001 kWh and 10,000 kWh), 1-minute duration, duration exceeding deadline, mismatched energy/power/duration relations, floating point precision preservation.
- **Feature 7**: DB file corruption/missing directory handling, concurrent SQLite writes, empty event payloads, rapid version append cycles, schema migrations / unknown fields in stored JSON.
- **Feature 8**: Corrupted JSON in localStorage, expired schedule ID in localStorage, rapid browser reload simulation, storage quota exceeded handling, multi-tab storage synchronization.
- **Feature 9**: HTTP 401 Unauthorized from Electricity Maps, HTTP 429 Rate Limit from Electricity Maps, empty points list in provider response, discontinuous timestamps in carbon data, extreme carbon intensity values (0 gCO2 and 2000 gCO2).
- **Feature 10**: Empty input text string, adversarial prompt injection input ("Ignore previous instructions..."), non-English input, 5,000-character description, ambiguous appliance ("mystery box with a plug").

### Tier 3: Cross-Feature Combinations (15 Tests)
Validates pairwise subsystem interactions and integration interfaces:
- **T3.01**: F1 + F2 — Username claim lifecycle and public profile accessibility after claim modification.
- **T3.02**: F3 + F4 — Multi-user building coordination evaluated under ROBUST carbon forecast mode.
- **T3.03**: F3 + F5 — Multi-user coordination including residential thermal appliances (water heaters & heat pumps).
- **T3.04**: F3 + F6 — Multi-user coordination preserving custom user-specified kWh and duration inputs across all buildings.
- **T3.05**: F4 + F5 — Thermal appliance scheduling optimized against ROBUST forecast prediction intervals.
- **T3.06**: F4 + F7 — Schedule planned under EXPECTED carbon forecast stored in durable SQLite and reloaded intact.
- **T3.07**: F5 + F6 — Thermal appliance with custom energy requirements and comfort bands scheduled without data loss.
- **T3.08**: F5 + F7 — Thermal execution state and simulated temperature trajectory persisted and reloaded across restarts.
- **T3.09**: F6 + F7 — Custom user kWh and duration inputs planned, persisted to SQLite, and verified in reloaded schedule record.
- **T3.10**: F7 + F8 — Backend process restart followed by frontend localStorage rehydration and execution timeline recovery.
- **T3.11**: F9 + F4 — Grid carbon adapter fallback output piped directly into carbon forecast uncertainty estimation.
- **T3.12**: F10 + F6 — Natural language load classification output feeding into preserved load specification parameters.
- **T3.13**: F10 + F5 — Natural language classifier detecting thermal appliances and configuring comfort boundaries.
- **T3.14**: F9 + F7 — Schedules planned with external/fallback carbon data persisted to SQLite and reloaded.
- **T3.15**: F3 + F7 — Coordinated multi-building schedule planned via `/api/v1/schedules/plan-coordinated` persisted and reloaded.

### Tier 4: Real-World Application Scenarios (5 Tests)
Realistic multi-step end-to-end operational workflows:
- **T4.01**: *Multi-Tenant Smart Commercial Building Diurnal Cycle* — Morning baseline, afternoon peak, coordinated EV fleet + HVAC pre-cooling under ROBUST forecast, evening curtailment, execution events, and persistence across server restart.
- **T4.02**: *Residential Prosumer Carbon-Aware Dynamic Day* — Morning EV plug-in (custom 14 kWh), water heater comfort band (48°C–65°C), solar duck curve forecast, mid-day cloud cover event triggering replan, browser reload rehydrating state.
- **T4.03**: *Community Microgrid Emergency Demand Curtailment* — Grid operator capacity drop from 50 kW to 20 kW, user override commands (start now, pause, cancel), recalculating schedule, and verifying audit events in SQLite.
- **T4.04**: *Natural Language Appliance Onboarding to Execution* — User enters natural language strings for EV charger and heat pump, system classifies loads, user sets comfort band, scheduler generates plan preserving exact values, and execution tracks delivered energy.
- **T4.05**: *External Grid Outage & Fallback Resilience Workflow* — Electricity Maps API failure during morning forecast -> graceful fallback to synthetic duck curve -> schedule generated -> saved to durable store -> browser reload rehydrates -> execution completed.

### Tier 5: Adversarial Stress Hardening (NOT STARTED — placeholder)
Reserved for M5 Phase 2 / PROJECT.md F12. Planned coverage (not yet implemented in `tests/e2e/runner.py`, which runs Tiers 1–4 only):
- Negative/excessive thermal comfort bounds, rapid event transitions (`JOB_STARTED` -> `JOB_FAILED` -> `REPLAN`), overlapping participant demands exceeding connection capacity.
- Existing related files (NOT wired into `runner.py` TIERS): `tests/e2e/test_m1_adversarial_challenger.py`, `tests/test_firestore_rules_challenge.py` — inventory only, execution status unverified.

---

## Directory & File Layout
```
Heliotrope-main/
├── docs/TEST_INFRA.md              # This infrastructure specification
├── tests/
│   └── e2e/
│       ├── __init__.py
│       ├── conftest.py             # Test fixtures, TestClient, FirestoreRulesEvaluator (regex), LocalStorageSimulator (5 MB), make_load_spec (fixed 2026-10-06)
│       ├── test_tier1_features.py  # 50 Tier 1 tests (5 per feature x 10 features)
│       ├── test_tier2_boundaries.py# 50 Tier 2 tests (5 per feature x 10 features)
│       ├── test_tier3_combinations.py # 15 Tier 3 cross-feature tests (full names: test_t3_01_f1_f2_claim_lifecycle_and_profile_access, …)
│       ├── test_tier4_scenarios.py # 5 Tier 4 real-world scenario tests (test_t4_01 commercial diurnal, test_t4_02 prosumer day, test_t4_03 microgrid curtailment, test_t4_04 NL onboarding, test_t4_05 outage fallback)
│       ├── test_m1_adversarial_challenger.py # Adversarial challenger (NOT in runner.py TIERS — Tier 5 candidate)
│       └── runner.py               # Standalone test runner with tier-by-tier reporting (TIERS 1–4: 50+50+15+5; no Tier 5 entry)
├── tests/
│   ├── test_firestore_rules_challenge.py # Firestore rules challenge (NOT in runner.py TIERS — Tier 5 candidate)
└── backend/
    └── tests/
        └── test_e2e_requirements.py # Pytest bridge to run E2E suite within backend test suite
```

---

## How to Run the Tests

### Primary Command (Pytest)
```bash
python -m pytest tests/e2e -v
```

### Structured Tier Runner (Comprehensive CLI Report)
```bash
python tests/e2e/runner.py
```

### Pytest within Backend Directory
```bash
python -m pytest backend/tests/test_e2e_requirements.py -v
```

---

## Quality & Exit Criteria
- **100% Pass Rate**: every test in Tiers 1–4 executes and passes (the runner prints the live count).
- **Zero Flakiness**: Deterministic execution without timing races or hardcoded wall-clock sleeps.
- **Zero Side Effects**: All SQLite test databases and mock environments clean up completely upon test completion.

### Correction appended 2026-10-05 (supersedes isolation/persistence claims above, history preserved)
- "Zero Side Effects" is aspirational, not verified: `client` is session-scoped (shared), and `clean_execution_store` uses the default shared DB path (`backend/data/heliotrope_execution.db` via `backend/app/services/execution_store.py:41,54-62`) unless a test overrides `db_path`. Per-test isolated SQLite files are not wired.
- SQLite durability in E2E (F7 reload-across-restart, e.g. T3.06/T3.10) is exercised through the real `ExecutionStore` code path (which does implement SQLite persistence — `execution_store.py:19,43-46,60-78`), but the "across restart" leg is simulated within one process (new store instance / API round-trip), not a process restart with an isolated DB file.
- localStorage durability in E2E (F8/T3.10/T4.02/T4.05) uses `LocalStorageSimulator` (5 MB mock in `conftest.py`), not a real browser `localStorage`; real mount-rehydration in `app/dashboard/page.tsx` remains M3 frontend work to verify.
- External adapters in E2E (F9/F10) test the fallback/rule-based path: `ExternalProvider.get_signal` raises `ProviderNotConfigured`/`ProviderNotIntegrated` by design (`backend/app/services/providers/external.py:34-46`, no `httpx` GET); `JevLoadIntelligence` always refuses (`backend/app/services/load_intelligence.py:83-95`). Live `httpx` integration is M4 work.
- `forecast_service.py:162` and `receding.py:321` bare `pass` lines are narrow `except`/threshold fall-throughs inside implemented functions, not missing implementations. `forecasting.py:589,609` `NotImplementedError`s are documented future-model boundaries (`MLCarbonForecaster`, `CarbonFeatureBuilder`), not regressions.

---

## Tier 5: Adversarial Hardening (appended 2026-10-05, existing content above preserved)

- **Scope**: opaque-box abuse tests against the public HTTP API — hostile/nonsense input must yield the structured error vocabulary (400/404/422, INFEASIBLE-as-200), never a 500.
- **File**: `tests/e2e/test_tier5_hardening.py` — 10 fast tests via `TestClient(app)` (same fixtures as Tier 1): inverted thermal band, duplicate COMPLETED idempotency, START-after-COMPLETE 422, over-capacity INFEASIBLE, open-access/foreign-id 404 shape, negative meter energy 422, capacity-profile length mismatch 422, unknown forecast model 400, tick-on-unknown-id 404, empty MOVE no-op.
- **Runner**: `tests/e2e/runner.py` runs Tier 5 separately after Tiers 1-4 and prints a `TIER5: X/10` line; the `TOTAL: 120/120` line still covers Tiers 1-4 only (bridge contract frozen).
- **Bridge**: `backend/tests/test_e2e_requirements.py::test_tier5_pass` runs the Tier 5 file standalone and asserts `10 passed`.
- **Known behavior codified**: duplicate `JOB_COMPLETED` is an idempotent no-op (200, still COMPLETED) per the same-state guard in `backend/app/services/execution_events.py:53`; backend is open-access by design (plan needs no auth; foreign ids are structured 404s).
