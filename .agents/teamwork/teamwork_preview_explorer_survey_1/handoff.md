# Handoff Report: Survey Explorer 1 (Security & UI Explorer)

**Date**: 2026-10-05T11:25:00Z  
**Author**: Survey Explorer 1 (`teamwork_preview_explorer_survey_1`)  
**Scope**: Requirements R1 (Security & Identity Guardrails) and R2 (UI Feature Completion & Form Integrity)  
**Parent Orchestrator ID**: `06dffeaf-8e62-4723-b704-1b7ef7cb5a98`

---

## 1. Observation

### R1: Security & Identity Guardrails

#### 1.1 `firestore.rules` Content and Vulnerabilities
- File: `firestore.rules` (lines 1–19):
  ```javascript
  rules_version = '2';
  service cloud.firestore {
    match /databases/{database}/documents {
      // Private per-user data: only the owner reads/writes.
      match /users/{uid} {
        allow read, write: if request.auth != null && request.auth.uid == uid;
        match /jobs/{jobId} {
          allow read, write: if request.auth != null && request.auth.uid == uid;
        }
      }
      // Username registry: anyone may read (public /{username} pages),
      // any signed-in user may write (claims run in a transaction).
      match /usernames/{name} {
        allow read: if true;
        allow write: if request.auth != null;
      }
    }
  }
  ```
- **Rule Defect 1 (`/users/{uid}`)**: Line 6 restricts read access to `request.auth != null && request.auth.uid == uid`. Unauthenticated visitors and authenticated users viewing other people's profiles have no read access to `/users/{uid}`.
- **Rule Defect 2 (`/usernames/{name}`)**: Line 15 permits `allow write: if request.auth != null;`. Any authenticated user can issue `setDoc`, `updateDoc`, or `deleteDoc` directly against ANY document path under `/usernames/{name}` without ownership checks.

#### 1.2 Frontend Public Profile Route `/[username]` Execution Trace
- File: `app/[username]/page.tsx` (lines 17–33):
  ```typescript
  useEffect(() => {
    if (!db) return;
    (async () => {
      const claim = await getDoc(doc(db, "usernames", name));
      if (!claim.exists()) {
        setMissing(true);
        return;
      }
      const snap = await getDoc(doc(db, "users", claim.data().uid as string));
      if (!snap.exists()) {
        setMissing(true);
        return;
      }
      const d = snap.data();
      setProfile({ username: name, displayName: (d.displayName as string | null) ?? null, photoURL: (d.photoURL as string | null) ?? null });
    })().catch(() => setMissing(true));
  }, [db, name]);
  ```
- Line 20: `getDoc(doc(db, "usernames", name))` succeeds because `allow read: if true;` on `/usernames/{name}`.
- Line 25: `getDoc(doc(db, "users", claim.data().uid as string))` fails with Firestore `permission-denied` for any visitor who is not logged in as `claim.data().uid`.
- Line 32: `catch(() => setMissing(true))` catches the `permission-denied` error and sets `missing: true`, rendering `"Nobody here yet: @{name} isn't claimed."` (lines 53–60) even when the username is actively claimed.

#### 1.3 Username Claim Handling (`/usernames/{name}`)
- File: `lib/username.ts` (lines 10–22):
  ```typescript
  export async function claimUsername(db: Firestore, uid: string, username: string, prev: string | null, profile: Record<string, unknown>) {
    const v = username.trim().toLowerCase();
    if (!validUsername(v)) throw new Error("invalid");
    await runTransaction(db, async (tx) => {
      const claimRef = doc(db, "usernames", v);
      const claim = await tx.get(claimRef);
      if (claim.exists() && claim.data().uid !== uid) throw new Error("taken");
      tx.set(claimRef, { uid, updatedAt: serverTimestamp() });
      tx.set(doc(db, "users", uid), { username: v, ...profile, updatedAt: serverTimestamp() }, { merge: true });
      if (prev && prev !== v) tx.delete(doc(db, "usernames", prev));
    });
    return v;
  }
  ```
