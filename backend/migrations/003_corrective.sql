-- EGA Update Console migration 003: corrective schema (senior review R01-R36).
--
-- Ordered migration source is backend/migrations/*.sql applied by
-- backend/app/db.py migrate() with a schema_migrations ledger. NEVER apply
-- by hand outside the controlled deploy procedure (consistent backup first).
--
-- Additive only; every new column has a default so older code keeps reading
-- newer databases (rollback compatibility, see db.ROLLBACK_OK).

-- Owner probe queue (R01): typed requests, never argv/paths/env.
CREATE TABLE IF NOT EXISTS probe_requests (
  id TEXT PRIMARY KEY,
  subject TEXT NOT NULL DEFAULT '',
  tool_id TEXT NOT NULL DEFAULT '',
  op TEXT NOT NULL DEFAULT '',
  arg_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT '',
  claim_deadline TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'queued',
  owner TEXT NOT NULL DEFAULT '',
  result_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS probe_results (
  request_id TEXT PRIMARY KEY REFERENCES probe_requests(id),
  status TEXT NOT NULL DEFAULT '',
  result_json TEXT NOT NULL DEFAULT '{}',
  finished_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_probe_queue
  ON probe_requests (state, created_at);

-- Retention tombstones (R29): missing retained logs are gaps, not empty logs.
CREATE TABLE IF NOT EXISTS tombstones (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL DEFAULT '',
  ref TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_tombstones_ref ON tombstones (kind, ref);

-- Attempt ownership + outcome separation (R03/R05/R18).
ALTER TABLE jobs ADD COLUMN attempt_claimed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN claim_deadline TEXT NOT NULL DEFAULT '';
ALTER TABLE jobs ADD COLUMN unresolved INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN installer_exit INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN install_outcome TEXT NOT NULL DEFAULT '';
ALTER TABLE jobs ADD COLUMN actual_change INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN final_log_seq INTEGER NOT NULL DEFAULT -1;
ALTER TABLE jobs ADD COLUMN release_path TEXT NOT NULL DEFAULT '';
ALTER TABLE jobs ADD COLUMN canonical_unit TEXT NOT NULL DEFAULT '';

-- Immutable versioned plans (R15).
ALTER TABLE plans ADD COLUMN plan_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE plans ADD COLUMN install_identity TEXT NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN artifact_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE plans ADD COLUMN config_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN plan_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN launch_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE plans ADD COLUMN state_homes_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE plans ADD COLUMN backup_policy_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE plans ADD COLUMN required_probes_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE plans ADD COLUMN budgets_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE plans ADD COLUMN space_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE plans ADD COLUMN deadlines_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE plans ADD COLUMN restart_detail TEXT NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN activity_ts TEXT NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN release_path TEXT NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN required_space_bytes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE plans ADD COLUMN required_checks_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE plans ADD COLUMN restart_impact TEXT NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN steps_json TEXT NOT NULL DEFAULT '[]';

-- Honest observation timestamps (R19): last success vs last attempt.
ALTER TABLE tools ADD COLUMN last_success_at TEXT NOT NULL DEFAULT '';
ALTER TABLE tools ADD COLUMN last_attempt_at TEXT NOT NULL DEFAULT '';
ALTER TABLE tools ADD COLUMN last_attempt_error TEXT NOT NULL DEFAULT '';
