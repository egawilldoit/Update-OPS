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
import threading
import time
from typing import Any, Dict, Tuple

from .. import reconcile_core as _rc
from .. import units as _units
from ..config import settings
from ..db import connect, validate_schema
from ..jobs import (NONTERMINAL, claim_with_nonce, expire_stale_accepted)
from ..owner_probes import (claim_probe, expire_probes,
                            finish_probe)
from ..schemas import utcnow_iso
from ..tx import TxError, transition_tx

LOCK_PATH = os.path.join(
    getattr(settings, "state_dir", "/var/lib/ega-update")
    or "/var/lib/ega-update", "worker.lock")
POLL_INTERVAL_S = 2
LAUNCH_PROVE_TIMEOUT_S = 10
# F12 lease/TTL relationship (explicit, guarded by test): a supervised
# probe op runs at most PROBE_OP_TIMEOUT_S while its probe lease lives
# leases.PROBE_LEASE_TTL_S (180s). The 60s margin guarantees a
# long-running read can never outlive its exclusion lease; the worker
# backstop (deadline+300s alarm) bounds only crash cleanup. D5: a lease
# that expires mid-probe is NOT reclaimed by time — reconciliation
# holds it (blocking mutation admission) until the bound probe unit is
# positively confirmed stopped by the explicit unit model.
PROBE_OP_TIMEOUT_S = 120
# Bounded startup-ready handshake for the required probe executor: the
# dispatcher must never report itself operational while its probe worker
# never connected (ProbeWorker._run returns silently on connect failure).
PROBE_WORKER_START_TIMEOUT_S = 5.0
HEARTBEAT_FILENAME = "dispatcher.heartbeat"
# Durable probe-executor readiness marker (W4-D8): written ONLY by the
# ProbeWorker thread while its connection is established and its loop is
# live. External processes (deploy readiness) cannot see the in-process
# `is_ready()` event, so this is the minimal durable signal that proves
# the REQUIRED executor is alive SEPARATELY from the dispatcher heartbeat.
PROBE_WORKER_HEARTBEAT_FILENAME = "probe_worker.heartbeat"
PROBE_WORKER_HEARTBEAT_MAX_AGE_S = 20
RETENTION_STAMP = "retention.lastdate"
_lock_fh = None  # type: Any

# W8 boot-reconciliation policy. Boot reconciliation is MANDATORY and is
# never silently skipped:
#   * safe hold  - live/unknown probe unit or unresolved job row: reported
#                  in the result, never fatal (the lease/row stays held and
#                  the continuous loop retries it);
#   * retryable  - transient SQLite contention (SQLITE_BUSY/SQLITE_LOCKED):
#                  bounded retry with short exponential backoff;
#   * fatal      - any other storage error, or transient contention that
#                  outlasts the retry bound: ReconcileError; worker startup
#                  must not proceed (systemd restarts the whole service).
# A failed reconciliation NEVER releases a lease: unknown -> held.
BOOT_RECONCILE_ATTEMPTS = 5
BOOT_RECONCILE_BACKOFF_S = 0.1
BOOT_RECONCILE_MAX_BACKOFF_S = 1.0
_boot_sleep = time.sleep  # injectable in tests; bounded by the policy


class ReconcileError(RuntimeError):
    """Mandatory boot reconciliation could not be completed.

    step names the failing phase (probe_request_reconcile,
    probe_lease_reconcile, job_reconcile); attempts is the number of
    tries spent; detail is the underlying error text. Startup converts
    this into SystemExit so the worker never reports a successful
    reconciliation it did not perform.
    """

    def __init__(self, step, detail, attempts=0):
        # type: (str, str, int) -> None
        super().__init__(
            "boot reconcile %s failed after %d attempt(s): %s"
            % (step, int(attempts), detail))
        self.step = str(step)
        self.detail = str(detail)
        self.attempts = int(attempts)


_TRANSIENT_STORAGE_MARKERS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "database is busy",
)


def _is_transient_storage_error(exc):
    # type: (BaseException) -> bool
    """True only for SQLite busy/locked contention (retryable).

    Everything else (healthy schema errors, disk failures, programming
    errors) is NOT transient and must fail loudly at boot.
    """
    if not isinstance(exc, sqlite3.Error):
        return False
    try:
        text = str(exc).lower()
    except Exception:
        return False
    return any(marker in text for marker in _TRANSIENT_STORAGE_MARKERS)


