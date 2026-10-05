"""Transparent, deterministic project execution intelligence for SiteLink.

Every input is a project record. Forecasts and simulations are estimates and never
write schedule actuals; only the existing planner approval workflow may do that.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, datetime
import math
import re
from typing import Any


CAUSE_TERMS = {
    "Material": ("material", "delivery", "delivered", "procurement", "stock", "valve", "spool", "cement", "cable"),
    "Manpower": ("manpower", "crew", "worker", "staff", "labor", "labour", "operator"),
    "Equipment": ("equipment", "crane", "pump", "tool", "machine", "breakdown"),
    "Dependency": ("predecessor", "dependency", "dependent", "sequence", "awaiting", "upstream"),
    "Approval": ("approval", "permit", "sign-off", "signoff", "authorization", "authorisation"),
    "Access": ("access", "road", "lane", "scaffold", "entry", "restricted"),
    "Contractor": ("contractor", "subcontractor", "vendor", "supplier"),
    "Quality": ("quality", "rework", "inspection", "defect", "test failed"),
    "Safety": ("safety", "incident", "unsafe", "safety stop", "confined-space"),
    "Weather": ("weather", "rain", "wind", "storm", "temperature"),
}


def _ids(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    elif isinstance(value, str):
        values = re.split(r"[,;\n]+", value)
    else:
        values = []
    return list(dict.fromkeys(str(v).strip() for v in values if str(v).strip()))


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return None


def _relationship(value: Any) -> str | None:
    raw = re.sub(r"[^A-Z]", "", str(value or "FS").upper())
    aliases = {
        "FS": "FS", "FINISHTOSTART": "FS", "STARTTOFINISH": "SF",
        "SF": "SF", "SS": "SS", "STARTTOSTART": "SS",
        "FF": "FF", "FINISHTOFINISH": "FF",
    }
    return aliases.get(raw)


def _calculate_cpm(by_id: dict[str, dict], edges: list[dict], topo: list[str], issues: list[dict]) -> dict:
    """Calendar-day CPM with inclusive planned dates and exclusive internal finishes."""
    durations: dict[str, int] = {}
    missing: list[str] = []
    starts: dict[str, date | None] = {}
    finishes: dict[str, date | None] = {}
    for aid, activity in by_id.items():
        starts[aid], finishes[aid] = _day(activity.get("plannedStart")), _day(activity.get("plannedFinish"))
        raw_duration = activity.get("durationDays")
        try:
            if raw_duration not in (None, "") and (float(raw_duration) > 0 or activity.get("isMilestone")):
                durations[aid] = max(0, min(36500, int(float(raw_duration))))
            elif activity.get("isMilestone"):
                durations[aid] = 0
            elif starts[aid] and finishes[aid] and finishes[aid] >= starts[aid]:
                durations[aid] = (finishes[aid] - starts[aid]).days + 1
            else:
                missing.append(aid)
        except (TypeError, ValueError):
            missing.append(aid)
    date_points = [d for d in starts.values() if d] + [d for d in finishes.values() if d]
    origin = min(date_points) if date_points else None
    base = {aid: ((starts[aid] - origin).days if starts[aid] and origin else
                  (finishes[aid] - origin).days - durations[aid] + 1 if finishes[aid] and origin and aid in durations else 0)
            for aid in by_id}
    blocker_types = {"cycle", "unknown_predecessor", "invalid_dependency_type"}
    blocking_issues = [issue for issue in issues if issue.get("type") in blocker_types]
    if blocking_issues:
        return {"status": "invalid", "basis": "CPM unavailable until schedule dependency issues are corrected.",
                "missingDurationActivityIds": missing, "criticalActivities": [], "criticalPaths": [], "projectDurationDays": None}
    if missing:
        return {"status": "insufficient_data", "basis": "CPM requires a recorded duration or planned start and finish for every activity.",
                "missingDurationActivityIds": sorted(missing), "criticalActivities": [], "criticalPaths": [], "projectDurationDays": None}
    if not by_id:
        return {"status": "insufficient_data", "basis": "No schedule activities are available for CPM.",
                "missingDurationActivityIds": [], "criticalActivities": [], "criticalPaths": [], "projectDurationDays": None}

    durations = {aid: durations[aid] for aid in by_id}
    outgoing: dict[str, list[dict]] = defaultdict(list)
    incoming: dict[str, list[dict]] = defaultdict(list)
    for edge in edges:
        outgoing[edge["from"]].append(edge)
        incoming[edge["to"]].append(edge)
    early_start: dict[str, int] = {}
    early_finish: dict[str, int] = {}
    for aid in topo:
        es = base[aid]
        for edge in incoming.get(aid, []):
            pred, rel, lag = edge["from"], _relationship(edge["type"]), edge["lagDays"]
            if rel == "FS": candidate = early_finish[pred] + lag
            elif rel == "SS": candidate = early_start[pred] + lag
            elif rel == "FF": candidate = early_finish[pred] + lag - durations[aid]
            else: candidate = early_start[pred] + lag - durations[aid]  # SF
            es = max(es, candidate)
        early_start[aid] = es
        early_finish[aid] = es + durations[aid]

    planned_deadlines = [(finishes[aid] - origin).days + 1 for aid in by_id if finishes[aid] and origin]
    project_finish = max([*early_finish.values(), *planned_deadlines], default=0)
    late_start: dict[str, int] = {}
    late_finish: dict[str, int] = {}
    for aid in reversed(topo):
        ls_limit = project_finish - durations[aid]
        for edge in outgoing.get(aid, []):
            succ, rel, lag = edge["to"], _relationship(edge["type"]), edge["lagDays"]
            if rel == "FS": candidate = late_start[succ] - lag - durations[aid]
            elif rel == "SS": candidate = late_start[succ] - lag
            elif rel == "FF": candidate = late_finish[succ] - lag - durations[aid]
            else: candidate = late_finish[succ] - lag  # SF
            ls_limit = min(ls_limit, candidate)
        late_start[aid] = ls_limit
        late_finish[aid] = ls_limit + durations[aid]
    total_float = {aid: late_start[aid] - early_start[aid] for aid in by_id}
    free_float = {}
    for aid in by_id:
        slacks = []
        for edge in outgoing.get(aid, []):
            succ, rel, lag = edge["to"], _relationship(edge["type"]), edge["lagDays"]
            if rel == "FS": slack = early_start[succ] - early_finish[aid] - lag
            elif rel == "SS": slack = early_start[succ] - early_start[aid] - lag
            elif rel == "FF": slack = early_finish[succ] - early_finish[aid] - lag
            else: slack = early_finish[succ] - early_start[aid] - lag
            slacks.append(slack)
        free_float[aid] = min(slacks) if slacks else project_finish - early_finish[aid]

    critical = {aid for aid in by_id if total_float[aid] <= 0}
    tight_edges = []
    for edge in edges:
        pred, succ, rel, lag = edge["from"], edge["to"], _relationship(edge["type"]), edge["lagDays"]
        if rel == "FS": slack = early_start[succ] - early_finish[pred] - lag
        elif rel == "SS": slack = early_start[succ] - early_start[pred] - lag
        elif rel == "FF": slack = early_finish[succ] - early_finish[pred] - lag
        else: slack = early_finish[succ] - early_start[pred] - lag
        if pred in critical and succ in critical and slack == 0:
            tight_edges.append(edge)
    tight_next: dict[str, list[str]] = defaultdict(list)
    tight_prev: dict[str, list[str]] = defaultdict(list)
    for edge in tight_edges:
        tight_next[edge["from"]].append(edge["to"])
        tight_prev[edge["to"]].append(edge["from"])
    path_starts = sorted(aid for aid in critical if not tight_prev[aid])
    paths: list[list[str]] = []
    def walk(aid: str, path: list[str]) -> None:
        if len(paths) >= 20: return
        choices = sorted(n for n in tight_next[aid] if n not in path)
        if not choices:
            paths.append(path)
            return
        for child in choices: walk(child, path + [child])
    for aid in path_starts:
        walk(aid, [aid])
    if not paths:
        paths = [[aid] for aid in sorted(critical)]
    def day_at(offset: int, duration: int, finish: bool = False) -> str | None:
        if not origin: return None
        day_offset = offset + duration - 1 if finish and duration else offset
        return date.fromordinal(origin.toordinal() + day_offset).isoformat()
    node_values = {}
    for aid in by_id:
        d = durations[aid]
        node_values[aid] = {"durationDays": d, "earlyStartOffset": early_start[aid], "earlyFinishOffset": early_finish[aid],
                            "lateStartOffset": late_start[aid], "lateFinishOffset": late_finish[aid],
                            "earlyStart": day_at(early_start[aid], d), "earlyFinish": day_at(early_start[aid], d, True),
                            "lateStart": day_at(late_start[aid], d), "lateFinish": day_at(late_start[aid], d, True),
                            "totalFloatDays": total_float[aid], "freeFloatDays": free_float[aid], "isCritical": aid in critical}
    return {"status": "calculated", "basis": "Calendar-day CPM from saved durations, planned-date release constraints, dependency types, and lag. No resource leveling.",
            "calendar": "calendar_days", "projectStartDate": origin.isoformat() if origin else None,
            "projectFinishDate": (date.fromordinal(origin.toordinal() + project_finish - 1).isoformat() if origin and project_finish else None),
            "projectDurationDays": project_finish, "criticalActivities": sorted(critical), "criticalPaths": paths,
            "nodes": node_values, "missingDurationActivityIds": []}


def build_dependency_graph(activities: list[dict]) -> dict:
    """Build the live predecessor graph and calculate CPM when inputs suffice."""
    by_id = {str(a.get("id") or a.get("activity_id") or ""): a for a in activities}
    by_id.pop("", None)
    predecessors: dict[str, list[str]] = {}
    children: dict[str, list[str]] = defaultdict(list)
    issues: list[dict] = []
    edges: list[dict] = []
    for aid, activity in by_id.items():
        raw_pred = activity.get("predecessors", activity.get("predecessorIds", []))
        if isinstance(raw_pred, list) and any(isinstance(v, dict) for v in raw_pred):
            pred_rows = [v for v in raw_pred if isinstance(v, dict)]
        else:
            pred_rows = [{"id": value} for value in _ids(raw_pred)]
        predecessors[aid] = []
        for relation in pred_rows:
            source = str(relation.get("id") or relation.get("activityId") or relation.get("predecessorId") or "").strip()
            if not source:
                continue
            if source not in by_id:
                issues.append({"type": "unknown_predecessor", "activityId": aid, "reference": source})
                continue
            predecessors[aid].append(source)
            children[source].append(aid)
            raw_type = relation.get("type") or relation.get("dependencyType") or activity.get("dependencyType") or "FS"
            relation_code = _relationship(raw_type)
            if relation_code is None:
                issues.append({"type": "invalid_dependency_type", "activityId": aid, "reference": source, "value": str(raw_type)})
                relation_code = "FS"
            relation_label = {"FS": "Finish-to-Start", "SS": "Start-to-Start", "FF": "Finish-to-Finish", "SF": "Start-to-Finish"}[relation_code]
            try:
                lag = int(relation.get("lagDays") or 0)
            except (TypeError, ValueError):
                lag = 0
            edges.append({"from": source, "to": aid, "type": relation_label, "lagDays": max(-365, min(365, lag))})

    indegree = {aid: len(predecessors[aid]) for aid in by_id}
    queue = deque(sorted(aid for aid, degree in indegree.items() if degree == 0))
    topo: list[str] = []
    while queue:
        aid = queue.popleft()
        topo.append(aid)
        for child in sorted(children.get(aid, [])):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    cyclic = sorted(aid for aid, degree in indegree.items() if degree > 0)
    if cyclic:
        issues.append({"type": "cycle", "activityIds": cyclic})

    cpm = _calculate_cpm(by_id, edges, topo, issues)
    legacy_chain: list[str] = []
    if cpm.get("status") == "calculated":
        legacy_chain = cpm.get("criticalPaths", [[]])[0] if cpm.get("criticalPaths") else []
    else:
        heuristic = {}
        for aid, activity in by_id.items():
            start, finish = _day(activity.get("plannedStart")), _day(activity.get("plannedFinish"))
            try:
                known = int(float(activity.get("durationDays"))) if activity.get("durationDays") not in (None, "", 0, "0") else 0
            except (TypeError, ValueError):
                known = 0
            heuristic[aid] = max(1, known or ((finish - start).days + 1 if start and finish and finish >= start else 1))
        if not cyclic:
            for aid in reversed(topo):
                heuristic[aid] += max((heuristic.get(child, 0) for child in children.get(aid, [])), default=0)
        if heuristic and not cyclic:
            cursor = max(heuristic, key=heuristic.get)
            seen = set()
            while cursor and cursor not in seen:
                seen.add(cursor)
                legacy_chain.append(cursor)
                choices = [child for child in children.get(cursor, []) if child in heuristic]
                cursor = max(choices, key=lambda child: heuristic[child]) if choices else ""

    nodes = []
    for aid, activity in by_id.items():
        cpm_node = cpm.get("nodes", {}).get(aid, {})
        nodes.append({
            "id": aid, "name": str(activity.get("name") or aid),
            "area": str(activity.get("area") or activity.get("location") or "Area not recorded"),
            "areaId": str(activity.get("areaId") or ""), "subArea": str(activity.get("subArea") or "Sub-area not recorded"),
            "subAreaId": str(activity.get("subAreaId") or ""),
            "predecessors": predecessors[aid], "successors": sorted(children.get(aid, [])),
            "plannedStart": activity.get("plannedStart", ""), "plannedFinish": activity.get("plannedFinish", ""),
            "progress": activity.get("progress", 0), "durationDays": cpm_node.get("durationDays"),
            "onCriticalChain": aid in (cpm.get("criticalActivities", []) if cpm.get("status") == "calculated" else legacy_chain),
            "onCpmCritical": aid in cpm.get("criticalActivities", []), "cpm": cpm_node,
        })
    return {"nodes": nodes, "edges": edges, "issues": issues, "valid": not issues,
            "cpm": cpm, "criticalChain": legacy_chain,
            "criticalChainBasis": "Calculated CPM critical path" if cpm.get("status") == "calculated" else "Longest-duration dependency-path heuristic; not CPM"}


def classify_cause(text: str, declared: str = "") -> tuple[str, str]:
    if declared in {*CAUSE_TERMS, "Unknown"}:
        return declared, "Category recorded on source record"
    low = text.casefold()
    contains = lambda term: bool(re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", low))
    ranked = [(sum(contains(term) for term in terms), category) for category, terms in CAUSE_TERMS.items()]
    score, category = max(ranked, default=(0, ""))
    if score:
        return category, "Keyword terminology mapping from source text"
    return "Unknown", "No supported cause category found in source text"


def _source_evidence(record: dict) -> list[str]:
    values = record.get("attachments") or record.get("evidenceIds") or []
    if not isinstance(values, list):
        return []
    return [str(v) for v in values if v]


def _cause_certainty(record: dict, category: str, evidence: list[str]) -> str:
    stated = str(record.get("causeCertainty") or record.get("rootCauseStatus") or "").strip().title()
    if stated == "Confirmed":
        return "Confirmed" if evidence else "Inferred"
    if stated in {"Inferred", "Unknown"}:
        return stated
    return "Inferred" if category != "Unknown" else "Unknown"


def analyze_execution(activities: list[dict], updates: list[dict], risks: list[dict], milestones: list[dict], today: str,
                      outcomes: list[dict] | None = None) -> dict:
    approved = [u for u in updates if u.get("status") == "Approved"]
    graph = build_dependency_graph(activities)
    activity_by_id = {str(a.get("id") or a.get("activity_id") or ""): a for a in activities}
    down: dict[str, list[str]] = defaultdict(list)
    for edge in graph["edges"]:
        down[edge["from"]].append(edge["to"])
    causes = []
    for risk in risks:
        if risk.get("status") not in {"Open", "Monitoring"}:
            continue
        aid = str(risk.get("activityId") or "")
        if not aid or aid not in activity_by_id:
            continue
        payload = risk.get("payload") if isinstance(risk.get("payload"), dict) else risk
        description = " ".join(str(payload.get(k) or "") for k in ("description", "mitigation", "type", "category", "rootCauseCategory"))
        category, basis = classify_cause(str(risk.get("title", "")) + " " + description, str(payload.get("rootCauseCategory") or payload.get("category") or ""))
        source_activity = activity_by_id[aid]
        evidence_ids = _source_evidence(payload)
        causes.append({"id": risk.get("id"), "recordType": "risk", "activityId": aid,
                       "activityName": activity_by_id[aid].get("name", aid), "title": risk.get("title", "Open risk"),
                       "area": source_activity.get("area") or source_activity.get("location") or "Area not recorded",
                       "areaId": source_activity.get("areaId") or "", "subArea": source_activity.get("subArea") or "Sub-area not recorded",
                       "category": category, "classificationBasis": basis,
                       "causeCertainty": _cause_certainty(payload, category, evidence_ids), "severity": risk.get("severity", "Medium"),
                       "status": risk.get("status"), "sourceRecords": [str(risk.get("id"))],
                       "evidenceIds": evidence_ids, "estimatedDelayDays": payload.get("estimatedDelayDays"),
                       "sourceText": description[:600]})
    for update in approved:
        text = " ".join(str(update.get(k) or "") for k in ("issues", "delayReason", "blocker", "notes", "workCompleted"))
        if not text.strip():
            continue
        explicit = str(update.get("rootCauseCategory") or update.get("causeCategory") or "")
        category, basis = classify_cause(text, explicit)
        # Only describe an update as a cause when its wording indicates a constraint.
        if category == "Unknown" and not re.search(r"block|delay|short|pending|await|unable|hold|issue", text, re.I):
            continue
        aid = str(update.get("activityId") or "")
        if aid not in activity_by_id:
            continue
        source_activity = activity_by_id[aid]
        evidence_ids = _source_evidence(update)
        causes.append({"id": update.get("id"), "recordType": "approved_update", "activityId": aid,
                       "activityName": activity_by_id[aid].get("name", aid), "title": (text[:150] or "Approved execution constraint"),
                       "area": source_activity.get("area") or source_activity.get("location") or "Area not recorded",
                       "areaId": update.get("areaId") or source_activity.get("areaId") or "",
                       "subArea": source_activity.get("subArea") or "Sub-area not recorded",
                       "category": category, "classificationBasis": basis,
                       "causeCertainty": _cause_certainty(update, category, evidence_ids), "severity": "Medium",
                       "status": "Approved source observation", "sourceRecords": [str(update.get("id"))],
                       "evidenceIds": evidence_ids, "estimatedDelayDays": update.get("estimatedDelayDays"),
                       "sourceText": text[:600]})

    impacts = []
    for cause in causes:
        root = cause["activityId"]
        visited, paths, queue = set(), {}, deque((aid, [root, aid]) for aid in down.get(root, []))
        while queue:
            affected, path = queue.popleft()
            if affected in visited:
                continue
            visited.add(affected)
            paths[affected] = path
            queue.extend((child, path + [child]) for child in down.get(affected, []))
        activity = activity_by_id[root]
        finish = _day(activity.get("plannedFinish"))
        as_of = _day(today)
        overdue_days = max(0, (as_of - finish).days) if finish and as_of and float(activity.get("progress") or 0) < 100 else 0
        explicit = cause.get("estimatedDelayDays")
        try:
            estimate = max(0, min(365, int(float(explicit)))) if explicit not in (None, "") else None
        except (ValueError, TypeError):
            estimate = None
        basis = "Explicit delay estimate on source record" if estimate is not None else ""
        if estimate is None and overdue_days:
            estimate, basis = overdue_days, "Elapsed days past planned finish; retrospective indicator, not a forecast"
        for aid in sorted(visited):
            impacted_milestones = []
            for milestone in milestones:
                linked = [str(milestone.get("activityId") or "")] + _ids(milestone.get("dependencies", []))
                if aid in linked or any(linked_id in visited for linked_id in linked if linked_id):
                    impacted_milestones.append(str(milestone.get("id") or milestone.get("name") or "Milestone"))
            path = paths.get(aid, [root, aid])
            incoming = next((edge for edge in graph["edges"] if edge["from"] == path[-2] and edge["to"] == aid), {})
            impacts.append({"causeId": cause["id"], "rootActivityId": root, "activityId": aid,
                            "activityName": activity_by_id[aid].get("name", aid),
                            "area": activity_by_id[aid].get("area") or activity_by_id[aid].get("location") or "Area not recorded",
                            "areaId": activity_by_id[aid].get("areaId") or "",
                            "subArea": activity_by_id[aid].get("subArea") or "Sub-area not recorded",
                            "subAreaId": activity_by_id[aid].get("subAreaId") or "",
                            "dependencyType": incoming.get("type", "Finish-to-Start"), "dependencyPath": path,
                            "affectedMilestones": impacted_milestones,
                            "severity": cause["severity"], "category": cause["category"],
                            "estimatedDelayDays": estimate, "estimateBasis": basis or "Duration impact is unknown from stored records",
                            "confidence": 0.58 if explicit not in (None, "") else (0.32 if overdue_days else 0.18),
                            "sourceRecords": cause["sourceRecords"], "evidenceIds": cause["evidenceIds"],
                            "impactStatus": "Estimated" if estimate is not None else "Exposure identified; duration unknown"})

    by_activity: dict[str, list[dict]] = defaultdict(list)
    for update in approved:
        if update.get("progress") is not None:
            by_activity[str(update.get("activityId") or "")].append(update)
    forecasts = []
    for aid, activity in activity_by_id.items():
        progress = float(activity.get("progress") or 0)
        history = sorted(by_activity.get(aid, []), key=lambda u: (str(u.get("workDate", "")), str(u.get("submittedAt", ""))))
        resource_observations = [u for u in history if u.get("manpower") not in (None, "") or u.get("equipmentUsed") or u.get("materialAvailability")]
        latest_resources = resource_observations[-1] if resource_observations else {}
        try:
            current_manpower = float(latest_resources.get("manpower")) if latest_resources.get("manpower") not in (None, "") else None
        except (TypeError, ValueError):
            current_manpower = None
        distinct = {}
        for item in history:
            day = _day(item.get("workDate"))
            if day:
                distinct[day] = float(item.get("progress") or 0)
        planned_finish = str(activity.get("plannedFinish") or "")
        if activity.get("actualFinish"):
            forecasts.append({"activityId": aid, "activityName": activity.get("name", aid), "status": "Actual completion",
                              "estimatedFinish": activity.get("actualFinish"), "plannedFinish": planned_finish,
                              "varianceDays": ((_day(activity.get("actualFinish")) - _day(planned_finish)).days if _day(activity.get("actualFinish")) and _day(planned_finish) else None),
                              "explanation": "Planner-approved actual finish recorded on the activity.",
                              "basisRecords": len(history), "confidence": "Observed", "sourceRecords": [str(u.get("id")) for u in history],
                              "currentManpower": current_manpower, "currentManpowerSource": str(latest_resources.get("id") or "") or None,
                              "equipmentUsed": str(latest_resources.get("equipmentUsed") or "") or None,
                              "materialAvailability": str(latest_resources.get("materialAvailability") or "Unknown")})
        elif progress >= 100:
            forecasts.append({"activityId": aid, "activityName": activity.get("name", aid), "status": "Progress reported complete; planner actual finish needed",
                              "estimatedFinish": None, "plannedFinish": planned_finish, "varianceDays": None,
                              "explanation": "Progress is reported as complete, but no planner-approved actual finish date is recorded.",
                              "basisRecords": len(history), "confidence": "Insufficient", "sourceRecords": [str(u.get("id")) for u in history],
                              "currentManpower": current_manpower, "currentManpowerSource": str(latest_resources.get("id") or "") or None,
                              "equipmentUsed": str(latest_resources.get("equipmentUsed") or "") or None,
                              "materialAvailability": str(latest_resources.get("materialAvailability") or "Unknown")})
        elif len(distinct) >= 2:
            first, last = min(distinct), max(distinct)
            rate = (distinct[last] - distinct[first]) / max(1, (last - first).days)
            estimate = None
            if rate > 0:
                estimate = date.fromordinal(_day(today).toordinal() + math.ceil((100 - progress) / rate)).isoformat() if _day(today) else None
            remaining = max(0.0, 100.0 - progress)
            explanation = (f"Observed progress changed {distinct[last] - distinct[first]:.1f} percentage points over {(last - first).days} calendar days ({rate:.2f} points/day); "
                           f"{remaining:.1f}% remains. Estimate assumes that historical rate continues.")
            forecasts.append({"activityId": aid, "activityName": activity.get("name", aid),
                              "status": "Estimate based on approved activity history" if estimate else "No positive observed rate",
                              "estimatedFinish": estimate, "plannedFinish": planned_finish, "dailyRate": round(rate, 3),
                              "remainingProgressPct": round(remaining, 2),
                              "varianceDays": ((_day(estimate) - _day(planned_finish)).days if _day(estimate) and _day(planned_finish) else None),
                              "explanation": explanation if estimate else explanation + " No finish date is estimated without a positive rate.",
                              "basisRecords": len(history), "confidence": "Low" if len(history) < 3 else "Medium",
                              "sourceRecords": [str(u.get("id")) for u in history],
                              "currentManpower": current_manpower, "currentManpowerSource": str(latest_resources.get("id") or "") or None,
                              "equipmentUsed": str(latest_resources.get("equipmentUsed") or "") or None,
                              "materialAvailability": str(latest_resources.get("materialAvailability") or "Unknown")})
        else:
            forecasts.append({"activityId": aid, "activityName": activity.get("name", aid), "status": "Insufficient approved activity history",
                              "estimatedFinish": None, "plannedFinish": planned_finish, "varianceDays": None,
                              "explanation": "At least two distinct dated, planner-approved progress observations are required to estimate a completion rate.",
                              "basisRecords": len(history), "confidence": "Insufficient", "sourceRecords": [str(u.get("id")) for u in history],
                              "currentManpower": current_manpower, "currentManpowerSource": str(latest_resources.get("id") or "") or None,
                              "equipmentUsed": str(latest_resources.get("equipmentUsed") or "") or None,
                              "materialAvailability": str(latest_resources.get("materialAvailability") or "Unknown")})

    milestone_forecasts = []
    for milestone in milestones:
        linked = [str(milestone.get("activityId") or "")] + _ids(milestone.get("dependencies", []))
        linked = [aid for aid in dict.fromkeys(linked) if aid in activity_by_id]
        related = [f for f in forecasts if f["activityId"] in linked]
        dates = [f["estimatedFinish"] for f in related if f.get("estimatedFinish")]
        estimate = max(dates) if dates else None
        planned = str(milestone.get("plannedDate") or "")
        variance = None
        if estimate and _day(estimate) and _day(planned):
            variance = (_day(estimate) - _day(planned)).days
        cpm_nodes = (graph.get("cpm") or {}).get("nodes", {})
        cpm_dates = [cpm_nodes[aid].get("earlyFinish") for aid in linked if cpm_nodes.get(aid, {}).get("earlyFinish")]
        cpm_estimate = max(cpm_dates) if cpm_dates else None
        cpm_variance = ((_day(cpm_estimate) - _day(planned)).days if cpm_estimate and _day(cpm_estimate) and _day(planned) else None)
        milestone_forecasts.append({"id": milestone.get("id"), "name": milestone.get("name", "Milestone"),
                                    "plannedDate": planned, "estimatedDate": estimate,
                                    "varianceDays": variance, "status": "Estimate" if estimate else "Insufficient approved activity history",
                                    "linkedActivities": linked, "basisRecords": sum(f.get("basisRecords", 0) for f in related),
                                    "cpmEstimatedDate": cpm_estimate, "cpmVarianceDays": cpm_variance,
                                    "cpmBasis": "Latest early-finish date among linked schedule activities; CPM path calculation, not actual or execution forecast." if cpm_estimate else "Insufficient linked CPM activity dates."})

    # Planner-recorded recovery outcomes become an explicit, auditable forecasting input.
    # They do not alter activity actuals or the approved baseline.
    outcome_records = outcomes or []
    for outcome in outcome_records:
        aid = str(outcome.get("activityId") or "")
        target = next((f for f in forecasts if f["activityId"] == aid), None)
        if not target:
            continue
        observed_finish = _day(outcome.get("actualFinishDate"))
        if observed_finish:
            target.update({"status": "Outcome-informed estimate · schedule actual unchanged",
                           "estimatedFinish": observed_finish.isoformat(), "confidence": "Planner-recorded outcome",
                           "varianceDays": ((_day(observed_finish) - _day(target.get("plannedFinish"))).days if _day(target.get("plannedFinish")) else None),
                           "explanation": "Finish date recorded as a measured outcome by a planner; this estimate does not write the activity actual.",
                           "sourceRecords": list(dict.fromkeys((target.get("sourceRecords") or []) + [str(outcome.get("id"))]))})
        elif outcome.get("actualDaysSaved") not in (None, ""):
            activity = activity_by_id[aid]
            baseline_finish = _day(activity.get("plannedFinish"))
            try:
                saved = int(outcome["actualDaysSaved"])
            except (ValueError, TypeError):
                continue
            if baseline_finish and 0 <= saved <= 365:
                target.update({"status": "Outcome-informed estimate · schedule actual unchanged",
                               "estimatedFinish": date.fromordinal(baseline_finish.toordinal() - saved).isoformat(),
                               "outcomeDaysSaved": saved, "confidence": "Planner-recorded outcome",
                               "varianceDays": -saved,
                               "explanation": f"Planner recorded {saved} calendar day(s) recovered against the planned finish; this is a historical outcome input, not an activity actual.",
                               "sourceRecords": list(dict.fromkeys((target.get("sourceRecords") or []) + [str(outcome.get("id"))]))})

    area_groups: dict[tuple[str, str, str, str], dict] = {}
    approved_by_activity = defaultdict(list)
    for update in approved:
        approved_by_activity[str(update.get("activityId") or "")].append(update)
    impacts_by_activity: dict[str, set[str]] = defaultdict(set)
    for impact in impacts:
        impacts_by_activity[str(impact.get("activityId") or "")].add(str(impact.get("causeId") or ""))
    for aid, activity in activity_by_id.items():
        area_name = str(activity.get("area") or activity.get("location") or "Area not recorded")
        area_id = str(activity.get("areaId") or "")
        sub_name = str(activity.get("subArea") or "Sub-area not recorded")
        sub_id = str(activity.get("subAreaId") or "")
        key = (area_id, area_name, sub_id, sub_name)
        group = area_groups.setdefault(key, {"areaId": area_id, "area": area_name, "subAreaId": sub_id, "subArea": sub_name,
                                             "activityIds": [], "activityCount": 0, "approvedUpdateCount": 0,
                                             "latestApprovedUpdate": None, "linkedCauseCount": 0,
                                             "affectedActivityCount": 0, "sourceRecords": []})
        group["activityIds"].append(aid)
        group["activityCount"] += 1
        group["approvedUpdateCount"] += len(approved_by_activity.get(aid, []))
        group["linkedCauseCount"] += sum(1 for cause in causes if cause.get("activityId") == aid)
        group["affectedActivityCount"] += len(impacts_by_activity.get(aid, set()))
        group["sourceRecords"].extend(str(u.get("id")) for u in approved_by_activity.get(aid, []) if u.get("id"))
        dates = [str(u.get("workDate") or "") for u in approved_by_activity.get(aid, []) if u.get("workDate")]
        if dates and (not group["latestApprovedUpdate"] or max(dates) > group["latestApprovedUpdate"]):
            group["latestApprovedUpdate"] = max(dates)
    area_insights = sorted(area_groups.values(), key=lambda item: (item["area"].casefold(), item["subArea"].casefold()))
    for group in area_insights:
        group["sourceRecords"] = list(dict.fromkeys(group["sourceRecords"]))

    return {"graph": graph, "causes": causes, "impacts": impacts, "areaInsights": area_insights, "activityForecasts": forecasts,
            "milestoneForecasts": milestone_forecasts, "recordCounts": {
                "approvedUpdates": len(approved), "openRisks": sum(1 for r in risks if r.get("status") in {"Open", "Monitoring"}),
                "causes": len(causes), "affectedActivities": len({i["activityId"] for i in impacts}),
                "forecastSourceRecords": sum(len(f.get("sourceRecords", [])) for f in forecasts),
                "recordedOutcomes": len(outcome_records)},
            "asOf": today, "source": "DETERMINISTIC", "requiresHumanReview": True,
            "confidenceContract": "Heuristic scores are uncalibrated; read each basis, assumptions, evidence IDs, and source records.",
            "method": "Deterministic rules over stored schedule and planner-approved execution records; causal labels and forecasts need human review and never change schedule actuals."}


def propose_recommendations(analysis: dict, activities: list[dict]) -> list[dict]:
    by_id = {str(a.get("id") or ""): a for a in activities}
    recommendations = []
    critical_chain = set((analysis.get("graph") or {}).get("criticalChain", []))
    for cause in analysis.get("causes", []):
        aid = cause.get("activityId")
        if aid not in by_id:
            continue
        activity = by_id[aid]
        category = cause.get("category", "Unknown")
        action = {
            "Material": "Confirm the delivery date and available quantity with the site store before the next shift.",
            "Manpower": "Confirm the next-shift crew count and reassignment with the supervisor.",
            "Equipment": "Verify equipment availability and maintenance release before work starts.",
            "Approval": "Name the approver and confirm the required permit or approval time.",
            "Access": "Confirm access clearance and the responsible access owner before mobilization.",
            "Safety": "Keep the activity on hold until the safety observation is reviewed and cleared.",
            "Quality": "Review the inspection or rework record and agree the hold-point release.",
        }.get(category, "Ask the supervisor to confirm the constraint, owner, and next update time.")
        downstream = sorted({str(item.get("activityId")) for item in analysis.get("impacts", [])
                             if item.get("rootActivityId") == aid and item.get("activityId")})
        milestones = sorted({name for item in analysis.get("impacts", []) if item.get("rootActivityId") == aid
                             for name in (item.get("affectedMilestones") or [])})
        is_critical = aid in critical_chain
        priority = "High" if cause.get("severity") in {"High", "Critical"} or is_critical else "Medium"
        priority_reason = ("High severity recorded source" if cause.get("severity") in {"High", "Critical"}
                           else "Activity is on the longest planned-duration path heuristic" if is_critical
                           else "Standard review priority from a linked project constraint")
        recommendations.append({"title": f"Review {category.lower()} constraint for {aid}", "activityId": aid,
                                "action": action, "reason": cause.get("title", "Recorded project constraint"),
                                "category": category, "priority": priority, "priorityReason": priority_reason,
                                "area": activity.get("area") or "Area not recorded", "subArea": activity.get("subArea") or "Sub-area not recorded",
                                "affectedActivities": downstream, "affectedMilestones": milestones,
                                "expectedImpact": (f"Could address a recorded constraint linked to {len(downstream)} downstream activity(ies)" +
                                                   (f" and milestone(s): {', '.join(milestones)}" if milestones else "") +
                                                   "; no numerical benefit is asserted without a measured outcome."),
                                "sourceRecords": cause.get("sourceRecords", []), "evidenceIds": cause.get("evidenceIds", []),
                                "confidence": "Heuristic · planner verification required", "status": "Proposed"})

    # Surface unfinished work that is on the schedule's longest-duration-path heuristic,
    # even when there is no supported blocker to claim as its cause.
    as_of = _day(analysis.get("asOf"))
    tomorrow = date.fromordinal(as_of.toordinal() + 1) if as_of else None
    caused_activities = {str(item.get("activityId") or "") for item in recommendations}
    for aid in (analysis.get("graph") or {}).get("criticalChain", []):
        activity = by_id.get(str(aid))
        if not activity or str(aid) in caused_activities or float(activity.get("progress") or 0) >= 100:
            continue
        planned_start = _day(activity.get("plannedStart"))
        if planned_start and tomorrow and planned_start > tomorrow:
            continue
        recommendations.append({"title": f"Check next-shift readiness for {aid}", "activityId": str(aid),
                                "action": "Confirm predecessor release, workfront access, crew, equipment, and material readiness with the supervisor before the next shift.",
                                "reason": "Unfinished activity appears on the longest planned calendar-duration path heuristic; no blocker cause is inferred.",
                                "category": "Readiness", "priority": "High", "priorityReason": "Unfinished work on a schedule-path heuristic; planner should verify milestone relevance.",
                                "area": activity.get("area") or activity.get("location") or "Area not recorded",
                                "subArea": activity.get("subArea") or "Sub-area not recorded", "affectedActivities": [], "affectedMilestones": [],
                                "expectedImpact": "Readiness confirmation only. Downstream dates and resource availability are not calculated.",
                                "sourceRecords": [str(aid)], "evidenceIds": [],
                                "confidence": "Schedule-path heuristic · planner verification required", "status": "Proposed"})
    return recommendations


def simulate_recovery(analysis: dict, activities: list[dict], activity_id: str, strategy: str,
                      days_saved: int, assumptions: str, today: str, resource_change: str = "",
                      resources: dict | None = None) -> dict:
    strategies = {"manpower": "Add or reassign a crew", "equipment": "Add or substitute equipment",
                  "material": "Expedite or substitute material", "resequence": "Resequence dependent work",
                  "parallel": "Propose parallel execution", "other": "Other planner-defined recovery"}
    if strategy not in strategies:
        raise ValueError("Choose a supported recovery approach.")
    if not isinstance(days_saved, int) or not 0 <= days_saved <= 30:
        raise ValueError("Enter an assumed recovery of 0 to 30 calendar days.")
    activity = next((a for a in activities if str(a.get("id") or "") == activity_id), None)
    if not activity:
        raise ValueError("Choose an activity in this project.")
    resource_change = str(resource_change or "").strip()[:300]
    if strategy in {"manpower", "equipment", "material"} and len(resource_change) < 3:
        raise ValueError("Describe the resource change being assumed for this scenario.")
    resources = resources if isinstance(resources, dict) else {}
    def bounded_number(key: str, minimum: float, maximum: float, integer: bool = False) -> float | int | None:
        value = resources.get(key)
        if value in (None, ""):
            return None
        try:
            number = int(value) if integer else float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a number between {minimum:g} and {maximum:g}.") from exc
        if not math.isfinite(float(number)) or number < minimum or number > maximum:
            raise ValueError(f"{key} must be a number between {minimum:g} and {maximum:g}.")
        return number

    reported_current = bounded_number("currentManpower", 0, 10000, integer=True)
    added_manpower = bounded_number("additionalManpower", 0, 10000, integer=True)
    productivity_change = bounded_number("assumedProductivityChangePct", 0, 200)
    additional_equipment = str(resources.get("additionalEquipment") or "").strip()[:300]
    material_status = str(resources.get("materialStatus") or "Unknown")
    if material_status not in {"Unknown", "Available", "Limited", "Blocked"}:
        raise ValueError("Material status must be Unknown, Available, Limited, or Blocked.")
    forecast = next((f for f in analysis.get("activityForecasts", []) if f["activityId"] == activity_id), None) or {}
    observed_material = str(forecast.get("materialAvailability") or "Unknown")
    material_input = str(resources.get("materialStatus") or "Unknown")
    if material_input == "Unknown" and observed_material in {"Available", "Limited", "Blocked"}:
        material_status = observed_material
        material_status_basis = "latest planner-approved field update"
    else:
        material_status = material_input
        material_status_basis = "planner-entered scenario assumption" if material_input != "Unknown" else "not recorded"
    observed_current = forecast.get("currentManpower")
    manpower_source = "latest planner-approved field update" if observed_current not in (None, "") else "not recorded"
    current_manpower = observed_current if observed_current not in (None, "") else reported_current
    if current_manpower is not None and reported_current is not None and observed_current not in (None, ""):
        # Preserve the stored observation. A conflicting planner assumption is visible, not silently substituted.
        manpower_source += "; conflicting planner entry retained separately"
    if strategy == "manpower" and productivity_change is None and current_manpower not in (None, 0) and added_manpower:
        productivity_change = round(float(added_manpower) / float(current_manpower) * 100, 2)
        productivity_basis = "Linear crew-to-productivity assumption using recorded/entered current crew size"
    elif productivity_change is not None:
        productivity_basis = "Planner-entered productivity-change assumption"
    else:
        productivity_basis = "No productivity-change model available from stored resource data"

    graph = analysis.get("graph", {})
    direct_edges = [edge for edge in graph.get("edges", []) if edge.get("from") == activity_id]
    parallel_id = str(resources.get("parallelActivityId") or "").strip()
    dependency_changes = []
    if strategy == "parallel":
        if not graph.get("valid", False):
            raise ValueError("Resolve the existing dependency graph issues before evaluating parallel work.")
        candidate_edge = next((edge for edge in direct_edges if edge.get("to") == parallel_id), None)
        if not candidate_edge:
            raise ValueError("Parallel work can only be proposed for a direct successor in this project's saved dependency graph.")
        if candidate_edge.get("type") != "Finish-to-Start":
            raise ValueError("This successor is not blocked by a Finish-to-Start link; no dependency change is needed for an overlap scenario.")
        dependency_changes = [{"from": activity_id, "to": parallel_id, "type": candidate_edge["type"],
                               "proposal": "Temporarily overlap these activities in this scenario only",
                               "cycleCheck": "Existing dependency graph is acyclic; this proposal does not rewrite the saved graph.",
                               "scheduleApplied": False, "plannerReviewRequired": True,
                               "limitation": "Technical, safety, resource, and contract constraints still require human validation."}]

    base_date = forecast.get("estimatedFinish") or activity.get("plannedFinish") or ""
    end_date = _day(base_date)
    if end_date is None:
        return {"activityId": activity_id, "activityName": activity.get("name", activity_id),
                "strategy": strategy, "strategyLabel": strategies[strategy], "assumedDaysSaved": days_saved,
                "resourceChange": resource_change,
                "modelInputs": {"remainingProgressPct": max(0.0, 100.0 - float(activity.get("progress") or 0)),
                                "observedDailyProgressPct": forecast.get("dailyRate"), "currentManpower": current_manpower,
                                "currentManpowerSource": manpower_source, "reportedManpowerAssumption": reported_current,
                                "additionalManpower": added_manpower, "additionalEquipment": additional_equipment or None,
                                "currentEquipmentUsed": forecast.get("equipmentUsed"), "materialStatus": material_status,
                                "materialStatusSource": material_status_basis, "assumedProductivityChangePct": productivity_change,
                                "productivityBasis": productivity_basis, "directSuccessorCount": len(direct_edges)},
                "baselineFinish": activity.get("plannedFinish") or None, "referenceFinish": None,
                "scenarioFinish": None, "assumptions": assumptions.strip()[:1200],
                "basis": "Insufficient project data: no approved-history estimate or planned finish is available.",
                "sourceRecords": forecast.get("sourceRecords", []), "affectedActivities": [], "dependencyChanges": dependency_changes,
                "riskAssessment": "Not quantified from stored records; planner review required.",
                "confidence": "Insufficient data", "status": "Insufficient project data for reliable simulation", "asOf": today}
    scenario_days_saved = days_saved
    observed_rate = forecast.get("dailyRate")
    remaining = max(0.0, float(forecast.get("remainingProgressPct", 100.0 - float(activity.get("progress") or 0))))
    estimated_remaining_days = None
    try:
        if observed_rate and float(observed_rate) > 0:
            estimated_remaining_days = math.ceil(remaining / float(observed_rate))
    except (TypeError, ValueError):
        estimated_remaining_days = None
    derived_savings = None
    if estimated_remaining_days is not None and productivity_change is not None and productivity_change > 0:
        faster_days = math.ceil(estimated_remaining_days / (1 + float(productivity_change) / 100))
        derived_savings = max(0, min(30, estimated_remaining_days - faster_days))
        scenario_days_saved = derived_savings
    scenario_date = date.fromordinal(end_date.toordinal() - scenario_days_saved).isoformat() if end_date else None
    planned_finish = _day(activity.get("plannedFinish"))
    variance = (_day(scenario_date) - planned_finish).days if planned_finish and scenario_date else None
    impacted = [i for i in analysis.get("impacts", []) if i.get("activityId") == activity_id]
    graph_node = next((n for n in graph.get("nodes", []) if n["id"] == activity_id), {})
    graph_nodes = graph.get("nodes", [])
    node_names = {node["id"]: node.get("name", node["id"]) for node in graph_nodes}
    transitive, queue = [], list(graph_node.get("successors", []))
    while queue:
        next_id = queue.pop(0)
        if next_id == activity_id or any(item["id"] == next_id for item in transitive):
            continue
        transitive.append({"id": next_id, "name": node_names.get(next_id, next_id)})
        successor = next((n for n in graph_nodes if n["id"] == next_id), {})
        queue.extend(successor.get("successors", []))
    basis = "Planner-entered recovery days saved; no productivity estimate was made from missing resource data."
    if derived_savings is not None:
        basis = (f"Observed progress rate {float(observed_rate):.2f} percentage points/day with {remaining:.1f}% remaining implies about {estimated_remaining_days} calendar days at the same rate. "
                 f"Applying the stated {float(productivity_change):.1f}% productivity-change assumption gives {derived_savings} estimated day(s) recovered; this is arithmetic, not a validated resource model.")
    return {"activityId": activity_id, "activityName": activity.get("name", activity_id),
            "strategy": strategy, "strategyLabel": strategies[strategy], "assumedDaysSaved": days_saved,
            "derivedDaysSaved": derived_savings,
            "resourceChange": resource_change,
            "modelInputs": {"remainingProgressPct": round(remaining, 2), "observedDailyProgressPct": observed_rate,
                            "estimatedRemainingDaysAtObservedRate": estimated_remaining_days,
                            "currentManpower": current_manpower, "currentManpowerSource": manpower_source,
                            "reportedManpowerAssumption": reported_current, "additionalManpower": added_manpower,
                            "additionalEquipment": additional_equipment or None, "currentEquipmentUsed": forecast.get("equipmentUsed"),
                            "materialStatus": material_status, "materialStatusSource": material_status_basis,
                            "assumedProductivityChangePct": productivity_change, "productivityBasis": productivity_basis,
                            "directSuccessorCount": len(direct_edges), "parallelActivityId": parallel_id or None},
            "baselineFinish": activity.get("plannedFinish") or None, "referenceFinish": base_date or None,
            "scenarioFinish": scenario_date, "baselineVarianceDays": variance,
            "affectedActivities": [{"id": activity_id, "name": activity.get("name", activity_id)}] + transitive,
            "dependencyChanges": dependency_changes, "riskAssessment": "Qualitative planner review required; risk probability is not modeled.",
            "assumptions": assumptions.strip()[:1200],
            "basis": basis + (" Parallel overlap remains a proposal; the saved dependency graph is unchanged." if dependency_changes else ""),
            "sourceRecords": list(dict.fromkeys(r for i in impacted for r in i.get("sourceRecords", []))),
            "confidence": "Heuristic scenario; requires human review", "status": "Simulation only · schedule unchanged",
            "simulatedAt": datetime.now().astimezone().isoformat(timespec="seconds"), "asOf": today}
