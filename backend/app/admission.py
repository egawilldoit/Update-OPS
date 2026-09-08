"""One shared update-admission service (N14/N15). Python 3.10 compatible.

API and CLI both call admit() — no duplicated admission logic anywhere.
Sequence per call:
  Step 1 (replay first): subject + Idempotency-Key lookup. Same payload
    returns the recorded job REGARDLESS of drain/recovery/expiry/worker
    state. Changed payload returns conflict. Replay never creates.
  Step 2 (new gates only): plan exists/valid (version, hash, subject,
    unused, unexpired) + drain absent + worker ready + no recovery +
    no conflicting probe lease + no active mutation + fresh fingerprint
    + config/release/env identity unchanged + activity ack policy.
    Step 3 (atomic reservation, N15): ONE transaction inserts the job,
    marks the plan used, acquires the mutation lease, and records the
    event. Any failure rolls back: a job can never exist while its plan
    stays logically unused, and no half-held lease survives.

Fresh owner evidence: the fingerprint comes from the CALLER (routes via
probe queue, CLI via probe queue — both owner-executed, gathered before
any transaction). Config/release/env identities are pure local reads
performed here before BEGIN — never probes, never subprocesses.
No subprocess use here. No transaction is held across probes: callers
probe first, then call admit() once.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, Tuple


def admit(conn, subject, idem_key, plan_id, ack, fresh_fp,
          worker_ready, drained):
    # type: (...) -> Tuple[str, bool, str]
    """Returns (job_id, created_new, error_code). error_code "" on success
    (created_new distinguishes 202 vs replay).

    error codes: conflict | not_found | stale_plan | maintenance |
    worker_unavailable | recovery_required | busy | fingerprint_changed |
    config_changed | activity_blocked | ack_required | disabled |
    invalid_request | unavailable.
    """
    from . import jobs as _jobs
    from . import leases as _leases
    from .plans import PlanInvalid, PlanNotFound, load_plan

    subject = str(subject or "")
    idem_key = str(idem_key or "")
    plan_id = str(plan_id or "")
    if not subject or not idem_key or not plan_id:
        return "", False, "invalid_request"
    try:
        digest = _jobs.request_hash(plan_id, bool(ack))
    except Exception:
        return "", False, "invalid_request"
    # Step 1 — replay precedes every new-admission condition.
    try:
        existing = _jobs.find_replay(conn, subject, idem_key)
    except Exception:
        return "", False, "unavailable"
    if existing is not None:
        try:
            same = (str(existing["request_hash"] or "") == digest)
            job_id = str(existing["id"])
        except Exception:
            return "", False, "unavailable"
        if same:
            return job_id, False, ""
        return "", False, "conflict"
    # Step 2 — new-admission gates (no writes yet).
    if drained:
        return "", False, "maintenance"
    if not worker_ready:
        return "", False, "worker_unavailable"
    try:
        plan = load_plan(conn, plan_id)
    except PlanNotFound:
        return "", False, "not_found"
    except PlanInvalid:
        return "", False, "stale_plan"
    except Exception:
        return "", False, "unavailable"
    tool_id = str(plan.get("tool_id", "") or "")
    if tool_id == "claude":
        return "", False, "disabled"
    if str(plan.get("subject", "") or "") != subject:
        return "", False, "invalid_request"
    if plan.get("used_at", ""):
        return "", False, "stale_plan"
    try:
        from datetime import datetime, timezone
        exp_raw = str(plan.get("expires_at", "") or "")
        exp = datetime.fromisoformat(exp_raw)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= datetime.now(timezone.utc):
            return "", False, "stale_plan"
    except ValueError:
        return "", False, "stale_plan"
    except Exception:
        return "", False, "unavailable"
    try:
        if _jobs.recovery_blocked(conn):
            return "", False, "recovery_required"
    except Exception:
        return "", False, "unavailable"
    try:
        if _jobs.active_job(conn) is not None:
            return "", False, "busy"
    except Exception:
        return "", False, "unavailable"
    try:
        if _leases.active_probe_leases(conn):
            return "", False, "busy"
    except Exception:
        return "", False, "unavailable"
    if fresh_fp and fresh_fp != str(plan.get("fingerprint", "") or ""):
        return "", False, "fingerprint_changed"
    if not fresh_fp:
        return "", False, "unavailable"
    # Local identity reads (pure config/release inspection: no probes,
    # no subprocesses, no transaction held yet).
    try:
        from .inventory import config_identity
        from .owner_env import canonical_fingerprint, resolve_release
        cur_hash = config_identity()
        cur_release = resolve_release()
        cur_env = canonical_fingerprint(None, None, cur_release)
    except Exception:
        return "", False, "unavailable"
    if not cur_hash or not cur_release or not cur_env:
        return "", False, "unavailable"
    if (cur_hash != str(plan.get("config_hash", "") or "")
            or cur_release != str(plan.get("release_path", "") or "")
            or cur_env != str(plan.get("env_fingerprint", "") or "")):
        return "", False, "config_changed"
    activity = str(plan.get("activity_state", "unknown") or "unknown")
    if activity == "busy":
        return "", False, "activity_blocked"
    if activity == "unknown" and not ack:
        return "", False, "ack_required"
    # Step 3 — atomic reservation (N15, H01): ONE transaction rechecks
    # plan unused + no active mutation + no probe lease, then creates the
    # job with its REAL UUID first and acquires the mutation lease under
    # that identity (mutation-<job-uuid>). No placeholder lease is ever
    # inserted or repaired: historical released rows can never collide
    # with future jobs. ROLLBACK on anything.
    try:
        from .schemas import utcnow_iso
        now = utcnow_iso()
    except Exception:
        return "", False, "unavailable"
    try:
        import uuid as _uuid
        job_id = str(_uuid.uuid4())
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "SELECT used_at FROM plans WHERE id=?", (plan_id,)).fetchone()
        if cur is None:
            conn.execute("ROLLBACK")
            return "", False, "stale_plan"
        if str(cur["used_at"] or ""):
            conn.execute("ROLLBACK")
            return "", False, "stale_plan"
        busy = conn.execute(
            "SELECT id FROM jobs WHERE state IN"
            " ('accepted','preflight','backup','updating','verifying')"
            " LIMIT 1").fetchone()
        if busy is not None:
            conn.execute("ROLLBACK")
            return "", False, "busy"
        if not _leases.acquire_mutation_lease(conn, job_id, subject):
            conn.execute("ROLLBACK")
            return "", False, "busy"
        try:
            from .config import settings as _settings
            window = float(getattr(_settings, "worker_claim_s", 10) or 10)
        except Exception:
            window = 10.0
        claim_deadline = _jobs._claim_deadline_from(now, window)
        before = ""
        try:
            tool = conn.execute("SELECT observed_version FROM tools"
                                " WHERE id=?", (tool_id,)).fetchone()
            before = str(tool["observed_version"]) if tool else ""
        except Exception:
            before = ""
        conn.execute(
            "INSERT INTO jobs(id,tool_id,plan_id,subject,idempotency_key,"
            "request_hash,state,step,before_version,created_at,ack,"
            "claim_deadline) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, tool_id, plan_id, subject, idem_key, digest,
             "accepted", "accepted", before, now,
             "ack" if ack else "", claim_deadline))
        conn.execute("UPDATE plans SET used_at=? WHERE id=?", (now, plan_id))
        conn.execute(
            "INSERT INTO events(job_id,created_at,event_type,detail)"
            " VALUES(?,?,?,?)", (job_id, now, "accepted", "reserved"))
        conn.execute("COMMIT")
        return job_id, True, ""
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return "", False, "unavailable"
