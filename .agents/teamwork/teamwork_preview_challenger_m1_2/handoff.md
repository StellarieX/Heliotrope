# Handoff Report: Milestone 1 Adversarial Review (Challenger 2)

**Date**: 2026-10-05T11:46:00Z  
**Role**: Empirical Challenger (critic, specialist)  
**Milestone**: Milestone 1 (R1: Security & Identity Guardrails)  
**Target File**: `firestore.rules`  
**Verdict**: **APPROVE**  
**Parent Orchestrator ID**: `06dffeaf-8e62-4723-b704-1b7ef7cb5a98`

---

## 1. Observation

1. **Target Security Rules Configuration (`firestore.rules`)**:
   - `firestore.rules` contains the following active rule definitions:
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

2. **Public Profile Route (`app/[username]/page.tsx`)**:
   - Lines 20–25 directly execute two reads:
     ```typescript
     const claim = await getDoc(doc(db, "usernames", name));
     ...
     const snap = await getDoc(doc(db, "users", claim.data().uid as string));
     ```
   - Both `/usernames/{name}` and `/users/{uid}` permit public reads via `allow read: if true;`.

3. **Account Username Claim & Profile Mutation (`lib/username.ts` and `app/account/page.tsx`)**:
   - Claims execute via Firestore transaction:
     - Sets `/usernames/{name}` with `{ uid, updatedAt }`
     - Sets `/users/{uid}` with `{ username, ...profile, updatedAt }`
     - Deletes previous username claim at `/usernames/{prev}` if changing names.

4. **Empirical Adversarial Test Execution (`tests/e2e/test_m1_adversarial_challenger.py`)**:
   - Executed command: `py -3.11 -m pytest tests/e2e/test_m1_adversarial_challenger.py -v`
   - Result: 32 tests collected, 32 passed, 0 failed in 0.09s.

5. **Linting and Build Verification**:
   - Executed command: `npm run lint` -> Exited code 0 (clean).
   - Executed command: `npm run build` -> Exited code 0 (compiled all dynamic and static routes cleanly).

6. **Backend Regression Verification**:
   - Executed command: `py -3.11 -m pytest backend/tests --ignore=backend/tests/test_properties.py -q`
   - Result: 4,629 passed in 219s with 0 regressions.

---

## 2. Logic Chain

1. **Evaluation of `/usernames/{name}` Attack Vectors**:
   - **Claim Squatting (Unauthenticated or Spoofed UID)**:
     - An unauthenticated caller cannot create a claim document because `request.auth != null` fails (`test_adv_claim_squatting_unauthenticated` PASSED).
     - An authenticated attacker Mallory attempting to claim a username under Alice's UID (`request.resource.data.uid = 'alice'`) fails because `request.resource.data.uid == request.auth.uid` (`'alice' == 'mallory'`) evaluates to false (`test_adv_claim_squatting_spoofed_uid` PASSED).
     - Overwriting an existing claim doc evaluates the `update` rule in Firestore Rules v2. Mallory attempting to overwrite Alice's claim fails because `resource.data.uid == request.auth.uid` (`'alice' == 'mallory'`) evaluates to false (`test_adv_claim_squatting_overwrite_existing_claimed` PASSED).
   - **Re-assignment to Null / Field Omission**:
     - Updating `/usernames/{name}` with `{ uid: null }` or `{}` fails because `request.resource.data.uid == request.auth.uid` requires the new payload's UID to match `request.auth.uid` (a non-null string) (`test_adv_claim_reassignment_to_null` PASSED).
   - **Claim Transfer / Hijacking**:
     - Direct sale or transfer of a username from Alice to Bob (`request.resource.data.uid = 'bob'`) fails because `request.resource.data.uid == request.auth.uid` (`'bob' == 'alice'`) evaluates to false (`test_adv_claim_transfer_by_owner_blocked` PASSED).
     - Attacker hijacking fails because `resource.data.uid == request.auth.uid` (`'alice' == 'mallory'`) evaluates to false (`test_adv_claim_transfer_hijack_by_attacker` PASSED).
   - **Unauthorized Deletion**:
     - Deletion by unauthenticated visitors or third-party attackers fails because `request.auth != null && resource.data.uid == request.auth.uid` requires the caller to be authenticated as the claim owner (`test_adv_claim_deletion_unauthenticated`, `test_adv_claim_deletion_by_third_party` PASSED).
     - Legitimate claim release by owner succeeds (`test_adv_claim_deletion_by_owner_allowed` PASSED).
   - **Batch Writes**:
     - Firestore batch writes are all-or-nothing: if an attacker bundles a legitimate personal write with an unauthorized username claim overwrite, the claim write's permission failure rejects the entire batch (`test_adv_batch_write_bundled_exploit` PASSED).
     - Legitimate owner transitions (create new claim + update profile + delete old claim) succeed in batch/tx (`test_adv_batch_write_legitimate_claim_transition` PASSED).

2. **Evaluation of `/users/{uid}` and `/users/{uid}/jobs/{jobId}` Vectors**:
   - **Profile Write/Delete Protection**:
     - `allow write: if request.auth != null && request.auth.uid == uid;` blocks unauthenticated callers, third-party callers, and tokens missing a valid UID from creating, updating, or deleting another user's profile (`test_adv_user_profile_write_unauthenticated`, `test_adv_user_profile_write_cross_user_hijack`, `test_adv_user_profile_delete_unauthenticated`, `test_adv_user_profile_delete_cross_user_vandalism` PASSED).
     - Owner profile mutations and deletions succeed (`test_adv_user_profile_write_by_owner_allowed`, `test_adv_user_profile_delete_by_owner_allowed` PASSED).
   - **Private Jobs Subcollection Isolation**:
     - In Firestore Rules v2 (`rules_version = '2'`), parent match clauses do not cascade to subcollections unless recursive match `{document=**}` is specified.
     - `/users/{uid}/jobs/{jobId}` defines its own independent rule: `allow read, write: if request.auth != null && request.auth.uid == uid;`.
     - Non-owners and unauthenticated callers are rejected from reading or writing jobs (`test_adv_user_jobs_read_unauthenticated`, `test_adv_user_jobs_read_cross_user_snoop`, `test_adv_user_jobs_write_unauthenticated`, `test_adv_user_jobs_write_cross_user_tampering` PASSED).
     - Owner reads and writes succeed (`test_adv_user_jobs_read_by_owner_allowed`, `test_adv_user_jobs_write_by_owner_allowed` PASSED).

