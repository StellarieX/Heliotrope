# Security Policy

## Authentication model

- Google-only sign-in via Firebase Auth (`signInWithPopup`, Google provider).
- Session tracking via `onAuthStateChanged` (`app/page.tsx`, `app/dashboard/page.tsx`).
- Profile operations use `updateProfile` / `deleteUser`. No email/password, no custom tokens.
- Firestore is the only layer that verifies identity (`request.auth.uid`). The FastAPI backend does **not** verify Firebase ID tokens.

## Firestore rules matrix (`firestore.rules`, 25 lines)

| Collection | Read | Write | Condition |
|---|---|---|---|
| `users/{uid}` | public (`true`) | owner only | `request.auth != null && request.auth.uid == uid` |
| `users/{uid}/jobs/{jobId}` | owner only | owner only | `request.auth != null && request.auth.uid == uid` |
| `usernames/{name}` | public (`true`) | create: caller claims own UID | `request.resource.data.uid == request.auth.uid` |
| `usernames/{name}` | public (`true`) | update: old and new UID both match caller | `resource.data.uid == auth.uid && request.resource.data.uid == auth.uid` |
| `usernames/{name}` | public (`true`) | delete: caller owns claim | `resource.data.uid == request.auth.uid` |

Public `users` reads are intentional so `/[username]` pages resolve without login. Do not store secrets, tokens, or exact addresses in `users/{uid}` documents.

## Backend: no-auth warning

- `backend/app/main.py` exposes the API without JWT / Firebase token verification.
- `Participant.id` is a plain caller-supplied string; any client can assert any ID.
- CORS is the only gate (see below). It is not an authorization boundary.
- Threat model: anyone with the backend URL can call schedule, execution, and thermal endpoints, submit arbitrary participant IDs, and read SQLite-backed execution state. Do not deploy the backend to a public URL without adding Firebase ID-token verification (`firebase-admin`) and per-user scoping of `ExecutionStore` records.
- Adversarial coverage for the Firestore layer (not the backend): `tests/test_firestore_rules_challenge.py` (22 tests) and `tests/e2e/test_m1_adversarial_challenger.py` (32 tests) cover UID hijack, cross-user job reads, username-claim takeover/deletion, spoofed-UID writes, and prompt-injection payload handling.

## CORS

- Dev: `allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?"` in `backend/app/main.py`.
- Prod: origins from `CORS_ALLOW_ORIGINS` env (`backend/app/core/config.py`, comma-separated). Empty value means no extra origins beyond the localhost regex.
- Verified by `backend/tests/test_cors.py`.

## Secrets

- No `.env` files are committed. Template is `.env.example` (copy to `.env.local`).
- Frontend Firebase config lives in client code; restrict the API key with Firebase console authorized domains and App Check if abuse occurs.
- Backend secrets (when added): use `CORS_ALLOW_ORIGINS` and future `FIREBASE_*` / database-path vars via environment, never hardcoded. Local dev uses `.env.local` (gitignored) for Next.js and shell env / `.env` (gitignored) for uvicorn — confirm gitignore before adding.

## Reporting

Do not open public issues with credentials, tokens, or user PII. Rotate any leaked Firebase or hosting keys immediately and update authorized domains / CORS origins.
