# Handoff Report: Milestone 1 (Security Adversarial Challenger 1)

**Date**: 2026-10-05T11:42:00Z  
**Worker**: Challenger 1 (Milestone 1)  
**Target File**: `c:/Users/dhrri/Desktop/Heliotrope-main/firestore.rules`  
**Parent Orchestrator ID**: `06dffeaf-8e62-4723-b704-1b7ef7cb5a98`  
**Verdict**: **APPROVE**  

---

## 1. Observation

1. **Target Ruleset Content (`c:/Users/dhrri/Desktop/Heliotrope-main/firestore.rules:1-26`)**:
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

2. **Empirical Adversarial Test Execution (`tests/test_firestore_rules_challenge.py`)**:
   - Command: `python -m pytest tests/test_firestore_rules_challenge.py -v`
   - Output:
     ```
     ============================= test session starts =============================
     collected 22 items

     tests/test_firestore_rules_challenge.py::test_claim_hijack_unauthenticated_cannot_create_claim PASSED [  4%]
     tests/test_firestore_rules_challenge.py::test_claim_hijack_spoofed_uid_creation_rejected PASSED [  9%]
     tests/test_firestore_rules_challenge.py::test_claim_hijack_cross_user_overwrite_rejected PASSED [ 13%]
     tests/test_firestore_rules_challenge.py::test_claim_hijack_metadata_tampering_rejected PASSED [ 18%]
     tests/test_firestore_rules_challenge.py::test_claim_hijack_owner_reassignment_to_third_party_rejected PASSED [ 22%]
     tests/test_firestore_rules_challenge.py::test_claim_valid_owner_can_create_and_update PASSED [ 27%]
     tests/test_firestore_rules_challenge.py::test_claim_deletion_unauthenticated_rejected PASSED [ 31%]
     tests/test_firestore_rules_challenge.py::test_claim_deletion_cross_user_rejected PASSED [ 36%]
     tests/test_firestore_rules_challenge.py::test_claim_deletion_legitimate_owner_allowed PASSED [ 40%]
     tests/test_firestore_rules_challenge.py::test_claim_deletion_missing_uid_in_doc_rejected PASSED [ 45%]
     tests/test_firestore_rules_challenge.py::test_unauthenticated_can_read_username_registry PASSED [ 50%]
     tests/test_firestore_rules_challenge.py::test_unauthenticated_can_read_public_user_profile PASSED [ 54%]
     tests/test_firestore_rules_challenge.py::test_unauthenticated_cannot_write_user_profile PASSED [ 59%]
     tests/test_firestore_rules_challenge.py::test_cross_user_cannot_write_user_profile PASSED [ 63%]
     tests/test_firestore_rules_challenge.py::test_owner_can_write_own_profile PASSED [ 68%]
     tests/test_firestore_rules_challenge.py::test_jobs_unauthenticated_read_rejected PASSED [ 72%]
     tests/test_firestore_rules_challenge.py::test_jobs_cross_user_read_rejected PASSED [ 77%]
     tests/test_firestore_rules_challenge.py::test_jobs_cross_user_write_rejected PASSED [ 81%]
     tests/test_firestore_rules_challenge.py::test_jobs_owner_can_read_and_write PASSED [ 86%]
     tests/test_firestore_rules_challenge.py::test_missing_uid_in_payload_rejected_on_create PASSED [ 90%]
     tests/test_firestore_rules_challenge.py::test_null_uid_in_payload_rejected_on_create PASSED [ 95%]
     tests/test_firestore_rules_challenge.py::test_empty_string_auth_uid_mismatch_rejected PASSED [100%]

     ============================= 22 passed in 0.07s ==============================
     ```

3. **Frontend Build & Typecheck**:
   - Command: `npm run lint; npm run build`
   - Result: Exit 0. Turbopack successfully compiled all routes (`/`, `/_not-found`, `/[username]`, `/account`, `/dashboard`) with 0 errors.

---

## 2. Logic Chain

