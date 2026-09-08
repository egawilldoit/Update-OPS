"""Durable execution leases: probe/mutation exclusion (N08, F02).

One transactional admission/exclusion mechanism replacing the
check-then-act race between owner probes and update reservation:

- probe lease: contending owner probes hold one while touching the
  installation. Acquired only when no mutation lease is held (and no
  conflicting maintenance for contending ops). Bounded TTL; expired
  probe leases are safely reclaimable without touching mutation leases.
- mutation lease: one per job, acquired atomically inside the reservation
  transaction. NEVER expires by time (no heartbeat-age release), is NEVER
  released opportunistically, and is disposed ONLY by tx.release_ownership()
  after a reconciler positively proves execution quiescence
  (canonical unit confirmed stopped + no execution-marked processes +
  no unresolved delegated mutation). Terminal DB state alone never
  releases it (F02: outcome != ownership).
- maintenance lease: held while drain-driven deploy work runs (optional;
  the drain file remains the admission signal; leases arbitrate probes).

All operations use explicit BEGIN IMMEDIATE transactions. Callers must
not hold transactions across subprocesses — acquire/release are instant
single statements. Python 3.10 compatible.
"""
from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Dict, List, Optional

PROBE_LEASE_TTL_S = 180

KINDS = ("probe", "mutation", "maintenance")


def _utcnow():
    # type: () -> str
    try:
        from .schemas import utcnow_iso
        return utcnow_iso()
    except Exception:
        import datetime
        return datetime.datetime.now(
            datetime.timezone.utc).isoformat()


def _tables_present(conn):
    # type: (sqlite3.Connection) -> bool
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND"
            " name='execution_leases'").fetchall()
        return len(rows) == 1
    except Exception:
        return False


def _live(conn, kind, now):
    # type: (sqlite3.Connection, str, str) -> list
    """Live (unreleased, unexpired-probe) leases of a kind."""
    try:
        if kind == "mutation":
            rows = conn.execute(
                "SELECT * FROM execution_leases WHERE kind='mutation'"
                " AND released_at=''").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM execution_leases WHERE kind=?"
                " AND released_at='' AND expires_at>?",
                (kind, now)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def active_probe_leases(conn):
    # type: (sqlite3.Connection) -> List[Dict[str, Any]]
    """Unexpired, unreleased probe leases (mutation must respect these)."""
    return _live(conn, "probe", _utcnow())


def active_mutation_lease(conn):
    # type: (sqlite3.Connection) -> Optional[Dict[str, Any]]
    """The held mutation lease, if any (never time-expires)."""
    rows = _live(conn, "mutation", _utcnow())
    return rows[0] if rows else None


def acquire_probe_lease(conn, tool_id, holder, ttl_s=PROBE_LEASE_TTL_S):
    # type: (sqlite3.Connection, str, str, float) -> Optional[str]
    """Atomically acquire a bounded probe lease, or None.

    Refused when a mutation lease is held (probes must not race
    mutation). Maintenance drain does NOT block read probes. Caller
    must already hold NO transaction; this manages its own short one.
    """
    if not _tables_present(conn):
        return None
    import datetime
    now = _utcnow()
    try:
        ttl = max(10.0, float(ttl_s))
    except (TypeError, ValueError):
        ttl = float(PROBE_LEASE_TTL_S)
    try:
        expires = (datetime.datetime.now(datetime.timezone.utc)
                   + datetime.timedelta(seconds=ttl)).isoformat()
    except Exception:
        return None
    lease_id = "probe-%s" % uuid.uuid4().hex
    try:
        conn.execute("BEGIN IMMEDIATE")
        held = conn.execute(
            "SELECT id FROM execution_leases WHERE kind='mutation'"
            " AND released_at='' LIMIT 1").fetchone()
        if held is not None:
            conn.execute("ROLLBACK")
            return None
        conn.execute(
            "INSERT INTO execution_leases(id,kind,subject,tool_id,job_id,"
            "request_id,holder,acquired_at,expires_at,released_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (lease_id, "probe", "", tool_id or "", "", "", holder or "",
             now, expires, ""))
        conn.execute("COMMIT")
        return lease_id
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return None


def release_lease(conn, lease_id):
    # type: (sqlite3.Connection, str) -> bool
    """Release a lease idempotently. Returns True when held-then-released
    or already released; False on storage failure."""
    if not lease_id or not _tables_present(conn):
        return False
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE execution_leases SET released_at=? WHERE id=?"
            " AND released_at=''", (_utcnow(), lease_id))
        conn.execute("COMMIT")
        return True
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return False


def acquire_mutation_lease(conn, job_id, holder):
    # type: (sqlite3.Connection, str, str) -> bool
    """Acquire the job's mutation lease INSIDE the caller's reservation
    transaction (admission owns the tx; this issues no COMMIT/ROLLBACK).

    Refused (False, no write) when any unexpired probe lease exists or
    when ANY other unreleased mutation lease exists — regardless of that
    job's state. In particular a lease attached to a terminal or missing
    job is NOT silently released here: that cleanup belongs exclusively
    to reconciliation with execution proof (F02), via
    tx.release_ownership(). Never time-based.
    """
    if not job_id or not _tables_present(conn):
        return False
    now = _utcnow()
    try:
        probes = conn.execute(
            "SELECT id FROM execution_leases WHERE kind='probe'"
            " AND released_at='' AND expires_at>? LIMIT 1",
            (now,)).fetchone()
        if probes is not None:
            return False
        other = conn.execute(
            "SELECT id FROM execution_leases WHERE kind='mutation'"
            " AND released_at='' AND job_id<>? LIMIT 1",
            (job_id,)).fetchone()
        if other is not None:
            return False
        conn.execute(
            "INSERT INTO execution_leases(id,kind,subject,tool_id,job_id,"
            "request_id,holder,acquired_at,expires_at,released_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("mutation-%s" % job_id, "mutation", "", "", job_id, "",
             holder or "", now, "", ""))
        return True
    except Exception:
        return False


def reclaim_expired_probes(conn):
    # type: (sqlite3.Connection) -> int
    """Safely reclaim expired probe leases (never touches mutation)."""
    if not _tables_present(conn):
        return 0
    try:
        cur = conn.execute(
            "UPDATE execution_leases SET released_at=? WHERE kind='probe'"
            " AND released_at='' AND expires_at<?",
            (_utcnow(), _utcnow()))
        conn.commit()
        return int(cur.rowcount or 0)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
