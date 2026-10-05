# Changelog

## v0.2.1 — Jev, done properly

- **Correction.** v0.2.0 treated Jev as a Gemini wrapper. Jev is TypeSafe AI's System One model, a different provider with a different API. v0.2.0's code would have sent a `JEV_API_KEY` to Google. That Gemini code path is removed entirely; the Jev key can only reach `api.typesafe.ai`.
- **Real Jev integration** (`backend/app/services/jev_client.py`, per docs.typesafe.ai/api): classification asks two Choice questions and uses Jev's calibrated confidence and probabilities (confidence is no longer a constant, and runner-up options become alternatives). Retries only the documented 429/529, caches successes, and caps calls per minute.
- **New `/loads/prioritize`.** Jev rates how essential each appliance is (a Score question, ignored below 0.5 confidence); time pressure, size and flexibility are combined in code with transparent weights. The dashboard's "Rank by priority" uses it, labels Jev-judged rows, and falls back to a labelled heuristic when Jev is unavailable.
- Config: `JEV_API_KEY` (or `TYPESAFE_API_KEY`), `JEV_MODEL`, `JEV_BASE_URL`, `JEV_MAX_CALLS_PER_MIN`. Removed `GEMINI_*`.

## v0.2.0 — Live data, Jev, no simulation, mobile (2026-10-05)

- **Live carbon signal:** new `weather` provider builds an estimated intensity from real solar and wind forecasts (Open-Meteo, no key), labelled `PROXY`/`ESTIMATED`. `render.yaml` selects it. The default test curve is no longer what the app shows.
- **Forecast bug fixed:** the forecast always trained on SYNTHETIC history because it asked for 14 days and a single query is capped at 7. History is now read in windows, so the forecast uses the real provider's data.
- **No simulation in the product:** removed the simulated clock (`+15 min`), the demo building data, and the landing page's hand-drawn simulator, invented "pilot" results and fixed-time timeline. The landing page now shows live solver output. The backend `/simulation` endpoint remains as a developer tool.
- **Real inputs:** "+Nh flexible" now sets the latest finish time (it was stored but never reached the planner). Energy for interruptible loads and run length for atomic loads are asked for instead of assumed; loads missing them are left out of the plan with a prompt on the row.
- **Shared capacity** now schedules the user's own loads (each its own participant) and refreshes automatically.
- **Mobile:** 16px gutters, 44px tap targets, no horizontal overflow, wrapping tracker rows, accessible onboarding on small screens.
- **Fixed:** a server/client hydration mismatch on `/dashboard`, `/account` and `/[username]` that flashed the sign-in screen for signed-in users.

## v0.1.1 — Demo hardening: privacy, rules, onboarding and dashboard UX (2026-10-05)

- **Privacy:** profile documents are world-readable, so they no longer store the user's email. Existing profiles are cleaned automatically the next time the owner opens the dashboard or account page (`lib/profile.ts`).
- **Firestore rules:** profile writes are validated against a field allow-list (name, photo, username, occupation, place, rooms, onboarded). Usernames must match `^[a-z0-9_]{3,20}$` and cannot be a reserved route name, enforced server-side instead of only in the browser. Verified in the real Firestore emulator (`tests/rules/rules.test.mjs`, 33 cases), not just the Python rule models.
- **Onboarding:** live username availability check; power rating required for every load; quick-add presets; Enter submits; loads and the `onboarded` flag are saved in one atomic batch (a failed save can no longer duplicate loads); labelled, keyboard-friendly dialog.
- **Dashboard:** step tracker (add loads → plan → track); backend status pill and cold-start warm-up (`waitForBackend`); "Re-plan from my loads" (previously impossible once a plan existed); stale-plan warning that survives reloads; backend-computed "impact vs running everything now" card (`/schedule/compare`) with each load's run windows; load names instead of database IDs in the tracker; profile/load read failures no longer re-trigger onboarding; time-of-day greeting; honest data-source cards replace placeholder tiles.
- **Account:** deletion re-authenticates before removing data; username claim reuses `lib/username.ts`.
- **Ops:** `render.yaml` (backend on Render), `firebase.json` (rules + emulators), `NEXT_PUBLIC_USE_FIREBASE_EMULATOR` for fully local runs, react/react-dom bumped together to 19.3.0 and grouped in Dependabot (a lone react-dom bump broke installs). Removed `backend/make_commits.sh` and the unused Next.js starter images.

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
- Gemini load-classification adapter (live `httpx` call when `GEMINI_API_KEY` — legacy alias `JEV_API_KEY`, `GEMINI_API_KEY` wins — is set); falls back to rule-based classifier, recorded as an assumption, on any failure. *(superseded by the real Jev integration in v0.2.1)*
- Tick + telemetry endpoints (`POST /schedules/{id}/tick`, `POST /schedules/{id}/telemetry` with `MEASURED` vs `SIMULATED` source) and time-varying `capacity_profile_kw` on `/schedule`, coordination, and replan.

