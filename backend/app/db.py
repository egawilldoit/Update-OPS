"""SQLite persistence + ordered migrations (R31). Python 3.10, stdlib only.

Single ordered migration source: backend/migrations/NNN_*.sql, tracked in
the schema_migrations ledger. migrate() applies missing migrations in
order (transactional DDL, idempotent rerun, partial-failure resume);
validate_schema() checks version, ledger, and required objects and REJECTS
unsupported newer schemas. Routine service startup validates only; the
controlled deploy procedure owns migration.

WAL on a local filesystem, foreign keys, busy timeout, synchronous=FULL
for the reboot durability contract. Short transactions; never hold one
across a subprocess. check_same_thread=False with disciplined use: only
one phase thread ever touches a connection at a time (the main thread
blocks in join), guarded by busy_timeout + BEGIN IMMEDIATE.
"""
from __future__ import annotations

import os
import sqlite3

CODE_VERSION = 3

MIGRATIONS = (
    (1, "001_init.sql"),
    (2, "002_execution_hardening.sql"),
    (3, "003_corrective.sql"),
)

# Rollback compatibility: additive migrations with defaults, so older code
# keeps reading newer databases. (newer_code, older_code) pairs whose
# restore is safe without a DB backup restore.
ROLLBACK_OK = frozenset([(3, 2), (3, 1), (2, 1)])

BASE_TABLES = ("schema_meta", "tools", "plans", "jobs", "checks",
               "backups", "events")
V3_TABLES = BASE_TABLES + ("schema_migrations", "probe_requests",
                           "probe_results", "tombstones")

# version -> required jobs/plans/tools columns beyond the base set.
REQUIRED_COLUMNS = {
    2: [("tools", "fingerprint"), ("jobs", "dispatch_nonce")],
    3: [("jobs", "attempt_claimed"), ("jobs", "claim_deadline"),
        ("jobs", "unresolved"), ("jobs", "installer_exit"),
        ("jobs", "install_outcome"), ("jobs", "actual_change"),
        ("jobs", "final_log_seq"), ("jobs", "release_path"),
        ("jobs", "canonical_unit"),
        ("plans", "plan_version"), ("plans", "plan_hash"),
        ("plans", "config_hash"), ("plans", "release_path"),
        ("plans", "required_checks_json"), ("plans", "steps_json"),
        ("plans", "deadlines_json"),
        ("tools", "last_success_at"), ("tools", "last_attempt_at")],
}


class SchemaError(Exception):
    """Schema incompatible, pending, or newer than this code."""


def _migrations_dir():
    # type: () -> str
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "..", "migrations")


def _migration_path(filename):
    # type: (str) -> str
    return os.path.normpath(os.path.join(_migrations_dir(), filename))


def _utcnow():
    # type: () -> str
    try:
        from .schemas import utcnow_iso
        return utcnow_iso()
    except Exception:
        import datetime
        return datetime.datetime.now(
            datetime.timezone.utc).isoformat()


def connect(db_path):
    # type: (str) -> sqlite3.Connection
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, mode=0o700, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None,
                           detect_types=sqlite3.PARSE_DECLTYPES,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    # FULL (not NORMAL): terminal state + recovery flag must survive a
    # crash between commit and checkpoint (R05 reboot contract).
    conn.execute("PRAGMA synchronous=FULL;")
    return conn


def _ensure_ledger(conn):
    # type: (sqlite3.Connection) -> None
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL,"
        "note TEXT NOT NULL DEFAULT '')")


def _applied_versions(conn):
    # type: (sqlite3.Connection) -> set
    _ensure_ledger(conn)
    try:
        rows = conn.execute(
            "SELECT version FROM schema_migrations").fetchall()
        return {int(r["version"]) for r in rows}
    except Exception:
        return set()


def _table_columns(conn, table):
    # type: (sqlite3.Connection, str) -> set
    try:
        return {str(r["name"]) for r in
                conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
    except Exception:
        return set()


def _infer_applied(conn):
    # type: (sqlite3.Connection) -> set
    """Pre-ledger databases: infer from objects present (recorded as
    inferred so reruns stay idempotent)."""
    inferred = set()  # type: set
    tables = set()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tables = {str(r["name"]) for r in rows}
    except Exception:
        return inferred
    if not {"tools", "plans", "jobs"} <= tables:
        return inferred
    inferred.add(1)
    cols_tools = _table_columns(conn, "tools")
    cols_jobs = _table_columns(conn, "jobs")
    if "fingerprint" in cols_tools and "dispatch_nonce" in cols_jobs:
        inferred.add(2)
    if "attempt_claimed" in cols_jobs and \
            "plan_hash" in _table_columns(conn, "plans"):
        inferred.add(3)
    return inferred


def _read_version(conn):
    # type: (sqlite3.Connection) -> int
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
        if row is None:
            return 0
        return int(str(row["value"] or "0").strip() or 0)
    except Exception:
        return 0


def _verify_objects(conn, upto):
    # type: (sqlite3.Connection, int) -> None
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tables = {str(r["name"]) for r in rows}
    except Exception as exc:
        raise SchemaError("cannot inspect schema: %s" % exc)
    missing = [t for t in
               (V3_TABLES if upto >= 3 else BASE_TABLES)
               if t not in tables]
    if missing:
        raise SchemaError("missing tables: %s" % ",".join(missing))
    for version in sorted(REQUIRED_COLUMNS):
        if version > upto:
            continue
        for table, column in REQUIRED_COLUMNS[version]:
            if column not in _table_columns(conn, table):
                raise SchemaError(
                    "migration %d incomplete: %s.%s missing"
                    % (version, table, column))
    try:
        idx = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND"
            " name='ux_jobs_single_active'").fetchone()
    except Exception as exc:
        raise SchemaError("cannot inspect index: %s" % exc)
    if idx is None or "(1)" not in str(idx["sql"] or ""):
        raise SchemaError("ux_jobs_single_active is not the "
                          "constant-expression form")


