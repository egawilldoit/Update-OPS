"""Singleton dispatcher: job queue + owner probes + reconcile (R01-R05).

- Holds the singleton lock handle for the whole process life (R03).
- Canonical launch (ubuntu user manager only): systemd-run --user with the
  resolved immutable release, allow-listed env, full-UUID unit name, and a
  one-shot attempt nonce claimed atomically with unit+release+deadline.
  Launch is PROVED via the explicit unit model (units.query_unit); unknown
  never releases the reservation (R04).
- Executes typed owner probe requests (R01) with probe/mutation exclusion.
- Continuous reconcile via reconcile_core.decide + bound receipts applied
  atomically via tx (R04/R05/R08); never reruns.
- All state changes via tx.transition_tx (single explicit transactions).
- Daily retention hook (R29). Heartbeat file every loop (H-03).
- Startup validates schema only (deploy owns migration); readiness gates
  start (R31/R34).

Python 3.10 compatible. Importing this file executes nothing.
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
from typing import Any, Dict, Tuple

from .. import reconcile_core as _rc
from .. import units as _units
from ..config import settings
from ..db import connect, validate_schema
from ..jobs import (NONTERMINAL, claim_with_nonce, expire_stale_accepted)
from ..owner_probes import (CONTENDING_OPS, claim_probe, expire_probes,
                            finish_probe)
from ..schemas import utcnow_iso
from ..tx import TxError, transition_tx

LOCK_PATH = os.path.join(
    getattr(settings, "state_dir", "/var/lib/ega-update")
    or "/var/lib/ega-update", "worker.lock")
POLL_INTERVAL_S = 2
LAUNCH_PROVE_TIMEOUT_S = 10
PROBE_OP_TIMEOUT_S = 120
HEARTBEAT_FILENAME = "dispatcher.heartbeat"
RETENTION_STAMP = "retention.lastdate"
_lock_fh = None  # type: Any


def _state_dir():
    # type: () -> str
    return getattr(settings, "state_dir", "/var/lib/ega-update") \
        or "/var/lib/ega-update"


def _heartbeat_path():
    # type: () -> str
    return os.path.join(_state_dir(), HEARTBEAT_FILENAME)


def _write_dispatcher_heartbeat():
    # type: () -> None
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


def _singleton_lock():
    # type: () -> object
    """Acquire and RETURN the held handle; main() retains it for life."""
    global _lock_fh
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        fh.close()
        raise SystemExit("another dispatcher holds the worker lock")
    _lock_fh = fh
    return fh


def _owner_env_for_job(nonce):
    # type: (str) -> Tuple[Dict[str, str], Dict[str, str], str]
    """Canonical env + resolved paths + release for one launch (R02)."""
    from ..owner_env import build_job_env, resolve_release, resolved_paths
    paths = resolved_paths(settings)
    release = paths.get("release_root", "")
    bus = {}  # type: Dict[str, str]
    try:
        from ..owner_env import systemd_user_bus
        bus = systemd_user_bus() or {}
    except Exception:
        bus = {}
    return build_job_env(nonce, paths, bus), paths, release


def _canonical_cmd(job_id, nonce, unit, env, paths):
    # type: (str, str, str, Dict[str, str], Dict[str, str]) -> list
    """Single canonical launch: owner_env.build_transient_cmd is the one
    source of unit properties (this wrapper only resolves interpreter +
    working dir from the release paths)."""
    from ..owner_env import build_transient_cmd

    release = paths.get("release_root", "") or "/opt/ega-update/current"
    python = paths.get("venv_python", "") or sys.executable
    return build_transient_cmd(unit, release, env, python, job_id, nonce)


def _prove_launch(conn, job_id, unit, nonce,
                  timeout_s=LAUNCH_PROVE_TIMEOUT_S):
    # type: (...) -> str
    """Prove live/starting, or confirm absence. Returns live | starting |
    confirmed_absent | unknown. Unknown never releases the reservation;
    reconcile owns ambiguous launches (R04). There is exactly one
    implementation of this function in this module."""
    deadline = time.time() + max(1.0, float(timeout_s))
    while time.time() < deadline:
        info = _units.query_unit(unit, timeout_s=5)
        state = str(info.get("state", "unknown"))
        if state in ("live", "starting"):
            return state
        if state == "confirmed_stopped":
            break
        try:
            row = conn.execute(
                "SELECT state, dispatch_nonce, finished_at FROM jobs"
                " WHERE id=?", (job_id,)).fetchone()
        except Exception:
            row = None
        if row is not None:
            try:
                if row["state"] != "preflight" \
                        or row["dispatch_nonce"] != nonce \
                        or row["finished_at"]:
                    return "live"  # runner already ran/progressed
            except Exception:
                pass
        time.sleep(0.5)
    info = _units.query_unit(unit, timeout_s=5)
    state = str(info.get("state", "unknown"))
    if state in ("live", "starting"):
        return state
    if state == "confirmed_stopped":
        try:
            from ..reconcile_core import job_processes, unit_hex
            if job_processes(unit_hex(job_id), job_id):
                return "unknown"
        except Exception:
            return "unknown"
        return "confirmed_absent"
    return "unknown"


def dispatch_once(conn):
    # type: (sqlite3.Connection) -> str
    """Claim one accepted job and launch its user-manager runner."""
    row = conn.execute(
        "SELECT * FROM jobs WHERE state='accepted' ORDER BY created_at"
        " LIMIT 1").fetchone()
    if row is None:
        return ""
    job_id = str(row["id"])
    unit = _rc.canonical_unit(job_id)
    nonce = secrets.token_urlsafe(16)
    try:
        env, paths, release = _owner_env_for_job(nonce)
    except ValueError as exc:
        _event(conn, job_id, "blocked",
               "owner env unresolvable: %s" % str(exc)[:300])
        return ""
    # Atomic single-statement claim (deadline enforced in SQL); the
    # reservation-time claim_deadline governs, never a Python recompute.
    if not claim_with_nonce(conn, job_id, nonce, unit=unit,
                            release_path=release):
        return ""
    cmd = _canonical_cmd(job_id, nonce, unit, env, paths)
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, shell=False, check=False)
        rc = int(getattr(proc, "returncode", 1) or 0)
    except Exception as exc:
        _event(conn, job_id, "launch_spawn_failed", str(exc)[:300])
        return job_id  # reconcile owns the ambiguous launch (R04)
    if rc != 0:
        _event(conn, job_id, "launch_spawn_rc",
               "systemd-run --user exit=%d" % rc)
        return job_id  # ambiguous: never assume non-launch (R04)
    proved = _prove_launch(conn, job_id, unit, nonce)
    if proved in ("live", "starting"):
        return job_id
    # confirmed_absent/unknown: reconcile resolves (never assume here).
    _event(conn, job_id, "launch_unproved", proved)
    return job_id


def _event(conn, job_id, event_type, detail):
    # type: (sqlite3.Connection, str, str, str) -> None
    # F11: central safe event persistence — sanitized, fixed marker on
    # sanitizer failure, never raw data. Transport failure is best
    # effort here (diagnostics); state transitions use tx.* which fail
    # loudly instead.
    try:
        from ..events import record_event
        record_event(conn, job_id, event_type, detail)
    except Exception:
        pass


# -- owner probe execution (R01) -------------------------------------------

def _sanitize_payload(payload):
    # type: (Dict[str, Any]) -> Dict[str, Any]
    # N12: sanitizer failure raises; run_probe_queue converts it to an
    # error result (never persists raw probe output).
    from ..config import load_secret_values
    from ..sanitize import sanitize_json
    return sanitize_json(payload, load_secret_values(settings))


def _execute_probe_op(tool_id, op, request_id=""):
    # type: (str, str, str) -> Tuple[str, Dict[str, Any]]
    """Run one probe op supervised (N10, F03): a worker process inside
    an owned scope with a monotonic deadline AND the canonical contract
    env — the exact environment execution phases receive. A hanging
    read-only probe can never wedge the dispatcher loop.
    Returns (status, payload)."""
    from .phase_run import run_supervised_phase
    from ..owner_env import build_owner_contract, contract_env

    try:
        log_dir = getattr(settings, "log_dir",
                          "/var/lib/ega-update/logs") \
            or "/var/lib/ega-update/logs"
    except Exception:
        log_dir = "/var/lib/ega-update/logs"
    try:
        env = contract_env(build_owner_contract(settings))
    except Exception as exc:
        return "error", {"reason": "owner contract unbuildable: %s" % exc}
    try:
        ok, data, error, timed_out = run_supervised_phase(
            tool_id, request_id or "probe", "probe", {"op": op},
            PROBE_OP_TIMEOUT_S, settings, log_dir,
            lambda _s, _l: None, op=op, env=env)
    except Exception as exc:
        return "error", {"reason": "supervision failed: %s" % exc}
    if timed_out:
        return "error", {"reason": "probe deadline exceeded"}
    if not ok:
        return "error", {"reason": (error or "probe failed")[:300]}
    if not isinstance(data, dict):
        return "error", {"reason": "probe result malformed"}
    return "ok", data


def run_probe_queue(conn):
    # type: (sqlite3.Connection) -> int
    """Execute claimed probe requests with mutation exclusion (R01).

    Contending ops (activity/plan/verify/refresh) are deferred while any
    nonterminal job exists. Returns the number processed.
    """
    from ..jobs import active_job

    done = 0
    for _ in range(4):  # bounded work per loop pass
        try:
            req = claim_probe(conn, "dispatcher-%d" % os.getpid())
        except Exception:
            break
        if req is None:
            break
        try:
            req_id = str(req["id"])
            tool_id = str(req["tool_id"] or "")
            op = str(req["op"] or "")
        except Exception:
            continue
        defer = False
        if op in CONTENDING_OPS:
            try:
                defer = active_job(conn) is not None
            except Exception:
                defer = True
        if defer:
            try:
                finish_probe(conn, req_id, "deferred", {})
            except Exception:
                pass
            done += 1
            continue
        # N08: durable probe lease around the installation touch. The
        # atomic acquire fails when a mutation lease is held, closing
        # the check-then-act race between this probe and reservation.
        lease_id = None
        if op in CONTENDING_OPS:
            try:
                from ..leases import acquire_probe_lease, release_lease
                lease_id = acquire_probe_lease(
                    conn, tool_id, "dispatcher-%d" % os.getpid())
            except Exception:
                lease_id = None
            if not lease_id:
                try:
                    finish_probe(conn, req_id, "deferred", {})
                except Exception:
                    pass
                done += 1
                continue
        try:
            status, payload = _execute_probe_op(tool_id, op, req_id)
        except Exception as exc:
            status, payload = "error", {"reason": str(exc)[:300]}
        finally:
            if lease_id:
                try:
                    from ..leases import release_lease as _release
                    _release(conn, lease_id)
                except Exception:
                    pass
        try:
            clean = _sanitize_payload(payload)
        except Exception:
            # N12: payload that cannot be sanitized is never persisted
            # raw; the probe reports an evidence failure instead.
            try:
                finish_probe(conn, req_id, "error",
                             {"reason": "evidence_sanitization_failed"})
            except Exception:
                pass
            done += 1
            continue
        try:
            finish_probe(conn, req_id, status, clean)
        except Exception:
            pass
        done += 1
    try:
        expire_probes(conn)
    except Exception:
        pass
    return done


# -- reconcile (R04/R05/R08) ------------------------------------------------

def _load_receipt_bound(job_id):
    # type: (str) -> Tuple[bool, Dict[str, Any], str]
    """Load + validate receipt; binding checked by callers via tx apply."""
    try:
        from ..receipts import load_receipt_file
    except Exception:
        return False, {}, "receipt module unavailable"
    return load_receipt_file(_receipt_path(job_id))


def _reconcile_row(conn, row):
    # type: (sqlite3.Connection, object) -> str
    """One row via reconcile_core.decide + atomic tx outcomes."""
    from ..receipts import (apply_receipt, check_binding, shows_mutation,
                            validate_receipt)

    try:
        job = dict(row)
        job_id = str(job.get("id", ""))
        state = str(job.get("state", ""))
    except Exception:
        return "skipped"
    unit = str(job.get("canonical_unit", "") or job.get("runner_unit", "")
               or "")
    if not unit:
        unit = _rc.canonical_unit(job_id)
    info = _units.query_unit(unit, timeout_s=5)
    try:
        procs = _rc.job_processes(_rc.unit_hex(job_id), job_id)
    except Exception:
        procs = []
    receipt_valid = False
    receipt_data = {}  # type: Dict[str, Any]
    ok, data, _reason = _load_receipt_bound(job_id)
    if ok and isinstance(data, dict):
        try:
            plan = conn.execute("SELECT * FROM plans WHERE id=?",
                                (job.get("plan_id", ""),)).fetchone()
            plan_row = dict(plan) if plan is not None else None
        except Exception:
            plan_row = None
        bound, _why = check_binding(data, job, plan_row, job_id)
        if bound:
            receipt_valid = True
            receipt_data = data
    receipt_view = dict(receipt_data) if receipt_valid else None
    if receipt_view is not None:
        receipt_view["_valid"] = True
    action, detail = _rc.decide(job, info, receipt_view, procs)
    if action in ("live", "starting", "stopping"):
        try:
            conn.execute("UPDATE jobs SET heartbeat=? WHERE id=?",
                         (utcnow_iso(), job_id))
            conn.commit()
        except Exception:
            pass
        return "live"
    if action == "keep-unknown":
        _event(conn, job_id, "reconcile_unknown", detail[:500])
        return "unknown-held"
    if action == "apply-receipt":
        try:
            applied = apply_receipt(conn, receipt_data, job_id)
        except ValueError as exc:
            _event(conn, job_id, "reconcile_receipt_rejected",
                   str(exc)[:500])
            return "receipt-rejected"
        except Exception as exc:
            _event(conn, job_id, "reconcile_receipt_error",
                   str(exc)[:300])
            return "receipt-error"
        try:
            needs_recovery = _rc.recovery_for(
                state, shows_mutation(receipt_data),
                bool(job.get("unresolved", 0)))
            if str(receipt_data.get(
                    "recovery_disposition", "")) == "required":
                needs_recovery = True
            if needs_recovery and applied in (
                    "failed", "interrupted", "health_failed", "succeeded"):
                conn.execute(
                    "UPDATE jobs SET recovery_required=1 WHERE id=?",
                    (job_id,))
                conn.execute(
                    "INSERT INTO events(job_id,created_at,event_type,"
                    "detail) VALUES(?,?,?,?)",
                    (job_id, utcnow_iso(), "recovery_required",
                     "reconciled %s with mutation evidence" % applied))
                conn.commit()
        except Exception:
            pass
        # F02: ownership release ONLY after the proof above (decide()
        # returned apply-receipt solely for confirmed-stopped units with
        # no execution-marked processes). Terminal state alone never
        # releases; a failed release stays loud and is retried.
        try:
            from ..tx import release_ownership
            release_ownership(
                conn, job_id, expect_states=[applied],
                event="ownership_released",
                event_detail="unit confirmed stopped; receipt %s; "
                             "no updater processes" % applied)
        except Exception as exc:
            _event(conn, job_id, "ownership_release_failed",
                   str(exc)[:300])
            return "ownership-release-failed"
        return "applied"
    # mark-interrupted: atomic terminalization, never rerun. Ownership is
    # released only afterwards, and only with quiescence proof (F02).
    needs_recovery = _rc.recovery_for(
        state, shows_mutation(receipt_data) if receipt_valid else False,
        bool(job.get("unresolved", 0)))
    if state in ("succeeded", "blocked", "failed", "health_failed",
                 "interrupted"):
        # Already terminal: no state change, prove-and-release only.
        try:
            from ..tx import release_ownership
            release_ownership(
                conn, job_id, expect_states=[state],
                event="ownership_released",
                event_detail="unit confirmed stopped; no updater "
                             "processes")
        except Exception as exc:
            _event(conn, job_id, "ownership_release_failed",
                   str(exc)[:300])
            return "ownership-release-failed"
        return "ownership-released"
    try:
        transition_tx(
            conn, job_id, "interrupted", step="interrupted",
            expect_states=list(NONTERMINAL),
            update={"error_code": "interrupted",
                    "error_detail": "dispatcher reconcile: %s" % detail[:400],
                    "recovery_required": 1 if needs_recovery else 0,
                    "unresolved": 0},
            event="interrupted", event_detail=detail[:500])
    except TxError as exc:
        _event(conn, job_id, "reconcile_guard", str(exc)[:300])
        return "skipped"
    try:
        from ..tx import release_ownership
        release_ownership(
            conn, job_id, expect_states=["interrupted"],
            event="ownership_released",
            event_detail="unit confirmed stopped; no updater processes")
    except Exception as exc:
        _event(conn, job_id, "ownership_release_failed",
               str(exc)[:300])
        return "ownership-release-failed"
    return "interrupted-recovery" if needs_recovery else "interrupted"


def _reconcile_candidates(conn):
    # type: (sqlite3.Connection) -> list
    """Rows needing reconcile: nonterminals, unresolved markers, and
    terminal jobs whose mutation lease is still held (F02: otherwise a
    terminal+held-lease row would never be revisited and its lease never
    released)."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT jobs.* FROM jobs LEFT JOIN execution_leases"
            " ON execution_leases.kind='mutation'"
            " AND execution_leases.job_id=jobs.id"
            " AND execution_leases.released_at=''"
            " WHERE jobs.state IN (?,?,?,?,?) OR jobs.unresolved=1"
            " OR execution_leases.id IS NOT NULL").fetchall()
        return list(rows)
    except Exception:
        pass
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE state IN (?,?,?,?,?)"
            " OR unresolved=1").fetchall()
        return list(rows)
    except Exception:
        pass
    # Pre-migration schema without unresolved: nonterminals only.
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE state IN (?,?,?,?,?)",
            NONTERMINAL).fetchall()
        return list(rows)
    except Exception:
        return []