## M5 — shipped (Tier 5)

- Tier 5 hardening suite: `tests/e2e/test_tier5_hardening.py` (10 tests: thermal abuse, transition guards, over-capacity, open-access shape, meter/capacity-profile validation, unknown model, tick 404, empty-MOVE no-op), wired into `tests/e2e/runner.py` (`TIER5: 10/10`) + `test_tier5_pass` bridge.

## Audit + deploy hardening — shipped

- Frontend: `lib/api/client.ts` trims trailing slash on `NEXT_PUBLIC_BACKEND_URL`, central `getJson`/`postJson` with `cache: no-store` + 30s `AbortSignal.timeout`, `encodeURIComponent` schedule ids with empty-id guard, `toHttpError` handles JSON-array 422 detail / text / HTML / empty bodies; non-JSON success throws. `lib/firebase.ts` requires all 4 vars + SSR `window` guard. `lib/username.ts` holds `RESERVED_USERNAMES` and shared `validUsername`. Dashboard: hoisted coordination default, optimistic `removeJob` with restore, unmount-cancelled effects, `aria-label`s, Onboarding re-validation.
- Backend API: credentialed loopback CORS regex + explicit origins (`"*"` gets credential-less middleware); handlers for `RequestValidationError` (422), `ResponseValidationError` (500), `StarletteHTTPException` (404 `not_found`), generic `Exception` (500); all route errors include `message`. Timezone: naive datetimes rejected 422 (schedule carbon start/end both-or-neither, `Z`-suffix coerced, finite capacity/resolution 5|15|30|60, `LoadSpec`/`Job`/`PlacedJob` finite checks, `end >= start`). `ValidateResponse` semantics exposes both `job_type` and `load_type`.
- Schedulers: thermal idle-step simulation, contention-aware thermal placement, contiguous-prefix interruptible allocation with min-chunk repair, non-negative CP-SAT delay term, tail-start forbidden, timeout -> `UNKNOWN`/`INTERNAL_ERROR` with solver reset, deadline-at-horizon-end fixed, robust carbon clamped >= 0.
- Providers: CSV finite-value rejection + 5MB/100k caps + regular-file check; external NaN rejection + 1 retry on transport error; synthetic phases in `__init__`; backtest provenance reuse; LLM confidence 0.6 `ambiguous` threshold; naive->UTC load normalization with warning; Firestore normalizer tz warning + numeric sanitize; execution store `Lock` + WAL + 30s timeout; validated execution events; carbon service prune + 512 cap + lock; locked meter provider.
- Deploy E2E: `next.config.ts` rewrites `/api/v1/:path*` to `BACKEND_URL` (server-only) only when set; empty `NEXT_PUBLIC_BACKEND_URL` = same-origin mode. `vercel.json` (`iad1`, no-store on `/api/v1`). Backend Dockerfile (root-context, non-root `appuser`, `HEALTHCHECK /api/v1/health`), runtime-only `requirements.lock`, Vercel-host auto-trust in `config.py`, `uid is string` Firestore guard, `HELIOTROPE_E2E_BASE_URL` live test, CI `e2e-and-rules` job with pinned versions + `py_compile`.
- Verification: `npm run build` passes (5 routes), `tsc`/`eslint` clean, backend ~4693 pass, e2e 162 pass.
