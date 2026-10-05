# SiteLink v4 verification report

## Repository audit

This release extends the supplied SiteLink v4. It preserves authentication, signup and admin approval, roles, tenant/project permissions, schedule review and baselines, report capture and approval, CSV/XLSX/MS Project XML exchange, PDF/image OCR flow, audit, notifications, and the existing synthetic demo. Changes extend the existing Python API/services and browser screens.

## Change summary

- `index.html`, `sitelink-home-ui.js`, `styles.css`, and `app.js`: added **AI Agent** as a standalone primary-navigation link outside all collapsible groups, with a distinct sparkle icon and compact AI badge while preserving the common row and active-page styling. Its existing route and permission-checked save behavior remain in place. **Capture & Review** keeps Today & work log and Report inbox; Ask & investigate, execution memory, and evidence insights remain in the separate **SiteLink AI** group. Project Home uses authenticated project-control and execution-intelligence responses, including the record-linked execution thread and retry/error states.
- `execution-intelligence-ui.js`, `server.py`, and `intelligence.py`: require a planner-written lesson when saving a measured recovery outcome and surface that lesson in historical outcome answers. An approved scenario remains separate from an outcome and does not change schedule actuals.
- `execution_intelligence.py`: added calendar-day CPM forward/backward passes, FS/SS/FF/SF relationships and lag, ES/EF/LS/LF, total/free float, critical activities/paths, and CPM-linked milestone dates. Missing durations, cycles, unknown predecessors, and invalid relationship types return a visible non-calculated state. Added explicit cause-certainty labels.
- `server.py` and `intelligence.py`: the authenticated, project-authorized memory API supplies current selected-project activities, approved updates, risks, milestones, scenarios, recommendations, actions, and outcomes. The assistant can answer current cause/evidence/impact, milestone, recent-change, planning, recovery, outcome, and category-level history questions without reading another project. Missing facts remain unknown.
- `sitelink_ai.py`, `server.py`, `sitelink-ai-ui.js`, `app.js`, and `index.html`: added the read-only Ask SiteLink screen and `/api/ai/ask`, `/api/ai/investigate`, `/api/ai/status`, and field-report extraction alias. Context is limited to the authorized project and may be narrowed to an activity or an area and its child areas. Optional server-side structured model explanations display only after exact approved-record ID and quote validation; otherwise the existing deterministic calculations answer. The new AI audit entry stores intent/provider/latency/scope/evidence metadata without the raw question.
- `execution-intelligence-ui.js` and `app.js`: show CPM dates/float/path and its input status, separate CPM milestone calculations from history-based forecasts, display cause certainty, and identify the source and review requirement on assistant answers.
- `tests/`: added CPM relationship, float, missing-duration, cycle, milestone, and project-assistant coverage. `README.md` and `IMPLEMENTATION-GAP-MATRIX.md` describe supported inputs and limitations.

## Verification status