def reconcile_claimed_jobs(conn):
    # type: (sqlite3.Connection) -> int
    """Continuous reconcile for nonterminal claimed rows + terminal rows
    with possibly unresolved runners (R04: reconcile unresolved terminals
    too) + terminal rows with held mutation leases (F02). Unknown unit
    state always holds the reservation."""
    rows = _reconcile_candidates(conn)
    acted = 0
    for row in rows:
        try:
            nonce = row["dispatch_nonce"]
        except Exception:
            nonce = ""
        try:
            state = row["state"]
        except Exception:
            continue
        if state in NONTERMINAL and not nonce:
            continue  # accepted, never claimed: expiry owns it
        try:
            outcome = _reconcile_row(conn, row)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            continue
        if outcome not in ("live", "skipped", "unknown-held"):
            acted += 1
    return acted


def reconcile_boot(conn):
    # type: (sqlite3.Connection) -> None
    """On start: same per-row reconcile. Never auto-resumes work."""
    for row in _reconcile_candidates(conn):
        try:
            _reconcile_row(conn, row)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            continue


def _maybe_retain(conn):
    # type: (sqlite3.Connection) -> None
    """Daily retention hook (R29). Best effort; never breaks the loop."""
    stamp_path = os.path.join(_state_dir(), RETENTION_STAMP)
    try:
        import datetime
        today = datetime.date.today().isoformat()
    except Exception:
        return
    try:
        with open(stamp_path, "r", encoding="utf-8") as fh:
            if fh.read().strip() == today:
                return
    except OSError:
        pass
    except Exception:
        return
    try:
        from ..retention import run_retention
        run_retention(conn, settings)
    except Exception:
        return
    try:
        with open(stamp_path, "w", encoding="utf-8") as fh:
            fh.write(today)
    except OSError:
        pass


