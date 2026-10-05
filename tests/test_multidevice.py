"""LAN, independent-session, RBAC, shared-state and stale-write integration checks."""
from __future__ import annotations

import http.cookiejar
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import server as sitelink


class BrowserSession:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.csrf = ""

    def request(self, path: str, method: str = "GET", data: dict | None = None, csrf: bool = True) -> tuple[int, dict]:
        headers = {"Accept": "application/json"}
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode("utf-8")
        if csrf and method != "GET" and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            response = self.opener.open(req, timeout=10)
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            status = exc.code
            payload = json.loads(exc.read().decode("utf-8"))
        if "csrf" in payload and payload["csrf"]:
            self.csrf = payload["csrf"]
        return status, payload


class MultiDeviceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_data_dir = sitelink.DATA_DIR
        cls.original_db_path = sitelink.DB_PATH
        cls.temp = tempfile.TemporaryDirectory(prefix="sitelink-multidevice-")
        root = Path(cls.temp.name)
        sitelink.DATA_DIR = root / "data"
        sitelink.DB_PATH = sitelink.DATA_DIR / "sitelink.sqlite3"
        sitelink.init_db()
        cls.httpd = ThreadingHTTPServer(("0.0.0.0", 0), sitelink.Handler)
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
        cls.temp.cleanup()

    def test_shared_team_workflow_and_safe_concurrent_writes(self) -> None:
        admin = BrowserSession(self.base_url)
        status, setup = admin.request("/api/setup", "POST", {
            "name": "Team Admin", "username": "admin@example.local", "password": "Adm12345"
        }, csrf=False)
        self.assertEqual(status, 201)
        self.assertEqual(setup["user"]["role"], "Admin")

        for name, username, password, role in [
            ("Planner One", "planner@example.local", "Plan1234", "Planner"),
            ("Supervisor One", "supervisor@example.local", "Sup12345", "Supervisor"),
            ("Viewer One", "viewer@example.local", "View1234", "Viewer"),
        ]:
            status, _ = admin.request("/api/users", "POST", {
                "name": name, "username": username, "password": password, "role": role,
                "reason": "Create SIH team prototype account"
            })
            self.assertEqual(status, 201)

        planner = self._login("planner@example.local", "Plan1234")
        supervisor = self._login("supervisor@example.local", "Sup12345")
        viewer = self._login("viewer@example.local", "View1234")

        # Two browser sessions start from the same revision and submit at nearly the same time.
        admin_state = admin.request("/api/state")[1]
        planner_state = planner.request("/api/state")[1]
        self.assertEqual(admin_state["revision"], planner_state["revision"])
        admin_activity = next(a for a in admin_state["activities"] if a["id"] == "CIV-L6-014")
        planner_activity = next(a for a in planner_state["activities"] if a["id"] == "INS-L6-008")
        admin_activity["progress"] = 79
        planner_activity["progress"] = 46
        payloads = [
            (admin, dict(activities=admin_state["activities"], reports=admin_state["reports"], reason="Concurrent admin progress change", revision=admin_state["revision"])),
            (planner, dict(activities=planner_state["activities"], reports=planner_state["reports"], reason="Concurrent planner progress change", revision=planner_state["revision"])),
        ]
        barrier = threading.Barrier(2)
        results: list[int] = []

        def simultaneous(client: BrowserSession, data: dict) -> None:
            barrier.wait(timeout=5)
            results.append(client.request("/api/state", "POST", data)[0])

        writers = [threading.Thread(target=simultaneous, args=item) for item in payloads]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join(timeout=10)
        self.assertEqual(sorted(results), [200, 409])
        shared = planner.request("/api/state")[1]
        changed = [a for a in shared["activities"] if a["id"] in {"CIV-L6-014", "INS-L6-008"}]
        self.assertEqual(sum(a["progress"] in (79, 46) for a in changed), 1)

        # A Supervisor's report is visible on the Planner's independent session.
        sup_state = supervisor.request("/api/state")[1]
        pending = {
            "id": "RPT-LAN-001", "date": "2026-09-29", "discipline": "Piping",
            "text": "Line 24-XX spool erection is 80% complete.", "reporter": "Supervisor One",
            "source": "LAN team test", "sourceType": "text", "actualStart": "", "actualEnd": "",
            "progress": 80, "status": "pending", "activityId": "", "note": ""
        }
        status, _ = supervisor.request("/api/state", "POST", {
            "activities": sup_state["activities"], "reports": sup_state["reports"] + [pending],
            "reason": "Record current piping progress", "revision": sup_state["revision"]
        })
        self.assertEqual(status, 200)
        plan_state = planner.request("/api/state")[1]
        report = next(r for r in plan_state["reports"] if r["id"] == "RPT-LAN-001")
        report.update(status="approved", activityId="PIP-L6-042", reviewer="Planner One", reviewedAt="2026-09-29T12:00:00Z")
        activity = next(a for a in plan_state["activities"] if a["id"] == "PIP-L6-042")
        activity.update(progress=80, status="In progress")
        status, _ = planner.request("/api/state", "POST", {
            "activities": plan_state["activities"], "reports": plan_state["reports"],
            "reason": "Approve confirmed supervisor update", "revision": plan_state["revision"]
        })
        self.assertEqual(status, 200)

        viewer_state = viewer.request("/api/state")[1]
        self.assertEqual(next(a for a in viewer_state["activities"] if a["id"] == "PIP-L6-042")["progress"], 80)
        self.assertEqual(next(r for r in viewer_state["reports"] if r["id"] == "RPT-LAN-001")["status"], "approved")
        status, denied = viewer.request("/api/state", "POST", {
            "activities": viewer_state["activities"], "reports": viewer_state["reports"],
            "reason": "Unauthorized viewer write", "revision": viewer_state["revision"]
        })
        self.assertEqual(status, 403)
        self.assertIn("permission", denied["error"].lower())
        status, _ = planner.request("/api/users", "POST", {
            "name": "Unauthorized", "username": "unauthorized", "password": "Long1234",
            "role": "Admin", "reason": "Try unauthorized admin endpoint"
        })
        self.assertEqual(status, 403)

        admin_state = admin.request("/api/state")[1]
        self.assertTrue(admin_state["audit"])
        approval = next(a for a in admin_state["audit"] if a["record"] == "RPT-LAN-001" and a["action"] == "Updated report")
        self.assertEqual(approval["role"], "Planner")
        self.assertTrue(approval["sourceIp"])
        viewer_row = next(u for u in admin_state["users"] if u["username"] == "viewer@example.local")
        self.assertIsNotNone(viewer_row["lastLogin"])

        # Admin password reset is audited and revokes only the target user's old session.
        status, _ = admin.request(f"/api/users/{viewer_row['id']}/reset-password", "POST", {
            "password": "Reset123", "reason": "Reset test viewer password"
        })
        self.assertEqual(status, 200)
        self.assertEqual(viewer.request("/api/state")[0], 401)
        self.assertEqual(planner.request("/api/state")[0], 200)
        viewer2 = self._login("viewer@example.local", "Reset123")
        self.assertEqual(viewer2.request("/api/state")[0], 200)

        # A user's logout does not invalidate a different user's server-side session.
        self.assertEqual(admin.request("/api/logout", "POST", {})[0], 200)
        self.assertEqual(planner.request("/api/state")[0], 200)

        with sitelink.connect() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        db.close()

        # Restart the listener against the same SQLite file: saved records and sessions persist.
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.__class__.httpd = ThreadingHTTPServer(("0.0.0.0", self.port), sitelink.Handler)
        self.__class__.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.__class__.thread.start()
        restarted = planner.request("/api/state")[1]
        self.assertEqual(next(a for a in restarted["activities"] if a["id"] == "PIP-L6-042")["progress"], 80)
        self.assertEqual(self.httpd.server_address[0], "0.0.0.0")

    def test_existing_sqlite_database_gets_additive_migrations(self) -> None:
        legacy_dir = Path(self.temp.name) / "legacy"
        legacy_dir.mkdir(exist_ok=True)
        legacy_path = legacy_dir / "existing.sqlite3"
        with sqlite3.connect(legacy_path) as old:
            old.executescript("""
                CREATE TABLE users (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                  display_name TEXT NOT NULL, role TEXT NOT NULL, salt TEXT NOT NULL,
                  password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
                );
                INSERT INTO users(username,display_name,role,salt,password_hash,created_at)
                  VALUES('preserved@example.local','Preserved User','Planner','salt','hash','2026-01-01T00:00:00Z');
                CREATE TABLE audit (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, happened_at TEXT NOT NULL, actor_id INTEGER,
                  actor_name TEXT NOT NULL, actor_role TEXT NOT NULL, action TEXT NOT NULL,
                  record_id TEXT NOT NULL, source_location TEXT NOT NULL, reason TEXT NOT NULL,
                  what TEXT NOT NULL, before_json TEXT, after_json TEXT
                );
                INSERT INTO audit(happened_at,actor_name,actor_role,action,record_id,source_location,reason,what)
                  VALUES('2026-01-01T00:00:00Z','Preserved User','Planner','Test legacy','ITEM-1','legacy','test','kept');
            """)
        old.close()
        before_data, before_db = sitelink.DATA_DIR, sitelink.DB_PATH
        try:
            sitelink.DATA_DIR, sitelink.DB_PATH = legacy_dir, legacy_path
            sitelink.init_db()
            with sitelink.connect() as migrated:
                user = migrated.execute("SELECT username,last_login FROM users WHERE username='preserved@example.local'").fetchone()
                audit = migrated.execute("SELECT what,source_ip FROM audit WHERE action='Test legacy'").fetchone()
                self.assertEqual(user["username"], "preserved@example.local")
                self.assertIsNone(user["last_login"])
                self.assertEqual(audit["what"], "kept")
                self.assertIsNone(audit["source_ip"])
                self.assertEqual(migrated.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        finally:
            sitelink.DATA_DIR, sitelink.DB_PATH = before_data, before_db

    def _login(self, username: str, password: str) -> BrowserSession:
        client = BrowserSession(self.base_url)
        status, payload = client.request("/api/login", "POST", {"username": username, "password": password}, csrf=False)
        self.assertEqual(status, 200, payload)
        return client


if __name__ == "__main__":
    unittest.main()
