"""SQLite persistence + ordered migrations (R31, N04-N06). Python 3.10,
stdlib only.

Single ordered migration source: backend/migrations/NNN_*.sql, tracked in
the schema_migrations ledger. migrate() applies missing migrations with a
programmatic idempotent applier (column-aware, one explicit transaction
per migration, ledger last). validate_schema() is STRICTLY read-only.

Migration ownership (N04) — the ONLY allowed migrate() callers are:
  * python -m backend.app.db migrate (controlled deploy procedure)
  * deploy/scripts/install.sh + upgrade.sh via the above entrypoint
  * test fixtures building disposable databases
API startup, worker startup, runner startup, owner probes, CLI
inspect/verify/status, job dispatch, and reconcile call validate_schema()
only and fail closed on mismatch.

WAL on a local filesystem, foreign keys, busy timeout, synchronous=FULL
for the reboot durability contract. Short transactions; never hold one
across a subprocess.
"""
from __future__ import annotations

import os
import sqlite3

CODE_VERSION = 4

MIGRATIONS = (
    (1, "001_init.sql"),
    (2, "002_execution_hardening.sql"),
    (3, "003_corrective.sql"),
    (4, "004_leases_env.sql"),
)

# Rollback compatibility: additive migrations with defaults, so older code
# keeps reading newer databases. (newer_code, older_code) pairs whose
# restore is safe without a DB backup restore.
ROLLBACK_OK = frozenset([(4, 3), (4, 2), (4, 1), (3, 2), (3, 1), (2, 1)])

# F05: complete per-version structural contracts. Every table lists ALL
# required columns (not just added ones); every version lists its index
# requirements. Inference and verification both consume these maps, so a
# partial signature (e.g. two marker columns of twenty changes) can
# never be mistaken for a complete migration.
BASE_COLUMNS = {
    "schema_meta": ("key", "value"),
    "tools": ("id", "install_identity", "observed_version",
              "available_target", "channel", "observation_time",
              "discovery_error", "health", "health_detail", "updated_at"),
    "plans": ("id", "tool_id", "subject", "created_at", "expires_at",
              "fingerprint", "target", "target_mode", "channel",
              "services", "backup_scope", "activity_state",
              "activity_evidence", "used_at"),
    "jobs": ("id", "tool_id", "plan_id", "subject", "idempotency_key",
             "request_hash", "state", "step", "before_version",
             "after_version", "created_at", "started_at", "finished_at",
             "exit_code", "error_code", "error_detail", "ack",
             "runner_unit", "heartbeat", "recovery_required"),
    "checks": ("id", "tool_id", "job_id", "name", "result", "mandatory",
               "summary", "created_at"),
    "backups": ("id", "job_id", "path", "scope", "consistency",
                "size_bytes", "completed_at"),
    "events": ("seq", "job_id", "created_at", "event_type", "detail"),
}

# version -> extra (table, column) pairs beyond the base set (complete:
# every ALTER in the corresponding migration file is represented).
REQUIRED_COLUMNS = {
    2: [("tools", "fingerprint"), ("jobs", "dispatch_nonce")],
    3: [("jobs", "attempt_claimed"), ("jobs", "claim_deadline"),
        ("jobs", "unresolved"), ("jobs", "installer_exit"),
        ("jobs", "install_outcome"), ("jobs", "actual_change"),
        ("jobs", "final_log_seq"), ("jobs", "release_path"),
        ("jobs", "canonical_unit"),
        ("plans", "plan_version"), ("plans", "install_identity"),
        ("plans", "artifact_json"), ("plans", "config_hash"),
        ("plans", "plan_hash"), ("plans", "launch_json"),
        ("plans", "state_homes_json"), ("plans", "backup_policy_json"),
        ("plans", "required_probes_json"), ("plans", "budgets_json"),
        ("plans", "space_json"), ("plans", "deadlines_json"),
        ("plans", "restart_detail"), ("plans", "activity_ts"),
        ("plans", "release_path"), ("plans", "required_space_bytes"),
        ("plans", "required_checks_json"), ("plans", "restart_impact"),
        ("plans", "steps_json"),
        ("tools", "last_success_at"), ("tools", "last_attempt_at"),
        ("tools", "last_attempt_error")],
    4: [("plans", "env_fingerprint")],
}

