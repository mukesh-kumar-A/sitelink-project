# SiteLink v4 — multi-user, multi-tenant project execution prototype

SiteLink captures field progress, links it to schedule activities, and keeps project data isolated by workspace and project membership. This is a versioned v4 copy; v3 remains unchanged.

All included project records and labeled examples are synthetic. Do not enter confidential, personal, or live project information in this hackathon prototype.

## What changed in v4

- Added workspaces (tenants), projects, memberships, per-project permission grants, and project-scoped revision checks.
- Every project API verifies the signed-in user's tenant and membership on the server. Changing a project ID in a URL or request does not grant access.
- Added a working project switcher and Admin controls to create/edit projects, assign members, adjust project permissions, edit/disable/reset/delete user accounts, and inspect assigned project access.
- Added public self-service signup in the configured existing workspace. New accounts are pending, default to Viewer, and receive no project memberships until an Admin approves and assigns them.
- Added optional PostgreSQL support through `DATABASE_URL`; SQLite stays the default for local development. Docker Compose starts PostgreSQL, the app, and a Caddy HTTPS reverse proxy.
- Added environment-based session, setup-token, sign-up, and database configuration. Production first-run Admin setup can be protected with a one-time token.
- Existing SQLite v3 databases migrate additively into the original demo tenant/project. Record payload IDs stay the same; internal keys are project-scoped.
- Added evidence-backed supervisor capture, planner approval/rejection/unmatched review, searchable report history, and approval-linked schedule/audit updates.
- Added report and schedule CSV/XLSX import, image/PDF text extraction, evidence-validated optional LLM assistance, heuristic semantic matching, and a Microsoft Project XML adapter/demo exchange path.
- Dashboard summaries and execution memory use planner-approved report records. Project memory also answers questions from planner-recorded action outcomes; small synthetic benchmark results are saved under `sample-data/measured_results.json`.
- Added **Plans & baselines**, **Project areas**, **Today & work log**, and **Risks & issues** to the authenticated application. Plan versions, approved baseline snapshots, daily execution records, risks, and notifications are stored in the project-scoped database.
- Plan uploads remain proposals through extraction and editable review; explicit approval creates a new baseline revision. Daily updates record progress, start/finish events, evidence files, manpower, materials, equipment, safety notes, and blockers. Planner approval alone updates activity actuals and produces before/after audit evidence.
- Weighted project progress uses configured weights when available, then planned quantities, then planned duration. Each result states its weighting method. Activity conditions use schedule dates, approved update cadence, and project risks; these are explainable status calculations, not schedule forecasts.
- Added **Execution intelligence** to connect the live predecessor graph, approved daily evidence, open risks, downstream impacts, area/sub-area summaries, history-based estimates with explanations, recovery scenarios, next-day proposals, human-approved actions, and recorded outcomes. These records use the existing tenant/project access checks and audit trail.

## Role-first navigation and project home

- The sidebar now follows **Home → Capture & Review → AI Agent → Needs attention → Intelligence → Project → SiteLink AI → More**. Navigation rows share a consistent style; **AI Agent** is a standalone primary-navigation link outside every collapsible group and uses a distinct sparkle icon and AI badge, so it stays easy to spot without looking selected on other pages. **Capture & Review** contains Today & work log and Report inbox; the separate **SiteLink AI** group contains Ask & investigate, Execution memory, and Evidence insights. No existing view was removed.
- Project Home is composed from the existing project-control and execution-intelligence APIs. It shows weighted progress and its calculation basis, the approved baseline, a clearly labeled history-based completion estimate when supported, today’s scheduled activities, current attention conditions, proposed decisions, and evidence-backed cause/impact summaries.
- A project execution thread on Home links the stored baseline, approved field updates, evidence, causes/impacts, forecast calculations, recovery reviews, actions/outcomes, and completed execution history to their working screens. Counts and source IDs come from project records; empty stages remain visibly empty. Reported constraints and heuristic inferences are labeled, and a missing approved-history basis does not produce a completion estimate.
- Recording a recovery outcome requires the measured result and a planner-written lesson. Both are saved with the action and shown in project execution memory, so future answers can cite the observed result and lesson instead of treating a proposal as proven.
- **Ask SiteLink** is available in the top bar, overview, activity details, and its dedicated screen. `POST /api/ai/ask` and `POST /api/ai/investigate` use the authenticated selected project, with optional activity or area scope. Responses separate deterministic calculations, approved evidence, inferences, unknowns, and any AI-assisted explanation. Model citations must refer to an authorized record and an exact quote. The assistant is read-only; it cannot approve or edit schedule records.
- The activity register opens an activity detail panel with planned/actual values, linked and suggested reports, source text, extracted evidence, and related audit events. Suggested matches are explicitly separated from planner-approved schedule actuals.
- Dependency graph and CPM detail are collapsed by default. Open **Schedule structure** from Execution analysis when a planner needs the graph, dependencies, and critical path; evidence summaries and decision workflows remain directly visible.

