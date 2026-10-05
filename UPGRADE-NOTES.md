# SiteLink v4 upgrade notes — SIH26122

This folder is a separate v4 copy of SiteLink v3. The original v3 folder and ZIP were not changed.

## Implementation summary

- Added `tenants`, `projects`, `project_members`, and `project_revisions` database tables. v3 SQLite data is migrated additively to the existing demo tenant/project.
- Scoped activities, reports, audit events, state revisions, and intelligence inputs by both tenant and project.
- Added membership and `view` permission checks to every project data API. Report submissions, reviews/approvals, and schedule edits check per-project grants as well as the server-side role.
- Added project listing, creation/edit, member assignment, permission updates, user detail/edit/delete, and optional public workspace sign-up (off by default).
- Added the actual project switcher, Admin project/member screens, project assignment fields during account creation, identity edit, deletion, and role-aware UI capabilities.
- Added PostgreSQL support using environment-driven `DATABASE_URL`; Docker Compose includes PostgreSQL, the app, and Caddy for HTTPS.
- Added `.env.example`, a first-run setup-token option, a non-root Docker image, persistent database/certificate volumes, and backup/deploy directions.

## Validation

SQLite verification passed: Python compile check, JavaScript syntax check, and all 13 unit/integration tests. These include v3 SQLite migration, separate sessions, project and tenant data isolation, authorization failures, project-level Supervisor approval grants, Viewer read-only behavior, and the previous intelligence flows.

This environment had no PostgreSQL server, Docker, or Caddy running. The PostgreSQL and public HTTPS deployment paths have not been exercised here; follow README.md and validate them in staging with synthetic data first.

## Important migration notes

- v3 SQLite to v4 SQLite: make a backup, copy `data/sitelink.sqlite3` to the v4 `data/` folder, then start v4. v4 migrates old activities/reports into the original demo project.
- SQLite to PostgreSQL: not automatic. Use a separately planned migration if existing v3 data needs to move to PostgreSQL.
- Public signup defaults off and does not include email verification. Admin-created accounts are the normal team onboarding path.