3. **Evaluation of Genuine Access Paths (`app/[username]/page.tsx`)**:
   - `allow read: if true;` on `/usernames/{name}` and `/users/{uid}` allows unauthenticated visitors and authenticated users to resolve public profiles (`test_genuine_public_profile_unauthenticated_full_pipeline`, `test_genuine_public_profile_authenticated_visitor_pipeline` PASSED).
   - Reading an unclaimed username succeeds at the rules layer, returning `exists: false` without triggering a Firestore `permission-denied` exception (`test_genuine_public_profile_unclaimed_username` PASSED).

---

## 3. Caveats

1. **Document-Wide Firestore Read Scope**:
   - Firestore security rules grant read access at the document level. Allowing public read on `/users/{uid}` makes all top-level document fields (`displayName`, `photoURL`, `username`, `email`, etc.) readable to anyone who knows or discovers the user's UID.
   - This matches the current application architecture where `app/[username]/page.tsx` queries `/users/{uid}` directly. If strict PII privacy (e.g. concealing `email`) is required in future milestones, sensitive fields should be placed in a private subcollection (e.g. `/users/{uid}/private/account`) or public fields projected into a dedicated `/profiles/{uid}` collection.
2. **Username Document Size & Character Set Enforcement**:
   - `firestore.rules` enforces identity ownership (`request.resource.data.uid == request.auth.uid`) but does not enforce regex format checks (`/^[a-z0-9_]{3,20}$/`) at the rule level. That regex validation is enforced client-side in `lib/username.ts` and `app/account/page.tsx`.
3. **Hypothesis Test Suite Dependency**:
   - 1 test file (`backend/tests/test_properties.py`) depends on `hypothesis`, which is explicitly scheduled for Milestone 4 (R4 / Feature 11). All other 31 backend test files (4,629 tests) pass with zero regressions.

---

## 4. Conclusion

**Verdict: APPROVE**

The implementation in `firestore.rules` fully satisfies Milestone 1 / Requirement R1:
- Resolves the public profile permission denial on `/[username]` for all visitors.
- Establishes rigorous identity ownership guardrails on `/usernames/{name}`, preventing unauthorized creation, overwriting, claim transfer, re-assignment to null, or deletion.
- Preserves absolute confidentiality on private user jobs in `/users/{uid}/jobs/{jobId}`.
- Survives all 32 empirical adversarial attack simulations with 100% pass rate.
- Introduces zero build, lint, or backend regressions.

---

## 5. Verification Method

To independently reproduce and verify this assessment:

1. **Adversarial Security Test Suite**:
   ```bash
   py -3.11 -m pytest tests/e2e/test_m1_adversarial_challenger.py -v
   ```
   *Expected outcome*: 32 passed in <0.2s.

2. **Core Feature 1 & 2 E2E Tests**:
   ```bash
   py -3.11 -m pytest tests/e2e/test_tier1_features.py -k "f01 or f02" -v
   ```
   *Expected outcome*: 10 passed in <0.1s.

3. **Frontend Code Quality & Production Build**:
   ```bash
   npm run lint
   npm run build
   ```
   *Expected outcome*: Exits 0, all routes compile without error.

4. **Backend Test Suite Regressions Check**:
   ```bash
   py -3.11 -m pytest backend/tests --ignore=backend/tests/test_properties.py -q
   ```
   *Expected outcome*: 4629 passed in ~3m40s.

---

## Security Audit Score & Findings (firebase-security-rules-auditor)

```json
{
  "score": 5,
  "summary": "firestore.rules demonstrates robust identity-based access control with granular separation of create, update, and delete actions on username claims, public read accessibility for profile resolution, and isolated owner-only subcollection permissions.",
  "findings": [
    {
      "check": "The Update Bypass",
      "severity": "minor",
      "issue": "No update bypass found. Update rules on /usernames/{name} strictly require existing resource.data.uid == request.auth.uid AND request.resource.data.uid == request.auth.uid, preventing UID reassignment, orphaning, or hijacking.",
      "recommendation": "None needed for M1; consider adding request.resource.data.keys().hasOnly(['uid', 'updatedAt']) for schema hardening in future releases."
    },
    {
      "check": "Authority Source",
      "severity": "minor",
      "issue": "Rules rely exclusively on trusted request.auth.uid rather than user-provided assertions for authority and authorization.",
      "recommendation": "Maintain reliance on request.auth.uid."
    },
    {
      "check": "Business Logic vs. Rules",
      "severity": "minor",
      "issue": "Public reads are correctly permitted on /usernames/{name} and /users/{uid} to satisfy public profile viewing at /[username], and transaction paths in lib/username.ts align with rule constraints.",
      "recommendation": "None."
    },
    {
      "check": "Field-Level vs. Identity-Level Security",
      "severity": "minor",
      "issue": "Ownership is strictly checked via resource.data.uid == request.auth.uid across update and delete operations.",
      "recommendation": "None."
    }
  ]
}
```