## Site report workflow and supported formats

- **Supervisor capture:** use **AI Time Agent** to type or dictate from a phone-friendly screen. Enter an optional confirmed event date and progress in separate fields when the source does not state them. The report-received date is not used as an actual start or finish date.
- **Evidence-first extraction:** SiteLink extracts activity description, discipline, location, line/equipment/activity IDs, event status/date, progress, times, and blockers. Extracted values include supporting source text or a clearly marked structured field. Ambiguous slash dates, missing years, unrelated dates, and multiple dates are left unresolved.
- **Planner review:** low-confidence and unmatched records remain searchable. A Planner can edit dates/progress, confirm a candidate, approve, reject, or keep a report unmatched. Only approval updates schedule actuals. Approval and activity before/after values are recorded in the audit trail with the signed-in reviewer and timestamp.
- **Report spreadsheets:** import `.csv` and `.xlsx`; `.txt` diaries are also supported. Common columns include `report_id`, `report_date`, `event_date`, `discipline`, `text`/`description`, `reporter`, `location`, `line_tag`, `equipment_tag`, `activity_id`, `actual_start`, `actual_end`, and `progress`.
- **Schedule spreadsheets:** Planners/Admins can import `.csv` and `.xlsx` with `activity_id`, `activity_name`, `discipline`, `location`, `planned_start`, and `planned_finish`; optional columns include WBS, actuals, progress, owner, predecessors, contractor, and aliases.
- **Scanned diaries:** from **AI Time Agent → Scan diary / photo**, choose a PNG/JPEG/TIFF/BMP/WebP photo or PDF. Extracted text is placed in the editable report text box; it is not saved until the supervisor reviews and submits it. OCR uses Tesseract. Scanned PDF pages also need Poppler; PDFs with a text layer use that text directly. Uploads are limited to 8 MB and 8 pages for OCR PDFs.
- **MS Project exchange:** schedule import accepts Microsoft Project XML (MSPDI), and the export dialog includes **MS Project XML adapter export**. This is a labeled exchange/demo adapter, not a live Primavera or MS Project connection. Primavera XER and live PMIS synchronization are not implemented.

### Optional LLM and OCR configuration

The report Time Agent calls an OpenAI Responses API-compatible endpoint only when `TIME_AGENT_ENABLED=1`, `OPENAI_API_KEY`, and `TIME_AGENT_MODEL` are configured on the **server**. Ask SiteLink's explanation layer uses the same server-side key when `SITELINK_AI_ENABLED=1`, `OPENAI_API_KEY`, and `SITELINK_AI_MODEL` are configured. It can use a compatible endpoint through `SITELINK_AI_API_URL` and bounded timeout/retry settings. The API key is never shipped in browser code. Structured JSON output is schema-validated, and explanations are displayed only when each cited record ID is authorized and each cited quote appears verbatim in its approved source text. Otherwise SiteLink returns its local deterministic answer. The user's question is not copied into the audit event; intent, provider, latency, scope, validation outcome, and evidence count are recorded. The local evidence-based extractor and read-only question flow remain available when configuration is absent, a request fails, or validation fails. Intent and match scores remain heuristic and uncalibrated.

For Docker, copy `.env.example` to `.env`, then set the variables for the feature you want. For local PowerShell, set variables in the same terminal before `py server.py`:

```powershell
$env:TIME_AGENT_ENABLED = "1"
$env:OPENAI_API_KEY = "your-server-side-key"
$env:TIME_AGENT_MODEL = "your-enabled-model"
# Optional AI explanation layer for Ask SiteLink:
$env:SITELINK_AI_ENABLED = "1"
$env:SITELINK_AI_MODEL = "your-enabled-model"
py server.py
```

