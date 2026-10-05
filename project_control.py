"""Project-control calculations and review-safe plan extraction helpers.

The module is intentionally deterministic by default. Optional plan extraction uses
the same server-side model configuration as the SiteLink Time Agent and validates
every returned activity against a verbatim source quote before it can be reviewed.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import date, datetime
from typing import Any

from imports import extract_ocr, parse_schedule_file


PROJECT_STATUSES = {"Planning", "Active", "On Hold", "Completed", "Archived"}
RISK_STATUSES = {"Open", "Monitoring", "Mitigated", "Resolved", "Closed"}
RISK_SEVERITIES = {"Low", "Medium", "High", "Critical"}
MATERIAL_STATES = {"Available", "Partially available", "Pending", "Not required", "Unknown"}
CONDITIONS = {"On Track", "At Risk", "Delayed", "Blocked", "Not Started", "Completed"}


def iso_date(value: Any, label: str, optional: bool = True) -> str:
    text = str(value or "").strip()
    if not text and optional:
        return ""
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD format.") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must use YYYY-MM-DD format.")
    return text


def numeric(value: Any, label: str, minimum: float | None = None, maximum: float | None = None, optional: bool = True) -> float | None:
    if value in (None, "") and optional:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if minimum is not None and result < minimum or maximum is not None and result > maximum:
        if minimum is not None and maximum is not None:
            raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}.")
        raise ValueError(f"{label} is outside the supported range.")
    return result


def weighted_progress(activities: list[dict[str, Any]]) -> dict[str, Any]:
    """Return current and planned completion using a documented weight hierarchy."""
    if not activities:
        return {"actual": 0, "planned": 0, "variance": 0, "weighting": "No activities", "activityCount": 0}
    use_weight = all(numeric(a.get("weight"), "Activity weight", 0, optional=True) not in (None, 0) for a in activities)
    use_quantity = not use_weight and all(numeric(a.get("plannedQuantity"), "Planned quantity", 0, optional=True) not in (None, 0) for a in activities)
    if use_weight:
        weights = [float(a["weight"]) for a in activities]
        method = "Configured activity weights"
    elif use_quantity:
        weights = [float(a["plannedQuantity"]) for a in activities]
        method = "Planned quantities"
    else:
        weights = [max(1, _duration_days(a)) for a in activities]
        method = "Planned duration fallback"
    total = sum(weights) or float(len(activities))
    actual = sum(min(100.0, max(0.0, float(a.get("progress") or 0))) * w for a, w in zip(activities, weights)) / total
    planned = sum(min(100.0, max(0.0, float(a.get("plannedProgress") or 0))) * w for a, w in zip(activities, weights)) / total
    return {"actual": round(actual, 1), "planned": round(planned, 1), "variance": round(actual - planned, 1), "weighting": method, "activityCount": len(activities)}


def _duration_days(activity: dict[str, Any]) -> int:
    try:
        start = date.fromisoformat(str(activity.get("plannedStart", ""))[:10])
        finish = date.fromisoformat(str(activity.get("plannedFinish", ""))[:10])
        return max(1, (finish - start).days + 1)
    except (TypeError, ValueError):
        return 1


def activity_condition(activity: dict[str, Any], today: str, open_risks: list[dict[str, Any]] | None = None, missing_days: int = 0) -> dict[str, Any]:
    """Calculate explainable condition without changing an approved schedule."""
    progress = min(100.0, max(0.0, float(activity.get("progress") or 0)))
    reasons: list[str] = []
    risks = open_risks or []
    blocking = [r for r in risks if str(r.get("status", "Open")) in {"Open", "Monitoring"} and (r.get("payload", {}).get("type") == "blocker" or r.get("severity") in {"Critical", "High"})]
    if progress >= 100 or activity.get("actualFinish"):
        condition = "Completed"
        reasons.append("Actual completion is recorded.")
    elif blocking:
        condition = "Blocked" if any(r.get("payload", {}).get("type") == "blocker" for r in blocking) else "At Risk"
        reasons.append("Open risk: " + str(blocking[0].get("title") or "reported issue"))
    elif progress == 0 and str(activity.get("status", "")).lower() in {"not started", "not started ", ""} and not activity.get("actualStart"):
        condition = "Not Started"
        reasons.append("No approved actual start or progress is recorded.")
    else:
        planned = activity.get("plannedProgress")
        try:
            planned_value = float(planned) if planned not in (None, "") else None
        except (TypeError, ValueError):
            planned_value = None
        finish = str(activity.get("plannedFinish") or "")[:10]
        overdue = bool(finish and finish < today)
        if overdue:
            condition = "Delayed"
            reasons.append("Planned finish date has passed with work incomplete.")
        elif planned_value is not None and progress + 10 < planned_value:
            condition = "Delayed" if planned_value - progress >= 20 else "At Risk"
            reasons.append(f"Actual progress is {progress:g}% versus {planned_value:g}% planned.")
        elif any(r.get("payload", {}).get("materialAvailability") == "Pending" for r in risks):
            condition = "At Risk"
            reasons.append("Material availability is reported as pending.")
        elif activity.get("blocker"):
            condition = "At Risk"
            reasons.append("A blocker is recorded: " + str(activity["blocker"]))
        elif missing_days >= 2:
            condition = "At Risk"
            reasons.append(f"No approved daily update has been received for {missing_days} days.")
        else:
            condition = "On Track"
            reasons.append("No overdue date or approved blocker has been recorded.")
    return {"status": condition, "reasons": reasons, "actualProgress": progress, "plannedProgress": activity.get("plannedProgress"), "missingDays": missing_days}


def normalize_schedule_row(row: dict[str, Any]) -> dict[str, Any]:
    def val(*keys: str, default: Any = "") -> Any:
        return next((row[k] for k in keys if row.get(k) not in (None, "")), default)
    predecessors = val("predecessors", default=[])
    if isinstance(predecessors, str):
        predecessors = [p.strip() for p in re.split(r"[,;|]", predecessors) if p.strip()]
    aliases = val("aliases", default=[])
    if isinstance(aliases, str):
        aliases = [p.strip() for p in re.split(r"[,;|]", aliases) if p.strip()]
    start = str(val("planned_start", "plannedStart"))[:10]
    finish = str(val("planned_finish", "plannedFinish"))[:10]
    return {
        "id": str(val("activity_id", "activityId", "id")).strip(),
        "wbs": str(val("wbs")), "name": str(val("activity_name", "activityName", "name")).strip(),
        "area": str(val("area", "area_name", "areaName")), "subArea": str(val("sub_area", "subarea", "sub_area_name", "subArea")),
        "workPackage": str(val("work_package", "workpackage", "workPackage")),
        "discipline": str(val("discipline", default="Unassigned")) or "Unassigned",
        "location": str(val("location")), "plannedStart": start, "plannedFinish": finish,
        "durationDays": numeric(val("duration_days", "durationDays"), "Duration", 0),
        "plannedQuantity": numeric(val("planned_quantity", "quantity", "plannedQuantity"), "Planned quantity", 0),
        "unit": str(val("unit", "uom")), "plannedProgress": numeric(val("planned_progress", "plannedProgress"), "Planned progress", 0, 100) or 0,
        "progress": numeric(val("percent_complete", "progress"), "Progress", 0, 100) or 0,
        "owner": str(val("owner", "responsible", "responsible_person", "team", default="")),
        "contractor": str(val("contractor")), "predecessors": predecessors, "aliases": aliases,
        "weight": numeric(val("weight", "activity_weight"), "Activity weight", 0),
        "isMilestone": str(val("milestone", "is_milestone", "isMilestone")).lower() in {"1", "yes", "true", "milestone"},
        "status": "Not started", "actualStart": "", "actualFinish": "",
        "sourceEvidence": {str(k): v for k, v in row.items() if v not in (None, "")},
    }


def _model_configured() -> bool:
    return os.environ.get("TIME_AGENT_ENABLED", "0").strip().lower() in {"1", "true", "yes"} and bool(os.environ.get("OPENAI_API_KEY", "").strip()) and bool(os.environ.get("TIME_AGENT_MODEL", "").strip())


_PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["activities"],
    "properties": {"activities": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["activityId", "name", "area", "subArea", "workPackage", "discipline", "location", "plannedStart", "plannedFinish", "plannedQuantity", "unit", "plannedProgress", "dependencies", "owner", "contractor", "weight", "isMilestone", "sourceQuote"],
        "properties": {
            **{k: {"type": ["string", "null"]} for k in ("activityId", "name", "area", "subArea", "workPackage", "discipline", "location", "plannedStart", "plannedFinish", "unit", "owner", "contractor")},
            **{k: {"type": ["number", "null"]} for k in ("plannedQuantity", "plannedProgress", "weight")},
            "dependencies": {"type": "array", "items": {"type": "string"}}, "isMilestone": {"type": "boolean"}, "sourceQuote": {"type": "string"},
        },
    }}}
}


def _model_plan_candidates(text: str) -> tuple[list[dict[str, Any]], str]:
    endpoint = os.environ.get("TIME_AGENT_API_URL", "https://api.openai.com/v1/responses").strip()
    timeout = max(2, min(30, int(os.environ.get("TIME_AGENT_TIMEOUT_SECONDS", "12"))))
    request_body = {
        "model": os.environ.get("TIME_AGENT_MODEL", "").strip(), "store": False,
        "instructions": "Extract candidate project schedule activities from the supplied plan text. Do not invent missing data. Every candidate must include an exact verbatim sourceQuote that appears in the supplied text. Use null for unsupported values. This is a draft for human review and must never approve or change a baseline.",
        "input": text[:40000],
        "text": {"format": {"type": "json_schema", "name": "sitelink_plan_candidates", "strict": True, "schema": _PLAN_SCHEMA}},
    }
    request = urllib.request.Request(endpoint, data=json.dumps(request_body).encode("utf-8"), headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"].strip(), "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
    output = str(result.get("output_text") or "")
    if not output:
        output = next((part.get("text", "") for item in result.get("output", []) for part in item.get("content", []) if part.get("type") == "output_text"), "")
    parsed = json.loads(output)
    activities = parsed.get("activities") if isinstance(parsed, dict) else None
    if not isinstance(activities, list) or len(activities) > 500:
        raise ValueError("The model returned an invalid activity list.")
    validated = []
    for item in activities:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("sourceQuote") or "")
        activity_id = str(item.get("activityId") or "").strip()
        name = str(item.get("name") or "").strip()
        if not quote or quote not in text or not activity_id or not name or activity_id.casefold() not in quote.casefold() or name.casefold() not in quote.casefold():
            continue
        row = {"id": activity_id, "name": name}
        for dest, source in (("area", "area"), ("subArea", "subArea"), ("workPackage", "workPackage"), ("discipline", "discipline"), ("location", "location"), ("plannedStart", "plannedStart"), ("plannedFinish", "plannedFinish"), ("unit", "unit"), ("owner", "owner"), ("contractor", "contractor")):
            value = item.get(source)
            if value is not None:
                text_value = str(value).strip()
                if not text_value or text_value.casefold() not in quote.casefold():
                    value = None
            row[dest] = str(value or "")
        for field, low, high in (("plannedQuantity", 0, None), ("plannedProgress", 0, 100), ("weight", 0, None)):
            value = numeric(item.get(field), field, low, high)
            row["plannedProgress" if field == "plannedProgress" else field] = value if value is not None else (0 if field == "plannedProgress" else None)
        for date_field in ("plannedStart", "plannedFinish"):
            if row[date_field]:
                try:
                    iso_date(row[date_field], date_field, optional=False)
                except ValueError:
                    row[date_field] = ""
        deps = item.get("dependencies") if isinstance(item.get("dependencies"), list) else []
        row.update({"wbs": "", "progress": 0, "actualStart": "", "actualFinish": "", "predecessors": [str(x) for x in deps if str(x).strip()], "aliases": [], "isMilestone": bool(item.get("isMilestone")), "status": "Not started", "aiGenerated": True, "sourceEvidence": {"quote": quote}})
        validated.append(row)
    return validated, "Configured LLM · structured candidates with quote validation"


def analyze_plan_file(filename: str, content: bytes) -> dict[str, Any]:
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix in {"csv", "xlsx", "xml"}:
        rows = [normalize_schedule_row(row) for row in parse_schedule_file(filename, content)]
        for row in rows:
            row["aiGenerated"] = False
        return {"activities": rows, "analysisMethod": "Schedule file parser · reviewer confirmation required", "transcription": "", "warning": "Imported values remain proposals until a user reviews and approves this baseline."}
    if suffix not in {"pdf", "png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"}:
        raise ValueError("Upload a schedule CSV/XLSX/XML, PDF, or plan image.")
    scan = extract_ocr(filename, content)
    transcription = scan.get("text", "")
    if _model_configured():
        try:
            activities, method = _model_plan_candidates(transcription)
            return {"activities": activities, "analysisMethod": method, "transcription": transcription, "warning": "AI candidates are unapproved. Inspect every field and source quote before approving the baseline."}
        except Exception:
            pass
    return {"activities": [], "analysisMethod": "OCR text only · model unavailable; no plan baseline changed", "transcription": transcription, "warning": "Review the OCR transcription. Upload a structured CSV/XLSX/XML schedule or configure the optional model to produce draft activity rows."}

