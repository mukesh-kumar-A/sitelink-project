#!/usr/bin/env python3
"""SiteLink v4 multi-tenant project execution prototype."""
from __future__ import annotations

import csv
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from intelligence import analyze_report, answer_memory, build_intelligence, extract_facts, _validated_llm_facts
from sitelink_ai import ask as ask_sitelink_ai, configured_provider as configured_ai_provider
from imports import MAX_UPLOAD_BYTES, export_mspdi_xml, extract_ocr, parse_report_file, parse_schedule_file
from project_control import (CONDITIONS, MATERIAL_STATES, PROJECT_STATUSES, RISK_SEVERITIES, RISK_STATUSES,
                              activity_condition, analyze_plan_file, iso_date, normalize_schedule_row,
                              numeric, weighted_progress)
from execution_intelligence import analyze_execution, propose_recommendations, simulate_recovery

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).expanduser()
DB_PATH = DATA_DIR / "sitelink.sqlite3"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")
SESSION_HOURS = max(1, min(168, int(os.environ.get("SESSION_HOURS", "12"))))
PBKDF2_ROUNDS = 310_000
MAX_BODY = 12 * 1024 * 1024
ROLES = {"Admin", "Project Manager", "Planner", "Supervisor", "Contractor", "Viewer"}
DEFAULT_TENANT = "tenant-default"
DEFAULT_PROJECT = "project-default"
SIGNUP_TENANT_ID = os.environ.get("SIGNUP_TENANT_ID", DEFAULT_TENANT).strip() or DEFAULT_TENANT
ROLE_PERMISSIONS = {
    "Project Manager": {"view": True, "submit": True, "review": True, "approve": True, "schedule_edit": False},
    "Planner": {"view": True, "submit": True, "review": True, "approve": True, "schedule_edit": True},
    "Supervisor": {"view": True, "submit": True, "review": True, "approve": False, "schedule_edit": False},
    "Contractor": {"view": True, "submit": True, "review": False, "approve": False, "schedule_edit": False},
    "Viewer": {"view": True, "submit": False, "review": False, "approve": False, "schedule_edit": False},
}


class StateConflictError(Exception):
    """Raised when a client tries to write a stale workspace snapshot."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ClosingSQLiteConnection(sqlite3.Connection):
    """Commit/rollback with a context manager and close the per-request handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class PostgresConnection:
    """Small qmark/dict-row adapter so the existing parameterized SQL can use PostgreSQL."""

    is_postgres = True

    def __init__(self, database_url: str):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("DATABASE_URL is PostgreSQL but psycopg is missing. Install requirements.txt.") from exc
        self._psycopg = psycopg
        self._connection = psycopg.connect(database_url, row_factory=_postgres_row_factory, autocommit=False)

    def execute(self, sql: str, params: tuple | list = ()):
        source = sql.strip()
        if source.upper().startswith("PRAGMA "):
            return EmptyCursor()
        if source.upper() == "BEGIN IMMEDIATE":
            source = "BEGIN"
        ignore = source.upper().startswith("INSERT OR IGNORE INTO ")
        if ignore:
            source = source.replace("INSERT OR IGNORE INTO", "INSERT INTO", 1)
        source = source.replace("username=? COLLATE NOCASE", "LOWER(username)=LOWER(?)")
        source = source.replace("CAST(value AS INTEGER)+1", "CAST(CAST(value AS INTEGER)+1 AS TEXT)")
        source = source.replace("?", "%s")
        if ignore:
            source += " ON CONFLICT DO NOTHING"
        try:
            return self._connection.execute(source, params)
        except self._psycopg.IntegrityError as exc:
            raise sqlite3.IntegrityError(str(exc)) from exc
        except self._psycopg.OperationalError as exc:
            raise sqlite3.OperationalError(str(exc)) from exc

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self) -> None:
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self._connection.close()
        return False


class EmptyCursor:
    def fetchone(self): return None
    def fetchall(self): return []


class PostgresRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def _postgres_row_factory(cursor):
    columns = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: PostgresRow(zip(columns, values))


def _table_columns(db, table: str) -> set[str]:
    if getattr(db, "is_postgres", False):
        return {row["name"] for row in db.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?", (table,)).fetchall()}
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def self_migrate_user_roles(db) -> None:
    """Add the two operational roles without dropping accounts or access history."""
    if getattr(db, "is_postgres", False):
        db.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check")
        db.execute("ALTER TABLE users ADD CONSTRAINT users_role_check CHECK(role IN ('Admin','Project Manager','Planner','Supervisor','Contractor','Viewer'))")
        return
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    sql = str(row["sql"] if row else "")
    if "Project Manager" in sql and "Contractor" in sql:
        return
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("""CREATE TABLE users_v5 (
      id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
      display_name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('Admin','Project Manager','Planner','Supervisor','Contractor','Viewer')),
      salt TEXT NOT NULL, password_hash TEXT NOT NULL, company TEXT NOT NULL DEFAULT '',
      requested_project TEXT NOT NULL DEFAULT '', requested_role TEXT NOT NULL DEFAULT 'Viewer',
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, last_login TEXT,
      account_status TEXT NOT NULL DEFAULT 'active', approved INTEGER NOT NULL DEFAULT 1,
      tenant_id TEXT NOT NULL DEFAULT 'tenant-default'
    )""")
    columns = _table_columns(db, "users")
    defaults = {"company": "''", "requested_project": "''", "requested_role": "'Viewer'", "last_login": "NULL", "account_status": "'active'", "approved": "1", "tenant_id": "'tenant-default'"}
    fields = ["id", "username", "display_name", "role", "salt", "password_hash", "company", "requested_project", "requested_role", "active", "created_at", "last_login", "account_status", "approved", "tenant_id"]
    select = [field if field in columns else defaults[field] for field in fields]
    db.execute("INSERT INTO users_v5(" + ",".join(fields) + ") SELECT " + ",".join(select) + " FROM users")
    db.execute("DROP TABLE users")
    db.execute("ALTER TABLE users_v5 RENAME TO users")
    db.execute("PRAGMA foreign_keys=ON")


def connect():
    if DATABASE_URL.startswith(("postgres://", "postgresql://")):
        url = "postgresql://" + DATABASE_URL[len("postgres://"):] if DATABASE_URL.startswith("postgres://") else DATABASE_URL
        return PostgresConnection(url)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15, factory=ClosingSQLiteConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=15000")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA synchronous=NORMAL")
    return db


def init_db() -> None:
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        schema = """
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT NOT NULL UNIQUE COLLATE NOCASE,
          display_name TEXT NOT NULL,
          role TEXT NOT NULL CONSTRAINT users_role_check CHECK(role IN ('Admin','Project Manager','Planner','Supervisor','Contractor','Viewer')),
          salt TEXT NOT NULL,
          password_hash TEXT NOT NULL,
          company TEXT NOT NULL DEFAULT '',
          requested_project TEXT NOT NULL DEFAULT '',
          requested_role TEXT NOT NULL DEFAULT 'Viewer',
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          last_login TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY,
          csrf_token TEXT NOT NULL,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS activities (
          activity_id TEXT PRIMARY KEY,
          payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reports (
          report_id TEXT PRIMARY KEY,
          payload TEXT NOT NULL,
          archived INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          created_by INTEGER REFERENCES users(id),
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          happened_at TEXT NOT NULL,
          actor_id INTEGER REFERENCES users(id),
          actor_name TEXT NOT NULL,
          actor_role TEXT NOT NULL,
          action TEXT NOT NULL,
          record_id TEXT NOT NULL,
          source_location TEXT NOT NULL,
          reason TEXT NOT NULL,
          what TEXT NOT NULL,
          before_json TEXT,
          after_json TEXT,
          source_ip TEXT
        );
        CREATE TABLE IF NOT EXISTS login_attempts (
          ip_key TEXT NOT NULL,
          username_key TEXT NOT NULL,
          happened_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS audit_happened_idx ON audit(happened_at DESC);
        CREATE TABLE IF NOT EXISTS workspace_meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tenants (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS projects (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
          name TEXT NOT NULL, client TEXT NOT NULL DEFAULT '', phase TEXT NOT NULL DEFAULT '',
          code TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '', project_type TEXT NOT NULL DEFAULT '',
          start_date TEXT NOT NULL DEFAULT '', planned_completion_date TEXT NOT NULL DEFAULT '',
          manager_id INTEGER REFERENCES users(id), description TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'Planning',
          created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS project_members (
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          tenant_id TEXT NOT NULL REFERENCES tenants(id),
          permissions TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(project_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS project_revisions (
          tenant_id TEXT NOT NULL REFERENCES tenants(id),
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          revision INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(tenant_id,project_id)
        );
        CREATE TABLE IF NOT EXISTS project_areas (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          parent_id TEXT REFERENCES project_areas(id), level TEXT NOT NULL CHECK(level IN ('area','sub_area')),
          name TEXT NOT NULL, code TEXT, description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
          created_by INTEGER REFERENCES users(id), UNIQUE(project_id,code)
        );
        CREATE TABLE IF NOT EXISTS project_documents (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          category TEXT NOT NULL, file_name TEXT NOT NULL, mime_type TEXT NOT NULL, file_size INTEGER NOT NULL,
          sha256 TEXT NOT NULL, storage_key TEXT NOT NULL UNIQUE, version_no INTEGER NOT NULL DEFAULT 1,
          area_id TEXT REFERENCES project_areas(id), activity_id TEXT, uploaded_at TEXT NOT NULL,
          uploaded_by INTEGER REFERENCES users(id), metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS plan_versions (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          document_id TEXT NOT NULL REFERENCES project_documents(id), version_no INTEGER NOT NULL,
          processing_status TEXT NOT NULL DEFAULT 'Uploaded', analysis_status TEXT NOT NULL DEFAULT 'Not analyzed',
          analysis_json TEXT NOT NULL DEFAULT '{}', transcription TEXT NOT NULL DEFAULT '',
          approved_at TEXT, approved_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
          UNIQUE(tenant_id,project_id,version_no)
        );
        CREATE TABLE IF NOT EXISTS baseline_revisions (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          version_no INTEGER NOT NULL, plan_version_id TEXT REFERENCES plan_versions(id), activities_json TEXT NOT NULL,
          approved_at TEXT NOT NULL, approved_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
          UNIQUE(tenant_id,project_id,version_no)
        );
        CREATE TABLE IF NOT EXISTS daily_updates (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          activity_id TEXT NOT NULL, area_id TEXT REFERENCES project_areas(id), sub_area_id TEXT REFERENCES project_areas(id),
          work_date TEXT NOT NULL, progress REAL, quantity_completed REAL, status TEXT NOT NULL DEFAULT 'Submitted',
          payload_json TEXT NOT NULL, submitted_by INTEGER REFERENCES users(id), reviewed_by INTEGER REFERENCES users(id),
          reviewed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_risks (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          activity_id TEXT, area_id TEXT REFERENCES project_areas(id), title TEXT NOT NULL, severity TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'Open', due_date TEXT, owner TEXT NOT NULL DEFAULT '',
          payload_json TEXT NOT NULL, created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, resolved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS project_milestones (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          activity_id TEXT, name TEXT NOT NULL, planned_date TEXT NOT NULL, actual_date TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'Planned', dependencies_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
          created_by INTEGER REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS notifications (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
          recipient_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, kind TEXT NOT NULL, title TEXT NOT NULL,
          message TEXT NOT NULL, link TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, read_at TEXT
        );
        CREATE TABLE IF NOT EXISTS execution_records (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          kind TEXT NOT NULL CHECK(kind IN ('scenario','recommendation','action','outcome','demo_seed')),
          parent_id TEXT, status TEXT NOT NULL, payload_json TEXT NOT NULL,
          created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS areas_project_idx ON project_areas(tenant_id,project_id,parent_id);
        CREATE INDEX IF NOT EXISTS documents_project_idx ON project_documents(tenant_id,project_id,uploaded_at DESC);
        CREATE INDEX IF NOT EXISTS plan_versions_project_idx ON plan_versions(tenant_id,project_id,version_no DESC);
        CREATE INDEX IF NOT EXISTS daily_updates_project_date_idx ON daily_updates(tenant_id,project_id,work_date DESC);
        CREATE INDEX IF NOT EXISTS daily_updates_activity_idx ON daily_updates(tenant_id,project_id,activity_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS risks_project_status_idx ON project_risks(tenant_id,project_id,status,severity);
        CREATE INDEX IF NOT EXISTS notifications_recipient_idx ON notifications(tenant_id,recipient_id,read_at,created_at DESC);
        CREATE INDEX IF NOT EXISTS execution_records_project_idx ON execution_records(tenant_id,project_id,kind,status,created_at DESC);
        """
        if getattr(db, "is_postgres", False):
            schema = schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
            schema = schema.replace("username TEXT NOT NULL UNIQUE COLLATE NOCASE", "username TEXT NOT NULL UNIQUE")
            for column in ("user_id", "actor_id", "created_by", "uploaded_by", "approved_by", "submitted_by", "reviewed_by", "recipient_id", "manager_id"):
                schema = schema.replace(column + " INTEGER", column + " BIGINT")
        db.executescript(schema)
        if getattr(db, "is_postgres", False):
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower_idx ON users(LOWER(username))")
        user_columns = _table_columns(db, "users")
        if "last_login" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN last_login TEXT")
        if "account_status" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN account_status TEXT NOT NULL DEFAULT 'active'")
        if "approved" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN approved INTEGER NOT NULL DEFAULT 1")
        user_columns = _table_columns(db, "users")
        for column, declaration in (("company", "TEXT NOT NULL DEFAULT ''"), ("requested_project", "TEXT NOT NULL DEFAULT ''"), ("requested_role", "TEXT NOT NULL DEFAULT 'Viewer'")):
            if column not in user_columns:
                db.execute(f"ALTER TABLE users ADD COLUMN {column} {declaration}")
        audit_columns = _table_columns(db, "audit")
        if "source_ip" not in audit_columns:
            db.execute("ALTER TABLE audit ADD COLUMN source_ip TEXT")
        # Additive migration: existing v3 data belongs to the original workspace/project.
        columns_added = {}
        for table in ("users", "activities", "reports", "audit"):
            cols = _table_columns(db, table)
            columns_added[table] = "project_id" not in cols
            if "tenant_id" not in cols:
                db.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT '{DEFAULT_TENANT}'")
            if "project_id" not in cols:
                default = "" if table == "users" else DEFAULT_PROJECT
                db.execute(f"ALTER TABLE {table} ADD COLUMN project_id TEXT NOT NULL DEFAULT '{default}'")
        project_columns = _table_columns(db, "projects")
        project_migrations = {
            "code": "TEXT NOT NULL DEFAULT ''", "location": "TEXT NOT NULL DEFAULT ''",
            "project_type": "TEXT NOT NULL DEFAULT ''", "start_date": "TEXT NOT NULL DEFAULT ''",
            "planned_completion_date": "TEXT NOT NULL DEFAULT ''", "manager_id": "INTEGER",
            "description": "TEXT NOT NULL DEFAULT ''", "status": "TEXT NOT NULL DEFAULT 'Planning'",
        }
        for column, declaration in project_migrations.items():
            if column not in project_columns:
                db.execute(f"ALTER TABLE projects ADD COLUMN {column} {declaration}")
        self_migrate_user_roles(db)
        stamp = now_iso()
        db.execute("INSERT OR IGNORE INTO tenants(id,name,created_at) VALUES(?,?,?)", (DEFAULT_TENANT, "SiteLink Workspace", stamp))
        db.execute("INSERT OR IGNORE INTO projects(id,tenant_id,name,client,phase,created_at) VALUES(?,?,?,?,?,?)",
                   (DEFAULT_PROJECT, DEFAULT_TENANT, "Synthetic Process Facility", "Demo Infrastructure Group", "Phase 02", stamp))
        db.execute("UPDATE projects SET code='SYN-PF-001',location='Synthetic demonstration site',project_type='Industrial infrastructure demo',start_date='2026-09-01',planned_completion_date='2026-12-15',description='Synthetic project for demonstration and testing.',status='Active' WHERE id=? AND code=''", (DEFAULT_PROJECT,))
        db.execute("INSERT OR IGNORE INTO project_revisions(tenant_id,project_id,revision) VALUES(?,?,0)", (DEFAULT_TENANT, DEFAULT_PROJECT))
        if columns_added.get("activities"):
            db.execute("UPDATE activities SET activity_id=? || activity_id", (DEFAULT_PROJECT + "::",))
        if columns_added.get("reports"):
            db.execute("UPDATE reports SET report_id=? || report_id", (DEFAULT_PROJECT + "::",))
        db.execute("CREATE INDEX IF NOT EXISTS activities_project_idx ON activities(tenant_id,project_id)")
        db.execute("CREATE INDEX IF NOT EXISTS reports_project_idx ON reports(tenant_id,project_id)")
        db.execute("CREATE INDEX IF NOT EXISTS audit_project_idx ON audit(tenant_id,project_id,id DESC)")
        db.execute("INSERT OR IGNORE INTO workspace_meta(key,value) VALUES('revision','0')")
        db.commit()
        if db.execute("SELECT COUNT(*) AS n FROM activities").fetchone()["n"] == 0:
            seed_activities(db)
        if db.execute("SELECT COUNT(*) AS n FROM reports").fetchone()["n"] == 0:
            seed_reports(db)
        existing_baseline = db.execute("SELECT 1 FROM baseline_revisions WHERE tenant_id=? AND project_id=? LIMIT 1", (DEFAULT_TENANT, DEFAULT_PROJECT)).fetchone()
        if not existing_baseline:
            seeded = [json.loads(row[0]) for row in db.execute("SELECT payload FROM activities WHERE tenant_id=? AND project_id=? ORDER BY activity_id", (DEFAULT_TENANT, DEFAULT_PROJECT))]
            if seeded:
                db.execute("INSERT INTO baseline_revisions(id,tenant_id,project_id,version_no,activities_json,approved_at,approved_by,created_at) VALUES(?,?,?,?,?,?,?,?)", ("baseline-" + uuid.uuid4().hex[:12], DEFAULT_TENANT, DEFAULT_PROJECT, 1, json.dumps(seeded, ensure_ascii=False), stamp, None, stamp))
                db.commit()


def seed_activities(db: sqlite3.Connection) -> None:
    path = ROOT / "sample-data" / "project_schedule.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            payload = {
                "id": row.get("activity_id", ""), "wbs": row.get("wbs", ""),
                "name": row.get("activity_name", ""), "discipline": row.get("discipline", ""),
                "location": row.get("location", ""), "plannedStart": row.get("planned_start", ""),
                "plannedFinish": row.get("planned_finish", ""), "actualStart": row.get("actual_start", ""),
                "actualFinish": row.get("actual_finish", ""),
                "progress": int(row.get("percent_complete") or 0),
                "plannedProgress": int(row.get("planned_progress") or 0),
                "owner": row.get("owner", ""), "status": row.get("status", "Not started"),
                "predecessors": [value.strip() for value in row.get("predecessors", "").replace(";", ",").split(",") if value.strip()],
                "aliases": [value.strip() for value in row.get("aliases", "").split(";") if value.strip()],
                "contractor": row.get("contractor", ""),
                "updateCadenceHours": int(row.get("update_cadence_hours") or 48),
            }
            if payload["id"]:
                db.execute("INSERT OR IGNORE INTO activities(activity_id,payload,tenant_id,project_id) VALUES(?,?,?,?)",
                           (DEFAULT_PROJECT + "::" + payload["id"], json.dumps(payload, ensure_ascii=False), DEFAULT_TENANT, DEFAULT_PROJECT))


def seed_reports(db: sqlite3.Connection) -> None:
    path = ROOT / "sample-data" / "progress_updates.csv"
    stamp = now_iso()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=1):
            report_id = row.get("report_id") or f"RPT-DEMO-{index:03d}"
            progress = row.get("progress", "")
            payload = {
                "id": report_id, "date": row.get("report_date", ""),
                "discipline": row.get("discipline", ""), "text": row.get("text", ""),
                "reporter": row.get("reporter", "Synthetic sample supervisor"),
                "source": "sample-data/progress_updates.csv", "sourceType": "spreadsheet",
                "actualStart": row.get("actual_start", ""), "actualEnd": row.get("actual_end", ""),
                "progress": int(progress) if progress else "", "status": "pending",
                "activityId": "", "sourceActivityId": row.get("activity_id", ""),
                "suggestedActivityId": row.get("activity_id", ""), "note": "Synthetic report included for demo. Review before accepting.",
                "isDefault": True, "archived": False, "createdBy": "Sample data",
                "createdAt": stamp, "createdByRole": "System",
            }
            db.execute("INSERT OR IGNORE INTO reports(report_id,payload,archived,created_at,updated_at,tenant_id,project_id) VALUES(?,?,?,?,?,?,?)",
                       (DEFAULT_PROJECT + "::" + report_id, json.dumps(payload, ensure_ascii=False), 0, stamp, stamp, DEFAULT_TENANT, DEFAULT_PROJECT))
            db.execute("INSERT INTO audit(happened_at,actor_name,actor_role,action,record_id,source_location,reason,what,after_json,tenant_id,project_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (stamp, "System demo", "System", "Added default sample report", report_id,
                        "sample-data/progress_updates.csv", "Seeded synthetic example so the prototype opens with demo data.",
                        payload.get("text", "")[:220], json.dumps(payload, ensure_ascii=False), DEFAULT_TENANT, DEFAULT_PROJECT))
    db.commit()


def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    value = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return salt.hex(), value.hex()


def validate_password(password: str) -> None:
    if not 6 <= len(password) <= 8:
        raise ValueError("Password must be 6–8 characters.")