def _boot_reconcile_step(step, operation, sleeper=None):
    # type: (str, Any, Any) -> Any
    """Run one mandatory boot step with bounded transient-lock retry.

    Returns the operation result unchanged (e.g. a reconcile report).
    Raises ReconcileError immediately for a non-transient failure, or
    after BOOT_RECONCILE_ATTEMPTS tries when contention persists. The
    retry loop is finite and the backoff stays small (<= 1s per wait).
    """
    wait = sleeper or _boot_sleep
    last = None  # type: Any
    for attempt in range(1, int(BOOT_RECONCILE_ATTEMPTS) + 1):
        try:
            return operation()
        except Exception as exc:
            if not _is_transient_storage_error(exc):
                raise ReconcileError(step, str(exc), attempt) from exc
            last = exc
            if attempt >= int(BOOT_RECONCILE_ATTEMPTS):
                break
            delay = BOOT_RECONCILE_BACKOFF_S * (2 ** (attempt - 1))
            try:
                wait(min(float(BOOT_RECONCILE_MAX_BACKOFF_S),
                         float(delay)))
            except Exception:
                pass
    raise ReconcileError(
        step, "transient storage contention persisted: %s" % (last,),
        int(BOOT_RECONCILE_ATTEMPTS)) from last


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


def _probe_worker_heartbeat_path():
    # type: () -> str
    return os.path.join(_state_dir(), PROBE_WORKER_HEARTBEAT_FILENAME)


def _write_probe_worker_heartbeat():
    # type: () -> None
    """Publish the durable probe-executor ready marker (W4-D8).

    Written by the ProbeWorker thread itself only after its DB connection
    succeeded, and refreshed each loop iteration while it stays ready.
    Failure to write is silent (deploy readiness then fails closed on the
    stale/absent marker)."""
    path = _probe_worker_heartbeat_path()
    try:
        parent = os.path.dirname(path)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, mode=0o700, exist_ok=True)
        payload = {"ts": utcnow_iso(), "pid": os.getpid(), "ready": True}
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


def _clear_probe_worker_heartbeat():
    # type: () -> None
    """Withdraw the marker on graceful stop (fail closed immediately)."""
    try:
        os.unlink(_probe_worker_heartbeat_path())
    except Exception:
        pass