The AI explanation and extraction settings are independent. Leave `OPENAI_API_KEY` empty or either feature disabled to use the local deterministic path. `GET /api/ai/status?projectId=…` reports whether provider settings are configured to signed-in project members without returning credentials; it does not make a live provider request. Local OCR setup requires `py -m pip install -r requirements.txt` plus a Tesseract installation available on `PATH`; install Poppler and add it to `PATH` for scanned PDFs. The Docker image installs Tesseract and Poppler. CSV and XLSX parsing, matching, and MS Project XML exchange use the included Python code.

### Ask SiteLink response contract

The dedicated **Ask SiteLink** screen accepts a natural-language question, optional activity ID, or area investigation. `POST /api/ai/ask` and `POST /api/ai/investigate` require the existing signed-in session, CSRF token, and access to the selected project. Context is assembled from that project only; activity and area queries reduce the schedule, evidence, history, and graph passed to the explanation provider. Only planner-approved updates/reports can appear as cited source evidence. The response includes `intent`, `facts`, `evidence`, `evidence_citations`, `inferences`, `unknowns`, `assumptions`, possible affected activities, provider/fallback state, evidence count, and a read-only flag. `POST /api/ai/extract/field-report` is an alias for the existing report extraction route. The older `/api/memory` route remains available for compatibility.

### Synthetic labeled example results

Run `py evaluate_examples.py` to reproduce the five cases in `sample-data/labeled_examples.json`. The checked-in `sample-data/measured_results.json` reports **31/31 exact expected-field checks (100%)**, **5/5 top-1 activity matches**, and **5/5 top-3 matches** for this small synthetic fixture using local extraction and heuristic ranking. This does not measure real site reports, establish production accuracy, or calibrate the displayed scores. The fixture includes examples for instrumentation, piping starts, hydrotesting, civil blockers, and a start report with an absent event date.

## Local run on Windows (SQLite)

1. Install Python 3.10 or newer. Extract/open this folder in VS Code.
2. Open **Terminal → New Terminal** and check Python:

   ```powershell
   py --version
   ```

3. Start the server:

   ```powershell
   py server.py
   ```

   If `py` is not recognized, use `python server.py`. SiteLink creates `data/sitelink.sqlite3` and prints a local URL and, when available, a private LAN URL.

4. Open `http://127.0.0.1:8000` on the server PC and create the first Admin account with a 6–8 character password. There are no default credentials.
5. In **Users & roles**, add each teammate, select their role, and assign one or more projects. Use **Create project** and **Manage members** to create isolated projects and change project permissions.
6. A LAN URL works only when the server PC and teammate devices can reach each other. For devices on different networks, deploy behind a public HTTPS domain using the cloud/server steps below. Do not expose the local HTTP server directly to the internet.

To stop the local server, press **Ctrl+C** in the terminal. `RUN-SITELINK.bat` starts the same local SQLite server and opens the local URL.

### Let teammates connect on the same Wi-Fi/LAN (Windows)

The server already listens on all network interfaces. A browser timeout at a private address such as `192.168.x.x:8000` usually means Windows Firewall or the Wi-Fi network is blocking device-to-device traffic.

1. On the server PC, set the trusted Wi-Fi/Ethernet network profile to **Private** in Windows Settings → Network & internet.
2. Right-click `ALLOW-TEAM-ACCESS.bat` and select **Run as administrator**. It adds an inbound TCP 8000 rule for the **Private** profile and local subnet only.
3. Start SiteLink with `py server.py` and leave its terminal open. Use the current `Team:` URL printed in that window; the local IP can change when reconnecting to Wi-Fi.
4. Have teammates open that exact URL while connected to the same Wi-Fi/LAN. Do not use `127.0.0.1` on their computers; that address refers to their own computer.

If teammates still get a timeout, confirm both devices are on the same network and check whether guest Wi-Fi or the phone hotspot has client isolation enabled. A `192.168.x.x` address does not work across different networks; teammates elsewhere need a central HTTPS deployment as described below. Keep the private-LAN rule off public network profiles.

### Run the existing v3 data in v4 (SQLite only)

Stop v3 first. Copy the v3 file `data/sitelink.sqlite3` into this v4 folder's `data` directory, then start v4. The first startup adds tenant/project tables and assigns existing records to the default synthetic demo project. Keep a separate backup of the original database before migrating. The automatic migration is SQLite-to-SQLite; it does not move a SQLite database into PostgreSQL.

## Deploy for access from different networks (Docker Compose + PostgreSQL + HTTPS)

