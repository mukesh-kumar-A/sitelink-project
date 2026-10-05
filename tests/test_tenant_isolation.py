"""Project membership and tenant isolation integration checks for v4."""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import server as sitelink
from test_multidevice import BrowserSession


class TenantIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_data_dir = sitelink.DATA_DIR
        cls.original_db_path = sitelink.DB_PATH
        cls.original_signup = os.environ.get("PUBLIC_SIGNUP")
        cls.original_require_setup = os.environ.get("REQUIRE_SETUP_TOKEN")
        cls.original_setup_token = os.environ.get("SETUP_TOKEN")
        os.environ["REQUIRE_SETUP_TOKEN"] = "1"
        os.environ["SETUP_TOKEN"] = "one-time-test-setup-token"
        cls.temp = tempfile.TemporaryDirectory(prefix="sitelink-v4-tenant-")
        root = Path(cls.temp.name)
        sitelink.DATA_DIR = root / "data"
        sitelink.DB_PATH = sitelink.DATA_DIR / "sitelink.sqlite3"
        sitelink.init_db()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), sitelink.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        sitelink.DATA_DIR = cls.original_data_dir
        sitelink.DB_PATH = cls.original_db_path
        if cls.original_signup is None:
            os.environ.pop("PUBLIC_SIGNUP", None)
        else:
            os.environ["PUBLIC_SIGNUP"] = cls.original_signup
        if cls.original_require_setup is None:
            os.environ.pop("REQUIRE_SETUP_TOKEN", None)
        else:
            os.environ["REQUIRE_SETUP_TOKEN"] = cls.original_require_setup
        if cls.original_setup_token is None:
            os.environ.pop("SETUP_TOKEN", None)
        else:
            os.environ["SETUP_TOKEN"] = cls.original_setup_token
        cls.temp.cleanup()

    def test_project_assignment_permission_and_tenant_boundaries(self) -> None:
        admin = BrowserSession(self.base_url)
        self.assertEqual(admin.request("/api/auth/me")[1]["setupTokenRequired"], True)
        self.assertEqual(admin.request("/api/setup", "POST", {
            "name": "Workspace Admin", "username": "owner@one.example", "password": "Owner123"
        }, csrf=False)[0], 403)
        status, _ = admin.request("/api/setup", "POST", {
            "name": "Workspace Admin", "username": "owner@one.example", "password": "Owner123",
            "setupToken": "one-time-test-setup-token"
        }, csrf=False)
        self.assertEqual(status, 201)
        status, initial = admin.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(initial["currentProject"]["id"], sitelink.DEFAULT_PROJECT)

        projects = []
        for name in ("Project Alpha", "Project Beta"):
            status, result = admin.request("/api/projects", "POST", {
                "name": name, "client": "Synthetic Client", "phase": "Phase 01",
                "reason": "Create isolated project for access verification"
            })
            self.assertEqual(status, 201)
            projects.append(result["project"]["id"])
        alpha, beta = projects

        accounts = {}
        for name, username, role, assigned in [
            ("Planner Alpha", "planner.alpha@one.example", "Planner", [alpha]),
            ("Planner Beta", "planner.beta@one.example", "Planner", [beta]),
            ("Field Supervisor", "supervisor@one.example", "Supervisor", [alpha]),
            ("Read Only", "viewer@one.example", "Viewer", [alpha]),
        ]:
            status, result = admin.request("/api/users", "POST", {
                "name": name, "username": username, "password": "Test1234",
                "role": role, "projectIds": assigned, "reason": "Create assigned project user account"
            })
            self.assertEqual(status, 201)
            accounts[role + username] = next(u for u in result["users"] if u["username"] == username)

        alpha_admin_state = admin.request(f"/api/state?projectId={alpha}")[1]
        sample_activity = {
            "id": "ALPHA-ACT-001", "wbs": "1.1", "name": "Install alpha pipe spool",
            "discipline": "Piping", "location": "Unit Alpha", "plannedStart": "2026-09-20",
            "plannedFinish": "2026-09-30", "actualStart": "", "actualFinish": "",
            "progress": 10, "plannedProgress": 15, "owner": "Crew A", "status": "In progress",
            "predecessors": [], "contractor": "Synthetic", "updateCadenceHours": 48,
        }
        status, _ = admin.request("/api/state", "POST", {
            "projectId": alpha, "activities": [sample_activity], "reports": [],
            "reason": "Add isolated alpha schedule activity", "revision": alpha_admin_state["revision"]
        })
        self.assertEqual(status, 200)

        planner_alpha = self._login("planner.alpha@one.example")
        planner_beta = self._login("planner.beta@one.example")
        supervisor = self._login("supervisor@one.example")
        viewer = self._login("viewer@one.example")

        status, alpha_state = planner_alpha.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual([a["id"] for a in alpha_state["activities"]], ["ALPHA-ACT-001"])
        self.assertEqual([p["id"] for p in planner_alpha.request("/api/projects")[1]["projects"]], [alpha])
        self.assertEqual(planner_alpha.request(f"/api/state?projectId={beta}")[0], 403)
        self.assertEqual(planner_alpha.request(f"/api/reports?projectId={beta}")[0], 403)
        self.assertEqual(planner_alpha.request(f"/api/projects/{beta}")[0], 403)
        status, denied = planner_alpha.request("/api/state", "POST", {
            "projectId": beta, "activities": [], "reports": [], "revision": 0,
            "reason": "Attempt cross-project update"
        })
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"], "You do not have permission to access this page.")
        beta_state = planner_beta.request("/api/state")[1]
        self.assertEqual(beta_state["activities"], [])
        self.assertNotIn("ALPHA-ACT-001", [a["id"] for a in beta_state["activities"]])

        supervisor_state = supervisor.request("/api/state")[1]
        self.assertFalse(supervisor_state["permissions"]["approve"])
        pending_report = {
            "id": "ALPHA-REPORT-001", "date": "2026-09-29", "discipline": "Piping",
            "text": "Alpha pipe spool installation is 35% complete.", "reporter": "Field Supervisor",
            "source": "Supervisor update", "sourceType": "manual", "actualStart": "",
            "actualEnd": "", "progress": 35, "status": "pending", "activityId": "", "note": ""
        }
        status, supervisor_state = supervisor.request("/api/state", "POST", {
            "projectId": alpha, "activities": supervisor_state["activities"],
            "reports": [pending_report], "reason": "Submit alpha field progress report",
            "revision": supervisor_state["revision"]
        })
        self.assertEqual(status, 200)
        pending_report = next(r for r in supervisor_state["reports"] if r["id"] == "ALPHA-REPORT-001")
        pending_report.update(status="approved", activityId="ALPHA-ACT-001", reviewer="Field Supervisor")
        self.assertEqual(supervisor.request("/api/state", "POST", {
            "projectId": alpha, "activities": supervisor_state["activities"],
            "reports": supervisor_state["reports"], "reason": "Attempt ungranted report approval",
            "revision": supervisor_state["revision"]
        })[0], 403)

        member_result = admin.request(f"/api/projects/{alpha}/members")[1]
        members = []
        for item in member_result["members"]:
            permissions = dict(item["permissions"])
            if item["username"] == "supervisor@one.example":
                permissions["approve"] = True
            members.append({"userId": item["id"], "permissions": permissions})
        status, _ = admin.request(f"/api/projects/{alpha}/members", "PUT", {
            "members": members, "reason": "Grant supervisor project approval permission"
        })
        self.assertEqual(status, 200)
        supervisor_state = supervisor.request("/api/state")[1]
        self.assertTrue(supervisor_state["permissions"]["approve"])

        self.assertEqual(viewer.request("/api/state")[0], 200)
        self.assertEqual(viewer.request("/api/users")[0], 403)
        viewer_state = viewer.request("/api/state")[1]
        self.assertEqual(viewer.request("/api/state", "POST", {
            "projectId": alpha, "activities": viewer_state["activities"], "reports": viewer_state["reports"],
            "reason": "Viewer tries to change shared data", "revision": viewer_state["revision"]
        })[0], 403)

        # The Admin can narrow a member's view grant; a membership alone is not sufficient.
        beta_members = admin.request(f"/api/projects/{beta}/members")[1]["members"]
        beta_assignments = []
        for item in beta_members:
            permissions = dict(item["permissions"])
            if item["username"] == "planner.beta@one.example":
                permissions["view"] = False
            beta_assignments.append({"userId": item["id"], "permissions": permissions})
        self.assertEqual(admin.request(f"/api/projects/{beta}/members", "PUT", {
            "members": beta_assignments, "reason": "Narrow beta planner project access"
        })[0], 200)
        self.assertEqual(planner_beta.request("/api/state")[0], 403)
        self.assertEqual(planner_beta.request("/api/projects")[1]["projects"], [])

        # A separately configured tenant remains isolated from the public signup tenant.
        with sitelink.connect() as db:
            salt, password_hash = sitelink.hash_password("Owner223")
            stamp = sitelink.now_iso()
            db.execute("INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)", ("tenant-two", "Second Workspace", stamp))
            db.execute("INSERT INTO projects(id,tenant_id,name,created_at) VALUES(?,?,?,?)", ("project-two", "tenant-two", "Private Project", stamp))
            db.execute("INSERT INTO project_revisions(tenant_id,project_id,revision) VALUES(?,?,0)", ("tenant-two", "project-two"))
            db.execute("INSERT INTO users(username,display_name,role,salt,password_hash,created_at,tenant_id) VALUES(?,?,?,?,?,?,?)", ("owner@two.example", "Second Owner", "Admin", salt, password_hash, stamp, "tenant-two"))
        second_owner = BrowserSession(self.base_url)
        status, _ = second_owner.request("/api/auth/login", "POST", {
            "username": "owner@two.example", "password": "Owner223"
        }, csrf=False)
        self.assertEqual(status, 200)
        second_state = second_owner.request("/api/state")[1]
        self.assertEqual(second_state["currentProject"]["name"], "Private Project")
        self.assertEqual(second_owner.request(f"/api/state?projectId={alpha}")[0], 403)
        self.assertEqual(second_owner.request("/api/users")[1]["users"][0]["username"], "owner@two.example")

        viewer_id = accounts["Viewer" + "viewer@one.example"]["id"]
        self.assertEqual(admin.request(f"/api/users/{viewer_id}", "DELETE", {
            "reason": "Remove test viewer account at end of isolated-access test"
        })[0], 200)
        self.assertEqual(viewer.request("/api/state")[0], 401)

    def _login(self, username: str) -> BrowserSession:
        client = BrowserSession(self.base_url)
        status, payload = client.request("/api/auth/login", "POST", {
            "username": username, "password": "Test1234"
        }, csrf=False)
        self.assertEqual(status, 200, payload)
        self.assertEqual(client.request("/api/auth/me")[0], 200)
        return client


if __name__ == "__main__":
    unittest.main()