# version -> extra tables with their full required columns.
EXTRA_TABLES = {
    3: {
        "schema_migrations": ("version", "applied_at", "note"),
        "probe_requests": ("id", "subject", "tool_id", "op", "arg_json",
                           "created_at", "claim_deadline", "state",
                           "owner", "result_id"),
        "probe_results": ("request_id", "status", "result_json",
                          "finished_at"),
        "tombstones": ("id", "kind", "ref", "reason", "created_at"),
    },
    4: {
        "execution_leases": ("id", "kind", "subject", "tool_id", "job_id",
                             "request_id", "holder", "acquired_at",
                             "expires_at", "released_at"),
    },
}

# version -> required index names (ux_jobs_single_active additionally
# requires the constant-expression form; see _index_ok).
REQUIRED_INDEXES = {
    1: ("ux_jobs_single_active", "ix_jobs_tool_created",
        "ix_events_job_seq", "ix_checks_job"),
    3: ("ix_probe_queue", "ix_tombstones_ref"),
    4: ("ix_leases_kind_released",),
}


def _index_ok(conn, name):
    # type: (sqlite3.Connection, str) -> bool
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND"
            " name=?", (name,)).fetchone()
    except Exception:
        return False
    if row is None:
        return False
    if name == "ux_jobs_single_active":
        return "(1)" in str(row["sql"] or "")
    return True


def is_schema_version_fully_present(conn, version):
    # type: (sqlite3.Connection, int) -> bool
    """F05: prove the ENTIRE structural contract for a version.

    Cumulative: version N requires every table/column/index of versions
    1..N. A partial signature (e.g. two marker columns of twenty
    changes) returns False and the migration engine finishes the work
    instead of recording a false applied entry. Never writes.
    """
    try:
        current = int(version)
    except (TypeError, ValueError):
        return False
    if current < 1 or current > CODE_VERSION:
        return False
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tables = {str(r["name"]) for r in rows}
    except Exception:
        return False
    for table, columns in BASE_COLUMNS.items():
        if table not in tables:
            return False
        if not set(columns) <= _table_columns(conn, table):
            return False
    for version_step in range(2, current + 1):
        for table, column in REQUIRED_COLUMNS.get(version_step, []):
            if column not in _table_columns(conn, table):
                return False
        for table, columns in EXTRA_TABLES.get(version_step, {}).items():
            if table not in tables:
                return False
            if not set(columns) <= _table_columns(conn, table):
                return False
    for version_step in range(1, current + 1):
        for index in REQUIRED_INDEXES.get(version_step, ()):
            if not _index_ok(conn, index):
                return False
    return True


class SchemaError(Exception):
    """Schema incompatible, pending, or newer than this code."""