- File: `app/account/page.tsx` (lines 106–115, 151–153):
  Client transactions and account deletion operations write to `/usernames/{v}`:
  - Line 112: `tx.set(claimRef, { uid: user!.uid, updatedAt: serverTimestamp() });`
  - Line 114: `if (prev) tx.delete(prev);`
  - Line 151: `if (currentUsername) await deleteDoc(doc(db, "usernames", currentUsername)).catch(() => {});`
- **Security Exposure**: While client-side transactions in `lib/username.ts` and `app/account/page.tsx` check `if (claim.exists() && claim.data().uid !== uid) throw new Error("taken")`, client logic provides zero security enforcement against malicious API calls. Under the current `allow write: if request.auth != null;` rule, any authenticated attacker can execute `deleteDoc(doc(db, "usernames", "victim"))` or `setDoc(doc(db, "usernames", "victim"), { uid: "attacker_id" })` and Firestore will permit the request.

---

### R2: UI Feature Completion & Form Integrity

#### 2.1 Multi-User Building Load Coordination & `BuildingChart`
- File: `app/dashboard/BuildingChart.tsx` (lines 8–59):
  - Defines an SVG component rendering building aggregate power curves: total kW (`total_kw`), flexible kW (`flexible_kw`), and capacity limit ceiling (`capacity_kw`).
  - Expects prop: `{ points: CoordinationAggregatePoint[] }`.
- File: `lib/api/types.ts` (lines 225–284):
  - Defines `CoordinationAggregatePoint`, `CoordinationCongestionPoint`, and `CoordinationResult`.
  - `CoordinationResult.aggregate_profile` contains `CoordinationAggregatePoint[]`.
- File: `lib/api/client.ts` (lines 145–164):
  - Defines `coordinateBuilding(body)` (`POST /api/v1/coordination/schedule`) and `compareCoordination(body)` (`POST /api/v1/coordination/compare`).
- File: `app/dashboard/page.tsx`:
  - `BuildingChart` is **never imported or rendered**.
  - Lines 670–687 render static placeholder cards ("Meters: Not connected", "Schedules: Not built").
  - The dashboard lacks any multi-user coordination panel or triggers for `coordinateBuilding`.

#### 2.2 Carbon Forecast Controls & Prediction Intervals on `CarbonChart`
- File: `app/dashboard/CarbonChart.tsx` (lines 7–59):
  - Component takes `{ signal: CarbonSignalResponse }`.
  - Only maps `signal.points.map((p) => p.carbon_intensity_gco2_per_kwh)` to a single line.
  - Contains no mode selector (`ACTUAL`, `EXPECTED`, `ROBUST`), no risk-weight controls, and no visualization of prediction intervals.
- Backend Forecasting Implementation:
  - `backend/app/domain/forecasting.py`:
    - Line 51: `ForecastMode` enum defines `ACTUAL`, `EXPECTED`, `ROBUST`.
    - Line 73: `CarbonForecastPoint` contains `timestamp`, `predicted_gco2_per_kwh`, `lower_gco2_per_kwh`, `upper_gco2_per_kwh`.
    - Lines 172–201: `risk_adjusted_intensity()` computes `predicted + risk_weight * (upper - predicted)` under `ROBUST` mode.
  - `backend/app/api/routes/forecast.py`:
    - Line 133: `POST /api/v1/carbon/forecast` returns `CarbonForecast` containing `points: list[CarbonForecastPoint]`.
  - `backend/app/api/routes/schedule.py` & `backend/app/api/routes/execution.py`:
    - Accept `carbon: CarbonForecastOptions` with `mode: "FORECAST"`, `forecast_mode: ForecastMode`, and `risk_weight: float`.
- Gaps in Frontend:
  - `lib/api/client.ts` does not expose `getCarbonForecast`.
  - `app/dashboard/page.tsx` only calls `getCarbonSignal` on mount (lines 283–288) and passes the observed signal to `CarbonChart`.
  - `planLive()` in `app/dashboard/page.tsx` (lines 221–241) hardcodes `{ jobs: specs, capacity_kw: 20, scheduler: "CPSAT" }` without passing forecast mode parameters.

