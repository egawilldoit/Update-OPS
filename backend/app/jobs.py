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
    try:
        from .config import settings as _settings
        _window = float(getattr(_settings, "worker_claim_s", 10) or 10)
    except Exception:
        _window = 10.0
    claim_deadline = _claim_deadline_from(now, _window)
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    before = ""
    if plan is not None:
        tool = conn.execute("SELECT * FROM tools WHERE id=?",
                            (plan["tool_id"],)).fetchone()
        before = tool["observed_version"] if tool else ""
    try:
        conn.execute(
            "INSERT INTO jobs(id,tool_id,plan_id,subject,idempotency_key,"
            "request_hash,state,step,before_version,created_at,ack,"
            "claim_deadline)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, tool_id, plan_id, subject, idem_key, digest,
             "accepted", "accepted", before, now,
             "ack" if ack else "", claim_deadline))
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


def find_replay(conn, subject, idem_key):
    # type: (sqlite3.Connection, str, str) -> Optional[sqlite3.Row]
    """Idempotency lookup FIRST (R14): replay checks precede every new-
    admission condition (expiry, drain, recovery, fingerprint)."""
    try:
        return conn.execute(
            "SELECT * FROM jobs WHERE subject=? AND idempotency_key=?",
            (subject, idem_key)).fetchone()
    except Exception:
        return None


def expire_unclaimed(conn, claim_deadline_s=10):
    # type: (sqlite3.Connection, float) -> int
    """Legacy entry: safe expiry of never-claimed reservations.

    Delegates to expire_stale_accepted (deadline-aware, nonce-guarded).
    """
    try:
        return expire_stale_accepted(conn, claim_deadline_s)
    except Exception:
        return 0


def _claim_deadline_from(created_at_iso, deadline_s):
    # type: (str, float) -> str
    try:
        created = datetime.datetime.fromisoformat(str(created_at_iso))
        if created.tzinfo is None:
            created = created.replace(tzinfo=datetime.timezone.utc)
        return (created + datetime.timedelta(
            seconds=max(1.0, float(deadline_s)))).isoformat()
    except Exception:
        return ""


def claim_with_nonce(conn, job_id, nonce, unit="", release_path="",
                     claim_deadline_s=10):
    # type: (...) -> bool
    """Atomically claim an accepted job (R03/R17).

    Sets dispatch_nonce + canonical unit + release + preflight state in one
    UPDATE guarded by state='accepted', unclaimed nonce, AND the claim
    deadline (created_at/claim_deadline + window). A worker past its
    deadline cannot claim; the row stays accepted for safe expiry.
    Returns True only when this call performed the claim.
    """
    if not isinstance(nonce, str) or not nonce:
        return False
    if not unit:
        unit = "ega-update-job-%s.service" % (job_id or "").replace("-", "")
    now = utcnow_iso()
    try:
        row = conn.execute(
            "SELECT created_at, claim_deadline, dispatch_nonce, state"
            " FROM jobs WHERE id=?", (job_id,)).fetchone()
    except Exception:
        return False
    if row is None:
        return False
    try:
        state = row["state"]
        stored = row["dispatch_nonce"] or ""
    except Exception:
        return False
    if state != "accepted" or (stored not in ("", nonce)):
        return False
    try:
        deadline_raw = row["claim_deadline"] or ""
    except Exception:
        deadline_raw = ""
    if not deadline_raw:
        try:
            created_raw = row["created_at"] or ""
        except Exception:
            created_raw = ""
        try:
            window = float(claim_deadline_s)
        except (TypeError, ValueError):
            window = 10.0
        deadline_raw = _claim_deadline_from(created_raw, window)
    if deadline_raw and deadline_raw <= now:
        return False
    try:
        cur = conn.execute(
            "UPDATE jobs SET dispatch_nonce=?, state='preflight',"
            " step='preflight', started_at=?, heartbeat=?,"
            " runner_unit=?, canonical_unit=?, release_path=?"
            " WHERE id=? AND state='accepted'"
            " AND (dispatch_nonce='' OR dispatch_nonce=?)",
            (nonce, now, now, unit, unit, release_path or "", job_id,
             nonce))
    except sqlite3.OperationalError:
        return False
    if cur.rowcount != 1:
        return False
    conn.execute(
        "INSERT INTO events(job_id,created_at,event_type,detail)"
        " VALUES(?,?,?,?)", (job_id, now, "claimed", unit))
    conn.commit()
    return True


def consume_attempt(conn, job_id, nonce):
    # type: (sqlite3.Connection, str, str) -> bool
    """Atomically consume the one-shot runner claim (R03).

    0->1 in one UPDATE guarded by state='preflight' + nonce match. A
    duplicate same-nonce runner gets rowcount 0 and must exit without
    opening logs, acquiring resources, or transitioning state.
    """
    if not nonce:
        return False
    try:
        cur = conn.execute(
            "UPDATE jobs SET attempt_claimed=1 WHERE id=?"
            " AND state='preflight' AND dispatch_nonce=?"
            " AND attempt_claimed=0",
            (job_id, nonce))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    return cur.rowcount == 1


def expire_stale_accepted(conn, deadline_s=10):
    # type: (sqlite3.Connection, float) -> int
    """Safe expiry for never-claimed reservations (R17).

    Only accepted rows with an empty nonce past their claim_deadline
    (or created_at window when unset) become blocked. Claimed rows are
    never touched here — reconciliation owns them. Returns count.
    Safe to call from API admission AND the dispatcher.
    """
    now = utcnow_iso()
    try:
        rows = conn.execute(
            "SELECT id, created_at, claim_deadline FROM jobs"
            " WHERE state='accepted' AND (dispatch_nonce='' OR"
            " dispatch_nonce IS NULL)").fetchall()
    except Exception:
        return 0
    try:
        window = float(deadline_s)
    except (TypeError, ValueError):
        window = 10.0
    expired = []
    for row in rows:
        try:
            deadline_raw = row["claim_deadline"] or ""
        except Exception:
            deadline_raw = ""
        if not deadline_raw:
            try:
                created_raw = row["created_at"] or ""
            except Exception:
                created_raw = ""
            deadline_raw = _claim_deadline_from(created_raw, window)
        if deadline_raw and deadline_raw <= now:
            expired.append(row["id"])
    for job_id in expired:
        try:
            cur = conn.execute(
                "UPDATE jobs SET state='blocked', step='preflight',"
                " error_code='worker_unavailable',"
                " error_detail='never claimed within the claim deadline',"
                " finished_at=? WHERE id=? AND state='accepted'"
                " AND (dispatch_nonce='' OR dispatch_nonce IS NULL)",
                (now, job_id))
            if cur.rowcount == 1:
                conn.execute(
                    "INSERT INTO events(job_id,created_at,event_type,detail)"
                    " VALUES(?,?,?,?)",
                    (job_id, now, "blocked", "worker_unavailable"))
        except Exception:
            continue
    if expired:
        try:
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return 0
    return len(expired)


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