| Area | Status | Evidence / boundary |
|---|---|---|
| Python test suite | **VERIFIED** | 64/64 Python tests pass, including auth/project boundaries, imports, reports, approvals, audit, outcomes and lessons, multi-device workflow, Home/navigation contracts, and the Ask SiteLink flow. |
| AI Agent visibility and sidebar styling | **VERIFIED** | AI Agent is a standalone primary-navigation button outside every collapsible group. Standard rows share one base style; the agent uses a sparkle icon and AI badge. UI contract verifies placement and route. |
| CPM and milestone calculations | **VERIFIED** | Tests cover forward/backward pass, total/free float, critical path, FS/SS/FF/SF with lag, incomplete durations, cycles, and linked milestone date impact. Calendar days only. |
| Project assistant | **VERIFIED** | Unit tests cover source/evidence/impact answers and insufficient-data responses; existing API integration verifies outcome history after a recorded action. Tenant/project permission checks remain in the authenticated API path. |
| Ask SiteLink assistant | **VERIFIED** | Unit/API tests cover project authorization, selected activity and parent/child area scoping, approved-only sources, exact citation checks and deterministic fallback, prompt injection boundary language, read-only response, and audit omission of raw question text. The live external provider call is NOT VERIFIED. |
| Schedule actual approval gate | **VERIFIED** | Existing integration tests confirm only approved daily updates write actuals; recovery simulation and action approval do not. |
| Synthetic extraction/matching benchmark | **VERIFIED** | Existing five synthetic examples produced 31/31 expected field checks and 5/5 top-1 and 5/5 top-3 matches. This small fixture is not a general accuracy or calibration claim. |
| Python/JavaScript syntax | **VERIFIED** | Python compilation and Node syntax checks pass for changed modules. |
| Docker Compose, PostgreSQL, Caddy/TLS | **NOT VERIFIED** | This workspace does not provide a running deployment target; staging connectivity, HTTPS, backup, and restore were not demonstrated. |
| Actual external LLM request | **NOT VERIFIED** | The optional environment-configured provider and validated fallback remain; no live credentialed request was made. No API key is stored in source. |
| OCR binaries and real scanned file | **NOT VERIFIED** | Existing OCR/transcription review flow is preserved; installed Tesseract/Poppler and a real OCR run were not validated here. |
| Authenticated browser workflow | **PARTIAL** | Chrome check completed first-admin setup/login, confirmed AI Agent stays visible with Capture & Review collapsed, checked the common row style and AI badge, navigated to Risks and confirmed the active highlight follows the current page, opened the existing Time Agent screen, and reported no page errors. Prior smoke checks exercised recovery approval/outcome memory and mobile layout. The full capture-to-planner-approval golden journey was not completed in one uninterrupted run. |
| Resource assignments/conflict detection | **NOT IMPLEMENTED** | Approved daily reports retain manpower/equipment observations and recovery forms accept stated resource assumptions, but the schedule has no explicit resource assignments, availability/capacity model, or overlap detector. No resource conflict result or predicted slip is claimed. |
| Resource leveling/optimization | **NOT IMPLEMENTED** | The schedule model lacks verified demand, availability calendars, skills, and work rules; no optimizer or automatic allocation is claimed. Recovery estimates remain stated assumptions. |
| GIS / spatial intelligence | **PARTIAL** | Existing project areas/sub-areas connect activities, reports, risks, and execution summaries. No coordinate/geometry model or map-based interaction is implemented because no synthetic geometry is supplied. |
| Vector retrieval / RAG | **NOT IMPLEMENTED** | Project memory uses structured, authorized project-scoped retrieval with evidence IDs. A vector database is not included; current synthetic data does not justify it. |
| Evidence-grounded causal intelligence | **PARTIAL** | Deterministic evidence-aware cause labels distinguish Confirmed/Inferred/Unknown; this is not a trained or validated causal model. |
| Kubernetes | **NOT IMPLEMENTED** | No Kubernetes manifests were added; Docker + PostgreSQL + Caddy remains the supplied deployment approach and is not verified here. |
| Calibrated causal AI / forecast accuracy | **NOT IMPLEMENTED** | Cause classification and execution rates remain deterministic heuristics; no real-project labeled validation or calibration data is included. |
| GIS and live PMIS synchronization | **NOT IMPLEMENTED** | No coordinates or live Primavera/MS Project connection are supplied. The included exchange path is a file adapter. |

## Limits

CPM operates in calendar days, treats planned starts as earliest-start constraints, uses saved planned finishes as a project deadline when present, and requires a usable duration for every activity. It does not model working calendars, resource constraints, or live PMIS updates. Critical-path calculations are schedule arithmetic, not execution predictions. Cause categories are not verified root causes unless explicitly marked Confirmed with a stored evidence reference; model scores remain uncalibrated. Keep using synthetic data for judging and stage the deployment before external use.

## Judge walkthrough

1. Start the app and open the synthetic project.
2. In **Execution intelligence**, load the synthetic storyline and inspect the dependency graph, CPM float/path, linked milestone date, source cause, and evidence.
3. Under **Capture & Review**, open **AI Time Agent** and enter a synthetic site update with its event date; review extracted facts, evidence, ranked activity candidates, then save it for the planner.
4. Open **Report inbox** in the same group and review the field report. Verify source text, schedule match, evidence, and approval audit; approve only after confirming its activity and actual values.
5. Run a recovery scenario and inspect its assumptions. Approve a recommendation as an action; verify schedule actuals remain unchanged.
6. Record a measured outcome and lesson, then ask project memory what happened. Review the source count, outcome lesson, and linked audit records.

## Architecture

```text
Browser UI → authenticated same-origin API → tenant/project authorization
           → SQLite demo / PostgreSQL adapter
           → schedules, approved field evidence, risks, decisions, outcomes
           → CPM + deterministic intelligence → read-only Ask SiteLink and planner review/audit
                                   ↘ optional server-side structured provider
                                     exact quote + record-ID citation validation
```
