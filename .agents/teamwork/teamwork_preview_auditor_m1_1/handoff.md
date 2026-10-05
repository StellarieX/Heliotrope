# Forensic Audit Report: Milestone 1 (Security & Identity Guardrails)

**Work Product**: `c:/Users/dhrri/Desktop/Heliotrope-main/firestore.rules`  
**Profile**: General Project  
**Integrity Enforcement Mode**: Development Mode (authoritative source: `ORIGINAL_REQUEST.md:8`)  
**Verdict**: **CLEAN**  
**Auditor**: Forensic Auditor (`teamwork_preview_auditor_m1_1`)  
**Timestamp**: 2026-10-05T11:48:00Z  
**Parent Orchestrator ID**: `06dffeaf-8e62-4723-b704-1b7ef7cb5a98`

---

## Forensic Audit Summary

### Phase Results
- **Hardcoded Output Detection**: **PASS** — Zero mock strings, test user constants, or hardcoded pass tokens found in `firestore.rules`.
- **Facade Implementation Detection**: **PASS** — Zero dummy branches, placeholder returns, or bypass conditions. All rules dynamically evaluate `request.auth`, `request.resource.data`, and `resource.data`.
- **Pre-populated Artifact Detection**: **PASS** — Zero pre-existing `.log`, `*result*`, or `*output*` files exist in the project repository outside transient build directories.
- **Self-Certifying Tests Check**: **PASS** — Tests dynamically evaluate AST-parsed Firestore security rule logic against distinct attack payloads and edge cases.
- **Build & Compilation Verification**: **PASS** — `npm run lint` exited 0; `npm run build` compiled all Next.js routes (including dynamic `/[username]`) with 0 errors.
- **Backend Zero-Regression Verification**: **PASS** — `python -m pytest backend/tests --ignore=backend/tests/test_properties.py` passed all 4,629 backend tests with 0 failures.
- **Adversarial & Empirical Invariant Verification**: **PASS** — 64 independent empirical security rule tests executed and passed (10 Tier 1 tests, 22 Challenger 1 tests, 32 Challenger 2 tests), plus 20-scenario empirical matrix evaluation passed 100%.

---

## 1. Observation

1. **Target Work Product (`c:/Users/dhrri/Desktop/Heliotrope-main/firestore.rules:1-26`)**:
   ```javascript
   rules_version = '2';
   service cloud.firestore {
     match /databases/{database}/documents {
       // User profile documents: public read so /[username] pages resolve;
       // only the account owner may write their own profile document.
       match /users/{uid} {
         allow read: if true;
         allow write: if request.auth != null && request.auth.uid == uid;

         // Private per-user jobs: only the owner may read or write.
         match /jobs/{jobId} {
           allow read, write: if request.auth != null && request.auth.uid == uid;
         }
       }

       // Username claim registry: anyone may read (public lookup);
       // claim creation, modification, and deletion are restricted to the owner of the UID.
       match /usernames/{name} {
         allow read: if true;
         allow create: if request.auth != null && request.resource.data.uid == request.auth.uid;
         allow update: if request.auth != null && resource.data.uid == request.auth.uid && request.resource.data.uid == request.auth.uid;
         allow delete: if request.auth != null && resource.data.uid == request.auth.uid;
       }
     }
   }
   ```

2. **Absence of Prohibited Artifacts**:
   - `find_by_name` for `*.log`, `*result*`, `*output*` outside `node_modules` and `.next`:
     ```
     Found 0 results for *.log
     Found 0 results for *result*
     Found 0 results for *output*
     ```
   - Automated scan for backdoor/facade patterns (`== 'test'`, `== 'admin'`, `bypass`, `DEBUG`, `MOCK`, `allow write: if true;`, `allow delete: if true;`):
     ```
     Prohibited patterns found: []
     VERDICT: CLEAN of prohibited patterns
     ```

3. **Frontend Build & Linter Output**:
   - `npm run lint` exited code 0:
     ```
     > heliotrope@0.1.0 lint
     > eslint
     ```
   - `npm run build` exited code 0:
     ```
     ▲ Next.js 16.3.8 (Turbopack)
     ✓ Compiled successfully in 1227ms
       Running TypeScript ...
       Finished TypeScript in 1697ms ...
     ✓ Generating static pages using 8 workers (6/6) in 1361ms
     Route (app)
     ┌ ○ /
     ├ ○ /_not-found
     ├ ƒ /[username]
     ├ ○ /account
     └ ○ /dashboard
     ```

