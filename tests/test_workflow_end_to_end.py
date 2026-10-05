"""Authenticated report import, planner review, rejection, and audit trail."""
from __future__ import annotations

import base64
import http.cookiejar
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server as sitelink


class Client:
    def __init__(self, origin: str):
        self.origin = origin
        self.cookies = http.cookiejar.CookieJar()
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.csrf = ""

    def request(self, path: str, method: str = "GET", data: dict | None = None, use_csrf: bool = True):
        headers = {"Accept": "application/json"}
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode()
        if method != "GET" and use_csrf and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        request = urllib.request.Request(self.origin + path, data=body, headers=headers, method=method)
        try:
            response = self.http.open(request, timeout=10)
            status, payload = response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, json.loads(exc.read().decode())
        self.csrf = payload.get("csrf", self.csrf)
        return status, payload


class EndToEndWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_data_dir, cls.old_db_path = sitelink.DATA_DIR, sitelink.DB_PATH
        cls.temp = tempfile.TemporaryDirectory(prefix="sitelink-e2e-")
        sitelink.DATA_DIR = Path(cls.temp.name) / "data"
        sitelink.DB_PATH = sitelink.DATA_DIR / "test.sqlite3"
        sitelink.init_db()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), sitelink.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = Client(f"http://127.0.0.1:{cls.httpd.server_address[1]}")
        status, _ = cls.client.request("/api/setup", "POST", {"name": "Synthetic Planner", "username": "planner@demo.local", "password": "Plan1234"}, use_csrf=False)
        if status != 201:
            raise RuntimeError("Could not initialize isolated workflow test workspace")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        sitelink.DATA_DIR, sitelink.DB_PATH = cls.old_data_dir, cls.old_db_path
        cls.temp.cleanup()

    def state(self):
        status, data = self.client.request("/api/state")
        self.assertEqual(status, 200)
        return data

    def add_report(self, report_id: str, text: str, status: str = "pending"):
        state = self.state()
        report = {"id": report_id, "date": "2026-09-30", "discipline": "Piping", "text": text,
                  "reporter": "Synthetic supervisor", "source": "Synthetic daily diary", "sourceType": "text",
                  "actualStart": "", "actualEnd": "", "actualStartTime": "", "actualEndTime": "",
                  "progress": "", "status": status, "activityId": "", "sourceActivityId": "PIP-L6-042",
                  "suggestedActivityId": "PIP-L6-042", "note": ""}
        payload = {"activities": state["activities"], "reports": state["reports"] + [report],
                   "reason": "Add synthetic test report", "revision": state["revision"]}
        code, result = self.client.request("/api/state", "POST", payload)
        self.assertEqual(code, 200, result)
        return result

    def test_csv_import_pending_approval_schedule_audit_and_rejection(self):
        status, analysis = self.client.request("/api/analyze", "POST", {"projectId": "project-default", "text": "Line 24-XX spool erection started.", "date": "2026-09-30", "eventDate": "2026-09-29", "progress": 65, "discipline": "Piping"})
        self.assertEqual(status, 200, analysis)
        self.assertEqual(analysis["facts"]["actualStart"], "2026-09-29")
        self.assertEqual(analysis["facts"]["progress"], 65)
        status, invalid = self.client.request("/api/analyze", "POST", {"projectId": "project-default", "text": "Invalid progress test", "progress": 101})
        self.assertEqual(status, 400)
        self.assertIn("0 to 100", invalid["error"])

        data = b"report_id,report_date,discipline,text,reporter,activity_id\nRPT-CSV-01,2026-09-30,Piping,Line 24-XX pipe spool erection started at 08:15 on 2026-09-30; progress 77%,Synthetic supervisor,PIP-L6-042\n"
        status, parsed = self.client.request("/api/import/parse", "POST", {"kind": "report", "fileName": "daily.csv", "projectId": "project-default", "fileBase64": base64.b64encode(data).decode()})
        self.assertEqual(status, 200, parsed)
        self.assertEqual(parsed["rows"][0]["activity_id"], "PIP-L6-042")

        result = self.add_report("RPT-WF-APPROVE", "Line 24-XX spool erection started at 08:15 on 2026-09-30; progress is 77%.")
        before = next(a for a in result["activities"] if a["id"] == "PIP-L6-042")
        self.assertEqual(before["actualStart"], "2026-09-26")
        pending = next(r for r in result["reports"] if r["id"] == "RPT-WF-APPROVE")
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["activityId"], "")
        self.assertTrue(any(item["field"] == "actualStart" for item in pending["extraction"]["evidence"]))

        pending.update(status="approved", activityId="PIP-L6-042", actualStart="2026-09-30", actualStartTime="08:15", progress=77)
        status, approved_state = self.client.request("/api/state", "POST", {"activities": result["activities"], "reports": result["reports"], "reason": "Approve confirmed event and progress", "revision": result["revision"]})
        self.assertEqual(status, 200, approved_state)
        activity = next(a for a in approved_state["activities"] if a["id"] == "PIP-L6-042")
        self.assertEqual(activity["actualStart"], "2026-09-30")
        self.assertEqual(activity["actualStartTime"], "08:15")
        self.assertEqual(activity["progress"], 77)
        approved = next(r for r in approved_state["reports"] if r["id"] == "RPT-WF-APPROVE")
        self.assertEqual(approved["reviewer"], "Synthetic Planner")
        self.assertTrue(approved["reviewedAt"])
        report_audit = next(a for a in approved_state["audit"] if a["record"] == "RPT-WF-APPROVE" and a["action"] == "Updated report")
        activity_audit = next(a for a in approved_state["audit"] if a["record"] == "PIP-L6-042" and a["action"] == "Updated schedule activity")
        self.assertEqual(report_audit["before"]["status"], "pending")
        self.assertEqual(report_audit["after"]["reviewer"], "Synthetic Planner")
        self.assertEqual(activity_audit["before"]["actualStart"], "2026-09-26")
        self.assertEqual(activity_audit["after"]["actualStart"], "2026-09-30")

        self.add_report("RPT-WF-MISSING-DATE", "Line 24-XX pipe spool erection started; progress is 78%.")
        current = self.state()
        rejected_attempt = next(r for r in current["reports"] if r["id"] == "RPT-WF-MISSING-DATE")
        rejected_attempt.update(status="approved", activityId="PIP-L6-042", progress=78)
        status, error = self.client.request("/api/state", "POST", {"activities": current["activities"], "reports": current["reports"], "reason": "Try without confirmed event date", "revision": current["revision"]})
        self.assertEqual(status, 400)
        self.assertIn("actual start date", error["error"].lower())
        self.assertEqual(next(r for r in self.state()["reports"] if r["id"] == "RPT-WF-MISSING-DATE")["status"], "pending")

        current = self.state()
        item = next(r for r in current["reports"] if r["id"] == "RPT-WF-MISSING-DATE")
        item.update(status="rejected", reviewer="forged reviewer", reviewedAt="2000-01-01T00:00:00Z")
        status, rejected_state = self.client.request("/api/state", "POST", {"activities": current["activities"], "reports": current["reports"], "reason": "Reject unsupported report", "revision": current["revision"]})
        self.assertEqual(status, 200, rejected_state)
        rejected = next(r for r in rejected_state["reports"] if r["id"] == "RPT-WF-MISSING-DATE")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["reviewer"], "Synthetic Planner")
        self.assertNotEqual(rejected["reviewedAt"], "2000-01-01T00:00:00Z")
        self.assertEqual(next(a for a in rejected_state["activities"] if a["id"] == "PIP-L6-042")["progress"], 77)

    def test_sitelink_ai_is_project_scoped_read_only_and_does_not_audit_raw_question(self):
        question = "PRIVATE-QUESTION-MUST-NOT-BE-IN-AUDIT-9147 What is the status of PIP-L6-042?"
        status, result = self.client.request("/api/ai/ask", "POST", {
            "projectId": "project-default", "message": question, "activityId": "PIP-L6-042"})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["read_only"])
        self.assertEqual(result["activity_id"], "PIP-L6-042")
        self.assertTrue(all(item["type"].startswith("approved_") for item in result["evidence"]))
        self.assertNotIn("PRIVATE-QUESTION-MUST-NOT-BE-IN-AUDIT-9147", json.dumps(result))
        with sitelink.connect() as db:
            audit = db.execute("SELECT reason,what,after_json FROM audit WHERE action='Asked SiteLink AI' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(audit)
        self.assertNotIn("PRIVATE-QUESTION-MUST-NOT-BE-IN-AUDIT-9147", json.dumps(dict(audit)))
        status, denied = self.client.request("/api/ai/ask", "POST", {
            "projectId": "project-does-not-belong-to-this-user", "message": "Show project status"})
        self.assertEqual(status, 403)
        self.assertIn("permission", denied["error"].lower())
        status, provider_status = self.client.request("/api/ai/status?projectId=project-default")
        self.assertEqual(status, 200, provider_status)
        self.assertTrue(provider_status["readOnly"])
        self.assertNotIn("apiKey", provider_status)

    def test_sitelink_ai_area_investigation_uses_validated_project_area(self):
        area_id = "area-ai-investigation-test"
        activity_id = "PIP-L6-042"
        storage_id = "project-default::" + activity_id
        with sitelink.connect() as db:
            row = db.execute("SELECT payload FROM activities WHERE tenant_id=? AND project_id=? AND activity_id=?",
                             ("tenant-default", "project-default", storage_id)).fetchone()
            original_payload = row["payload"]
            payload = json.loads(original_payload)
            payload["areaId"] = area_id
            db.execute("UPDATE activities SET payload=? WHERE tenant_id=? AND project_id=? AND activity_id=?",
                       (json.dumps(payload), "tenant-default", "project-default", storage_id))
            db.execute("INSERT INTO project_areas(id,tenant_id,project_id,level,name,code,description,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       (area_id, "tenant-default", "project-default", "area", "Synthetic Unit B", "AI-AREA-TEST", "Test-only synthetic area", sitelink.now_iso()))
        try:
            status, result = self.client.request("/api/ai/investigate", "POST", {
                "projectId": "project-default", "message": "What happened in this area?", "scope": "area", "areaId": area_id})
            self.assertEqual(status, 200, result)
            self.assertIn("Synthetic Unit B", result["answer"])
            self.assertTrue(result["read_only"])
            status, error = self.client.request("/api/ai/investigate", "POST", {
                "projectId": "project-default", "message": "Investigate another area", "scope": "area", "areaId": "not-in-this-project"})
            self.assertEqual(status, 400)
        finally:
            with sitelink.connect() as db:
                db.execute("UPDATE activities SET payload=? WHERE tenant_id=? AND project_id=? AND activity_id=?",
                           (original_payload, "tenant-default", "project-default", storage_id))
                db.execute("DELETE FROM project_areas WHERE id=?", (area_id,))


if __name__ == "__main__":
    unittest.main()