def read_probe_worker_heartbeat(state_dir="", max_age_s=None):
    # type: (str, object) -> Dict[str, object]
    """Read the durable probe-executor marker; {} unless fresh AND ready.

    Stale, unparseable, missing, or non-ready markers yield {}. The
    dispatcher heartbeat is deliberately NOT accepted here: the probe
    executor is a separate mandatory component (W3.1).
    """
    try:
        base = str(state_dir or "") or _state_dir()
    except Exception:
        base = _state_dir()
    if not base:
        return {}
    try:
        bound = float(max_age_s) if max_age_s is not None \
            else float(PROBE_WORKER_HEARTBEAT_MAX_AGE_S)
    except (TypeError, ValueError):
        bound = float(PROBE_WORKER_HEARTBEAT_MAX_AGE_S)
    path = os.path.join(base, PROBE_WORKER_HEARTBEAT_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    if not isinstance(data, dict) or data.get("ready") is not True:
        return {}
    raw_ts = data.get("ts", "")
    if not isinstance(raw_ts, str) or not raw_ts.strip():
        return {}
    text = raw_ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        import datetime as _dt
        ts = _dt.datetime.fromisoformat(text)
        if ts.tzinfo is None:
            return {}
        now = _dt.datetime.now(_dt.timezone.utc)
        age = (now - ts.astimezone(_dt.timezone.utc)).total_seconds()
    except Exception:
        return {}
    if age < 0:
        age = 0.0
    if age > bound:
        return {}
    out = dict(data)
    out["age_s"] = int(age)
    out["max_age_s"] = int(bound)
    out["path"] = path
    return out


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
            from ..reconcile_core import prove_processes, unit_hex
            proof = prove_processes(unit_hex(job_id), job_id)
            if not proof.get("ok", False):
                return "unknown"
            if proof.get("processes"):
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


def _probe_unit_name(request_id):
    # type: (str) -> str
    """Canonical transient probe unit for one request id, or "" when the
    id cannot bind one (stop is then unprovable and must hold)."""
    try:
        from ..owner_env import transient_probe_name
        return transient_probe_name(request_id or "")
    except Exception:
        return ""


def _query_probe_unit_state(unit, timeout_s=5):
    # type: (str, float) -> str
    """Explicit unit-model state for the probe service (never inferred
    from time or from a supervision message)."""
    try:
        info = _units.query_unit(unit, timeout_s=timeout_s)
        return str((info or {}).get("state", "unknown"))
    except Exception:
        return "unknown"


def _probe_stop_proven(request_id, supervised):
    # type: (str, object) -> bool
    """D5: positive stop proof for one supervised probe execution.

    run_supervised_probe returning ok=True carries the H03 exit proof
    (probe service confirmed stopped), so it is proof. ANY other
    outcome — deadline, refusal, crash, or an explicit
    "not quiescent" failure — is re-checked against the explicit unit
    model: release requires confirmed_stopped. A missing/unqueryable
    unit leaves stop unproven (fail closed: the lease stays held).
    """
    try:
        ok = bool(supervised[0])
    except Exception:
        return False
    if ok:
        return True
    unit = _probe_unit_name(request_id)
    if not unit:
        return False
    return _query_probe_unit_state(unit) == _units.CONFIRMED_STOPPED


def _execute_probe_op(tool_id, op, request_id=""):
    # type: (str, str, str) -> Tuple[str, Dict[str, Any], bool]
    """Run one probe op supervised (N10, F03, H04): a worker process
    inside a transient probe SERVICE with the same NNP-off owner
    profile as the job runner — never an inherited scope from the
    NNP-on dispatcher — plus a monotonic deadline AND the canonical
    contract env, the exact environment execution phases receive.
    A hanging read-only probe can never wedge the dispatcher loop.

    Returns (status, payload, stop_proven). stop_proven is True only
    when the probe execution is positively known to have ended (or was
    never launched at all); the caller may release the probe lease only
    then.
    """
    from .phase_run import run_supervised_probe
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
        # No launch can have happened: stop is vacuously proven.
        return "error", {"reason": "owner contract unbuildable: %s" % exc}, \
            True
    try:
        supervised = run_supervised_probe(
            tool_id, request_id or "probe", {"op": op},
            PROBE_OP_TIMEOUT_S, settings, log_dir,
            lambda _s, _l: None, op=op, env=env)
    except Exception as exc:
        # The unit may exist: stop is UNKNOWN and must hold the lease.
        return "error", {"reason": "supervision failed: %s" % exc}, False
    stop_proven = _probe_stop_proven(request_id, supervised)
    try:
        ok, data, error, timed_out = supervised
    except Exception:
        return "error", {"reason": "supervision result malformed"}, False
    if timed_out:
        return "error", {"reason": "probe deadline exceeded"}, stop_proven
    if not ok:
        return "error", {"reason": (error or "probe failed")[:300]}, \
            stop_proven
    if not isinstance(data, dict):
        return "error", {"reason": "probe result malformed"}, stop_proven
    return "ok", data, stop_proven


def run_probe_queue(conn):
    # type: (sqlite3.Connection) -> int
    """Execute claimed probe requests with mutation exclusion (R01, F12,
    D5).

    Every installation read holds a probe lease while touching the
    installation; active mutations defer probes, and the atomic lease
    acquire closes the residual race. The lease is bound to the probe
    request id + canonical probe unit and is released ONLY on proven
    stop (never in a blanket finally): an unproven/non-quiescent stop
    keeps the lease held so mutation admission stays blocked until
    reconciliation proves the unit stopped. Returns processed count.
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
        # F12: EVERY installation read holds a probe lease while touching
        # the installation (INSTALLATION_READ_OPS == all current ops).
        # Fast path first: an active mutation defers without taking a
        # lease; then the atomic lease acquire closes the residual race
        # (a mutation admitted between the check and the acquire makes
        # the acquire fail and the probe defers).
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
        lease_id = None
        try:
            from ..leases import acquire_probe_lease
            lease_id = acquire_probe_lease(
                conn, tool_id, "dispatcher-%d" % os.getpid(),
                request_id=req_id)
        except Exception:
            lease_id = None
        if not lease_id:
            try:
                finish_probe(conn, req_id, "deferred", {})
            except Exception:
                pass
            done += 1
            continue
        stop_proven = False
        try:
            status, payload, stop_proven = _execute_probe_op(
                tool_id, op, req_id)
        except Exception as exc:
            status, payload = "error", {"reason": str(exc)[:300]}
            stop_proven = False
        if lease_id and not stop_proven:
            # D5 evidence: events.job_id REFERENCES jobs(id), so a probe
            # request id can never be recorded there (the insert fails
            # closed and would be silently dropped). Persist the held
            # fact in the durable probe result instead — machine-
            # readable booleans/ids only; status/reason unchanged.
            try:
                if not isinstance(payload, dict):
                    payload = {"reason": str(payload)[:300]}
                payload["exclusion_held"] = True
                payload["lease_id"] = str(lease_id)
                payload["reconciliation"] = "required"
            except Exception:
                pass
        try:
            clean = _sanitize_payload(payload)
        except Exception:
            # N12: payload that cannot be sanitized is never persisted
            # raw; the probe reports an evidence failure instead.
            clean, status = {"reason": "evidence_sanitization_failed"}, \
                "error"
        # W3 target lifecycle: RESULT DURABLY STORED -> OBSERVATION
        # DURABLY APPLIED -> EXECUTION CONFIRMED STOPPED -> EXCLUSION
        # RELEASED. The result is stored before the lease is released so
        # a mutation admitted after release cannot clobber an
        # observation whose result was never persisted; application is
        # idempotent and reconciliation retries from durable state.
        stored = False
        try:
            stored = bool(finish_probe(conn, req_id, status, clean))
        except Exception:
            stored = False
        if stored:
            try:
                from .. import observation as _observation
                _observation.apply_probe_result(conn, req_id)
            except Exception:
                pass
        # D5: release the exclusion ONLY on positive stop proof AND only
        # after the result is durable. An unproven stop (or an unstored
        # result) keeps the lease held (mutation admission stays blocked)
        # until reconciliation proves the bound probe unit stopped —
        # never by TTL/time and never in a blanket finally.
        if lease_id and stop_proven and stored:
            try:
                from ..leases import release_lease as _release
                _release(conn, lease_id)
            except Exception:
                pass
        done += 1
    try:
        expire_probes(conn)
    except Exception:
        pass
    try:
        from .. import observation as _observation
        _observation.reconcile_observations(conn)
    except Exception:
        pass
    return done


class ProbeWorker(object):
    """Bounded probe executor on its own SQLite connection (W3).

    The dispatcher main loop never executes a probe: one probe chain can
    run up to PROBE_OP_TIMEOUT_S here while the main loop keeps writing
    the heartbeat, dispatching jobs, and reconciling. SQLite is
    single-writer; short transactions only, never a tx across the
    supervised subprocess call.

    Concurrency is exactly one: the loop claims/executes/finishes one
    queue pass at a time on this thread.
    """

    def __init__(self, db_path="", interval_s=POLL_INTERVAL_S):
        # type: (str, float) -> None
        self._db_path = str(db_path or settings.db_path)
        try:
            self._interval_s = max(0.05, float(interval_s))
        except (TypeError, ValueError):
            self._interval_s = float(POLL_INTERVAL_S)
        self._stop = threading.Event()
        self._thread = None  # type: Any
        self._ready = threading.Event()
        self._startup_error = ""

    def start(self, timeout_s=None):
        # type: (float) -> "ProbeWorker"
        """Start the executor thread and prove it became ready.

        Bounded startup handshake (W3.1): the thread sets _ready only
        after its DB connection succeeded, so start() returning is never
        mistaken for a live executor. Callers MUST check is_ready() (or
        use _require_probe_worker) before treating the dispatcher as
        operational. On failure (or timeout) startup_error() explains.
        """
        t = self._thread
        if t is not None and t.is_alive() and self._ready.is_set():
            return self
        self._stop.clear()
        self._ready.clear()
        self._startup_error = ""
        self._thread = threading.Thread(
            target=self._run, name="ega-probe-worker", daemon=True)
        self._thread.start()
        try:
            bound = float(timeout_s) if timeout_s is not None \
                else float(PROBE_WORKER_START_TIMEOUT_S)
        except (TypeError, ValueError):
            bound = float(PROBE_WORKER_START_TIMEOUT_S)
        deadline = time.monotonic() + max(0.05, bound)
        while time.monotonic() < deadline:
            if self._ready.is_set():
                return self
            if not self._thread.is_alive():
                break
            time.sleep(0.01)
        if not self._startup_error:
            self._startup_error = "probe worker failed to become ready"
        return self

    def _run(self):
        # type: () -> None
        try:
            conn = connect(self._db_path)
        except Exception as exc:
            # Startup handshake: report the failure instead of returning
            # silently (the dispatcher main loop gates on is_ready()).
            self._startup_error = "probe worker database unavailable: %s" \
                % type(exc).__name__
            return
        self._ready.set()
        _write_probe_worker_heartbeat()
        try:
            while not self._stop.is_set():
                try:
                    run_probe_queue(conn)
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                # Refresh the durable ready marker while this executor
                # thread stays live (W4-D8); it stops advancing on death.
                _write_probe_worker_heartbeat()
                self._stop.wait(self._interval_s)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def stop(self, timeout_s=5.0):
        # type: (float) -> None
        self._stop.set()
        self._ready.clear()
        t = self._thread
        if t is not None:
            try:
                t.join(timeout=max(0.1, float(timeout_s)))
            except Exception:
                pass
        _clear_probe_worker_heartbeat()

    def is_alive(self):
        # type: () -> bool
        t = self._thread
        return bool(t is not None and t.is_alive())

    def is_ready(self):
        # type: () -> bool
        """True only while the executor is alive AND its DB connection
        was established (post-startup handshake). Fail closed."""
        return bool(self._ready.is_set() and self.is_alive())

    def startup_error(self):
        # type: () -> str
        return self._startup_error


def _require_probe_worker(worker):
    # type: (Any) -> None
    """Fail closed on a missing/never-ready/dead required probe executor.

    The dispatcher must never keep heartbeating as operational while its
    required probe executor has permanently died (W3.1). Raising
    SystemExit terminates the process so systemd restarts the complete
    service and boot reconciliation owns unresolved durable probes.
    SystemExit is a BaseException, so the loop's `except Exception` never
    swallows it.
    """
    try:
        ready = bool(worker is not None and worker.is_ready())
    except Exception:
        ready = False
    if not ready:
        raise SystemExit("probe worker not ready; dispatcher exiting")


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
    from ..receipts import (apply_receipt, check_binding,
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
    # H03: structured process proof — an unprovable scan (None) holds
    # via decide(), exactly like surviving processes. A bare [] is only
    # ever passed when the scan COMPLETED empty.
    try:
        _proof = _rc.prove_processes(_rc.unit_hex(job_id), job_id)
    except Exception:
        _proof = {"ok": False, "processes": [], "reason": "proof crashed"}
    try:
        procs = list(_proof.get("processes", []) or []) \
            if _proof.get("ok", False) else None
    except Exception:
        procs = None
    # G02/G03: delegated proof is computed only once the unit itself is
    # confirmed stopped (otherwise decide() holds on the unit alone and
    # no service queries are spent). It is REQUIRED before any receipt
    # application or ownership release.
    delegated = None
    scopes_quiescent = False
    scopes_reason = ""
    if str(info.get("state", "")) == "confirmed_stopped":
        # H03: every job-owned phase scope must itself be confirmed
        # stopped — hex-less stray children are invisible to the
        # process scan. A non-quiescent scope holds below (never
        # applies, never releases) even with a perfect receipt.
        try:
            scopes_quiescent, _scope_ev, scopes_reason = \
                _rc.phase_scopes_quiescence(job_id)
        except Exception as exc:
            scopes_quiescent = False
            scopes_reason = "phase scope proof crashed: %s" % exc
        try:
            quiescent, evidence, reason = _rc.delegated_quiescence(
                conn, job)
            delegated = {"quiescent": bool(quiescent),
                         "evidence": evidence, "reason": reason}
        except Exception as exc:
            delegated = {"quiescent": False, "evidence": [],
                         "reason": "delegated proof crashed: %s" % exc}
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
    if str(info.get("state", "")) == "confirmed_stopped" and \
            not scopes_quiescent:
        action, detail = "keep-unknown", \
            "phase scopes unproven (%s); holding reservation" % (
                scopes_reason or "scope proof missing")[:200]
    else:
        action, detail = _rc.decide(job, info, receipt_view, procs,
                                    delegated)
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
            # H05 final: resolved-success mode. decide() returned
            # apply-receipt solely for fully proven execution
            # quiescence (unit stopped + phase scopes stopped + no
            # processes + delegated quiescent), so the apply itself
            # clears historical unresolved/recovery markers
            # atomically for a clean succeeded receipt — before the
            # ownership release below. Anything else preserves flags.
            applied = apply_receipt(conn, receipt_data, job_id,
                                    execution_quiescent=True)
        except ValueError as exc:
            _event(conn, job_id, "reconcile_receipt_rejected",
                   str(exc)[:500])
            return "receipt-rejected"
        except Exception as exc:
            _event(conn, job_id, "reconcile_receipt_error",
                   str(exc)[:300])
            return "receipt-error"
        try:
            # H05: ONE canonical recovery decision. Proven success
            # (valid bound receipt, disposition none, execution
            # quiescent — just established by decide()) is resolved
            # and must NOT set recovery; mutation evidence alone
            # never implies recovery.
            needs_recovery, _rec_reason = \
                _rc.recovery_required_for_outcome(
                    applied, receipt_data, receipt_valid=True,
                    unresolved=bool(job.get("unresolved", 0)),
                    prior_state=state, execution_quiescent=True)
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
    # H05: the canonical decision — pre-mutation anchors without
    # mutation evidence resolve; window/interrupted states require.
    needs_recovery, _rec_reason = _rc.recovery_required_for_outcome(
        state, receipt_data if receipt_valid else None,
        receipt_valid=bool(receipt_valid),
        unresolved=bool(job.get("unresolved", 0)),
        prior_state=state, execution_quiescent=True)
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


def _reconcile_candidates(conn, strict=False):
    # type: (sqlite3.Connection, bool) -> list
    """Rows needing reconcile: nonterminals, unresolved markers, and
    terminal jobs whose mutation lease is still held (F02: otherwise a
    terminal+held-lease row would never be revisited and its lease never
    released). strict=True (boot path) re-raises transient SQLite
    contention instead of degrading to a silent empty list."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT jobs.* FROM jobs LEFT JOIN execution_leases"
            " ON execution_leases.kind='mutation'"
            " AND execution_leases.job_id=jobs.id"
            " AND execution_leases.released_at=''"
            " WHERE jobs.state IN (?,?,?,?,?) OR jobs.unresolved=1"
            " OR execution_leases.id IS NOT NULL").fetchall()
        return list(rows)
    except Exception as exc:
        if strict and _is_transient_storage_error(exc):
            raise
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE state IN (?,?,?,?,?)"
            " OR unresolved=1").fetchall()
        return list(rows)
    except Exception as exc:
        if strict and _is_transient_storage_error(exc):
            raise
    # Pre-migration schema without unresolved: nonterminals only.
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE state IN (?,?,?,?,?)",
            NONTERMINAL).fetchall()
        return list(rows)
    except Exception as exc:
        if strict and _is_transient_storage_error(exc):
            raise
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


