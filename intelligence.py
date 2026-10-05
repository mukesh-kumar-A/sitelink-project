"""Explainable evidence extraction and heuristic matching for SiteLink.

Local rules always provide the safe fallback. Optional server-side structured
LLM extraction is quote-validated. This module never updates a schedule; a
planner approval in the authenticated application is required for actuals.
"""
from __future__ import annotations

import re
import json
import math
import os
import urllib.error
import urllib.request
from difflib import SequenceMatcher
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from statistics import mean
from typing import Any


STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "by", "can", "continue",
    "continues", "crew", "day", "for", "from", "in", "is", "it", "of", "on", "or",
    "our", "site", "team", "the", "their", "this", "to", "today", "was", "were", "with",
    "work", "activity", "percent", "complete", "completed", "finished", "done", "started",
}
SYNONYMS = {
    "erect": "install", "erected": "install", "erecting": "install", "erection": "install",
    "installation": "install", "installed": "install", "installing": "install",
    "mounted": "install", "mounting": "install", "assembled": "install", "assembly": "install",
    "spools": "spool", "pipelines": "pipe", "piping": "pipe", "pipes": "pipe",
    "calibrated": "calibrate", "calibration": "calibrate", "calibrating": "calibrate",
    "began": "start", "begin": "start", "commenced": "start", "commencing": "start",
    "finishing": "complete", "closed": "complete", "closeout": "complete", "closedout": "complete",
    "hydrotesting": "hydrotest", "hydrotested": "hydrotest", "hydrotests": "hydrotest",
    "inspected": "inspect", "inspection": "inspect", "inspecting": "inspect",
    "waiting": "wait", "awaiting": "wait", "delayed": "delay",
    "tube": "pipe", "tubing": "pipe", "piping": "pipe", "pipeline": "pipe",
    "fitup": "fit-up", "fit-up": "fit-up", "termination": "terminate", "terminated": "terminate",
    "traywork": "tray", "cableway": "tray", "cables": "cable", "wiring": "cable",
    "pressure-test": "hydrotest", "hydrostatic": "hydrotest", "hydrostatic-test": "hydrotest",
    "alignment": "align", "aligned": "align", "coupled": "couple", "coupling": "couple",
    "pouring": "pour", "poured": "pour", "backfilled": "backfill", "reinstatement": "backfill",
}
DISCIPLINES = {
    "civil": ("civil", "concrete", "foundation", "backfill", "excavation", "trench"),
    "piping": ("piping", "pipe", "spool", "weld", "hydrotest", "line"),
    "electrical": ("electrical", "cable", "tray", "substation", "earthing"),
    "instrumentation": ("instrumentation", "instrument", "transmitter", "calibrate", "loop check"),
    "equipment": ("equipment", "pump", "compressor", "alignment", "coupling", "mechanical"),
    "hse": ("hse", "safety", "permit", "confined space", "inspection"),
}
ACTIVITY_ID = re.compile(r"\b(?:CIV|PIP|ELE|INS|EQP|HSE)(?:-L[56])?-\d{2,4}(?:-[A-Z0-9]+)*\b", re.I)
EQUIPMENT_TAG = re.compile(r"\b(?:PT|P|CW|V|TK|M|E)-\d{2,4}(?:-[A-Z0-9]+)*\b", re.I)
LINE_TAG = re.compile(r"\b(?:LINE\s*)?(\d{1,3}-[A-Z]{1,4})\b", re.I)
LINE_ENTITY = re.compile(r"\b(?:LINE\s*)?\d{1,3}-[A-Z]{1,4}\b", re.I)
TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.I)
ISO_DATE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
SLASH_DATE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b")
MONTH_DATE = re.compile(r"\b(\d{1,2})\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)(?:\s+(20\d{2}))?\b", re.I)
MONTH_FIRST_DATE = re.compile(r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2})(?:,?\s+(20\d{2}))?\b", re.I)
TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\b", re.I)
MONTHS = {name.lower(): index for index, name in enumerate(("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1)}
MONTH_ALIASES = {alias: number for number, name in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1) for alias in (name, name + "uary" if name == "jan" else name + "ruary" if name == "feb" else name + "ch" if name == "mar" else name + "il" if name == "apr" else name + "e" if name in {"jun", "jul"} else name + "ust" if name == "aug" else name + "tember" if name == "sep" else name + "ober" if name == "oct" else name + "ember" if name in {"nov", "dec"} else name)}


def _valid_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return ""


def _date_from_text(text: str, report_date: str) -> tuple[str, str]:
    iso_matches = list(ISO_DATE.finditer(text))
    if len(iso_matches) > 1:
        return "", ""
    match = iso_matches[0] if iso_matches else None
    if match:
        value = _valid_date("-".join(match.groups()))
        if value:
            return value, match.group(0)
    slash_matches = list(SLASH_DATE.finditer(text))
    if len(slash_matches) > 1:
        return "", ""
    match = slash_matches[0] if slash_matches else None
    if match:
        first, second, year = map(int, match.groups())
        # Accept only slash dates whose order is unambiguous; do not assume locale.
        if first > 12 and second <= 12:
            day, month = first, second
        elif second > 12 and first <= 12:
            month, day = first, second
        else:
            day = month = 0
        if day and month:
            try:
                return date(year, month, day).isoformat(), match.group(0)
            except ValueError:
                pass
    for pattern, day_index, month_index, year_index in ((MONTH_DATE, 1, 2, 3), (MONTH_FIRST_DATE, 2, 1, 3)):
        matches = list(pattern.finditer(text))
        if len(matches) > 1:
            return "", ""
        match = matches[0] if matches else None
        if not match:
            continue
        day_number = int(match.group(day_index))
        month_number = MONTH_ALIASES.get(match.group(month_index).lower()[:3])
        year_number = int(match.group(year_index)) if match.group(year_index) else 0
        try:
            if year_number:
                return date(year_number, month_number or 1, day_number).isoformat(), match.group(0)
        except ValueError:
            pass
    relative_day = re.search(r"\b(today|this morning|this afternoon|tonight)\b", text, re.I)
    if relative_day and _valid_date(report_date):
        return report_date, relative_day.group(0)
    return "", ""


def normalize_text(text: str) -> str:
    normalized = str(text or "").lower().replace("–", "-").replace("—", "-")
    words = TOKEN.findall(normalized)
    return " ".join(SYNONYMS.get(word, word) for word in words)


def _tokens(text: str) -> set[str]:
    return {token for token in normalize_text(text).split() if len(token) > 1 and token not in STOP_WORDS}


def _entities(text: str) -> set[str]:
    upper = str(text or "").upper().replace("–", "-").replace("—", "-")
    found = {value.replace(" ", "") for value in ACTIVITY_ID.findall(upper)}
    found.update(value.replace(" ", "") for value in EQUIPMENT_TAG.findall(upper))
    found.update(value.replace(" ", "") for value in LINE_ENTITY.findall(upper))
    return found


def detect_discipline(text: str, declared: str = "") -> str:
    if declared:
        low = declared.strip().lower()
        aliases = {"mechanical": "Equipment", "instrument": "Instrumentation", "elec": "Electrical", "pipe": "Piping"}
        return aliases.get(low, next((name.title() if name != "hse" else "HSE" for name in DISCIPLINES if name == low), declared.strip()))
    low = str(text or "").lower()
    scores = {name: sum(1 for word in words if word in low) for name, words in DISCIPLINES.items()}
    best = max(scores, key=scores.get)
    return best.title() if scores[best] else "Unassigned"


def canonical_activity(text: str, discipline: str = "") -> dict[str, Any]:
    return {
        "raw": str(text or "").strip(),
        "normalized": normalize_text(text),
        "tokens": sorted(_tokens(text)),
        "entities": sorted(_entities(text)),
        "discipline": detect_discipline(text, discipline),
    }


def extract_facts(report: dict[str, Any]) -> dict[str, Any]:
    text = str(report.get("text") or report.get("report") or report.get("description") or "")
    low = text.lower()
    report_date = _valid_date(str(report.get("date") or report.get("reportDate") or ""))
    event_date, date_evidence = _date_from_text(text, report_date)
    structured_event_date = _valid_date(str(report.get("eventDate") or report.get("event_date") or ""))
    if structured_event_date:
        event_date, date_evidence = structured_event_date, str(report.get("eventDate") or report.get("event_date"))
    date_context = next((part.strip() for part in re.split(r"(?<=[.!?;])\s+|[;\n]+", text) if date_evidence and date_evidence.casefold() in part.casefold()), "")
    if event_date and not structured_event_date and not re.search(r"\b(started|start|begin|began|commenced|mobilized|mobilised|completed|complete|finished|closed out|done|blocked|waiting|awaiting|delayed)\b|\b\d+\s*(?:%|percent)\b", date_context, re.I):
        event_date, date_evidence, date_context = "", "", ""
    evidence: list[dict[str, Any]] = []
    report_discipline = str(report.get("discipline") or "")
    discipline = detect_discipline(text, report_discipline)
    facts: dict[str, Any] = {
        "activityDescription": "", "discipline": discipline, "location": "", "lineTag": "", "equipmentTag": "",
        "activityId": "", "eventStatus": "", "eventDate": event_date, "progress": None,
        "actualStart": "", "actualEnd": "", "startTime": "", "endTime": "", "blocker": "",
        "reportDate": report_date, "evidence": evidence,
    }

    description_match = next((sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+|\n+", text) if sentence.strip()), "")
    if description_match:
        facts["activityDescription"] = description_match[:240]
        evidence.append({"field": "activityDescription", "value": facts["activityDescription"], "method": "source sentence", "quote": facts["activityDescription"]})
    declared_location = str(report.get("location") or "").strip()
    if declared_location:
        facts["location"] = declared_location[:120]
        evidence.append({"field": "location", "value": facts["location"], "method": "structured source field", "quote": declared_location[:220]})
    else:
        location_match = re.search(r"\b(?:in|at|near|within|outside|on)\s+((?:unit\s+[A-Z0-9]+|zone\s+[A-Z0-9]+|substation\s+[A-Z0-9]+|process\s+train\s+[A-Z0-9]+|pump\s+house(?:\s*[- ]\s*[A-Z0-9]+)?|tank\s+[A-Z0-9]+|grid\s+[A-Z0-9]+|rack\s+[A-Z0-9]+|utility\s+corridor|north\s+access(?:\s+road)?|south\s+access(?:\s+road)?|area\s+[A-Z0-9]+)(?:\s*[-:]\s*[A-Z0-9]+)?)", text, re.I)
        if location_match:
            facts["location"] = location_match.group(1).strip(" .,-")
            evidence.append({"field": "location", "value": facts["location"], "method": "location phrase in source", "quote": location_match.group(0)})
    activity_match = ACTIVITY_ID.search(text)
    structured_activity_id = str(report.get("activityId") or report.get("activity_id") or report.get("sourceActivityId") or "").strip()
    if activity_match or structured_activity_id:
        facts["activityId"] = (activity_match.group(0) if activity_match else structured_activity_id).upper()
        evidence.append({"field": "activityId", "value": facts["activityId"], "method": "activity ID in source" if activity_match else "structured source field", "quote": activity_match.group(0) if activity_match else structured_activity_id})
    line_match = LINE_TAG.search(text)
    structured_line_tag = str(report.get("lineTag") or report.get("line_tag") or "").strip()
    if line_match or structured_line_tag:
        facts["lineTag"] = (re.sub(r"^LINE\s*", "", line_match.group(0), flags=re.I) if line_match else structured_line_tag).strip().upper()
        evidence.append({"field": "lineTag", "value": facts["lineTag"], "method": "line tag in source" if line_match else "structured source field", "quote": line_match.group(0) if line_match else structured_line_tag})
    equipment_match = EQUIPMENT_TAG.search(text)
    structured_equipment_tag = str(report.get("equipmentTag") or report.get("equipment_tag") or "").strip()
    if equipment_match or structured_equipment_tag:
        facts["equipmentTag"] = (equipment_match.group(0) if equipment_match else structured_equipment_tag).upper()
        evidence.append({"field": "equipmentTag", "value": facts["equipmentTag"], "method": "equipment tag in source" if equipment_match else "structured source field", "quote": equipment_match.group(0) if equipment_match else structured_equipment_tag})
    if report_discipline:
        evidence.append({"field": "discipline", "value": discipline, "method": "structured source field", "quote": report_discipline})
    else:
        discipline_quote = next((sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+|\n+", text) if any(term in sentence.lower() for terms in DISCIPLINES.values() for term in terms)), "")
        if discipline_quote and discipline != "Unassigned":
            evidence.append({"field": "discipline", "value": discipline, "method": "discipline terminology in source", "quote": discipline_quote[:220]})

    explicit_start = _valid_date(str(report.get("actualStart") or report.get("actual_start") or ""))
    explicit_end = _valid_date(str(report.get("actualEnd") or report.get("actualFinish") or report.get("actual_end") or ""))
    if explicit_start:
        facts["actualStart"] = explicit_start
        evidence.append({"field": "actualStart", "value": explicit_start, "method": "structured source field", "quote": "actual_start"})
    if explicit_end:
        facts["actualEnd"] = explicit_end
        evidence.append({"field": "actualEnd", "value": explicit_end, "method": "structured source field", "quote": "actual_end"})
    percent_match = re.search(r"\b(100|[1-9]?\d)\s*(?:%|percent\b)", text, re.I)
    structured_progress = report.get("progress")
    if structured_progress not in (None, ""):
        try:
            value = max(0, min(100, int(float(structured_progress))))
            facts["progress"] = value
            evidence.append({"field": "progress", "value": value, "method": "supervisor-confirmed structured field", "quote": str(structured_progress)})
        except (ValueError, TypeError):
            pass
    elif percent_match:
        facts["progress"] = int(percent_match.group(1))
        evidence.append({"field": "progress", "value": facts["progress"], "method": "explicit percentage in report", "quote": percent_match.group(0)})

    start_words = bool(re.search(r"\b(started|start|begin|began|commenced|commencing|mobilized|mobilised)\b", low))
    date_start_words = bool(re.search(r"\b(started|start|begin|began|commenced|commencing|mobilized|mobilised)\b", date_context, re.I)) or bool(structured_event_date and start_words)
    complete_words = bool(re.search(r"\b(completed|complete|finished|closed out|done)\b", low))
    date_complete_words = bool(re.search(r"\b(completed|complete|finished|closed out|done)\b", date_context, re.I))
    if facts["progress"] is not None and facts["progress"] < 100 and re.search(r"\b\d+\s*(?:%|percent\b)\s*complete\b", low):
        complete_words = False
    blocked_words = bool(re.search(r"\b(blocked|cannot start|can't start|on hold|waiting for|waiting on|awaiting|delayed by|held up by)\b", low))
    if blocked_words:
        facts["eventStatus"] = "blocked"
    elif complete_words or facts["progress"] == 100:
        facts["eventStatus"] = "complete"
    elif start_words:
        facts["eventStatus"] = "started"
    time_matches = list(TIME.finditer(text))
    for match in time_matches:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = (match.group(3) or "").lower().replace(".", "")
        before = re.split(r"[.!?;\n]", low[:match.start()])[-1]
        after = re.split(r"[.!?;\n]", low[match.end():])[0]
        explicit_24_hour = not meridiem and bool(match.group(2)) and 0 <= hour <= 23 and minute < 60
        if not meridiem and not explicit_24_hour:
            if re.search(r"\b(morning|a\.m\.)\b", before + " " + after):
                meridiem = "am"
            elif re.search(r"\b(afternoon|evening|tonight|p\.m\.)\b", before + " " + after):
                meridiem = "pm"
            else:
                continue
        if explicit_24_hour:
            value = f"{hour:02d}:{minute:02d}"
        elif 1 <= hour <= 12 and minute < 60:
            hour = hour % 12 + (12 if meridiem == "pm" else 0)
            value = f"{hour:02d}:{minute:02d}"
        else:
            continue
        if value:
            if re.search(r"\b(started|start|begin|began|commenced)\b", before):
                facts["startTime"] = value
                if event_date and not facts["actualStart"]:
                    facts["actualStart"] = event_date
                evidence.append({"field": "startTime", "value": value, "method": "explicit start clock time", "quote": match.group(0).strip()})
            elif re.search(r"\b(finish|finished|complete|completed|ended|end)\b", before):
                facts["endTime"] = value
                if event_date and not facts["actualEnd"]:
                    facts["actualEnd"] = event_date
                evidence.append({"field": "endTime", "value": value, "method": "explicit finish clock time", "quote": match.group(0).strip()})
            elif re.search(r"\b(started|start|begin|began)\b", after):
                facts["startTime"] = value
                if event_date and not facts["actualStart"]:
                    facts["actualStart"] = event_date
                evidence.append({"field": "startTime", "value": value, "method": "explicit start clock time", "quote": match.group(0).strip()})
            elif re.search(r"\b(finish|finished|complete|completed|end)\b", after):
                facts["endTime"] = value
                if event_date and not facts["actualEnd"]:
                    facts["actualEnd"] = event_date
                evidence.append({"field": "endTime", "value": value, "method": "explicit finish clock time", "quote": match.group(0).strip()})
            evidence.append({"field": "time", "value": value, "method": "explicit clock time", "quote": match.group(0)})
    if event_date:
        facts["eventDate"] = event_date
        evidence.append({"field": "eventDate", "value": event_date, "method": "structured event date" if structured_event_date else "explicit date/relative-day wording", "quote": date_evidence})
        if date_start_words and not facts["actualStart"]:
            facts["actualStart"] = event_date
        if date_start_words and facts["actualStart"] == event_date and not any(item["field"] == "actualStart" for item in evidence):
            evidence.append({"field": "actualStart", "value": event_date, "method": "explicit start event tied to stated event date", "quote": date_context or date_evidence})
        if (date_complete_words or facts["eventStatus"] == "complete") and not facts["actualEnd"]:
            facts["actualEnd"] = event_date
        if (date_complete_words or facts["eventStatus"] == "complete") and facts["actualEnd"] == event_date and not any(item["field"] == "actualEnd" for item in evidence):
            evidence.append({"field": "actualEnd", "value": event_date, "method": "explicit completion tied to stated event date", "quote": date_context or date_evidence})

    blocker_match = re.search(r"\b(?:waiting\s+(?:for|on)|awaiting|blocked\s+by|delayed\s+by|held\s+up\s+by|cannot\s+start\s+until|can't\s+start\s+until)\s+([^.!?;,]+)", text, re.I)
    if blocker_match:
        facts["blocker"] = blocker_match.group(1).strip(" .,")[:160]
        evidence.append({"field": "blocker", "value": facts["blocker"], "method": "reported dependency/blocker phrase", "quote": blocker_match.group(0)[:220]})
    if facts["eventStatus"]:
        event_words = re.search(r"\b(?:blocked|cannot start|can't start|on hold|waiting for|waiting on|awaiting|delayed by|held up by|started|start|begin|began|commenced|commencing|mobilized|mobilised|completed|complete|finished|closed out|done)\b", text, re.I)
        evidence.append({"field": "eventStatus", "value": facts["eventStatus"], "method": "event wording in source", "quote": event_words.group(0) if event_words else str(facts["progress"]) + "%"})
    return facts


LLM_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["activityDescription", "discipline", "location", "lineTag", "equipmentTag", "activityId", "eventStatus", "eventDate", "actualStart", "actualEnd", "startTime", "endTime", "progress", "blocker", "evidence"],
    "properties": {
        **{field: {"type": ["string", "null"]} for field in ("activityDescription", "discipline", "location", "lineTag", "equipmentTag", "activityId", "eventStatus", "eventDate", "actualStart", "actualEnd", "startTime", "endTime", "blocker")},
        "progress": {"type": ["number", "null"]},
        "evidence": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["field", "value", "quote"], "properties": {"field": {"type": "string"}, "value": {"type": "string"}, "quote": {"type": "string"}}}},
    },
}


def _llm_configured() -> bool:
    return os.environ.get("TIME_AGENT_ENABLED", "0").strip().lower() in {"1", "true", "yes"} and bool(os.environ.get("OPENAI_API_KEY", "").strip()) and bool(os.environ.get("TIME_AGENT_MODEL", "").strip())


def _llm_json(report: dict[str, Any]) -> dict[str, Any]:
    endpoint = os.environ.get("TIME_AGENT_API_URL", "https://api.openai.com/v1/responses").strip()
    model = os.environ.get("TIME_AGENT_MODEL", "").strip()
    timeout = max(2, min(30, int(os.environ.get("TIME_AGENT_TIMEOUT_SECONDS", "12"))))
    body = {
        "model": model,
        "store": False,
        "instructions": "Extract only facts explicitly supported by the supervisor's source text or structured report fields. Never infer missing event dates, progress, identifiers, or blockers. For every non-null extracted value, provide an exact verbatim quote from the source text; for a structured field, quote its supplied value. If uncertain, return null. Activity matching and schedule updates are handled separately and require human approval.",
        "input": json.dumps({"text": report.get("text", ""), "reportDate": report.get("date", ""), "eventDate": report.get("eventDate", ""), "progress": report.get("progress"), "discipline": report.get("discipline", ""), "location": report.get("location", ""), "activityId": report.get("activityId", "")}, ensure_ascii=False),
        "text": {"format": {"type": "json_schema", "name": "sitelink_time_agent_facts", "strict": True, "schema": LLM_SCHEMA}},
    }
    request = urllib.request.Request(endpoint, data=json.dumps(body).encode("utf-8"), headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"].strip(), "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read(1024 * 1024).decode("utf-8"))
    output_text = str(result.get("output_text") or "")
    if not output_text:
        for item in result.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    output_text = content.get("text", "")
                    break
            if output_text:
                break
    parsed = json.loads(output_text)
    if not isinstance(parsed, dict):
        raise ValueError("Model response was not an object")
    return parsed


def _validated_llm_facts(report: dict[str, Any], proposed: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    source = str(report.get("text") or "")
    report_date = str(report.get("date") or "")
    allowed = {"activityDescription", "discipline", "location", "lineTag", "equipmentTag", "activityId", "eventStatus", "eventDate", "actualStart", "actualEnd", "startTime", "endTime", "progress", "blocker"}
    evidence_by_field: dict[str, dict[str, str]] = {}
    for evidence in proposed.get("evidence", []) if isinstance(proposed.get("evidence"), list) else []:
        if not isinstance(evidence, dict):
            continue
        field, value, quote = str(evidence.get("field") or ""), evidence.get("value"), str(evidence.get("quote") or "")
        if field in allowed and value is not None and quote and quote in source:
            evidence_by_field[field] = {"value": value, "quote": quote}
    facts = dict(base)
    for field, item in evidence_by_field.items():
        value = item["value"]
        quote = item["quote"]
        if field in {"actualStart", "actualEnd", "startTime", "endTime", "eventDate", "eventStatus", "progress"}:
            extracted = extract_facts({"text": quote, "date": report_date, "discipline": report.get("discipline", "")})
            proposed_value = str(value)
            extracted_value = extracted.get(field)
            if field == "progress" and extracted_value not in (None, ""):
                try:
                    if float(extracted_value) != float(value):
                        continue
                except (TypeError, ValueError):
                    continue
            elif extracted_value in (None, "") or str(extracted_value) != proposed_value:
                continue
        elif field == "activityId":
            if str(value).upper() not in quote.upper() or not ACTIVITY_ID.search(str(value)):
                continue
            value = str(value).upper()
        elif field == "lineTag":
            if str(value).upper().replace(" ", "") not in quote.upper().replace(" ", ""):
                continue
            value = str(value).upper()
        elif field == "equipmentTag":
            if str(value).upper() not in quote.upper() or not EQUIPMENT_TAG.search(str(value)):
                continue
            value = str(value).upper()
        elif field == "discipline":
            if str(value) != detect_discipline(quote, str(report.get("discipline") or "")) or value == "Unassigned":
                continue
        else:
            if not isinstance(value, str) or not value.strip() or value.casefold() not in quote.casefold():
                continue
        facts[field] = value
        facts.setdefault("evidence", []).append({"field": field, "value": value, "method": "LLM extraction; quote validated against source", "quote": quote})
    return facts


def extract_facts_with_provider(report: dict[str, Any]) -> tuple[dict[str, Any], str]:
    base = extract_facts(report)
    if not _llm_configured():
        return base, "Local rules fallback (model not configured)"
    try:
        proposed = _llm_json(report)
        return _validated_llm_facts(report, proposed, base), "OpenAI Responses API · structured output with source-quote validation"
    except Exception:
        return base, "Local rules fallback (configured model unavailable or invalid response)"


def _semantic_similarity(report_text: str, activity_text: str, all_activity_texts: list[str]) -> float:
    report_tokens = _tokens(report_text)
    activity_tokens = _tokens(activity_text)
    if not report_tokens or not activity_tokens:
        return 0.0
    corpus = [_tokens(value) for value in all_activity_texts]
    count = max(1, len(corpus))
    df = Counter(token for document in corpus for token in document)
    weights = {token: math.log(1 + count / (1 + df.get(token, 0))) for token in report_tokens | activity_tokens}
    dot = sum(weights[token] ** 2 for token in report_tokens & activity_tokens)
    norm_a = math.sqrt(sum(weights[token] ** 2 for token in report_tokens))
    norm_b = math.sqrt(sum(weights[token] ** 2 for token in activity_tokens))
    tfidf_cosine = dot / max(1e-9, norm_a * norm_b)
    fuzzy = SequenceMatcher(None, normalize_text(report_text), normalize_text(activity_text)).ratio()
    coverage = len(report_tokens & activity_tokens) / max(1, len(activity_tokens))
    return min(1.0, 0.62 * tfidf_cosine + 0.20 * fuzzy + 0.18 * coverage)


def _match_score(report: dict[str, Any], activity: dict[str, Any], activities: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    raw = str(report.get("text") or "") + " " + str(report.get("discipline") or "") + " " + str(report.get("location") or "")
    aliases = activity.get("aliases") or []
    if isinstance(aliases, str):
        aliases = re.split(r"[,;|]", aliases)
    aliases = [str(alias).strip() for alias in aliases if str(alias).strip()]
    activity_text = " ".join([str(activity.get(key) or "") for key in ("id", "wbs", "name", "discipline", "location")] + aliases)
    report_entities = _entities(raw)
    declared_id = str(report.get("activityId") or report.get("activity_id") or report.get("sourceActivityId") or "").strip().upper()
    activity_id = str(activity.get("id") or "").strip()
    reasons: list[str] = []
    if declared_id and declared_id == activity_id.upper():
        return {"activity_id": activity_id, "score": 0.995, "level": "strong", "score_type": "heuristic", "reasons": ["Exact activity ID supplied by source"]}
    if activity_id and activity_id.upper() in report_entities:
        return {"activity_id": activity_id, "score": 0.995, "level": "strong", "score_type": "heuristic", "reasons": ["Exact L5/L6 activity ID appears in source text"]}

    report_tokens = _tokens(raw)
    activity_texts = []
    for item in activities or [activity]:
        item_aliases = item.get("aliases") or []
        if isinstance(item_aliases, str):
            item_aliases = re.split(r"[,;|]", item_aliases)
        activity_texts.append(" ".join([str(item.get(key) or "") for key in ("id", "wbs", "name", "discipline", "location")] + [" ".join(str(alias) for alias in item_aliases)]))
    semantic = _semantic_similarity(raw, activity_text, activity_texts)
    activity_tokens = _tokens(activity_text)
    shared = report_tokens & activity_tokens
    coverage = len(shared) / max(1, len(activity_tokens))
    report_discipline = detect_discipline(raw, str(report.get("discipline") or ""))
    activity_discipline = str(activity.get("discipline") or "").strip().lower()
    same_discipline = report_discipline.lower() == activity_discipline and report_discipline != "Unassigned"
    report_location = _tokens(str(report.get("location") or "") + " " + raw)
    activity_location = _tokens(str(activity.get("location") or ""))
    location_overlap = len(report_location & activity_location) / max(1, len(activity_location)) if activity_location else 0.0
    activity_entities = _entities(activity_text)
    entity_overlap = report_entities & activity_entities
    entity_score = 1.0 if entity_overlap else 0.0
    verb_terms = {"install", "calibrate", "align", "hydrotest", "inspect", "excavate", "backfill", "weld", "test", "pour"}
    shared_verbs = sorted((report_tokens & activity_tokens) & verb_terms)
    verb_score = 1.0 if shared_verbs else 0.0
    alias_score = max((SequenceMatcher(None, normalize_text(raw), normalize_text(alias)).ratio() for alias in aliases), default=0.0)
    score = 0.58 * semantic + 0.16 * entity_score + 0.10 * float(same_discipline) + 0.08 * location_overlap + 0.05 * verb_score + 0.03 * alias_score
    if entity_overlap and (shared_verbs or len(shared) >= 2) and same_discipline:
        score = max(score, 0.90)
    score = min(0.97, score)
    if entity_overlap:
        reasons.append("Shared equipment/line tag: " + ", ".join(sorted(entity_overlap)))
    if shared:
        reasons.append("Normalized terms: " + ", ".join(sorted(shared)[:8]))
    if same_discipline:
        reasons.append("Same discipline: " + report_discipline)
    if location_overlap:
        reasons.append("Location terms overlap")
    if alias_score >= 0.72:
        reasons.append("Terminology/alias similarity")
    reasons.append(f"Term-normalized TF-IDF and phrase similarity: {semantic:.2f}")
    level = "strong" if score >= 0.88 else "review" if score >= 0.62 else "manual"
    return {"activity_id": activity_id, "score": round(score, 3), "level": level, "score_type": "heuristic", "reasons": reasons or ["No distinctive matching evidence found"]}


def analyze_report(report: dict[str, Any], activities: list[dict[str, Any]], use_llm: bool = False) -> dict[str, Any]:
    facts, provider = extract_facts_with_provider(report) if use_llm else (extract_facts(report), "Local rules fallback (model not used for this view)")
    raw = " ".join((str(report.get("text") or ""), str(report.get("discipline") or ""), str(report.get("location") or "")))
    canonical = canonical_activity(raw, str(report.get("discipline") or ""))
    candidates = [_match_score(report, activity, activities) for activity in activities]
    candidates.sort(key=lambda item: (-item["score"], item["activity_id"]))
    by_id = {str(activity.get("id")): activity for activity in activities}
    evidence = [{"kind": "source", "label": "Submitted source", "value": str(report.get("source") or report.get("sourceType") or "Supervisor text"), "reference": str(report.get("id") or "Draft")}, {"kind": "reporter", "label": "Submitted by", "value": str(report.get("reporter") or "Current signed-in supervisor"), "reference": "authenticated account"}]
    evidence.extend({"kind": "extracted", **item} for item in facts["evidence"])
    candidates = [candidate for candidate in candidates if candidate["score"] >= 0.12][:3]
    for item in candidates:
        activity = by_id.get(item["activity_id"], {})
        item["activity_name"] = str(activity.get("name") or "")
        item["discipline"] = str(activity.get("discipline") or "")
        item["location"] = str(activity.get("location") or "")
    best = candidates[0] if candidates else None
    explicit_source_id = str(report.get("activityId") or report.get("activity_id") or report.get("sourceActivityId") or "")
    match_id = str(explicit_source_id or (best["activity_id"] if best else ""))
    confidence = 0.995 if explicit_source_id and explicit_source_id.upper() == match_id.upper() else (best["score"] if best else 0.0)
    if explicit_source_id and not any(item["activity_id"].upper() == explicit_source_id.upper() for item in candidates):
        confidence = 0.0
        match_id = ""
    return {
        "report_id": str(report.get("id") or "DRAFT"),
        "canonical": canonical,
        "facts": facts,
        "candidates": candidates[:3],
        "suggested_activity_id": match_id if best and best["score"] >= 0.70 else "",
        "confidence": round(confidence, 3),
        "confidence_level": best["level"] if best else "manual",
        "match_reason": best["reasons"] if best else ["No schedule activities are available"],
        "evidence": evidence,
        "approval_required": True,
        "provider": provider,
        "score_type": "heuristic",
    }


def _date_diff(first: str, second: str) -> int | None:
    a, b = _valid_date(first), _valid_date(second)
    if not a or not b:
        return None
    return (date.fromisoformat(b) - date.fromisoformat(a)).days + 1


def _report_target(report: dict[str, Any], match: dict[str, Any], activity_ids: set[str]) -> str:
    explicit = str(report.get("activityId") or report.get("activity_id") or "")
    if explicit in activity_ids:
        return explicit
    if str(report.get("status") or "").lower() in {"unmatched", "rejected"}:
        return ""
    if match.get("confidence", 0) >= 0.70:
        return str(match.get("suggested_activity_id") or "")
    return ""


def _conflicts(reports: list[dict[str, Any]], matches: dict[str, Any], activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    activity_by_id = {str(a.get("id")): a for a in activities}
    activity_ids = set(activity_by_id)
    grouped: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for report in reports:
        if report.get("archived"):
            continue
        match = matches.get(str(report.get("id")), {})
        target = _report_target(report, match, activity_ids)
        report_date = _valid_date(str(report.get("date") or report.get("reportDate") or ""))
        if target and report_date:
            grouped[(target, report_date)].append((report, match.get("facts", {})))
    conflicts: list[dict[str, Any]] = []
    fields = (("progress", "Progress"), ("actualStart", "Actual start"), ("actualEnd", "Actual finish"), ("eventStatus", "Reported status"))
    for (activity_id, report_date), entries in grouped.items():
        if len(entries) < 2:
            continue
        activity = activity_by_id[activity_id]
        for field, label in fields:
            values: list[dict[str, Any]] = []
            for report, facts in entries:
                value = facts.get(field)
                if value in (None, ""):
                    value = report.get(field) if field != "eventStatus" else ""
                if value not in (None, ""):
                    values.append({"value": value, "report_id": str(report.get("id") or ""), "source": str(report.get("source") or "Unknown source"), "reporter": str(report.get("reporter") or "Unknown reporter"), "source_type": str(report.get("sourceType") or "text"), "status": str(report.get("status") or "pending")})
            unique_values = {str(item["value"]).lower() for item in values}
            conflicting = len(unique_values) > 1
            if field == "progress" and len(values) > 1:
                try:
                    conflicting = max(float(item["value"]) for item in values) - min(float(item["value"]) for item in values) >= 10
                except (TypeError, ValueError):
                    pass
            if not conflicting:
                continue
            unresolved = any(item["status"] != "approved" for item in values)
            conflicts.append({"activity_id": activity_id, "activity_name": str(activity.get("name") or ""), "discipline": str(activity.get("discipline") or ""), "report_date": report_date, "field": field, "field_label": label, "values": values, "state": "Planner review required" if unresolved else "Planner reviewed", "unresolved": unresolved})
    return conflicts


def _missing_updates(activities: list[dict[str, Any]], reports: list[dict[str, Any]], matches: dict[str, Any], today: date) -> list[dict[str, Any]]:
    by_id = {str(a.get("id")): a for a in activities}
    activity_ids = set(by_id)
    latest: dict[str, str] = {}
    for report in reports:
        if report.get("archived") or str(report.get("status") or "").lower() != "approved":
            continue
        match = matches.get(str(report.get("id")), {})
        target = _report_target(report, match, activity_ids)
        report_date = _valid_date(str(report.get("date") or report.get("reportDate") or ""))
        if target and report_date and report_date > latest.get(target, ""):
            latest[target] = report_date
    missing: list[dict[str, Any]] = []
    for activity_id, activity in by_id.items():
        progress = int(activity.get("progress") or 0)
        if progress >= 100 or str(activity.get("status") or "").lower() == "complete":
            continue
        start = _valid_date(str(activity.get("plannedStart") or ""))
        if not start or date.fromisoformat(start) > today:
            continue
        cadence = max(24, int(activity.get("updateCadenceHours") or 48))
        last_update = latest.get(activity_id, "")
        anchor = last_update or start
        hours = max(0, (today - date.fromisoformat(anchor)).days * 24)
        if hours < cadence:
            continue
        missing.append({"activity_id": activity_id, "activity_name": str(activity.get("name") or ""), "discipline": str(activity.get("discipline") or ""), "last_update": last_update or "No report received", "missing_hours": hours, "cadence_hours": cadence, "owner": str(activity.get("owner") or "—"), "planned_start": start, "status": str(activity.get("status") or "Not started")})
    return sorted(missing, key=lambda item: (-item["missing_hours"], item["activity_id"]))


def _fingerprints(activities: list[dict[str, Any]], reports: list[dict[str, Any]], matches: dict[str, Any]) -> list[dict[str, Any]]:
    ids = {str(a.get("id")) for a in activities}
    related: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for report in reports:
        if report.get("archived") or str(report.get("status") or "").lower() != "approved":
            continue
        target = _report_target(report, matches.get(str(report.get("id")), {}), ids)
        if target:
            related[target].append(report)
    output: list[dict[str, Any]] = []
    for activity in activities:
        if int(activity.get("progress") or 0) < 100 and str(activity.get("status") or "").lower() != "complete":
            continue
        planned_days = _date_diff(str(activity.get("plannedStart") or ""), str(activity.get("plannedFinish") or ""))
        actual_start = str(activity.get("actualStart") or "")
        actual_finish = str(activity.get("actualFinish") or activity.get("actualEnd") or "")
        actual_days = _date_diff(actual_start, actual_finish)
        if not planned_days or not actual_days:
            continue
        evidence = related.get(str(activity.get("id")), [])
        if not evidence:
            continue
        blockers = [matches.get(str(report.get("id")), {}).get("facts", {}).get("blocker", "") for report in evidence]
        blockers = [cause for cause in blockers if cause]
        output.append({"activity_id": str(activity.get("id")), "activity_name": str(activity.get("name") or ""), "discipline": str(activity.get("discipline") or ""), "contractor": str(activity.get("contractor") or "—"), "planned_start": activity.get("plannedStart") or "", "planned_finish": activity.get("plannedFinish") or "", "actual_start": actual_start, "actual_finish": actual_finish, "planned_days": planned_days, "actual_days": actual_days, "variance_days": actual_days - planned_days, "delay_cause": blockers[0] if blockers else "Not recorded in available evidence", "cause_source": "Reported in source report" if blockers else "No source states a cause", "confidence": 0.98 if evidence else 0.85, "evidence": [{"report_id": str(report.get("id") or ""), "source": str(report.get("source") or ""), "reporter": str(report.get("reporter") or ""), "date": str(report.get("date") or "")} for report in evidence]})
    return sorted(output, key=lambda item: item["actual_finish"], reverse=True)


def _why_chains(activities: list[dict[str, Any]], reports: list[dict[str, Any]], matches: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {str(a.get("id")): a for a in activities}
    children: dict[str, list[str]] = defaultdict(list)
    for activity in activities:
        predecessors = activity.get("predecessors") or []
        if isinstance(predecessors, str):
            predecessors = re.split(r"[,;|]", predecessors)
        for predecessor in predecessors:
            predecessor = str(predecessor).strip()
            if predecessor in by_id:
                children[predecessor].append(str(activity.get("id")))
    output: list[dict[str, Any]] = []
    for report in reports:
        if report.get("archived"):
            continue
        match = matches.get(str(report.get("id")), {})
        cause = str(match.get("facts", {}).get("blocker") or "")
        target = _report_target(report, match, set(by_id))
        if not cause or not target:
            continue
        downstream: list[str] = []
        queue, seen = list(children.get(target, [])), {target}
        while queue and len(downstream) < 12:
            next_id = queue.pop(0)
            if next_id in seen:
                continue
            seen.add(next_id)
            downstream.append(next_id)
            queue.extend(children.get(next_id, []))
        output.append({"cause": cause, "cause_status": "Reported cause", "activity_id": target, "activity_name": str(by_id[target].get("name") or ""), "source": str(report.get("source") or ""), "report_id": str(report.get("id") or ""), "downstream": [{"activity_id": item, "activity_name": str(by_id[item].get("name") or "")} for item in downstream], "downstream_label": "Possible schedule-linked exposure" if downstream else "No predecessor links available; downstream impact not inferred"})
    return output


def _answer_project_memory(question: str, project: dict[str, Any], fingerprints: list[dict], outcomes: list[dict]) -> dict[str, Any] | None:
    """Answer operational questions only from the selected project's stored records."""
    low = str(question or "").casefold()
    activities = project.get("activities", [])
    updates = project.get("approvedUpdates", [])
    analysis = project.get("analysis", {})
    records = project.get("records", [])
    by_id = {str(row.get("id") or ""): row for row in activities}
    causes = analysis.get("causes", [])
    impacts = analysis.get("impacts", [])
    def result(answer: str, basis: str, rows: list[dict]) -> dict:
        evidence_ids: set[str] = set()
        activity_ids: set[str] = set()
        milestone_ids: set[str] = set()
        for row in rows:
            evidence_ids.update(str(v) for v in row.get("evidenceIds", []) if v)
            activity_ids.update(str(row.get(k)) for k in ("activityId", "rootActivityId") if row.get(k))
            milestone_ids.update(str(v) for v in row.get("affectedMilestones", []) if v)
            cause = row.get("cause", {})
            evidence_ids.update(str(v) for v in cause.get("evidenceIds", []) if v)
            if cause.get("activityId"):
                activity_ids.add(str(cause["activityId"]))
            for impact in row.get("impacts", []):
                if impact.get("activityId"):
                    activity_ids.add(str(impact["activityId"]))
                milestone_ids.update(str(v) for v in impact.get("affectedMilestones", []) if v)
        return {"answer": answer, "basis": basis, "sample_count": len(rows), "records": rows,
                "source": "Selected project records", "confidence": "Record-grounded; not a calibrated probability",
                "assumptions": "Only stored project records are summarized; unknown facts remain unknown.",
                "evidence_ids": sorted(evidence_ids), "affected_activity_ids": sorted(activity_ids),
                "affected_milestone_ids": sorted(milestone_ids), "requires_human_review": True}
    area_match = re.search(r"\barea\s+([a-z0-9-]+)\b", low)
    if area_match:
        wanted = area_match.group(1)
        scoped_ids = {aid for aid, row in by_id.items()
                      if wanted in str(row.get("area") or row.get("location") or "").casefold()
                      or wanted in str(row.get("areaId") or "").casefold()}
        causes = [row for row in causes if str(row.get("activityId") or "") in scoped_ids]
        impacts = [row for row in impacts if str(row.get("activityId") or "") in scoped_ids
                   or str(row.get("rootActivityId") or "") in scoped_ids]
    if re.search(r"since yesterday|changed since|what changed", low):
        today = _parse_date(project.get("asOf")) or date.today()
        since = (today - timedelta(days=1)).isoformat()
        recent = [row for row in updates if str(row.get("workDate") or "")[:10] >= since]
        if not recent:
            return result("Insufficient project evidence: no planner-approved daily update is recorded since yesterday.",
                          "Approved daily updates scoped to this project", [])
        lines = [f"{row.get('workDate')}: {row.get('activityId') or 'Project'} — {str(row.get('workCompleted') or row.get('notes') or row.get('issues') or 'approved update')[:180]} (progress {row.get('progress') if row.get('progress') is not None else 'not recorded'}%)."
                 for row in recent]
        return result("Changes since yesterday: " + " ".join(lines), "Planner-approved daily updates", recent)
    if re.search(r"prioriti[sz]|tomorrow|next.shift|next day", low):
        saved = [r for r in records if r.get("kind") == "recommendation" and r.get("status") in {"Proposed", "Approved", "Rejected"}]
        rows = [{"id": r.get("id"), "activityId": r.get("payload", {}).get("activityId"),
                 "title": r.get("payload", {}).get("title"), "action": r.get("payload", {}).get("action"),
                 "priority": r.get("payload", {}).get("priority"), "status": r.get("status"),
                 "sourceRecords": r.get("payload", {}).get("sourceRecords", [])} for r in saved]
        open_rows = [r for r in rows if r.get("status") in {"Proposed", "Approved"}]
        if open_rows:
            text = "; ".join(f"{r.get('activityId') or 'Project'}: {r.get('action') or r.get('title') or 'planner review'} ({r.get('priority') or 'priority not recorded'}, {r.get('status')})" for r in open_rows[:8])
            return result("Stored next-day planner proposals: " + text + ". These are for human review; they do not allocate resources automatically.", "Saved project recommendations", open_rows)
        if causes:
            text = "; ".join(f"{c.get('activityId')}: confirm {c.get('category')} constraint with the owner" for c in causes[:5])
            rows = causes[:5]
            return result("Project evidence supports these review priorities: " + text + ". No recommendation is yet recorded as approved.", "Open risks and approved source observations", rows)
        return result("Insufficient project evidence to rank tomorrow's work. No open recommendation or linked cause is recorded.",
                      "No matching current project decision records", [])
    if re.search(r"approved recovery|what recovery|recovery option|recovery options", low):
        matches = [r for r in records if r.get("kind") in {"scenario", "action"}]
        if re.search(r"approved recovery", low):
            matches = [r for r in matches if r.get("status") in {"Approved", "Completed"}]
        if not matches:
            return result("Insufficient project evidence: no matching saved recovery decision is recorded.", "Saved recovery scenarios and actions", [])
        rows = [{"id": r.get("id"), "kind": r.get("kind"), "status": r.get("status"),
                 "activityId": r.get("payload", {}).get("activityId"),
                 "strategy": r.get("payload", {}).get("strategyLabel") or r.get("payload", {}).get("title"),
                 "decisionBy": r.get("payload", {}).get("decidedBy") or r.get("payload", {}).get("approvedBy"),
                 "reason": r.get("payload", {}).get("reason"), "sourceRecords": r.get("payload", {}).get("sourceRecords", [])}
                for r in matches[:8]]
        text = "; ".join(f"{r.get('activityId')}: {r.get('strategy') or r.get('kind')} ({r.get('status')})" for r in rows)
        return result("Recorded recovery decisions: " + text + ". Approval records describe a decision; they do not prove a measured benefit without an outcome.", "Saved recovery scenario and action records", rows)
    if re.search(r"improv|after the action|what happened after|recovery action|execution action|outcome", low):
        matching = outcomes
        if not matching:
            return result("Insufficient project evidence: no planner-recorded outcome follows a recovery action.", "Recorded execution outcomes for this project", [])
        rows = []
        details = []
        for item in matching[:8]:
            before = item.get("forecastBefore") or []
            after = item.get("forecastAfter") or []
            aid = str(item.get("activityId") or "")
            old = next((f.get("estimatedFinish") for f in before if str(f.get("activityId")) == aid), None)
            new = next((f.get("estimatedFinish") for f in after if str(f.get("activityId")) == aid), None)
            change = "forecast comparison unavailable"
            if old and new:
                delta = (_parse_date(old) - _parse_date(new)).days
                change = f"forecast finish moved {abs(delta)} day(s) {'earlier' if delta > 0 else 'later' if delta < 0 else 'not at all'} ({old} to {new})"
            lesson = str(item.get("lesson") or "").strip()
            lesson_text = f" Lesson recorded: {lesson[:180]}." if lesson else " No lesson was recorded for this outcome."
            details.append(f"{aid or 'Project action'}: {str(item.get('result') or 'outcome recorded')[:180]}; {change}.{lesson_text}")
            rows.append({"id": item.get("id"), "activityId": aid, "result": item.get("result"), "lesson": lesson or None,
                         "actualFinishDate": item.get("actualFinishDate"), "actualDaysSaved": item.get("actualDaysSaved"),
                         "forecastBefore": before, "forecastAfter": after, "sourceRecords": item.get("sourceRecords", [])})
        return result("Planner-recorded outcome history: " + "; ".join(details) + ". A measured outcome is historical evidence, not a guaranteed future effect.",
                      "Recorded action outcomes and saved before/after estimates", rows)
    if re.search(r"similar problem|seen.*before|similar issue", low):
        categories = {str(c.get("category") or "").casefold() for c in causes if c.get("category") not in (None, "Unknown")}
        terms = {"material": ("material", "delivery", "deliver", "fitting", "valve", "spool", "cement", "cable", "stock"),
                 "manpower": ("manpower", "crew", "worker", "staff", "labor", "labour", "operator"),
                 "equipment": ("equipment", "crane", "pump", "tool", "machine", "breakdown"),
                 "dependency": ("predecessor", "dependency", "sequence", "awaiting", "upstream"),
                 "approval": ("approval", "permit", "sign-off", "signoff", "authorization"),
                 "access": ("access", "road", "lane", "scaffold", "entry", "restricted"),
                 "quality": ("quality", "rework", "inspection", "defect", "test failed"),
                 "safety": ("safety", "incident", "unsafe", "confined-space"),
                 "weather": ("weather", "rain", "wind", "storm", "temperature")}
        def historical_categories(value: Any) -> set[str]:
            text = str(value or "").casefold()
            return {category for category, vocabulary in terms.items() if any(term in text for term in vocabulary)}
        matched = [f for f in fingerprints if categories.intersection(historical_categories(f.get("delay_cause")))]
        if not matched:
            return result("Insufficient project evidence: no stored historical execution with the same recorded cause category was found.",
                          "Historical approved execution memory in this project", [])
        return result(f"Found {len(matched)} historical execution record(s) with a matching recorded cause category. This is a category match, not semantic similarity or proof the current cause is the same.",
                      "Historical approved execution memory; category-only match", matched)
    if re.search(r"why|cause|delay|block|affected activity|activities.*affected|milestone.*risk|evidence.*support", low):
        if not causes:
            return result("Insufficient project evidence: no open risk or planner-approved source observation supports a current cause for this question.",
                          "Open project risks and approved daily updates", [])
        rows = []
        lines = []
        for cause in causes[:8]:
            linked = [i for i in impacts if i.get("rootActivityId") == cause.get("activityId") and i.get("causeId") == cause.get("id")]
            affected = sorted({i.get("activityId") for i in linked if i.get("activityId")})
            milestones = sorted({m for i in linked for m in i.get("affectedMilestones", [])})
            lines.append(f"{cause.get('activityId')} ({cause.get('area') or 'area not recorded'}): {cause.get('category')} is {cause.get('causeCertainty', 'Unknown').lower()} from “{str(cause.get('title') or '')[:160]}”; source {', '.join(cause.get('sourceRecords', [])) or 'not recorded'}; evidence {', '.join(cause.get('evidenceIds', [])) or 'none attached'}; linked downstream activities {', '.join(affected) or 'none'}; linked milestones {', '.join(milestones) or 'none recorded'}.")
            rows.append({"cause": cause, "impacts": linked})
        return result("Project-record findings: " + " ".join(lines) + " Causal certainty is shown separately; a terminology category is not a verified root-cause finding.",
                      "Current project risks, approved updates, schedule links, and evidence references", rows)
    return None


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return None


def answer_memory(question: str, intelligence: dict[str, Any]) -> dict[str, Any]:
    low = str(question or "").lower()
    fingerprints = intelligence.get("fingerprints", [])
    outcomes = intelligence.get("execution_outcomes", [])
    category_terms = {
        "piping": ("pipe", "spool", "erect", "weld", "hydrotest"),
        "civil": ("civil", "foundation", "concrete", "backfill", "trench"),
        "electrical": ("electrical", "cable", "tray", "substation"),
        "instrumentation": ("instrument", "transmitter", "calibrat", "loop"),
        "equipment": ("pump", "equipment", "mechanical", "alignment"),
        "hse": ("hse", "safety", "permit"),
    }
    selected_terms = next((terms for category, terms in category_terms.items() if category in low or any(term in low for term in terms)), ())
    project_data = intelligence.get("project_execution")
    project_answer = _answer_project_memory(question, project_data, fingerprints, outcomes) if project_data else None
    if project_answer:
        return project_answer
    if re.search(r"outcome|after the action|what happened after|recovery action|execution action", low):
        matching_outcomes = []
        for item in outcomes:
            searchable = " ".join(str(item.get(key) or "") for key in ("activityId", "result", "actualFinishDate", "sourceUpdateId" )).lower()
            if not selected_terms or any(term in searchable for term in selected_terms):
                matching_outcomes.append(item)
        if not matching_outcomes:
            return {"answer": "No recorded execution-action outcomes match this question yet. Outcomes are added only after a planner-approved action and a measured result.",
                    "basis": "Stored, planner-recorded action outcomes", "sample_count": 0, "records": []}
        details = []
        records = []
        for item in matching_outcomes:
            measured = ("observed finish " + str(item.get("actualFinishDate"))) if item.get("actualFinishDate") else (
                str(item.get("actualDaysSaved")) + " calendar day(s) recovered" if item.get("actualDaysSaved") not in (None, "") else "no numeric measure recorded")
            lesson = str(item.get("lesson") or "").strip()
            lesson_text = f" Lesson recorded: {lesson[:220]}." if lesson else " No lesson was recorded for this outcome."
            details.append(f"{item.get('activityId') or 'Project action'}: {str(item.get('result') or 'Outcome recorded')[:220]} ({measured}).{lesson_text}")
            records.append({"id": item.get("id"), "activity_id": item.get("activityId"), "result": item.get("result"),
                            "lesson": lesson or None,
                            "observed_finish": item.get("actualFinishDate"), "days_recovered": item.get("actualDaysSaved"),
                            "recorded_at": item.get("recordedAt"), "recorded_by": item.get("recordedBy"),
                            "source_records": item.get("sourceRecords", [])})
        return {"answer": f"{len(matching_outcomes)} planner-recorded execution outcome(s): " + "; ".join(details) + ". These are recorded historical results, not guaranteed future effects.",
                "basis": "Approved action and linked planner-recorded outcome history", "sample_count": len(matching_outcomes), "records": records}
    history = [item for item in fingerprints if not selected_terms or any(term in (str(item.get("activity_name", "")) + " " + str(item.get("discipline", ""))).lower() for term in selected_terms)]
    delayed = [item for item in history if int(item.get("variance_days") or 0) > 0]
    causes = Counter(item.get("delay_cause") for item in history if item.get("delay_cause") and item.get("delay_cause") != "Not recorded in available evidence")
    discipline_delay: dict[str, list[int]] = defaultdict(list)
    for item in history:
        discipline_delay[str(item.get("discipline") or "Unassigned")].append(int(item.get("variance_days") or 0))
    if re.search(r"average|duration|how long|similar", low) and not re.search(r"what.?if|plan|benchmark|baseline|forecast|expected", low):
        days = [int(item["actual_days"]) for item in history]
        if days:
            scope = "matching historical activities" if selected_terms else "completed sample executions"
            return {"answer": f"Across {len(days)} {scope}, the observed average duration is {mean(days):.1f} calendar days. This is a historical observation, not a guaranteed estimate.", "basis": "Completed activities with recorded planned and actual dates", "sample_count": len(days), "records": history}
        return {"answer": "There are not enough completed activities with both planned and actual dates to calculate an average yet.", "basis": "No complete duration pairs available", "sample_count": 0, "records": []}
    if re.search(r"what.?if|plan|benchmark|baseline|forecast|expected", low):
        if not history:
            return {"answer": "There are no completed executions matching that activity group yet. No duration benchmark is available.", "basis": "No matching historical fingerprints", "sample_count": 0, "records": []}
        actuals = [int(item["actual_days"]) for item in history]
        plan_days = [int(item["planned_days"]) for item in history]
        common = causes.most_common(3)
        cause_text = "; ".join(f"{cause} ({count})" for cause, count in common) or "no explicit reported cause"
        return {"answer": f"For {len(history)} matching completed execution(s), actual duration ranged {min(actuals)}–{max(actuals)} calendar days (mean {mean(actuals):.1f}); recorded baseline durations ranged {min(plan_days)}–{max(plan_days)} days. Common source-reported causes: {cause_text}. Use this as planning context, not a guaranteed forecast.", "basis": "Observed completed fingerprints; sample may be very small", "sample_count": len(history), "records": history}
    if re.search(r"cause|delay|why|block", low):
        common = causes.most_common(3)
        if common:
            return {"answer": "Reported causes in the available completed history: " + "; ".join(f"{cause} ({count})" for cause, count in common) + ". These are source-reported observations, not verified root-cause findings.", "basis": "Explicit blocker phrases preserved from reports", "sample_count": sum(causes.values()), "records": [{"cause": key, "count": value} for key, value in common]}
        return {"answer": "No explicit delay cause is recorded in the available execution evidence. SiteLink will not infer a cause from schedule variance alone.", "basis": "No source-reported causes found", "sample_count": 0, "records": []}
    if re.search(r"discipline|bottleneck|repeat|exceed", low):
        ranked = sorted(((name, sum(1 for days in values if days > 0), len(values)) for name, values in discipline_delay.items()), key=lambda item: (-item[1], item[0]))
        if ranked:
            return {"answer": "Historical positive-variance counts by discipline: " + "; ".join(f"{name}: {positive}/{total}" for name, positive, total in ranked) + ". Small synthetic samples are not predictive.", "basis": "Recorded fingerprints grouped by discipline", "sample_count": sum(total for _, _, total in ranked), "records": [{"discipline": name, "positive_variance": positive, "executions": total} for name, positive, total in ranked]}
        return {"answer": f"Project memory currently contains {len(fingerprints)} completed execution fingerprint(s), {len(delayed)} with positive duration variance, and {sum(causes.values())} source-reported blocker mention(s). Ask about average duration, reported delay causes, discipline bottlenecks, or planning benchmarks.", "basis": "Current synthetic execution memory", "sample_count": len(fingerprints), "records": fingerprints}


def build_intelligence(activities: list[dict[str, Any]], reports: list[dict[str, Any]], today: date | None = None) -> dict[str, Any]:
    active_reports = [report for report in reports if not report.get("archived")]
    matches = {str(report.get("id") or ""): analyze_report(report, activities) for report in active_reports}
    reviewed_reports = [report for report in active_reports if str(report.get("status") or "").lower() == "approved"]
    current = today or date.today()
    result: dict[str, Any] = {
        "provider": "Local term-normalized matching heuristic",
        "matches": matches,
        "conflicts": _conflicts(active_reports, matches, activities),
        "missing_updates": _missing_updates(activities, active_reports, matches, current),
        "fingerprints": _fingerprints(activities, reviewed_reports, matches),
        "reviewed_report_count": len(reviewed_reports),
    }
    result["why_chains"] = _why_chains(activities, reviewed_reports, matches)
    result["memory_summary"] = answer_memory("summary", result)
    result["rules_version"] = "term-normalized-tfidf-v2"
    return result
