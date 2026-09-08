"""Machine-readable deployment quiescence (N01). Python 3.10 compatible.

One canonical schema consumed by `cli status`, install.sh, upgrade.sh,
and tests — never reimplemented in shell:

  schema_version, action, state, detail{worker_alive, active_job,
  unresolved_jobs, recovery_jobs, unresolved_units, live_units,
  delegated_operations, held_leases, drain, quiescent, reasons}, exit mapping.

Quiescence requires ALL of: worker heartbeat fresh; no active/nonterminal
job; no unresolved or recovery-required job; every expected runner unit
confirmed stopped (live/starting/stopping/UNKNOWN all block); no stray
job unit in a non-stopped state; no delegated service operation active
or unprovable; admission drain present. Unknown systemd/process state
means quiescent=false. Quiescence is never inferred from DB rows alone.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List

QUIESCENCE_SCHEMA_VERSION = 1

NONTERMINAL = ("accepted", "preflight", "backup", "updating", "verifying")


def _utcnow():
    # type: () -> str
    try:
        from .schemas import utcnow_iso
        return utcnow_iso()
    except Exception:
        import datetime
        return datetime.datetime.now(
            datetime.timezone.utc).isoformat()


def _drain_present(state_dir):
    # type: (str) -> bool
    try:
        return os.path.exists(os.path.join(str(state_dir or ""), "drain"))
    except Exception:
        return False


def _worker_alive(state_dir):
    # type: (str) -> bool
    try:
        from .jobs import read_dispatcher_heartbeat
        return bool(read_dispatcher_heartbeat(state_dir, max_age_s=20))
    except Exception:
        return False


def _job_rows(conn):
    # type: (sqlite3.Connection) -> list
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM jobs").fetchall()]
    except Exception:
        return []


def _expected_units(jobs):
    # type: (list) -> List[str]
    units = []
    for job in jobs:
        try:
            state = str(job.get("state", ""))
            if state not in NONTERMINAL and \
                    not int(job.get("unresolved", 0) or 0) and \
                    not int(job.get("recovery_required", 0) or 0):
                continue
            unit = str(job.get("canonical_unit", "")
                       or job.get("runner_unit", "") or "")
            if unit and unit not in units:
                units.append(unit)
        except Exception:
            continue
    return units


def _held_lease_units(conn):
    # type: (object) -> List[str]
    """Canonical units of jobs with unreleased mutation leases (F02).

    A terminal job whose lease was never disposed (missed reconciler
    pass) must still have its unit proven stopped before deploy
    proceeds. Never raises; absence of the leases table yields [].
    """
    try:
        from .reconcile_core import canonical_unit
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT job_id FROM execution_leases WHERE kind='mutation'"
            " AND released_at=''").fetchall()
    except Exception:
        return []
    units = []
    for row in rows:
        try:
            jid = str(row["job_id"] or "")
        except Exception:
            continue
        if not jid:
            continue
        try:
            unit = canonical_unit(jid)
        except Exception:
            continue
        if unit and unit not in units:
            units.append(unit)
    return units


def _service_states(services):
    # type: (list) -> List[Dict[str, str]]
    """Bounded user-then-system state read per delegated service."""
    out = []  # type: List[Dict[str, str]]
    try:
        from . import units as _units
    except Exception:
        return [{"service": str(s), "state": "unknown",
                 "detail": "unit model unavailable"} for s in services]
    for service in services[:20]:
        name = str(service or "")
        if not name:
            continue
        info = None
        bus = ""
        try:
            info = _units.query_unit(name, timeout_s=5)
            bus = "user"
            if str(info.get("state", "")) == "unknown" and \
                    "identity mismatch" not in str(
                        info.get("detail", "")):
                # Unknown on the user bus may mean system scope: try it
                # before concluding (either bus proving live blocks).
                alt = _units.query_unit_system(name, timeout_s=5)
                if str(alt.get("state", "")) != "unknown":
                    info, bus = alt, "system"
        except Exception:
            info = {"state": "unknown", "detail": "query crashed"}
        out.append({"service": name, "bus": bus,
                    "state": str((info or {}).get("state", "unknown")),
                    "detail": str((info or {}).get("detail", ""))[:200]})
    return out


def assess_quiescence(conn, settings):
    # type: (sqlite3.Connection, object) -> Dict[str, Any]
    """Prove deployment quiescence. Never raises; unknown => not quiescent."""
    reasons = []  # type: List[str]
    try:
        state_dir = str(getattr(settings, "state_dir", "")
                        or "/var/lib/ega-update")
    except Exception:
        state_dir = "/var/lib/ega-update"
    worker_alive = _worker_alive(state_dir)
    if not worker_alive:
        reasons.append("worker heartbeat stale/missing")
    jobs = _job_rows(conn)
    active = [j for j in jobs
              if str(j.get("state", "")) in NONTERMINAL]
    active_job = str(active[0].get("id", "")) if active else ""
    if active_job:
        reasons.append("active job %s (%s)" % (
            active_job[:8],
            str(active[0].get("state", ""))))
    unresolved = [str(j.get("id", "")) for j in jobs
                  if int(j.get("unresolved", 0) or 0)]
    if unresolved:
        reasons.append("unresolved jobs: %s" % ",".join(
            j[:8] for j in unresolved[:5]))
    recovery = [str(j.get("id", "")) for j in jobs
                if int(j.get("recovery_required", 0) or 0)]
    if recovery:
        reasons.append("recovery-required jobs: %s" % ",".join(
            j[:8] for j in recovery[:5]))
    # Expected runner units must be CONFIRMED stopped; live, starting,
    # stopping, and unknown all block (R04/N07 semantics).
    live_units = []  # type: List[Dict[str, str]]
    try:
        from . import units as _units
        units_ok = True
    except Exception:
        units_ok = False
        reasons.append("unit model unavailable")
    if units_ok:
        check_units = _expected_units(jobs)
        for held in _held_lease_units(conn):
            if held not in check_units:
                check_units.append(held)
        for unit in check_units:
            try:
                info = _units.query_unit(unit, timeout_s=5)
                state = str(info.get("state", "unknown"))
            except Exception:
                state = "unknown"
                info = {}
            if state != "confirmed_stopped":
                live_units.append({
                    "unit": unit, "state": state,
                    "detail": str((info or {}).get("detail", ""))[:200]})
        # Stray job units (no DB row) in non-stopped states also block.
        try:
            stray, list_err = _units.list_job_units()
        except Exception as exc:
            stray, list_err = [], "list-units crashed: %s" % exc
        if list_err:
            reasons.append("unit enumeration unproven: %s" % list_err[:200])
            live_units.append({"unit": "<enumeration>",
                               "state": "unknown", "detail": list_err[:200]})
        else:
            known = set(_expected_units(jobs))
            for entry in stray:
                try:
                    name = str(entry.get("unit", ""))
                    combo = "%s/%s" % (entry.get("active_state", ""),
                                       entry.get("sub_state", ""))
                except Exception:
                    continue
                if name in known:
                    continue
                if entry.get("active_state") not in ("inactive",) or \
                        entry.get("sub_state") not in ("dead",):
                    live_units.append({
                        "unit": name, "state": "stray-%s" % combo,
                        "detail": "no DB job references this unit"})
    if live_units:
        reasons.append("runner units not quiescent: %s" % ",".join(
            str(u.get("unit", "?"))[-12:] for u in live_units[:5]))
    # Delegated service operations for jobs under scrutiny.
    delegated = []  # type: List[Dict[str, str]]
    for job in jobs:
        try:
            state = str(job.get("state", ""))
            if state not in NONTERMINAL and \
                    not int(job.get("unresolved", 0) or 0):
                continue
            import json as _json
            plan = conn.execute("SELECT services FROM plans WHERE id=?",
                                (job.get("plan_id", ""),)).fetchone()
            services = []
            if plan is not None:
                try:
                    services = list(_json.loads(plan["services"] or "[]"))
                except Exception:
                    services = []
            for entry in _service_states(services):
                entry["job"] = str(job.get("id", ""))[:8]
                delegated.append(entry)
        except Exception:
            continue
    for entry in delegated:
        if entry.get("state") not in ("confirmed_stopped",):
            reasons.append("delegated operation %s (%s) is %s" % (
                entry.get("service", "?"), entry.get("job", "?"),
                entry.get("state", "unknown")))
            break
    drain = _drain_present(state_dir)
    if not drain:
        reasons.append("admission drain absent")
    quiescent = (worker_alive and not active_job and not unresolved
                 and not recovery and not live_units and not any(
                     e.get("state") not in ("confirmed_stopped",)
                     for e in delegated) and drain)
    try:
        active_services = [e for e in delegated
                           if e.get("state") != "confirmed_stopped"]
    except Exception:
        active_services = list(delegated)
    return {
        "worker_alive": bool(worker_alive),
        "active_job": active_job,
        "unresolved_jobs": unresolved,
        "recovery_jobs": recovery,
        "unresolved_units": [str(u.get("unit", "")) for u in live_units],
        "live_units": live_units,
        "delegated_operations": active_services,
        "held_leases": _held_lease_job_ids(conn),
        "drain": bool(drain),
        "quiescent": bool(quiescent),
        "reasons": reasons,
    }


def _held_lease_job_ids(conn):
    # type: (object) -> List[str]
    try:
        rows = conn.execute(
            "SELECT job_id FROM execution_leases WHERE kind='mutation'"
            " AND released_at=''").fetchall()
        return [str(r["job_id"] or "") for r in rows
                if str(r["job_id"] or "")]
    except Exception:
        return []