def migrate(conn, target=None):
    # type: (sqlite3.Connection, object) -> int
    """Apply missing migrations in order. Returns the schema version.

    Raises SchemaError when the DB is newer than this code. Partial
    failure leaves the ledger short; rerun resumes (idempotent files,
    transactional DDL).
    """
    if target is None:
        target = CODE_VERSION
    try:
        target_i = int(target)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise SchemaError("bad migration target: %r" % (target,))
    stored = _read_version(conn)
    if stored > CODE_VERSION:
        raise SchemaError(
            "database schema v%d newer than code v%d; refusing"
            % (stored, CODE_VERSION))
    applied = _applied_versions(conn)
    if not applied:
        for inferred in sorted(_infer_applied(conn)):
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations"
                    "(version,applied_at,note) VALUES(?,?,?)",
                    (inferred, _utcnow(), "inferred-pre-ledger"))
                applied.add(inferred)
            except Exception:
                pass
    for version, filename in MIGRATIONS:
        if version > target_i or version in applied:
            continue
        path = _migration_path(filename)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                sql = fh.read()
        except OSError as exc:
            raise SchemaError("migration file missing: %s (%s)"
                              % (filename, exc))
        try:
            conn.executescript(sql)
        except Exception as exc:
            raise SchemaError("migration %d failed: %s" % (version, exc))
        _verify_objects(conn, version)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations"
                "(version,applied_at,note) VALUES(?,?,?)",
                (version, _utcnow(), filename))
            conn.execute(
                "INSERT INTO schema_meta(key,value) VALUES('version',?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(version),))
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            raise SchemaError("migration %d ledger failed: %s"
                              % (version, exc))
        applied.add(version)
    for tool_id in ("hermes", "opencode", "codex", "t3"):
        try:
            conn.execute("INSERT OR IGNORE INTO tools(id) VALUES(?)",
                         (tool_id,))
        except Exception:
            pass
    try:
        conn.commit()
    except Exception:
        pass
    return _read_version(conn)


def validate_schema(conn):
    # type: (sqlite3.Connection) -> int
    """Validate-only for service startup (R31). Raises SchemaError on
    pending migrations, object drift, or newer-than-code schemas."""
    stored = _read_version(conn)
    if stored > CODE_VERSION:
        raise SchemaError(
            "database schema v%d newer than code v%d; refusing to serve"
            % (stored, CODE_VERSION))
    applied = _applied_versions(conn)
    if not applied:
        applied = set(_infer_applied(conn))
    pending = [v for v, _f in MIGRATIONS if v <= CODE_VERSION
               and v not in applied]
    if pending or stored < CODE_VERSION:
        raise SchemaError(
            "pending migrations %s (db v%d, code v%d); run the controlled"
            " migration procedure, startup validates only"
            % (pending, stored, CODE_VERSION))
    _verify_objects(conn, CODE_VERSION)
    return stored


def db_version(conn):
    # type: (sqlite3.Connection) -> int
    return _read_version(conn)


def rollback_compatible(db_v, target_code_v):
    # type: (int, int) -> bool
    """True when running older code against this DB needs no backup
    restore (additive migrations with defaults only)."""
    try:
        return (int(db_v), int(target_code_v)) in ROLLBACK_OK \
            or int(target_code_v) >= int(db_v)
    except (TypeError, ValueError):
        return False


def get_migration_sql():
    # type: () -> str
    """Concatenated ordered migration sources (compat helper)."""
    parts = []
    for _version, filename in MIGRATIONS:
        try:
            with open(_migration_path(filename), "r",
                      encoding="utf-8") as fh:
                parts.append(fh.read())
        except OSError:
            continue
    return "\n".join(parts)


def main(argv=None):
    # type: (object) -> int
    """Controlled migration entry: python -m backend.app.db
    (migrate|validate) [--db PATH]. Deploy-owned; never auto-run."""
    import argparse

    ap = argparse.ArgumentParser(description="Update-OPS schema tool")
    ap.add_argument("command", choices=("migrate", "validate"))
    ap.add_argument("--db", default="")
    args = ap.parse_args(argv)
    try:
        from .config import settings
        db_path = args.db or settings.db_path
    except Exception:
        db_path = args.db or "/var/lib/ega-update/state.db"
    try:
        conn = connect(db_path)
    except Exception as exc:
        print("ERROR: cannot open db: %s" % exc)
        return 2
    try:
        if args.command == "migrate":
            version = migrate(conn)
            print("migrate ok: version=%d db=%s" % (version, db_path))
        else:
            version = validate_schema(conn)
            print("validate ok: version=%d db=%s" % (version, db_path))
        return 0
    except SchemaError as exc:
        print("ERROR: %s" % exc)
        return 3
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