This is the provided single-server cloud setup. It uses one central PostgreSQL database for all workspaces and projects. A browser only needs the deployed domain; teammates do not run Python or share a LAN. The project contains a PostgreSQL adapter, but this environment had no PostgreSQL/Docker service available, so the PostgreSQL deployment path could not be exercised here. SQLite integration tests passed; run a staging deployment before entering any real data.

For a concise deployment checklist, see `DEPLOY-REMOTE.txt`. This project folder includes the required Docker Compose and Caddy configuration, but it does not include a public server, DNS domain, or TLS certificate. Those are required for teammates on different networks.

You need a Linux server/VPS with Docker Compose, a domain name, and DNS A/AAAA records pointing to that server. Allow inbound TCP 80 and 443 (and optionally UDP 443) in the cloud firewall.

1. Upload this folder to the server and enter it:

   ```sh
   cd /path/to/SiteLink-Prototype-v4
   cp .env.example .env
   ```

2. Edit `.env` and set:
   - `SITE_DOMAIN` to your real domain, such as `sitelink.example.com`.
   - `POSTGRES_PASSWORD` to a long random password.
   - `SETUP_TOKEN` to a different long random one-time setup token.
   - Keep `REQUIRE_SETUP_TOKEN=1`, `PUBLIC_SIGNUP=1`, `SIGNUP_TENANT_ID=tenant-default`, and `COOKIE_SECURE=auto` for first deployment. Change `SIGNUP_TENANT_ID` when the existing workspace uses another tenant ID.

   Generate random values on the server with `openssl rand -hex 32`. Do not commit or share `.env`.

3. Start the services:

   ```sh
   docker compose up -d --build
   docker compose ps
   docker compose logs -f app proxy
   ```

   Caddy obtains and renews the domain's HTTPS certificate after DNS is correct and ports 80/443 are reachable. PostgreSQL data is stored in the persistent `postgres_data` volume; Caddy certificates/configuration have their own persistent volumes. The app port is only exposed to the internal Compose network.

4. Open `https://YOUR-DOMAIN` and enter the one-time `SETUP_TOKEN` when creating the first Admin. After setup succeeds, change `REQUIRE_SETUP_TOKEN=0` in `.env`, then restart the app:

   ```sh
   docker compose up -d app
   ```

   Keep the PostgreSQL password and `.env` private. Do not open port 8000 publicly.

5. Teammates choose **Create a SiteLink account** from the sign-in page. The account remains pending until an Admin approves it under **Users & roles → Pending Users**. Then assign its role and project memberships. Teammates use the same HTTPS domain from their own networks and sign in with their own credentials. Each browser gets its own HttpOnly session cookie; sessions are not stored in local storage.

### PostgreSQL and configuration

- Compose supplies `DATABASE_URL` to the app from `POSTGRES_PASSWORD`. A separate, isolated PostgreSQL database is not created per user; tenant/project IDs scope data inside the central database.
- On startup, v4 creates the tables and indexes it needs. SQLite schema upgrades are additive. For an existing SQLite deployment moving to PostgreSQL, use a separately planned/exported data migration: v4 does not automatically transfer a SQLite file into Postgres.
- Use a managed PostgreSQL service instead of the bundled Compose `db` if your host requires it; set `DATABASE_URL` to its private connection string and remove/disable the Compose `db` service.
- `SESSION_HOURS` sets session lifetime (1–168 hours; default 12). `COOKIE_SECURE=auto` adds the Secure cookie flag when the reverse proxy forwards HTTPS. `PUBLIC_SIGNUP=1` enables self-service accounts in the existing workspace; `SIGNUP_TENANT_ID` selects that tenant on the server. The signup form never accepts a tenant, role, or project ID. There is no email verification or invitation flow.
- No `SECRET_KEY` is required: sessions are random opaque tokens, stored in the database only as SHA-256 hashes. The PostgreSQL connection and setup token are supplied via environment variables, not source files.
- The browser UI and API are served from one origin, so no permissive CORS policy is enabled. Writes require a CSRF token and SameSite cookie.

### Backups and operations

Back up PostgreSQL regularly and keep the backup off the server:

```sh
docker compose exec -T db pg_dump -U sitelink sitelink > sitelink-backup.sql
```

Check service status/logs with `docker compose ps` and `docker compose logs app db proxy`. Restoring a backup should be rehearsed on a staging copy first. Back up `.env` securely too; it contains database credentials.

## Plan review and daily field execution