1. **Claim Hijacking Prevention (Observation 1, lines 20-21; Observation 2)**:
   - *Create protection*: `allow create: if request.auth != null && request.resource.data.uid == request.auth.uid;`. A user can only create a claim document whose `uid` field matches their authenticated token. Any attempt to claim a username under someone else's UID is denied (`test_claim_hijack_spoofed_uid_creation_rejected` PASSED).
   - *Update protection*: `allow update: if request.auth != null && resource.data.uid == request.auth.uid && request.resource.data.uid == request.auth.uid;`. An attacker attempting to modify an existing claim document owned by another user cannot satisfy `resource.data.uid == request.auth.uid`, which rejects both UID hijacking and metadata tampering (`test_claim_hijack_cross_user_overwrite_rejected`, `test_claim_hijack_metadata_tampering_rejected` PASSED). Furthermore, an owner cannot transfer a claim to an arbitrary third-party UID without that third party authorizing it (`test_claim_hijack_owner_reassignment_to_third_party_rejected` PASSED).

2. **Claim Deletion Protection (Observation 1, line 22; Observation 2)**:
   - `allow delete: if request.auth != null && resource.data.uid == request.auth.uid;`. Deletions require explicit authentication and identity match against the document's stored `uid`. Cross-user deletion attempts and unauthenticated deletion attempts are denied (`test_claim_deletion_cross_user_rejected`, `test_claim_deletion_unauthenticated_rejected` PASSED).

3. **Public Profile Accessibility vs. User Profile Integrity (Observation 1, lines 6-8; Observation 2)**:
   - Public routing `/[username]` requires reading `/usernames/{name}` to resolve `uid`, then reading `/users/{uid}` for display name and avatar (`app/[username]/page.tsx:20-31`).
   - `allow read: if true;` on `/users/{uid}` and `/usernames/{name}` enables unauthenticated visitors to resolve and view profile pages without permission errors (`test_unauthenticated_can_read_public_user_profile`, `test_unauthenticated_can_read_username_registry` PASSED).
   - Writes on `/users/{uid}` are restricted to `request.auth != null && request.auth.uid == uid`, completely blocking unauthenticated or third-party tampering (`test_unauthenticated_cannot_write_user_profile`, `test_cross_user_cannot_write_user_profile` PASSED).

4. **Job Leakage & Privacy Isolation (Observation 1, lines 11-13; Observation 2)**:
   - In Firestore Rules v2, match blocks are non-recursive unless `{document=**}` is specified.
   - Therefore, `allow read: if true;` on `/users/{uid}` does NOT grant access to `/users/{uid}/jobs/{jobId}`.
   - Subcollection access explicitly requires `request.auth != null && request.auth.uid == uid`, preventing unauthenticated users and third parties from reading or writing private jobs (`test_jobs_unauthenticated_read_rejected`, `test_jobs_cross_user_read_rejected`, `test_jobs_cross_user_write_rejected` PASSED).

---

## 3. Caveats

1. **Document-Wide Read Scope on `/users/{uid}`**:
   - Firestore security rules operate at document granularity; field-level read masks are not natively supported in Firestore rules without schema redesign or Cloud Functions.
   - Any field stored directly in `/users/{uid}` (such as `occupation`, `place`, `rooms`, `onboarded` added during onboarding) is readable by unauthenticated callers. This matches the current architecture where `app/[username]/page.tsx` directly reads `/users/{uid}` on the client. Sensitive data should not be written directly to root `/users/{uid}` documents.
2. **First-Come, First-Served Unclaimed Namespace**:
   - Any authenticated user can create an unclaimed username mapping pointing to their own UID. This is expected behavior for public username registration.

---

## 4. Conclusion

**Verdict: APPROVE**

The security rules in `firestore.rules` are sound, robust, and correctly implement least-privilege identity guardrails for Milestone 1:
- R1 requirements from `ORIGINAL_REQUEST.md` are satisfied.
- Public profile pages at `/[username]` resolve for any visitor without Firestore permission errors.
- `/usernames/{name}` claims cannot be created for others, overwritten, or deleted by unauthorized third parties.
- Private jobs at `/users/{uid}/jobs/{jobId}` remain hermetically sealed to the account owner.
- Overall risk assessment: **LOW**.

---

## 5. Verification Method

To independently verify this evaluation:

1. **Run the empirical challenge test suite**:
   ```bash
   python -m pytest tests/test_firestore_rules_challenge.py -v
   ```
   *Expected outcome*: 22 tests pass in < 0.1s.

2. **Run frontend build & lint**:
   ```bash
   npm run lint
   npm run build
   ```
   *Expected outcome*: Zero errors, exit code 0.

3. **Invalidation condition**:
   Any test failure in `tests/test_firestore_rules_challenge.py` or regression in profile accessibility / ownership isolation invalidates this approval.
