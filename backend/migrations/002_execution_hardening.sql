-- EGA Update Console migration 002: execution hardening (ALTERs only).
--
-- Apply with sqlite3 AFTER a consistent backup:
--   sqlite3 /var/lib/ega-update/state.db < backend/migrations/002_execution_hardening.sql
--
-- Adds the replay-protection nonce on jobs and the install fingerprint on
-- tools. Mirrors backend/app/db.py DDL. The partial unique index
-- ux_jobs_single_active is already correct in 001/db.py (constant-expression
-- form) and is NOT repeated here; this file contains ALTERs only.
-- Runtime pragmas are applied by backend/app/db.py connect(), not here.

ALTER TABLE tools ADD COLUMN fingerprint TEXT NOT NULL DEFAULT '';
ALTER TABLE jobs ADD COLUMN dispatch_nonce TEXT NOT NULL DEFAULT '';