def main():
    # type: () -> None
    global _lock_fh
    _lock_fh = _singleton_lock()  # held for the whole process life (R03)
    # N09: canonical user-bus environment for --user calls (systemd-run,
    # systemctl --user) and owner probes. Linger starts the manager; these
    # variables connect to it without any interactive login. Dispatcher
    # probes and runner phases therefore share one bus contract.
    try:
        from ..owner_env import systemd_user_bus
        for _k, _v in (systemd_user_bus() or {}).items():
            try:
                if _v and not os.environ.get(_k):
                    os.environ[_k] = str(_v)
            except Exception:
                continue
    except Exception:
        pass
    try:
        from ..readiness import ReadinessError, validate_startup
        validate_startup("worker", settings)
    except Exception as exc:
        raise SystemExit("worker readiness failed: %s" % exc)
    conn = connect(settings.db_path)
    try:
        validate_schema(conn)
    except Exception as exc:
        raise SystemExit("schema validation failed: %s" % exc)
    reconcile_boot(conn)
    while True:
        try:
            expire_stale_accepted(conn)
            run_probe_queue(conn)
            try:
                from ..leases import reclaim_expired_probes
                reclaim_expired_probes(conn)
            except Exception:
                pass
            dispatch_once(conn)
            reconcile_claimed_jobs(conn)
            _maybe_retain(conn)
            _write_dispatcher_heartbeat()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
