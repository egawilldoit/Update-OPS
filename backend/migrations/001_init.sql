-- EGA Update Console migration 001: initial schema (mirrors backend/app/db.py DDL).
--
-- Apply with sqlite3 AFTER a consistent backup (see deploy/scripts/install.sh):
--   sqlite3 /var/lib/ega-update/state.db < backend/migrations/001_init.sql
--
-- Runtime pragmas (WAL / foreign_keys / busy_timeout / synchronous) are NOT
-- set by this file: backend/app/db.py connect() applies them on every open
--   PRAGMA journal_mode=WAL;
--   PRAGMA foreign_keys=ON;
--   PRAGMA busy_timeout=5000;
--   PRAGMA synchronous=NORMAL;
-- because journal_mode and synchronous are per-connection/persistent modes
-- that must hold for every writer, not just at migrate time.

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
  updated_at TEXT NOT NULL DEFAULT ''
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
  UNIQUE (subject, idempotency_key)
);

-- Single active slot (SPEC §7): at most one nonterminal update job.
-- Constant-expression partial unique index: every nonterminal row indexes
-- the same constant (1), so a second nonterminal INSERT/UPDATE fails with
-- IntegrityError and the API returns 409 (never waits). An index on jobs(id)
-- would be ineffective here because id is already unique.
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

INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', '1');
INSERT OR IGNORE INTO tools(id) VALUES ('hermes');
INSERT OR IGNORE INTO tools(id) VALUES ('opencode');
INSERT OR IGNORE INTO tools(id) VALUES ('codex');
INSERT OR IGNORE INTO tools(id) VALUES ('t3');