1. Open **Plans & baselines** and upload a synthetic schedule CSV/XLSX or MSPDI XML. PDF/image plans can be uploaded for OCR; without a configured model, SiteLink shows the transcription but does not fabricate schedule rows.
2. Analyze the version. Confirm the extracted fields against the source file, edit the structured activity rows in the review section if needed, then approve with a reason. The new version and snapshot are retained in baseline history; actual dates/progress from matching activities are preserved.
3. Open **Today & work log** as a Supervisor. Choose the scheduled activity, work date, progress, event type/time, and write what happened. The optional Time Agent ranks up to three candidate matches with heuristic reasons and extracted source evidence; use **Select** only to populate the draft. Attach source files and submit for review.
4. Sign in as Planner or Project Manager, inspect the queued report and its evidence, then approve or reject it with a reason. A start/finish actual is only written on approval. Rejected and unmatched source reports remain in history. Use **Audit trail** to review the recorded actor and before/after values.
5. Use **Project areas** to create area/sub-area structure and **Risks & issues** to record project risks, mitigations, owners, and status. Values derive from stored synthetic records.

## Execution intelligence: judge walkthrough

The **Execution intelligence** workspace extends SiteLink's existing schedule, daily capture, report review, risk register, and audit screens.

1. Sign in as an Admin, open **Execution intelligence**, and select **Load synthetic story**. This idempotent, project-scoped action creates a labeled four-activity material → installation → calibration → commissioning chain, a sample approved baseline revision, two synthetic approved progress updates, a synthetic text diary, one open material risk, and one milestone. It records a demo-seed audit event. Do this only in a disposable synthetic project.
2. Inspect **Live dependency graph** and **Cause and downstream impact**. The graph is calculated from saved schedule predecessors/dependency types. Open risks and planner-approved updates are eligible cause inputs; pending reports are not. The view links the source records, synthetic diary evidence, areas, paths, and affected milestone.
3. In **Recovery simulator**, state the assumed resource change, fallback calendar days recovered, written assumptions, and reason. When two or more dated approved progress observations exist, the estimate shows remaining work and observed daily progress. For a crew scenario, the latest approved crew count can support a clearly labeled linear productivity assumption; a planner may enter an explicit productivity-change assumption when the project records do not support that calculation. Material and equipment state are shown as inputs and do not produce an invented productivity benefit. A parallel-work proposal must reference a direct Finish-to-Start successor and pass a saved-graph cycle check. The proposal never edits schedule links or dates. Approving a scenario creates an action record only.
4. Generate **Tomorrow’s execution plan** from current supported causes. Each deterministic proposal includes its source records, reason, activity, priority, and confidence boundary. Edit, approve, or reject it; approval alone creates an action.
5. On an approved action, enter the observed result and an observed finish date or measured days recovered. SiteLink stores the outcome, before/after estimate, user, time, reason, and optional approved daily-update link. The recorded result informs later estimates but does not write activity actuals.
6. Inspect **Project execution thread** and **Audit trail** for baseline, evidence, approved updates, cause, impacts, scenario, decision, action, and outcome. Separately submit a dated field update and approve it as Planner to show the existing approval-gated schedule actual flow.

### Calculation and confidence boundaries

- The dependency graph calculates calendar-day CPM when each activity has a positive recorded `durationDays`, a milestone zero duration, or an inclusive planned start/finish pair. It supports FS, SS, FF, and SF links with integer calendar-day lag, computes ES/EF/LS/LF and total/free float, and identifies zero/negative-float critical activities and linked paths. Planned starts are treated as earliest-start constraints; saved planned finishes contribute to the project finish deadline. Unknown predecessor references and cycles disable CPM and stay visible. This is a calendar-day calculation without working calendars, resource leveling, or a live PMIS connection.
- Cause categories use an explicitly recorded category where present, then a small word-boundary terminology map. The UI exposes the source record and classification basis; this is not a trained cause classifier.
- A downstream delay number appears only with a source-record estimate or elapsed days after the root activity's planned finish. The latter is labeled a retrospective indicator. Displayed heuristic scores are not calibrated probabilities.
- Activity completion estimates require at least two dated, approved progress observations with a positive rate, or a separately recorded planner-observed outcome. The UI displays the observed rate, remaining percentage, record count, explanation, and baseline variance when available. Otherwise it says history is insufficient. There is no validated project-wide prediction model.
- Recovery estimates use the approved activity history and any recorded crew size when available. Crew gains use a stated linear scaling assumption; other gains need a planner-entered productivity-change or days-saved assumption. Additional equipment and material constraints are recorded but do not imply a productivity gain. These are not verified resource optimization and require human review. Parallel execution is only proposed against a direct Finish-to-Start link; safety/technical suitability is not inferred. No schedule link or actual date changes during simulation or approval.
- Project memory can answer cause/evidence/impact, area, milestone, since-yesterday, tomorrow-priority, saved recovery, measured-outcome, and category-level similar-history questions from the selected project's stored records. Responses show their source and record count, preserve unknown values, and require planner review. “Similar” is a recorded-category match rather than semantic similarity.
- Next-day proposals are deterministic rules over open risks, approved update constraints, and unfinished work on the longest-duration-path heuristic. They explain the priority basis, do not allocate resources, and are not a model-generated work schedule. Only planner-approved proposals become action records.