def reconcile_boot_jobs(conn):
    # type: (sqlite3.Connection) -> Dict[str, Any]
    """Boot-path job reconcile with explicit failure classification.

    Transient storage contention propagates so the boot wrapper can
    retry it. A non-transient per-row failure is a safe hold: the row
    stays nonterminal and the continuous loop retries it; it is counted
    in the returned report and never silently dropped. Unknown/live
    units remain holds by design (reconcile_core.decide)."""
    report = {"candidates": 0, "acted": 0, "held": 0, "errors": 0}
    rows = _reconcile_candidates(conn, strict=True)
    report["candidates"] = len(rows)
    for row in rows:
        try:
            nonce = row["dispatch_nonce"]
        except Exception:
            nonce = ""
        try:
            state = row["state"]
        except Exception:
            report["errors"] += 1
            continue
        if state in NONTERMINAL and not nonce:
            continue  # accepted, never claimed: expiry owns it
        try:
            outcome = _reconcile_row(conn, row)
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            if _is_transient_storage_error(exc):
                raise
            report["errors"] += 1
            continue
        if outcome not in ("live", "skipped", "unknown-held"):
            report["acted"] += 1
        else:
            report["held"] += 1
    return report


def reconcile_probe_requests(conn, units_mod=None, only_past_deadline=False):
    # type: (sqlite3.Connection, object, bool) -> Dict[str, Any]
    """Restart reconciliation of durable probe execution state (W3).

    Durable facts decide; time never does:
    - request has a durable result       -> finalize (never rerun);
    - unit confirmed stopped, no result  -> resume when the claim
      deadline has not passed, else mark expired (classified terminal);
    - unit live/starting/stopping/unknown -> hold: the request stays
      claimed and the bound probe lease keeps blocking mutation
      admission (recovery-required equivalent).
    A stored-but-unapplied refresh result is applied (idempotently).
    units_mod defaults to backend.app.units; tests inject a fake.

    only_past_deadline=True is the loop path: it leaves live in-flight
    probes untouched (no unit query while a probe is inside its bounded
    execution window) and only resolves or holds orphaned claims whose
    claim deadline already passed.
    """
    report = {"checked": 0, "resumed": 0, "expired": 0, "finalized": 0,
              "held": 0, "applied": 0}  # type: Dict[str, Any]
    try:
        rows = conn.execute(
            "SELECT * FROM probe_requests WHERE state='running'").fetchall()
    except Exception as exc:
        # W8: transient contention must reach the boot retry (a silent
        # empty report would claim a reconciliation that never ran).
        if _is_transient_storage_error(exc):
            raise
        return report
    query = units_mod or _units
    stopped = str(getattr(query, "CONFIRMED_STOPPED",
                          _units.CONFIRMED_STOPPED)
                  or _units.CONFIRMED_STOPPED)
    now = utcnow_iso()
    for row in rows:
        try:
            rid = str(row["id"] or "")
            deadline = str(row["claim_deadline"] or "")
        except Exception:
            continue
        if not rid:
            continue
        if only_past_deadline and not (deadline and deadline < now):
            continue
        report["checked"] += 1
        try:
            has_result = conn.execute(
                "SELECT 1 FROM probe_results WHERE request_id=?",
                (rid,)).fetchone() is not None
        except Exception:
            has_result = True  # unreadable: hold (fail closed)
        if has_result:
            try:
                conn.execute(
                    "UPDATE probe_requests SET state='done' WHERE id=?"
                    " AND state='running'", (rid,))
                conn.commit()
                report["finalized"] += 1
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                # W8: surface transient storage contention (boot retries);
                # any other storage failure leaves the row running (hold).
                if _is_transient_storage_error(exc):
                    raise
            continue
        unit = _probe_unit_name(rid)
        unit_state = "unknown"
        if unit:
            try:
                info = query.query_unit(unit, timeout_s=5)
                unit_state = str((info or {}).get("state", "unknown"))
            except Exception:
                unit_state = "unknown"
        if unit_state != stopped:
            report["held"] += 1
            continue
        try:
            if deadline and deadline > now:
                conn.execute(
                    "UPDATE probe_requests SET state='queued', owner=''"
                    " WHERE id=? AND state='running'", (rid,))
                report["resumed"] += 1
            else:
                conn.execute(
                    "UPDATE probe_requests SET state='expired'"
                    " WHERE id=? AND state='running'", (rid,))
                report["expired"] += 1
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            # W8: transient storage contention is retryable by the boot
            # wrapper; anything else leaves the request running (hold).
            if _is_transient_storage_error(exc):
                raise
    try:
        from .. import observation as _observation
        report["applied"] = int(
            _observation.reconcile_observations(conn) or 0)
    except Exception:
        pass
    return report