#### 2.3 Thermal Load Configuration, Comfort Boundaries & Appliance Scheduling
- File: `app/dashboard/page.tsx`:
  - Progressive-disclosure helper `fieldsFor` (lines 30–43):
    ```typescript
    case "THERMAL":
      return { energy: false, duration: false, note: "Stores comfort as heat or cool — configured from its comfort band." };
    ```
    Returns `energy: false, duration: false` and offers no inputs for temperature boundaries.
  - The load creation form (lines 580–617) does not render inputs for minimum temperature, maximum temperature, or target temperature.
  - Live planning filter `loadsToSpecs()` (lines 197–200):
    ```typescript
    if (/heater|geyser|cool|ac\b|thermal/i.test(j.kind || "")) {
      skipped.push(j.name);
      continue;
    }
    ```
    Thermal loads are unconditionally dropped and added to `skipped`.
  - `planLive()` (lines 224–227, 235):
    Throws error `"Only thermal loads present — they need a comfort band first."` or warns `"Thermal skipped for now: ... — comfort band unknown."`.
- Backend Capability:
  - `backend/app/services/schedulers/cpsat.py` (lines 83, 197, 435, 465, 500, 528, 550) and `test_cpsat.py` (lines 334–395): CP-SAT scheduler provides native support for `LoadType.THERMAL` loads using `ThermalScale`.
  - `backend/app/domain/thermal_examples.py` defines standard synthetic dynamics models `GEYSER_SYNTHETIC` and `AC_SYNTHETIC`.
  - `backend/app/services/scheduler_normalizer.py` (lines 341–349) verifies `spec.thermal` is present and constructs `ThermalScale.from_spec()`.

#### 2.4 Load Creation Form Parameter Preservation (kWh & Duration)
- File: `app/dashboard/page.tsx`:
  - In `addJob()` (lines 332–364):
    User-entered `fEnergy` is parsed into `energyKwh` and `fDuration` into `durationMin`. Both are saved into Firestore (`doc(db, "users", user.uid, "jobs")`) and saved into component state `jobs`.
  - In `loadsToSpecs()` (lines 201–216):
    ```typescript
    if (/ev|charge|pump|laundry|wash/i.test(`${j.name} ${j.kind || ""}`)) {
      specs.push({
        id: j.id, normalized_name: j.name, category: j.kind || "Flexible",
        job_type: "DEFERRABLE_INTERRUPTIBLE", power_kw: j.powerKw, max_power_kw: j.powerKw,
        energy_required_kwh: j.powerKw * 2, min_chunk_minutes: 15,
        release_at: now.toISOString(), deadline_at: readyByToDeadline(j.readyBy, now).toISOString(),
        assumptions: ["energy assumed = rating × 2h (frontend default — declare exact values later)"],
      });
    } else {
      specs.push({
        id: j.id, normalized_name: j.name, category: j.kind || "Flexible",
        job_type: "DEFERRABLE_ATOMIC", power_kw: j.powerKw, duration_minutes: 60,
        release_at: now.toISOString(), deadline_at: readyByToDeadline(j.readyBy, now).toISOString(),
        assumptions: ["duration assumed 60 min (frontend default — declare exact values later)"],
      });
    }
    ```
  - **Data Destruction**:
    1. Even if `j.energyKwh` was entered by the user (e.g. 15 kWh), `loadsToSpecs` replaces `energy_required_kwh` with `j.powerKw * 2`.
    2. Even if `j.durationMin` was entered by the user (e.g. 45 min), `loadsToSpecs` replaces `duration_minutes` with `60`.
    3. Static assumptions (`"energy assumed = rating × 2h..."`, `"duration assumed 60 min..."`) are attached even when real user values were provided.

---

## 2. Logic Chain