def _migrations_dir():
    # type: () -> str
    # D2: exactly one parent level — backend/migrations alongside the
    # backend/app package (two levels resolved to a nonexistent
    # <repo>/migrations and broke every fresh migration).
    return os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "migrations"))


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
    """Create the ledger (MIGRATION path only; never from validation)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL,"
        "note TEXT NOT NULL DEFAULT '')")


def _ledger_exists(conn):
    # type: (sqlite3.Connection) -> bool
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND"
            " name='schema_migrations'").fetchall()
        return len(rows) == 1
    except Exception:
        return False


def _applied_versions(conn, create=False):
    # type: (sqlite3.Connection, bool) -> set
    """Ledger contents. create=True (migrate path) may create the empty
    ledger; create=False (validate path) never writes (N06)."""
    if create:
        _ensure_ledger(conn)
    elif not _ledger_exists(conn):
        return set()
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
    """Pre-ledger databases: infer ONLY fully proven versions (F05).

    Highest fully-present version wins (contracts are cumulative, so
    proving N proves 1..N). A partial signature records nothing — the
    migration engine then completes the work instead of the ledger
    permanently blocking repair. Read-only.
    """
    for version in (CODE_VERSION, 3, 2, 1):
        if version < 1:
            continue
        try:
            if is_schema_version_fully_present(conn, version):
                return set(range(1, version + 1))
        except Exception:
            continue
    return set()


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
    """Verify the complete structural contract through `upto` (F05).

    Same maps as inference: base columns, every ALTER column, extra
    tables with full columns, and every required index (including the
    constant-expression form of the single-slot index).
    """
    try:
        upto_i = int(upto)
    except (TypeError, ValueError):
        raise SchemaError("bad verify target: %r" % (upto,))
    for table, columns in BASE_COLUMNS.items():
        if not set(columns) <= _table_columns(conn, table):
            raise SchemaError("base table %s incomplete" % table)
    for version in sorted(REQUIRED_COLUMNS):
        if version > upto_i:
            continue
        for table, column in REQUIRED_COLUMNS[version]:
            if column not in _table_columns(conn, table):
                raise SchemaError(
                    "migration %d incomplete: %s.%s missing"
                    % (version, table, column))
    for version in sorted(EXTRA_TABLES):
        if version > upto_i:
            continue
        for table, columns in EXTRA_TABLES[version].items():
            if not set(columns) <= _table_columns(conn, table):
                raise SchemaError(
                    "migration %d incomplete: %s incomplete"
                    % (version, table))
    for version in sorted(REQUIRED_INDEXES):
        if version > upto_i:
            continue
        for index in REQUIRED_INDEXES[version]:
            if not _index_ok(conn, index):
                raise SchemaError(
                    "migration %d incomplete: index %s missing/invalid"
                    % (version, index))


def _split_statements(sql):
    # type: (str) -> list
    """Split migration SQL into statements (N05, A).

    Our migration files contain only simple DDL/DML (no triggers, no
    semicolons inside string literals); full-line `--` comments are
    non-SQL and are removed BEFORE splitting, so a semicolon inside a
    comment (e.g. migration 002's header) can never fuse prose into a
    statement. Splitting the uncommented remainder on semicolons is
    exact for this corpus. If a future migration needs procedural
    logic, implement it in Python instead. Not a general SQL parser
    by design: blank lines skipped, multiline statements kept whole,
    final statement needs no trailing semicolon.
    """
    uncommented_lines = []
    for line in sql.splitlines():
        if not line.strip():
            continue
        if line.lstrip().startswith("--"):
            continue
        uncommented_lines.append(line)
    uncommented = "\n".join(uncommented_lines)
    statements = []
    for chunk in uncommented.split(";"):
        clean = chunk.strip()
        if clean:
            statements.append(clean)
    return statements


def _alter_add_column(conn, statement):
    # type: (sqlite3.Connection, str) -> str
    """Apply ALTER TABLE x ADD COLUMN c ... idempotently (N05).

    Returns skipped|applied. Inspects current columns first: present
    columns are skipped so rerun/resume after a partial failure is safe.
    Only the ADD COLUMN form is supported here; anything else raises.
    """
    import re as _re
    match = _re.match(
        r"(?is)^\s*ALTER\s+TABLE\s+(\S+)\s+ADD\s+COLUMN\s+(\S+)\s+(.*)$",
        statement)
    if not match:
        raise SchemaError("unsupported migration statement: %r"
                          % (statement[:80],))
    table, column = match.group(1), match.group(2)
    column = column.strip('"[]`')
    existing = _table_columns(conn, table)
    if column in existing:
        return "skipped"
    conn.execute(statement)
    return "applied"


def _apply_migration_file(conn, version, filename):
    # type: (sqlite3.Connection, int, str) -> None
    """Apply one migration inside ONE explicit transaction (N05).

    Column-aware (ADD COLUMN skips present columns), CREATE TABLE/INDEX
    files already carry IF NOT EXISTS, seed INSERTs use OR IGNORE.
    The ledger row is written last in the same transaction: interruption
    before commit leaves no ledger claim, and rerun completes safely.
    Raises SchemaError (after ROLLBACK) on any failure.
    """
    path = _migration_path(filename)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            sql = fh.read()
    except OSError as exc:
        raise SchemaError("migration file missing: %s (%s)"
                          % (filename, exc))
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _split_statements(sql):
            upper = statement.strip().upper()
            if upper.startswith("ALTER TABLE"):
                _alter_add_column(conn, statement)
            else:
                conn.execute(statement)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations"
            "(version,applied_at,note) VALUES(?,?,?)",
            (version, _utcnow(), filename))
        conn.execute(
            "INSERT INTO schema_meta(key,value) VALUES('version',?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(version),))
        conn.execute("COMMIT")
    except SchemaError:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise SchemaError("migration %d failed: %s" % (version, exc))
    _verify_objects(conn, version)


def migrate(conn, target=None):
    # type: (sqlite3.Connection, object) -> int
    """Apply missing migrations in order. Returns the schema version.

    Raises SchemaError when the DB is newer than this code. Each
    migration runs through _apply_migration_file (column-aware, one
    explicit transaction, ledger last): partial failure leaves the
    ledger short and rerun resumes safely.
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
    applied = _applied_versions(conn, create=True)
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
    try:
        conn.commit()
    except Exception:
        pass
    for version, filename in MIGRATIONS:
        if version > target_i or version in applied:
            continue
        _apply_migration_file(conn, version, filename)
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
    """Validate-only for service startup (N06/R31). ZERO DDL/DML.

    Raises SchemaError on pending migrations, object drift, or
    newer-than-code schemas. A missing ledger is itself pending
    migration (never created here); run the controlled migration
    procedure, which records it.
    """
    stored = _read_version(conn)
    if stored > CODE_VERSION:
        raise SchemaError(
            "database schema v%d newer than code v%d; refusing to serve"
            % (stored, CODE_VERSION))
    if not _ledger_exists(conn):
        raise SchemaError(
            "schema ledger absent (db v%d, code v%d); run the controlled"
            " migration procedure, startup validates only"
            % (stored, CODE_VERSION))
    applied = _applied_versions(conn, create=False)
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
    (migrate|validate|backup SRC DST) [--db PATH]. Deploy-owned."""
    import argparse

    ap = argparse.ArgumentParser(description="Update-OPS schema tool")
    ap.add_argument("command", choices=("migrate", "validate", "backup"))
    ap.add_argument("--db", default="")
    ap.add_argument("extra", nargs="*")
    args = ap.parse_args(argv)
    if args.command == "backup":
        if len(args.extra) != 2:
            print("usage: python -m backend.app.db backup SRC DST")
            return 2
        return _backup_command(args.extra[0], args.extra[1])
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


def _backup_command(src, dst):
    # type: (str, str) -> int
    """Consistent SQLite backup (backup API, never bare cp of a live DB)."""
    import sqlite3 as _sqlite3

    try:
        src_conn = _sqlite3.connect(src, timeout=10.0)
        dst_conn = _sqlite3.connect(dst, timeout=10.0)
    except Exception as exc:
        print("ERROR: cannot open db: %s" % exc)
        return 3
    try:
        src_conn.backup(dst_conn)
        print("backup ok: %s" % dst)
        return 0
    except Exception as exc:
        print("ERROR: backup failed: %s" % exc)
        return 3
    finally:
        try:
            src_conn.close()
        except Exception:
            pass
        try:
            dst_conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