See [IMPLEMENTATION-GAP-MATRIX.md](IMPLEMENTATION-GAP-MATRIX.md) for a file-by-file assessment of reused, extended, and still-limited capabilities.

## Short judge demo script

1. Start SiteLink, create the first Admin, and open the synthetic project.
2. Open **Execution intelligence** and load its labeled synthetic story. Trace material evidence through the live predecessor graph to affected activities and the milestone.
3. Enter a recovery option with resource details and assumptions. Show remaining work, observed rate, any derived crew estimate, dependency exposure, and baseline variance; then approve it. Confirm schedule actuals and baseline dates remain unchanged.
4. Generate next-day recommendations, edit one, and approve it. Show that approval creates the action record.
5. Record an observed outcome with an observed date or measured days recovered. Show the before/after forecast, project thread, and audit record.
6. In a second browser profile, sign in as Supervisor and submit a separate dated update with evidence. Return as Planner, approve it, and show the existing schedule-actual and before/after audit workflow.
7. Optionally import and approve a synthetic schedule from **Plans & baselines**, or demonstrate CSV/XLSX/MSPDI exchange, OCR, and remote HTTPS deployment.

For a different-network demo, deploy the included Docker Compose/Caddy setup to a server with a domain and HTTPS as described above. A private `192.168.x.x` link is only reachable on that same LAN.

## Roles and project permissions

| Role | Default access in assigned projects |
|---|---|
| Admin | All projects and all workspace users. Admin is tenant-scoped: it does not cross into another workspace. |
| Project Manager | View, submit, review/approve reports, manage project risks/plans and baseline review. Schedule editing permission is not granted by default. |
| Planner | View, submit, review/approve reports, and edit schedules, project areas, plans and baselines. An Admin can narrow these grants per project. |
| Supervisor | View and submit updates, with review access. Approval is off by default; an Admin can grant it for a specific project. Schedule editing is not allowed. |
| Contractor | View and submit daily updates in assigned projects. Review and schedule editing are off by default. |
| Viewer | View-only. Other permissions cannot be granted to Viewer. |

Project membership is checked by the backend for state, reports, dashboard, audit, project details, analysis, memory, and writes. A user can only list projects they are allowed to view. If their access is removed while they are signed in, the next refresh clears the project data from the screen and shows an access message.

## Admin account and user management

