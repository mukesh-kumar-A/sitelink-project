"""Safe, in-memory adapters for SiteLink report and schedule files."""
from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime
from typing import Any

try:
    from defusedxml.common import DefusedXmlException
    from defusedxml.ElementTree import fromstring as _safe_xml_fromstring
except ImportError:  # Keep the core app usable without optional import extras.
    DefusedXmlException = ValueError
    _safe_xml_fromstring = None


MAX_UPLOAD_BYTES = 8 * 1024 * 1024
REPORT_HEADERS = {
    "id": "report_id", "reportid": "report_id", "report_id": "report_id",
    "date": "report_date", "reportdate": "report_date", "report_date": "report_date",
    "eventdate": "event_date", "event_date": "event_date",
    "discipline": "discipline", "text": "text", "report": "text", "description": "text",
    "workdescription": "text", "work_description": "text", "update": "text", "notes": "text",
    "observation": "text", "observations": "text", "reporter": "reporter", "supervisor": "reporter",
    "actualstart": "actual_start", "actual_start": "actual_start", "actualstartdate": "actual_start",
    "actual_start_date": "actual_start", "actualfinish": "actual_end", "actual_end": "actual_end",
    "actualfinishdate": "actual_end", "actual_finish_date": "actual_end", "progress": "progress",
    "actualstarttime": "actual_start_time", "actual_start_time": "actual_start_time",
    "actualfinishtime": "actual_end_time", "actual_end_time": "actual_end_time",
    "percentcomplete": "progress", "percent_complete": "progress", "activityid": "activity_id",
    "activity_id": "activity_id", "activitycode": "activity_id", "location": "location", "linetag": "line_tag",
    "line_tag": "line_tag", "equipmenttag": "equipment_tag", "equipment_tag": "equipment_tag", "source": "source",
}
SCHEDULE_HEADERS = {
    "id": "activity_id", "activityid": "activity_id", "activity_id": "activity_id", "code": "activity_id",
    "text1": "activity_id", "wbs": "wbs", "wbsid": "wbs", "activityname": "activity_name",
    "activity_name": "activity_name", "name": "activity_name", "taskname": "activity_name", "discipline": "discipline",
    "location": "location", "plannedstart": "planned_start", "planned_start": "planned_start", "start": "planned_start",
    "plannedfinish": "planned_finish", "planned_finish": "planned_finish", "finish": "planned_finish",
    "actualstart": "actual_start", "actual_start": "actual_start", "actualfinish": "actual_finish",
    "actual_finish": "actual_finish", "actualend": "actual_finish", "actual_end": "actual_finish",
    "actualstarttime": "actual_start_time", "actual_start_time": "actual_start_time",
    "actualfinishtime": "actual_finish_time", "actual_finish_time": "actual_finish_time",
    "percentcomplete": "percent_complete", "percent_complete": "percent_complete", "progress": "progress",
    "plannedprogress": "planned_progress", "planned_progress": "planned_progress", "owner": "owner",
    "responsible": "owner", "contractor": "contractor", "status": "status", "predecessors": "predecessors",
    "predecessor": "predecessors", "aliases": "aliases", "text2": "discipline", "text3": "location",
    "area": "area", "area_name": "area", "sub_area": "sub_area", "subarea": "sub_area",
    "sub_area_name": "sub_area", "work_package": "work_package", "workpackage": "work_package",
    "planned_quantity": "planned_quantity", "quantity": "planned_quantity", "unit": "unit", "uom": "unit",
    "duration": "duration_days", "duration_days": "duration_days", "weight": "weight", "activity_weight": "weight",
    "milestone": "milestone", "is_milestone": "milestone", "planned_percent_complete": "planned_progress",
}


def _header(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]+", "", str(value or "").strip().lower().replace("-", "_").replace(" ", "_"))


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _rows_csv(data: bytes) -> list[dict[str, str]]:
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return [{_header(k): _cell(v) for k, v in row.items() if k is not None} for row in reader]


