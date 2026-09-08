-- EGA Update Console migration 004: execution leases + env fingerprint.
--
-- Ordered migration source (backend/app/db.py applier). Additive only.
-- execution_leases implements durable probe/mutation/maintenance mutual
-- exclusion (N08). plans.env_fingerprint binds preview==apply owner
-- environment (N09).

CREATE TABLE IF NOT EXISTS execution_leases (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  tool_id TEXT NOT NULL DEFAULT '',
  job_id TEXT NOT NULL DEFAULT '',
  request_id TEXT NOT NULL DEFAULT '',
  holder TEXT NOT NULL DEFAULT '',
  acquired_at TEXT NOT NULL DEFAULT '',
  expires_at TEXT NOT NULL DEFAULT '',
  released_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_leases_kind_released
  ON execution_leases (kind, released_at);

ALTER TABLE plans ADD COLUMN env_fingerprint TEXT NOT NULL DEFAULT '';
