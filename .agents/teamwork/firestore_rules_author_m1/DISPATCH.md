# Task Assignment: Milestone 1 Worker (Firestore Rules Author)

You are the Worker for Milestone 1: R1 Security & Identity Guardrails.
- TypeName: firestore-rules-author
- Role: Firestore Security Rules Specialist
- Working directory: c:/Users/dhrri/Desktop/Heliotrope-main/.agents/teamwork/firestore_rules_author_m1/
- Parent Orchestrator conversation ID: 06dffeaf-8e62-4723-b704-1b7ef7cb5a98
- Authoritative requirements: c:/Users/dhrri/Desktop/Heliotrope-main/.agents/teamwork/ORIGINAL_REQUEST.md
- Project Scope Document: c:/Users/dhrri/Desktop/Heliotrope-main/.agents/teamwork/PROJECT.md
- Technical findings reference: c:/Users/dhrri/Desktop/Heliotrope-main/.agents/teamwork/teamwork_preview_explorer_survey_1/handoff.md

You MUST read c:/Users/dhrri/Desktop/Heliotrope-main/.agents/teamwork/ORIGINAL_REQUEST.md before starting work.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Write Ownership:
You have exclusive write ownership of:
- `c:/Users/dhrri/Desktop/Heliotrope-main/firestore.rules`

Objectives:
1. Fix Firestore security rules so public profile pages at `/[username]` can be viewed by any visitor without permission errors:
   - On `/users/{uid}`, allow public reads (`allow read: if true;`) so unauthenticated visitors and logged-in visitors can view public profile data (`displayName`, `photoURL`, `username`).
   - Retain strict owner write: `allow write: if request.auth != null && request.auth.uid == uid;`.
   - Maintain private subcollection access on `/users/{uid}/jobs/{jobId}` (`allow read, write: if request.auth != null && request.auth.uid == uid;`).
2. Protect `/usernames/{name}` claims so documents cannot be overwritten or deleted by unauthorized third-party users:
   - `allow read: if true;`
   - `allow create: if request.auth != null && request.resource.data.uid == request.auth.uid;`
   - `allow update: if request.auth != null && resource.data.uid == request.auth.uid && request.resource.data.uid == request.auth.uid;`
   - `allow delete: if request.auth != null && resource.data.uid == request.auth.uid;`
3. Verify syntax and logic.
4. Maintain `progress.md` with timestamps.
5. When complete, write `handoff.md` with Observation, Logic Chain, Caveats, Conclusion, and Verification Method, and send a completion message to the parent orchestrator via `send_message`.
