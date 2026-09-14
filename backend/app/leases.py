"""Durable execution leases: probe/mutation exclusion (N08, F02, D5).

One transactional admission/exclusion mechanism replacing the
check-then-act race between owner probes and update reservation:

- probe lease: contending owner probes hold one while touching the
  installation. Acquired only when no mutation lease is held (and no
  conflicting maintenance for contending ops). The lease is BOUND to
  the probe execution identity: request_id (probe request UUID) and
  subject (canonical transient probe unit derived via
  owner_env.transient_probe_name). D5: the TTL (expires_at) is an
  advisory horizon/heartbeat, NEVER a release trigger. An unreleased
  probe lease blocks mutation admission regardless of expiry; it may
  be released ONLY by reconciliation with positive stop proof from the
  explicit unit model (units.CONFIRMED_STOPPED). Live/starting/
  stopping/unknown holds the exclusion (recovery-required equivalent).
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

# Derived probe-lease reconciliation states (D5). No schema column is
# required: released_at plus expires_at fully determine the state.
PROBE_STATE_ACTIVE = "active"
PROBE_STATE_RECONCILE_REQUIRED = "reconcile-required"
PROBE_STATE_RELEASED = "released"

_CONFIRMED_STOPPED = "confirmed_stopped"


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


def _probe_unit_for(request_id):
    # type: (str) -> str
    """Canonical transient probe unit for a request id, or "" when the
    request id cannot bind one (callers then treat stop as unprovable)."""
    try:
        if not request_id:
            return ""
        from .owner_env import transient_probe_name
        return transient_probe_name(request_id)
    except Exception:
        return ""


def _lease_probe_unit(lease):
    # type: (Dict[str, Any]) -> str
    """Bound probe unit for a lease: explicit subject first, then the
    canonical derivation from request_id (legacy rows with empty subject)."""
    try:
        unit = str((lease or {}).get("subject") or "")
    except Exception:
        unit = ""
    if unit:
        return unit
    try:
        request_id = str((lease or {}).get("request_id") or "")
    except Exception:
        request_id = ""
    return _probe_unit_for(request_id)


def _unit_query(units_mod=None):
    # type: (object) -> object
    """query_unit callable for reconciliation, or None (never infer)."""
    if units_mod is None:
        try:
            from . import units as _units_mod
            units_mod = _units_mod
        except Exception:
            return None
    query = getattr(units_mod, "query_unit", None)
    return query if callable(query) else None


def _unreleased(conn, kind):
    # type: (sqlite3.Connection, str) -> list
    """All unreleased leases of a kind (probe expiry is advisory)."""
    try:
        rows = conn.execute(
            "SELECT * FROM execution_leases WHERE kind=?"
            " AND released_at=''", (kind,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def active_probe_leases(conn):
    # type: (sqlite3.Connection) -> List[Dict[str, Any]]
    """Every unreleased probe lease (mutation must respect ALL of them).

    D5: expiry is NOT proof the probe process stopped, so an expired
    probe lease keeps blocking mutation admission until reconciliation
    positively proves the bound probe unit stopped.
    """
    return _unreleased(conn, "probe")


def active_mutation_lease(conn):
    # type: (sqlite3.Connection) -> Optional[Dict[str, Any]]
    """The held mutation lease, if any (never time-expires)."""
    rows = _unreleased(conn, "mutation")
    return rows[0] if rows else None


def probe_lease_state(lease, now=None):
    # type: (Dict[str, Any], str) -> str
    """Derived reconciliation state for one probe lease row (D5).

    active:             unreleased, TTL not yet passed (owner responsible).
    reconcile-required: unreleased, TTL passed/absent — the exclusion is
                        held and blocking until reconciliation proves the
                        probe unit stopped (recovery-required equivalent).
    released:           released_at set, only ever after positive proof.
    """
    try:
        released = str((lease or {}).get("released_at") or "")
    except Exception:
        released = ""
    if released:
        return PROBE_STATE_RELEASED
    try:
        expires = str((lease or {}).get("expires_at") or "")
    except Exception:
        expires = ""
    try:
        current = str(now or _utcnow())
    except Exception:
        current = ""
    if expires and current and expires > current:
        return PROBE_STATE_ACTIVE
    return PROBE_STATE_RECONCILE_REQUIRED


def acquire_probe_lease(conn, tool_id, holder, ttl_s=PROBE_LEASE_TTL_S,
                        request_id=""):
    # type: (sqlite3.Connection, str, str, float, str) -> Optional[str]
    """Atomically acquire a bounded probe lease, or None.

    Refused when a mutation lease is held (probes must not race
    mutation). Maintenance drain does NOT block read probes. Caller
    must already hold NO transaction; this manages its own short one.

    D5 binding: request_id is the probe request UUID and subject holds
    the canonical transient probe unit
    (owner_env.transient_probe_name(request_id)) so reconciliation can
    query the exact execution the lease protects. The TTL written to
    expires_at is an advisory horizon only; it never releases the lease.
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
    unit = _probe_unit_for(request_id)
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
            (lease_id, "probe", unit, tool_id or "", "",
             str(request_id or ""), holder or "", now, expires, ""))
        conn.execute("COMMIT")
        return lease_id
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return None


