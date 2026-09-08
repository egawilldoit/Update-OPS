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

import hashlib
import json
import sqlite3
import uuid
from typing import Optional, Tuple

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
