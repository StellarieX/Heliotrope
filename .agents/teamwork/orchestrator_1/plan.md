# Overhaul Plan: Heliotrope

## Objective
Coordinate the full overhaul of Heliotrope to satisfy all requirements in ORIGINAL_REQUEST.md:
- R1: Security & Identity Guardrails
- R2: UI Feature Completion & Form Integrity
- R3: Execution State Persistence & Session Durability
- R4: Live External Provider Adapters
Ensuring build passes (`npm run build` exits 0), existing tests pass (`pytest backend/tests` exits 0), and E2E test suite passes 100% with clean forensic audit.

## Steps
1. **Survey (Phase 0)**:
   - Spawn 3 parallel Explorers:
     - Explorer 1: R1 (Firestore security rules, username claim protection, public profile `/[username]`) and R2 (UI dashboard, BuildingChart, CarbonChart modes, comfort bands, energy/duration preservation).
     - Explorer 2: R3 (Backend execution store persistence, SQLite/file/db durability across restarts, client session persistence) and R4 (Live external carbon provider adapter, natural language load classification adapter, graceful fallbacks).
     - Explorer 3: System, build & test infrastructure (`npm run build`, `pytest backend/tests`, dependencies, directory structure, environment setup).
   - Collect and synthesize survey reports into `PROJECT.md`.
2. **Dual Track Setup (Phase 1)**:
   - Launch E2E Testing Track: Establish `TEST_INFRA.md` and generate Tiers 1-4 requirement-driven opaque-box tests.
   - Plan implementation milestones M1 through M4.
3. **Execution & Verification (Phase 2)**:
   - Milestone 1: R1 Security & Identity Guardrails
   - Milestone 2: R2 UI Feature Completion & Form Integrity
   - Milestone 3: R3 Execution State Persistence & Session Durability
   - Milestone 4: R4 Live External Provider Adapters
   - For each milestone: Explorer -> Worker -> Reviewers (2) -> Challengers (2) -> Forensic Auditor -> Gate.
4. **Final Milestone (Phase 3)**:
   - Wait for `TEST_READY.md`.
   - Phase 1: 100% pass of E2E test suite across Tiers 1-4.
   - Phase 2: Adversarial coverage hardening (Tier 5) with Challengers and Reviewers.
5. **Final Review & Report (Phase 4)**:
   - Verify all acceptance criteria.
   - Synthesize results and report completion to parent.
