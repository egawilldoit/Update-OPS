"""Job reservation, idempotency, and recovery helpers. Python 3.10 compatible.

Rules (SPEC §7-§8):
- ``UNIQUE(subject, idempotency_key)``; same key + same payload returns the
  original job; same key + different payload is 409.
- At most one nonterminal update job via partial unique index
  (``ux_jobs_single_active``); competing requests get 409 immediately, never wait.
- ``recovery_required`` blocks new jobs until the SSH-only reconcile command
  clears it after proving no updater remains. Never cleared by heartbeat age.
- Terminal state alone does not clear a live runner or recovery block.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sqlite3
import uuid
from typing import Dict, Optional, Tuple

from .schemas import utcnow_iso

NONTERMINAL = ("accepted", "preflight", "backup", "updating", "verifying")


def request_hash(plan_id, activity_ack):
    # type: (str, bool) -> str
    raw = json.dumps({"plan_id": plan_id, "ack": bool(activity_ack)},
                     sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def active_job(conn):
    # type: (sqlite3.Connection) -> Optional[sqlite3.Row]
    return conn.execute(
        "SELECT * FROM jobs WHERE state IN (?,?,?,?,?) ORDER BY created_at DESC LIMIT 1",
        NONTERMINAL).fetchone()


def recovery_blocked(conn):
    # type: (sqlite3.Connection) -> bool
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE recovery_required=1").fetchone()
    return bool(row and row["n"] > 0)


def reserve_job(conn, tool_id, plan_id, subject, idem_key, ack):
    # type: (sqlite3.Connection, str, str, str, str, bool) -> Tuple[str, bool, str]
    """Returns (job_id, created_new, error_code). Empty error_code on success.

    Caller must hold a short write transaction (IMMEDIATE) and commit/rollback
    promptly; never hold across a subprocess.
    """
    if recovery_blocked(conn):
        return "", False, "recovery_required"
    digest = request_hash(plan_id, ack)
    existing = conn.execute(
        "SELECT * FROM jobs WHERE subject=? AND idempotency_key=?",
        (subject, idem_key)).fetchone()
    if existing is not None:
        if existing["request_hash"] == digest:
            return existing["id"], False, ""
        return "", False, "conflict"
    if active_job(conn) is not None:
        return "", False, "busy"
    job_id = str(uuid.uuid4())
    now = utcnow_iso()
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    before = ""
    if plan is not None:
        tool = conn.execute("SELECT * FROM tools WHERE id=?",
                            (plan["tool_id"],)).fetchone()
        before = tool["observed_version"] if tool else ""
    try:
        conn.execute(
            "INSERT INTO jobs(id,tool_id,plan_id,subject,idempotency_key,"
            "request_hash,state,step,before_version,created_at,ack)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, tool_id, plan_id, subject, idem_key, digest,
             "accepted", "accepted", before, now,
             "ack" if ack else ""))
        conn.execute(
            "INSERT INTO events(job_id,created_at,event_type,detail)"
            " VALUES(?,?,?,?)", (job_id, now, "accepted", "reserved"))
    except sqlite3.IntegrityError:
        # Lost a race: re-read to distinguish idempotent replay vs busy.
        row = conn.execute(
            "SELECT * FROM jobs WHERE subject=? AND idempotency_key=?",
            (subject, idem_key)).fetchone()
        if row is not None and row["request_hash"] == digest:
            return row["id"], False, ""
        return "", False, "busy"
    return job_id, True, ""


def transition(conn, job_id, to_state, step="", error_code="", error_detail="",
               exit_code=None, after_version=None):
    # type: (...) -> None
    now = utcnow_iso()
    sets = ["state=?"]
    args = [to_state]  # type: list
    if step:
        sets.append("step=?")
        args.append(step)
    if error_code:
        sets.append("error_code=?")
        args.append(error_code)
    if error_detail:
        sets.append("error_detail=?")
        args.append(error_detail)
    if exit_code is not None:
        sets.append("exit_code=?")
        args.append(exit_code)
    if after_version is not None:
        sets.append("after_version=?")
        args.append(after_version)
    if to_state in ("preflight",):
        sets.append("started_at=?")
        args.append(now)
    if to_state in ("succeeded", "blocked", "failed", "health_failed",
                    "interrupted"):
        sets.append("finished_at=?")
        args.append(now)
    args.append(job_id)
    conn.execute("UPDATE jobs SET %s WHERE id=?" % ",".join(sets), args)
    conn.execute(
        "INSERT INTO events(job_id,created_at,event_type,detail) VALUES(?,?,?,?)",
        (job_id, now, to_state, error_code or step))


def set_recovery(conn, job_id, required=True):
    # type: (sqlite3.Connection, str, bool) -> None
    conn.execute("UPDATE jobs SET recovery_required=? WHERE id=?",
                 (1 if required else 0, job_id))
    conn.execute(
        "INSERT INTO events(job_id,created_at,event_type,detail) VALUES(?,?,?,?)",
        (job_id, utcnow_iso(), "recovery_required" if required else "recovered",
         ""))


def claim_with_nonce(conn, job_id, nonce):
    # type: (sqlite3.Connection, str, str) -> bool
    """Atomically claim an accepted job for a dispatch nonce (H-01).

    Sets dispatch_nonce + preflight state in one UPDATE so a replayed
    runner argv (wrong/empty nonce) can never claim or resume the job.
    Returns True when this call performed the claim.
    """
    if not isinstance(nonce, str) or not nonce:
        return False
    now = utcnow_iso()
    unit = "ega-update-job-%s.service" % (job_id[:8] if job_id else "")
    try:
        cur = conn.execute(
            "UPDATE jobs SET dispatch_nonce=?, state='preflight',"
            " step='preflight', started_at=?, heartbeat=?, runner_unit=?"
            " WHERE id=? AND state='accepted'"
            " AND (dispatch_nonce='' OR dispatch_nonce=?)",
            (nonce, now, now, unit, job_id, nonce))
    except sqlite3.OperationalError:
        # Pre-migration database without dispatch_nonce: refuse to claim
        # rather than launching an unprotected runner.
        return False
    if cur.rowcount != 1:
        return False
    conn.execute(
        "INSERT INTO events(job_id,created_at,event_type,detail)"
        " VALUES(?,?,?,?)", (job_id, now, "claimed", unit))
    conn.commit()
    return True


def read_dispatcher_heartbeat(state_dir, max_age_s=20):
    # type: (object, int) -> Dict[str, object]
    """Read <state_dir>/dispatcher.heartbeat JSON {ts, pid}.

    Returns {} when missing, unparseable, or stale (ts older than
    max_age_s vs UTC now). Timestamps parse via fromisoformat.
    """
    try:
        base = str(state_dir or "")
    except Exception:
        return {}
    if not base:
        return {}
    path = os.path.join(base, "dispatcher.heartbeat")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    raw_ts = data.get("ts", "")
    if not isinstance(raw_ts, str) or not raw_ts.strip():
        return {}
    text = raw_ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        ts = datetime.datetime.fromisoformat(text)
    except Exception:
        return {}
    try:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        age_s = (now - ts).total_seconds()
    except Exception:
        return {}
    try:
        limit = float(max_age_s)
    except (TypeError, ValueError):
        limit = 20.0
    if age_s > limit:
        return {}
    return data
