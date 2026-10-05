"""Approved baseline and daily execution controls stay review-gated and auditable."""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server as sitelink
from project_control import activity_condition, weighted_progress
from test_workflow_end_to_end import Client


class ProjectControlCalculations(unittest.TestCase):
    def test_weighted_progress_uses_configured_weights_and_explains_method(self):
        result = weighted_progress([{"weight": 3, "progress": 50, "plannedProgress": 60},
                                    {"weight": 1, "progress": 100, "plannedProgress": 100}])
        self.assertEqual(result["actual"], 62.5)
        self.assertEqual(result["planned"], 70.0)
        self.assertEqual(result["weighting"], "Configured activity weights")

    def test_activity_condition_reports_high_severity_blocker(self):
        value = activity_condition({"progress": 20, "plannedFinish": "2026-09-29"}, "2026-09-30",
                                   [{"status": "Open", "severity": "High", "payload": {"type": "blocker"}}])
        self.assertEqual(value["status"], "Blocked")
        self.assertTrue(value["reasons"])


class ProjectControlWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_data_dir, cls.old_db_path = sitelink.DATA_DIR, sitelink.DB_PATH
        cls.temp = tempfile.TemporaryDirectory(prefix="sitelink-control-")
        sitelink.DATA_DIR = Path(cls.temp.name) / "data"
        sitelink.DB_PATH = sitelink.DATA_DIR / "test.sqlite3"
        sitelink.init_db()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), sitelink.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = Client(f"http://127.0.0.1:{cls.httpd.server_address[1]}")
        status, _ = cls.client.request("/api/setup", "POST", {
            "name": "Synthetic Planner", "username": "planner@test.local", "password": "Plan1234"
        }, use_csrf=False)
        if status != 201:
            raise RuntimeError("Could not initialize project control workflow test")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close(); cls.thread.join(timeout=5)
        sitelink.DATA_DIR, sitelink.DB_PATH = cls.old_data_dir, cls.old_db_path
        cls.temp.cleanup()

    def test_import_review_approve_start_event_and_audit(self):
        project = "project-default"
        status, before = self.client.request("/api/project-control?projectId=" + project)
        self.assertEqual(status, 200, before)
        initial_count = len(before["activities"])
        initial_baselines = len(before["baselines"])
        schedule = ("activity_id,activity_name,discipline,location,planned_start,planned_finish,planned_quantity,unit,weight\n"
                    "SYN-L6-001,Install synthetic cooling-water spool,Piping,Unit Test CW-01,2026-10-01,2026-10-03,10,m,2\n").encode()
        status, uploaded = self.client.request("/api/plans", "POST", {
            "projectId": project, "fileName": "synthetic-baseline.csv",
            "fileBase64": base64.b64encode(schedule).decode(),
            "reason": "Import synthetic schedule for the project control workflow test"
        })
        self.assertEqual(status, 201, uploaded)
        plan_id = uploaded["plan"]["id"]
        status, extracted = self.client.request(f"/api/plans/{plan_id}/analyze", "POST", {"projectId": project})
        self.assertEqual(status, 200, extracted)
        self.assertEqual(extracted["activities"][0]["id"], "SYN-L6-001")
        status, unchanged = self.client.request("/api/project-control?projectId=" + project)
        self.assertEqual(len(unchanged["activities"]), initial_count)
        self.assertEqual(len(unchanged["baselines"]), initial_baselines, "analysis must not approve another baseline")

        status, approved_plan = self.client.request(f"/api/plans/{plan_id}/approve", "POST", {
            "projectId": project, "activities": extracted["activities"],
            "reason": "Planner checked source values and approved the synthetic baseline"
        })
        self.assertEqual(status, 200, approved_plan)
        self.assertEqual(approved_plan["baselineVersion"], initial_baselines + 1)
        activity = next(a for a in approved_plan["activities"] if a["id"] == "SYN-L6-001")
        self.assertEqual(activity["progress"], 0)

        status, area = self.client.request("/api/areas", "POST", {
            "projectId": project, "name": "Unit Test Zone", "level": "area",
            "reason": "Create a synthetic area for the workflow test"
        })
        self.assertEqual(status, 201, area)
        status, risk = self.client.request("/api/risks", "POST", {
            "projectId": project, "activityId": "SYN-L6-001", "areaId": area["id"],
            "title": "Synthetic access constraint", "description": "Access lane is temporarily obstructed.",
            "severity": "Medium", "probability": "Low", "mitigation": "Clear the lane before shift change.",
            "reason": "Record a synthetic issue against the test activity"
        })
        self.assertEqual(status, 201, risk)
        status, changed_risk = self.client.request("/api/risks/" + risk["id"], "PATCH", {
            "projectId": project, "status": "Monitoring", "reason": "Track the access constraint until site confirmation"
        })
        self.assertEqual(status, 200, changed_risk)

        status, clarification = self.client.request("/api/daily-updates/analyze", "POST", {
            "projectId": project, "text": "Work started on the cooling-water spool."
        })
        self.assertEqual(status, 200, clarification)
        self.assertEqual(clarification["facts"]["actualStart"], "")
        status, missing_date = self.client.request("/api/daily-updates", "POST", {
            "projectId": project, "activityId": "SYN-L6-001", "workCompleted": "Work started.",
            "eventStatus": "started", "progress": 10
        })
        self.assertEqual(status, 400)
        self.assertIn("work date", missing_date["error"].lower())

        status, submitted = self.client.request("/api/daily-updates", "POST", {
            "projectId": project, "activityId": "SYN-L6-001", "workDate": "2026-09-30",
            "workCompleted": "Supervisor confirmed that cooling-water spool fit-up started at 08:15.",
            "eventStatus": "started", "eventTime": "08:15", "progress": 20,
            "manpower": 4, "materialAvailability": "Available", "attachments": []
        })
        self.assertEqual(status, 201, submitted)
        update_id = submitted["update"]["id"]
        status, pending = self.client.request("/api/project-control?projectId=" + project)
        pending_activity = next(a for a in pending["activities"] if a["id"] == "SYN-L6-001")
        self.assertEqual(pending_activity["progress"], 0)
        self.assertEqual(next(u for u in pending["dailyUpdates"] if u["id"] == update_id)["status"], "Submitted")

        status, invalid = self.client.request(f"/api/daily-updates/{update_id}/review", "POST", {
            "projectId": project, "status": "Approved", "reason": "short"
        })
        self.assertEqual(status, 400)
        status, reviewed = self.client.request(f"/api/daily-updates/{update_id}/review", "POST", {
            "projectId": project, "status": "Approved", "reason": "Verified the supervisor report and source event date"
        })
        self.assertEqual(status, 200, reviewed)
        self.assertEqual(reviewed["activity"]["progress"], 20)
        self.assertEqual(reviewed["activity"]["actualStart"], "2026-09-30")
        self.assertEqual(reviewed["activity"]["actualStartTime"], "08:15")
        self.assertEqual(reviewed["update"]["reviewedBy"], "Synthetic Planner")
        self.assertTrue(reviewed["update"]["reviewedAt"])
        status, audit = self.client.request("/api/audit?projectId=" + project)
        self.assertEqual(status, 200)
        decision = next(a for a in audit["audit"] if a["record"] == update_id and a["action"] == "Approved daily work update")
        self.assertEqual(decision["after"]["activity"]["actualStart"], "2026-09-30")
        self.assertEqual(decision["after"]["update"]["reviewedBy"], "Synthetic Planner")

        status, finish = self.client.request("/api/daily-updates", "POST", {
            "projectId": project, "activityId": "SYN-L6-001", "workDate": "2026-10-03",
            "workCompleted": "Synthetic cooling water spool fit-up and checks finished.",
            "eventStatus": "completed", "eventTime": "16:40", "progress": 100,
            "manpower": 3, "attachments": []
        })
        self.assertEqual(status, 201, finish)
        status, finish_decision = self.client.request(f"/api/daily-updates/{finish['update']['id']}/review", "POST", {
            "projectId": project, "status": "Approved", "reason": "Verified the synthetic completion report and work date"
        })
        self.assertEqual(status, 200, finish_decision)
        self.assertEqual(finish_decision["activity"]["actualFinish"], "2026-10-03")
        self.assertEqual(finish_decision["activity"]["actualFinishTime"], "16:40")


if __name__ == "__main__":
    unittest.main()
