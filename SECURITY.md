# Security

Supabase Auth provides identity. PostgreSQL row-level security isolates profiles and jobs by owner; the public profile directory intentionally exposes only its projection. Execution and account endpoints require verified bearer tokens and enforce ownership. Apply and test the SQL migration before exposing the application.

Keep the Supabase secret key and database URL on the backend only. The publishable key is intended for the browser and does not replace row-level security. Limit Google callback URLs and backend CORS origins to trusted deployments. Avoid logging access tokens or personal data.

Report security issues privately to the maintainers. Do not include credentials or personal data in public issues; rotate exposed credentials promptly.