4. **Backend Test Suite Execution**:
   - `python -m pytest backend/tests --ignore=backend/tests/test_properties.py -q`:
     ```
     4629 passed, 2 warnings in 220.85s (0:03:40)
     ```
     Zero regressions across all existing backend unit, integration, and solver test suites.

5. **Empirical Forensic Security Rule Matrix (20 Test Scenarios)**:
   - Tool command: Python matrix evaluator executing all permutations of visitor, owner, and attacker operations:
     ```
     [PASS] USERS - Unauthenticated read /users/alice: got True, expected True
     [PASS] USERS - Authenticated visitor Bob read /users/alice: got True, expected True
     [PASS] USERS - Unauthenticated write /users/alice: got False, expected False
     [PASS] USERS - Attacker Bob write /users/alice: got False, expected False
     [PASS] USERS - Owner Alice write /users/alice: got True, expected True
     [PASS] JOBS - Unauthenticated read /users/alice/jobs/j1: got False, expected False
     [PASS] JOBS - Attacker Bob read /users/alice/jobs/j1: got False, expected False
     [PASS] JOBS - Owner Alice read /users/alice/jobs/j1: got True, expected True
     [PASS] JOBS - Attacker Bob write /users/alice/jobs/j1: got False, expected False
     [PASS] JOBS - Owner Alice write /users/alice/jobs/j1: got True, expected True
     [PASS] USERNAMES_READ - Unauthenticated read /usernames/alice: got True, expected True
     [PASS] USERNAMES_READ - Authenticated visitor Bob read /usernames/alice: got True, expected True
     [PASS] USERNAMES_CREATE - Unauthenticated create /usernames/charlie: got False, expected False
     [PASS] USERNAMES_CREATE - Owner Charlie create /usernames/charlie with uid=charlie: got True, expected True
     [PASS] USERNAMES_CREATE - Attacker Bob create /usernames/charlie with uid=charlie (spoof): got False, expected False
     [PASS] USERNAMES_CREATE - Attacker Bob create /usernames/charlie with uid=bob: got True, expected True
     [PASS] USERNAMES_UPDATE - Unauthenticated update /usernames/alice: got False, expected False
     [PASS] USERNAMES_UPDATE - Attacker Bob update /usernames/alice (resource=alice, request=bob): got False, expected False
     [PASS] USERNAMES_UPDATE - Attacker Bob update /usernames/alice (resource=alice, request=alice): got False, expected False
     [PASS] USERNAMES_UPDATE - Owner Alice update /usernames/alice (resource=alice, request=alice): got True, expected True
     [PASS] USERNAMES_UPDATE - Owner Alice transfer /usernames/alice (resource=alice, request=bob): got False, expected False
     [PASS] USERNAMES_DELETE - Unauthenticated delete /usernames/alice: got False, expected False
     [PASS] USERNAMES_DELETE - Attacker Bob delete /usernames/alice (resource=alice): got False, expected False
     [PASS] USERNAMES_DELETE - Owner Alice delete /usernames/alice (resource=alice): got True, expected True
     ```

6. **Independent Adversarial Test Suites**:
   - `python -m pytest tests/e2e/test_tier1_features.py -k "f01 or f02"`: 10 passed.
   - `python -m pytest tests/test_firestore_rules_challenge.py`: 22 passed.
   - `python -m pytest tests/e2e/test_m1_adversarial_challenger.py`: 32 passed.

---

## 2. Logic Chain

1. **R1 Mandate 1: Public Profile Accessibility**:
   - *Observation Ref*: Observation 1, 3, 5.
   - Ground truth requirement (`ORIGINAL_REQUEST.md:12-14`): "Fix Firestore security rules so public profile pages at `/[username]` can be viewed by any visitor without permission errors".
   - `app/[username]/page.tsx:20-25` resolves user profile data by querying `usernames/{name}` to obtain `uid`, then querying `users/{uid}`.
   - In `firestore.rules`:
     - Line 7: `allow read: if true;` on `/users/{uid}`.
     - Line 19: `allow read: if true;` on `/usernames/{name}`.
   - Any visitor (whether unauthenticated or authenticated as another user) is granted read access to both documents, resolving the previous `permission-denied` regression without errors.