def reconcile_boot(conn):
    # type: (sqlite3.Connection) -> Dict[str, Any]
    """On start: probe-request + probe-lease reconcile, then job rows.

    Never auto-resumes work. D5: a dispatcher restart does not stop the
    transient probe service surviving under the user manager, so every
    unreleased probe lease (expired or not) is reconciled with positive
    stop proof — a live survivor keeps blocking mutation admission. W3
    adds durable probe-request reconciliation (resume/terminal) and
    observation application before any exclusion release.

    W8 return/failure contract (never a void swallow):
    - returns a structured per-step report
      {"probe_request_reconcile": ..., "probe_lease_reconcile": ...,
       "job_reconcile": ...}; step reports carry the safe holds;
    - transient SQLite contention is retried with a short bounded
      backoff (BOOT_RECONCILE_*);
    - if a mandatory step still cannot complete, raises ReconcileError:
      startup fails closed (see _reconcile_boot_or_exit) and NO lease is
      released on failure (unknown reconciliation -> lease held).

    Failure classification per step:
    - probe_request_reconcile / probe_lease_reconcile:
      live/unknown unit state = safe hold (reported); SQLITE_BUSY/locked
      = retryable; any other storage error or exhausted retries = fatal.
    - job_reconcile:
      unknown/live unit or per-row non-storage error = safe hold
      (reported in "errors"/"held"; continuous loop retries); transient
      storage contention = retryable; exhausted retries = fatal.
    """
    # W3 lifecycle order: finalize/apply durable result state BEFORE any
    # exclusion release, so a mutation admitted right after a release can
    # never race an unapplied observation.
    request_report = _boot_reconcile_step(
        "probe_request_reconcile",
        lambda: reconcile_probe_requests(conn))
    lease_report = _boot_reconcile_step(
        "probe_lease_reconcile", lambda: _reconcile_boot_leases(conn))
    job_report = _boot_reconcile_step(
        "job_reconcile", lambda: reconcile_boot_jobs(conn))
    return {"probe_request_reconcile": request_report or {},
            "probe_lease_reconcile": lease_report or {},
            "job_reconcile": job_report or {}}