def _rows_xlsx(data: bytes) -> list[dict[str, str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 300 or sum(entry.file_size for entry in entries) > 40 * 1024 * 1024:
                raise ValueError("This workbook expands beyond the supported import size.")
            names = set(archive.namelist())
            workbook_xml = _xml_from_bytes(archive.read("xl/workbook.xml"))
            rels_xml = _xml_from_bytes(archive.read("xl/_rels/workbook.xml.rels"))
            workbook_ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
            package_rel_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
            sheet = workbook_xml.find(f"{workbook_ns}sheets/{workbook_ns}sheet")
            if sheet is None:
                return []
            rel_id = sheet.attrib.get(rel_ns + "id", "")
            relation = next((node for node in rels_xml.findall(package_rel_ns + "Relationship") if node.attrib.get("Id") == rel_id), None)
            if relation is None:
                raise ValueError("The XLSX workbook has no active worksheet relationship.")
            target = relation.attrib.get("Target", "")
            sheet_path = target.lstrip("/") if target.startswith("/") else "xl/" + target
            if ".." in sheet_path.split("/") or sheet_path not in names:
                raise ValueError("The XLSX worksheet path is invalid.")
            sheet_xml = _xml_from_bytes(archive.read(sheet_path))
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                shared_xml = _xml_from_bytes(archive.read("xl/sharedStrings.xml"))
                shared = ["".join(node.itertext()) for node in shared_xml.findall(f"{workbook_ns}si")]
            rows: list[list[str]] = []
            for row in sheet_xml.findall(f".//{workbook_ns}sheetData/{workbook_ns}row"):
                values: list[str] = []
                for cell in row.findall(workbook_ns + "c"):
                    ref = cell.attrib.get("r", "")
                    col = 0
                    for char in re.match(r"[A-Z]+", ref.upper()).group(0) if re.match(r"[A-Z]+", ref.upper()) else "":
                        col = col * 26 + ord(char) - 64
                    index = max(0, col - 1)
                    while len(values) <= index:
                        values.append("")
                    kind = cell.attrib.get("t", "")
                    if kind == "inlineStr":
                        value = "".join(cell.itertext())
                    else:
                        raw = cell.findtext(workbook_ns + "v", "")
                        if kind == "s" and raw:
                            try:
                                value = shared[int(raw)]
                            except (ValueError, IndexError):
                                value = ""
                        elif kind == "b":
                            value = "TRUE" if raw == "1" else "FALSE"
                        else:
                            value = raw
                    values[index] = str(value or "").strip()
                rows.append(values)
        if not rows:
            return []
        headers = [_header(value) for value in rows[0]]
        result = []
        for row in rows[1:]:
            mapped = {}
            for key, value in zip(headers, row):
                if not key:
                    continue
                cell = _cell(value)
                if key in {"date", "report_date", "event_date", "actual_start", "actual_finish", "actual_end", "planned_start", "planned_finish"}:
                    try:
                        serial = float(cell)
                        if 1 <= serial <= 100000 and "." not in cell:
                            cell = date(1899, 12, 30).fromordinal(date(1899, 12, 30).toordinal() + int(serial)).isoformat()
                    except (ValueError, OverflowError):
                        pass
                mapped[key] = cell
            if any(value for value in mapped.values()):
                result.append(mapped)
        return result
    except Exception as exc:
        if isinstance(exc, ValueError) and ("expands beyond" in str(exc) or "worksheet path" in str(exc)):
            raise
        raise ValueError("Could not read this XLSX workbook. Save it as a standard .xlsx file and retry.") from exc


def _xml_from_bytes(data: bytes) -> Any:
    # Reject DTD/entity declarations before the stdlib parser sees untrusted XML.
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.I):
        raise ValueError("XML DTD/entity declarations are not supported.")
    if _safe_xml_fromstring:
        return _safe_xml_fromstring(data)
    return ET.fromstring(data)


def _table_rows(filename: str, data: bytes) -> list[dict[str, str]]:
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix == "csv":
        return _rows_csv(data)
    if suffix == "xlsx":
        return _rows_xlsx(data)
    raise ValueError("Choose a CSV or XLSX spreadsheet.")


def _mapped_rows(rows: list[dict[str, str]], aliases: dict[str, str]) -> list[dict[str, str]]:
    result = []
    for row in rows:
        mapped: dict[str, str] = {}
        for key, value in row.items():
            target = aliases.get(_header(key))
            if target and value not in (None, ""):
                mapped[target] = _cell(value)
        result.append(mapped)
    return result


def parse_report_file(filename: str, data: bytes) -> list[dict[str, str]]:
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("Files must be 8 MB or smaller.")
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix == "txt":
        text = data.decode("utf-8-sig", errors="replace").strip()
        return [{"text": text, "report_date": "", "source": filename}] if text else []
    rows = _mapped_rows(_table_rows(filename, data), REPORT_HEADERS)
    result = []
    for row in rows:
        text = row.get("text", "").strip()
        if not text:
            # Keep a human-readable source transcription of the imported columns.
            text = "; ".join(f"{key.replace('_', ' ').title()}: {value}" for key, value in row.items() if value)
        if text:
            row["text"] = text
        if row.get("progress"):
            try:
                progress_value = float(row["progress"].replace("%", ""))
                value = min(100, max(0, progress_value * 100 if 0 < progress_value <= 1 and "." in row["progress"] else progress_value))
                row["progress"] = format(value, ".15g")
            except ValueError:
                row["progress"] = ""
        if text or any(row.get(key) for key in ("report_date", "discipline", "actual_start", "actual_end", "activity_id", "progress")):
            row["source"] = row.get("source") or filename
            result.append(row)
    if not result:
        raise ValueError("No report text or usable structured fields were found in this file.")
    return result


def _split_values(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;|]", value or "") if part.strip()]


def parse_mspdi_xml(data: bytes) -> list[dict[str, Any]]:
    try:
        root = _xml_from_bytes(data)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise ValueError("This is not valid Microsoft Project XML.") from exc

    def tag_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    tasks = [node for node in root.iter() if tag_name(node.tag) == "Task"]
    uid_to_id: dict[str, str] = {}
    raw_tasks = []
    for task in tasks:
        values = {tag_name(child.tag): (child.text or "").strip() for child in task}
        if values.get("Summary", "0").lower() in {"1", "true"} or not values.get("Name"):
            continue
        uid = values.get("UID", "")
        notes_match = re.search(r"SiteLink activity ID:\s*([^\r\n]+)", values.get("Notes", ""), re.I)
        activity_id = values.get("Text1") or (notes_match.group(1).strip() if notes_match else "")
        activity_id = activity_id or values.get("ID", "") or uid
        if uid:
            uid_to_id[uid] = activity_id
        raw_tasks.append((values, activity_id))

    output = []
    for values, activity_id in raw_tasks:
        predecessors = []
        task_node = next((node for node in tasks if any(tag_name(c.tag) == "UID" and (c.text or "").strip() == values.get("UID", "") for c in node)), None)
        if task_node is not None:
            for link in task_node:
                if tag_name(link.tag) == "PredecessorLink":
                    pred_uid = next(((c.text or "").strip() for c in link if tag_name(c.tag) == "PredecessorUID"), "")
                    if pred_uid in uid_to_id:
                        predecessors.append(uid_to_id[pred_uid])
        percent = values.get("PercentComplete", "0") or "0"
        try:
            progress = max(0, min(100, float(percent)))
        except ValueError:
            progress = 0
        output.append({
            "activity_id": activity_id, "wbs": values.get("WBS") or values.get("OutlineNumber", ""),
            "activity_name": values.get("Name", ""), "discipline": values.get("Text2", "Unassigned") or "Unassigned",
            "location": values.get("Text3", ""), "planned_start": _xml_date(values.get("Start", "")),
            "planned_finish": _xml_date(values.get("Finish", "")), "actual_start": _xml_date(values.get("ActualStart", "")),
            "actual_finish": _xml_date(values.get("ActualFinish", "")), "progress": progress,
            "planned_progress": 0, "owner": values.get("ResourceNames", "") or "—", "status": _status(progress, values),
            "predecessors": predecessors, "aliases": [], "external_id": values.get("ID", ""), "source_format": "MS Project XML (MSPDI adapter)",
        })
    return output


def _xml_date(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            return ""


def _status(progress: float, values: dict[str, str]) -> str:
    if progress >= 100 or values.get("ActualFinish"):
        return "Complete"
    if progress > 0 or values.get("ActualStart"):
        return "In progress"
    return "Not started"


def parse_schedule_file(filename: str, data: bytes) -> list[dict[str, Any]]:
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("Files must be 8 MB or smaller.")
    if filename.lower().endswith(".xml"):
        rows = parse_mspdi_xml(data)
    else:
        rows = _mapped_rows(_table_rows(filename, data), SCHEDULE_HEADERS)
        for row in rows:
            row["predecessors"] = _split_values(row.get("predecessors", ""))
            row["aliases"] = _split_values(row.get("aliases", ""))
            row["source_format"] = "CSV/XLSX schedule import"
    result = []
    for row in rows:
        if not row.get("activity_id") or not row.get("activity_name"):
            continue
        for key in ("progress", "planned_progress", "percent_complete"):
            if key in row and row[key] != "":
                try:
                    value = float(row[key].replace("%", ""))
                    if 0 <= value <= 1 and "." in row[key]:
                        value *= 100
                    row["progress" if key == "percent_complete" else key] = min(100, max(0, value))
                except (ValueError, AttributeError):
                    row[key] = 0
        result.append(row)
    if not result:
        raise ValueError("No schedule rows with an activity ID and activity name were found.")
    return result


def export_mspdi_xml(activities: list[dict[str, Any]], project_name: str = "SiteLink Synthetic Project") -> bytes:
    namespace = "http://schemas.microsoft.com/project"
    ET.register_namespace("", namespace)
    q = lambda name: f"{{{namespace}}}{name}"
    root = ET.Element(q("Project"))
    ET.SubElement(root, q("Name")).text = project_name
    tasks = ET.SubElement(root, q("Tasks"))
    uid_by_id = {str(activity.get("id")): str(index) for index, activity in enumerate(activities, start=1)}
    for index, activity in enumerate(activities, start=1):
        task = ET.SubElement(tasks, q("Task"))
        fields = {
            "UID": str(index), "ID": str(index), "Name": str(activity.get("name") or ""),
            "WBS": str(activity.get("wbs") or ""), "OutlineLevel": "1", "Summary": "0",
            "Start": _mspdi_datetime(activity.get("plannedStart")), "Finish": _mspdi_datetime(activity.get("plannedFinish")),
            "ActualStart": _mspdi_datetime(activity.get("actualStart")), "ActualFinish": _mspdi_datetime(activity.get("actualFinish")),
            "PercentComplete": str(int(float(activity.get("progress") or 0))),
            "Text1": str(activity.get("id") or ""), "Text2": str(activity.get("discipline") or ""),
            "Text3": str(activity.get("location") or ""), "Notes": "SiteLink activity ID: " + str(activity.get("id") or ""),
        }
        for name, value in fields.items():
            ET.SubElement(task, q(name)).text = value
        for predecessor in activity.get("predecessors") or []:
            pred_uid = uid_by_id.get(str(predecessor))
            if not pred_uid:
                continue
            link = ET.SubElement(task, q("PredecessorLink"))
            ET.SubElement(link, q("PredecessorUID")).text = pred_uid
            ET.SubElement(link, q("Type")).text = "1"
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _mspdi_datetime(value: Any) -> str:
    if not value:
        return ""
    try:
        return date.fromisoformat(str(value)[:10]).isoformat() + "T08:00:00"
    except ValueError:
        return ""


def extract_ocr(filename: str, data: bytes) -> dict[str, Any]:
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("Scans and photos must be 8 MB or smaller.")
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix == "pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            if len(reader.pages) > 8:
                raise ValueError("Scanned PDFs are limited to 8 pages per upload.")
            text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
            if len(text) >= 30:
                return {"text": text[:20000], "method": "PDF text layer", "source": filename}
            from pdf2image import convert_from_bytes
            images = convert_from_bytes(data, dpi=180, first_page=1, last_page=min(8, len(reader.pages)))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Could not read this PDF. For scanned pages, install Poppler and Tesseract OCR on the server.") from exc
    elif suffix in {"png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"}:
        try:
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = 20_000_000
            image = Image.open(io.BytesIO(data)).convert("RGB")
            images = [image]
        except Exception as exc:
            raise ValueError("Could not open this image. Use PNG, JPEG, TIFF, BMP, or WebP.") from exc
    else:
        raise ValueError("Choose a scanned image or PDF diary.")
    try:
        import pytesseract
        text = "\n".join(pytesseract.image_to_string(image, config="--psm 6") for image in images).strip()
    except Exception as exc:
        raise ValueError("OCR is unavailable. Install Tesseract OCR (and Poppler for scanned PDFs), then restart SiteLink.") from exc
    if not text:
        raise ValueError("OCR found no readable text. Try a sharper, well-lit image or type the update.")
    return {"text": text[:20000], "method": "Tesseract OCR", "source": filename}
