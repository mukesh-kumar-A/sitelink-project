"""File adapters, evidence-only extraction, and MS Project exchange checks."""
from __future__ import annotations

import io
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from imports import export_mspdi_xml, parse_mspdi_xml, parse_report_file, parse_schedule_file
from intelligence import _validated_llm_facts, extract_facts, extract_facts_with_provider


def minimal_xlsx() -> bytes:
    workbook = b'''<?xml version="1.0" encoding="UTF-8"?>
    <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>
      <sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'''
    rels = b'''<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
    </Relationships>'''
    cells = [
        ["activity_id", "activity_name", "discipline", "location", "planned_start", "planned_finish", "aliases"],
        ["PIP-L6-999", "Hydrotest Line 9-AA", "Piping", "Unit C", "2026-10-01", "2026-10-03", "pressure test; line leak test"],
    ]
    sheet = ['<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
    for row_number, row in enumerate(cells, 1):
        sheet.append(f'<row r="{row_number}">')
        for col_number, value in enumerate(row, 1):
            n, letters = col_number, ""
            while n:
                n, rem = divmod(n - 1, 26)
                letters = chr(65 + rem) + letters
            sheet.append(f'<c r="{letters}{row_number}" t="inlineStr"><is><t>{value}</t></is></c>')
        sheet.append("</row>")
    sheet.append("</sheetData></worksheet>")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", "".join(sheet))
    return buffer.getvalue()


class FileAdapterTests(unittest.TestCase):
    def test_report_csv_maps_source_fields_without_guessing_event_date(self):
        source = b"report_id,report_date,discipline,text,location,activity_id,event_date,progress\nR-1,2026-09-30,Piping,Line 9-AA weld started,Unit C,PIP-L6-999,,65\n"
        rows = parse_report_file("daily.csv", source)
        self.assertEqual(rows[0]["report_id"], "R-1")
        self.assertEqual(rows[0]["activity_id"], "PIP-L6-999")
        self.assertEqual(rows[0]["progress"], "65")
        self.assertEqual(rows[0].get("event_date", ""), "")

    def test_xlsx_schedule_headers_and_aliases(self):
        rows = parse_schedule_file("schedule.xlsx", minimal_xlsx())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["activity_id"], "PIP-L6-999")
        self.assertEqual(rows[0]["planned_start"], "2026-10-01")
        self.assertEqual(rows[0]["aliases"], ["pressure test", "line leak test"])

    def test_mspdi_export_import_round_trip_preserves_sitelink_activity_id(self):
        activities = [{"id": "PIP-L6-999", "name": "Hydrotest Line 9-AA", "wbs": "2.1",
                       "discipline": "Piping", "location": "Unit C", "plannedStart": "2026-10-01",
                       "plannedFinish": "2026-10-03", "actualStart": "", "actualFinish": "",
                       "progress": 0, "predecessors": []}]
        rows = parse_mspdi_xml(export_mspdi_xml(activities, "Synthetic Demo"))
        self.assertEqual(rows[0]["activity_id"], "PIP-L6-999")
        self.assertEqual(rows[0]["activity_name"], "Hydrotest Line 9-AA")
        self.assertEqual(rows[0]["planned_finish"], "2026-10-03")

    def test_xml_dtd_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_mspdi_xml(b'<!DOCTYPE Project [<!ENTITY x "bad">]><Project/>')

    def test_ambiguous_slash_date_and_missing_year_are_not_guessed(self):
        self.assertEqual(extract_facts({"date": "2026-09-30", "text": "Work started on 09/10/2026."})["eventDate"], "")
        self.assertEqual(extract_facts({"date": "2026-09-30", "text": "Work started on 30 Sep."})["eventDate"], "")
        unrelated = extract_facts({"date": "2026-09-30", "text": "Work started. The previous milestone was 2026-09-28."})
        self.assertEqual(unrelated["eventDate"], "")
        self.assertEqual(unrelated["actualStart"], "")
        multiple = extract_facts({"date": "2026-09-30", "text": "Started on 2026-09-28 and finished on 2026-09-30."})
        self.assertEqual(multiple["eventDate"], "")

    def test_supervisor_confirmed_event_date_and_progress_are_structured_evidence(self):
        facts = extract_facts({"date": "2026-09-30", "eventDate": "2026-09-29", "progress": 55, "text": "Pipe spool erection started."})
        self.assertEqual(facts["actualStart"], "2026-09-29")
        self.assertEqual(facts["progress"], 55)
        self.assertTrue(any(item["field"] == "actualStart" for item in facts["evidence"]))
        self.assertTrue(any(item["field"] == "progress" and item["method"] == "supervisor-confirmed structured field" for item in facts["evidence"]))

    def test_model_facts_require_source_quote_validation_and_fallback_is_clear(self):
        report = {"text": "Piping spool progress is 65% today.", "date": "2026-09-30"}
        base = extract_facts(report)
        proposed = {"evidence": [{"field": "progress", "value": "93", "quote": "65%"},
                                 {"field": "blocker", "value": "missing crane", "quote": "missing crane"}]}
        checked = _validated_llm_facts(report, proposed, base)
        self.assertEqual(checked["progress"], 65)
        self.assertEqual(checked["blocker"], "")
        self.assertEqual(extract_facts_with_provider(report)[1], "Local rules fallback (model not configured)")

    def test_configured_time_agent_uses_structured_provider_and_keeps_validated_evidence(self):
        report = {"text": "Pipe spool erection is 65% complete on 2026-09-30.", "date": "2026-09-30", "discipline": "Piping"}
        proposed = {"evidence": [{"field": "progress", "value": 65, "quote": "65%"}]}
        with patch.dict("os.environ", {"TIME_AGENT_ENABLED": "1", "OPENAI_API_KEY": "test-only", "TIME_AGENT_MODEL": "mock-model"}):
            with patch("intelligence._llm_json", return_value=proposed) as model_call:
                facts, provider = extract_facts_with_provider(report)
        model_call.assert_called_once_with(report)
        self.assertIn("OpenAI Responses API", provider)
        self.assertTrue(any(item["method"].startswith("LLM extraction") for item in facts["evidence"]))


if __name__ == "__main__":
    unittest.main()
