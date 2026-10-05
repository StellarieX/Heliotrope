# Handoff Report: Milestone 1 (Firestore Rules Author)

**Date**: 2026-10-05T11:35:00Z  
**Worker**: firestore-rules-author  
**Milestone**: M1 - R1 Security & Identity Guardrails  
**Target File**: `firestore.rules`  
**Parent Orchestrator ID**: `06dffeaf-8e62-4723-b704-1b7ef7cb5a98`

---

## 1. Observation

1. **Public Profile Route (`/[username]`) Permission Failure**:
   - `app/[username]/page.tsx` resolves a username by first reading `usernames/{name}` to obtain `claim.data().uid`, then reading `users/{uid}` to fetch `displayName` and `photoURL`.
   - Previously, `firestore.rules` had:
     ```javascript
     match /users/{uid} {
       allow read, write: if request.auth != null && request.auth.uid == uid;
       match /jobs/{jobId} {
         allow read, write: if request.auth != null && request.auth.uid == uid;
       }
     }
     ```
   - Because `read` on `/users/{uid}` required `request.auth != null && request.auth.uid == uid`, any unauthenticated visitor or authenticated user viewing another user's profile encountered a Firestore `permission-denied` error, triggering `.catch(() => setMissing(true))` and mistakenly showing `"Nobody here yet: @{name} isn't claimed."`.

2. **Username Claim Hijacking Vulnerability (`/usernames/{name}`)**:
   - Previously, `firestore.rules` permitted:
     ```javascript
     match /usernames/{name} {
       allow read: if true;
       allow write: if request.auth != null;
     }
     ```
   - Because `allow write: if request.auth != null;` placed zero ownership conditions on `update` or `delete`, any signed-in attacker could overwrite or delete any existing username document owned by another user.

---

## 2. Logic Chain

To resolve both vulnerabilities while upholding least privilege:

1. **Public Profile Read & Owner-Only Writes on `/users/{uid}`**:
   - Allowed public read: `allow read: if true;`.
   - Maintained owner-only writes: `allow write: if request.auth != null && request.auth.uid == uid;`.
   - Preserved private subcollection security on `/users/{uid}/jobs/{jobId}`:
     ```javascript
     match /jobs/{jobId} {
       allow read, write: if request.auth != null && request.auth.uid == uid;
     }
     ```
   - In Firestore Rules v2, rules are not recursive across collection matches unless wildcards (`{document=**}`) are specified. Therefore, granting public read on `/users/{uid}` does not expose the nested `/users/{uid}/jobs/{jobId}` subcollection.

2. **Granular Ownership Enforcement on `/usernames/{name}`**:
   - Decomposed the blanket `write` rule into `create`, `update`, and `delete`:
     - `allow read: if true;`: Allows public lookup of username claim mappings.
     - `allow create: if request.auth != null && request.resource.data.uid == request.auth.uid;`: Ensures a user can only create a claim doc referencing their own UID.
     - `allow update: if request.auth != null && resource.data.uid == request.auth.uid && request.resource.data.uid == request.auth.uid;`: Ensures that only the current claim owner can update the claim doc, and cannot transfer or reassign the UID to another user.
     - `allow delete: if request.auth != null && resource.data.uid == request.auth.uid;`: Ensures that only the current claim owner can release or delete the claim doc.

---

## 3. Caveats

1. **Document-Wide Firestore Read Scope**:
   - Firestore security rules apply to entire documents; field-level read masks are not supported. By allowing public read on `/users/{uid}`, all fields currently in that document (`displayName`, `photoURL`, `username`, `email`, `occupation`, `place`, `rooms`, `onboarded`) are readable by callers. This matches the existing architecture in `app/[username]/page.tsx` which directly reads `/users/{uid}`.
   - If private fields (like `email`) need strict lockdown in the future, non-public data should be partitioned into a private subcollection (e.g. `/users/{uid}/private/profile`) or public data denormalized into a dedicated public collection.
2. **Prototype Review**:
   - I've set up prototype Security Rules to keep the data in Firestore safe. They are designed to be secure by enforcing strict UID ownership checks on username claims and user document writes while allowing public profile resolution. However, you should review and verify them before broadly sharing your app. If you'd like, I can help you harden these rules.

---

## 4. Conclusion

The updated `firestore.rules` file has been written to `c:/Users/dhrri/Desktop/Heliotrope-main/firestore.rules`.
The rules strictly satisfy the R1 requirements from `ORIGINAL_REQUEST.md` and `PROJECT.md`:
- Visitors can view public profile data at `/[username]` without encountering Firestore permission denied errors.
- Authenticated users cannot create username claims for others, overwrite claims owned by others, or delete claims owned by others.
- All per-user jobs in `/users/{uid}/jobs/{jobId}` remain strictly restricted to the owning authenticated user.

---

## 5. Verification Method

1. **Syntax & Rule Structure Verification**:
   - Validated `firestore.rules` syntax against Firestore Rules v2 specification:
     - `rules_version = '2';`
     - Nested match paths `/users/{uid}` and `/users/{uid}/jobs/{jobId}`
     - Path `/usernames/{name}` with explicit `read`, `create`, `update`, and `delete` handlers
2. **Access Evaluation Matrix**:
   - `GET /users/{uid}`: Unauthenticated -> **Allowed**; Third-party user -> **Allowed**; Owner -> **Allowed**.
   - `WRITE /users/{uid}`: Unauthenticated -> **Denied**; Third-party user -> **Denied**; Owner -> **Allowed**.
   - `GET /users/{uid}/jobs/{jobId}`: Unauthenticated -> **Denied**; Third-party user -> **Denied**; Owner -> **Allowed**.
   - `POST /usernames/{name}` with `data.uid == auth.uid`: **Allowed**.
   - `POST /usernames/{name}` with `data.uid != auth.uid`: **Denied**.
   - `PUT /usernames/{name}` when `resource.data.uid == auth.uid` and `request.resource.data.uid == auth.uid`: **Allowed**.
   - `PUT /usernames/{name}` when `resource.data.uid != auth.uid`: **Denied**.
   - `DELETE /usernames/{name}` when `resource.data.uid == auth.uid`: **Allowed**.
   - `DELETE /usernames/{name}` when `resource.data.uid != auth.uid`: **Denied**.
3. **Application Build & Lint**:
   - `npm run lint` exited 0.
   - `npm run build` compiled all routes (including `/[username]`, `/account`, `/dashboard`) with 0 errors.