class Handler(SimpleHTTPRequestHandler):
    server_version = "SiteLink/4.0"
    sys_version = ""

    def log_message(self, fmt: str, *args: object) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def _client_ip(self) -> str | None:
        address = self.client_address[0] if self.client_address else None
        if os.environ.get("TRUST_PROXY_HEADERS", "0").strip().lower() in {"1", "true", "yes"}:
            forwarded = self.headers.get("X-Forwarded-For", "")
            candidate = forwarded.split(",")[-1].strip() if forwarded else ""
            try:
                if candidate:
                    return str(ipaddress.ip_address(candidate))
            except ValueError:
                pass
        return address

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        super().end_headers()

    def translate_path(self, path: str) -> str:
        parsed = urlparse(path)
        clean = unquote(parsed.path).lstrip("/") or "index.html"
        if clean == "data" or clean.startswith("data/") or clean == "data\\" or clean.startswith("data\\"):
            return str(ROOT / "missing-private-file")
        candidate = (ROOT / clean).resolve()
        try:
            candidate.relative_to(ROOT)
        except ValueError:
            return str(ROOT / "404.html")
        try:
            candidate.relative_to(DATA_DIR.resolve())
            return str(ROOT / "missing-private-file")
        except ValueError:
            pass
        return str(candidate)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/") or path == "/health":
            self.api_get(path)
            return
        if path == "/" or path.endswith(".html"):
            self._serve_shell()
            return
        super().do_GET()

    def _serve_shell(self) -> None:
        self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        self.api_write("POST")

    def do_PUT(self) -> None:
        self.api_write("PUT")

    def do_PATCH(self) -> None:
        self.api_write("PATCH")

    def do_DELETE(self) -> None:
        self.api_write("DELETE")

    def _json(self, status: int, value: object, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for key, val in (headers or {}).items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY:
            raise ValueError("Request is too large.")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object.")
        return data

    def _session(self) -> tuple[sqlite3.Row | None, str | None, str | None]:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("sitelink_session")
        if not morsel:
            return None, None, None
        token = morsel.value
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with connect() as db:
            row = db.execute("SELECT s.csrf_token,s.expires_at,u.id,u.username,u.display_name,u.role,u.active,u.tenant_id,u.account_status,u.approved FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (token_hash,)).fetchone()
            if not row or not row["active"] or row["account_status"] != "active" or not row["approved"] or row["expires_at"] < now_iso():
                db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
                return None, None, None
            return row, row["csrf_token"], token_hash

    def _require(self, roles: set[str] | None = None, csrf: bool = False) -> tuple[sqlite3.Row, str, str]:
        user, token, token_hash = self._session()
        if not user:
            raise PermissionError("Please sign in again.")
        if roles and user["role"] not in roles:
            raise PermissionError("Your account does not have permission for this action.")
        if csrf and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), token or ""):
            raise PermissionError("Security token expired. Refresh and sign in again.")
        return user, token or "", token_hash or ""

    def _cookie(self, token: str, clear: bool = False) -> str:
        secure_setting = os.environ.get("COOKIE_SECURE", "auto").strip().lower()
        use_secure = secure_setting in {"1", "true", "yes", "on"} or (secure_setting == "auto" and self.headers.get("X-Forwarded-Proto", "").lower() == "https")
        secure = "; Secure" if use_secure else ""
        if clear:
            return "sitelink_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" + secure
        return "sitelink_session=" + token + "; Path=/; HttpOnly; SameSite=Strict; Max-Age=" + str(SESSION_HOURS * 3600) + secure

    def _public_user(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "username": row["username"], "name": row["display_name"], "role": row["role"]}

    def _user_json(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "username": row["username"], "name": row["display_name"],
                "role": row["role"], "active": bool(row["active"]),
                "approved": bool(row["approved"]) if "approved" in row.keys() else True,
                "status": (("Rejected" if row["account_status"] == "rejected" else "Disabled" if not row["active"] else str(row["account_status"]).title()) if "account_status" in row.keys() else ("Active" if row["active"] else "Disabled")),
                "createdAt": row["created_at"], "lastLogin": row["last_login"],
                "company": row["company"] if "company" in row.keys() else "",
                "requestedProject": row["requested_project"] if "requested_project" in row.keys() else "",
                "requestedRole": row["requested_role"] if "requested_role" in row.keys() else "Viewer"}

    def _permissions(self, role: str, raw: str | None = None) -> dict:
        base = dict(ROLE_PERMISSIONS.get(role, {}))
        if raw:
            try:
                stored = json.loads(raw)
                for key in base:
                    # Membership rows can narrow a role; only Supervisor approval may be explicitly granted.
                    if key in stored:
                        base[key] = bool(stored[key]) and (role != "Viewer" or key == "view")
            except (TypeError, ValueError):
                pass
        return base

    def _project_access(self, db: sqlite3.Connection, user: sqlite3.Row, project_id: str | None = None) -> tuple[sqlite3.Row, dict]:
        if project_id:
            project = db.execute("SELECT * FROM projects WHERE id=? AND tenant_id=? AND active=1", (project_id, user["tenant_id"])).fetchone()
        else:
            project = None
            if user["role"] != "Admin":
                project = db.execute("SELECT p.* FROM projects p JOIN project_members m ON m.project_id=p.id WHERE p.tenant_id=? AND p.active=1 AND m.user_id=? ORDER BY p.created_at,p.name LIMIT 1", (user["tenant_id"], user["id"])).fetchone()
            if project is None:
                project = db.execute("SELECT * FROM projects WHERE tenant_id=? AND active=1 ORDER BY created_at,name LIMIT 1", (user["tenant_id"],)).fetchone()
        if not project:
            raise PermissionError("You do not have permission to access this page.")
        if user["role"] == "Admin":
            return project, {"view": True, "submit": True, "review": True, "approve": True, "schedule_edit": True, "manage": True}
        membership = db.execute("SELECT permissions FROM project_members WHERE project_id=? AND user_id=? AND tenant_id=?", (project["id"], user["id"], user["tenant_id"])).fetchone()
        if not membership:
            raise PermissionError("You do not have permission to access this page.")
        permissions = self._permissions(user["role"], membership["permissions"])
        if not permissions.get("view"):
            raise PermissionError("You do not have permission to access this page.")
        return project, permissions

    def _storage_id(self, project_id: str, record_id: str) -> str:
        return project_id + "::" + record_id

    def api_get(self, path: str) -> None:
        try:
            if path == "/health":
                with connect() as db:
                    db.execute("SELECT 1").fetchone()
                return self._json(200, {"ok": True, "service": "sitelink"})
            if path == "/api/execution-intelligence":
                user, _, _ = self._require()
                project_id = (parse_qs(urlparse(self.path).query).get("projectId") or [None])[0]
                return self._json(200, self._execution_data(user, project_id))
            if path == "/api/project-control":
                user, _, _ = self._require()
                project_id = (parse_qs(urlparse(self.path).query).get("projectId") or [None])[0]
                return self._json(200, self._control_data(user, project_id))
            if path.startswith("/api/documents/") and path.endswith("/download"):
                return self._read_document(path.split("/")[3])
            if path == "/api/health":
                with connect() as db:
                    db.execute("SELECT 1").fetchone()
                return self._json(200, {"ok": True})
            if path == "/api/ai/status":
                user, _, _ = self._require()
                project_id = (parse_qs(urlparse(self.path).query).get("projectId") or [None])[0]
                with connect() as db:
                    project, permissions = self._project_access(db, user, project_id)
                if not permissions.get("view"):
                    raise PermissionError("Your account cannot view this project.")
                provider = configured_ai_provider()
                return self._json(200, {"configured": bool(provider), "provider": provider.name if provider else "Local deterministic engine",
                                        "modelConfigured": bool(getattr(provider, "model", "")), "liveChecked": False, "readOnly": True,
                                        "projectId": project["id"]})
            if path in {"/api/session", "/api/auth/me"}:
                user, csrf, _ = self._session()
                with connect() as db:
                    setup = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
                return self._json(200, {"user": self._public_user(user) if user else None, "csrf": csrf, "setupRequired": setup,
                                        "signupEnabled": os.environ.get("PUBLIC_SIGNUP", "1").strip().lower() in {"1", "true", "yes"},
                                        "setupTokenRequired": os.environ.get("REQUIRE_SETUP_TOKEN", "0").strip().lower() in {"1", "true", "yes"}})
            if path == "/api/state":
                user, _, _ = self._require()
                project_id = (parse_qs(urlparse(self.path).query).get("projectId") or [None])[0]
                return self._json(200, self._state(user, project_id))
            if path == "/api/users":
                user, _, _ = self._require({"Admin"})
                with connect() as db:
                    rows = db.execute("SELECT id,username,display_name,role,active,account_status,approved,created_at,last_login,company,requested_project,requested_role FROM users WHERE tenant_id=? ORDER BY id", (user["tenant_id"],)).fetchall()
                return self._json(200, {"users": [self._user_json(r) for r in rows]})
            if path.startswith("/api/users/"):
                user, _, _ = self._require({"Admin"})
                try: user_id = int(path.rsplit("/", 1)[-1])
                except ValueError: raise ValueError("Invalid user ID.")
                with connect() as db:
                    row = db.execute("SELECT id,username,display_name,role,active,account_status,approved,created_at,last_login,company,requested_project,requested_role FROM users WHERE id=? AND tenant_id=?", (user_id, user["tenant_id"])).fetchone()
                if not row: raise ValueError("User account not found.")
                return self._json(200, {"user": self._user_json(row)})
            if path == "/api/projects":
                user, _, _ = self._require()
                with connect() as db:
                    if user["role"] == "Admin":
                        rows = db.execute("SELECT p.* FROM projects p WHERE p.tenant_id=? AND p.active=1 ORDER BY p.created_at,p.name", (user["tenant_id"],)).fetchall()
                    else:
                        rows = db.execute("SELECT p.*,m.permissions FROM projects p JOIN project_members m ON m.project_id=p.id WHERE p.tenant_id=? AND p.active=1 AND m.user_id=? ORDER BY p.created_at,p.name", (user["tenant_id"], user["id"])).fetchall()
                visible = [r for r in rows if user["role"] == "Admin" or self._permissions(user["role"], r["permissions"]).get("view")]
                return self._json(200, {"projects": [self._project_json(r) for r in visible]})
            if path.startswith("/api/projects/") and path.endswith("/members"):
                user, _, _ = self._require({"Admin"})
                project_id = path.split("/")[3]
                with connect() as db:
                    project, _ = self._project_access(db, user, project_id)
                    rows = db.execute("SELECT u.id,u.username,u.display_name,u.role,u.active,u.created_at,u.last_login,m.permissions FROM project_members m JOIN users u ON u.id=m.user_id WHERE m.project_id=? AND m.tenant_id=? ORDER BY u.display_name", (project_id, user["tenant_id"])).fetchall()
                return self._json(200, {"project": self._project_json(project), "members": [{**self._user_json(r), "permissions": self._permissions(r["role"], r["permissions"])} for r in rows]})
            if path.startswith("/api/projects/"):
                user, _, _ = self._require()
                project_id = path.rsplit("/", 1)[-1]
                with connect() as db:
                    project, permissions = self._project_access(db, user, project_id)
                return self._json(200, {"project": self._project_json(project), "permissions": permissions})
            if path in {"/api/reports", "/api/dashboard", "/api/audit"}:
                user, _, _ = self._require()
                project_id = (parse_qs(urlparse(self.path).query).get("projectId") or [None])[0]
                data = self._state(user, project_id)
                if path == "/api/reports": return self._json(200, {"reports": data["reports"]})
                if path == "/api/audit": return self._json(200, {"audit": data["audit"]})
                return self._json(200, {"activities": data["activities"], "reports": data["reports"], "intelligence": data["intelligence"]})
            if path == "/api/export/msproject.xml":
                user, _, _ = self._require()
                project_id = (parse_qs(urlparse(self.path).query).get("projectId") or [None])[0]
                with connect() as db:
                    project, permissions = self._project_access(db, user, project_id)
                    if not permissions.get("view"):
                        raise PermissionError("You do not have permission to export this schedule.")
                    activities = [json.loads(row[0]) for row in db.execute("SELECT payload FROM activities WHERE tenant_id=? AND project_id=? ORDER BY activity_id", (user["tenant_id"], project["id"]))]
                content = export_mspdi_xml(activities, project["name"])
                self.send_response(200)
                self.send_header("Content-Type", "application/xml; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="sitelink-msproject-adapter.xml"')
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(content)
                return
            if path == "/api/intelligence":
                user, _, _ = self._require()
                data = self._state(user)
                return self._json(200, data["intelligence"])
            self._json(404, {"error": "Not found."})
        except PermissionError as exc:
            self._json(401 if "sign in" in str(exc).lower() else 403, {"error": str(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": "Server could not read this request.", "detail": str(exc)})

    def _control_data(self, user: sqlite3.Row, project_id: str | None = None) -> dict:
        today = datetime.now(timezone.utc).date().isoformat()
        with connect() as db:
            project, permissions = self._project_access(db, user, project_id)
            tenant_id, pid = user["tenant_id"], project["id"]
            activities = [json.loads(r[0]) for r in db.execute("SELECT payload FROM activities WHERE tenant_id=? AND project_id=? ORDER BY activity_id", (tenant_id, pid))]
            areas = [dict(r) for r in db.execute("SELECT id,parent_id,level,name,code,description,created_at FROM project_areas WHERE tenant_id=? AND project_id=? ORDER BY level,name", (tenant_id, pid))]
            plans = []
            for row in db.execute("SELECT p.*,d.file_name,d.mime_type,d.file_size,d.uploaded_at,d.uploaded_by,d.id AS doc_id FROM plan_versions p JOIN project_documents d ON d.id=p.document_id WHERE p.tenant_id=? AND p.project_id=? ORDER BY p.version_no DESC", (tenant_id, pid)):
                uploader = db.execute("SELECT display_name FROM users WHERE id=?", (row["uploaded_by"],)).fetchone() if row["uploaded_by"] else None
                plans.append({"id": row["id"], "documentId": row["doc_id"], "version": row["version_no"], "fileName": row["file_name"], "fileType": row["mime_type"], "fileSize": row["file_size"], "uploadedAt": row["uploaded_at"], "uploadedBy": uploader["display_name"] if uploader else "—", "processingStatus": row["processing_status"], "analysisStatus": row["analysis_status"], "analysis": json.loads(row["analysis_json"] or "{}"), "transcription": row["transcription"], "approvedAt": row["approved_at"]})
            baselines = [{"id": r["id"], "version": r["version_no"], "planVersionId": r["plan_version_id"], "approvedAt": r["approved_at"], "approvedBy": (db.execute("SELECT display_name FROM users WHERE id=?", (r["approved_by"],)).fetchone() or {"display_name": "System"})["display_name"], "activityCount": len(json.loads(r["activities_json"]))} for r in db.execute("SELECT * FROM baseline_revisions WHERE tenant_id=? AND project_id=? ORDER BY version_no DESC", (tenant_id, pid))]
            updates = []
            for row in db.execute("SELECT * FROM daily_updates WHERE tenant_id=? AND project_id=? ORDER BY work_date DESC,created_at DESC LIMIT 1000", (tenant_id, pid)):
                item = json.loads(row["payload_json"])
                item.update({"id": row["id"], "activityId": row["activity_id"], "areaId": row["area_id"] or "", "subAreaId": row["sub_area_id"] or "", "workDate": row["work_date"], "progress": row["progress"], "quantityCompleted": row["quantity_completed"], "status": row["status"], "submittedAt": row["created_at"], "reviewedAt": row["reviewed_at"]})
                submitted = db.execute("SELECT display_name FROM users WHERE id=?", (row["submitted_by"],)).fetchone() if row["submitted_by"] else None
                reviewer = db.execute("SELECT display_name FROM users WHERE id=?", (row["reviewed_by"],)).fetchone() if row["reviewed_by"] else None
                item["submittedBy"] = submitted["display_name"] if submitted else "—"
                item["reviewedBy"] = reviewer["display_name"] if reviewer else ""
                updates.append(item)
            risks = []
            for row in db.execute("SELECT * FROM project_risks WHERE tenant_id=? AND project_id=? ORDER BY CASE severity WHEN 'Critical' THEN 0 WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END,created_at DESC", (tenant_id, pid)):
                item = json.loads(row["payload_json"])
                item.update({"id": row["id"], "activityId": row["activity_id"] or "", "areaId": row["area_id"] or "", "title": row["title"], "severity": row["severity"], "status": row["status"], "dueDate": row["due_date"] or "", "owner": row["owner"], "createdAt": row["created_at"], "updatedAt": row["updated_at"], "resolvedAt": row["resolved_at"] or ""})
                risks.append(item)
            milestones = [json.loads(r["payload_json"]) for r in db.execute("SELECT payload_json FROM project_milestones WHERE tenant_id=? AND project_id=? ORDER BY planned_date", (tenant_id, pid))] if "payload_json" in _table_columns(db, "project_milestones") else []
            # Older installations created the normalized milestone table before its presentation payload column.
            if not milestones:
                milestones = [{"id": r["id"], "activityId": r["activity_id"] or "", "name": r["name"], "plannedDate": r["planned_date"], "actualDate": r["actual_date"], "status": r["status"], "dependencies": json.loads(r["dependencies_json"] or "[]")} for r in db.execute("SELECT * FROM project_milestones WHERE tenant_id=? AND project_id=? ORDER BY planned_date", (tenant_id, pid))]
            documents = []
            for row in db.execute("SELECT id,category,file_name,mime_type,file_size,version_no,area_id,activity_id,uploaded_at,uploaded_by,metadata_json FROM project_documents WHERE tenant_id=? AND project_id=? ORDER BY uploaded_at DESC LIMIT 300", (tenant_id, pid)):
                uploader = db.execute("SELECT display_name FROM users WHERE id=?", (row["uploaded_by"],)).fetchone() if row["uploaded_by"] else None
                documents.append({"id": row["id"], "category": row["category"], "fileName": row["file_name"], "fileType": row["mime_type"], "fileSize": row["file_size"], "version": row["version_no"], "areaId": row["area_id"] or "", "activityId": row["activity_id"] or "", "uploadedAt": row["uploaded_at"], "uploadedBy": uploader["display_name"] if uploader else "—", "metadata": json.loads(row["metadata_json"] or "{}")})
            notifications = [{"id": r["id"], "kind": r["kind"], "title": r["title"], "message": r["message"], "link": r["link"], "createdAt": r["created_at"], "readAt": r["read_at"] or ""} for r in db.execute("SELECT * FROM notifications WHERE tenant_id=? AND recipient_id=? AND (project_id=? OR project_id IS NULL) ORDER BY created_at DESC LIMIT 100", (tenant_id, user["id"], pid))]
            updates_by_activity: dict[str, list[dict]] = {}
            for item in updates:
                if item["status"] == "Approved":
                    updates_by_activity.setdefault(item["activityId"], []).append(item)
            risks_by_activity: dict[str, list[dict]] = {}
            for risk in risks:
                if risk["status"] in {"Open", "Monitoring"}:
                    risks_by_activity.setdefault(risk["activityId"], []).append(risk)
            missing, condition_rows = [], []
            for activity in activities:
                activity_id = str(activity.get("id") or "")
                history = updates_by_activity.get(activity_id, [])
                latest_day = max((str(u.get("workDate") or "") for u in history), default="")
                try:
                    days_missing = max(0, (date.fromisoformat(today) - date.fromisoformat(latest_day)).days) if latest_day else max(0, (date.fromisoformat(today) - date.fromisoformat(str(activity.get("plannedStart") or today)[:10])).days)
                except ValueError:
                    days_missing = 0
                cadence = max(1, int(activity.get("updateCadenceHours") or 48))
                if float(activity.get("progress") or 0) < 100 and days_missing * 24 >= cadence:
                    missing.append({"activityId": activity_id, "activityName": activity.get("name", ""), "area": activity.get("area", ""), "owner": activity.get("owner", ""), "lastUpdate": latest_day or "No approved daily update", "daysMissing": days_missing, "severity": "High" if days_missing >= 3 else "Medium"})
                condition_rows.append({"activityId": activity_id, **activity_condition(activity, today, risks_by_activity.get(activity_id, []), days_missing)})
            weighted = weighted_progress(activities)
            area_summaries = []
            for area in areas:
                if area["level"] != "area":
                    continue
                children = [a for a in activities if a.get("areaId") == area["id"] or str(a.get("area", "")).casefold() == area["name"].casefold()]
                if children:
                    progress = weighted_progress(children)
                    area_summaries.append({"id": area["id"], "name": area["name"], "progress": progress["actual"], "activityCount": len(children), "condition": next((c["status"] for c in condition_rows if any(a.get("id") == c["activityId"] for a in children) and c["status"] in {"Delayed", "Blocked", "At Risk"}), "On Track")})
            today_updates = [u for u in updates if u["workDate"] == today]
            approved_today = [u for u in today_updates if u["status"] == "Approved"]
            completed_work = sum(float(u.get("quantityCompleted") or 0) for u in approved_today)
            manpower = sum(int(u.get("manpower") or 0) for u in approved_today)
            conditions = {c["activityId"]: c["status"] for c in condition_rows}
            condition_counts = {name: sum(1 for value in conditions.values() if value == name) for name in CONDITIONS}
            current_baseline = baselines[0] if baselines else None
            project_json = self._project_json(project)
            permissions_json = permissions
        newest = sorted(updates, key=lambda u: (u.get("workDate", ""), u.get("submittedAt", "")), reverse=True)
        forecast = self._forecast(activities, updates, today)
        return {"project": project_json, "permissions": permissions_json, "areas": areas, "plans": plans, "baselines": baselines,
                "currentBaseline": current_baseline, "documents": documents, "activities": activities, "dailyUpdates": updates,
                "risks": risks, "milestones": milestones, "notifications": notifications, "conditions": condition_rows,
                "missingUpdates": sorted(missing, key=lambda x: x["daysMissing"], reverse=True), "areaSummary": area_summaries,
                "progress": weighted, "conditionCounts": condition_counts, "forecast": forecast,
                "today": {"date": today, "plannedActivities": sum(1 for a in activities if (a.get("plannedStart") or "") <= today <= (a.get("plannedFinish") or "9999-12-31")), "updatesSubmitted": len(today_updates), "updatesApproved": len(approved_today), "quantityCompleted": round(completed_work, 2), "manpower": manpower, "missingUpdates": len(missing), "newRisks": sum(1 for r in risks if str(r.get("createdAt", ""))[:10] == today), "delayed": condition_counts.get("Delayed", 0), "blocked": condition_counts.get("Blocked", 0), "atRisk": condition_counts.get("At Risk", 0), "materialIssues": sum(1 for u in newest if u.get("workDate") == today and u.get("materialAvailability") in {"Pending", "Partially available"})}}

    def _forecast(self, activities: list[dict], updates: list[dict], today: str) -> dict:
        approved = sorted((u for u in updates if u.get("status") == "Approved" and u.get("progress") is not None), key=lambda x: (x.get("workDate", ""), x.get("submittedAt", "")))
        points = []
        for item in approved:
            try: points.append((date.fromisoformat(item["workDate"]), float(item["progress"])))
            except (ValueError, TypeError): pass
        if len(points) < 2:
            return {"status": "Insufficient approved history", "estimatedCompletion": None, "dailyRate": None, "basisRecords": len(points), "label": "Estimate only"}
        first_day, first_progress = points[0]
        last_day, last_progress = points[-1]
        days = (last_day - first_day).days
        rate = max(0.0, (last_progress - first_progress) / days) if days > 0 else 0.0
        current = weighted_progress(activities)["actual"]
        if rate <= 0 or current >= 100:
            return {"status": "No positive historical rate", "estimatedCompletion": None, "dailyRate": round(rate, 3), "basisRecords": len(points), "label": "Estimate only"}
        estimated = date.fromisoformat(today).toordinal() + int((100 - current) / rate + .999)
        return {"status": "Estimate based on approved history", "estimatedCompletion": date.fromordinal(estimated).isoformat(), "dailyRate": round(rate, 3), "basisRecords": len(points), "label": "Estimate only · not a schedule change"}

    def _control_notifications(self, db, user, project_id: str, kind: str, title: str, message: str, link: str) -> None:
        recipients = {int(user["id"])}
        for row in db.execute("SELECT id FROM users WHERE tenant_id=? AND role='Admin' AND active=1 AND approved=1", (user["tenant_id"],)):
            recipients.add(int(row["id"]))
        for row in db.execute("SELECT m.user_id,m.permissions,u.role FROM project_members m JOIN users u ON u.id=m.user_id WHERE m.tenant_id=? AND m.project_id=? AND u.active=1 AND u.account_status='active' AND u.approved=1", (user["tenant_id"], project_id)):
            try: grants = json.loads(row["permissions"])
            except (TypeError, ValueError): grants = {}
            if grants.get("review") or grants.get("approve") or row["role"] in {"Project Manager", "Planner"}:
                recipients.add(int(row["user_id"]))
        stamp = now_iso()
        for recipient in recipients:
            db.execute("INSERT INTO notifications(id,tenant_id,project_id,recipient_id,kind,title,message,link,created_at) VALUES(?,?,?,?,?,?,?,?,?)", ("ntf-" + uuid.uuid4().hex[:14], user["tenant_id"], project_id, recipient, kind, title[:140], message[:500], link[:300], stamp))

    def _decode_upload(self, body: dict) -> tuple[str, bytes]:
        name = Path(str(body.get("fileName", "upload"))).name
        encoded = body.get("fileBase64", "")
        if not isinstance(encoded, str) or not encoded or len(encoded) > (MAX_UPLOAD_BYTES * 4 // 3 + 32):
            raise ValueError("Choose a file smaller than 8 MB.")
        try: content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc: raise ValueError("The selected file could not be decoded.") from exc
        if not content or len(content) > MAX_UPLOAD_BYTES:
            raise ValueError("Files must be between 1 byte and 8 MB.")
        return name, content

    def _store_document(self, db, user, project_id: str, filename: str, content: bytes, category: str, area_id: str = "", activity_id: str = "", metadata: dict | None = None) -> dict:
        suffix = Path(filename).suffix.lower()
        if suffix not in {".csv", ".xlsx", ".xml", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".txt", ".docx", ".doc", ".xlsx"}:
            raise ValueError("Unsupported file type. Use CSV, XLSX, XML, PDF, image, or text documents.")
        allowed_mimes = {".csv": "text/csv", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xml": "application/xml", ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".tif": "image/tiff", ".tiff": "image/tiff", ".bmp": "image/bmp", ".webp": "image/webp", ".txt": "text/plain", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".doc": "application/msword"}
        safe_ext = suffix if suffix else ".bin"
        storage_key = uuid.uuid4().hex + safe_ext
        folder = (DATA_DIR / "uploads" / user["tenant_id"] / project_id).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / storage_key
        target.resolve().relative_to(folder)
        target.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        prior = db.execute("SELECT MAX(version_no) AS v FROM project_documents WHERE tenant_id=? AND project_id=? AND category=? AND file_name=?", (user["tenant_id"], project_id, category, filename)).fetchone()
        version = int(prior["v"] or 0) + 1
        doc_id, stamp = "doc-" + uuid.uuid4().hex[:14], now_iso()
        db.execute("INSERT INTO project_documents(id,tenant_id,project_id,category,file_name,mime_type,file_size,sha256,storage_key,version_no,area_id,activity_id,uploaded_at,uploaded_by,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (doc_id, user["tenant_id"], project_id, category, filename[:240], allowed_mimes[suffix], len(content), sha, storage_key, version, area_id or None, activity_id or None, stamp, user["id"], json.dumps(metadata or {}, ensure_ascii=False)))
        return {"id": doc_id, "fileName": filename, "category": category, "fileType": allowed_mimes[suffix], "fileSize": len(content), "version": version, "uploadedAt": stamp, "uploadedBy": user["display_name"]}

    def _create_plan_version(self, body: dict) -> None:
        user, _, _ = self._require({"Admin", "Project Manager", "Planner"}, csrf=True)
        project_id = str(body.get("projectId") or "").strip() or None
        reason = str(body.get("reason") or "").strip()
        self._need_reason(reason)
        filename, content = self._decode_upload(body)
        if Path(filename).suffix.lower() not in {".csv", ".xlsx", ".xml", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
            raise ValueError("Project plans must be CSV, XLSX, MS Project XML, PDF, or an image.")
        with connect() as db:
            project, permissions = self._project_access(db, user, project_id)
            if not permissions.get("schedule_edit") and user["role"] not in {"Admin", "Project Manager"}:
                raise PermissionError("Only a Planner, Project Manager, or Admin may upload a project plan.")
            next_version = int(db.execute("SELECT COALESCE(MAX(version_no),0)+1 AS v FROM plan_versions WHERE tenant_id=? AND project_id=?", (user["tenant_id"], project["id"])).fetchone()["v"])
            document = self._store_document(db, user, project["id"], filename, content, "plan", metadata={"planVersion": next_version})
            plan_id = "plan-" + uuid.uuid4().hex[:14]
            db.execute("INSERT INTO plan_versions(id,tenant_id,project_id,document_id,version_no,processing_status,analysis_status,analysis_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (plan_id, user["tenant_id"], project["id"], document["id"], next_version, "Uploaded", "Not analyzed", "{}", now_iso()))
            self._audit(db, user, "Uploaded project plan", plan_id, "Project plan versions", reason, None, {"filename": filename, "version": next_version, "sha256": hashlib.sha256(content).hexdigest()}, project_id=project["id"])
            self._bump_revision(db, project["id"], user["tenant_id"])
        self._json(201, {"plan": {"id": plan_id, "version": next_version, "documentId": document["id"], "fileName": filename, "analysisStatus": "Not analyzed"}})

    def _analyze_plan_version(self, plan_id: str, body: dict) -> None:
        user, _, _ = self._require({"Admin", "Project Manager", "Planner"}, csrf=True)
        project_id = str(body.get("projectId") or "").strip() or None
        with connect() as db:
            project, permissions = self._project_access(db, user, project_id)
            if not permissions.get("schedule_edit") and user["role"] not in {"Admin", "Project Manager"}:
                raise PermissionError("Only a Planner, Project Manager, or Admin may analyze a project plan.")
            row = db.execute("SELECT p.*,d.storage_key,d.file_name FROM plan_versions p JOIN project_documents d ON d.id=p.document_id WHERE p.id=? AND p.tenant_id=? AND p.project_id=?", (plan_id, user["tenant_id"], project["id"])).fetchone()
            if not row:
                raise ValueError("Plan version not found in this project.")
            base = (DATA_DIR / "uploads" / user["tenant_id"] / project["id"]).resolve()
            target = (base / row["storage_key"]).resolve()
            target.relative_to(base)
            content = target.read_bytes()
            result = analyze_plan_file(row["file_name"], content)
            encoded = json.dumps(result, ensure_ascii=False)
            status = "Extracted" if result.get("activities") else "Needs manual review"
            db.execute("UPDATE plan_versions SET processing_status='Processed',analysis_status=?,analysis_json=?,transcription=? WHERE id=? AND project_id=? AND tenant_id=?", (status, encoded, result.get("transcription", ""), plan_id, project["id"], user["tenant_id"]))
            self._audit(db, user, "Analyzed project plan", plan_id, "Project plan review", "Extracted schedule facts are unapproved proposals.", None, {"analysisStatus": status, "candidateCount": len(result.get("activities", [])), "analysisMethod": result.get("analysisMethod")}, project_id=project["id"])
            self._bump_revision(db, project["id"], user["tenant_id"])
        self._json(200, {"planId": plan_id, **result, "analysisStatus": status})

    def _ensure_area(self, db, user, project_id: str, name: str, level: str, parent_id: str | None = None) -> str | None:
        name = str(name or "").strip()
        if not name:
            return None
        if len(name) > 120 or level not in {"area", "sub_area"}:
            raise ValueError("Area names must be 1–120 characters.")
        if level == "sub_area":
            parent = db.execute("SELECT id,level FROM project_areas WHERE id=? AND tenant_id=? AND project_id=?", (parent_id, user["tenant_id"], project_id)).fetchone()
            if not parent or parent["level"] != "area":
                raise ValueError("Choose an area in this project before adding a sub-area.")
        key = name.casefold()
        for row in db.execute("SELECT id,name,parent_id,level FROM project_areas WHERE tenant_id=? AND project_id=? AND level=?", (user["tenant_id"], project_id, level)):
            if str(row["name"]).casefold() == key and (level == "area" or row["parent_id"] == parent_id):
                return row["id"]
        area_id = "area-" + uuid.uuid4().hex[:12]
        db.execute("INSERT INTO project_areas(id,tenant_id,project_id,parent_id,level,name,code,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?)", (area_id, user["tenant_id"], project_id, parent_id, level, name, None, now_iso(), user["id"]))
        return area_id

    def _approve_plan_version(self, plan_id: str, body: dict) -> None:
        user, _, _ = self._require({"Admin", "Project Manager", "Planner"}, csrf=True)
        project_id = str(body.get("projectId") or "").strip() or None
        reason = str(body.get("reason") or "").strip()
        self._need_reason(reason)
        proposals = body.get("activities")
        if not isinstance(proposals, list) or not proposals or len(proposals) > 2000:
            raise ValueError("Review at least one extracted activity before approving the baseline.")
        with connect() as db:
            project, permissions = self._project_access(db, user, project_id)
            if not permissions.get("schedule_edit") and user["role"] not in {"Admin", "Project Manager"}:
                raise PermissionError("Only a Planner, Project Manager, or Admin may approve a baseline.")
            plan = db.execute("SELECT * FROM plan_versions WHERE id=? AND tenant_id=? AND project_id=?", (plan_id, user["tenant_id"], project["id"])).fetchone()
            if not plan:
                raise ValueError("Plan version not found in this project.")
            if plan["analysis_status"] not in {"Extracted", "Needs manual review"}:
                raise ValueError("Analyze this plan version before approval.")
            prior = [json.loads(r[0]) for r in db.execute("SELECT payload FROM activities WHERE tenant_id=? AND project_id=?", (user["tenant_id"], project["id"]))]
            prior_by_id = {a["id"]: a for a in prior}
            proposal_ids: set[str] = set()
            normalized = []
            for raw in proposals:
                if not isinstance(raw, dict):
                    raise ValueError("Every baseline row must be an activity object.")
                item = normalize_schedule_row(raw)
                # Browser edits use camel-case fields; normalize_schedule_row also retains them.
                for target, source in (("id", "id"), ("name", "name"), ("wbs", "wbs"), ("area", "area"), ("subArea", "subArea"), ("workPackage", "workPackage"), ("plannedStart", "plannedStart"), ("plannedFinish", "plannedFinish"), ("discipline", "discipline"), ("location", "location"), ("plannedQuantity", "plannedQuantity"), ("unit", "unit"), ("plannedProgress", "plannedProgress"), ("owner", "owner"), ("contractor", "contractor"), ("weight", "weight"), ("isMilestone", "isMilestone")):
                    if source in raw:
                        item[target] = raw[source]
                item["id"], item["name"] = str(item.get("id") or "").strip(), str(item.get("name") or "").strip()
                if not item["id"] or len(item["id"]) > 100 or not item["name"] or len(item["name"]) > 240:
                    raise ValueError("Each activity needs an ID (up to 100 characters) and name (up to 240 characters).")
                if item["id"] in proposal_ids:
                    raise ValueError("Activity IDs must be unique within a baseline.")
                proposal_ids.add(item["id"])
                item["plannedStart"] = iso_date(item.get("plannedStart"), "Planned start")
                item["plannedFinish"] = iso_date(item.get("plannedFinish"), "Planned finish")
                if item["plannedStart"] and item["plannedFinish"] and item["plannedFinish"] < item["plannedStart"]:
                    raise ValueError(f"Planned finish cannot precede planned start for {item['id']}.")
                item["plannedQuantity"] = numeric(item.get("plannedQuantity"), "Planned quantity", 0)
                item["plannedProgress"] = numeric(item.get("plannedProgress"), "Planned progress", 0, 100) or 0
                item["weight"] = numeric(item.get("weight"), "Activity weight", 0)
                existing = prior_by_id.get(item["id"], {})
                area_id = self._ensure_area(db, user, project["id"], item.get("area", ""), "area")
                sub_area_id = self._ensure_area(db, user, project["id"], item.get("subArea", ""), "sub_area", area_id) if area_id else None
                activity = {**existing, "id": item["id"], "wbs": str(item.get("wbs") or existing.get("wbs", "")), "name": item["name"], "area": str(item.get("area") or ""), "areaId": area_id or "", "subArea": str(item.get("subArea") or ""), "subAreaId": sub_area_id or "", "workPackage": str(item.get("workPackage") or ""), "discipline": str(item.get("discipline") or "Unassigned"), "location": str(item.get("location") or ""), "plannedStart": item["plannedStart"], "plannedFinish": item["plannedFinish"], "durationDays": item.get("durationDays"), "plannedQuantity": item["plannedQuantity"], "unit": str(item.get("unit") or ""), "plannedProgress": item["plannedProgress"], "weight": item["weight"], "owner": str(item.get("owner") or ""), "contractor": str(item.get("contractor") or ""), "predecessors": item.get("predecessors") if isinstance(item.get("predecessors"), list) else [], "aliases": item.get("aliases") if isinstance(item.get("aliases"), list) else [], "isMilestone": bool(item.get("isMilestone")), "actualStart": existing.get("actualStart", ""), "actualFinish": existing.get("actualFinish", ""), "progress": float(existing.get("progress") or 0), "quantityCompleted": float(existing.get("quantityCompleted") or 0), "status": existing.get("status", "Not started")}
                normalized.append(activity)
            all_ids = set(prior_by_id) | proposal_ids
            for item in normalized:
                missing_deps = [str(dep) for dep in item.get("predecessors", []) if str(dep) not in all_ids]
                if missing_deps:
                    raise ValueError(f"Activity {item['id']} references unknown dependencies: {', '.join(missing_deps)}.")
            active_activities = dict(prior_by_id)
            active_activities.update({item["id"]: item for item in normalized})
            before_baseline = prior
            for activity in normalized:
                storage = self._storage_id(project["id"], activity["id"])
                old = prior_by_id.get(activity["id"])
                if old is None:
                    db.execute("INSERT INTO activities(activity_id,payload,tenant_id,project_id) VALUES(?,?,?,?)", (storage, json.dumps(activity, ensure_ascii=False), user["tenant_id"], project["id"]))
                else:
                    db.execute("UPDATE activities SET payload=? WHERE activity_id=? AND tenant_id=? AND project_id=?", (json.dumps(activity, ensure_ascii=False), storage, user["tenant_id"], project["id"]))
            approved_at = now_iso()
            baseline_version = int(db.execute("SELECT COALESCE(MAX(version_no),0)+1 AS v FROM baseline_revisions WHERE tenant_id=? AND project_id=?", (user["tenant_id"], project["id"])).fetchone()["v"])
            snapshot = list(active_activities.values())
            baseline_id = "baseline-" + uuid.uuid4().hex[:12]
            db.execute("INSERT INTO baseline_revisions(id,tenant_id,project_id,version_no,plan_version_id,activities_json,approved_at,approved_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (baseline_id, user["tenant_id"], project["id"], baseline_version, plan_id, json.dumps(snapshot, ensure_ascii=False), approved_at, user["id"], approved_at))
            db.execute("UPDATE plan_versions SET approved_at=?,approved_by=?,analysis_status='Approved baseline' WHERE id=? AND tenant_id=? AND project_id=?", (approved_at, user["id"], plan_id, user["tenant_id"], project["id"]))
            self._audit(db, user, "Approved baseline version", baseline_id, "Baseline schedule", reason, {"activityCount": len(before_baseline)}, {"version": baseline_version, "planVersionId": plan_id, "activityCount": len(snapshot), "approvedAt": approved_at}, project_id=project["id"])
            self._bump_revision(db, project["id"], user["tenant_id"])
        self._json(200, {"ok": True, "baselineId": baseline_id, "baselineVersion": baseline_version, "activityCount": len(snapshot), "activities": snapshot})

    def _create_area(self, body: dict) -> None:
        user, _, _ = self._require({"Admin", "Project Manager", "Planner"}, csrf=True)
        project_id = str(body.get("projectId") or "").strip() or None
        name = str(body.get("name") or "").strip()
        level = str(body.get("level") or "area")
        parent_id = str(body.get("parentId") or "") or None
        reason = str(body.get("reason") or "").strip()
        self._need_reason(reason)
        if level not in {"area", "sub_area"} or not name or len(name) > 120:
            raise ValueError("Choose Area or Sub-area and enter a name up to 120 characters.")
        with connect() as db:
            project, permissions = self._project_access(db, user, project_id)
            if not permissions.get("schedule_edit") and user["role"] not in {"Admin", "Project Manager"}:
                raise PermissionError("Only a Planner, Project Manager, or Admin may edit the project hierarchy.")
            area_id = self._ensure_area(db, user, project["id"], name, level, parent_id)
            self._audit(db, user, "Added project hierarchy node", area_id or "", "Project areas", reason, None, {"name": name, "level": level, "parentId": parent_id}, project_id=project["id"])
            self._bump_revision(db, project["id"], user["tenant_id"])
        self._json(201, {"id": area_id, "name": name, "level": level, "parentId": parent_id})

    def _create_daily_update(self, body: dict) -> None:
        user, _, _ = self._require({"Admin", "Project Manager", "Planner", "Supervisor", "Contractor"}, csrf=True)
        project_id = str(body.get("projectId") or "").strip() or None
        with connect() as db:
            project, permissions = self._project_access(db, user, project_id)
            if not permissions.get("submit") and user["role"] != "Admin":
                raise PermissionError("Your project permissions do not allow daily work entry.")
            activity_id = str(body.get("activityId") or "").strip()
            activity_row = db.execute("SELECT payload FROM activities WHERE activity_id=? AND tenant_id=? AND project_id=?", (self._storage_id(project["id"], activity_id), user["tenant_id"], project["id"])).fetchone()
            if not activity_id or not activity_row:
                raise ValueError("Choose an activity from this project schedule.")
            activity = json.loads(activity_row["payload"])
            area_id = str(body.get("areaId") or "")
            sub_area_id = str(body.get("subAreaId") or "")
            for selected_id, expected_level, label in ((area_id, "area", "Area"), (sub_area_id, "sub_area", "Sub-area")):
                if selected_id and not db.execute("SELECT 1 FROM project_areas WHERE id=? AND tenant_id=? AND project_id=? AND level=?", (selected_id, user["tenant_id"], project["id"], expected_level)).fetchone():
                    raise ValueError(f"{label} must belong to the selected project.")
            work_date = iso_date(body.get("workDate"), "Work date", optional=False)
            text = str(body.get("workCompleted") or body.get("text") or "").strip()
            if not text or len(text) > 5000:
                raise ValueError("Describe today's work in 1–5,000 characters.")
            progress = numeric(body.get("progress"), "Progress", 0, 100, optional=False)
            event_status = str(body.get("eventStatus") or "progress")
            if event_status not in {"progress", "started", "completed"}:
                raise ValueError("Choose progress update, activity start, or activity finish.")
            event_time = str(body.get("eventTime") or "").strip()
            if event_time:
                try: datetime.strptime(event_time, "%H:%M")
                except ValueError as exc: raise ValueError("Event time must use 24-hour HH:MM format.") from exc
            if event_status == "completed" and progress < 100:
                raise ValueError("Set progress to 100% when recording an activity finish event.")
            quantity = numeric(body.get("quantityCompleted"), "Quantity completed", 0)
            manpower = numeric(body.get("manpower"), "Manpower", 0, 10000) or 0
            current_progress = float(activity.get("progress") or 0)
            explanation = str(body.get("delayReason") or body.get("notes") or body.get("issues") or "").strip()
            if progress < current_progress and not explanation:
                raise ValueError("Progress cannot move backwards without an explanation in notes, issues, or delay reason.")
            material = str(body.get("materialAvailability") or "Unknown")
            if material not in MATERIAL_STATES:
                raise ValueError("Choose a valid material availability state.")
            if not isinstance(body.get("attachments", []), list) or len(body.get("attachments", [])) > 20:
                raise ValueError("Choose no more than 20 supporting files.")
            attachments = []
            for doc_id in body.get("attachments", []):
                doc = db.execute("SELECT id,file_name FROM project_documents WHERE id=? AND tenant_id=? AND project_id=?", (str(doc_id), user["tenant_id"], project["id"])).fetchone()
                if not doc:
                    raise ValueError("Every attachment must belong to this project.")
                attachments.append({"id": doc["id"], "fileName": doc["file_name"]})
            warnings = []
            planned_qty = numeric(activity.get("plannedQuantity"), "Planned quantity")
            if quantity is not None and planned_qty and quantity + float(activity.get("quantityCompleted") or 0) > planned_qty:
                warnings.append("Reported quantity exceeds the planned quantity; the submitted value was kept for review.")
            if planned_qty and quantity is not None:
                expected = min(100, (float(activity.get("quantityCompleted") or 0) + quantity) / planned_qty * 100)
                if abs(expected - progress) > 15:
                    warnings.append(f"Quantity-based progress is about {expected:.0f}%, which differs from the entered {progress:.0f}%; verify before approval.")
            update_id, stamp = "upd-" + uuid.uuid4().hex[:14], now_iso()
            record = {"id": update_id, "activityId": activity_id, "workDate": work_date, "workCompleted": text, "text": text,
                      "progress": progress, "quantityCompleted": quantity, "manpower": int(manpower), "equipmentUsed": str(body.get("equipmentUsed") or "")[:500],
                      "materialAvailability": material, "condition": str(body.get("condition") or ""), "issues": str(body.get("issues") or "")[:1500],
                      "delayReason": str(body.get("delayReason") or "")[:1000], "safetyObservation": str(body.get("safetyObservation") or "")[:1000],
                      "notes": str(body.get("notes") or "")[:2000], "eventStatus": event_status, "eventTime": event_time,
                      "attachments": attachments, "warnings": warnings,
                      "conditionAtSubmission": str(body.get("condition") or ""), "aiAnalysis": body.get("aiAnalysis") if isinstance(body.get("aiAnalysis"), dict) else {},
                      "status": "Submitted", "source": "Daily work entry"}
            db.execute("INSERT INTO daily_updates(id,tenant_id,project_id,activity_id,area_id,sub_area_id,work_date,progress,quantity_completed,status,payload_json,submitted_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (update_id, user["tenant_id"], project["id"], activity_id, area_id or None, sub_area_id or None, work_date, progress, quantity, "Submitted", json.dumps(record, ensure_ascii=False), user["id"], stamp, stamp))
            self._audit(db, user, "Submitted daily work update", update_id, "Daily work entry", "Supervisor-submitted actual awaiting approval.", None, record, project_id=project["id"])
            self._control_notifications(db, user, project["id"], "daily_update", "Daily update needs review", f"{activity_id} · {work_date} was submitted for review.", "today")
            self._bump_revision(db, project["id"], user["tenant_id"])
        self._json(201, {"update": record, "warnings": warnings})

    def _analyze_daily_update(self, body: dict) -> None:
        user, _, _ = self._require({"Admin", "Project Manager", "Planner", "Supervisor", "Contractor"}, csrf=True)
        project_id = str(body.get("projectId") or "").strip() or None
        text = str(body.get("text") or "").strip()
        if not text or len(text) > 5000:
            raise ValueError("Enter a work description of 1–5,000 characters.")
        with connect() as db:
            project, permissions = self._project_access(db, user, project_id)
            if not permissions.get("submit") and user["role"] != "Admin":
                raise PermissionError("Your project permissions do not allow work analysis.")
            activities = [json.loads(r[0]) for r in db.execute("SELECT payload FROM activities WHERE tenant_id=? AND project_id=? ORDER BY activity_id", (user["tenant_id"], project["id"]))]
        draft = {"id": "DAILY-DRAFT", "date": str(body.get("workDate") or ""), "eventDate": str(body.get("workDate") or ""), "text": text,
                 "discipline": str(body.get("discipline") or ""), "location": str(body.get("location") or ""), "activityId": str(body.get("activityId") or ""), "progress": body.get("progress")}
        self._json(200, analyze_report(draft, activities, use_llm=True))

    def _review_daily_update(self, update_id: str, body: dict) -> None:
        user, _, _ = self._require({"Admin", "Project Manager", "Planner"}, csrf=True)
        project_id = str(body.get("projectId") or "").strip() or None
        status = str(body.get("status") or "Approved")
        reason = str(body.get("reason") or "").strip()
        self._need_reason(reason)
        if status not in {"Approved", "Rejected"}:
            raise ValueError("Choose Approved or Rejected.")
        with connect() as db:
            project, permissions = self._project_access(db, user, project_id)
            if not permissions.get("approve") and user["role"] != "Admin":
                raise PermissionError("Your project permissions do not allow daily update approval.")
            row = db.execute("SELECT * FROM daily_updates WHERE id=? AND tenant_id=? AND project_id=?", (update_id, user["tenant_id"], project["id"])).fetchone()
            if not row:
                raise ValueError("Daily update not found in this project.")
            if row["status"] != "Submitted":
                raise ValueError("Only submitted updates can be approved or rejected.")
            record = json.loads(row["payload_json"])
            before_activity = db.execute("SELECT payload FROM activities WHERE activity_id=? AND tenant_id=? AND project_id=?", (self._storage_id(project["id"], row["activity_id"]), user["tenant_id"], project["id"])).fetchone()
            if not before_activity:
                raise ValueError("The activity for this update is no longer in the schedule.")
            old = json.loads(before_activity["payload"])
            record["status"], record["reviewedBy"], record["reviewedAt"], record["reviewReason"] = status, user["display_name"], now_iso(), reason
            after = dict(old)
            if status == "Approved":
                new_progress = float(record["progress"])
                if new_progress < float(old.get("progress") or 0) and not any(str(record.get(k) or "").strip() for k in ("delayReason", "issues", "notes")):
                    raise ValueError("Progress regression requires an explanation before approval.")
                after["progress"] = new_progress
                after["quantityCompleted"] = float(old.get("quantityCompleted") or 0) + float(record.get("quantityCompleted") or 0)
                after["lastDailyUpdateId"] = update_id
                after["lastDailyUpdateAt"] = now_iso()
                if record.get("eventStatus") == "started":
                    after["actualStart"] = row["work_date"]
                    if record.get("eventTime"): after["actualStartTime"] = record["eventTime"]
                elif record.get("eventStatus") == "completed":
                    after["actualFinish"] = row["work_date"]
                    if record.get("eventTime"): after["actualFinishTime"] = record["eventTime"]
                    after["progress"] = 100
                if new_progress >= 100:
                    after["status"] = "Complete"
                    if not after.get("actualFinish") and ("activity completed" in str(record.get("workCompleted", "")).lower() or "finished" in str(record.get("workCompleted", "")).lower()):
                        after["actualFinish"] = row["work_date"]
                elif new_progress > 0:
                    after["status"] = "In progress"
                if record.get("materialAvailability") == "Pending":
                    after["materialAvailability"] = "Pending"
                if record.get("issues"):
                    after["blocker"] = str(record["issues"])[:500]
                db.execute("UPDATE activities SET payload=? WHERE activity_id=? AND tenant_id=? AND project_id=?", (json.dumps(after, ensure_ascii=False), self._storage_id(project["id"], row["activity_id"]), user["tenant_id"], project["id"]))
            db.execute("UPDATE daily_updates SET status=?,payload_json=?,reviewed_by=?,reviewed_at=?,updated_at=? WHERE id=? AND tenant_id=? AND project_id=?", (status, json.dumps(record, ensure_ascii=False), user["id"], record["reviewedAt"], now_iso(), update_id, user["tenant_id"], project["id"]))
            self._audit(db, user, "Approved daily work update" if status == "Approved" else "Rejected daily work update", update_id, "Daily update review", reason, {"update": json.loads(row["payload_json"]), "activity": old}, {"update": record, "activity": after if status == "Approved" else old}, project_id=project["id"])
            submitter = db.execute("SELECT submitted_by FROM daily_updates WHERE id=?", (update_id,)).fetchone()
            if submitter:
                db.execute("INSERT INTO notifications(id,tenant_id,project_id,recipient_id,kind,title,message,link,created_at) VALUES(?,?,?,?,?,?,?,?,?)", ("ntf-" + uuid.uuid4().hex[:14], user["tenant_id"], project["id"], submitter["submitted_by"], "daily_update_decision", "Daily update " + status.lower(), f"{row['activity_id']} · {row['work_date']} was {status.lower()} by {user['display_name']}.", "today", now_iso()))
            if status == "Approved" and record.get("issues"):
                record["riskSuggestion"] = {"title": str(record["issues"])[:120], "description": str(record.get("delayReason") or record["issues"]), "activityId": row["activity_id"], "areaId": row["area_id"] or "", "severity": "Medium", "sourceUpdateId": update_id}
                db.execute("UPDATE daily_updates SET payload_json=? WHERE id=?", (json.dumps(record, ensure_ascii=False), update_id))
            self._bump_revision(db, project["id"], user["tenant_id"])
        self._json(200, {"ok": True, "update": record, "activity": after if status == "Approved" else old})

    def _create_risk(self, body: dict) -> None:
        user, _, _ = self._require({"Admin", "Project Manager", "Planner"}, csrf=True)
        project_id = str(body.get("projectId") or "").strip() or None
        reason = str(body.get("reason") or "").strip()
        self._need_reason(reason)
        title = str(body.get("title") or "").strip()
        description = str(body.get("description") or "").strip()
        severity = str(body.get("severity") or "Medium")
        probability = str(body.get("probability") or "Medium")
        status = str(body.get("status") or "Open")
        if not title or len(title) > 140 or not description or len(description) > 3000:
            raise ValueError("Enter a risk title and description within the allowed length.")
        if severity not in RISK_SEVERITIES or probability not in {"Low", "Medium", "High"} or status not in RISK_STATUSES:
            raise ValueError("Choose valid risk severity, probability, and status values.")
        due_date = iso_date(body.get("dueDate"), "Risk due date")
        with connect() as db:
            project, permissions = self._project_access(db, user, project_id)
            if not permissions.get("approve") and user["role"] not in {"Admin", "Project Manager"}:
                raise PermissionError("Only a Project Manager, Planner, or Admin may create risks.")
            activity_id = str(body.get("activityId") or "")
            if activity_id and not db.execute("SELECT 1 FROM activities WHERE activity_id=? AND tenant_id=? AND project_id=?", (self._storage_id(project["id"], activity_id), user["tenant_id"], project["id"])).fetchone():
                raise ValueError("Risk activity must belong to this project.")
            area_id = str(body.get("areaId") or "")
            if area_id and not db.execute("SELECT 1 FROM project_areas WHERE id=? AND tenant_id=? AND project_id=?", (area_id, user["tenant_id"], project["id"])).fetchone():
                raise ValueError("Risk area must belong to this project.")
            risk_id, stamp = "risk-" + uuid.uuid4().hex[:14], now_iso()
            source_update_id = str(body.get("sourceUpdateId") or "")
            if source_update_id and not db.execute("SELECT 1 FROM daily_updates WHERE id=? AND tenant_id=? AND project_id=?", (source_update_id, user["tenant_id"], project["id"])).fetchone():
                raise ValueError("Source update must belong to this project.")
            attachment_ids = body.get("attachments") if isinstance(body.get("attachments"), list) else []
            if len(attachment_ids) > 20:
                raise ValueError("Choose no more than 20 supporting files.")
            attachments = []
            for document_id in attachment_ids:
                doc = db.execute("SELECT id,file_name FROM project_documents WHERE id=? AND tenant_id=? AND project_id=?", (str(document_id), user["tenant_id"], project["id"])).fetchone()
                if not doc: raise ValueError("Every risk attachment must belong to this project.")
                attachments.append({"id": doc["id"], "fileName": doc["file_name"]})
            payload = {"description": description, "probability": probability, "owner": str(body.get("owner") or "")[:100], "mitigation": str(body.get("mitigation") or "")[:3000], "type": str(body.get("type") or "issue"), "sourceUpdateId": source_update_id, "attachments": attachments}
            db.execute("INSERT INTO project_risks(id,tenant_id,project_id,activity_id,area_id,title,severity,status,due_date,owner,payload_json,created_by,created_at,updated_at,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (risk_id, user["tenant_id"], project["id"], activity_id or None, area_id or None, title, severity, status, due_date or None, payload["owner"], json.dumps(payload, ensure_ascii=False), user["id"], stamp, stamp, stamp if status in {"Resolved", "Closed"} else None))
            self._audit(db, user, "Created project risk", risk_id, "Risks and issues", reason, None, {"title": title, "activityId": activity_id, "severity": severity, "status": status, **payload}, project_id=project["id"])
            self._control_notifications(db, user, project["id"], "risk", "New project risk", title, "risks")
            self._bump_revision(db, project["id"], user["tenant_id"])
        self._json(201, {"id": risk_id, "title": title, "severity": severity, "status": status})

    def _change_risk(self, risk_id: str, body: dict) -> None:
        user, _, _ = self._require({"Admin", "Project Manager", "Planner"}, csrf=True)
        project_id = str(body.get("projectId") or "").strip() or None
        reason = str(body.get("reason") or "").strip()
        self._need_reason(reason)
        with connect() as db:
            project, permissions = self._project_access(db, user, project_id)
            if not permissions.get("approve") and user["role"] not in {"Admin", "Project Manager"}:
                raise PermissionError("Your project permissions do not allow risk management.")
            row = db.execute("SELECT * FROM project_risks WHERE id=? AND tenant_id=? AND project_id=?", (risk_id, user["tenant_id"], project["id"])).fetchone()
            if not row:
                raise ValueError("Risk not found in this project.")
            old = {"title": row["title"], "severity": row["severity"], "status": row["status"], "owner": row["owner"], "dueDate": row["due_date"]}
            status = str(body.get("status", row["status"]))
            severity = str(body.get("severity", row["severity"]))
            if status not in RISK_STATUSES or severity not in RISK_SEVERITIES:
                raise ValueError("Choose a valid risk status and severity.")
            payload = json.loads(row["payload_json"])
            for key in ("description", "probability", "mitigation"):
                if key in body: payload[key] = str(body[key] or "")[:3000]
            owner = str(body.get("owner", row["owner"]) or "")[:100]
            due_date = iso_date(body.get("dueDate", row["due_date"]), "Risk due date")
            resolved = now_iso() if status in {"Resolved", "Closed"} else None
            db.execute("UPDATE project_risks SET severity=?,status=?,due_date=?,owner=?,payload_json=?,updated_at=?,resolved_at=? WHERE id=? AND tenant_id=? AND project_id=?", (severity, status, due_date or None, owner, json.dumps(payload, ensure_ascii=False), now_iso(), resolved, risk_id, user["tenant_id"], project["id"]))
            after = {"title": row["title"], "severity": severity, "status": status, "owner": owner, "dueDate": due_date, **payload}
            self._audit(db, user, "Updated project risk", risk_id, "Risks and issues", reason, old, after, project_id=project["id"])
            self._bump_revision(db, project["id"], user["tenant_id"])
        self._json(200, {"ok": True, "id": risk_id, **after})

    def _create_milestone(self, body: dict) -> None:
        user, _, _ = self._require({"Admin", "Project Manager", "Planner"}, csrf=True)
        project_id = str(body.get("projectId") or "").strip() or None
        name = str(body.get("name") or "").strip()
        planned = iso_date(body.get("plannedDate"), "Milestone planned date", optional=False)
        actual = iso_date(body.get("actualDate"), "Milestone actual date")
        reason = str(body.get("reason") or "").strip()
        self._need_reason(reason)
        if not name or len(name) > 160:
            raise ValueError("Enter a milestone name up to 160 characters.")
        with connect() as db:
            project, permissions = self._project_access(db, user, project_id)
            if not permissions.get("schedule_edit") and user["role"] not in {"Admin", "Project Manager"}:
                raise PermissionError("Only a Planner, Project Manager, or Admin may manage milestones.")
            activity_id = str(body.get("activityId") or "")
            if activity_id and not db.execute("SELECT 1 FROM activities WHERE activity_id=? AND tenant_id=? AND project_id=?", (self._storage_id(project["id"], activity_id), user["tenant_id"], project["id"])).fetchone():
                raise ValueError("Milestone activity must belong to this project.")
            dependencies = body.get("dependencies", []) if isinstance(body.get("dependencies"), list) else []
            for dependency in dependencies:
                if not db.execute("SELECT 1 FROM activities WHERE activity_id=? AND tenant_id=? AND project_id=?", (self._storage_id(project["id"], str(dependency)), user["tenant_id"], project["id"])).fetchone():
                    raise ValueError("Every milestone dependency must be a schedule activity in this project.")
            milestone_id, stamp = "mile-" + uuid.uuid4().hex[:12], now_iso()
            payload = {"id": milestone_id, "activityId": activity_id, "name": name, "plannedDate": planned, "actualDate": actual, "status": "Complete" if actual else "Planned", "delayDays": max(0, (date.fromisoformat(actual or datetime.now(timezone.utc).date().isoformat()) - date.fromisoformat(planned)).days) if actual else 0, "dependencies": dependencies}
            db.execute("INSERT INTO project_milestones(id,tenant_id,project_id,activity_id,name,planned_date,actual_date,status,dependencies_json,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (milestone_id, user["tenant_id"], project["id"], activity_id or None, name, planned, actual, payload["status"], json.dumps(dependencies), stamp, user["id"]))
            self._audit(db, user, "Created project milestone", milestone_id, "Project milestones", reason, None, payload, project_id=project["id"])
            self._bump_revision(db, project["id"], user["tenant_id"])
        self._json(201, payload)

    def _upload_document(self, body: dict) -> None:
        user, _, _ = self._require({"Admin", "Project Manager", "Planner", "Supervisor", "Contractor"}, csrf=True)
        project_id = str(body.get("projectId") or "").strip() or None
        filename, content = self._decode_upload(body)
        category = str(body.get("category") or "supporting_document")[:60]
        reason = str(body.get("reason") or "").strip()
        self._need_reason(reason)
        with connect() as db:
            project, permissions = self._project_access(db, user, project_id)
            if category == "site_photo" or category == "daily_update":
                if not permissions.get("submit") and user["role"] != "Admin": raise PermissionError("Your project permissions do not allow uploads.")
            elif not permissions.get("schedule_edit") and user["role"] not in {"Admin", "Project Manager"}:
                raise PermissionError("Your project permissions do not allow project-document uploads.")
            area_id = str(body.get("areaId") or "")
            if area_id and not db.execute("SELECT 1 FROM project_areas WHERE id=? AND tenant_id=? AND project_id=?", (area_id, user["tenant_id"], project["id"])).fetchone(): raise ValueError("Document area must belong to the current project.")
            doc = self._store_document(db, user, project["id"], filename, content, category, area_id, str(body.get("activityId") or ""), {"reason": reason})
            self._audit(db, user, "Uploaded project document", doc["id"], "Project documents", reason, None, doc, project_id=project["id"])
            self._bump_revision(db, project["id"], user["tenant_id"])
        self._json(201, {"document": doc})

    def _read_document(self, document_id: str) -> None:
        user, _, _ = self._require()
        project_id = (parse_qs(urlparse(self.path).query).get("projectId") or [None])[0]
        with connect() as db:
            project, _ = self._project_access(db, user, project_id)
            row = db.execute("SELECT * FROM project_documents WHERE id=? AND tenant_id=? AND project_id=?", (document_id, user["tenant_id"], project["id"])).fetchone()
        if not row: raise ValueError("Document not found in this project.")
        base = (DATA_DIR / "uploads" / user["tenant_id"] / project["id"]).resolve()
        target = (base / row["storage_key"]).resolve()
        target.relative_to(base)
        content = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", row["mime_type"])
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + __import__("urllib.parse", fromlist=["quote"]).quote(row["file_name"]))
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(content)

    def _mark_notification_read(self, notification_id: str, body: dict) -> None:
        user, _, _ = self._require(csrf=True)
        with connect() as db:
            project_id = str(body.get("projectId") or "").strip() or None
            if project_id:
                self._project_access(db, user, project_id)
            db.execute("UPDATE notifications SET read_at=? WHERE id=? AND tenant_id=? AND recipient_id=? AND (project_id=? OR project_id IS NULL)", (now_iso(), notification_id, user["tenant_id"], user["id"], project_id))
        self._json(200, {"ok": True})

    def _project_json(self, project: sqlite3.Row) -> dict:
        return {"id": project["id"], "name": project["name"], "client": project["client"], "phase": project["phase"],
                "code": project["code"], "location": project["location"], "projectType": project["project_type"],
                "startDate": project["start_date"], "plannedCompletionDate": project["planned_completion_date"],
                "managerId": project["manager_id"], "description": project["description"], "status": project["status"]}

    def _state(self, user: sqlite3.Row, project_id: str | None = None) -> dict:
        with connect() as db:
            project, permissions = self._project_access(db, user, project_id)
            tenant_id, project_id = user["tenant_id"], project["id"]
            activities = [json.loads(r[0]) for r in db.execute("SELECT payload FROM activities WHERE tenant_id=? AND project_id=? ORDER BY activity_id", (tenant_id, project_id))]
            reports = [json.loads(r[0]) for r in db.execute("SELECT payload FROM reports WHERE tenant_id=? AND project_id=? AND archived=0 ORDER BY created_at DESC", (tenant_id, project_id))]
            if user["role"] == "Admin":
                reports = [json.loads(r[0]) for r in db.execute("SELECT payload FROM reports WHERE tenant_id=? AND project_id=? ORDER BY created_at DESC", (tenant_id, project_id))]
            audit_rows = db.execute("SELECT happened_at,actor_name,actor_role,action,record_id,source_location,reason,what,before_json,after_json,actor_id,source_ip FROM audit WHERE tenant_id=? AND project_id=? ORDER BY id DESC", (tenant_id, project_id)).fetchall()
            if user["role"] == "Supervisor":
                audit_rows = [r for r in audit_rows if r["actor_id"] == user["id"]]
            audits = [{"time": r["happened_at"], "actor": r["actor_name"], "role": r["actor_role"], "action": r["action"], "record": r["record_id"], "where": r["source_location"], "sourceIp": r["source_ip"], "why": r["reason"], "what": r["what"], "before": json.loads(r["before_json"]) if r["before_json"] else None, "after": json.loads(r["after_json"]) if r["after_json"] else None} for r in audit_rows]
            users = []
            if user["role"] == "Admin":
                users = [self._user_json(r) for r in db.execute("SELECT id,username,display_name,role,active,account_status,approved,created_at,last_login,company,requested_project,requested_role FROM users WHERE tenant_id=? ORDER BY id", (tenant_id,))]
                for item in users:
                    item["projects"] = [r[0] for r in db.execute("SELECT project_id FROM project_members WHERE tenant_id=? AND user_id=? ORDER BY project_id", (tenant_id, item["id"]))]
            db.execute("INSERT OR IGNORE INTO project_revisions(tenant_id,project_id,revision) VALUES(?,?,0)", (tenant_id, project_id))
            revision = int(db.execute("SELECT revision FROM project_revisions WHERE tenant_id=? AND project_id=?", (tenant_id, project_id)).fetchone()["revision"])
            if user["role"] == "Admin":
                projects = [self._project_json(r) for r in db.execute("SELECT * FROM projects WHERE tenant_id=? AND active=1 ORDER BY created_at,name", (tenant_id,))]
            else:
                project_rows = db.execute("SELECT p.*,m.permissions FROM projects p JOIN project_members m ON m.project_id=p.id WHERE p.tenant_id=? AND p.active=1 AND m.user_id=? ORDER BY p.created_at,p.name", (tenant_id, user["id"])).fetchall()
                projects = [self._project_json(r) for r in project_rows if self._permissions(user["role"], r["permissions"]).get("view")]
        intelligence = build_intelligence(activities, reports)
        current = self._public_user(user)
        current["permissions"] = permissions
        return {"activities": activities, "reports": reports, "audit": audits, "users": users, "projects": projects, "currentProject": self._project_json(project), "currentUser": current, "permissions": permissions, "intelligence": intelligence, "revision": revision}

    def api_write(self, method: str) -> None:
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/demo/seed-execution-storyline" and method == "POST":
                return self._seed_execution_storyline(body)
            if path == "/api/recovery-scenarios" and method == "POST":
                return self._simulate_recovery(body)
            if path.startswith("/api/recovery-scenarios/") and path.endswith("/decision") and method == "POST":
                return self._decide_recovery_scenario(path.split("/")[3], body)
            if path == "/api/recommendations/generate" and method == "POST":
                return self._generate_execution_recommendations(body)
            if path.startswith("/api/recommendations/") and path.endswith("/decision") and method == "POST":
                return self._decide_execution_recommendation(path.split("/")[3], body)
            if path.startswith("/api/execution-actions/") and path.endswith("/outcome") and method == "POST":
                return self._record_execution_outcome(path.split("/")[3], body)
            if path == "/api/plans" and method == "POST":
                return self._create_plan_version(body)
            if path.startswith("/api/plans/") and path.endswith("/analyze") and method == "POST":
                return self._analyze_plan_version(path.split("/")[3], body)
            if path.startswith("/api/plans/") and path.endswith("/approve") and method == "POST":
                return self._approve_plan_version(path.split("/")[3], body)
            if path == "/api/areas" and method == "POST":
                return self._create_area(body)
            if path == "/api/daily-updates/analyze" and method == "POST":
                return self._analyze_daily_update(body)
            if path == "/api/daily-updates" and method == "POST":
                return self._create_daily_update(body)
            if path.startswith("/api/daily-updates/") and path.endswith("/review") and method == "POST":
                return self._review_daily_update(path.split("/")[3], body)
            if path == "/api/risks" and method == "POST":
                return self._create_risk(body)
            if path.startswith("/api/risks/") and method == "PATCH":
                return self._change_risk(path.rsplit("/", 1)[-1], body)
            if path == "/api/milestones" and method == "POST":
                return self._create_milestone(body)
            if path == "/api/documents" and method == "POST":
                return self._upload_document(body)
            if path.startswith("/api/notifications/") and path.endswith("/read") and method == "POST":
                return self._mark_notification_read(path.split("/")[3], body)
            if path == "/api/setup" and method == "POST":
                return self._setup(body)
            if path in {"/api/login", "/api/auth/login"} and method == "POST":
                return self._login(body)
            if path in {"/api/logout", "/api/auth/logout"} and method == "POST":
                user, _, token_hash = self._require(csrf=True)
                with connect() as db:
                    db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
                    self._audit(db, user, "Signed out", "SESSION", "SiteLink sign-in", "User signed out.", None, None)
                    self._bump_revision(db)
                return self._json(200, {"ok": True}, {"Set-Cookie": self._cookie("", True)})
            if path == "/api/auth/password" and method == "POST":
                return self._change_own_password(body)
            if path == "/api/state" and method == "POST":
                return self._sync_state(body)
            if path == "/api/import/parse" and method == "POST":
                user, _, _ = self._require({"Admin", "Project Manager", "Planner", "Supervisor", "Contractor"}, csrf=True)
                project_id = str(body.get("projectId", "")).strip() or None
                kind = str(body.get("kind", "")).lower()
                filename = Path(str(body.get("fileName", "upload"))).name
                encoded = body.get("fileBase64", "")
                if not isinstance(encoded, str) or not encoded or len(encoded) > (MAX_UPLOAD_BYTES * 4 // 3 + 32):
                    raise ValueError("Choose a file smaller than 8 MB.")
                try:
                    file_data = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise ValueError("The selected file could not be decoded.") from exc
                if not file_data or len(file_data) > MAX_UPLOAD_BYTES:
                    raise ValueError("Files must be between 1 byte and 8 MB.")
                with connect() as db:
                    project, permissions = self._project_access(db, user, project_id)
                if kind == "schedule":
                    if not permissions.get("schedule_edit") and user["role"] != "Admin":
                        raise PermissionError("Only a Planner or Admin can import a schedule.")
                    return self._json(200, {"rows": parse_schedule_file(filename, file_data), "format": "MS Project XML (MSPDI adapter/demo)" if filename.lower().endswith(".xml") else filename.rsplit(".", 1)[-1].upper()})
                if kind == "report":
                    if not permissions.get("submit") and user["role"] != "Admin":
                        raise PermissionError("Your account cannot import reports.")
                    return self._json(200, {"rows": parse_report_file(filename, file_data)})
                if kind == "ocr":
                    if not permissions.get("submit") and user["role"] != "Admin":
                        raise PermissionError("Your account cannot submit a scan.")
                    return self._json(200, extract_ocr(filename, file_data))
                raise ValueError("Choose a report, schedule, or scanned diary import.")
            if path in {"/api/analyze", "/api/ai/extract/field-report"} and method == "POST":
                user, _, _ = self._require({"Admin", "Project Manager", "Planner", "Supervisor", "Contractor"}, csrf=True)
                with connect() as db:
                    project, permissions = self._project_access(db, user, str(body.get("projectId", "")) or None)
                    if not permissions.get("submit") and user["role"] != "Admin":
                        raise PermissionError("You do not have permission to access this page.")
                text = str(body.get("text", "")).strip()
                if not text or len(text) > 5000:
                    raise ValueError("Enter an update of 1 to 5,000 characters.")
                event_date = str(body.get("eventDate", "")).strip()
                if event_date:
                    try:
                        datetime.strptime(event_date, "%Y-%m-%d")
                    except ValueError as exc:
                        raise ValueError("Confirmed event date must use YYYY-MM-DD format.") from exc
                if body.get("progress") not in (None, ""):
                    try:
                        numeric_progress = float(body["progress"])
                    except (TypeError, ValueError) as exc:
                        raise ValueError("Confirmed progress must be a number from 0 to 100 percent.") from exc
                    if not 0 <= numeric_progress <= 100:
                        raise ValueError("Confirmed progress must be a number from 0 to 100 percent.")
                report = {
                    "id": "DRAFT", "date": str(body.get("date", "")), "text": text,
                    "discipline": str(body.get("discipline", "")), "reporter": user["display_name"],
                    "source": str(body.get("source", "AI Time Agent")), "sourceType": str(body.get("sourceType", "text")),
                    "eventDate": str(body.get("eventDate", "")), "progress": body.get("progress"),
                }
                with connect() as db:
                    activities = [json.loads(row[0]) for row in db.execute("SELECT payload FROM activities WHERE tenant_id=? AND project_id=? ORDER BY activity_id", (user["tenant_id"], project["id"]))]
                report["location"] = str(body.get("location", ""))
                report["activityId"] = str(body.get("activityId", ""))
                return self._json(200, analyze_report(report, activities, use_llm=True))
            if path in {"/api/ai/ask", "/api/ai/investigate"} and method == "POST":
                user, _, _ = self._require(csrf=True)
                question = str(body.get("message") or body.get("question") or "").strip()
                if not question or len(question) > 1200:
                    raise ValueError("Enter an AI question of 1 to 1,200 characters.")
                project_id = str(body.get("projectId") or "").strip() or None
                activity_id = str(body.get("activityId") or "").strip()
                with connect() as db:
                    project, permissions = self._project_access(db, user, project_id)
                if not permissions.get("view"):
                    raise PermissionError("Your account cannot view this project.")
                selected_project_id = str(project["id"])
                started = time.perf_counter()
                with connect() as db:
                    activities, updates, risks, milestones = self._execution_inputs(db, user["tenant_id"], selected_project_id)
                    areas = [dict(row) for row in db.execute(
                        "SELECT id,parent_id,level,name,code FROM project_areas WHERE tenant_id=? AND project_id=? ORDER BY level,name",
                        (user["tenant_id"], selected_project_id))]
                    records = [{"id": row["id"], "kind": row["kind"], "status": row["status"],
                                "payload": json.loads(row["payload_json"] or "{}"), "createdAt": row["created_at"]}
                               for row in db.execute("SELECT id,kind,status,payload_json,created_at FROM execution_records WHERE tenant_id=? AND project_id=? ORDER BY created_at DESC LIMIT 250", (user["tenant_id"], selected_project_id))]
                    outcomes = self._execution_outcomes(db, user["tenant_id"], selected_project_id)
                today = datetime.now(timezone.utc).date().isoformat()
                analysis = analyze_execution(activities, updates, risks, milestones, today, outcomes)
                state_snapshot = self._state(user, selected_project_id)
                memory = state_snapshot["intelligence"]
                memory["execution_outcomes"] = outcomes
                memory["project_execution"] = {"activities": activities,
                    "approvedUpdates": [row for row in updates if row.get("status") == "Approved"],
                    "risks": risks, "milestones": milestones, "records": records,
                    "analysis": analysis, "asOf": today}
                area_filter = str(body.get("areaId") or "").strip() if path.endswith("investigate") else ""
                investigation = {"scope": str(body.get("scope") or "project"), "areaId": area_filter}
                if area_filter and not any(str(row.get("id") or "") == area_filter for row in areas):
                    raise ValueError("Choose an area recorded in this project.")
                response = ask_sitelink_ai(question, {"role": user["role"], "permissions": permissions},
                    self._project_json(project), {"activities": activities, "updates": updates, "reports": state_snapshot.get("reports", []),
                    "records": records, "analysis": analysis, "fingerprints": memory.get("fingerprints", []),
                    "memoryAnswer": answer_memory(question, memory), "investigation": investigation, "areas": areas,
                    "startedAt": started}, activity_id)
                with connect() as db:
                    self._audit(db, user, "Asked SiteLink AI", "AI-ASK", "SiteLink AI / read-only assistant",
                        "Intent " + response["intent"] + "; provider " + response["provider"] + "; record citations " + str(response["record_count"]),
                        None, {"intent": response["intent"], "provider": response["provider"],
                               "providerModel": response.get("provider_model", ""), "providerFallback": response["provider_fallback"],
                               "validation": response["provider_validation"], "latencyMs": response["latency_ms"],
                               "activityId": response["activity_id"], "recordCount": response["record_count"]},
                        project_id=selected_project_id)
                return self._json(200, response)
            if path == "/api/memory" and method == "POST":
                user, _, _ = self._require(csrf=True)
                project_id = str(body.get("projectId", "")) or None
                with connect() as db:
                    selected_project, permissions = self._project_access(db, user, project_id)
                if not permissions.get("view"):
                    raise PermissionError("You do not have permission to access this page.")
                question = str(body.get("question", "")).strip()
                if not question or len(question) > 500:
                    raise ValueError("Enter a question of 1 to 500 characters.")
                memory = self._state(user, selected_project["id"])["intelligence"]
                with connect() as db:
                    memory["execution_outcomes"] = self._execution_outcomes(db, user["tenant_id"], selected_project["id"])
                    activities, updates, risks, milestones = self._execution_inputs(db, user["tenant_id"], selected_project["id"])
                    records = [{"id": row["id"], "kind": row["kind"], "status": row["status"],
                                "payload": json.loads(row["payload_json"] or "{}"), "createdAt": row["created_at"]}
                               for row in db.execute("SELECT id,kind,status,payload_json,created_at FROM execution_records WHERE tenant_id=? AND project_id=? ORDER BY created_at DESC", (user["tenant_id"], selected_project["id"]))]
                    today = datetime.now(timezone.utc).date().isoformat()
                    project_analysis = analyze_execution(activities, updates, risks, milestones, today, memory["execution_outcomes"])
                    memory["project_execution"] = {"activities": activities,
                        "approvedUpdates": [row for row in updates if row.get("status") == "Approved"],
                        "risks": risks, "milestones": milestones, "records": records,
                        "analysis": project_analysis, "asOf": today}
                return self._json(200, answer_memory(question, memory))
            if path == "/api/users" and method == "POST":
                return self._create_user(body)
            if path.startswith("/api/users/") and method == "PATCH":
                return self._change_user(path.rsplit("/", 1)[-1], body)
            if path.startswith("/api/users/") and path.endswith("/approve") and method == "POST":
                return self._review_signup_user(path.split("/")[-2], body, approve=True)
            if path.startswith("/api/users/") and path.endswith("/reject") and method == "POST":
                return self._review_signup_user(path.split("/")[-2], body, approve=False)
            if path.startswith("/api/users/") and path.endswith("/reset-password") and method == "POST":
                return self._reset_user_password(path.split("/")[-2], body)
            if path == "/api/auth/signup" and method == "POST":
                if os.environ.get("PUBLIC_SIGNUP", "1").strip().lower() not in {"1", "true", "yes"}:
                    raise PermissionError("Public signup is disabled. Ask your workspace administrator for an account.")
                return self._signup(body)
            if path.startswith("/api/users/") and method == "DELETE":
                return self._delete_user(path.rsplit("/", 1)[-1], body)
            if path == "/api/projects" and method == "POST":
                return self._create_project(body)
            if path.startswith("/api/projects/") and path.endswith("/members") and method in {"PUT", "POST"}:
                return self._set_project_members(path.split("/")[3], body)
            if path.startswith("/api/projects/") and method == "PATCH":
                return self._change_project(path.rsplit("/", 1)[-1], body)
            self._json(404, {"error": "Not found."})
        except PermissionError as exc:
            self._json(401 if "sign in" in str(exc).lower() else 403, {"error": str(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except sqlite3.IntegrityError:
            self._json(409, {"error": "That username or report ID is already in use."})
        except StateConflictError as exc:
            self._json(409, {"error": str(exc), "code": "stale_workspace", "refresh": True})
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                self._json(503, {"error": "The shared database is busy. Wait a moment and retry.", "code": "database_busy"})
            else:
                self._json(500, {"error": "Server could not complete this request."})
        except Exception as exc:
            self._json(500, {"error": "Server could not complete this request.", "detail": str(exc)})

    def _setup(self, body: dict) -> None:
        if os.environ.get("REQUIRE_SETUP_TOKEN", "0").strip().lower() in {"1", "true", "yes"}:
            expected = os.environ.get("SETUP_TOKEN", "")
            provided = str(body.get("setupToken", ""))
            if not expected or not hmac.compare_digest(provided, expected):
                raise PermissionError("Use the one-time setup token provided by the server administrator.")
        name, username, password = str(body.get("name", "")).strip(), str(body.get("username", "")).strip(), str(body.get("password", ""))
        if not name or len(name) > 80 or not username or len(username) < 3 or len(username) > 50:
            raise ValueError("Enter a name and a username with 3–50 characters.")
        validate_password(password)
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]:
                raise PermissionError("Workspace setup is already complete. Sign in instead.")
            salt, pwd_hash = hash_password(password)
            stamp = now_iso()
            cur = db.execute("INSERT INTO users(username,display_name,role,salt,password_hash,created_at,last_login) VALUES(?,?,?,?,?,?,?) RETURNING id", (username, name, "Admin", salt, pwd_hash, stamp, stamp))
            user_id = cur.fetchone()["id"]
            user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            token, csrf = self._new_session(db, user_id)
            self._audit(db, user, "Created first administrator", "WORKSPACE", "First-run setup", "Workspace owner account created.", None, {"username": username, "role": "Admin"})
        self._json(201, {"user": self._public_user(user), "csrf": csrf}, {"Set-Cookie": self._cookie(token)})

    def _signup(self, body: dict) -> None:
        """Create a pending Viewer in the server-configured SiteLink workspace."""
        name = str(body.get("name", "")).strip()
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        company = str(body.get("company", "")).strip()[:120]
        requested_project = str(body.get("requestedProject", "")).strip()[:160]
        requested_role = str(body.get("requestedRole", "Viewer")).strip()
        if not name or len(name) > 80 or len(username) < 3 or len(username) > 50:
            raise ValueError("Enter your name and a username with 3–50 characters.")
        if requested_role not in ROLES - {"Admin"}:
            raise ValueError("Choose a valid requested role. Administrator access must be assigned by an existing Admin.")
        validate_password(password)
        if str(body.get("confirmPassword", "")) != password:
            raise ValueError("Passwords do not match.")
        ip_key = self._client_ip() or "unknown"
        stamp = now_iso()
        tenant_id = SIGNUP_TENANT_ID
        salt, pwd_hash = hash_password(password)
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cutoff = time.time() - 600
            db.execute("DELETE FROM login_attempts WHERE happened_at<?", (cutoff,))
            attempts = db.execute("SELECT COUNT(*) AS n FROM login_attempts WHERE ip_key=? AND username_key='signup'", (ip_key,)).fetchone()["n"]
            if attempts >= 5:
                raise PermissionError("Too many sign-up attempts. Wait 10 minutes before trying again.")
            db.execute("INSERT INTO login_attempts(ip_key,username_key,happened_at) VALUES(?,'signup',?)", (ip_key, time.time()))
            if not db.execute("SELECT 1 FROM tenants WHERE id=?", (tenant_id,)).fetchone():
                raise ValueError("Self-service signup is not configured for an existing SiteLink workspace.")
            cur = db.execute("INSERT INTO users(username,display_name,role,salt,password_hash,company,requested_project,requested_role,created_at,tenant_id,account_status,approved) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id", (username, name, "Viewer", salt, pwd_hash, company, requested_project, requested_role, stamp, tenant_id, "pending", 0))
            user_id = cur.fetchone()["id"]
            user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            self._audit(db, user, "User signup", "USER-" + str(user_id), "Public account signup", "Self-registered; administrator approval required.", None, {"username": username, "name": name, "company": company, "requestedProject": requested_project, "requestedRole": requested_role, "role": "Viewer", "status": "Pending"})
        self._json(201, {"ok": True, "status": "pending", "message": "Account created successfully. Your account is waiting for administrator approval."})

    def _login(self, body: dict) -> None:
        username, password = str(body.get("username", "")).strip(), str(body.get("password", ""))
        ip_key = self._client_ip() or "unknown"
        with connect() as db:
            user = db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        candidate = hash_password(password, user["salt"])[1] if user else ""
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cutoff = time.time() - 600
            db.execute("DELETE FROM login_attempts WHERE happened_at<?", (cutoff,))
            count = db.execute("SELECT COUNT(*) AS n FROM login_attempts WHERE ip_key=? AND username_key=?", (ip_key, username.lower())).fetchone()["n"]
            if count >= 8:
                raise PermissionError("Too many sign-in attempts. Wait 10 minutes before trying again.")
            latest = db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
            valid = bool(latest and user and latest["salt"] == user["salt"] and hmac.compare_digest(candidate, latest["password_hash"]))
            if not valid:
                db.execute("INSERT INTO login_attempts(ip_key,username_key,happened_at) VALUES(?,?,?)", (ip_key, username.lower(), time.time()))
                db.commit()
                raise PermissionError("Username or password is incorrect.")
            if latest["account_status"] == "pending":
                db.commit()
                raise PermissionError("Your account is waiting for administrator approval.")
            if not latest["active"] or latest["account_status"] != "active" or not latest["approved"]:
                db.commit()
                raise PermissionError("Your account is disabled. Contact your administrator.")
            db.execute("DELETE FROM login_attempts WHERE ip_key=? AND username_key=?", (ip_key, username.lower()))
            stamp = now_iso()
            db.execute("UPDATE users SET last_login=? WHERE id=?", (stamp, latest["id"]))
            user = db.execute("SELECT * FROM users WHERE id=?", (latest["id"],)).fetchone()
            token, csrf = self._new_session(db, user["id"])
            self._audit(db, user, "Signed in", "SESSION", "SiteLink sign-in", "Successful account sign-in.", None, None)
            self._bump_revision(db)
        self._json(200, {"user": self._public_user(user), "csrf": csrf}, {"Set-Cookie": self._cookie(token)})

    def _new_session(self, db: sqlite3.Connection, user_id: int) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        expiry = (datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds").replace("+00:00", "Z")
        db.execute("INSERT INTO sessions(token_hash,csrf_token,user_id,expires_at) VALUES(?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), csrf, user_id, expiry))
        return token, csrf

    def _audit(self, db: sqlite3.Connection, user: sqlite3.Row | None, action: str, record: str, where: str, why: str, before: object, after: object, project_id: str | None = None) -> None:
        actor_name = user["display_name"] if user else "System demo"
        actor_role = user["role"] if user else "System"
        actor_id = user["id"] if user else None
        if isinstance(after, dict) and "text" in after:
            summary = str(after.get("text", "")).strip().replace("\\n", " ")
            if len(summary) > 170:
                summary = summary[:167] + "…"
            what = "Report: " + summary
            if after.get("activityId"):
                what += " · activity " + str(after.get("activityId"))
            if after.get("status"):
                what += " · state " + str(after.get("status"))
            if after.get("progress") != "" and after.get("progress") is not None:
                what += " · progress " + str(after.get("progress")) + "%"
        elif isinstance(after, dict) and "username" in after:
            what = "Account: " + str(after.get("name", "")) + " (" + str(after.get("username", "")) + ") · role " + str(after.get("role", ""))
        elif isinstance(after, dict) and "name" in after:
            what = "Activity: " + str(after.get("id", "")) + " · " + str(after.get("name", "")) + " · " + str(after.get("progress", "")) + "% complete"
        elif after is not None:
            what = json.dumps(after, ensure_ascii=False)[:500]
        else:
            what = action
        source_ip = self._client_ip()
        tenant_id = user["tenant_id"] if user and "tenant_id" in user.keys() else DEFAULT_TENANT
        if project_id:
            audit_project = project_id
        else:
            project = db.execute("SELECT id FROM projects WHERE tenant_id=? AND id=?", (tenant_id, DEFAULT_PROJECT)).fetchone()
            if project is None:
                project = db.execute("SELECT id FROM projects WHERE tenant_id=? AND active=1 ORDER BY created_at,name LIMIT 1", (tenant_id,)).fetchone()
            audit_project = project["id"] if project else DEFAULT_PROJECT
        def audit_snapshot(value):
            if value is None:
                return None
            serialized = json.dumps(value, ensure_ascii=False)
            if len(serialized) <= 6000:
                return serialized
            return json.dumps({"_truncated": True, "originalCharacters": len(serialized),
                               "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                               "summary": "Audit payload exceeded the 6,000 character preview limit."}, ensure_ascii=False)
        db.execute("INSERT INTO audit(happened_at,actor_id,actor_name,actor_role,action,record_id,source_location,reason,what,before_json,after_json,source_ip,tenant_id,project_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (now_iso(), actor_id, actor_name, actor_role, action, record, where or "SiteLink workspace", why, what,
                    audit_snapshot(before), audit_snapshot(after), source_ip, tenant_id, audit_project))

    def _validate_permissions(self, role: str, requested: dict | None) -> dict:
        ceiling = dict(ROLE_PERMISSIONS.get(role, {}))
        if role == "Admin":
            raise ValueError("Admin accounts manage every project in their workspace and do not need project permissions.")
        if requested is None:
            return ceiling
        if not isinstance(requested, dict):
            raise ValueError("Project permissions must be an object.")
        result = {}
        for name, default in ceiling.items():
            value = bool(requested.get(name, default))
            if value and not default and not (role == "Supervisor" and name == "approve"):
                raise ValueError("The selected permission exceeds this role's allowed access.")
            result[name] = value
        return result

    def _create_project(self, body: dict) -> None:
        actor, _, _ = self._require({"Admin"}, csrf=True)
        name, client, phase = str(body.get("name", "")).strip(), str(body.get("client", "")).strip(), str(body.get("phase", "")).strip()
        reason = str(body.get("reason", "")).strip()
        if not name or len(name) > 100: raise ValueError("Enter a project name of 1 to 100 characters.")
        metadata = self._project_metadata(body, create=True)
        self._need_reason(reason)
        project_id, stamp = "prj-" + uuid.uuid4().hex[:12], now_iso()
        if not metadata["code"]:
            metadata["code"] = "PRJ-" + project_id[-6:].upper()
        with connect() as db:
            if metadata["managerId"] is not None:
                manager = db.execute("SELECT id,role,active,account_status FROM users WHERE id=? AND tenant_id=?", (metadata["managerId"], actor["tenant_id"])).fetchone()
                if not manager or not manager["active"] or manager["account_status"] != "active" or manager["role"] not in {"Project Manager", "Admin"}:
                    raise ValueError("Project manager must be an active Project Manager account in this workspace.")
            db.execute("INSERT INTO projects(id,tenant_id,name,client,phase,code,location,project_type,start_date,planned_completion_date,manager_id,description,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (project_id, actor["tenant_id"], name, client[:100], phase[:100], metadata["code"], metadata["location"], metadata["projectType"], metadata["startDate"], metadata["plannedCompletionDate"], metadata["managerId"], metadata["description"], metadata["status"], stamp))
            db.execute("INSERT INTO project_revisions(tenant_id,project_id,revision) VALUES(?,?,0)", (actor["tenant_id"], project_id))
            after = {"id": project_id, "name": name, "client": client, "phase": phase, **metadata}
            self._audit(db, actor, "Created project", project_id, "Administration / projects", reason, None, after, project_id=project_id)
            self._bump_revision(db, project_id, actor["tenant_id"])
        self._json(201, {"project": {"id": project_id, "name": name, "client": client, "phase": phase, **metadata}})

    def _project_metadata(self, body: dict, create: bool = False, old: sqlite3.Row | None = None) -> dict:
        def previous(key: str, column: str, default: str = "") -> str:
            if key in body:
                return str(body.get(key) or "").strip()
            return str(old[column] or "") if old is not None else default
        metadata = {
            "code": previous("code", "code"), "location": previous("location", "location"),
            "projectType": previous("projectType", "project_type"), "startDate": previous("startDate", "start_date"),
            "plannedCompletionDate": previous("plannedCompletionDate", "planned_completion_date"),
            "description": previous("description", "description"), "status": previous("status", "status", "Planning" if create else "Active"),
        }
        manager = body.get("managerId", old["manager_id"] if old is not None else None)
        if manager in (None, ""):
            metadata["managerId"] = None
        else:
            try: metadata["managerId"] = int(manager)
            except (ValueError, TypeError) as exc: raise ValueError("Choose a valid project manager account.") from exc
        if len(metadata["code"]) > 40 or len(metadata["location"]) > 160 or len(metadata["projectType"]) > 80 or len(metadata["description"]) > 4000:
            raise ValueError("Project code, location, type, or description is longer than allowed.")
        if metadata["status"] not in PROJECT_STATUSES:
            raise ValueError("Choose Planning, Active, On Hold, Completed, or Archived for the project status.")
        metadata["startDate"] = iso_date(metadata["startDate"], "Project start date")
        metadata["plannedCompletionDate"] = iso_date(metadata["plannedCompletionDate"], "Planned completion date")
        if metadata["startDate"] and metadata["plannedCompletionDate"] and metadata["plannedCompletionDate"] < metadata["startDate"]:
            raise ValueError("Planned completion date cannot be earlier than the project start date.")
        return metadata

    def _change_project(self, project_id: str, body: dict) -> None:
        actor, _, _ = self._require({"Admin"}, csrf=True)
        name, client, phase = str(body.get("name", "")).strip(), str(body.get("client", "")).strip(), str(body.get("phase", "")).strip()
        reason = str(body.get("reason", "")).strip()
        if not name or len(name) > 100: raise ValueError("Enter a project name of 1 to 100 characters.")
        self._need_reason(reason)
        with connect() as db:
            project = db.execute("SELECT * FROM projects WHERE id=? AND tenant_id=? AND active=1", (project_id, actor["tenant_id"])).fetchone()
            if not project: raise PermissionError("You do not have permission to access this page.")
            before = self._project_json(project)
            metadata = self._project_metadata(body, old=project)
            if metadata["managerId"] is not None:
                manager = db.execute("SELECT id,role,active,account_status FROM users WHERE id=? AND tenant_id=?", (metadata["managerId"], actor["tenant_id"])).fetchone()
                if not manager or not manager["active"] or manager["account_status"] != "active" or manager["role"] not in {"Project Manager", "Admin"}:
                    raise ValueError("Project manager must be an active Project Manager account in this workspace.")
            db.execute("UPDATE projects SET name=?,client=?,phase=?,code=?,location=?,project_type=?,start_date=?,planned_completion_date=?,manager_id=?,description=?,status=? WHERE id=? AND tenant_id=?", (name, client[:100], phase[:100], metadata["code"], metadata["location"], metadata["projectType"], metadata["startDate"], metadata["plannedCompletionDate"], metadata["managerId"], metadata["description"], metadata["status"], project_id, actor["tenant_id"]))
            self._audit(db, actor, "Updated project", project_id, "Administration / projects", reason, before, {"id": project_id, "name": name, "client": client, "phase": phase, **metadata}, project_id=project_id)
            self._bump_revision(db, project_id, actor["tenant_id"])
        self._json(200, {"project": {"id": project_id, "name": name, "client": client, "phase": phase, **metadata}})

    def _set_project_members(self, project_id: str, body: dict) -> None:
        actor, _, _ = self._require({"Admin"}, csrf=True)
        members, reason = body.get("members"), str(body.get("reason", "")).strip()
        self._need_reason(reason)
        if not isinstance(members, list) or len(members) > 1000: raise ValueError("Provide a list of project members.")
        with connect() as db:
            project = db.execute("SELECT * FROM projects WHERE id=? AND tenant_id=? AND active=1", (project_id, actor["tenant_id"])).fetchone()
            if not project: raise PermissionError("You do not have permission to access this page.")
            valid = {}
            for item in members:
                try: user_id = int(item.get("userId"))
                except (TypeError, ValueError): raise ValueError("Each project member needs a valid user ID.")
                target = db.execute("SELECT id,role,username FROM users WHERE id=? AND tenant_id=? AND active=1 AND account_status='active' AND approved=1", (user_id, actor["tenant_id"])).fetchone()
                if not target: raise ValueError("A selected account is not active in this workspace.")
                if target["role"] == "Admin": continue
                valid[user_id] = (target, self._validate_permissions(target["role"], item.get("permissions")))
            before = db.execute("SELECT user_id,permissions FROM project_members WHERE project_id=? AND tenant_id=?", (project_id, actor["tenant_id"])).fetchall()
            db.execute("DELETE FROM project_members WHERE project_id=? AND tenant_id=?", (project_id, actor["tenant_id"]))
            for user_id, (target, permissions) in valid.items():
                db.execute("INSERT INTO project_members(project_id,user_id,tenant_id,permissions,created_at) VALUES(?,?,?,?,?)", (project_id, user_id, actor["tenant_id"], json.dumps(permissions), now_iso()))
            after = [{"userId": uid, "username": target["username"], "permissions": permissions} for uid, (target, permissions) in valid.items()]
            self._audit(db, actor, "Changed project members", project_id, "Administration / project members", reason, [{"userId": r["user_id"], "permissions": json.loads(r["permissions"])} for r in before], after, project_id=project_id)
            self._bump_revision(db, project_id, actor["tenant_id"])
        self._json(200, {"ok": True, "members": after})

    def _delete_user(self, user_id_text: str, body: dict) -> None:
        actor, _, _ = self._require({"Admin"}, csrf=True)
        try: user_id = int(user_id_text)
        except ValueError: raise ValueError("Invalid user ID.")
        reason = str(body.get("reason", "")).strip()
        self._need_reason(reason)
        with connect() as db:
            target = db.execute("SELECT id,username,display_name,role,active FROM users WHERE id=? AND tenant_id=?", (user_id, actor["tenant_id"])).fetchone()
            if not target: raise ValueError("User account not found.")
            if user_id == actor["id"]: raise ValueError("You cannot delete your own account.")
            if target["role"] == "Admin" and target["active"]:
                count = db.execute("SELECT COUNT(*) AS n FROM users WHERE tenant_id=? AND role='Admin' AND active=1", (actor["tenant_id"],)).fetchone()["n"]
                if count <= 1: raise ValueError("The workspace must keep at least one active administrator.")
            before = {"username": target["username"], "name": target["display_name"], "role": target["role"]}
            affected_projects = [r[0] for r in db.execute("SELECT project_id FROM project_members WHERE user_id=? AND tenant_id=?", (user_id, actor["tenant_id"]))]
            # Preserve audit snapshots while removing the foreign-key link to the deleted account.
            db.execute("UPDATE audit SET actor_id=NULL WHERE actor_id=? AND tenant_id=?", (user_id, actor["tenant_id"]))
            db.execute("DELETE FROM users WHERE id=? AND tenant_id=?", (user_id, actor["tenant_id"]))
            for project_id in affected_projects:
                self._bump_revision(db, project_id, actor["tenant_id"])
            self._audit(db, actor, "Deleted user account", "USER-" + str(user_id), "Administration / user management", reason, before, None)
            self._bump_revision(db)
            rows = db.execute("SELECT id,username,display_name,role,active,account_status,approved,created_at,last_login,company,requested_project,requested_role FROM users WHERE tenant_id=? ORDER BY id", (actor["tenant_id"],)).fetchall()
        self._json(200, {"users": [self._user_json(r) for r in rows]})

    def _review_signup_user(self, user_id_text: str, body: dict, approve: bool) -> None:
        actor, _, _ = self._require({"Admin"}, csrf=True)
        try:
            user_id = int(user_id_text)
        except ValueError:
            raise ValueError("Invalid user ID.")
        reason = str(body.get("reason", "")).strip()
        self._need_reason(reason)
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            target = db.execute("SELECT id,username,display_name,role,active,account_status,approved FROM users WHERE id=? AND tenant_id=?", (user_id, actor["tenant_id"])).fetchone()
            if not target:
                raise ValueError("User account not found.")
            if target["account_status"] != "pending" or target["approved"]:
                raise ValueError("This account is no longer awaiting approval.")
            before = {"username": target["username"], "name": target["display_name"], "role": target["role"], "status": "Pending"}
            if approve:
                db.execute("DELETE FROM project_members WHERE user_id=? AND tenant_id=?", (user_id, actor["tenant_id"]))
                db.execute("UPDATE users SET account_status='active',approved=1,active=1 WHERE id=? AND tenant_id=?", (user_id, actor["tenant_id"]))
                action, after = "Approved user signup", dict(before, role=target["role"], status="Active", approved=True)
            else:
                db.execute("UPDATE users SET account_status='rejected',approved=0,active=0 WHERE id=? AND tenant_id=?", (user_id, actor["tenant_id"]))
                db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                db.execute("DELETE FROM project_members WHERE user_id=? AND tenant_id=?", (user_id, actor["tenant_id"]))
                action, after = "Rejected user signup", dict(before, status="Rejected", approved=False)
            self._audit(db, actor, action, "USER-" + str(user_id), "Administration / pending users", reason, before, after)
            self._bump_revision(db)
            rows = db.execute("SELECT id,username,display_name,role,active,account_status,approved,created_at,last_login,company,requested_project,requested_role FROM users WHERE tenant_id=? ORDER BY id", (actor["tenant_id"],)).fetchall()
        self._json(200, {"users": [self._user_json(r) for r in rows]})

    def _change_own_password(self, body: dict) -> None:
        user, _, token_hash = self._require(csrf=True)
        current_password = str(body.get("currentPassword", ""))
        password = str(body.get("password", ""))
        validate_password(password)
        if str(body.get("confirmPassword", "")) != password:
            raise ValueError("Passwords do not match.")
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute("SELECT * FROM users WHERE id=? AND tenant_id=?", (user["id"], user["tenant_id"])).fetchone()
            candidate = hash_password(current_password, latest["salt"])[1]
            if not hmac.compare_digest(candidate, latest["password_hash"]):
                raise PermissionError("Current password is incorrect.")
            salt, pwd_hash = hash_password(password)
            db.execute("UPDATE users SET salt=?,password_hash=? WHERE id=?", (salt, pwd_hash, user["id"]))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
            token, csrf = self._new_session(db, user["id"])
            self._audit(db, latest, "Changed account password", "USER-" + str(user["id"]), "Account security", "User changed their own password; other sessions revoked.", None, {"sessionsRevoked": True})
        self._json(200, {"ok": True, "csrf": csrf}, {"Set-Cookie": self._cookie(token)})

    def _bump_revision(self, db: sqlite3.Connection, project_id: str | None = None, tenant_id: str | None = None) -> int:
        db.execute("UPDATE workspace_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        if project_id and tenant_id:
            db.execute("INSERT OR IGNORE INTO project_revisions(tenant_id,project_id,revision) VALUES(?,?,0)", (tenant_id, project_id))
            db.execute("UPDATE project_revisions SET revision=revision+1 WHERE tenant_id=? AND project_id=?", (tenant_id, project_id))
            return int(db.execute("SELECT revision FROM project_revisions WHERE tenant_id=? AND project_id=?", (tenant_id, project_id)).fetchone()["revision"])
        return int(db.execute("SELECT value FROM workspace_meta WHERE key='revision'").fetchone()["value"])

    def _execution_inputs(self, db, tenant_id: str, project_id: str) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
        activities = [json.loads(r[0]) for r in db.execute("SELECT payload FROM activities WHERE tenant_id=? AND project_id=? ORDER BY activity_id", (tenant_id, project_id))]
        updates = []
        for row in db.execute("SELECT * FROM daily_updates WHERE tenant_id=? AND project_id=? ORDER BY work_date,created_at", (tenant_id, project_id)):
            payload = json.loads(row["payload_json"] or "{}")
            payload.update({"id": row["id"], "activityId": row["activity_id"], "workDate": row["work_date"],
                            "areaId": row["area_id"] or "", "subAreaId": row["sub_area_id"] or "",
                            "progress": row["progress"], "status": row["status"], "reviewedAt": row["reviewed_at"] or ""})
            updates.append(payload)
        risks = []
        for row in db.execute("SELECT * FROM project_risks WHERE tenant_id=? AND project_id=? ORDER BY created_at", (tenant_id, project_id)):
            payload = json.loads(row["payload_json"] or "{}")
            payload.update({"id": row["id"], "activityId": row["activity_id"] or "", "title": row["title"],
                            "areaId": row["area_id"] or "", "severity": row["severity"], "status": row["status"], "dueDate": row["due_date"] or ""})
            payload["payload"] = json.loads(row["payload_json"] or "{}")
            risks.append(payload)
        milestones = []
        for row in db.execute("SELECT * FROM project_milestones WHERE tenant_id=? AND project_id=? ORDER BY planned_date", (tenant_id, project_id)):
            if "payload_json" in _table_columns(db, "project_milestones"):
                payload = json.loads(row["payload_json"] or "{}")
            else:
                payload = {}
            payload.update({"id": row["id"], "activityId": row["activity_id"] or "", "name": row["name"],
                            "plannedDate": row["planned_date"], "actualDate": row["actual_date"],
                            "status": row["status"], "dependencies": json.loads(row["dependencies_json"] or "[]")})
            milestones.append(payload)
        return activities, updates, risks, milestones

    def _execution_outcomes(self, db, tenant_id: str, project_id: str) -> list[dict]:
        return [dict(json.loads(row["payload_json"] or "{}"), id=row["id"])
                for row in db.execute("SELECT id,payload_json FROM execution_records WHERE tenant_id=? AND project_id=? AND kind='outcome' AND status='Recorded' ORDER BY created_at", (tenant_id, project_id))]

    def _execution_data(self, user: sqlite3.Row, project_id: str | None = None) -> dict:
        today = datetime.now(timezone.utc).date().isoformat()
        with connect() as db:
            project, permissions = self._project_access(db, user, project_id)
            tenant_id, pid = user["tenant_id"], project["id"]
            activities, updates, risks, milestones = self._execution_inputs(db, tenant_id, pid)
            records = []
            for row in db.execute("SELECT * FROM execution_records WHERE tenant_id=? AND project_id=? ORDER BY created_at DESC", (tenant_id, pid)):
                records.append({"id": row["id"], "kind": row["kind"], "parentId": row["parent_id"] or "",
                                "status": row["status"], "payload": json.loads(row["payload_json"] or "{}"),
                                "createdAt": row["created_at"], "updatedAt": row["updated_at"]})
            seeded = any(r["kind"] == "demo_seed" for r in records)
            baseline_events = [{"id": r["id"], "version": r["version_no"], "approvedAt": r["approved_at"],
                                "activityCount": len(json.loads(r["activities_json"] or "[]"))}
                               for r in db.execute("SELECT id,version_no,approved_at,activities_json FROM baseline_revisions WHERE tenant_id=? AND project_id=? ORDER BY version_no DESC LIMIT 100", (tenant_id, pid))]
            evidence_events = [{"id": r["id"], "activityId": r["activity_id"] or "", "areaId": r["area_id"] or "",
                                "fileName": r["file_name"], "category": r["category"], "uploadedAt": r["uploaded_at"],
                                "metadata": json.loads(r["metadata_json"] or "{}")}
                               for r in db.execute("SELECT id,activity_id,area_id,file_name,category,uploaded_at,metadata_json FROM project_documents WHERE tenant_id=? AND project_id=? ORDER BY uploaded_at DESC LIMIT 300", (tenant_id, pid))]
        outcomes = [dict(r["payload"], id=r["id"]) for r in records if r["kind"] == "outcome" and r["status"] == "Recorded"]
        analysis = analyze_execution(activities, updates, risks, milestones, today, outcomes)
        activity_index = {str(item.get("id") or ""): item for item in activities}
        by_kind = {kind: [r for r in records if r["kind"] == kind] for kind in ("scenario", "recommendation", "action", "outcome")}
        thread = []
        for item in updates:
            if item.get("status") == "Approved":
                thread.append({"id": item.get("id"), "kind": "approved_update", "activityId": item.get("activityId"),
                               "areaId": item.get("areaId"), "subAreaId": item.get("subAreaId"),
                               "date": item.get("workDate"), "title": item.get("workCompleted") or item.get("issues") or "Approved daily update",
                               "sourceRecords": [item.get("id")], "evidenceIds": item.get("attachments", [])})
        for baseline in baseline_events:
            thread.append({"id": baseline["id"], "kind": "baseline", "activityId": "", "date": baseline["approvedAt"],
                           "title": f"Planner-approved baseline version {baseline['version']} ({baseline['activityCount']} activities)",
                           "sourceRecords": [baseline["id"]]})
        for evidence in evidence_events:
            thread.append({"id": evidence["id"], "kind": "evidence", "activityId": evidence["activityId"],
                           "areaId": evidence["areaId"], "date": evidence["uploadedAt"],
                           "title": evidence["fileName"] + (" · synthetic demo" if evidence["metadata"].get("syntheticDemo") else ""),
                           "sourceRecords": [evidence["id"]], "evidenceIds": [evidence["id"]]})
        for risk in risks:
            if risk.get("status") in {"Open", "Monitoring"}:
                thread.append({"id": risk.get("id"), "kind": "open_risk", "activityId": risk.get("activityId"),
                               "areaId": risk.get("areaId"),
                               "date": risk.get("createdAt", ""), "title": risk.get("title", "Open risk"), "sourceRecords": [risk.get("id")]})
        for record in records:
            if record["kind"] in {"action", "outcome", "scenario"}:
                record_activity = activity_index.get(str(record["payload"].get("activityId") or ""), {})
                thread.append({"id": record["id"], "kind": record["kind"], "activityId": record["payload"].get("activityId", ""),
                               "areaId": record_activity.get("areaId", ""), "subAreaId": record_activity.get("subAreaId", ""),
                               "date": record["createdAt"], "title": record["payload"].get("title") or record["payload"].get("result") or record["payload"].get("strategyLabel") or record["kind"],
                               "sourceRecords": record["payload"].get("sourceRecords", [])})
        return {"project": self._project_json(project), "permissions": permissions, "analysis": analysis,
                "scenarios": by_kind["scenario"], "recommendations": by_kind["recommendation"],
                "actions": by_kind["action"], "outcomes": by_kind["outcome"],
                "thread": sorted(thread, key=lambda e: (e.get("date", ""), e.get("id", "")), reverse=True),
                "demoSeeded": seeded, "activities": activities,
                "approvedUpdates": [{"id": u.get("id"), "activityId": u.get("activityId"), "workDate": u.get("workDate"), "progress": u.get("progress")}
                                    for u in updates if u.get("status") == "Approved"],
                "sourceRecordCount": len(updates) + len(risks)}

    def _execution_context(self, body: dict):
        user, _, _ = self._require({"Admin", "Project Manager", "Planner", "Supervisor"}, csrf=True)
        project_id = str(body.get("projectId") or "").strip() or None
        with connect() as db:
            project, permissions = self._project_access(db, user, project_id)
            if not permissions.get("approve") and user["role"] != "Admin":
                raise PermissionError("A planner or project manager must review execution decisions.")
            return user, project, permissions

    def _insert_execution_record(self, db, user, project_id: str, kind: str, status: str, payload: dict,
                                 parent_id: str | None = None, record_id: str | None = None) -> str:
        record_id = record_id or ("ei-" + kind + "-" + uuid.uuid4().hex[:14])
        stamp = now_iso()
        db.execute("INSERT INTO execution_records(id,tenant_id,project_id,kind,parent_id,status,payload_json,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (record_id, user["tenant_id"], project_id, kind, parent_id, status,
                    json.dumps(payload, ensure_ascii=False), user["id"], stamp, stamp))
        return record_id

    def _simulate_recovery(self, body: dict) -> None:
        user, project, _ = self._execution_context(body)
        reason = str(body.get("reason") or "").strip()
        self._need_reason(reason)
        activity_id = str(body.get("activityId") or "").strip()
        strategy = str(body.get("strategy") or "")
        try:
            days_saved = int(body.get("daysSaved"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Enter an assumed recovery of 0 to 30 calendar days.") from exc
        assumptions = str(body.get("assumptions") or "").strip()
        resource_change = str(body.get("resourceChange") or "").strip()
        if len(assumptions) < 8:
            raise ValueError("Describe the recovery assumption so another planner can review it.")
        resource_inputs = body.get("resources") if isinstance(body.get("resources"), dict) else {}
        resource_inputs = {key: resource_inputs[key] for key in (
            "currentManpower", "additionalManpower", "assumedProductivityChangePct",
            "additionalEquipment", "materialStatus", "parallelActivityId") if key in resource_inputs}
        today = datetime.now(timezone.utc).date().isoformat()
        with connect() as db:
            activities, updates, risks, milestones = self._execution_inputs(db, user["tenant_id"], project["id"])
            analysis = analyze_execution(activities, updates, risks, milestones, today, self._execution_outcomes(db, user["tenant_id"], project["id"]))
            payload = simulate_recovery(analysis, activities, activity_id, strategy, days_saved, assumptions, today, resource_change, resource_inputs)
            payload["reason"] = reason
            record_id = self._insert_execution_record(db, user, project["id"], "scenario", "Proposed", payload)
            self._audit(db, user, "Simulated recovery scenario", record_id, "Execution intelligence / recovery simulator", reason, None, payload, project_id=project["id"])
            self._control_notifications(db, user, project["id"], "recovery_proposal", "Recovery scenario needs review", f"A recovery estimate for {activity_id} is waiting for a planner decision.", "execution")
            self._bump_revision(db, project["id"], user["tenant_id"])
        return self._json(201, {"id": record_id, "status": "Proposed", "scenario": payload})

    def _decide_recovery_scenario(self, record_id: str, body: dict) -> None:
        user, project, _ = self._execution_context(body)
        decision = str(body.get("decision") or "").title()
        reason = str(body.get("reason") or "").strip()
        self._need_reason(reason)
        if decision not in {"Approved", "Rejected"}:
            raise ValueError("Choose approve or reject for this recovery estimate.")
        with connect() as db:
            row = db.execute("SELECT * FROM execution_records WHERE id=? AND tenant_id=? AND project_id=? AND kind='scenario'", (record_id, user["tenant_id"], project["id"])).fetchone()
            if not row:
                raise ValueError("Recovery scenario was not found in this project.")
            if row["status"] != "Proposed":
                raise ValueError("This recovery scenario already has a planner decision.")
            before = json.loads(row["payload_json"] or "{}")
            payload = dict(before, decision=decision, decisionReason=reason, decidedBy=user["display_name"], decidedAt=now_iso())
            if decision == "Approved" and not payload.get("scenarioFinish"):
                raise ValueError("This scenario lacks enough schedule data for a finish estimate and cannot be approved as a recovery action.")
            db.execute("UPDATE execution_records SET status=?,payload_json=?,updated_at=? WHERE id=? AND tenant_id=? AND project_id=?", (decision, json.dumps(payload, ensure_ascii=False), now_iso(), record_id, user["tenant_id"], project["id"]))
            action_id = None
            if decision == "Approved":
                action_payload = {"title": "Execute approved recovery scenario", "activityId": payload.get("activityId"),
                                  "action": payload.get("strategyLabel", "Planner-approved recovery"), "scenarioId": record_id,
                                  "sourceRecords": payload.get("sourceRecords", []), "approvedEstimate": payload.get("scenarioFinish"),
                                  "status": "Approved", "approvedBy": user["display_name"], "approvedAt": now_iso(), "reason": reason}
                action_id = self._insert_execution_record(db, user, project["id"], "action", "Approved", action_payload, record_id)
                self._control_notifications(db, user, project["id"], "execution_action", "Recovery action approved", f"A planner approved a recovery action for {payload.get('activityId', 'the project')}; field outcome is pending.", "execution")
            self._audit(db, user, "Decided recovery scenario", record_id, "Execution intelligence / recovery simulator", reason, before, payload, project_id=project["id"])
            self._bump_revision(db, project["id"], user["tenant_id"])
        return self._json(200, {"id": record_id, "decision": decision, "actionId": action_id, "scenario": payload})

    def _generate_execution_recommendations(self, body: dict) -> None:
        user, project, _ = self._execution_context(body)
        reason = str(body.get("reason") or "").strip()
        self._need_reason(reason)
        today = datetime.now(timezone.utc).date().isoformat()
        with connect() as db:
            activities, updates, risks, milestones = self._execution_inputs(db, user["tenant_id"], project["id"])
            analysis = analyze_execution(activities, updates, risks, milestones, today, self._execution_outcomes(db, user["tenant_id"], project["id"]))
            candidates = propose_recommendations(analysis, activities)
            active = db.execute("SELECT payload_json FROM execution_records WHERE tenant_id=? AND project_id=? AND kind='recommendation' AND status='Proposed'", (user["tenant_id"], project["id"])).fetchall()
            known = {(tuple(json.loads(r[0]).get("sourceRecords", [])), json.loads(r[0]).get("activityId"), json.loads(r[0]).get("category")) for r in active}
            created = []
            for payload in candidates:
                key = (tuple(payload.get("sourceRecords", [])), payload.get("activityId"), payload.get("category"))
                if key in known:
                    continue
                payload["reason"] = reason
                rid = self._insert_execution_record(db, user, project["id"], "recommendation", "Proposed", payload)
                created.append({"id": rid, "status": "Proposed", "payload": payload})
            if created:
                self._audit(db, user, "Generated next-day recommendations", ",".join(r["id"] for r in created), "Execution intelligence / next-day planner", reason, None, {"created": created}, project_id=project["id"])
                self._control_notifications(db, user, project["id"], "execution_recommendation", "Planner recommendations need review", f"{len(created)} next-day recommendation(s) were prepared for human review.", "execution")
                self._bump_revision(db, project["id"], user["tenant_id"])
        return self._json(201, {"created": created, "count": len(created)})

    def _decide_execution_recommendation(self, record_id: str, body: dict) -> None:
        user, project, _ = self._execution_context(body)
        decision = str(body.get("decision") or "").lower()
        reason = str(body.get("reason") or "").strip()
        self._need_reason(reason)
        if decision not in {"approve", "edit", "reject"}:
            raise ValueError("Choose approve, edit, or reject.")
        with connect() as db:
            row = db.execute("SELECT * FROM execution_records WHERE id=? AND tenant_id=? AND project_id=? AND kind='recommendation'", (record_id, user["tenant_id"], project["id"])).fetchone()
            if not row:
                raise ValueError("Recommendation was not found in this project.")
            if row["status"] != "Proposed":
                raise ValueError("This recommendation already has a planner decision.")
            before = json.loads(row["payload_json"] or "{}")
            payload = dict(before)
            if decision == "edit":
                action_text = str(body.get("action") or "").strip()
                if len(action_text) < 8 or len(action_text) > 1200:
                    raise ValueError("Enter the revised action as a clear instruction (8 to 1,200 characters).")
                payload.update({"action": action_text, "editedBy": user["display_name"], "editReason": reason})
                status = "Proposed"
            elif decision == "reject":
                payload.update({"decisionReason": reason, "decidedBy": user["display_name"], "decidedAt": now_iso()})
                status = "Rejected"
            else:
                payload.update({"decisionReason": reason, "decidedBy": user["display_name"], "decidedAt": now_iso()})
                status = "Approved"
            db.execute("UPDATE execution_records SET status=?,payload_json=?,updated_at=? WHERE id=? AND tenant_id=? AND project_id=?", (status, json.dumps(payload, ensure_ascii=False), now_iso(), record_id, user["tenant_id"], project["id"]))
            action_id = None
            if decision == "approve":
                action_payload = {"title": payload.get("title", "Approved next-day action"), "activityId": payload.get("activityId"),
                                  "action": payload.get("action"), "sourceRecords": payload.get("sourceRecords", []),
                                  "evidenceIds": payload.get("evidenceIds", []), "status": "Approved", "approvedBy": user["display_name"], "approvedAt": now_iso(), "reason": reason}
                action_id = self._insert_execution_record(db, user, project["id"], "action", "Approved", action_payload, record_id)
                self._control_notifications(db, user, project["id"], "execution_action", "Next-day action approved", f"A planner approved an execution action for {payload.get('activityId', 'the project')}.", "execution")
            self._audit(db, user, ("Approved" if decision == "approve" else "Edited" if decision == "edit" else "Rejected") + " planner recommendation", record_id, "Execution intelligence / next-day planner", reason, before, payload, project_id=project["id"])
            self._bump_revision(db, project["id"], user["tenant_id"])
        return self._json(200, {"id": record_id, "status": status, "actionId": action_id, "recommendation": payload})

    def _record_execution_outcome(self, action_id: str, body: dict) -> None:
        user, project, _ = self._execution_context(body)
        result = str(body.get("result") or "").strip()
        reason = str(body.get("reason") or "").strip()
        lesson = str(body.get("lesson") or "").strip()
        self._need_reason(reason)
        if len(result) < 8 or len(result) > 3000:
            raise ValueError("Describe the observed outcome in 8 to 3,000 characters.")
        if len(lesson) < 8 or len(lesson) > 1000:
            raise ValueError("Record a lesson supported by the observed result in 8 to 1,000 characters.")
        source_update_id = str(body.get("sourceUpdateId") or "").strip()
        today = datetime.now(timezone.utc).date().isoformat()
        with connect() as db:
            action = db.execute("SELECT * FROM execution_records WHERE id=? AND tenant_id=? AND project_id=? AND kind='action'", (action_id, user["tenant_id"], project["id"])).fetchone()
            if not action:
                raise ValueError("Approved action was not found in this project.")
            if action["status"] != "Approved":
                raise ValueError("Only a planner-approved action can record an execution outcome.")
            source_update = None
            if source_update_id:
                source_update = db.execute("SELECT id,status,activity_id FROM daily_updates WHERE id=? AND tenant_id=? AND project_id=?", (source_update_id, user["tenant_id"], project["id"])).fetchone()
                if not source_update or source_update["status"] != "Approved":
                    raise ValueError("Link only a planner-approved daily update from this project.")
            activities, updates, risks, milestones = self._execution_inputs(db, user["tenant_id"], project["id"])
            outcome_id = "ei-outcome-" + uuid.uuid4().hex[:14]
            existing_outcomes = self._execution_outcomes(db, user["tenant_id"], project["id"])
            forecast_before = analyze_execution(activities, updates, risks, milestones, today, existing_outcomes)
            payload_action = json.loads(action["payload_json"] or "{}")
            finish_date = str(body.get("actualFinishDate") or "").strip()
            if finish_date:
                try:
                    datetime.strptime(finish_date, "%Y-%m-%d")
                except ValueError as exc:
                    raise ValueError("Observed finish date must use YYYY-MM-DD format.") from exc
            days_saved = body.get("actualDaysSaved")
            if days_saved not in (None, ""):
                try:
                    days_saved = int(days_saved)
                except (TypeError, ValueError) as exc:
                    raise ValueError("Measured days recovered must be a whole number from 0 to 365.") from exc
                if not 0 <= days_saved <= 365:
                    raise ValueError("Measured days recovered must be a whole number from 0 to 365.")
            if not finish_date and days_saved in (None, ""):
                raise ValueError("Enter an observed finish date or measured days recovered so the forecast update has a real measure.")
            payload = {"actionId": action_id, "activityId": payload_action.get("activityId"), "result": result, "lesson": lesson,
                       "sourceUpdateId": source_update_id or None, "sourceRecords": [source_update_id] if source_update_id else payload_action.get("sourceRecords", []),
                       "recordedBy": user["display_name"], "recordedAt": now_iso(), "reason": reason,
                       "actualFinishDate": finish_date or None, "actualDaysSaved": days_saved if days_saved not in (None, "") else None,
                       "forecastBefore": forecast_before["activityForecasts"], "forecastMethod": forecast_before["method"]}
            after_outcomes = list(existing_outcomes)
            after_outcomes.append(dict(payload, id=outcome_id))
            forecast_after = analyze_execution(activities, updates, risks, milestones, today, after_outcomes)
            payload["forecastAfter"] = forecast_after["activityForecasts"]
            payload["forecastAfterMethod"] = forecast_after["method"]
            self._insert_execution_record(db, user, project["id"], "outcome", "Recorded", payload, action_id, outcome_id)
            payload_action.update({"status": "Completed", "outcomeId": outcome_id})
            db.execute("UPDATE execution_records SET status='Completed',payload_json=?,updated_at=? WHERE id=? AND tenant_id=? AND project_id=?", (json.dumps(payload_action, ensure_ascii=False), now_iso(), action_id, user["tenant_id"], project["id"]))
            activity_before = next((item for item in forecast_before["activityForecasts"] if item["activityId"] == payload.get("activityId")), None)
            activity_after = next((item for item in forecast_after["activityForecasts"] if item["activityId"] == payload.get("activityId")), None)
            audit_after = {key: value for key, value in payload.items() if key not in {"forecastBefore", "forecastAfter"}}
            audit_after.update({"forecastBefore": activity_before, "forecastAfter": activity_after})
            self._audit(db, user, "Recorded execution action outcome", outcome_id, "Execution intelligence / closed-loop outcome", reason,
                        {"action": payload_action, "forecastBefore": activity_before}, audit_after, project_id=project["id"])
            self._control_notifications(db, user, project["id"], "execution_outcome", "Execution outcome recorded", f"An observed outcome was added for {payload.get('activityId') or 'a project action'}; estimates refreshed.", "execution")
            self._bump_revision(db, project["id"], user["tenant_id"])
        return self._json(201, {"id": outcome_id, "actionId": action_id, "outcome": payload})

    def _seed_execution_storyline(self, body: dict) -> None:
        user, _, _ = self._require({"Admin"}, csrf=True)
        project_id = str(body.get("projectId") or "").strip() or None
        reason = str(body.get("reason") or "Loaded a clearly labeled synthetic execution storyline for the judge walkthrough.").strip()
        self._need_reason(reason)
        with connect() as db:
            project, _ = self._project_access(db, user, project_id)
            existing = db.execute("SELECT id FROM execution_records WHERE tenant_id=? AND project_id=? AND kind='demo_seed'", (user["tenant_id"], project["id"])).fetchone()
            if existing:
                return self._json(200, {"seeded": True, "alreadySeeded": True, "projectId": project["id"]})
            tenant_id, pid = user["tenant_id"], project["id"]
            today = datetime.now(timezone.utc).date()
            day = lambda offset: (today + timedelta(days=offset)).isoformat()
            area_id = "ei-area-" + uuid.uuid4().hex[:10]
            area_code = "EI-DEMO-" + uuid.uuid4().hex[:6].upper()
            stamp = now_iso()
            db.execute("INSERT INTO project_areas(id,tenant_id,project_id,parent_id,level,name,code,description,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (area_id, tenant_id, pid, None, "area", "Synthetic Unit 4", area_code, "Clearly labeled synthetic execution intelligence walkthrough area.", stamp, user["id"]))
            base_ids = ["EI-MAT-001", "EI-PIP-001", "EI-INS-001", "EI-COM-001"]
            existing_ids = {json.loads(r[0]).get("id") for r in db.execute("SELECT payload FROM activities WHERE tenant_id=? AND project_id=?", (tenant_id, pid))}
            ids = []
            for value in base_ids:
                candidate, suffix = value, 1
                while candidate in existing_ids:
                    suffix += 1
                    candidate = value + "-" + str(suffix)
                ids.append(candidate)
                existing_ids.add(candidate)
            specs = [
                ("Receive valve kit V-204", "Procurement", day(-6), day(-1), [], "V-204", 65, 4),
                ("Install valve assembly V-204", "Piping", day(0), day(2), [ids[0]], "V-204", 0, 3),
                ("Calibrate valve transmitter PT-204", "Instrumentation", day(3), day(4), [ids[1]], "PT-204", 0, 2),
                ("Commission synthetic cooling circuit", "Commissioning", day(5), day(6), [ids[2]], "CW-04", 0, 1),
            ]
            created_activities = []
            for index, (name, discipline, start, finish, predecessors, tag, progress, weight) in enumerate(specs):
                item = {"id": ids[index], "wbs": "9.4." + str(index + 1), "name": name, "discipline": discipline,
                        "location": "Synthetic Unit 4", "area": "Synthetic Unit 4", "areaId": area_id,
                        "lineTag": tag, "equipmentTag": tag, "aliases": [tag, name.split()[0]],
                        "plannedStart": start, "plannedFinish": finish, "actualStart": day(-3) if index == 0 else "",
                        "actualFinish": "", "progress": progress, "plannedProgress": 100 if index == 0 else 0,
                        "weight": weight, "plannedQuantity": 1, "quantityCompleted": 0, "unit": "lot",
                        "predecessors": predecessors, "owner": "Synthetic Site Team", "updateCadenceHours": 24,
                        "syntheticDemo": True, "blocker": index == 0}
                item["quantityCompleted"] = round(progress / 100, 2)
                db.execute("INSERT INTO activities(activity_id,payload,tenant_id,project_id) VALUES(?,?,?,?)",
                           (self._storage_id(pid, item["id"]), json.dumps(item, ensure_ascii=False), tenant_id, pid))
                created_activities.append(item)

            evidence_text = ("SYNTHETIC DEMO SITE DIARY\n"
                             f"Work date: {day(-1)}\nActivity: {ids[0]} Receive valve kit V-204\n"
                             "Material update: final flange kit is not yet available; partial delivery received.\n"
                             "Supervisor note: confirm remaining kit delivery before the installation crew mobilizes.\n"
                             "This text is generated synthetic demo content, not a real project record.")
            evidence = self._store_document(db, user, pid, "SYNTHETIC-demo-material-diary.txt", evidence_text.encode("utf-8"),
                                            "daily_update", area_id, ids[0], {"syntheticDemo": True, "purpose": "execution intelligence sample evidence"})
            updates = [
                {"workDate": day(-3), "progress": 35, "workCompleted": "Synthetic valve kit receiving and count started.",
                 "issues": "Partial delivery; final flange kit has not arrived.", "rootCauseCategory": "Material", "materialAvailability": "Partially available", "attachments": []},
                {"workDate": day(-1), "progress": 65, "workCompleted": "Synthetic valve kit receiving reached 65 percent; final flange kit still pending.",
                 "issues": "Final flange kit not yet available; installation depends on receipt.", "delayReason": "Material delivery constraint", "rootCauseCategory": "Material",
                 "materialAvailability": "Partially available", "attachments": [evidence["id"]]},
            ]
            for entry in updates:
                update_id = "du-ei-" + uuid.uuid4().hex[:12]
                payload = dict(entry, syntheticDemo=True, eventStatus="progress", source="Synthetic demo diary",
                               sourceType="text", reporter=user["display_name"], submittedBy=user["display_name"])
                reviewed_at = now_iso()
                db.execute("INSERT INTO daily_updates(id,tenant_id,project_id,activity_id,area_id,sub_area_id,work_date,progress,quantity_completed,status,payload_json,submitted_by,reviewed_by,reviewed_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (update_id, tenant_id, pid, ids[0], area_id, None, entry["workDate"], entry["progress"], round(entry["progress"] / 100, 2), "Approved",
                            json.dumps(payload, ensure_ascii=False), user["id"], user["id"], reviewed_at, reviewed_at, reviewed_at))
            risk_id = "risk-ei-" + uuid.uuid4().hex[:12]
            risk_payload = {"description": "Synthetic partial valve kit delivery is holding the planned installation handoff.",
                            "mitigation": "Confirm the supplier commitment and remaining kit quantity before the next shift.",
                            "probability": "Medium", "rootCauseCategory": "Material", "estimatedDelayDays": 2,
                            "syntheticDemo": True, "evidenceIds": [evidence["id"]]}
            db.execute("INSERT INTO project_risks(id,tenant_id,project_id,activity_id,area_id,title,severity,status,due_date,owner,payload_json,created_by,created_at,updated_at,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (risk_id, tenant_id, pid, ids[0], area_id, "Synthetic valve kit delivery shortfall", "High", "Open", day(0), "Synthetic Procurement Lead",
                        json.dumps(risk_payload, ensure_ascii=False), user["id"], stamp, stamp, None))
            milestone_id = "ms-ei-" + uuid.uuid4().hex[:12]
            db.execute("INSERT INTO project_milestones(id,tenant_id,project_id,activity_id,name,planned_date,actual_date,status,dependencies_json,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (milestone_id, tenant_id, pid, ids[3], "Synthetic cooling circuit ready for commissioning", day(6), "", "Planned", json.dumps([ids[0], ids[1], ids[2]]), stamp, user["id"]))
            all_activities = [json.loads(r[0]) for r in db.execute("SELECT payload FROM activities WHERE tenant_id=? AND project_id=? ORDER BY activity_id", (tenant_id, pid))]
            baseline_version = int(db.execute("SELECT COALESCE(MAX(version_no),0)+1 AS v FROM baseline_revisions WHERE tenant_id=? AND project_id=?", (tenant_id, pid)).fetchone()["v"])
            baseline_snapshot = []
            for activity in all_activities:
                base = dict(activity)
                base.update({"progress": 0, "plannedProgress": 0, "actualStart": "", "actualFinish": "", "quantityCompleted": 0})
                baseline_snapshot.append(base)
            baseline_id = "baseline-ei-" + uuid.uuid4().hex[:12]
            db.execute("INSERT INTO baseline_revisions(id,tenant_id,project_id,version_no,plan_version_id,activities_json,approved_at,approved_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (baseline_id, tenant_id, pid, baseline_version, None, json.dumps(baseline_snapshot, ensure_ascii=False), stamp, user["id"], stamp))
            seed_id = "demo-seed-" + uuid.uuid4().hex[:12]
            seed_payload = {"syntheticDemo": True, "areaId": area_id, "activityIds": ids, "sourceDocumentId": evidence["id"],
                            "riskId": risk_id, "milestoneId": milestone_id, "baselineId": baseline_id,
                            "notice": "Synthetic walkthrough data; no real project evidence."}
            self._insert_execution_record(db, user, pid, "demo_seed", "Seeded", seed_payload, record_id=seed_id)
            self._audit(db, user, "Loaded synthetic execution storyline", seed_id, "Execution intelligence / demo data", reason, None, seed_payload, project_id=pid)
            self._control_notifications(db, user, pid, "execution_demo", "Synthetic execution storyline loaded", "Synthetic material constraint, evidence, dependency chain, and milestone are ready for review.", "execution")
            self._bump_revision(db, pid, tenant_id)
        return self._json(201, {"seeded": True, "alreadySeeded": False, "projectId": project["id"], "activityIds": ids, "evidenceId": evidence["id"]})

    def _sync_state(self, body: dict) -> None:
        user, _, _ = self._require({"Admin", "Project Manager", "Planner", "Supervisor", "Contractor"}, csrf=True)
        requested_project = str(body.get("projectId", "")).strip() or None
        incoming_activities = body.get("activities")
        incoming_reports = body.get("reports")
        reason = str(body.get("reason", "")).strip()
        if not isinstance(incoming_activities, list) or not isinstance(incoming_reports, list):
            raise ValueError("State must include activity and report lists.")
        if len(incoming_activities) > 10000 or len(incoming_reports) > 10000:
            raise ValueError("This import is too large for the prototype.")
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            project, permissions = self._project_access(db, user, requested_project)
            project_id, tenant_id = project["id"], user["tenant_id"]
            if not permissions.get("submit") and not permissions.get("schedule_edit") and user["role"] != "Admin":
                raise PermissionError("You do not have permission to access this page.")
            db.execute("INSERT OR IGNORE INTO project_revisions(tenant_id,project_id,revision) VALUES(?,?,0)", (tenant_id, project_id))
            revision_query = "SELECT revision FROM project_revisions WHERE tenant_id=? AND project_id=?" + (" FOR UPDATE" if getattr(db, "is_postgres", False) else "")
            current_revision = int(db.execute(revision_query, (tenant_id, project_id)).fetchone()["revision"])
            try:
                client_revision = int(body.get("revision", -1))
            except (TypeError, ValueError):
                client_revision = -1
            if client_revision != current_revision:
                raise StateConflictError("Another team member changed the shared workspace. The latest data has been loaded; review and reapply your change.")
            old_activities = {json.loads(r["payload"])["id"]: json.loads(r["payload"]) for r in db.execute("SELECT payload FROM activities WHERE tenant_id=? AND project_id=?", (tenant_id, project_id))}
            all_reports = {json.loads(r["payload"])["id"]: json.loads(r["payload"]) for r in db.execute("SELECT payload FROM reports WHERE tenant_id=? AND project_id=?", (tenant_id, project_id))}
            old_reports = all_reports if user["role"] == "Admin" else {json.loads(r["payload"])["id"]: json.loads(r["payload"]) for r in db.execute("SELECT payload FROM reports WHERE tenant_id=? AND project_id=? AND archived=0", (tenant_id, project_id))}
            next_reports = {str(r.get("id", "")): r for r in incoming_reports if isinstance(r, dict) and r.get("id")}
            if len(next_reports) != len([r for r in incoming_reports if isinstance(r, dict) and r.get("id")]):
                raise ValueError("Report IDs must be unique.")
            for rid in old_reports:
                if rid not in next_reports:
                    raise PermissionError("Reports cannot be erased from the audit trail. Archive them with a reason instead.")
            for rid in next_reports:
                if rid in all_reports and rid not in old_reports:
                    raise ValueError("That report ID belongs to an archived record. Use a new report ID.")
            approved_changes = []
            for rid, report in next_reports.items():
                old = old_reports.get(rid)
                for field in ("actualStart", "actualEnd"):
                    value = str(report.get(field) or "")
                    if value:
                        try:
                            datetime.strptime(value, "%Y-%m-%d")
                        except ValueError:
                            raise ValueError(f"{field} must use YYYY-MM-DD format.")
                for field in ("actualStartTime", "actualEndTime"):
                    value = str(report.get(field) or "")
                    if value:
                        try:
                            parsed_time = datetime.strptime(value, "%H:%M")
                        except ValueError:
                            raise ValueError(f"{field} must use 24-hour HH:MM format.")
                        if parsed_time.strftime("%H:%M") != value:
                            raise ValueError(f"{field} must use 24-hour HH:MM format.")
                if report.get("actualStart") and report.get("actualEnd") and report["actualEnd"] < report["actualStart"]:
                    raise ValueError("Actual finish date cannot be earlier than actual start date.")
                progress = report.get("progress")
                if progress not in (None, ""):
                    try:
                        if not 0 <= float(progress) <= 100:
                            raise ValueError("Report progress must be from 0 to 100 percent.")
                    except (TypeError, ValueError) as exc:
                        if isinstance(exc, ValueError) and "from 0 to 100" in str(exc):
                            raise
                        raise ValueError("Report progress must be a number from 0 to 100 percent.")
                if old is None:
                    if not permissions.get("submit") and user["role"] != "Admin":
                        raise PermissionError("Your account cannot add reports.")
                    self._need_reason(reason)
                    source_activity_id = str(report.get("sourceActivityId") or report.get("activityId") or "").strip()
                    report["activityId"] = ""
                    report["status"] = "unmatched" if str(report.get("status", "")).lower() == "unmatched" else "pending"
                    report.pop("reviewer", None)
                    report.pop("reviewedAt", None)
                    analysis_source = dict(report, activityId=source_activity_id)
                    analysis = analyze_report(analysis_source, list(old_activities.values()))
                    requested = "" if report["status"] == "unmatched" else str(report.get("suggestedActivityId") or "")
                    selected = next((candidate for candidate in analysis.get("candidates", []) if candidate.get("activity_id") == requested), None)
                    report["suggestedActivityId"] = requested if selected else analysis.get("suggested_activity_id", "")
                    report["confidence"] = selected.get("score", 0.0) if selected else analysis.get("confidence", 0.0)
                    report["confidenceLevel"] = selected.get("level", "manual") if selected else analysis.get("confidence_level", "manual")
                    report["matchReason"] = selected.get("reasons", []) if selected else analysis.get("match_reason", [])
                    if source_activity_id:
                        report["sourceActivityId"] = source_activity_id
                    submitted_facts = report.get("extraction") if isinstance(report.get("extraction"), dict) else {}
                    report["evidence"] = analysis.get("evidence", [])
                    report["extraction"] = _validated_llm_facts(report, {"evidence": submitted_facts.get("evidence", [])}, analysis.get("facts", {}))
                    provider = str(report.get("analysisProvider") or "")
                    report["analysisProvider"] = provider if provider.startswith("OpenAI Responses API · structured output") else analysis.get("provider", "Local rules fallback")
                    report["isDefault"] = False
                    report["archived"] = False
                    report["createdBy"] = user["display_name"]
                    report["createdById"] = user["id"]
                    report["createdByRole"] = user["role"]
                    report["createdAt"] = now_iso()
                    db.execute("INSERT INTO reports(report_id,payload,archived,created_at,created_by,updated_at,tenant_id,project_id) VALUES(?,?,?,?,?,?,?,?)",
                               (self._storage_id(project_id, rid), json.dumps(report, ensure_ascii=False), 0, report["createdAt"], user["id"], report["createdAt"], tenant_id, project_id))
                    self._audit(db, user, "Added report", rid, report.get("source", "Report inbox"), reason, None, report, project_id=project_id)
                    continue
                old_archived = bool(old.get("archived"))
                new_archived = bool(report.get("archived"))
                if old_archived != new_archived:
                    if user["role"] != "Admin":
                        raise PermissionError("Only an administrator can remove or restore reports.")
                    self._need_reason(reason)
                    report["isDefault"] = old.get("isDefault", False)
                    report["createdBy"] = old.get("createdBy", "")
                    report["createdByRole"] = old.get("createdByRole", "")
                    report["createdAt"] = old.get("createdAt", "")
                    event = "Removed report" if new_archived else "Restored report"
                    self._audit(db, user, event, rid, old.get("source", "Report inbox"), reason, old, report, project_id=project_id)
                    db.execute("UPDATE reports SET payload=?,archived=?,updated_at=? WHERE report_id=? AND tenant_id=? AND project_id=?", (json.dumps(report, ensure_ascii=False), int(new_archived), now_iso(), self._storage_id(project_id, rid), tenant_id, project_id))
                    continue
                changed = {k for k in set(old) | set(report) if old.get(k) != report.get(k) and k not in {"createdAt", "createdBy", "createdByRole"}}
                if not changed:
                    continue
                self._need_reason(reason)
                if user["role"] == "Viewer":
                    raise PermissionError("Viewer accounts cannot modify records.")
                approval_fields = {"status", "activityId", "suggestedActivityId", "actualStart", "actualEnd", "actualStartTime", "actualEndTime", "progress", "reviewer", "reviewedAt"}
                if changed & approval_fields and not permissions.get("approve") and user["role"] != "Admin":
                    raise PermissionError("You do not have permission to access this page.")
                if user["role"] in {"Supervisor", "Contractor"} and not permissions.get("approve"):
                    owner_id = old.get("createdById")
                    owner_mismatch = owner_id != user["id"] if owner_id is not None else old.get("createdBy") != user["display_name"]
                    if owner_mismatch or old.get("status") != "pending" or changed - {"text", "discipline", "date", "reporter", "note", "progress", "actualStart", "actualEnd"}:
                        raise PermissionError("Supervisors and Contractors may only correct their own pending reports.")
                elif user["role"] in {"Planner", "Project Manager"}:
                    if changed & approval_fields and not permissions.get("review"):
                        raise PermissionError("You do not have permission to access this page.")
                    if changed - {"status", "activityId", "suggestedActivityId", "actualStart", "actualEnd", "actualStartTime", "actualEndTime", "progress", "note", "reviewer", "reviewedAt", "archived", "changeReason"}:
                        raise PermissionError("Planners can review reports but cannot rewrite their original source details or remove them.")
                if report.get("status") == "approved" and changed & approval_fields:
                    if not report.get("activityId"):
                        raise ValueError("Choose a schedule activity before approving this report, or keep it unmatched.")
                    if report["activityId"] not in old_activities:
                        raise ValueError("A report must link to an activity in the current schedule. Unknown activity IDs cannot be approved.")
                    facts = report.get("extraction") if isinstance(report.get("extraction"), dict) else extract_facts(report)
                    if facts.get("eventStatus") == "started" and not report.get("actualStart"):
                        raise ValueError("This report says work started but has no confirmed actual start date. Clarify the date or leave the report pending.")
                    if facts.get("eventStatus") == "complete" and not report.get("actualEnd"):
                        raise ValueError("This report says work finished but has no confirmed actual finish date. Clarify the date or leave the report pending.")
                    report["reviewer"] = user["display_name"]
                    report["reviewedAt"] = now_iso()
                    approved_changes.append(report)
                elif report.get("status") in {"unmatched", "rejected"} and changed & approval_fields:
                    report["activityId"] = ""
                    if report.get("status") == "rejected":
                        report["suggestedActivityId"] = ""
                    report["reviewer"] = user["display_name"]
                    report["reviewedAt"] = now_iso()
                report["createdBy"] = old.get("createdBy", "")
                report["createdAt"] = old.get("createdAt", "")
                report["isDefault"] = old.get("isDefault", False)
                report["createdByRole"] = old.get("createdByRole", "")
                db.execute("UPDATE reports SET payload=?,updated_at=? WHERE report_id=? AND tenant_id=? AND project_id=?", (json.dumps(report, ensure_ascii=False), now_iso(), self._storage_id(project_id, rid), tenant_id, project_id))
                self._audit(db, user, "Updated report", rid, old.get("source", "Report inbox"), reason, old, report, project_id=project_id)

            approved_activity_ids = set()
            for approved in approved_changes:
                aid = str(approved.get("activityId") or "")
                activity = next((item for item in incoming_activities if isinstance(item, dict) and str(item.get("id")) == aid), None)
                if activity is None:
                    raise ValueError("The approved report activity is missing from the submitted schedule snapshot. Refresh and retry.")
                before_activity = old_activities.get(aid, {})
                if not permissions.get("schedule_edit") and user["role"] != "Admin":
                    # An approver without general schedule-edit permission may change
                    # only fields derived from this report, never unrelated schedule data.
                    activity.clear()
                    activity.update(before_activity)
                if approved.get("actualStart"):
                    activity["actualStart"] = approved["actualStart"]
                if approved.get("actualEnd"):
                    activity["actualFinish"] = approved["actualEnd"]
                if approved.get("actualStartTime"):
                    activity["actualStartTime"] = approved["actualStartTime"]
                if approved.get("actualEndTime"):
                    activity["actualFinishTime"] = approved["actualEndTime"]
                if approved.get("progress") not in (None, ""):
                    activity["progress"] = float(approved["progress"])
                elif approved.get("actualEnd"):
                    activity["progress"] = 100
                if float(activity.get("progress") or 0) >= 100:
                    activity["progress"] = 100
                    activity["status"] = "Complete"
                    if approved.get("actualEnd"):
                        activity["actualFinish"] = approved["actualEnd"]
                elif approved.get("actualStart") or float(activity.get("progress") or 0) > 0:
                    activity["status"] = "In progress"
                activity["lastApprovedReportId"] = approved.get("id", "")
                activity["lastApprovedAt"] = approved.get("reviewedAt", now_iso())
                approved_activity_ids.add(aid)

            new_activity_ids = set()
            for activity in incoming_activities:
                if not isinstance(activity, dict) or not activity.get("id"):
                    continue
                aid = str(activity["id"])
                new_activity_ids.add(aid)
                for field in ("actualStart", "actualFinish"):
                    value = str(activity.get(field) or "")
                    if value:
                        try:
                            datetime.strptime(value, "%Y-%m-%d")
                        except ValueError:
                            raise ValueError(f"Activity {field} must use YYYY-MM-DD format.")
                for field, date_field in (("actualStartTime", "actualStart"), ("actualFinishTime", "actualFinish")):
                    value = str(activity.get(field) or "")
                    if value:
                        try:
                            parsed_time = datetime.strptime(value, "%H:%M")
                        except ValueError:
                            raise ValueError(f"Activity {field} must use 24-hour HH:MM format.")
                        if parsed_time.strftime("%H:%M") != value:
                            raise ValueError(f"Activity {field} must use 24-hour HH:MM format.")
                        if not activity.get(date_field):
                            raise ValueError(f"Activity {field} requires a corresponding actual date.")
                if activity.get("actualStart") and activity.get("actualFinish") and activity["actualFinish"] < activity["actualStart"]:
                    raise ValueError("Activity actual finish cannot be earlier than actual start.")
                progress = activity.get("progress")
                if progress not in (None, ""):
                    try:
                        numeric_progress = float(progress)
                    except (TypeError, ValueError):
                        raise ValueError("Activity progress must be a number from 0 to 100 percent.")
                    if not 0 <= numeric_progress <= 100:
                        raise ValueError("Activity progress must be from 0 to 100 percent.")
                old = old_activities.get(aid)
                if old is None:
                    if not permissions.get("schedule_edit") and user["role"] != "Admin":
                        raise PermissionError("Only planners and administrators can import schedule activities.")
                    self._need_reason(reason)
                    db.execute("INSERT INTO activities(activity_id,payload,tenant_id,project_id) VALUES(?,?,?,?)", (self._storage_id(project_id, aid), json.dumps(activity, ensure_ascii=False), tenant_id, project_id))
                    self._audit(db, user, "Added schedule activity", aid, "Activity register / schedule import", reason, None, activity, project_id=project_id)
                elif old != activity:
                    if not permissions.get("schedule_edit") and user["role"] != "Admin" and aid not in approved_activity_ids:
                        raise PermissionError("Only planners and administrators can update schedule activities.")
                    self._need_reason(reason)
                    db.execute("UPDATE activities SET payload=? WHERE activity_id=? AND tenant_id=? AND project_id=?", (json.dumps(activity, ensure_ascii=False), self._storage_id(project_id, aid), tenant_id, project_id))
                    self._audit(db, user, "Updated schedule activity", aid, "Activity register", reason, old, activity, project_id=project_id)
            if set(old_activities) - new_activity_ids:
                raise PermissionError("Schedule activities cannot be removed through this prototype.")
            self._bump_revision(db, project_id, tenant_id)
        return self._json(200, self._state(user, project_id))

    def _need_reason(self, reason: str) -> None:
        if len(reason.strip()) < 8:
            raise ValueError("Enter a reason of at least 8 characters so the audit trail explains this change.")

    def _create_user(self, body: dict) -> None:
        actor, _, _ = self._require({"Admin"}, csrf=True)
        name = str(body.get("name", "")).strip()
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        role = str(body.get("role", ""))
        reason = str(body.get("reason", "")).strip()
        if not name or not username or len(username) < 3 or len(username) > 50 or len(name) > 80:
            raise ValueError("Enter a display name and username (at least 3 characters).")
        if role not in ROLES:
            raise ValueError("Choose Admin, Project Manager, Planner, Supervisor, Contractor, or Viewer.")
        validate_password(password)
        self._need_reason(reason)
        salt, pwd_hash = hash_password(password)
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            created_at = now_iso()
            cur = db.execute("INSERT INTO users(username,display_name,role,salt,password_hash,created_at,tenant_id) VALUES(?,?,?,?,?,?,?) RETURNING id", (username, name, role, salt, pwd_hash, created_at, actor["tenant_id"]))
            uid = cur.fetchone()["id"]
            project_ids = body.get("projectIds")
            if project_ids is None:
                project_ids = [r[0] for r in db.execute("SELECT id FROM projects WHERE tenant_id=? AND active=1 ORDER BY created_at,name LIMIT 1", (actor["tenant_id"],))]
            if not isinstance(project_ids, list):
                raise ValueError("Project assignments must be a list of project IDs.")
            if role != "Admin" and not project_ids:
                raise ValueError("Assign this account to at least one project before creating it.")
            for project_id in dict.fromkeys(str(v) for v in project_ids):
                project = db.execute("SELECT id FROM projects WHERE id=? AND tenant_id=? AND active=1", (project_id, actor["tenant_id"])).fetchone()
                if not project:
                    raise ValueError("A selected project is not in this workspace.")
                if role != "Admin":
                    permissions = self._validate_permissions(role, None)
                    db.execute("INSERT INTO project_members(project_id,user_id,tenant_id,permissions,created_at) VALUES(?,?,?,?,?)", (project_id, uid, actor["tenant_id"], json.dumps(permissions), created_at))
                    self._bump_revision(db, project_id, actor["tenant_id"])
            self._audit(db, actor, "Added user account", "USER-" + str(uid), "Administration / user management", reason, None, {"username": username, "name": name, "role": role})
            self._bump_revision(db)
            rows = db.execute("SELECT id,username,display_name,role,active,account_status,approved,created_at,last_login,company,requested_project,requested_role FROM users WHERE tenant_id=? ORDER BY id", (actor["tenant_id"],)).fetchall()
        users = [self._user_json(r) for r in rows]
        self._json(201, {"users": users})

    def _change_user(self, user_id_text: str, body: dict) -> None:
        actor, _, _ = self._require({"Admin"}, csrf=True)
        try:
            user_id = int(user_id_text)
        except ValueError:
            raise ValueError("Invalid user ID.")
        reason = str(body.get("reason", "")).strip()
        self._need_reason(reason)
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            target = db.execute("SELECT id,username,display_name,role,active,account_status,approved FROM users WHERE id=? AND tenant_id=?", (user_id, actor["tenant_id"])).fetchone()
            if not target:
                raise ValueError("User account not found.")
            if user_id == actor["id"] and body.get("active") is False:
                raise ValueError("You cannot disable your own administrator account.")
            old = dict(id=target["id"], username=target["username"], name=target["display_name"], role=target["role"], active=bool(target["active"]), status=target["account_status"])
            role = body.get("role", target["role"])
            if target["account_status"] == "pending" and (role != target["role"] or "projectIds" in body):
                raise ValueError("Approve this account before assigning a role or project access.")
            active = int(bool(body.get("active", target["active"])))
            account_status = "active" if target["account_status"] == "rejected" and active else target["account_status"]
            approved = 1 if target["account_status"] == "rejected" and active else int(target["approved"])
            name = str(body.get("name", target["display_name"])).strip()
            username = str(body.get("username", target["username"])).strip()
            if not name or len(name) > 80 or len(username) < 3 or len(username) > 50:
                raise ValueError("Enter a display name and username (username must be 3–50 characters).")
            if role not in ROLES:
                raise ValueError("Choose a valid role.")
            if user_id == actor["id"] and (role != "Admin" or not active):
                raise ValueError("You cannot remove administrator access from your own account.")
            if target["role"] == "Admin" and target["active"] and (role != "Admin" or not active):
                other_admins = db.execute("SELECT COUNT(*) AS n FROM users WHERE tenant_id=? AND role='Admin' AND active=1 AND id<>?", (actor["tenant_id"], user_id)).fetchone()["n"]
                if other_admins < 1:
                    raise ValueError("The workspace must keep at least one active administrator.")
            prior_projects = [r[0] for r in db.execute("SELECT project_id FROM project_members WHERE user_id=? AND tenant_id=?", (user_id, actor["tenant_id"]))]
            db.execute("UPDATE users SET display_name=?,username=?,role=?,active=?,account_status=?,approved=? WHERE id=? AND tenant_id=?", (name, username, role, active, account_status, approved, user_id, actor["tenant_id"]))
            if role != target["role"] or "projectIds" in body:
                selected_projects = body.get("projectIds")
                if selected_projects is None:
                    selected_projects = [r[0] for r in db.execute("SELECT project_id FROM project_members WHERE user_id=? AND tenant_id=? ORDER BY project_id", (user_id, actor["tenant_id"]))]
                if not isinstance(selected_projects, list):
                    raise ValueError("Project assignments must be a list of project IDs.")
                db.execute("DELETE FROM project_members WHERE user_id=? AND tenant_id=?", (user_id, actor["tenant_id"]))
                if role == "Admin":
                    selected_projects = [r[0] for r in db.execute("SELECT id FROM projects WHERE tenant_id=? AND active=1", (actor["tenant_id"],))]
                if role != "Admin":
                    for project_id in dict.fromkeys(str(v) for v in selected_projects):
                        if not db.execute("SELECT 1 FROM projects WHERE id=? AND tenant_id=? AND active=1", (project_id, actor["tenant_id"])).fetchone():
                            raise ValueError("A selected project is not in this workspace.")
                        grants = self._validate_permissions(role, None)
                        db.execute("INSERT INTO project_members(project_id,user_id,tenant_id,permissions,created_at) VALUES(?,?,?,?,?)", (project_id, user_id, actor["tenant_id"], json.dumps(grants), now_iso()))
                for project_id in set(prior_projects + selected_projects):
                    self._bump_revision(db, project_id, actor["tenant_id"])
            if not active:
                db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            after = dict(old, name=name, username=username, role=role, active=bool(active), status=account_status)
            if bool(target["active"]) != bool(active):
                action = "Enabled user account" if active else "Disabled user account"
            elif role != target["role"]:
                action = "Changed user role"
            elif "projectIds" in body:
                action = "Changed user project access"
            else:
                action = "Edited user account"
            self._audit(db, actor, action, "USER-" + str(user_id), "Administration / user management", reason, old, after)
            self._bump_revision(db)
            rows = db.execute("SELECT id,username,display_name,role,active,account_status,approved,created_at,last_login,company,requested_project,requested_role FROM users WHERE tenant_id=? ORDER BY id", (actor["tenant_id"],)).fetchall()
        users = [self._user_json(r) for r in rows]
        self._json(200, {"users": users})

    def _reset_user_password(self, user_id_text: str, body: dict) -> None:
        actor, _, _ = self._require({"Admin"}, csrf=True)
        try:
            user_id = int(user_id_text)
        except ValueError:
            raise ValueError("Invalid user ID.")
        password = str(body.get("password", ""))
        reason = str(body.get("reason", "")).strip()
        validate_password(password)
        self._need_reason(reason)
        salt, pwd_hash = hash_password(password)
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            target = db.execute("SELECT id,username,display_name,role,active FROM users WHERE id=? AND tenant_id=?", (user_id, actor["tenant_id"])).fetchone()
            if not target:
                raise ValueError("User account not found.")
            old = {"id": target["id"], "username": target["username"], "name": target["display_name"], "role": target["role"], "active": bool(target["active"])}
            db.execute("UPDATE users SET salt=?,password_hash=? WHERE id=?", (salt, pwd_hash, user_id))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self._audit(db, actor, "Reset user password", "USER-" + str(user_id), "Administration / user management", reason, old, dict(old, sessionsRevoked=True))
            self._bump_revision(db)
            rows = db.execute("SELECT id,username,display_name,role,active,account_status,approved,created_at,last_login,company,requested_project,requested_role FROM users WHERE tenant_id=? ORDER BY id", (actor["tenant_id"],)).fetchall()
        self._json(200, {"users": [self._user_json(r) for r in rows]})


def lan_ipv4() -> str | None:
    """Return the IPv4 address selected for outbound LAN traffic, without hard-coding it."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 80))
        address = probe.getsockname()[0]
        parsed = ipaddress.ip_address(address)
        if parsed.version == 4 and not parsed.is_loopback and parsed.is_private:
            return address
    except OSError:
        pass
    finally:
        probe.close()
    try:
        candidates = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM)
        for item in candidates:
            address = item[4][0]
            parsed = ipaddress.ip_address(address)
            if parsed.version == 4 and not parsed.is_loopback and parsed.is_private:
                return address
    except OSError:
        pass
    return None


def main() -> None:
    init_db()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    address = lan_ipv4()
    print("=" * 48)
    print("SiteLink v4 server started")
    print("=" * 48)
    print("Local: http://127.0.0.1:%s" % PORT)
    if address:
        print("Team:  http://%s:%s" % (address, PORT))
    else:
        print("LAN address not detected. Run ipconfig and use your active Wi-Fi/Ethernet IPv4 address.")
    print("Direct server connections use HTTP. Use a trusted HTTPS reverse proxy before public hosting.")
    print("First run: create the workspace administrator in the browser.")
    print("Database: %s" % ("PostgreSQL (DATABASE_URL)" if DATABASE_URL.startswith(("postgres://", "postgresql://")) else DB_PATH))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nSiteLink stopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
