# Progress Log: Milestone 1 (Firestore Rules Author)

**Last visited**: 2026-10-05T11:34:00Z  
**Worker**: firestore-rules-author  
**Milestone**: M1 - R1 Security & Identity Guardrails  
**Target File**: `firestore.rules`

## Current Status
- [x] Read authoritative requirements (`ORIGINAL_REQUEST.md`), project scope (`PROJECT.md`), and survey findings (`handoff.md`).
- [x] Inspected existing `firestore.rules` and codebase Firestore usages (`app/`, `lib/`).
- [x] Identified vulnerability surface:
  1. `/users/{uid}` read was restricted to `request.auth.uid == uid`, causing permission denied errors on `/[username]` public profile route.
  2. `/usernames/{name}` write was permitted for any authenticated user (`allow write: if request.auth != null;`), allowing third parties to overwrite or delete username claim documents owned by others.
- [x] Implemented R1 security rules in `firestore.rules`:
  1. On `/users/{uid}`: `allow read: if true;` and `allow write: if request.auth != null && request.auth.uid == uid;`.
  2. On `/users/{uid}/jobs/{jobId}`: maintained private subcollection access `allow read, write: if request.auth != null && request.auth.uid == uid;`.
  3. On `/usernames/{name}`:
     - `allow read: if true;`
     - `allow create: if request.auth != null && request.resource.data.uid == request.auth.uid;`
     - `allow update: if request.auth != null && resource.data.uid == request.auth.uid && request.resource.data.uid == request.auth.uid;`
     - `allow delete: if request.auth != null && resource.data.uid == request.auth.uid;`
- [x] Verified frontend compilation and linting: `npm run lint` and `npm run build` both exit 0.
- [x] Verified rules syntax and logical authorization conditions against all CRUD vectors.
- [ ] Write `handoff.md` and notify parent orchestrator (`06dffeaf-8e62-4723-b704-1b7ef7cb5a98`) via `send_message`.
