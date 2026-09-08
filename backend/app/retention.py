"""Retention enforcement (UI/DEPLOY owned). Python 3.10 compatible, stdlib only.

Policy (CONTRACTS section 7, frozen interfaces):
- 500 MiB total completed-log cap (oldest first).
- 30d completed logs (terminal jobs with finished_at older than cutoff).
- 90d metadata (jobs/checks/events/plans rows for terminal jobs).
- Latest-two completed backups per tool (completed_at ordered, excess removed).
- Expired unused plans (expires_at past, used_at empty, unreferenced).
- Old receipts (<job-id>.receipt.json files for aged-out terminal jobs).

Protection (NEVER touch):
- Nonterminal jobs (accepted/preflight/backup/updating/verifying).
- recovery_required jobs (recovery_required=1).
- unresolved=1 jobs (when the jobs.unresolved column exists; absent column
  means no unresolved marker to protect — main agent adds it).
- Backups referenced by protected jobs.

Tombstones: rows in the tombstones table (kind, ref, reason, created_at)
are written for every removal. The table is created here when absent so
this module works against both the current schema and the hardened schema
(main agent owns migrations; dispatcher calls run_retention daily).

Filesystem layout (from settings, never hardcoded):
- logs: <log_dir>/<job-id>.jsonl
- receipts: <log_dir>/<job-id>.receipt.json
- backups: paths stored in backups.path (file or dir) under <backup_dir>

Entry point: run_retention(conn, settings) -> dict with keys
deleted_logs, deleted_backups, deleted_plans, deleted_receipts,
tombstoned, errors. Never raises; all failures are collected in errors.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Set

NONTERMINAL = ("accepted", "preflight", "backup", "updating", "verifying")
TERMINAL = ("succeeded", "blocked", "failed", "health_failed", "interrupted")

_TOMBSTONE_DDL = """
CREATE TABLE IF NOT EXISTS tombstones (
  kind TEXT NOT NULL,
  ref TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tombstones_kind_ref
  ON tombstones(kind, ref);
"""


def utcnow_iso():
    # type: () -> str
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(raw):
    # type: (object) -> Any
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    return dt


def _get(obj, key, default):
    # type: (Any, str, Any) -> Any
    try:
        if isinstance(obj, dict):
            return obj.get(key, default)
        val = getattr(obj, key, default)
        return default if val is None else val
    except Exception:
        return default


def _table_columns(conn, table):
    # type: (Any, str) -> Set[str]
    try:
        rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    except Exception:
        return set()
    cols = set()
    for r in rows:
        try:
            cols.add(str(dict(r).get("name", "")))
        except Exception:
            try:
                cols.add(str(r[1]))
            except Exception:
                continue
    return cols


def _table_exists(conn, table):
    # type: (Any, str) -> bool
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
    except Exception:
        return False
    return row is not None


def _safe_job_id(job_id):
    # type: (object) -> str
    if not isinstance(job_id, str) or not job_id:
        return ""
    if "/" in job_id or "\\" in job_id or ".." in job_id:
        return ""
    return job_id


def run_retention(conn, settings):
    # type: (Any, Any) -> Dict[str, Any]
    """Enforce retention; return counts. Never raises."""
    result = {
        "deleted_logs": 0,
        "deleted_backups": 0,
        "deleted_plans": 0,
        "deleted_receipts": 0,
        "tombstoned": 0,
        "errors": [],
    }  # type: Dict[str, Any]

    def _err(msg):
        # type: (str) -> None
        try:
            result["errors"].append(str(msg)[:500])
        except Exception:
            pass

    # Settings with documented defaults (CONTRACTS section 7).
    try:
        log_dir = str(_get(settings, "log_dir", "/var/lib/ega-update/logs") or
                      "/var/lib/ega-update/logs")
    except Exception:
        log_dir = "/var/lib/ega-update/logs"
    try:
        backup_dir = str(_get(settings, "backup_dir", "/var/lib/ega-update/backups") or
                         "/var/lib/ega-update/backups")
    except Exception:
        backup_dir = "/var/lib/ega-update/backups"
    try:
        total_cap = int(_get(settings, "total_log_cap_bytes", 500 * 1024 * 1024) or
                        (500 * 1024 * 1024))
    except Exception:
        total_cap = 500 * 1024 * 1024
    try:
        log_days = int(_get(settings, "completed_log_retention_days", 30) or 30)
    except Exception:
        log_days = 30
    try:
        meta_days = int(_get(settings, "metadata_retention_days", 90) or 90)
    except Exception:
        meta_days = 90

    now = datetime.now(timezone.utc)
    log_cutoff = now - timedelta(days=max(0, log_days))
    meta_cutoff = now - timedelta(days=max(0, meta_days))

    # Ensure tombstones table exists (idempotent; main agent may also
    # create it via migrations — CREATE IF NOT EXISTS is safe either way).
    try:
        conn.executescript(_TOMBSTONE_DDL)
    except Exception as exc:
        _err("tombstones ddl: %s" % exc)
        return result

    def _tombstone(kind, ref, reason):
        # type: (str, str, str) -> None
        try:
            if not kind or not ref:
                return
            cur = conn.execute(
                "INSERT OR IGNORE INTO tombstones(kind, ref, reason, created_at)"
                " VALUES(?,?,?,?)",
                (str(kind), str(ref), str(reason)[:500], utcnow_iso()))
            if cur.rowcount and cur.rowcount > 0:
                result["tombstoned"] += 1
        except Exception as exc:
            _err("tombstone %s %s: %s" % (kind, ref, exc))

    # Protected jobs: nonterminal OR recovery_required OR unresolved=1.
    job_cols = _table_columns(conn, "jobs")
    has_unresolved = "unresolved" in job_cols
    has_recovery = "recovery_required" in job_cols
    protected = set()  # type: Set[str]
    try:
        if has_unresolved and has_recovery:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE state IN (?,?,?,?,?)"
                " OR recovery_required=1 OR unresolved=1",
                NONTERMINAL).fetchall()
        elif has_recovery:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE state IN (?,?,?,?,?)"
                " OR recovery_required=1",
                NONTERMINAL).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE state IN (?,?,?,?,?)",
                NONTERMINAL).fetchall()
        for r in rows:
            try:
                protected.add(str(dict(r).get("id", "")))
            except Exception:
                continue
    except Exception as exc:
        _err("protected scan: %s" % exc)
    protected.discard("")

    def _is_protected(job_id):
        # type: (str) -> bool
        return job_id in protected

    # Snapshot terminal jobs with finished_at for age decisions.
    terminal_jobs = []  # type: List[Dict[str, Any]]
    try:
        cols = job_cols
        if "finished_at" in cols:
            rows = conn.execute(
                "SELECT id, tool_id, plan_id, state, finished_at FROM jobs"
                " WHERE state IN (?,?,?,?,?)",
                TERMINAL).fetchall()
            for r in rows:
                try:
                    terminal_jobs.append(dict(r))
                except Exception:
                    continue
    except Exception as exc:
        _err("terminal scan: %s" % exc)

    def _finished_dt(job):
        # type: (Dict[str, Any]) -> Any
        return _parse_ts(job.get("finished_at", ""))

    # Precompute the 90d metadata-deletion set (terminal, aged, unprotected)
    # so the latest-two backup rule keeps only survivors.
    meta_delete_ids = set()  # type: Set[str]
    for job in terminal_jobs:
        jid = str(job.get("id", "") or "")
        if not jid or _is_protected(jid):
            continue
        fdt = _finished_dt(job)
        if fdt is None:
            continue
        try:
            if fdt < meta_cutoff:
                meta_delete_ids.add(jid)
        except Exception:
            continue

    # 1. Expired unused plans: past expiry, never used, unreferenced by any
    #    surviving job row.
    try:
        if _table_exists(conn, "plans"):
            now_iso = now.isoformat()
            try:
                rows = conn.execute(
                    "SELECT id FROM plans WHERE expires_at < ?"
                    " AND (used_at IS NULL OR used_at='')",
                    (now_iso,)).fetchall()
            except Exception as exc:
                _err("plans scan: %s" % exc)
                rows = []
            for r in rows:
                try:
                    pid = str(dict(r).get("id", "") or "")
                except Exception:
                    continue
                if not pid:
                    continue
                try:
                    ref = conn.execute(
                        "SELECT COUNT(*) AS n FROM jobs WHERE plan_id=?",
                        (pid,)).fetchone()
                    n = int(dict(ref).get("n", 0) or 0) if ref is not None else 0
                except Exception:
                    n = 1
                if n > 0:
                    continue
                try:
                    conn.execute("DELETE FROM plans WHERE id=?", (pid,))
                    try:
                        conn.commit()
                    except Exception:
                        pass
                    result["deleted_plans"] += 1
                    _tombstone("plan", pid, "expired-unused-plan")
                except Exception as exc:
                    _err("plan %s: %s" % (pid, exc))
    except Exception as exc:
        _err("plans: %s" % exc)

    # 2. Completed logs older than 30d (files only; rows go in step 5).
    try:
        for job in terminal_jobs:
            jid = str(job.get("id", "") or "")
            safe = _safe_job_id(jid)
            if not safe or _is_protected(jid):
                continue
            fdt = _finished_dt(job)
            if fdt is None:
                continue
            try:
                aged = fdt < log_cutoff
            except Exception:
                continue
            if not aged:
                continue
            path = os.path.join(log_dir, safe + ".jsonl")
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    result["deleted_logs"] += 1
                    _tombstone("log", jid, "completed-log-30d")
            except Exception as exc:
                _err("log %s: %s" % (jid, exc))
        try:
            conn.commit()
        except Exception:
            pass
    except Exception as exc:
        _err("logs-30d: %s" % exc)

    # 3. 500 MiB total completed-log cap: oldest first among surviving
    #    terminal unprotected logs (excluding metadata-deleted and 30d-gone).
    try:
        entries = []  # type: List[Any]
        total = 0
        for job in terminal_jobs:
            jid = str(job.get("id", "") or "")
            safe = _safe_job_id(jid)
            if not safe or _is_protected(jid) or jid in meta_delete_ids:
                continue
            fdt = _finished_dt(job)
            if fdt is None:
                continue
            path = os.path.join(log_dir, safe + ".jsonl")
            try:
                if not os.path.isfile(path):
                    continue
                size = os.path.getsize(path)
            except Exception:
                continue
            total += int(size or 0)
            entries.append((fdt, int(size or 0), jid, path))
        entries.sort(key=lambda e: e[0])
        idx = 0
        while total > total_cap and idx < len(entries):
            _fdt, size, jid, path = entries[idx]
            idx += 1
            try:
                os.remove(path)
                total -= int(size or 0)
                result["deleted_logs"] += 1
                _tombstone("log", jid, "completed-log-total-cap")
            except Exception as exc:
                _err("log-cap %s: %s" % (jid, exc))
        try:
            conn.commit()
        except Exception:
            pass
    except Exception as exc:
        _err("logs-cap: %s" % exc)

    # 4. Latest-two completed backups per tool (survivors only; protected
    #    jobs and their backups are never touched).
    try:
        if _table_exists(conn, "backups"):
            bcols = _table_columns(conn, "backups")
            if "job_id" in bcols and "completed_at" in bcols:
                try:
                    rows = conn.execute(
                        "SELECT b.id AS bid, b.job_id AS job_id, b.path AS path,"
                        " b.completed_at AS completed_at, j.tool_id AS tool_id,"
                        " j.finished_at AS finished_at"
                        " FROM backups b LEFT JOIN jobs j ON j.id=b.job_id"
                        " ORDER BY b.completed_at DESC").fetchall()
                except Exception as exc:
                    _err("backups scan: %s" % exc)
                    rows = []
                # Group surviving unprotected backups per tool; metadata-deleted
                # jobs are excluded here (their rows go with step 5).
                seen_per_tool = {}  # type: Dict[str, int]
                for r in rows:
                    try:
                        d = dict(r)
                    except Exception:
                        continue
                    bid = str(d.get("bid", "") or "")
                    bjob = str(d.get("job_id", "") or "")
                    tool = str(d.get("tool_id", "") or "unknown")
                    if not bid:
                        continue
                    if bjob and (_is_protected(bjob) or bjob in meta_delete_ids):
                        continue
                    kept = seen_per_tool.get(tool, 0)
                    if kept < 2:
                        seen_per_tool[tool] = kept + 1
                        continue
                    # Excess: remove filesystem path then the DB row.
                    bpath = str(d.get("path", "") or "")
                    try:
                        if bpath:
                            # Constrain to the configured backup dir when the
                            # stored path is relative; absolute paths outside
                            # the dir are still honored (operator-created) but
                            # never traversed via job_id.
                            target = bpath
                            if not os.path.isabs(target):
                                target = os.path.join(backup_dir, target)
                            if os.path.isdir(target):
                                shutil.rmtree(target, ignore_errors=True)
                            elif os.path.isfile(target):
                                try:
                                    os.remove(target)
                                except FileNotFoundError:
                                    pass
                        conn.execute("DELETE FROM backups WHERE id=?", (bid,))
                        try:
                            conn.commit()
                        except Exception:
                            pass
                        result["deleted_backups"] += 1
                        _tombstone("backup", bid, "latest-two-per-tool")
                    except Exception as exc:
                        _err("backup %s: %s" % (bid, exc))
    except Exception as exc:
        _err("backups: %s" % exc)

    # 5. Old receipts: <job-id>.receipt.json files for aged-out terminal
    #    jobs (90d), plus orphan receipts with no job row past the cutoff.
    try:
        try:
            names = os.listdir(log_dir) if os.path.isdir(log_dir) else []
        except Exception as exc:
            _err("receipts list: %s" % exc)
            names = []
        job_finished = {}  # type: Dict[str, Any]
        for job in terminal_jobs:
            job_finished[str(job.get("id", "") or "")] = _finished_dt(job)
        for name in names:
            if not name.endswith(".receipt.json"):
                continue
            jid = name[: -len(".receipt.json")]
            safe = _safe_job_id(jid)
            if not safe:
                continue
            if _is_protected(jid):
                continue
            path = os.path.join(log_dir, name)
            fdt = job_finished.get(jid)
            delete = False
            if fdt is not None:
                try:
                    delete = fdt < meta_cutoff
                except Exception:
                    delete = False
            else:
                # No terminal job row: orphan receipt. Delete only when the
                # file itself is older than the metadata cutoff (never fresh
                # crash receipts).
                try:
                    mtime = os.path.getmtime(path)
                    age = now - datetime.fromtimestamp(mtime, tz=timezone.utc)
                    delete = age > timedelta(days=max(0, meta_days))
                except Exception:
                    delete = False
            if not delete:
                continue
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    result["deleted_receipts"] += 1
                    _tombstone("receipt", jid, "old-receipt-90d")
            except Exception as exc:
                _err("receipt %s: %s" % (jid, exc))
        try:
            conn.commit()
        except Exception:
            pass
    except Exception as exc:
        _err("receipts: %s" % exc)

    # 6. 90d metadata rows for terminal jobs: checks/events/backups/jobs,
    #    plus plans left orphaned by the deleted jobs.
    try:
        for jid in sorted(meta_delete_ids):
            try:
                if _table_exists(conn, "backups"):
                    conn.execute("DELETE FROM backups WHERE job_id=?", (jid,))
                conn.execute("DELETE FROM checks WHERE job_id=?", (jid,))
                conn.execute("DELETE FROM events WHERE job_id=?", (jid,))
                # Receipts table (future schema): remove rows when present.
                if _table_exists(conn, "receipts"):
                    try:
                        rcols = _table_columns(conn, "receipts")
                        if "job_id" in rcols:
                            conn.execute(
                                "DELETE FROM receipts WHERE job_id=?", (jid,))
                    except Exception as exc:
                        _err("receipts-table %s: %s" % (jid, exc))
                # Remember the plan before deleting the job row.
                plan_id = ""
                try:
                    if "plan_id" in job_cols:
                        prow = conn.execute(
                            "SELECT plan_id FROM jobs WHERE id=?",
                            (jid,)).fetchone()
                        if prow is not None:
                            plan_id = str(dict(prow).get("plan_id", "") or "")
                except Exception:
                    plan_id = ""
                conn.execute("DELETE FROM jobs WHERE id=?", (jid,))
                if plan_id and _table_exists(conn, "plans"):
                    try:
                        still = conn.execute(
                            "SELECT COUNT(*) AS n FROM jobs WHERE plan_id=?",
                            (plan_id,)).fetchone()
                        n = int(dict(still).get("n", 1) or 1) \
                            if still is not None else 1
                    except Exception:
                        n = 1
                    if n == 0:
                        try:
                            conn.execute(
                                "DELETE FROM plans WHERE id=?", (plan_id,))
                            result["deleted_plans"] += 1
                            _tombstone("plan", plan_id, "metadata-90d-orphan")
                        except Exception as exc:
                            _err("plan %s: %s" % (plan_id, exc))
                try:
                    conn.commit()
                except Exception:
                    pass
                _tombstone("job", jid, "metadata-90d")
            except Exception as exc:
                _err("metadata %s: %s" % (jid, exc))
                try:
                    conn.rollback()
                except Exception:
                    pass
    except Exception as exc:
        _err("metadata: %s" % exc)

    try:
        conn.commit()
    except Exception:
        pass
    return result
