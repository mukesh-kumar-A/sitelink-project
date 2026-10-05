"""Public self-registration, approval, password policy, and access checks."""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import server as sitelink
from test_multidevice import BrowserSession


class SignupApprovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_data_dir = sitelink.DATA_DIR
        cls.original_db_path = sitelink.DB_PATH
        cls.original_signup = os.environ.get("PUBLIC_SIGNUP")
        cls.original_setup = os.environ.get("REQUIRE_SETUP_TOKEN")
        cls.original_tenant = sitelink.SIGNUP_TENANT_ID
        os.environ["PUBLIC_SIGNUP"] = "1"
        os.environ["REQUIRE_SETUP_TOKEN"] = "0"
        sitelink.SIGNUP_TENANT_ID = sitelink.DEFAULT_TENANT
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), sitelink.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        sitelink.DATA_DIR = cls.original_data_dir
        sitelink.DB_PATH = cls.original_db_path
        sitelink.SIGNUP_TENANT_ID = cls.original_tenant
        if cls.original_signup is None:
            os.environ.pop("PUBLIC_SIGNUP", None)
        else:
            os.environ["PUBLIC_SIGNUP"] = cls.original_signup
        if cls.original_setup is None:
            os.environ.pop("REQUIRE_SETUP_TOKEN", None)
        else:
            os.environ["REQUIRE_SETUP_TOKEN"] = cls.original_setup

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sitelink-signup-")
        root = Path(self.temp.name)
        sitelink.DATA_DIR = root / "data"
        sitelink.DB_PATH = sitelink.DATA_DIR / "sitelink.sqlite3"
        sitelink.init_db()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _admin(self) -> BrowserSession:
        admin = BrowserSession(self.base_url)
        status, result = admin.request("/api/setup", "POST", {
            "name": "Workspace Admin", "username": "admin@example.test", "password": "Admin123"
        }, csrf=False)
        self.assertEqual(status, 201, result)
        return admin

    def _signup(self, username: str, password: str = "User123", **extras) -> tuple[BrowserSession, int, dict]:
        client = BrowserSession(self.base_url)
        data = {"name": "New Teammate", "username": username, "password": password, "confirmPassword": password}
        data.update(extras)
        status, result = client.request("/api/auth/signup", "POST", data, csrf=False)
        return client, status, result

    def test_self_signup_is_pending_in_existing_tenant_and_requires_approval_and_assignment(self) -> None:
        admin = self._admin()
        with sitelink.connect() as db:
            tenant_count = db.execute("SELECT COUNT(*) AS n FROM tenants").fetchone()["n"]
            project_count = db.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]

        user, status, signup = self._signup(
            "new@example.test", "User123", role="Admin", tenantId="attacker-tenant",
            projectIds=[sitelink.DEFAULT_PROJECT]
        )
        self.assertEqual(status, 201, signup)
        self.assertEqual(signup["status"], "pending")
        self.assertEqual(signup["message"], "Account created successfully. Your account is waiting for administrator approval.")
        self.assertEqual(user.request("/api/auth/me")[1]["user"], None)
        self.assertEqual(user.request("/api/auth/login", "POST", {"username": "new@example.test", "password": "User123"}, csrf=False),
                         (403, {"error": "Your account is waiting for administrator approval."}))
        for endpoint in ("/api/state", "/api/projects", "/api/reports", "/api/dashboard", "/api/audit", "/api/intelligence", "/api/users"):
            self.assertEqual(user.request(endpoint)[0], 401, endpoint)
        self.assertEqual(user.request("/api/analyze", "POST", {"text": "protected"})[0], 401)
        self.assertEqual(user.request("/api/memory", "POST", {"question": "protected"})[0], 401)

        with sitelink.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) AS n FROM tenants").fetchone()["n"], tenant_count)
            self.assertEqual(db.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"], project_count)
            account = db.execute("SELECT role,tenant_id,account_status,approved FROM users WHERE username=?", ("new@example.test",)).fetchone()
            self.assertEqual(tuple(account), ("Viewer", sitelink.DEFAULT_TENANT, "pending", 0))
            self.assertEqual(db.execute("SELECT COUNT(*) AS n FROM project_members WHERE user_id=(SELECT id FROM users WHERE username=?)", ("new@example.test",)).fetchone()["n"], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) AS n FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)", ("new@example.test",)).fetchone()["n"], 0)

        pending = next(row for row in admin.request("/api/users")[1]["users"] if row["username"] == "new@example.test")
        self.assertEqual(pending["status"], "Pending")
        self.assertEqual(pending["role"], "Viewer")
        self.assertEqual(admin.request(f"/api/users/{pending['id']}/approve", "POST", {"reason": "Approve teammate signup"})[0], 200)
        user_status, _ = user.request("/api/auth/login", "POST", {"username": "new@example.test", "password": "User123"}, csrf=False)
        self.assertEqual(user_status, 200)
        self.assertEqual(user.request("/api/state")[0], 403)

        status, _ = admin.request(f"/api/users/{pending['id']}", "PATCH", {
            "role": "Planner", "projectIds": [sitelink.DEFAULT_PROJECT], "reason": "Assign approved project access"
        })
        self.assertEqual(status, 200)
        self.assertEqual(user.request("/api/state")[0], 200)
        self.assertEqual(user.request(f"/api/state?projectId=attacker-project")[0], 403)

    def test_password_lengths_duplicate_username_and_rejection(self) -> None:
        self._admin()
        for size in (5, 9):
            password = "x" * size
            _, status, result = self._signup(f"invalid{size}@example.test", password)
            self.assertEqual(status, 400)
            self.assertEqual(result["error"], "Password must be 6–8 characters.")
        for size in (6, 7, 8):
            password = "x" * size
            _, status, result = self._signup(f"valid{size}@example.test", password)
            self.assertEqual(status, 201, result)
        rejected_user, _, _ = self._signup("rejected@example.test", "Reject6")
        _, duplicate_status, _ = self._signup("valid6@example.test", "xxxxxx")
        self.assertEqual(duplicate_status, 409)

        admin = BrowserSession(self.base_url)
        admin.request("/api/auth/login", "POST", {"username": "admin@example.test", "password": "Admin123"}, csrf=False)
        user_id = next(u["id"] for u in admin.request("/api/users")[1]["users"] if u["username"] == "rejected@example.test")
        status, rejected = admin.request(f"/api/users/{user_id}/reject", "POST", {"reason": "Reject test signup"})
        self.assertEqual(status, 200)
        self.assertEqual(next(u for u in rejected["users"] if u["id"] == user_id)["status"], "Rejected")
        status, disabled = rejected_user.request("/api/auth/login", "POST", {"username": "rejected@example.test", "password": "Reject6"}, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(disabled["error"], "Your account is disabled. Contact your administrator.")

    def test_admin_create_reset_and_self_service_password_change_use_same_limit(self) -> None:
        for size in (5, 9):
            status, result = BrowserSession(self.base_url).request("/api/setup", "POST", {
                "name": "Workspace Admin", "username": "admin@example.test", "password": "x" * size
            }, csrf=False)
            self.assertEqual(status, 400)
            self.assertEqual(result["error"], "Password must be 6–8 characters.")
        admin = self._admin()
        for size in (5, 9):
            status, result = admin.request("/api/users", "POST", {
                "name": "Manual User", "username": f"manual{size}", "password": "x" * size,
                "role": "Viewer", "projectIds": [sitelink.DEFAULT_PROJECT], "reason": "Create manual test user"
            })
            self.assertEqual(status, 400)
            self.assertEqual(result["error"], "Password must be 6–8 characters.")
        status, result = admin.request("/api/users", "POST", {
            "name": "Manual User", "username": "manual6", "password": "Manual6",
            "role": "Viewer", "projectIds": [sitelink.DEFAULT_PROJECT], "reason": "Create manual test user"
        })
        self.assertEqual(status, 201, result)
        user_id = next(u["id"] for u in result["users"] if u["username"] == "manual6")
        for size in (5, 9):
            status, result = admin.request(f"/api/users/{user_id}/reset-password", "POST", {
                "password": "x" * size, "reason": "Validate reset password length"
            })
            self.assertEqual(status, 400)
        self.assertEqual(admin.request(f"/api/users/{user_id}/reset-password", "POST", {
            "password": "Reset6", "reason": "Reset to valid short password"
        })[0], 200)

        other_device = BrowserSession(self.base_url)
        self.assertEqual(other_device.request("/api/auth/login", "POST", {"username": "admin@example.test", "password": "Admin123"}, csrf=False)[0], 200)
        for size in (5, 9):
            status, result = admin.request("/api/auth/password", "POST", {
                "currentPassword": "Admin123", "password": "x" * size, "confirmPassword": "x" * size
            })
            self.assertEqual(status, 400)
        self.assertEqual(admin.request("/api/auth/password", "POST", {
            "currentPassword": "Admin123", "password": "Admin67", "confirmPassword": "Admin67"
        })[0], 200)
        self.assertEqual(admin.request("/api/auth/me")[1]["user"]["role"], "Admin")
        self.assertEqual(other_device.request("/api/auth/me")[1]["user"], None)
        self.assertEqual(other_device.request("/api/auth/login", "POST", {"username": "admin@example.test", "password": "Admin123"}, csrf=False)[0], 403)
        self.assertEqual(other_device.request("/api/auth/login", "POST", {"username": "admin@example.test", "password": "Admin67"}, csrf=False)[0], 200)

    def test_disabled_account_cannot_login_and_admin_can_enable_it(self) -> None:
        admin = self._admin()
        status, result = admin.request("/api/users", "POST", {
            "name": "Disabled User", "username": "disabled-user", "password": "Disable6",
            "role": "Viewer", "projectIds": [sitelink.DEFAULT_PROJECT], "reason": "Create account to test disable"
        })
        self.assertEqual(status, 201, result)
        user_id = next(u["id"] for u in result["users"] if u["username"] == "disabled-user")
        user = BrowserSession(self.base_url)
        self.assertEqual(user.request("/api/auth/login", "POST", {"username": "disabled-user", "password": "Disable6"}, csrf=False)[0], 200)
        status, _ = admin.request(f"/api/users/{user_id}", "PATCH", {"active": False, "reason": "Disable account test user"})
        self.assertEqual(status, 200)
        self.assertEqual(user.request("/api/state")[0], 401)
        status, disabled = user.request("/api/auth/login", "POST", {"username": "disabled-user", "password": "Disable6"}, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(disabled["error"], "Your account is disabled. Contact your administrator.")
        self.assertEqual(admin.request(f"/api/users/{user_id}", "PATCH", {"active": True, "reason": "Enable account test user"})[0], 200)
        self.assertEqual(user.request("/api/auth/login", "POST", {"username": "disabled-user", "password": "Disable6"}, csrf=False)[0], 200)


if __name__ == "__main__":
    unittest.main()
