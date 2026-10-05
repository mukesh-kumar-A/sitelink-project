import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from intelligence import analyze_report, answer_memory, build_intelligence, extract_facts


class SemanticCaptureTests(unittest.TestCase):
    def setUp(self):
        self.activities = [
            {
                "id": "PIP-L6-042", "name": "Erect Line 24-XX pipe spools",
                "discipline": "Piping", "location": "Unit B - Line 24-XX",
                "plannedStart": "2026-09-25", "plannedFinish": "2026-09-30",
                "actualStart": "2026-09-26", "actualFinish": "", "progress": 65,
                "status": "In progress", "owner": "Piping crew 2", "contractor": "ABC",
                "predecessors": [],
            },
            {
                "id": "PIP-L6-043", "name": "Weld Line 24-XX erected spools",
                "discipline": "Piping", "location": "Unit B - Line 24-XX",
                "plannedStart": "2026-10-01", "plannedFinish": "2026-10-04",
                "actualStart": "", "actualFinish": "", "progress": 0,
                "status": "Not started", "owner": "Welding crew", "contractor": "ABC",
                "predecessors": ["PIP-L6-042"],
            },
        ]

    def test_cross_discipline_wording_matches_schedule_terms(self):
        report = {"id": "R1", "date": "2026-09-29", "discipline": "Piping",
                  "text": "Line 24-XX spool erection finished; installation progress 80%."}
        result = analyze_report(report, self.activities)
        self.assertEqual(result["suggested_activity_id"], "PIP-L6-042")
        self.assertGreaterEqual(result["confidence"], 0.90)
        self.assertTrue(result["match_reason"])

    def test_no_event_date_or_time_is_invented(self):
        facts = extract_facts({"date": "2026-09-29", "text": "Work started; crew mobilized."})
        self.assertEqual(facts["actualStart"], "")
        self.assertEqual(facts["startTime"], "")

    def test_explicit_relative_date_and_clock_time_are_extracted_with_evidence(self):
        facts = extract_facts({"date": "2026-09-29", "discipline": "Piping",
                               "text": "Line 24-XX erection started at 8:30 this morning."})
        self.assertEqual(facts["actualStart"], "2026-09-29")
        self.assertEqual(facts["startTime"], "08:30")
        self.assertTrue(any(item["field"] == "time" for item in facts["evidence"]))

    def test_explicit_blocker_is_preserved(self):
        facts = extract_facts({"date": "2026-09-29", "text": "Crew waiting on carbon steel fittings from stores."})
        self.assertEqual(facts["blocker"], "carbon steel fittings from stores")

    def test_explicit_24_hour_finish_time_is_extracted(self):
        facts = extract_facts({"date": "2026-09-29", "text": "Line 24-XX activity finished at 17:00 today."})
        self.assertEqual(facts["endTime"], "17:00")
        self.assertEqual(facts["actualEnd"], "2026-09-29")

    def test_partial_percent_complete_is_not_misclassified_as_finished(self):
        facts = extract_facts({"text": "Activity is 65% complete; fit-up checks continue."})
        self.assertEqual(facts["progress"], 65)
        self.assertNotEqual(facts["eventStatus"], "complete")


class ExecutionIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.activities = [
            {"id": "PIP-L6-042", "name": "Erect Line 24-XX pipe spools", "discipline": "Piping",
             "location": "Unit B - Line 24-XX", "plannedStart": "2026-09-25", "plannedFinish": "2026-09-30",
             "actualStart": "2026-09-26", "actualFinish": "2026-09-29", "progress": 100,
             "status": "Complete", "owner": "Piping crew", "contractor": "ABC",
             "predecessors": [], "updateCadenceHours": 48},
            {"id": "PIP-L6-043", "name": "Weld Line 24-XX erected spools", "discipline": "Piping",
             "location": "Unit B - Line 24-XX", "plannedStart": "2026-10-01", "plannedFinish": "2026-10-04",
             "actualStart": "", "actualFinish": "", "progress": 0, "status": "Not started",
             "owner": "Welding crew", "contractor": "ABC", "predecessors": ["PIP-L6-042"],
             "updateCadenceHours": 48},
        ]
        self.reports = [
            {"id": "R1", "date": "2026-09-29", "discipline": "Piping", "activityId": "PIP-L6-042",
             "text": "Spool erection complete at 100%. Waiting on carbon steel fittings from stores.",
             "progress": 100, "status": "approved", "actualStart": "2026-09-26", "actualEnd": "2026-09-29",
             "source": "Daily report.csv", "reporter": "Supervisor"},
            {"id": "R2", "date": "2026-09-29", "discipline": "Piping", "activityId": "PIP-L6-042",
             "text": "Contractor records this activity at 80% complete.", "progress": 80,
             "status": "pending", "source": "Contractor sheet.csv", "reporter": "Contractor"},
        ]

    def test_progress_disagreement_is_flagged_for_review(self):
        result = build_intelligence(self.activities, self.reports, date(2026, 9, 29))
        conflicts = [item for item in result["conflicts"] if item["field"] == "progress"]
        self.assertEqual(len(conflicts), 1)
        self.assertTrue(conflicts[0]["unresolved"])
        self.assertEqual({item["value"] for item in conflicts[0]["values"]}, {80, 100})

    def test_unmatched_reports_do_not_hide_missing_updates(self):
        reports = [{"id": "U1", "date": "2026-09-29", "text": "Unmatched work", "status": "unmatched"}]
        result = build_intelligence(self.activities[1:], reports, date(2026, 10, 4))
        self.assertEqual([item["activity_id"] for item in result["missing_updates"]], ["PIP-L6-043"])

    def test_completed_activity_generates_duration_fingerprint_and_reported_why_chain(self):
        result = build_intelligence(self.activities, self.reports, date(2026, 9, 29))
        fingerprint = next(item for item in result["fingerprints"] if item["activity_id"] == "PIP-L6-042")
        self.assertEqual(fingerprint["planned_days"], 6)
        self.assertEqual(fingerprint["actual_days"], 4)
        self.assertEqual(fingerprint["variance_days"], -2)
        self.assertIn("fittings", fingerprint["delay_cause"])
        self.assertEqual(result["why_chains"][0]["downstream"][0]["activity_id"], "PIP-L6-043")
        self.assertIn("Possible", result["why_chains"][0]["downstream_label"])

    def test_memory_reports_historical_observations_not_predictions(self):
        result = build_intelligence(self.activities, self.reports, date(2026, 9, 29))
        answer = answer_memory("What was the average actual duration?", result)
        self.assertIn("historical observation", answer["answer"])
        self.assertEqual(answer["sample_count"], 1)

    def test_memory_includes_planner_recorded_execution_outcomes(self):
        memory = {"fingerprints": [], "execution_outcomes": [{
            "id": "OUT-7", "activityId": "PIP-L6-042", "result": "Two fitters joined after the valve kit arrived.",
            "lesson": "Confirm material availability before adding a crew.",
            "actualDaysSaved": 2, "recordedAt": "2026-09-30T10:00:00+00:00", "recordedBy": "Planner",
            "sourceRecords": ["upd-7"],
        }]}
        answer = answer_memory("What happened after the recovery action?", memory)
        self.assertEqual(answer["sample_count"], 1)
        self.assertIn("2 calendar day(s) recovered", answer["answer"])
        self.assertIn("Confirm material availability", answer["answer"])
        self.assertEqual(answer["records"][0]["id"], "OUT-7")
        self.assertEqual(answer["records"][0]["lesson"], "Confirm material availability before adding a crew.")
        empty = answer_memory("What happened after the recovery action?", {"fingerprints": [], "execution_outcomes": []})
        self.assertEqual(empty["sample_count"], 0)

    def test_project_assistant_uses_scoped_causes_evidence_and_impact(self):
        memory = {"fingerprints": [], "execution_outcomes": [], "project_execution": {
            "asOf": "2026-09-30",
            "activities": [{"id": "PIP-1", "area": "Area A", "name": "Install pipe"},
                           {"id": "ELE-1", "area": "Area B", "name": "Cable tray"}],
            "approvedUpdates": [], "records": [],
            "analysis": {"causes": [{"id": "R-1", "activityId": "PIP-1", "category": "Material",
                "causeCertainty": "Inferred", "title": "Valve kit is short", "area": "Area A",
                "sourceRecords": ["R-1"], "evidenceIds": ["DOC-1"]}],
                "impacts": [{"causeId": "R-1", "rootActivityId": "PIP-1", "activityId": "PIP-2",
                    "affectedMilestones": ["M-1"]}]}}}
        answer = answer_memory("Why is Area A delayed and what evidence supports it?", memory)
        self.assertIn("PIP-1", answer["answer"])
        self.assertIn("DOC-1", answer["answer"])
        self.assertIn("M-1", answer["answer"])
        self.assertIn("inferred", answer["answer"])
        self.assertEqual(answer["source"], "Selected project records")
        self.assertEqual(answer["evidence_ids"], ["DOC-1"])
        self.assertIn("PIP-2", answer["affected_activity_ids"])
        self.assertEqual(answer["affected_milestone_ids"], ["M-1"])
        self.assertTrue(answer["requires_human_review"])

    def test_project_assistant_reports_insufficient_data_instead_of_guessing(self):
        answer = answer_memory("Why is Area A delayed?", {"project_execution": {
            "activities": [{"id": "A", "area": "Area A"}], "approvedUpdates": [], "records": [],
            "analysis": {"causes": [], "impacts": []}}})
        self.assertIn("Insufficient project evidence", answer["answer"])

    def test_project_memory_similar_case_is_labeled_as_category_match(self):
        answer = answer_memory("Have we seen a similar problem before?", {
            "fingerprints": [{"activity_name": "Install line", "delay_cause": "Valve fittings were not in stock"}],
            "project_execution": {"activities": [], "approvedUpdates": [], "records": [],
                "analysis": {"causes": [{"id": "R1", "category": "Material"}], "impacts": []}}})
        self.assertEqual(answer["sample_count"], 1)
        self.assertIn("category match", answer["answer"])


if __name__ == "__main__":
    unittest.main()
