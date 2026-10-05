"""Execution graph, planner governance, and measured outcome integration tests."""
from __future__ import annotations

from datetime import date, timedelta
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server as sitelink
from execution_intelligence import analyze_execution, build_dependency_graph, classify_cause, propose_recommendations, simulate_recovery
from test_workflow_end_to_end import Client


class ExecutionEngineTests(unittest.TestCase):
    def test_graph_propagates_dependency_types_and_flags_cycles(self):
        activities = [
            {"id": "A", "name": "Procurement", "plannedStart": "2026-09-01", "plannedFinish": "2026-09-02", "predecessors": []},
            {"id": "B", "name": "Install", "plannedStart": "2026-09-03", "plannedFinish": "2026-09-04", "predecessors": [{"id": "A", "type": "Start-to-Start", "lagDays": 1}]},
            {"id": "C", "name": "Test", "predecessors": ["B"]},
        ]
        graph = build_dependency_graph(activities)
        self.assertTrue(graph["valid"])
        self.assertEqual(graph["edges"][0], {"from": "A", "to": "B", "type": "Start-to-Start", "lagDays": 1})
        analysis = analyze_execution(activities, [], [{"id": "R1", "activityId": "A", "title": "Material shortfall", "severity": "High", "status": "Open", "payload": {"rootCauseCategory": "Material", "estimatedDelayDays": 2}}], [], "2026-09-30")
        self.assertEqual({item["activityId"] for item in analysis["impacts"]}, {"B", "C"})
        self.assertEqual(next(item for item in analysis["impacts"] if item["activityId"] == "B")["dependencyType"], "Start-to-Start")
        cyclic = build_dependency_graph([{"id": "A", "predecessors": ["B"]}, {"id": "B", "predecessors": ["A"]}])
        self.assertFalse(cyclic["valid"])
        self.assertEqual(cyclic["issues"][0]["type"], "cycle")

    def test_cpm_forward_backward_pass_float_and_critical_path(self):
        activities = [
            {"id": "A", "durationDays": 3, "plannedStart": "2026-09-01", "predecessors": []},
            {"id": "B", "durationDays": 2, "predecessors": [{"id": "A", "type": "FS"}]},
            {"id": "C", "durationDays": 1, "predecessors": []},
        ]
        cpm = build_dependency_graph(activities)["cpm"]
        self.assertEqual(cpm["status"], "calculated")
        self.assertEqual(cpm["criticalPaths"], [["A", "B"]])
        self.assertEqual(cpm["nodes"]["A"]["earlyFinishOffset"], 3)
        self.assertEqual(cpm["nodes"]["B"]["earlyStartOffset"], 3)
        self.assertEqual(cpm["nodes"]["A"]["totalFloatDays"], 0)
        self.assertEqual(cpm["nodes"]["C"]["totalFloatDays"], 4)
        self.assertEqual(cpm["nodes"]["C"]["freeFloatDays"], 4)

    def test_cpm_supports_all_relationship_codes_and_lag(self):
        for relation, expected_start in (("FS", 3), ("SS", 1), ("FF", 0), ("SF", 0)):
            with self.subTest(relation=relation):
                successor_duration = 1 if relation in {"FF", "SF"} else 3
                if relation == "FF":
                    expected_start = 2
                graph = build_dependency_graph([
                    {"id": "A", "durationDays": 2, "plannedStart": "2026-09-01", "predecessors": []},
                    {"id": "B", "durationDays": successor_duration, "predecessors": [{"id": "A", "type": relation, "lagDays": 1}]},
                ])
                self.assertEqual(graph["cpm"]["status"], "calculated")
                self.assertEqual(graph["cpm"]["nodes"]["B"]["earlyStartOffset"], expected_start)

    def test_confirmed_cause_requires_an_explicit_evidence_reference(self):
        activities = [{"id": "A", "name": "Install valve", "durationDays": 2}]
        risks = [
            {"id": "R1", "activityId": "A", "title": "Material shortage", "status": "Open",
             "payload": {"rootCauseCategory": "Material", "rootCauseStatus": "Confirmed", "evidenceIds": ["DOC-1"]}},
            {"id": "R2", "activityId": "A", "title": "Cable delivery pending", "status": "Open",
             "payload": {"rootCauseCategory": "Material", "rootCauseStatus": "Confirmed"}},
        ]
        analysis = analyze_execution(activities, [], risks, [], "2026-09-30")
        self.assertEqual({row["id"]: row["causeCertainty"] for row in analysis["causes"]},
                         {"R1": "Confirmed", "R2": "Inferred"})

    def test_cpm_requires_durations_and_rejects_cycles(self):
        missing = build_dependency_graph([{"id": "A", "predecessors": []}])["cpm"]
        self.assertEqual(missing["status"], "insufficient_data")
        self.assertEqual(missing["missingDurationActivityIds"], ["A"])
        cyclic = build_dependency_graph([
            {"id": "A", "durationDays": 1, "predecessors": ["B"]},
            {"id": "B", "durationDays": 1, "predecessors": ["A"]},
        ])["cpm"]
        self.assertEqual(cyclic["status"], "invalid")

    def test_cpm_milestone_impact_is_calculated_from_linked_schedule(self):
        activities = [
            {"id": "A", "durationDays": 3, "plannedStart": "2026-09-01", "predecessors": []},
            {"id": "B", "durationDays": 3, "predecessors": [{"id": "A", "type": "FS"}]},
        ]
        analysis = analyze_execution(activities, [], [], [{"id": "M1", "name": "Ready for test", "activityId": "B", "plannedDate": "2026-09-05"}], "2026-09-01")
        milestone = analysis["milestoneForecasts"][0]
        self.assertEqual(milestone["cpmEstimatedDate"], "2026-09-06")
        self.assertEqual(milestone["cpmVarianceDays"], 1)
        self.assertTrue(analysis["requiresHumanReview"])

    def test_forecast_and_recovery_do_not_invent_missing_dates_or_history(self):
        activities = [{"id": "A", "name": "Unscheduled work", "progress": 20}]
        analysis = analyze_execution(activities, [{"id": "U1", "activityId": "A", "workDate": "2026-09-29", "progress": 20, "status": "Approved"}], [], [], "2026-09-30")
        self.assertEqual(analysis["activityForecasts"][0]["status"], "Insufficient approved activity history")
        scenario = simulate_recovery(analysis, activities, "A", "manpower", 2, "Crew can be reassigned from another area.", "2026-09-30", "Add two fitters for one shift")
        self.assertIsNone(scenario["scenarioFinish"])
        self.assertEqual(scenario["confidence"], "Insufficient data")
        self.assertEqual(classify_cause("No constraint text found")[0], "Unknown")

    def test_outcome_changes_forecast_estimate_without_changing_schedule_payload(self):
        today = date.today()
        activities = [{"id": "A", "name": "Work", "progress": 40, "plannedFinish": (today + timedelta(days=5)).isoformat(), "actualFinish": ""}]
        before = analyze_execution(activities, [], [], [], today.isoformat())
        after = analyze_execution(activities, [], [], [], today.isoformat(), [{"id": "OUT-1", "activityId": "A", "actualDaysSaved": 2}])
        self.assertNotEqual(before["activityForecasts"][0]["estimatedFinish"], after["activityForecasts"][0]["estimatedFinish"])
        self.assertIn("Outcome-informed", after["activityForecasts"][0]["status"])
        self.assertEqual(activities[0]["actualFinish"], "")

    def test_recovery_uses_approved_productivity_and_workforce_with_explicit_assumptions(self):
        today = date.today()
        activities = [{"id": "A", "name": "Pipe installation", "progress": 40,
                       "plannedFinish": (today + timedelta(days=35)).isoformat(), "area": "Unit A", "subArea": "Rack B4"}]
        updates = [
            {"id": "U1", "activityId": "A", "workDate": (today - timedelta(days=10)).isoformat(), "progress": 20, "manpower": 5, "status": "Approved"},
            {"id": "U2", "activityId": "A", "workDate": today.isoformat(), "progress": 40, "manpower": 5, "status": "Approved"},
        ]
        analysis = analyze_execution(activities, updates, [], [], today.isoformat())
        forecast = analysis["activityForecasts"][0]
        self.assertIn("Observed progress changed", forecast["explanation"])
        self.assertEqual(forecast["currentManpower"], 5)
        scenario = simulate_recovery(analysis, activities, "A", "manpower", 1,
                                     "Two additional fitters follow the same shift pattern.", today.isoformat(),
                                     "Add two fitters for one shift", {"additionalManpower": 2})
        self.assertEqual(scenario["modelInputs"]["remainingProgressPct"], 60)
        self.assertEqual(scenario["modelInputs"]["observedDailyProgressPct"], 2.0)
        self.assertEqual(scenario["derivedDaysSaved"], 8)
        self.assertIn("Linear crew-to-productivity assumption", scenario["modelInputs"]["productivityBasis"])
        self.assertIn("areaInsights", analysis)
        self.assertEqual(analysis["areaInsights"][0]["area"], "Unit A")

    def test_parallel_scenario_only_proposes_a_direct_finish_to_start_overlap(self):
        today = date.today()
        activities = [
            {"id": "A", "name": "Prepare workfront", "progress": 40, "plannedFinish": (today + timedelta(days=4)).isoformat(), "predecessors": []},
            {"id": "B", "name": "Install line", "progress": 0, "plannedFinish": (today + timedelta(days=8)).isoformat(), "predecessors": [{"id": "A", "type": "Finish-to-Start"}]},
            {"id": "C", "name": "Commission", "progress": 0, "plannedFinish": (today + timedelta(days=12)).isoformat(), "predecessors": ["B"]},
        ]
        analysis = analyze_execution(activities, [], [], [], today.isoformat())
        scenario = simulate_recovery(analysis, activities, "A", "parallel", 1,
                                     "Crew has a separate safe workfront and supervisor coverage.", today.isoformat(),
                                     resources={"parallelActivityId": "B"})
        self.assertEqual(scenario["dependencyChanges"][0]["to"], "B")
        self.assertFalse(scenario["dependencyChanges"][0]["scheduleApplied"])
        self.assertTrue(scenario["dependencyChanges"][0]["plannerReviewRequired"])
        with self.assertRaises(ValueError):
            simulate_recovery(analysis, activities, "A", "parallel", 1,
                              "Try to overlap a non-direct successor.", today.isoformat(),
                              resources={"parallelActivityId": "C"})

    def test_next_day_planner_surfaces_unfinished_path_work_without_inventing_a_cause(self):
        today = date.today()
        activities = [
            {"id": "A", "name": "Prepare workfront", "progress": 40,
             "plannedStart": (today - timedelta(days=1)).isoformat(), "plannedFinish": (today + timedelta(days=1)).isoformat(), "predecessors": []},
            {"id": "B", "name": "Install line", "progress": 0,
             "plannedStart": (today + timedelta(days=1)).isoformat(), "plannedFinish": (today + timedelta(days=5)).isoformat(), "predecessors": ["A"]},
        ]
        analysis = analyze_execution(activities, [], [], [], today.isoformat())
        proposals = propose_recommendations(analysis, activities)
        readiness = next(item for item in proposals if item["activityId"] == "A")
        self.assertEqual(readiness["category"], "Readiness")
        self.assertIn("no blocker cause is inferred", readiness["reason"])
        self.assertIn("not calculated", readiness["expectedImpact"])


class ExecutionWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_data_dir, cls.old_db_path = sitelink.DATA_DIR, sitelink.DB_PATH
        cls.temp = tempfile.TemporaryDirectory(prefix="sitelink-execution-intelligence-")
        sitelink.DATA_DIR = Path(cls.temp.name) / "data"
        sitelink.DB_PATH = sitelink.DATA_DIR / "test.sqlite3"
        sitelink.init_db()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), sitelink.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = Client(f"http://127.0.0.1:{cls.httpd.server_address[1]}")
        status, _ = cls.client.request("/api/setup", "POST", {"name": "Execution Planner", "username": "ei-admin@test.local", "password": "Plan1234"}, use_csrf=False)
        if status != 201:
            raise RuntimeError("Could not initialize execution intelligence test workspace")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        sitelink.DATA_DIR, sitelink.DB_PATH = cls.old_data_dir, cls.old_db_path
        cls.temp.cleanup()

    def test_seed_simulate_approve_recommend_execute_outcome_and_audit(self):
        self.assertEqual(self.client.request("/health")[0], 200)
        status, seeded = self.client.request("/api/demo/seed-execution-storyline", "POST", {"projectId": "project-default", "reason": "Load the clearly labeled synthetic judge walkthrough data"})
        self.assertEqual(status, 201, seeded)
        source_id, install_id = seeded["activityIds"][0], seeded["activityIds"][1]
        status, data = self.client.request("/api/execution-intelligence?projectId=project-default")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["demoSeeded"])
        self.assertEqual(len([edge for edge in data["analysis"]["graph"]["edges"] if edge["from"] in seeded["activityIds"]]), 3)
        self.assertTrue(any(cause["category"] == "Material" and cause["activityId"] == source_id for cause in data["analysis"]["causes"]))
        self.assertTrue({install_id, seeded["activityIds"][2], seeded["activityIds"][3]}.issubset({i["activityId"] for i in data["analysis"]["impacts"]}))
        self.assertTrue(data["approvedUpdates"][0]["id"])

        before_status, before = self.client.request("/api/project-control?projectId=project-default")
        self.assertEqual(before_status, 200)
        before_activity = next(a for a in before["activities"] if a["id"] == install_id)
        scenario_status, scenario = self.client.request("/api/recovery-scenarios", "POST", {
            "projectId": "project-default", "activityId": install_id, "strategy": "material", "daysSaved": 1,
            "resourceChange": "Expedite the remaining synthetic V-204 flange kit.",
            "resources": {"materialStatus": "Available", "additionalEquipment": "Synthetic pipe handling support"},
            "assumptions": "Remaining kit arrives before the installation crew starts.",
            "reason": "Compare an explicit synthetic material recovery assumption"
        })
        self.assertEqual(scenario_status, 201, scenario)
        self.assertTrue(scenario["scenario"]["scenarioFinish"])
        self.assertEqual(scenario["scenario"]["modelInputs"]["materialStatus"], "Available")
        self.assertEqual(scenario["scenario"]["modelInputs"]["additionalEquipment"], "Synthetic pipe handling support")
        decision_status, decision = self.client.request(f"/api/recovery-scenarios/{scenario['id']}/decision", "POST", {
            "projectId": "project-default", "decision": "Approved", "reason": "Planner accepts this estimate for field coordination only"
        })
        self.assertEqual(decision_status, 200, decision)
        self.assertTrue(decision["actionId"])
        after = self.client.request("/api/project-control?projectId=project-default")[1]
        after_activity = next(a for a in after["activities"] if a["id"] == install_id)
        self.assertEqual(after_activity["progress"], before_activity["progress"])
        self.assertEqual(after_activity["actualFinish"], before_activity["actualFinish"])
        self.assertEqual(after["currentBaseline"]["version"], before["currentBaseline"]["version"])

        generate_status, generated = self.client.request("/api/recommendations/generate", "POST", {
            "projectId": "project-default", "reason": "Prepare next-shift suggestions from approved synthetic site evidence"
        })
        self.assertEqual(generate_status, 201, generated)
        self.assertGreaterEqual(generated["count"], 1)
        recommendation = generated["created"][0]
        approve_status, approval = self.client.request(f"/api/recommendations/{recommendation['id']}/decision", "POST", {
            "projectId": "project-default", "decision": "approve", "reason": "Approve this proposed task for supervisor coordination"
        })
        self.assertEqual(approve_status, 200, approval)
        action_id = approval["actionId"]

        finish_day = (date.today() + timedelta(days=1)).isoformat()
        missing_lesson_status, missing_lesson = self.client.request(f"/api/execution-actions/{action_id}/outcome", "POST", {
            "projectId": "project-default", "result": "Synthetic supplier confirmed a delivery slot and crew kept the next shift available.",
            "actualFinishDate": finish_day, "sourceUpdateId": "", "reason": "Record the planner-observed synthetic outcome for the audit trail"
        })
        self.assertEqual(missing_lesson_status, 400, missing_lesson)
        outcome_status, outcome = self.client.request(f"/api/execution-actions/{action_id}/outcome", "POST", {
            "projectId": "project-default", "result": "Synthetic supplier confirmed a delivery slot and crew kept the next shift available.",
            "lesson": "Confirm the delivery slot before assigning the next shift.",
            "actualFinishDate": finish_day, "sourceUpdateId": "", "reason": "Record the planner-observed synthetic outcome for the audit trail"
        })
        self.assertEqual(outcome_status, 201, outcome)
        refreshed = self.client.request("/api/execution-intelligence?projectId=project-default")[1]
        forecast = next(f for f in refreshed["analysis"]["activityForecasts"] if f["activityId"] == recommendation["payload"]["activityId"])
        self.assertEqual(forecast["estimatedFinish"], finish_day)
        self.assertIn("Outcome-informed", forecast["status"])
        memory_status, memory_result = self.client.request("/api/memory", "POST", {
            "projectId": "project-default", "question": "What happened after the recovery action?"
        })
        self.assertEqual(memory_status, 200, memory_result)
        self.assertGreaterEqual(memory_result["sample_count"], 1)
        self.assertIn("Synthetic supplier confirmed", memory_result["answer"])
        self.assertIn("Confirm the delivery slot", memory_result["answer"])
        self.assertEqual(memory_result["records"][0]["lesson"], "Confirm the delivery slot before assigning the next shift.")
        state = self.client.request("/api/project-control?projectId=project-default")[1]
        final_activity = next(a for a in state["activities"] if a["id"] == recommendation["payload"]["activityId"])
        self.assertEqual(final_activity["actualFinish"], "", "execution outcome must not bypass daily update approval")
        audit_status, audit_response = self.client.request("/api/audit?projectId=project-default")
        self.assertEqual(audit_status, 200, audit_response)
        audit = audit_response["audit"]
        self.assertTrue(any(row["record"] == outcome["id"] and row["action"] == "Recorded execution action outcome" for row in audit))
        self.assertTrue(state["notifications"], "execution decisions should create project notifications")

    def test_execution_endpoints_keep_project_and_role_boundaries(self):
        status, denied = self.client.request("/api/execution-intelligence?projectId=another-tenants-project")
        self.assertEqual(status, 403, denied)
        created_status, created = self.client.request("/api/users", "POST", {
            "name": "Execution Viewer", "username": "ei-viewer@test.local", "password": "View1234",
            "role": "Viewer", "projectIds": ["project-default"], "reason": "Create a view-only user for execution API permission test"
        })
        self.assertEqual(created_status, 201, created)
        viewer = Client(self.client.origin)
        self.assertEqual(viewer.request("/api/login", "POST", {"username": "ei-viewer@test.local", "password": "View1234"})[0], 200)
        self.assertEqual(viewer.request("/api/execution-intelligence?projectId=project-default")[0], 200)
        denied_status, result = viewer.request("/api/recovery-scenarios", "POST", {
            "projectId": "project-default", "activityId": "PIP-L6-042", "strategy": "manpower", "daysSaved": 1,
            "assumptions": "Viewer tries to create a proposal.", "reason": "This write request must remain forbidden"
        })
        self.assertEqual(denied_status, 403, result)


if __name__ == "__main__":
    unittest.main()
