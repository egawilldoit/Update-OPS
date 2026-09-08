"""Singleton dispatcher: SQLite job queue -> systemd-run job runner.

- Polls for ``accepted`` jobs; claims within 10 s or marks the job ``blocked``
  as ``worker_unavailable`` before any mutation (SPEC §8 dispatch handoff).
- Persists the runner unit name before dispatch; a dispatch retry inspects
  that unit instead of spawning a duplicate.
- API restart: independent runner continues. Dispatcher restart: inspect the
  recorded unit and resume observation, never re-execute.
- Runner crash / VM reboot: reconcile unit state; record ``interrupted``
  unless a persisted completion receipt proves the outcome.
- File lock (``fcntl.flock``) guarantees a singleton dispatcher; the job
  runner owns the execution lock for the full procedure.
- Never clears a lock because a heartbeat is old.

Python 3.10 compatible. No execution performed by creating this file.
"""
from __future__ import annotations

import fcntl
import os
import sqlite3
import subprocess
import sys
import time

from ..config import settings
from ..db import connect, migrate
from ..jobs import NONTERMINAL
from ..schemas import utcnow_iso

LOCK_PATH = os.path.join(
    getattr(settings, "state_dir", "/var/lib/ega-update")
    or "/var/lib/ega-update", "worker.lock")
POLL_INTERVAL_S = 2


def _singleton_lock():
    # type: () -> object
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        fh.close()
        raise SystemExit("another dispatcher holds the worker lock")
    return fh


def _unit_active(unit):
    # type: (str) -> str
    """Return 'active', 'inactive', or 'unknown' without raising."""
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", unit],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=10, shell=False)
        state = proc.stdout.strip()
        if state == "active":
            return "active"
        if state in ("inactive", "failed", "unknown"):
            return "inactive"
        return "unknown"
    except Exception:
        return "unknown"


def claim_or_block(conn, job_id):
    # type: (sqlite3.Connection, str) -> bool
    """Move accepted->preflight and persist runner unit. True if claimed."""
    now = utcnow_iso()
    unit = "ega-update-job-%s.service" % job_id[:8]
    cur = conn.execute(
        "UPDATE jobs SET state='preflight', step='preflight', started_at=?,"
        " runner_unit=?, heartbeat=? WHERE id=? AND state='accepted'",
        (now, unit, now, job_id))
    if cur.rowcount == 1:
        conn.execute(
            "INSERT INTO events(job_id,created_at,event_type,detail)"
            " VALUES(?,?,?,?)", (job_id, now, "claimed", unit))
        conn.commit()
        return True
    return False


def expire_unclaimed(conn, claim_deadline_s=10):
    # type: (sqlite3.Connection, int) -> None
    """Jobs still accepted past the claim window become blocked."""
    # Timestamps are ISO-8601 UTC; compare lexicographically against cutoff.
    import datetime
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(seconds=claim_deadline_s)).isoformat()
    rows = conn.execute(
        "SELECT id FROM jobs WHERE state='accepted' AND created_at<?",
        (cutoff,)).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE jobs SET state='blocked', step='preflight',"
            " error_code='worker_unavailable',"
            " error_detail='dispatcher did not claim within 10s',"
            " finished_at=? WHERE id=? AND state='accepted'",
            (utcnow_iso(), row["id"]))
        conn.execute(
            "INSERT INTO events(job_id,created_at,event_type,detail)"
            " VALUES(?,?,?,?)",
            (row["id"], utcnow_iso(), "blocked", "worker_unavailable"))
    if rows:
        conn.commit()


def dispatch_once(conn):
    # type: (sqlite3.Connection) -> str
    """Claim one accepted job and launch its systemd runner. Returns job id or ''."""
    row = conn.execute(
        "SELECT * FROM jobs WHERE state='accepted' ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row is None:
        return ""
    job_id = row["id"]
    # Persist unit BEFORE spawn so retries inspect instead of duplicating.
    unit = "ega-update-job-%s.service" % job_id[:8]
    conn.execute("UPDATE jobs SET runner_unit=? WHERE id=? AND runner_unit=''",
                 (unit, job_id))
    conn.commit()
    if _unit_active(unit) == "active":
        return job_id  # already running; resume observation
    if not claim_or_block(conn, job_id):
        return ""
    cmd = [
        "systemd-run", "--collect", "--unit=%s" % unit,
        "--working-directory=/opt/ega-update/current",
        sys.executable, "-m", "backend.app.worker.runner", job_id,
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=30, shell=False, check=False)
    except Exception as exc:
        conn.execute(
            "UPDATE jobs SET state='blocked', error_code='worker_unavailable',"
            " error_detail=? WHERE id=?", (str(exc)[:500], job_id))
        conn.commit()
        return ""
    return job_id


def reconcile_boot(conn):
    # type: (sqlite3.Connection) -> None
    """On dispatcher start: mark unprovable nonterminal jobs interrupted.

    Never resumes update steps automatically. Sets recovery_required so the
    SSH-only reconcile command must confirm no updater remains before new
    jobs are accepted.
    """
    rows = conn.execute(
        "SELECT * FROM jobs WHERE state IN (?,?,?,?,?)",
        NONTERMINAL).fetchall()
    for row in rows:
        unit = row["runner_unit"] or ""
        receipt = "/var/lib/ega-update/logs/%s.receipt.json" % row["id"]
        if unit and _unit_active(unit) == "active":
            continue  # runner still alive; resume observation
        if os.path.exists(receipt):
            continue  # runner receipt proves outcome; runner.py reconciles
        now = utcnow_iso()
        conn.execute(
            "UPDATE jobs SET state='interrupted', error_code='interrupted',"
            " error_detail='dispatcher restart/reboot before completion proof',"
            " recovery_required=1, finished_at=? WHERE id=?",
            (now, row["id"]))
        conn.execute(
            "INSERT INTO events(job_id,created_at,event_type,detail)"
            " VALUES(?,?,?,?)", (row["id"], now, "interrupted",
                                 "unproved completion"))
    conn.commit()


def main():
    # type: () -> None
    _singleton_lock()
    conn = connect(settings.db_path)
    migrate(conn)
    reconcile_boot(conn)
    while True:
        try:
            expire_unclaimed(conn)
            dispatch_once(conn)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