def release_lease(conn, lease_id, fail_on_storage_error=False):
    # type: (sqlite3.Connection, str, bool) -> bool
    """Release a lease idempotently. Returns True when held-then-released
    or already released; False on storage failure.

    fail_on_storage_error=True re-raises the storage error instead of
    returning False. Reconciliation uses it (W8) to distinguish transient
    SQLite contention (boot retries) from a legitimate safe hold; a
    release failure must never be silently mistaken for "already held by
    a live unit".
    """
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
        if fail_on_storage_error:
            raise
        return False


def acquire_mutation_lease(conn, job_id, holder):
    # type: (sqlite3.Connection, str, str) -> bool
    """Acquire the job's mutation lease INSIDE the caller's reservation
    transaction (admission owns the tx; this issues no COMMIT/ROLLBACK).

    Refused (False, no write) when ANY unreleased probe lease exists
    (D5: expiry is not proof of stop, so expired-but-unresolved probe
    leases block exactly like live ones) or when ANY other unreleased
    mutation lease exists — regardless of that job's state. In
    particular a lease attached to a terminal or missing job is NOT
    silently released here: that cleanup belongs exclusively to
    reconciliation with execution proof (F02), via
    tx.release_ownership(). Never time-based.
    """
    if not job_id or not _tables_present(conn):
        return False
    now = _utcnow()
    try:
        probes = conn.execute(
            "SELECT id FROM execution_leases WHERE kind='probe'"
            " AND released_at='' LIMIT 1").fetchone()
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


def reconcile_probe_leases(conn, units_mod=None, include_unexpired=False):
    # type: (sqlite3.Connection, object, bool) -> Dict[str, Any]
    """Reconcile unreleased probe leases against execution proof (D5).

    A probe lease may be released ONLY when its bound canonical probe
    unit is positively CONFIRMED_STOPPED by the explicit unit model.
    TTL expiry alone NEVER releases; live/starting/stopping/unknown (or
    an unqueryable/unbound unit) keeps the lease held and blocking
    mutation admission — the recovery-required equivalent.

    include_unexpired=False (loop path) reconciles only leases whose
    advisory TTL has passed. include_unexpired=True (boot path) also
    reconciles younger leases: a dispatcher restart does not stop the
    transient probe service under the user manager, so each unit is
    queried for positive proof rather than assumed gone.

    units_mod defaults to backend.app.units (lazy import); tests inject
    a fake exposing query_unit — real systemd is never required.

    W8: a release that cannot commit (e.g. transient SQLITE_BUSY) raises
    the storage error instead of being reported as "held": the caller
    (boot reconciliation) retries with a bounded backoff or fails
    startup. A lease is still NEVER released on failure.

    Returns {"checked", "released", "held", "unbound", "evidence"}.
    """
    report = {"checked": 0, "released": 0, "held": 0, "unbound": 0,
              "evidence": []}  # type: Dict[str, Any]
    if not _tables_present(conn):
        return report
    now = _utcnow()
    try:
        rows = conn.execute(
            "SELECT * FROM execution_leases WHERE kind='probe'"
            " AND released_at=''").fetchall()
    except Exception:
        return report
    query = _unit_query(units_mod)
    stopped = str(getattr(units_mod, "CONFIRMED_STOPPED",
                          _CONFIRMED_STOPPED) or _CONFIRMED_STOPPED)
    for row in rows:
        try:
            lease = dict(row)
        except Exception:
            continue
        lease_state = probe_lease_state(lease, now)
        if lease_state == PROBE_STATE_ACTIVE and not include_unexpired:
            continue
        report["checked"] += 1
        unit = _lease_probe_unit(lease)
        entry = {"lease": str(lease.get("id") or ""), "unit": unit,
                 "state": "unbound", "lease_state": lease_state,
                 "action": "held"}  # type: Dict[str, Any]
        if not unit:
            # No execution identity to prove stop for: hold (fail closed).
            report["unbound"] += 1
            report["held"] += 1
            report["evidence"].append(entry)
            continue
        unit_state = "unknown"
        if query is not None:
            try:
                info = query(unit, timeout_s=5)
                unit_state = str((info or {}).get("state", "unknown"))
            except Exception:
                unit_state = "unknown"
        entry["state"] = unit_state
        if unit_state == stopped and release_lease(
                conn, str(lease.get("id") or ""),
                fail_on_storage_error=True):
            report["released"] += 1
            entry["action"] = "released"
        else:
            report["held"] += 1
        report["evidence"].append(entry)
    return report


def reconcile_expired_probes(conn, units_mod=None):
    # type: (sqlite3.Connection, object) -> int
    """Loop-path reconciliation: returns reported releases (never time)."""
    return int(reconcile_probe_leases(
        conn, units_mod=units_mod, include_unexpired=False).get(
            "released", 0))


def reclaim_expired_probes(conn, units_mod=None):
    # type: (sqlite3.Connection, object) -> int
    """Compatibility name for reconcile_expired_probes (D5).

    Semantics are now proof-based: expired probe leases are NEVER
    released because time passed; only CONFIRMED_STOPPED proof releases.
    """
    return reconcile_expired_probes(conn, units_mod=units_mod)
