# Handoff Report: Reviewer 1 (Milestone 1 — Security & Identity Guardrails)

**Date**: 2026-10-05T11:43:00Z  
**Reviewer**: teamwork_preview_reviewer (Reviewer 1)  
**Roles**: Reviewer, Critic  
**Milestone**: M1 - R1 Security & Identity Guardrails  
**Target File**: `firestore.rules`  
**Verdict**: **APPROVE**  
**Parent Orchestrator ID**: `06dffeaf-8e62-4723-b704-1b7ef7cb5a98`

---

## 1. Observation

1. **`firestore.rules` Contents (`c:/Users/dhrri/Desktop/Heliotrope-main/firestore.rules:1-26`)**:
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

2. **Public Profile Query Flow (`app/[username]/page.tsx:20-31`)**:
   - Line 20: `const claim = await getDoc(doc(db, "usernames", name));`
   - Line 25: `const snap = await getDoc(doc(db, "users", claim.data().uid as string));`
   - Line 31: `setProfile({ username: name, displayName: (d.displayName as string | null) ?? null, photoURL: (d.photoURL as string | null) ?? null });`

3. **Username Claim Modification Flow (`lib/username.ts:13-20` and `app/account/page.tsx:106-115`)**:
   - `lib/username.ts`:
     ```typescript
     tx.set(claimRef, { uid, updatedAt: serverTimestamp() });
     tx.set(doc(db, "users", uid), { username: v, ...profile, updatedAt: serverTimestamp() }, { merge: true });
     if (prev && prev !== v) tx.delete(doc(db, "usernames", prev));
     ```
   - `app/account/page.tsx`:
     ```typescript
     tx.set(claimRef, { uid: user!.uid, updatedAt: serverTimestamp() });
     tx.set(userRef, { username: v, displayName: user!.displayName ?? null, email: user!.email ?? null, photoURL: user!.photoURL ?? null, updatedAt: serverTimestamp() }, { merge: true });
     if (prev) tx.delete(prev);
     ```

4. **Private Per-User Jobs Operations (`app/dashboard/page.tsx:158, 335, 379` & `app/dashboard/Onboarding.tsx:78`)**:
   - Read collection: `getDocs(collection(db, "users", u.uid, "jobs"))`
   - Create job: `addDoc(collection(db, "users", user.uid, "jobs"), { ... })`
   - Delete job: `deleteDoc(doc(db, "users", user.uid, "jobs", id))`

5. **Build and Lint Verification**:
   - `npm run lint` exited 0:
     ```
     > heliotrope@0.1.0 lint
     > eslint
     ```
   - `npm run build` exited 0:
     ```
     ✓ Compiled successfully in 803ms
     ✓ Finished TypeScript in 995ms
     ✓ Generating static pages using 8 workers (6/6) in 744ms
     Route (app)
     ├ ○ /
     ├ ○ /_not-found
     ├ ƒ /[username]
     ├ ○ /account
     └ ○ /dashboard
     ```

6. **Integrity Violations Check**:
   - No hardcoded test results or mock returns found in `firestore.rules`.
   - No dummy/facade implementations.
   - No bypassed rules or shortcut backdoors.

---

## 2. Logic Chain

1. **R1 Criterion 1 — Public Profile Route (`/[username]`) Resolvability**:
   - **Observation Ref**: Observation 1 & 2.
   - `app/[username]/page.tsx` executes two read operations: `getDoc` on `usernames/{name}` followed by `getDoc` on `users/{claim.uid}`.
   - In `firestore.rules`, `match /usernames/{name}` contains `allow read: if true;`, and `match /users/{uid}` contains `allow read: if true;`.
   - Any visitor (unauthenticated or authenticated) is granted read permission on both documents.
   - When a visitor visits `/[username]`, no Firestore `permission-denied` exception is thrown. If the username does not exist or user does not exist, `.exists()` evaluates cleanly to false without permission errors, rendering the expected missing UI.
   - Conclusion: Criterion 1 is fully satisfied.

2. **R1 Criterion 2 — Protection of `/usernames/{name}` Claims Against Unauthorized Third-Party Operations**:
   - **Observation Ref**: Observation 1 & 3.
   - **Creation**: `allow create: if request.auth != null && request.resource.data.uid == request.auth.uid;`
     - Requires authentication.
     - Enforces that the incoming document's `uid` matches the caller's authenticated UID. A third party cannot register a claim on behalf of another user's UID.
   - **Update/Overwrite**: `allow update: if request.auth != null && resource.data.uid == request.auth.uid && request.resource.data.uid == request.auth.uid;`
     - Evaluated when an existing document is targeted (including via `setDoc` or transaction).
     - Requires that the current stored `resource.data.uid` equals `request.auth.uid` (only the owner can update).
     - Also requires that `request.resource.data.uid` equals `request.auth.uid`, preventing an owner from maliciously or mistakenly transferring/reassigning the claim to another UID.
     - Any third party attempting to overwrite a claim owned by another user fails with `permission-denied`.
   - **Deletion**: `allow delete: if request.auth != null && resource.data.uid == request.auth.uid;`
     - Requires that the current stored document's `resource.data.uid` equals `request.auth.uid`.
     - Third parties attempting to delete another user's claim fail with `permission-denied`.
   - Conclusion: Criterion 2 is fully satisfied.

