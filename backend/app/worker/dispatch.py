"""Singleton dispatcher: SQLite job queue -> systemd-run job runner.

- Polls for ``accepted`` jobs; claims with a dispatch nonce within 10 s or
  marks the job ``blocked`` as ``worker_unavailable`` before any mutation.
- Canonical launch (ubuntu user manager only, no sudo, no system fallback):
  systemd-run --user --collect --unit=ega-update-job-<shortid>.service ...
  ``<sys.executable> -m backend.app.worker.runner <job-id> <nonce>``.
  The nonce persists via jobs.claim_with_nonce BEFORE spawn; the runner
  verifies it at startup (H-01 replay protection).
- After spawn the launch is PROVED (unit reaches active within 10 s unless
  the job already left preflight); otherwise the job is marked blocked
  worker_unavailable instead of stranded.
- Continuous reconcile every loop pass for nonterminal claimed jobs
  (dispatch_nonce set): live unit -> touch heartbeat; dead + valid receipt
  -> apply_receipt; dead bare -> interrupted (+recovery when mutation may
  have begun). Never reruns. Boot reconcile shares the same per-row logic.
- File lock (``fcntl.flock``) guarantees a singleton dispatcher; the job
  runner owns the execution lock for the full procedure.
- Never clears a lock because a heartbeat is old. Writes
  ``<state_dir>/dispatcher.heartbeat`` JSON {ts, pid} every loop.

Python 3.10 compatible. No execution performed by importing this file.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time

from ..config import settings
from ..db import connect, migrate
from ..jobs import NONTERMINAL, claim_with_nonce
from ..schemas import utcnow_iso

LOCK_PATH = os.path.join(
    getattr(settings, "state_dir", "/var/lib/ega-update")
    or "/var/lib/ega-update", "worker.lock")
POLL_INTERVAL_S = 2
LAUNCH_PROVE_TIMEOUT_S = 10
HEARTBEAT_FILENAME = "dispatcher.heartbeat"
RUNNER_WORKDIR = "/opt/ega-update/current"
RUNNER_CONFIG_FILE = "/etc/ega-update/config.json"
_MUTATION_STATES = ("backup", "updating", "verifying")


def _singleton_lock():
    # type: () -> object
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        fh.close()
        raise SystemExit("another dispatcher holds the worker lock")
    return fh


def _state_dir():
    # type: () -> str
    return getattr(settings, "state_dir", "/var/lib/ega-update") \
        or "/var/lib/ega-update"


def _heartbeat_path():
    # type: () -> str
    return os.path.join(_state_dir(), HEARTBEAT_FILENAME)


def _write_dispatcher_heartbeat():
    # type: () -> None
    """Write <state_dir>/dispatcher.heartbeat JSON {ts, pid}. Best effort."""
    path = _heartbeat_path()
    try:
        parent = os.path.dirname(path)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, mode=0o700, exist_ok=True)
        payload = {"ts": utcnow_iso(), "pid": os.getpid()}
        tmp = "%s.tmp-%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception:
        pass


def _receipt_path(job_id):
    # type: (str) -> str
    log_dir = getattr(settings, "log_dir", "/var/lib/ega-update/logs") \
        or "/var/lib/ega-update/logs"
    return os.path.join(log_dir, "%s.receipt.json" % job_id)


def _unit_active(unit):
    # type: (str) -> str
    """Return 'active', 'inactive', or 'unknown' via the user manager."""
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
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
    """Legacy claim wrapper: generate a nonce and claim atomically."""
    return claim_with_nonce(conn, job_id, secrets.token_urlsafe(16))


def expire_unclaimed(conn, claim_deadline_s=10):
    # type: (sqlite3.Connection, int) -> None
    """Jobs still accepted past the claim window become blocked."""
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


def _mark_launch_blocked(conn, job_id, nonce, detail):
    # type: (sqlite3.Connection, str, str, str) -> None
    """Mark an unproved launch blocked without clobbering runner progress."""
    now = utcnow_iso()
    cur = conn.execute(
        "UPDATE jobs SET state='blocked', step='preflight',"
        " error_code='worker_unavailable', error_detail=?,"
        " finished_at=? WHERE id=? AND state='preflight'"
        " AND dispatch_nonce=?",
        (detail[:500], now, job_id, nonce))
    if cur.rowcount == 1:
        conn.execute(
            "INSERT INTO events(job_id,created_at,event_type,detail)"
            " VALUES(?,?,?,?)", (job_id, now, "blocked",
                                 "worker_unavailable"))
        conn.commit()


def _prove_launch(conn, job_id, unit, nonce, timeout_s=LAUNCH_PROVE_TIMEOUT_S):
    # type: (sqlite3.Connection, str, str, str, float) -> bool
    """True when the runner unit proved live or the job already progressed."""
    deadline = time.time() + max(1.0, float(timeout_s))
    while time.time() < deadline:
        if _unit_active(unit) == "active":
            return True
        try:
            row = conn.execute(
                "SELECT state, dispatch_nonce, finished_at FROM jobs"
                " WHERE id=?", (job_id,)).fetchone()
        except Exception:
            row = None
        if row is not None:
            try:
                state = row["state"]
                cur_nonce = row["dispatch_nonce"]
                finished = row["finished_at"]
            except Exception:
                state, cur_nonce, finished = "", "", ""
            if state != "preflight" or cur_nonce != nonce or finished:
                # Runner already ran/progressed (fast exit); not stranded.
                return True
        time.sleep(0.5)
    try:
        row = conn.execute(
            "SELECT state, dispatch_nonce, finished_at FROM jobs WHERE id=?",
            (job_id,)).fetchone()
    except Exception:
        return False
    if row is None:
        return False
    try:
        if row["state"] != "preflight" or row["dispatch_nonce"] != nonce \
                or row["finished_at"]:
            return True
    except Exception:
        return False
    return _unit_active(unit) == "active"


def _canonical_cmd(job_id, nonce, unit):
    # type: (str, str, str) -> list
    return [
        "systemd-run", "--user", "--collect",
        "--unit=%s" % unit,
        "--working-directory=%s" % RUNNER_WORKDIR,
        "--setenv=EGA_CONFIG_FILE=%s" % RUNNER_CONFIG_FILE,
        "--setenv=EGA_ATTEMPT_NONCE=%s" % nonce,
        "--property=KillMode=control-group",
        "--property=Restart=no",
        sys.executable, "-m", "backend.app.worker.runner",
        job_id, nonce,
    ]


def dispatch_once(conn):
    # type: (sqlite3.Connection) -> str
    """Claim one accepted job and launch its user-manager runner."""
    row = conn.execute(
        "SELECT * FROM jobs WHERE state='accepted' ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row is None:
        return ""
    job_id = row["id"]
    unit = "ega-update-job-%s.service" % job_id[:8]
    if _unit_active(unit) == "active":
        return job_id  # already running; resume observation
    nonce = secrets.token_urlsafe(16)
    if not claim_with_nonce(conn, job_id, nonce):
        return ""
    cmd = _canonical_cmd(job_id, nonce, unit)
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, shell=False, check=False)
    except Exception as exc:
        _mark_launch_blocked(conn, job_id, nonce,
                             "runner spawn failed: %s" % str(exc)[:400])
        return ""
    rc = getattr(proc, "returncode", 0)
    if rc != 0:
        try:
            err = proc.stderr
            if isinstance(err, bytes):
                err = err.decode("utf-8", errors="replace")
        except Exception:
            err = ""
        _mark_launch_blocked(
            conn, job_id, nonce,
            "systemd-run --user exit=%s %s" % (rc, (err or "")[:300]))
        return ""
    if not _prove_launch(conn, job_id, unit, nonce):
        _mark_launch_blocked(
            conn, job_id, nonce,
            "runner unit %s never reached active within %ds"
            % (unit, int(LAUNCH_PROVE_TIMEOUT_S)))
        return ""
    return job_id


def _receipt_shows_mutation(data):
    # type: (object) -> bool
    if not isinstance(data, dict):
        return False
    try:
        if str(data.get("after_version") or ""):
            return True
        checks = data.get("checks", [])
        if isinstance(checks, list) and len(checks) > 0:
            return True
        if str(data.get("backup_summary") or ""):
            return True
        if str(data.get("state") or "") in (
                "backup", "updating", "verifying", "failed",
                "interrupted", "health_failed", "succeeded"):
            return True
    except Exception:
        return False
    return False


def _read_receipt(job_id):
    # type: (str) -> tuple
    """Return (valid, data, shows_mutation) for the on-disk receipt."""
    path = _receipt_path(job_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return False, {}, False
    try:
        from ..receipts import validate_receipt
    except Exception:
        return False, {}, _receipt_shows_mutation(data) \
            if isinstance(data, dict) else False
    try:
        ok, _reason = validate_receipt(data)
    except Exception:
        return False, {}, _receipt_shows_mutation(data) \
            if isinstance(data, dict) else False
    if ok and isinstance(data, dict):
        return True, data, _receipt_shows_mutation(data)
    return False, {}, _receipt_shows_mutation(data) \
        if isinstance(data, dict) else False


def _reconcile_row(conn, row):
    # type: (sqlite3.Connection, object) -> str
    """Shared per-row reconcile: live->heartbeat; dead+receipt->apply;
    dead bare->interrupted (+recovery when mutation may have begun)."""
    try:
        job_id = row["id"]
        state = row["state"]
    except Exception:
        return "skipped"
    try:
        unit = row["runner_unit"] or ""
    except Exception:
        unit = ""
    if not unit:
        try:
            unit = "ega-update-job-%s.service" % job_id[:8]
        except Exception:
            unit = ""
    if unit and _unit_active(unit) == "active":
        try:
            conn.execute("UPDATE jobs SET heartbeat=? WHERE id=?",
                         (utcnow_iso(), job_id))
            conn.commit()
        except Exception:
            pass
        return "live"
    valid, data, shows_mutation = _read_receipt(job_id)
    if valid:
        try:
            from ..receipts import apply_receipt
            from ..jobs import set_recovery
        except Exception:
            return "skipped"
        try:
            applied_state = apply_receipt(conn, data)
        except ValueError:
            valid = False
        else:
            try:
                if applied_state in ("failed", "interrupted",
                                     "health_failed") and (
                        state in _MUTATION_STATES
                        or _receipt_shows_mutation(data)):
                    set_recovery(conn, job_id, True)
                    conn.commit()
            except Exception:
                pass
            return "applied"
    # Terminated with no (or invalid) receipt: never rerun.
    needs_recovery = (state in _MUTATION_STATES) or shows_mutation
    now = utcnow_iso()
    try:
        cur = conn.execute(
            "UPDATE jobs SET state='interrupted', error_code='interrupted',"
            " error_detail='dispatcher reconcile: unit terminated without"
            " completion proof', recovery_required=?, finished_at=?"
            " WHERE id=? AND state=?",
            (1 if needs_recovery else 0, now, job_id, state))
        if cur.rowcount != 1:
            return "skipped"
        conn.execute(
            "INSERT INTO events(job_id,created_at,event_type,detail)"
            " VALUES(?,?,?,?)", (job_id, now, "interrupted",
                                 "unproved completion"))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return "skipped"
    return "interrupted-recovery" if needs_recovery else "interrupted"


def reconcile_claimed_jobs(conn):
    # type: (sqlite3.Connection) -> int
    """Continuous reconcile for nonterminal claimed jobs (nonce set)."""
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE state IN (?,?,?,?,?)",
            NONTERMINAL).fetchall()
    except Exception:
        return 0
    acted = 0
    for row in rows:
        try:
            nonce = row["dispatch_nonce"]
        except Exception:
            nonce = ""
        if not nonce:
            continue
        outcome = _reconcile_row(conn, row)
        if outcome not in ("live", "skipped"):
            acted += 1
    return acted


def reconcile_boot(conn):
    # type: (sqlite3.Connection) -> None
    """On dispatcher start: shared per-row reconcile for all nonterminals.

    Never resumes update steps automatically. Sets recovery_required when
    mutation may have begun so the SSH-only reconcile command must confirm
    no updater remains before new jobs are accepted.
    """
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE state IN (?,?,?,?,?)",
            NONTERMINAL).fetchall()
    except Exception:
        return
    for row in rows:
        try:
            _reconcile_row(conn, row)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            continue


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
            reconcile_claimed_jobs(conn)
            _write_dispatcher_heartbeat()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