1. **R1 Profile Accessibility**:
   - Because `firestore.rules` enforces `request.auth != null && request.auth.uid == uid` on `/users/{uid}`, queries from unauthenticated users or users whose UID does not equal `{uid}` receive a Firestore permission denied error.
   - Because `app/[username]/page.tsx` catches this error in `.catch(() => setMissing(true))`, visitors are shown the "Nobody here yet" missing state instead of profile data.
   - Allowing public read on `/users/{uid}` (`allow read: if true;`) while retaining strict owner write (`allow write: if request.auth != null && request.auth.uid == uid;`) and strict subcollection access on `/users/{uid}/jobs/{jobId}` resolves the permission failure and satisfies Acceptance Criterion 1.

2. **R1 Claim Protection**:
   - Because `firestore.rules` currently sets `allow write: if request.auth != null;` for `/usernames/{name}`, Firestore grants write/update/delete permission to any logged-in user regardless of document ownership.
   - Decomposing the write rule into granular operations:
     - `create`: permitted only if `request.auth != null && request.resource.data.uid == request.auth.uid` (prevent claiming for someone else).
     - `update`: permitted only if `request.auth != null && resource.data.uid == request.auth.uid && request.resource.data.uid == request.auth.uid` (prevent stealing or reassigning existing claims).
     - `delete`: permitted only if `request.auth != null && resource.data.uid == request.auth.uid` (prevent deleting other users' claims).
   - This directly satisfies Acceptance Criterion 2.

3. **R2 Multi-User Coordination**:
   - The backend `/api/v1/coordination/schedule` endpoint and frontend `BuildingChart` component are already implemented and tested independently.
   - Because `BuildingChart` is never imported into `app/dashboard/page.tsx` and no trigger invokes `coordinateBuilding`, the multi-user building coordination feature remains orphaned.
   - Connecting `coordinateBuilding` and embedding `<BuildingChart points={coordinationResult.aggregate_profile} />` inside a coordination dashboard section fulfills Acceptance Criterion 3.

4. **R2 Carbon Forecast Controls & Prediction Intervals**:
   - The backend `/api/v1/carbon/forecast` endpoint provides `CarbonForecastPoint`s with `predicted_gco2_per_kwh`, `lower_gco2_per_kwh`, and `upper_gco2_per_kwh`.
   - `CarbonChart` currently only draws a single baseline path from `CarbonSignalResponse.points`.
   - By adding a forecast mode selector (`ACTUAL`, `EXPECTED`, `ROBUST`), fetching the forecast points, rendering the uncertainty interval area between `lower` and `upper`, and passing the selected mode to `planSchedule`, Acceptance Criterion 4 is satisfied.

5. **R2 Thermal Scheduling & Comfort Boundaries**:
   - The backend scheduler (`CPSAT`) already supports thermal loads when supplied with a `ThermalSpec`.
   - The frontend currently skips thermal loads solely because it lacks comfort boundaries.
   - Adding comfort boundary inputs (min temp °C, max temp °C) to the load creation form for `THERMAL` loads and populating `spec.thermal` using the user's boundaries combined with standard synthetic dynamics (`GEYSER_SYNTHETIC` or `AC_SYNTHETIC`) enables thermal appliances to be scheduled instead of skipped, satisfying Acceptance Criterion 5.

6. **R2 Form Integrity & Preservation**:
   - The form currently captures `energyKwh` and `durationMin` into Firestore.
   - `loadsToSpecs()` in `app/dashboard/page.tsx` unconditionally overwrites `energy_required_kwh` with `j.powerKw * 2` and `duration_minutes` with `60`.
   - Updating `loadsToSpecs()` to prioritize `j.energyKwh` and `j.durationMin` when present (falling back to defaults only when absent) preserves user-specified values in the scheduling payload, fulfilling Acceptance Criterion 6.

---

## 3. Caveats

1. **Firestore Client-Side Exposure of User Documents**:
   - Allowing `read: if true` on `/users/{uid}` exposes fields stored in the user document (`displayName`, `photoURL`, `username`, `email`, `occupation`, `place`, `rooms`). In Firestore, document reads are all-or-nothing; if `email` is considered sensitive, the public profile should either only store public fields in `/users/{uid}` or public fields should be in a separate public profile document. However, `app/[username]/page.tsx` explicitly reads `/users/{uid}` directly, so making `/users/{uid}` readable is the direct architectural requirement of R1.
2. **Thermal Dynamics Coefficients**:
   - Users cannot be expected to know decay parameter `a`, gain `b`, or drift `c`. As documented in `backend/app/domain/thermal_examples.py`, these are abstract synthetic defaults. The frontend should capture user comfort boundaries (`temperature_min_c`, `temperature_max_c`, `temperature_target_c`), while pairing them with the standard synthetic dynamics for the appliance class (`geyser` vs `ac`).
3. **Execution State Persistence (R3) & External Adapters (R4)**:
   - This investigation focused exclusively on R1 and R2 per the survey scope. Integration with R3 (persisting live session across browser reload) and R4 (live external carbon adapters) must be coordinated with the respective explorers.

---

## 4. Conclusion

The root causes and remediation pathways for R1 and R2 are fully identified:

1. **R1 Fixes**:
   - Update `firestore.rules`:
     - Allow public read on `/users/{uid}`: `allow read: if true; allow write: if request.auth != null && request.auth.uid == uid;`.
     - Secure `/usernames/{name}`: enforce `request.auth != null && request.resource.data.uid == request.auth.uid` on create, `resource.data.uid == request.auth.uid && request.resource.data.uid == request.auth.uid` on update, and `resource.data.uid == request.auth.uid` on delete.
2. **R2 Fixes**:
   - **Building Coordination**: Import `BuildingChart` into `app/dashboard/page.tsx`, expose a "Coordinate building" action calling `coordinateBuilding()`, and render `BuildingChart` with `aggregate_profile`.
   - **Carbon Forecast Controls**: Add forecast mode selection (`ACTUAL`, `EXPECTED`, `ROBUST`) to `CarbonChart`, add `getCarbonForecast` to `lib/api/client.ts`, render prediction intervals on `CarbonChart`, and forward the mode into `planLive()` payload.
   - **Thermal Comfort Boundaries**: Add min/max temperature input fields to `fieldsFor("THERMAL")` in `app/dashboard/page.tsx`, persist them on the job, and update `loadsToSpecs()` to build valid thermal `LoadSpec`s instead of skipping thermal appliances.
   - **Form Parameter Preservation**: In `loadsToSpecs()`, use `j.energyKwh` when defined instead of `j.powerKw * 2`, and `j.durationMin` when defined instead of static `60`.

---

## 5. Verification Method

To independently verify the findings and proposed solutions:

1. **Frontend Build & Typecheck**:
   ```bash
   npm run lint
   npm run build
   ```
   Both must exit with code 0.
2. **Backend Regression Test Suite**:
   ```bash
   python -m pytest backend/tests -q
   ```
   Runs the test suite to ensure zero regressions in backend scheduler, CP-SAT thermal handling, and coordination APIs.
3. **Security Rules Verification**:
   Inspect `firestore.rules` and verify that:
   - Reading `/users/{target_uid}` without authentication returns allowed.
   - Deleting or overwriting `/usernames/{name}` where `auth.uid != resource.data.uid` returns permission denied.
4. **Form Payload Verification**:
   Add a load in the dashboard with 12 kWh energy and 45 minutes duration, click "Plan live", and verify via network inspection that the outgoing payload to `/api/v1/schedules/plan` preserves `energy_required_kwh = 12` and `duration_minutes = 45`.
5. **Thermal Scheduling Verification**:
   Add a Geyser with min 40°C, max 60°C. Click "Plan live". Confirm no "Thermal skipped" error occurs and the job is included in the live execution panel.
6. **BuildingChart Verification**:
   Navigate to the multi-user coordination section on `/dashboard` and verify `BuildingChart` renders total kW, flexible kW, and capacity ceiling curves.