3. **R1 Criterion 3 — Private Per-User Jobs (`/users/{uid}/jobs/{jobId}`) Isolation**:
   - **Observation Ref**: Observation 1 & 4.
   - `match /jobs/{jobId}` inside `match /users/{uid}` specifies:
     `allow read, write: if request.auth != null && request.auth.uid == uid;`
   - In Firestore Rules v2, rules are non-recursive. Granting `allow read: if true;` on `/users/{uid}` does NOT propagate to the subcollection `/users/{uid}/jobs/{jobId}`.
   - Third parties or unauthenticated callers attempting to read a single job document or query the `/users/{uid}/jobs` collection will receive `permission-denied`.
   - Only the owning authenticated user (`request.auth.uid == uid`) can read, list, create, update, or delete jobs in their subcollection.
   - Conclusion: Criterion 3 is fully satisfied.

4. **R1 Criterion 4 — Frontend Build & Lint Verification**:
   - **Observation Ref**: Observation 5.
   - Both `npm run lint` and `npm run build` executed and exited with status code 0.
   - Next.js compiled all static and dynamic routes cleanly.

---

## 3. Caveats

1. **Document-Level Read Scope on `/users/{uid}`**:
   - Cloud Firestore security rules operate at document granularity; field-level read masks are not supported natively in rules.
   - Because `match /users/{uid}` specifies `allow read: if true;`, all fields currently written to `/users/{uid}` (such as `email`, `occupation`, `place`, `rooms`, `onboarded`) are readable by anyone who queries `/users/{uid}` directly.
   - This matches the project architecture where `app/[username]/page.tsx` reads directly from `/users/{uid}`.
   - Recommendation for future architectural hardening: If user email or sensitive settings require strict privacy, partition non-public fields into a private subcollection (e.g., `/users/{uid}/settings/private`) or denormalize public profile attributes into a separate `public_profiles/{uid}` collection.
2. **Document ID Format / Length Validation**:
   - Client code in `lib/username.ts` validates usernames via regex `/^[a-z0-9_]{3,20}$/`.
   - The security rules enforce ownership and UID binding, but do not enforce document ID character set constraints at the rules level. This is standard in Firestore rules when ownership invariants are strictly maintained, but worth noting for comprehensive security posture.

---

## 4. Adversarial Challenge & Stress-Test Assessment

### Overall Risk Assessment: LOW (All attack scenarios mitigated)

### Challenges Evaluated:

1. **Attack Scenario: Claim Hijacking via Overwrite (`setDoc` on existing claim)**
   - *Attack*: Attacker signs in with `attacker_uid`, calls `setDoc(doc(db, "usernames", "victim_name"), { uid: "attacker_uid" })`.
   - *Rule Evaluation*: Since document exists, Firestore evaluates `allow update`. Rule checks `resource.data.uid == request.auth.uid`. `resource.data.uid` is `victim_uid` != `attacker_uid`.
   - *Result*: Blocked with `permission-denied`. **PASSED**.

2. **Attack Scenario: Unauthorized Claim Deletion**
   - *Attack*: Attacker signs in with `attacker_uid`, calls `deleteDoc(doc(db, "usernames", "victim_name"))`.
   - *Rule Evaluation*: `resource.data.uid == request.auth.uid` fails.
   - *Result*: Blocked with `permission-denied`. **PASSED**.

3. **Attack Scenario: Reassignment / UID Swap on Existing Claim**
   - *Attack*: Legitimate owner `alice` attempts to update claim `usernames/alice` to set `uid: "bob_uid"`.
   - *Rule Evaluation*: `request.resource.data.uid == request.auth.uid` checks if new data uid matches alice. Because new uid is `bob_uid`, it fails.
   - *Result*: Blocked with `permission-denied`. **PASSED**.

4. **Attack Scenario: Unauthenticated Job Subcollection Scraping**
   - *Attack*: Unauthenticated visitor requests `getDocs(collection(db, "users", "victim_uid", "jobs"))`.
   - *Rule Evaluation*: `match /jobs/{jobId}` requires `request.auth != null && request.auth.uid == uid`. Unauthenticated visitor has `request.auth == null`.
   - *Result*: Blocked with `permission-denied`. **PASSED**.

5. **Attack Scenario: Cross-Tenant Job Tampering**
   - *Attack*: User `alice` attempts to write `users/bob/jobs/evil_job`.
   - *Rule Evaluation*: Path variable `uid` is `"bob"`. `request.auth.uid` is `"alice"`.
   - *Result*: Blocked with `permission-denied`. **PASSED**.

---

## 5. Conclusion & Verdict

**Verdict**: **APPROVE**

The implementation in `firestore.rules` is concise, correct, robust, and completely satisfies the Milestone 1 acceptance criteria from `ORIGINAL_REQUEST.md` and `PROJECT.md`:
1. Any visitor can read public profile data at `/[username]` without encountering permission-denied errors.
2. Third-party users cannot create claims for other UIDs, overwrite claims owned by others, or delete claims owned by others.
3. Private per-user jobs in `/users/{uid}/jobs/{jobId}` remain strictly isolated to the authenticated owner.
4. Next.js application compiles cleanly and passes linting (`npm run lint` and `npm run build` both exit 0).
5. Zero integrity violations detected.

---

## 6. Verification Method

To independently verify this evaluation:

1. **Verify Security Rules Syntax & Rules Version**:
   - Inspect `c:/Users/dhrri/Desktop/Heliotrope-main/firestore.rules`.
   - Confirm `rules_version = '2';`.
   - Confirm granular `read`, `create`, `update`, `delete` definitions under `match /usernames/{name}`.
   - Confirm private protection under `match /users/{uid}/jobs/{jobId}`.

2. **Verify Next.js Linting & Compilation**:
   ```powershell
   cd c:\Users\dhrri\Desktop\Heliotrope-main
   npm run lint
   npm run build
   ```
   *Expected*: Both exit code 0.