2. **R1 Mandate 2: Username Claim Ownership & Hijack Prevention**:
   - *Observation Ref*: Observation 1, 5, 6.
   - Ground truth requirement (`ORIGINAL_REQUEST.md:13-14` & `28`): "protect `/usernames/{name}` claims so documents cannot be overwritten or deleted by unauthorized third-party users".
   - In `firestore.rules`:
     - `create`: `allow create: if request.auth != null && request.resource.data.uid == request.auth.uid;` enforces that the creator must be authenticated and can only create a claim doc referencing their own UID.
     - `update`: `allow update: if request.auth != null && resource.data.uid == request.auth.uid && request.resource.data.uid == request.auth.uid;` enforces that (1) only the existing claim owner can modify the document, and (2) the claim cannot be transferred or reassigned to a different UID.
     - `delete`: `allow delete: if request.auth != null && resource.data.uid == request.auth.uid;` enforces that only the existing claim owner can delete the claim document.
   - All unauthorized attempts to overwrite, hijack, or delete claims owned by other users fail with `permission-denied`.

3. **Subcollection Isolation & Least Privilege**:
   - *Observation Ref*: Observation 1, 5, 6.
   - Nested subcollection `/users/{uid}/jobs/{jobId}` explicitly enforces `allow read, write: if request.auth != null && request.auth.uid == uid;`.
   - In Firestore Rules v2, rules are not recursive across collections unless wildcard recursion `{document=**}` is specified.
   - Public read on `/users/{uid}` does not leak access into `/users/{uid}/jobs/{jobId}`.

4. **Integrity Forensics Evaluation**:
   - *Observation Ref*: Observation 1, 2, 3, 4, 5, 6.
   - Under Development Mode (and evaluated across all modes):
     - Zero hardcoded test constants or result strings.
     - Zero facade implementations or mock branches.
     - Zero fabricated or pre-populated verification logs.
     - Zero regression across the existing test suite (4,629 tests pass).
     - Full operational build and lint pass (0 errors).

---

## 3. Caveats

1. **Document-Level Read Scope in Firestore**:
   - Firestore security rules evaluate read permissions per document, not per field. `allow read: if true;` on `/users/{uid}` permits visitors to read all fields stored in that document (`displayName`, `photoURL`, `username`, `email`, `occupation`, `place`, `rooms`, `onboarded`). This matches the existing client architecture (`app/[username]/page.tsx`), but any sensitive personal information intended to be private should not be stored in the root user document.
2. **Global Hypothesis Dependency**:
   - `backend/tests/test_properties.py` requires `hypothesis`, which is defined under optional dev dependencies in `pyproject.toml` and scheduled for verification in Milestone 4 (`PROJECT.md` Feature 11). Running all other 31 backend test files passes 100% (4,629 tests).

---

## 4. Conclusion

**Verdict: CLEAN**

The work product `c:/Users/dhrri/Desktop/Heliotrope-main/firestore.rules` authenticates genuine, rigorous, and secure implementation of the Milestone 1 requirements. It contains zero facades, zero hardcoding, zero backdoor tokens, and zero integrity violations. All security and identity guardrails specified in `ORIGINAL_REQUEST.md` and `PROJECT.md` are completely and genuinely satisfied.

---

## 5. Verification Method

To independently verify this verdict:

1. **Rule Content Inspection**:
   ```bash
   cat firestore.rules
   ```
   Verify lines 19–23 contain granular handlers for `create`, `update`, and `delete` on `/usernames/{name}`, and lines 6–8 permit public read on `/users/{uid}` with owner-only writes.

2. **Frontend Build & Linter**:
   ```bash
   npm run lint
   npm run build
   ```
   Both commands exit 0.

3. **Backend Test Suite**:
   ```bash
   python -m pytest backend/tests --ignore=backend/tests/test_properties.py -q
   ```
   Verifies 4,629 backend tests pass without regression.

4. **Adversarial & Empirical Rule Test Suites**:
   ```bash
   python -m pytest tests/e2e/test_tier1_features.py -k "f01 or f02" -v
   python -m pytest tests/test_firestore_rules_challenge.py -v
   python -m pytest tests/e2e/test_m1_adversarial_challenger.py -v
   ```
   All 64 tests pass with 0 failures.

5. **Invalidation Conditions**:
   The verdict is invalidated if any rule allows unauthenticated writes, allows cross-user claim overwriting/deletion, leaks `/users/{uid}/jobs/{jobId}` to third parties, or introduces hardcoded test mocks.
