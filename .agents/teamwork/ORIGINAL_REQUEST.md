# Original User Request

## 2026-10-05T11:03:52Z

Execute a full overhaul of Heliotrope to resolve critical security vulnerabilities, connect orphaned backend features to the frontend UI, provide persistent execution state, and implement live external data adapters with graceful fallbacks.

Working directory: c:/Users/dhrri/Desktop/Heliotrope-main
Integrity mode: development

## Requirements

### R1. Security & Identity Guardrails
Fix Firestore security rules so public profile pages at `/[username]` can be viewed by any visitor without permission errors, and protect `/usernames/{name}` claims so documents cannot be overwritten or deleted by unauthorized third-party users.

### R2. UI Feature Completion & Form Integrity
Connect all orphaned backend capabilities to the frontend dashboard: multi-user building load coordination (rendering `BuildingChart`), carbon forecast controls (mode selection and prediction intervals on `CarbonChart`), and thermal load configuration (comfort bands). Ensure user-entered load parameters (energy targets and duration) are preserved when generating schedule specs rather than overwritten with placeholder defaults.

### R3. Execution State Persistence & Session Durability
Replace the ephemeral in-memory execution store with durable storage so schedule versions, job states, and events persist across backend service restarts. Retain the active live schedule session on the client across browser page reloads.

### R4. Live External Provider Adapters
Implement working adapters for external grid carbon intensity data and natural language load classification, supporting live API integrations with graceful fallbacks when credentials are unconfigured.

## Acceptance Criteria

### Security & Access Control
- [ ] Public profile route `/[username]` successfully retrieves and renders public profile data for a claimed username for any visitor without encountering Firestore permission denied errors.
- [ ] Firestore security rules reject attempts by authenticated users to overwrite or delete username claim documents owned by other users.

### User Interface & Coordination
- [ ] Dashboard includes a multi-user coordination panel rendering aggregate power, flexible demand, and capacity limits via `BuildingChart`.
- [ ] Dashboard supports selecting carbon forecast modes (`ACTUAL`, `EXPECTED`, `ROBUST`) and displays prediction intervals on the carbon intensity chart.
- [ ] Load creation interface captures comfort boundaries for thermal appliances and allows them to be scheduled in the live panel rather than skipped.
- [ ] User-specified energy (kWh) and duration (minutes) fields on loads are preserved in the generated scheduling payload rather than replaced with static defaults.

### Persistence & Durability
- [ ] Schedule records, versions, and execution events remain intact and queryable after the backend process restarts.
- [ ] Reloading the dashboard restores the active live schedule state and execution timeline.

### Integrations & Code Quality
- [ ] External carbon provider and load classification adapters execute against live APIs when configured and fall back cleanly when unconfigured.
- [ ] Frontend builds without TypeScript or ESLint errors (`npm run build` exits 0).
- [ ] Existing backend test suite passes with zero regressions (`pytest backend/tests` exits 0).