def _reconcile_boot_leases(conn):
    # type: (sqlite3.Connection) -> Dict[str, Any]
    """Boot-path probe-lease reconcile (positive stop proof only).

    Transient storage contention originates in the strict release and
    propagates; a legitimate live/unknown unit is reported as held."""
    from ..leases import reconcile_probe_leases
    return reconcile_probe_leases(conn, include_unexpired=True)


def _reconcile_boot_or_exit(conn):
    # type: (sqlite3.Connection) -> Dict[str, Any]
    """Startup gate: boot reconciliation must complete, or the worker
    must not start. ReconcileError -> SystemExit so systemd restarts the
    whole service and retries; a failed reconciliation never releases a
    lease (fail closed)."""
    try:
        return reconcile_boot(conn)
    except ReconcileError as exc:
        raise SystemExit("boot reconciliation failed: %s" % exc)


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


def _validate_or_exit(role):
    # type: (str) -> None
    """D2 startup gate: run readiness for role and fail closed.

    Extracted from main() so the gate is directly testable without the
    dispatcher loop or DB connect. Behavior unchanged: any readiness
    failure (ReadinessError or any other exception) becomes a SystemExit
    whose message names fields/paths only, never secret contents. A clean
    check returns None and startup continues.
    """
    try:
        from ..readiness import validate_startup
        validate_startup(role, settings)
    except Exception as exc:
        raise SystemExit("worker readiness failed: %s" % exc)


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
    _validate_or_exit("worker")
    conn = connect(settings.db_path)
    try:
        validate_schema(conn)
    except Exception as exc:
        raise SystemExit("schema validation failed: %s" % exc)
    # W8: mandatory boot reconciliation; completes or startup aborts.
    _reconcile_boot_or_exit(conn)
    # W3: probes execute on a bounded worker thread with its own
    # connection (the thread owns the reference). The main loop keeps
    # heartbeat, job dispatch, and reconciliation alive while a
    # 120s-class probe runs. W3.1: the worker is RETAINED and supervised
    # — the dispatcher never reports operational health without a live,
    # connected probe executor (SystemExit on death => systemd restart).
    probe_worker = ProbeWorker(settings.db_path)
    probe_worker.start()
    _require_probe_worker(probe_worker)
    while True:
        try:
            # Fail closed before any heartbeat/dispatch work: a dead
            # required executor terminates the process (SystemExit is a
            # BaseException, never caught by `except Exception` below).
            _require_probe_worker(probe_worker)
            expire_stale_accepted(conn)
            # W3 backstop: apply/finalize durable probe state first
            # (proof-based; live probes inside their window are
            # untouched), then reconcile probe leases.
            try:
                reconcile_probe_requests(conn, only_past_deadline=True)
            except Exception:
                pass
            try:
                from ..leases import reconcile_expired_probes
                reconcile_expired_probes(conn)
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