- First local startup creates the first Admin through the setup page. There are no default/demo accounts.
- In a deployed Compose setup, the setup token protects first Admin creation. The token is only checked during initial setup and is not saved in the database.
- Admins can create teammate accounts directly with a temporary password and project assignment. Publicly registered users start as pending Viewers with no project membership. Admins can approve/reject them, change roles and project access, enable/disable, reset passwords, or delete accounts. Passwords must be 6–8 characters and are PBKDF2-HMAC-SHA256 hashes; API responses never return password values/hashes.
- Admins can edit names/usernames, change roles, enable/disable, reset passwords (revoking that user's sessions), delete users, edit projects, and replace project membership/permissions. These actions require an audit reason.
- Public signup is enabled by default. `/api/auth/signup` creates a pending account in the existing tenant selected by `SIGNUP_TENANT_ID`; it never creates a tenant or project and never grants a role or project membership from request data. There is no email verification, password reset email, or invitation mail system.

## API surface

Authentication: `/api/auth/login`, `/api/auth/logout`, `/api/auth/me`, `/api/auth/signup`, and `/api/auth/password`.
Admin approval: `/api/users/{id}/approve` and `/api/users/{id}/reject`.

Workspace administration: `/api/users`, `/api/users/{id}`, `/api/projects`, `/api/projects/{id}`, `/api/projects/{id}/members`.

Project data: `/api/state?projectId=…`, `/api/project-control?projectId=…`, `/api/reports?projectId=…`, `/api/dashboard?projectId=…`, `/api/audit?projectId=…`, `/api/analyze`, `/api/ai/status`, `/api/ai/ask`, `/api/ai/investigate`, `/api/ai/extract/field-report`, `/api/memory`, `/api/import/parse`, and `/api/export/msproject.xml`.

Project controls: `/api/plans`, `/api/plans/{id}/analyze`, `/api/plans/{id}/approve`, `/api/areas`, `/api/daily-updates`, `/api/daily-updates/analyze`, `/api/daily-updates/{id}/review`, `/api/risks`, `/api/milestones`, and `/api/documents`.

Execution intelligence: `GET /api/execution-intelligence?projectId=…`, `POST /api/demo/seed-execution-storyline`, `POST /api/recovery-scenarios`, `POST /api/recovery-scenarios/{id}/decision`, `POST /api/recommendations/generate`, `POST /api/recommendations/{id}/decision`, and `POST /api/execution-actions/{id}/outcome`. Writes require CSRF, a valid session, project access, and an approval grant. `GET /health` and the existing `GET /api/health` are lightweight health probes.

Legacy `/api/login`, `/api/logout`, `/api/session`, `/api/setup`, and the existing SiteLink routes remain for compatibility.

## Verification

Run these in the v4 folder using Python 3.10+:

```powershell
py -m py_compile server.py project_control.py execution_intelligence.py intelligence.py imports.py sitelink_ai.py evaluate_examples.py
py -m unittest discover -s tests -v
node --check app.js
node --check project-control-ui.js
node --check execution-intelligence-ui.js
node --check sitelink-ai-ui.js
py evaluate_examples.py
```

The SQLite suite passed **64 tests**, including all four CPM relationship types, lag, forward/backward passes, total/free float, cycle and missing-duration handling, milestone impacts, project-scoped assistant answers, Ask SiteLink activity/area scope and citation validation, provider fallback, audit privacy, role/project boundaries, imports, the recovery approval and measured-outcome loop, lesson-backed execution memory, schedule/baseline immutability, audit/notification records, and UI navigation/detail-view contracts. Python compilation and JavaScript syntax checks passed. The synthetic capture fixture reports **31/31 exact expected-field checks** and **5/5 top-1 and top-3 matches** across five examples; this small synthetic result does not establish production accuracy or calibrate displayed scores. Browser smoke checks exercised authenticated SiteLink AI, recovery approval, measured outcome/lesson memory, project Home, and a 390 px mobile viewport; one uninterrupted end-to-end judge journey was not completed. A live model request, OCR against an installed Tesseract/Poppler toolchain, PostgreSQL server, Docker runtime, and public HTTPS deployment were not verified in this environment. The PostgreSQL adapter, Docker image, and HTTPS deployment still need staging verification.

## Remaining limitations

- The HTTP application server has no built-in TLS. Public use must go through the included Caddy HTTPS reverse proxy or an equivalent trusted proxy.
- No email verification, invitation email, self-service password reset, SSO/MFA, account recovery, or production security assessment is included.
- The requested 6–8 character password limit is weak for public production accounts. Keep HTTPS enabled, restrict signups through administrator approval, and consider a stronger password policy before using real project data.
- Existing v3 SQLite data migrates automatically only when its SQLite file is placed in v4's `data/` folder. Moving it to a PostgreSQL database requires a separate data migration.
- PostgreSQL/Compose/Caddy setup is supplied but untested in this environment. Test the entire deployment and restore procedure with synthetic data before relying on it.
- Execution forecasts, cause categories, and recovery comparisons are deterministic prototype calculations and have not been calibrated on real-project records. CPM requires complete usable durations and dependencies; it uses calendar days and does not model project calendars or resources. There is no resource leveling/optimization, autonomous machine learning, or live Primavera/MS Project synchronization. Crew uplift assumes a stated linear productivity relationship; equipment/material gains require explicit planner assumptions. Parallel proposals only validate saved direct Finish-to-Start links and graph cycles, not technical/safety constraints.
- Do not enter confidential or live project data until deployment, backups, access controls, and security have been reviewed by the project owner.
