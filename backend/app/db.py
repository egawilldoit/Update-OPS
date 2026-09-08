"""SQLite persistence. Python 3.10 compatible, stdlib sqlite3 only.

WAL on a local filesystem, foreign keys, busy timeout, short transactions.
Never hold a transaction during a subprocess (callers follow this rule).
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA_VERSION = 1

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tools (
  id TEXT PRIMARY KEY,
  install_identity TEXT NOT NULL DEFAULT '',
  observed_version TEXT NOT NULL DEFAULT '',
  available_target TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '',
  observation_time TEXT NOT NULL DEFAULT '',
  discovery_error TEXT NOT NULL DEFAULT '',
  health TEXT NOT NULL DEFAULT 'unknown',
  health_detail TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT '',
  fingerprint TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY,
  tool_id TEXT NOT NULL REFERENCES tools(id),
  subject TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  target TEXT NOT NULL,
  target_mode TEXT NOT NULL,
  channel TEXT NOT NULL,
  services TEXT NOT NULL DEFAULT '[]',
  backup_scope TEXT NOT NULL DEFAULT '{}',
  activity_state TEXT NOT NULL DEFAULT 'unknown',
  activity_evidence TEXT NOT NULL DEFAULT '',
  used_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  tool_id TEXT NOT NULL REFERENCES tools(id),
  plan_id TEXT NOT NULL REFERENCES plans(id),
  subject TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  state TEXT NOT NULL,
  step TEXT NOT NULL,
  before_version TEXT NOT NULL DEFAULT '',
  after_version TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  started_at TEXT NOT NULL DEFAULT '',
  finished_at TEXT NOT NULL DEFAULT '',
  exit_code INTEGER NOT NULL DEFAULT 0,
  error_code TEXT NOT NULL DEFAULT '',
  error_detail TEXT NOT NULL DEFAULT '',
  ack TEXT NOT NULL DEFAULT '',
  runner_unit TEXT NOT NULL DEFAULT '',
  heartbeat TEXT NOT NULL DEFAULT '',
  recovery_required INTEGER NOT NULL DEFAULT 0,
  dispatch_nonce TEXT NOT NULL DEFAULT '',
  UNIQUE (subject, idempotency_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_single_active
  ON jobs((1)) WHERE state IN ('accepted','preflight','backup','updating','verifying');

CREATE TABLE IF NOT EXISTS checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tool_id TEXT NOT NULL DEFAULT '',
  job_id TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL,
  result TEXT NOT NULL,
  mandatory INTEGER NOT NULL DEFAULT 1,
  summary TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backups (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id),
  path TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT '{}',
  consistency TEXT NOT NULL DEFAULT '',
  size_bytes INTEGER NOT NULL DEFAULT 0,
  completed_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES jobs(id),
  created_at TEXT NOT NULL,
  event_type TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_jobs_tool_created ON jobs (tool_id, created_at);
CREATE INDEX IF NOT EXISTS ix_events_job_seq ON events (job_id, seq);
CREATE INDEX IF NOT EXISTS ix_checks_job ON checks (job_id);
"""


def connect(db_path):
    # type: (str) -> sqlite3.Connection
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, mode=0o700, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None,
                           detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def migrate(conn):
    # type: (sqlite3.Connection) -> int
    conn.executescript(_DDL)
    row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_meta(key,value) VALUES('version','1')")
    for tool_id in ("hermes", "opencode", "codex", "t3"):
        conn.execute("INSERT OR IGNORE INTO tools(id) VALUES(?)", (tool_id,))
    return SCHEMA_VERSION


def get_migration_sql():
    # type: () -> str
    return _DDL
