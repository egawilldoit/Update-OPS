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
    release = paths.get("release_root", "") or "/opt/ega-update/current"
    python = paths.get("venv_python", "") or sys.executable
    cmd = [
        "systemd-run", "--user", "--collect",
        "--unit=%s" % unit,
        "--working-directory=%s" % release,
        "--property=KillMode=control-group",
        "--property=Restart=no",
    ]
    for key in ("EGA_CONFIG_FILE", "EGA_ATTEMPT_NONCE", "EGA_RELEASE_ROOT",
                "PATH", "HOME", "USER", "LOGNAME", "XDG_RUNTIME_DIR",
                "DBUS_SESSION_BUS_ADDRESS"):
        value = env.get(key, "")
        if value:
            cmd.append("--setenv=%s=%s" % (key, value))
    cmd += [python, "-m", "backend.app.worker.runner", job_id, nonce]
    return cmd


def _prove_launch(conn, job_id, unit, nonce,
                  timeout_s=LAUNCH_PROVE_TIMEOUT_S):
    # type: (...) -> str
    """Prove the runner unit live/starting, or confirm it never started.

    Returns live | starting | confirmed_absent | unknown. Unknown (bus
    failure etc.) NEVER releases the reservation; reconcile owns it (R04).
    """
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
        # No receipt can exist yet (just spawned) and no procs expected;
        # verify absence of execution-marked processes before concluding.
        try:
            from ..reconcile_core import job_processes, unit_hex
            leftovers = job_processes(unit_hex(job_id), job_id)
        except Exception:
            return "unknown"
        if leftovers:
            return "unknown"
        return "confirmed_absent"
    return "unknown"


def _prove_launch(conn, job_id, unit, nonce,
                  timeout_s=LAUNCH_PROVE_TIMEOUT_S):
    # type: (...) -> str
    """Prove live/starting, or confirm absence. Returns live | starting |
    confirmed_absent | unknown. Unknown never releases the reservation;
    reconcile owns ambiguous launches (R04)."""
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
    try:
        window = float(getattr(settings, "worker_claim_s", 10) or 10)
    except (TypeError, ValueError):
        window = 10.0
    if not claim_with_nonce(conn, job_id, nonce, unit=unit,
                            release_path=release,
                            claim_deadline_s=window):
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
    try:
        from ..sanitize import sanitize_text
        detail = sanitize_text(detail, ())
    except Exception:
        pass
    try:
        conn.execute(
            "INSERT INTO events(job_id,created_at,event_type,detail)"
            " VALUES(?,?,?,?)",
            (job_id, utcnow_iso(), event_type[:100], detail[:1000]))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


# -- owner probe execution (R01) -------------------------------------------

def _sanitize_payload(payload):
    # type: (Dict[str, Any]) -> Dict[str, Any]
    try:
        from ..config import load_secret_values
        from ..sanitize import sanitize_json
        return sanitize_json(payload, load_secret_values(settings))
    except Exception:
        return payload


def _execute_probe_op(tool_id, op):
    # type: (str, str) -> Tuple[str, Dict[str, Any]]
    """Run one probe op as the owner. Returns (status, payload)."""
    try:
        from ..adapters import registry as _registry
    except Exception as exc:
        return "error", {"reason": "adapter registry: %s" % exc}
    try:
        adapter = _registry.get_adapter(tool_id)
    except KeyError:
        return "error", {"reason": "unknown tool"}
    except Exception as exc:
        return "error", {"reason": "adapter unavailable: %s" % exc}
    if not getattr(adapter, "enabled", True):
        return "error", {"reason": "adapter disabled"}
    try:
        if op == "inspect":
            return "ok", _dump(adapter.inspect())
        if op == "discover":
            return "ok", _dump(adapter.discover())
        if op == "activity":
            return "ok", _dump(adapter.activity())
        if op == "plan":
            planned = adapter.plan()
            try:
                activity = adapter.activity()
            except Exception:
                activity = None
            try:
                inspection = adapter.inspect()
            except Exception:
                inspection = None
            return "ok", {
                "planned": _dump(planned),
                "activity": _dump(activity) if activity else {
                    "state": "unknown",
                    "evidence": "activity probe failed",
                    "checked_at": utcnow_iso()},
                "inspection": _dump(inspection) if inspection else {},
            }
        if op == "verify":
            return "ok", _dump(adapter.verify())
        if op == "refresh":
            try:
                inspection = adapter.inspect()
            except Exception as exc:
                return "error", {
                    "reason": "inspect failed: %s" % str(exc)[:300]}
            payload = {"inspection": _dump(inspection)}  # type: Dict[str, Any]
            try:
                payload["discovery"] = _dump(adapter.discover())
            except Exception as exc:
                payload["discovery"] = {
                    "available": False,
                    "unknown_reason": "discover failed: %s" % str(exc)[:300]}
            try:
                payload["activity"] = _dump(adapter.activity())
            except Exception:
                payload["activity"] = {
                    "state": "unknown", "evidence": "activity failed",
                    "checked_at": utcnow_iso()}
            try:
                payload["verification"] = _dump(adapter.verify())
            except Exception as exc:
                payload["verification"] = {
                    "passed": False, "version": "",
                    "error_detail": "verify failed: %s" % str(exc)[:300],
                    "checks": []}
            return "ok", payload
    except Exception as exc:
        return "error", {"reason": "probe crashed: %s" % str(exc)[:300]}
    return "error", {"reason": "unknown op"}


def _dump(model):
    # type: (object) -> Dict[str, Any]
    try:
        if model is None:
            return {}
        if hasattr(model, "model_dump"):
            data = model.model_dump()
        elif isinstance(model, dict):
            data = dict(model)
        else:
            return {}
        if not isinstance(data, dict):
            return {}
        # Preserve explicitly attached non-field extras (e.g.
        # daemon_expected, manual_restart_limitation): model_dump drops
        # them, but they are part of the plan contract (R15/R25/R27).
        try:
            extra = vars(model)
        except Exception:
            extra = {}
        if isinstance(extra, dict):
            for key, value in extra.items():
                try:
                    if key.startswith("_") or key in data:
                        continue
                    if isinstance(value, (str, bool, int, float)) or \
                            value is None:
                        data[key] = value
                except Exception:
                    continue
        return data
    except Exception:
        return {}


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
        try:
            status, payload = _execute_probe_op(tool_id, op)
        except Exception as exc:
            status, payload = "error", {"reason": str(exc)[:300]}
        try:
            finish_probe(conn, req_id, status,
                         _sanitize_payload(payload))
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
        bound, _why = check_binding(data, job, job_id)
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
        return "applied"
    # mark-interrupted: atomic terminalization, never rerun.
    needs_recovery = _rc.recovery_for(
        state, shows_mutation(receipt_data) if receipt_valid else False,
        bool(job.get("unresolved", 0)))
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
    return "interrupted-recovery" if needs_recovery else "interrupted"


def reconcile_claimed_jobs(conn):
    # type: (sqlite3.Connection) -> int
    """Continuous reconcile for nonterminal claimed rows + terminal rows
    with possibly unresolved runners (R04: reconcile unresolved terminals
    too). Unknown unit state always holds the reservation."""
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE state IN (?,?,?,?,?)"
            " OR unresolved=1").fetchall()
    except Exception:
        # Pre-migration schema without unresolved: nonterminals only.
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
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE state IN (?,?,?,?,?)"
            " OR unresolved=1").fetchall()
    except Exception:
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
