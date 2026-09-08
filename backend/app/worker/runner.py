"""Job runner: phase machine + adapter invocation.

Phases: accepted -> preflight -> backup -> updating -> verifying -> succeeded,
with blocked / failed / health_failed / interrupted terminals. The runner owns
the execution lock for the full procedure and the hard deadline for every
phase (adapter plan step timeouts; adapters run mutations with
step_timeout + registry.MUTATION_TIMEOUT_MARGIN_S so the runner fires first).
Dispatcher launches via the ubuntu user manager with a dispatch nonce
(``runner <job-id> <nonce>``); the runner verifies the nonce against
jobs.dispatch_nonce (and state==preflight) before any mutation and exits 6
without touching the tool on mismatch (H-01 replay protection).

Rules honored:
- Preflight rechecks plan freshness, fingerprint, activity (incl. recorded
  ack), disk floor, git cleanliness, install-method support, and backup
  capability; refreshes jobs.before_version from the live inspect version
  (H-02). Any failure yields blocked with a reason; files/Git stay intact.
- Backup runs in a supervised phase worker; failure blocks before mutation.
- Execute runs in a supervised phase worker (phase_run) inside a
  coordinator-owned transient scope under the plan step deadline. Any
  timeout (coordinator deadline, adapter timeout report, or
  error_code=='timeout') funnels into the hard-timeout path: the scope
  cgroup is killed (never the coordinator's own unit), quiescence is
  proven, delegated services are inspected, bounded read-only recovery
  checks run, then terminal failed/interrupted + recovery_required
  whenever completion is not fully proved. No supervised worker Python
  survives a declared timeout.
- Verify runs in a supervised phase worker. Installer nonzero exit yields
  failed even when the old version stays healthy. Zero exit plus mandatory
  check failure yields health_failed. Exact-target plans
  (target_mode=='exact') yield health_failed/exact_target_mismatch when
  after_version != plan target, regardless of adapter opinion. Missing
  required verification never yields success.
- stdout/stderr stream live from the phase worker's sanitized stream file
  with per-job sequence numbers starting at 1, JSONL flush at least every
  second, and a 20 MiB/job cap (persisting stops but pipes keep draining,
  with the unchanged truncation marker). A partial final record after a
  crash is tolerated. A persisted bound completion receipt
  (receipts.build_receipt v2) is required; success is never returned
  without durable evidence: JobLog, check, and receipt write failures set
  a sticky flag that finalize maps to interrupted/storage_failure.
- Terminal state is operation outcome only: the runner never releases
  mutation ownership (F02). The dispatcher/SSH reconciler disposes the
  mutation lease after proving execution quiescence.
- A DB write failure before mutation blocks. During execution the bound
  receipt is preserved where possible and recovery_required is marked after
  reconciliation. No retry, no browser cancellation.

Python 3.10 compatible.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from typing import Any, Dict, List, Optional, Tuple

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_BLOCKED = 3
EXIT_INSTALL_FAILED = 4
EXIT_VERIFY_FAILED = 5
EXIT_INTERRUPTED = 6


# -- supervision: scopes owned by the runner, killed as cgroups --------------
# Phase work runs in supervised worker processes inside per-phase scope
# units (phase_run.run_supervised_phase); the runner (coordinator, in the
# parent job unit) kills scopes — never its own unit. Reparented children
# stay in the scope cgroup, so scope-kill + quiescence verify
# (units.query_unit) is proof; PPID scans are not used for termination.


def _job_processes_alive(job_id):
    # type: (str) -> list
    """Execution-marked processes for this job, observer excluded (R07).

    Matches the canonical unit hex + runner markers (never the bare
    dashed UUID the observer itself carries), so the caller never
    detects itself as a live updater.
    """
    try:
        from ..reconcile_core import job_processes, unit_hex
        return job_processes(unit_hex(job_id), job_id)
    except Exception:
        return []


def _is_timeout_result(exec_result):
    # type: (Any) -> bool
    """True for any adapter-reported timeout signal.

    Covers ExecuteResult.timed_out, ProcResult-style timed_out attrs, and
    error_code=='timeout' so every adapter timeout funnels into the runner
    hard-timeout path (single timeout owner: the runner).
    """
    try:
        if bool(getattr(exec_result, "timed_out", False)):
            return True
    except Exception:
        pass
    try:
        if isinstance(exec_result, dict):
            code = exec_result.get("error_code", "")
        else:
            code = getattr(exec_result, "error_code", "")
        if code == "timeout":
            return True
    except Exception:
        pass
    return False


def _exact_target_mismatch(target_mode, target, after_version):
    # type: (object, object, object) -> bool
    """Central exact-target guard: exact plans must land exactly on target."""
    try:
        if str(target_mode or "") != "exact":
            return False
        return str(after_version or "") != str(target or "")
    except Exception:
        return False


# -- redacted JSONL log ------------------------------------------------------

def _known_secrets():
    # type: () -> tuple
    """Known secret values for redaction (never logged).

    Reads via backend.app.config.load_secret_values(settings); the settings
    object is always passed explicitly (never a bare call). G05: no
    silent degradation — a configured-but-broken secret source raises
    SecretSourceError so JobLog initialization fails instead of
    persisting with zero secrets.
    """
    from ..config import load_secret_values as _loader
    from ..config import settings as _settings

    values = _loader(_settings)
    return tuple(v for v in (values or ()) if v)


class JobLog(object):
    """Per-job ordered sanitized JSONL log with cap + truncation marker.

    All bytes flow through sanitize.SanitizingStream: empty sanitizer
    output means buffered (never raw fallback); incomplete sensitive
    blocks are withheld, never finalized by timer flushes; sanitizer
    failure sets persist_failed (fail closed). Sequence starts at 1.
    """

    def __init__(self, path, cap_bytes, secrets=()):
        # type: (str, int, tuple) -> None
        self.path = path
        self.cap_bytes = int(cap_bytes)
        # Log sequence starts at 1: the first record is seq=1, so a reader
        # with after=0 delivers everything.
        self.seq = 1
        self.bytes_written = 0
        self.truncated = False
        # Sticky evidence flag: any persistence failure maps finalize away
        # from success (never success without durable evidence).
        self.persist_failed = False
        self._fh = None
        self._last_flush = time.time()
        self._lock = threading.Lock()
        self._secrets = tuple(secrets or ())
        self._streams = {}
        try:
            from ..sanitize import SanitizingStream
            for _name in ("stdout", "stderr", "event"):
                self._streams[_name] = SanitizingStream(
                    tuple(secrets or ()))
        except Exception:
            self._streams = {}
            self.persist_failed = True

    def open(self):
        # type: () -> None
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent and not os.path.exists(parent):
            os.makedirs(parent, mode=0o700, exist_ok=True)
        # Partial-final-record tolerance: never append mid-line after a crash.
        if os.path.exists(self.path):
            try:
                with open(self.path, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    size = fh.tell()
                    tail = b""
                    if size:
                        fh.seek(max(0, size - 1), os.SEEK_SET)
                        tail = fh.read(1)
                if tail not in (b"", b"\n"):
                    with open(self.path, "ab") as fh:
                        fh.write(b"\n")
                self.bytes_written = os.path.getsize(self.path)
                # Recover the sequence counter so seq stays increasing.
                with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            record = json.loads(line)
                            seq = int(record.get("seq", 0))
                            if seq >= self.seq:
                                self.seq = seq + 1
                        except Exception:
                            continue
                        if "[truncated: per-job log cap reached;" in line:
                            self.truncated = True
            except OSError:
                pass
        self._fh = open(self.path, "a", encoding="utf-8")

    def _write_record(self, stream, line):
        # type: (str, str) -> None
        try:
            from ..schemas import utcnow_iso

            ts = utcnow_iso()
        except Exception:
            # +00:00 (not Z) so Python 3.10 fromisoformat parses everywhere.
            ts = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        record = {"seq": self.seq, "ts": ts, "stream": stream, "line": line}
        try:
            encoded = json.dumps(record, ensure_ascii=False) + "\n"
        except Exception:
            encoded = json.dumps(
                {"seq": self.seq, "ts": ts, "stream": stream,
                 "line": "[unencodable output omitted]"}) + "\n"
        if self.bytes_written + len(encoded.encode("utf-8")) > self.cap_bytes:
            if not self.truncated:
                self.truncated = True
                marker = {"seq": self.seq, "ts": ts, "stream": "event",
                          "line": "[truncated: per-job log cap reached; "
                                  "draining without persisting]"}
                try:
                    self._fh.write(json.dumps(marker) + "\n")
                    self._fh.flush()
                except OSError:
                    self.persist_failed = True
            self.seq += 1
            return
        try:
            self._fh.write(encoded)
            self.bytes_written += len(encoded.encode("utf-8"))
            self.seq += 1
        except OSError:
            self.persist_failed = True
            return
        now = time.time()
        if now - self._last_flush >= 1.0:
            try:
                self._fh.flush()
            except OSError:
                self.persist_failed = True
            self._last_flush = now

    def emit(self, stream, text):
        # type: (str, str) -> None
        """Sanitize and persist (or drain-only past the cap). Thread-safe.

        Empty sanitizer output means the input is intentionally buffered
        (e.g. an incomplete sensitive block) — never raw fallback (R10).
        """
        if stream not in ("stdout", "stderr", "event"):
            stream = "stdout"
        with self._lock:
            if self._fh is None:
                return
            target = self._streams.get(stream)
            if target is None:
                self.persist_failed = True
                return
            try:
                chunk = text if text.endswith("\n") else text + "\n"
                lines = target.feed(chunk.encode("utf-8", errors="replace"))
            except Exception:
                # Sanitizer failure fails closed (R10): mark evidence bad,
                # never emit raw.
                self.persist_failed = True
                return
            for line in lines:
                self._write_record(stream, line)

    def _tick(self):
        # type: () -> None
        """Timer flush: complete non-sensitive lines only (R10).

        Never finalizes an incomplete sensitive block.
        """
        for name, target in self._streams.items():
            try:
                lines = target.flush_tick()
            except Exception:
                self.persist_failed = True
                continue
            for line in lines:
                self._write_record(name, line)

    def flush(self):
        # type: () -> None
        with self._lock:
            if self._fh is not None:
                try:
                    self._tick()
                    self._fh.flush()
                except OSError:
                    self.persist_failed = True
                self._last_flush = time.time()

    def close(self):
        # type: () -> None
        try:
            with self._lock:
                if self._fh is not None:
                    # End-of-stream: incomplete blocks become a marker,
                    # never body text (R10).
                    for name, target in self._streams.items():
                        try:
                            lines = target.flush_final()
                        except Exception:
                            self.persist_failed = True
                            continue
                        for line in lines:
                            self._write_record(name, line)
                    try:
                        self._fh.flush()
                    except OSError:
                        self.persist_failed = True
        finally:
            with self._lock:
                if self._fh is not None:
                    try:
                        self._fh.close()
                    except OSError:
                        self.persist_failed = True
                    self._fh = None


# -- runner -------------------------------------------------------------------

class Runner(object):
    def __init__(self, job_id, nonce=""):
        # type: (str, str) -> None
        self.job_id = job_id
        self.expected_nonce = nonce or ""
        self.conn = None  # type: Optional[sqlite3.Connection]
        self.job = None  # type: Optional[Dict[str, Any]]
        self.plan_row = None  # type: Optional[Dict[str, Any]]
        self.adapter = None  # type: Any
        self.log = None  # type: Optional[JobLog]
        self._lock_fh = None
        self._stop = threading.Event()
        self._timed_out = False
        # Sticky evidence flag: JobLog/check/receipt write failures converge
        # finalize to interrupted/storage_failure (never success without
        # durable evidence).
        self._evidence_failed = False
        self.tool_id = ""
        self.before_version = ""
        self.after_version = ""
        self.exit_code = EXIT_INTERRUPTED
        self.installer_exit = EXIT_INTERRUPTED
        self.install_outcome = ""
        self.actual_change = False
        self.error_code = "interrupted"
        self.error_detail = ""
        self.checks = []  # type: List[Dict[str, Any]]
        self.backup_summary = ""
        self.verify_passed = False
        # Guarded-transition cursor (R05): every _set_state expects this.
        self._known_state = "accepted"
        self._release_path = ""
        self._before_commit = ""
        self._after_commit = ""

    # -- setup ----------------------------------------------------------

    def _settings(self):
        # type: () -> Any
        from ..config import settings

        return settings

    def _connect(self):
        # type: () -> None
        # N04: execution NEVER migrates. Startup validates only; the
        # controlled deploy procedure owns migration. Mismatch fails early
        # before any mutation or tool touch.
        from ..db import connect, validate_schema

        settings = self._settings()
        self.conn = connect(settings.db_path)
        validate_schema(self.conn)

    def _load_rows(self):
        # type: () -> Tuple[bool, str]
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE id=?", (self.job_id,)).fetchone()
        if row is None:
            return False, "job not found: %s" % self.job_id
        self.job = dict(row)
        self.tool_id = self.job.get("tool_id", "")
        self.before_version = self.job.get("before_version", "")
        plan = self.conn.execute(
            "SELECT * FROM plans WHERE id=?", (self.job.get("plan_id"),)).fetchone()
        if plan is None:
            return False, "trusted server plan missing for job %s" % self.job_id
        self.plan_row = dict(plan)
        if self.plan_row.get("tool_id") != self.tool_id:
            return False, "plan tool mismatch for job %s" % self.job_id
        return True, ""

    def _acquire_execution_lock(self):
        # type: () -> Tuple[bool, str]
        # Execution lock path derives from settings.state_dir, the same
        # convention the dispatcher uses for its state directory.
        settings = self._settings()
        state_dir = getattr(settings, "state_dir", "/var/lib/ega-update") or "/var/lib/ega-update"
        lock_path = os.path.join(state_dir, "execution.lock")
        parent = os.path.dirname(lock_path)
        try:
            if parent and not os.path.exists(parent):
                os.makedirs(parent, mode=0o700, exist_ok=True)
            fh = open(lock_path, "w")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                fh.close()
                return False, "another runner holds the execution lock"
            self._lock_fh = fh
            return True, ""
        except OSError as exc:
            return False, "execution lock unavailable blocks mutation: %s" % exc

    def _release_lock(self):
        # type: () -> None
        if self._lock_fh is not None:
            try:
                fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._lock_fh.close()
            except OSError:
                pass
            self._lock_fh = None

    def _open_log(self):
        # type: () -> None
        # G05: durable log initialization is a mutation gate. Secrets
        # failure raises from _known_secrets; an unopenable log raises
        # here. run() maps both to blocked BEFORE any mutation.
        settings = self._settings()
        log_dir = getattr(settings, "log_dir", "/var/lib/ega-update/logs")
        cap = int(getattr(settings, "per_job_log_cap_bytes", 20 * 1024 * 1024))
        self.log = JobLog(
            os.path.join(log_dir, "%s.jsonl" % self.job_id), cap,
            secrets=_known_secrets())
        try:
            self.log.open()
        except Exception as exc:
            self._evidence_failed = True
            try:
                self.log.close()
            except Exception:
                pass
            self.log = None
            raise OSError("job log unavailable: %s" % exc)

    def _verify_nonce(self):
        # type: () -> Tuple[bool, str]
        """H-01 replay protection: argv nonce must equal the claimed row.

        Checks expected_nonce == jobs.dispatch_nonce (non-empty) and
        state==preflight before any mutation or tool touch. No DB writes
        here; callers exit 6 without touching the tool on failure.
        """
        try:
            expected = self.expected_nonce or ""
        except Exception:
            expected = ""
        if not expected:
            return False, "missing dispatch nonce; refusing replay"
        try:
            row = dict(self.job or {})
        except Exception:
            return False, "job row unreadable; refusing replay"
        try:
            stored = row.get("dispatch_nonce", "") or ""
            state = row.get("state", "") or ""
        except Exception:
            return False, "job row unreadable; refusing replay"
        if not stored or stored != expected:
            return False, "dispatch nonce mismatch; refusing replay"
        if state != "preflight":
            return False, "job not in preflight (state=%s); refusing replay" % state
        return True, ""

    def _own_unit(self):
        # type: () -> str
        # Canonical full-UUID unit (R03); the DB row is authoritative.
        try:
            assert self.conn is not None
            row = self.conn.execute(
                "SELECT runner_unit, canonical_unit FROM jobs WHERE id=?",
                (self.job_id,)).fetchone()
            if row is not None:
                try:
                    if row["canonical_unit"]:
                        return str(row["canonical_unit"])
                except Exception:
                    pass
                try:
                    if row["runner_unit"]:
                        return str(row["runner_unit"])
                except Exception:
                    pass
        except Exception:
            pass
        try:
            from ..reconcile_core import canonical_unit
            return canonical_unit(self.job_id)
        except Exception:
            return "ega-update-job-%s.service" % (
                self.job_id or "").replace("-", "")

    def emit(self, stream, line):
        # type: (str, str) -> None
        if self.log is not None:
            self.log.emit(stream, line)

    def _event(self, text):
        # type: (str) -> None
        self.emit("event", text)

    def _set_state(self, state, step="", error_code="", error_detail="",
                   exit_code=None, after_version=None, extra=None):
        # type: (...) -> None
        """Guarded atomic transition (R05): expected prior state + attempt
        ownership enforced; recovery flag and versions move together.
        Never held across a subprocess. Raises TxError on guard/commit
        failure (callers map to blocked/interrupted explicitly)."""
        from ..tx import transition_tx

        assert self.conn is not None
        update = dict(extra or {})
        if exit_code is not None:
            update["exit_code"] = exit_code
        if after_version is not None:
            update["after_version"] = after_version
        if error_code:
            update["error_code"] = error_code
        if error_detail:
            update["error_detail"] = error_detail
        new_row = transition_tx(
            self.conn, self.job_id, state, step=step or state,
            expect_states=[self._known_state],
            expect_nonce=self.expected_nonce or "",
            update=update, event=state,
            event_detail=(error_code or step or "")[:500])
        self._known_state = str(new_row.get("state", state))
        try:
            self.conn.execute("UPDATE jobs SET heartbeat=? WHERE id=?",
                              (_now_iso(), self.job_id))
            self.conn.commit()
        except Exception:
            pass

    def _heartbeat(self):
        # type: () -> None
        try:
            assert self.conn is not None
            self.conn.execute("UPDATE jobs SET heartbeat=? WHERE id=?",
                              (_now_iso(), self.job_id))
            self.conn.commit()
        except Exception:
            pass

    # -- plan reconstruction ---------------------------------------------

    def _validate_env_release(self):
        # type: () -> Tuple[bool, str, str]
        """R02/R15 gate: required executables + plan release/config binding.

        No mutation, no tool touch. Returns (ok, error_code, detail).
        """
        assert self.plan_row is not None
        try:
            from ..owner_env import (resolve_release, resolved_paths,
                                     validate_executables)
            from ..inventory import config_identity
        except Exception as exc:
            return False, "unavailable", \
                "owner env contract unavailable: %s" % str(exc)[:200]
        try:
            paths = resolved_paths(self._settings())
        except Exception as exc:
            return False, "unavailable", \
                "owner paths unresolvable: %s" % str(exc)[:200]
        ok, missing = validate_executables(paths)
        if not ok:
            return False, "install_method_unsupported", \
                "required executables missing: %s" % ", ".join(missing)[:300]
        try:
            current_release = resolve_release()
        except ValueError as exc:
            return False, "unavailable", str(exc)[:300]
        self._release_path = current_release
        planned_release = str(self.plan_row.get("release_path", "") or "")
        if planned_release and planned_release != current_release:
            return False, "config_changed", \
                "console release changed since plan (%s -> %s); fresh plan" \
                " required" % (planned_release[-12:], current_release[-12:])
        try:
            current_hash = config_identity(self._settings())
        except Exception:
            current_hash = ""
        if not current_hash:
            return False, "unavailable", \
                "configuration identity unprovable"
        planned_hash = str(self.plan_row.get("config_hash", "") or "")
        if planned_hash and planned_hash != current_hash:
            return False, "config_changed", \
                "configuration changed since plan; fresh plan required"
        # N09: preview==apply environment parity, rechecked pre-mutation.
        try:
            from ..owner_env import canonical_fingerprint
            current_env = canonical_fingerprint(
                self._settings(), None, current_release)
        except Exception:
            current_env = ""
        if not current_env:
            return False, "unavailable", \
                "environment identity unprovable"
        planned_env = str(self.plan_row.get("env_fingerprint", "") or "")
        if planned_env and planned_env != current_env:
            return False, "config_changed", \
                "owner environment changed since plan (preview/apply " \
                "parity broken); fresh plan required"
        return True, "", ""

    def _build_plan(self):
        # type: () -> Any
        """Trusted server plan from the DB row, used WHOLLY (R15).

        Timeouts, budgets, steps, checks manifest, and scope come from the
        persisted immutable contract. Fresh probes may only INVALIDATE
        (preflight compares fingerprint/config); they never refill fields.
        """
        from ..adapters.base import PlanResult

        assert self.plan_row is not None

        def _loads(value, default):
            # type: (object, object) -> object
            try:
                if isinstance(value, str) and value:
                    parsed = json.loads(value)
                    return parsed
            except Exception:
                pass
            return default

        row = self.plan_row
        timeouts = _loads(row.get("deadlines_json"), {}) or {}
        if not isinstance(timeouts, dict):
            timeouts = {}
        try:
            from ..config import get_adapter_timeouts
            effective = get_adapter_timeouts(self._settings())
            for key, default in (("preflight", 120), ("backup", 600),
                                 ("updating", 1800), ("verifying", 300)):
                if key not in timeouts:
                    timeouts[key] = effective.get(key, default)
        except Exception:
            for key, default in (("preflight", 120), ("backup", 600),
                                 ("updating", 1800), ("verifying", 300)):
                timeouts.setdefault(key, default)
        services = _loads(row.get("services"), []) or []
        plan = PlanResult(
            tool=self.tool_id,
            target=row.get("target", ""),
            target_mode=row.get("target_mode", "exact"),
            channel=row.get("channel", ""),
            fingerprint=row.get("fingerprint", ""),
            services=list(services) if isinstance(services, list) else [],
            backup_scope=dict(_loads(row.get("backup_scope"), {}) or {}),
            required_space_bytes=int(
                row.get("required_space_bytes", 0) or 0),
            steps=list(_loads(row.get("steps_json"), []) or []),
            timeouts={str(k): int(v) for k, v in timeouts.items()},
            restart_impact=row.get("restart_impact", ""),
            already_current=False,
            install_identity=row.get("install_identity", ""),
            config_hash=row.get("config_hash", ""),
            launch=dict(_loads(row.get("launch_json"), {}) or {}),
            state_homes=list(_loads(row.get("state_homes_json"), []) or []),
            backup_policy=dict(
                _loads(row.get("backup_policy_json"), {}) or {}),
            required_probes=list(
                _loads(row.get("required_probes_json"), []) or []),
            required_checks=list(
                _loads(row.get("required_checks_json"), []) or []),
            budgets=dict(_loads(row.get("budgets_json"), {}) or {}),
            space_fs=dict(_loads(row.get("space_json"), {}) or {}),
            deadlines={str(k): int(v) for k, v in timeouts.items()},
            restart_detail=row.get("restart_detail", ""),
            activity_ts=row.get("activity_ts", ""),
            release_path=row.get("release_path", ""),
        )
        try:
            plan.scope_unit = ""
        except Exception:
            pass
        return plan

    def _space_need(self, plan, footprint, floor, reserve):
        # type: (Any, object, int, int) -> Tuple[int, List[str], str]
        """Grounded space budget (R28): measured footprint + plan budgets.

        Returns (need_bytes, fs_paths, unknown_reason). need =
        max(floor, estimate + reserve) applied on EVERY affected
        filesystem. Any unknown required component blocks with a reason.
        """
        fps = footprint if isinstance(footprint, dict) else {}
        try:
            budgets = dict(getattr(plan, "budgets", {}) or {})
        except Exception:
            budgets = {}
        try:
            space_fs = dict(getattr(plan, "space_fs", {}) or {})
        except Exception:
            space_fs = {}
        paths = []  # type: List[str]
        total = 0
        for source in (fps, budgets, space_fs):
            try:
                items = list(source.items())
            except Exception:
                continue
            for path, size in items:
                try:
                    size_i = int(size)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    return 0, [], \
                        "unmeasurable budget for %s" % str(path)[:120]
                if size_i < 0:
                    return 0, [], \
                        "unknown size blocks mutation: %s" % str(path)[:120]
                total += size_i
                if path and str(path) not in paths:
                    paths.append(str(path))
        try:
            planned = int(getattr(plan, "required_space_bytes", 0) or 0)
        except (TypeError, ValueError):
            planned = 0
        if planned > 0:
            total += planned
        # Zero total with no measured component is not an estimate: block
        # rather than hiding behind the floor (R28).
        if total <= 0:
            return 0, [], "no space estimate available"
        try:
            settings = self._settings()
            for extra in (getattr(settings, "backup_dir", ""),
                          getattr(settings, "log_dir", "")):
                if extra and str(extra) not in paths:
                    paths.append(str(extra))
        except Exception:
            pass
        try:
            need = max(int(floor), total + int(reserve))
        except (TypeError, ValueError):
            need = total
        return need, paths, ""

    # -- preflight ---------------------------------------------------------

    def _preflight(self, plan, bundle):
        # type: (Any, Dict[str, Any]) -> Tuple[bool, str, str]
        """Policy over a supervised collection bundle. No tool mutation.

        bundle (from the preflight phase worker): inspection/activity/
        footprint dicts. Fresh data may INVALIDATE the plan; it never
        refills plan fields. DB writes stay caller-side.
        """
        assert self.conn is not None and self.plan_row is not None
        bundle = bundle if isinstance(bundle, dict) else {}
        inspection = bundle.get("inspection", {})
        activity = bundle.get("activity", {})
        footprint = bundle.get("footprint", {})
        if not isinstance(inspection, dict):
            inspection = {}
        if not isinstance(activity, dict):
            activity = {}
        if not isinstance(footprint, dict):
            footprint = {}
        # 1. Plan freshness: stale plans require a fresh plan.
        try:
            expires = self.plan_row.get("expires_at", "")
            if expires and expires <= _now_iso():
                return False, "stale_plan", "server plan expired at %s" % expires
        except Exception:
            return False, "stale_plan", "plan expiry unreadable; fresh plan required"
        # 2. Adapter availability (disabled adapters never mutate).
        if self.adapter is None or not getattr(self.adapter, "enabled", True):
            return False, "install_method_unsupported", \
                "adapter for %s is disabled" % self.tool_id
        if bundle.get("__error__"):
            return False, "unavailable", \
                "preflight collection failed: %s" % str(
                    bundle.get("__error__"))[:300]
        if not inspection:
            return False, "unavailable", \
                "inspect probe failed: %s" % str(
                    bundle.get("inspection_error", "no data"))[:300]
        # 3. Fingerprint: changed installation needs a fresh plan.
        expected_fp = self.plan_row.get("fingerprint", "")
        fresh_fp = str(inspection.get("fingerprint", "") or "")
        if expected_fp and fresh_fp != expected_fp:
            return False, "fingerprint_changed", \
                "installation changed since plan; fresh plan required"
        self._last_inspection_fingerprint = fresh_fp
        # H-02: fresh preflight version persisted caller-side.
        try:
            fresh_before = str(inspection.get("version", "") or "")
            if fresh_before and fresh_before != self.before_version:
                self._fresh_before = fresh_before
        except Exception:
            pass
        # 4. Activity incl. recorded ack.
        ack = (self.job or {}).get("ack", "")
        activity_state = str(activity.get("state", "unknown") or "unknown")
        activity_evidence = str(activity.get("evidence", "") or "")
        if activity_state == "busy":
            return False, "activity_blocked", activity_evidence[:500]
        if activity_state == "unknown" and not ack:
            return False, "ack_required", \
                "unknown activity requires plan-specific owner ack: %s" \
                % activity_evidence[:400]
        # 5. Disk: plan budgets + MEASURED footprint on every affected
        # filesystem (R28). Fixed invented sizes never prove safety;
        # unknown estimates block. Reserve + floor apply at every gate.
        settings = self._settings()
        try:
            floor = int(getattr(settings, "disk_floor_bytes",
                                3 * 1024 * 1024 * 1024))
        except (TypeError, ValueError):
            floor = 3 * 1024 * 1024 * 1024
        try:
            reserve = int(getattr(settings, "reserve_bytes",
                                  1 * 1024 * 1024 * 1024))
        except (TypeError, ValueError):
            reserve = 1024 * 1024 * 1024
        if isinstance(footprint.get("__error__"), str) and \
                "__error__" in footprint and len(footprint) == 1:
            return False, "disk_blocked", \
                "footprint probe failed: %s" % str(
                    footprint.get("__error__"))[:300]
        need, per_fs, unknown = self._space_need(plan, footprint, floor,
                                                 reserve)
        if unknown:
            return False, "disk_blocked", \
                "unknown space estimate blocks mutation: %s" \
                % unknown[:300]
        try:
            from ..adapters.registry import check_disk
            disk_ok, disk_detail, _per = check_disk(per_fs, need)
        except Exception as exc:
            return False, "disk_blocked", "disk probe failed: %s" % str(exc)[:300]
        if not disk_ok:
            return False, "disk_blocked", disk_detail[:500]
        # 6. Source cleanliness (git tools; dirty/unreadable blocks).
        if self.tool_id == "hermes" and \
                str(inspection.get("source_clean", "")) != "clean":
            return False, "git_dirty", str(
                inspection.get("source_detail", ""))[:500]
        # 7. Install-method support.
        kind = str(inspection.get("install_kind", "") or "")
        if (not str(inspection.get("executable", "") or "")) or \
                kind in ("", "unknown"):
            return False, "install_method_unsupported", \
                "unsupported installation: kind=%s %s" % (
                    kind, str(inspection.get("source_detail", ""))[:200])
        # 8. Backup capability (read-only signals; the backup itself runs
        # next). Steps come from the immutable plan row, never a fresh
        # reconstruction.
        backup_dir = getattr(settings, "backup_dir", "/var/lib/ega-update/backups")
        if not os.path.isdir(backup_dir):
            try:
                os.makedirs(backup_dir, mode=0o700, exist_ok=True)
            except OSError as exc:
                return False, "backup_unsupported", \
                    "backup dir unavailable: %s" % exc
        if not os.access(backup_dir, os.W_OK):
            return False, "backup_unsupported", "backup dir not writable"
        try:
            steps = list((self.plan_row or {}).get("_steps_list", []) or [])
        except Exception:
            steps = []
        if not steps:
            try:
                import json as _json
                steps = list(_json.loads(
                    (self.plan_row or {}).get("steps_json", "[]") or "[]"))
            except Exception:
                steps = []
        if not steps:
            detail = str((self.plan_row or {}).get("restart_impact", "")
                         or "planning blocked")
            code = "backup_unsupported" if "backup" in detail.lower() \
                else "stale_plan"
            return False, code, detail[:500]
        if self.tool_id == "hermes" and "BLOCKED" in str(
                (self.plan_row or {}).get("restart_impact", "") or ""):
            return False, "install_method_unsupported", \
                str((self.plan_row or {}).get("restart_impact", ""))[:500]
        return True, "", ""

    # -- phases --------------------------------------------------------------

    # -- supervised phases (N10) ------------------------------------------

    def _plan_dump(self, plan):
        # type: (Any) -> Dict[str, Any]
        """PlanResult -> plain dict for the phase payload (extras kept)."""
        try:
            if hasattr(plan, "model_dump"):
                data = plan.model_dump()
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        try:
            return dict(plan or {})
        except Exception:
            return {}

    def _run_phase(self, phase, payload_extra, timeout_s, op=""):
        # type: (str, Dict[str, Any], float, str) -> Tuple[bool, Dict[str, Any], str, bool]
        """One supervised phase (N10, F03): worker process in an owned
        scope launched with the canonical contract env.

        Returns (ok, data, error, timed_out). Live stream lines flow to
        the log while the worker runs. No worker Python survives a
        declared timeout: it is an OS process in the killed cgroup.
        """
        from .phase_run import run_supervised_phase
        from ..owner_env import build_owner_contract, contract_env

        settings = self._settings()
        try:
            log_dir = str(getattr(settings, "log_dir",
                                  "/var/lib/ega-update/logs"))
        except Exception:
            log_dir = "/var/lib/ega-update/logs"
        try:
            env = contract_env(build_owner_contract(settings))
        except Exception as exc:
            return False, {}, "owner contract unbuildable: %s" % exc, \
                False
        try:
            out = run_supervised_phase(
                self.tool_id, self.job_id, phase,
                dict(payload_extra or {}), timeout_s, settings,
                log_dir, self._live_on_line, op=op,
                cancel_event=self._stop, env=env)
        except Exception as exc:
            return False, {}, "supervision failed: %s" % exc, False
        try:
            self._heartbeat()
        except Exception:
            pass
        try:
            if self.log is not None:
                self.log.flush()
        except Exception:
            pass
        return out

    def _do_backup(self, plan, timeout_s):
        # type: (Any, float) -> Tuple[bool, str, str]
        assert self.conn is not None
        ok, data, error, timed_out = self._run_phase(
            "backup", {"plan": self._plan_dump(plan),
                       "ack": bool((self.job or {}).get("ack", ""))},
            timeout_s)
        if timed_out:
            self._timed_out = True
            self._hard_timeout_recovery(timeout_s, "backup deadline")
            # Backup never completed: killed mid-backup leaves ambiguous
            # backup state -> interrupted with recovery (fail closed),
            # never a plain blocked that invites immediate retry.
            return False, "interrupted", \
                "backup timed out; recovery_required set"
        if not ok:
            if "interrupted" in (error or "") and self._stop.is_set():
                return False, "interrupted", "runner stopped during backup"
            return False, "backup_failed", \
                "backup failed: %s" % (error or "")[:400]
        data = data if isinstance(data, dict) else {}
        # F11: backup evidence is required job evidence. An undurable
        # backup record blocks (retryable: artifacts are job-scoped).
        if data.get("evidence_durable", False) is False:
            self._event("backup evidence durability failed")
            return False, "backup_failed", \
                "backup evidence durability failed"
        if not data.get("supported", False):
            reason = str(data.get("unsupported_reason", "")
                         or "backup unsupported")
            code = "backup_unsupported" if "backup_unsupported" in reason \
                else "backup_failed"
            if reason.startswith("backup_failed"):
                code = "backup_failed"
            return False, code, reason[:500]
        try:
            scope = data.get("scope", {})
            if not isinstance(scope, dict):
                scope = {}
            self.conn.execute(
                "INSERT OR REPLACE INTO backups(id,job_id,path,scope,consistency,"
                "size_bytes,completed_at) VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), self.job_id,
                 str(data.get("path", "") or ""),
                 json.dumps(scope),
                 str(data.get("consistency", "") or ""),
                 int(data.get("size_bytes", 0) or 0), _now_iso()))
            self.conn.commit()
        except Exception as exc:
            return False, "storage_failure", "backup record unwritable: %s" % str(exc)[:300]
        self.backup_summary = "%s (%s)" % (
            str(data.get("path", "") or ""),
            str(data.get("consistency", "") or ""))
        self._event("backup ok: %s" % self.backup_summary)
        return True, "", ""

    def _live_on_line(self, stream, line):
        # type: (str, str) -> None
        """R09 live sink: supervised worker lines land in the log while
        the phase process still runs (drained from its stream file)."""
        self.emit(stream, line)

    def _hard_timeout_recovery(self, timeout_s, reason):
        # type: (float, str, str) -> None
        """Post-timeout recovery (N10): the phase scope is already killed
        and proven empty by run_supervised_phase before this runs. Here:
        verify no execution-marked survivors, inspect delegated service
        states from the plan, run ONE bounded supervised recovery verify.
        The coordinator (this process, in the parent job unit) is never
        killed: it must persist the outcome. Callers terminalize with
        recovery_required whenever completion is not fully proved."""
        self._timed_out = True
        self._event("hard timeout (%s) after %ds; phase scope killed by"
                    " coordinator; verifying quiescence"
                    % (reason, int(timeout_s)))
        try:
            leftovers = _job_processes_alive(self.job_id)
            if leftovers:
                self._event("timeout verify: %d job processes still carry"
                            " job id; treating as unresolved"
                            % len(leftovers))
        except Exception:
            pass
        # Delegated service operations (plan services): bounded state read
        # so an uncertain service keeps recovery required explicitly.
        try:
            services = list((self.plan_row or {}).get("_services_list", [])
                            or [])
        except Exception:
            services = []
        if not services:
            try:
                import json as _json
                services = list(_json.loads(
                    (self.plan_row or {}).get("services", "[]") or "[]"))
            except Exception:
                services = []
        for svc in services[:10]:
            try:
                from ..executor import run_stream
                res = run_stream(
                    ["/usr/bin/systemctl", "--user", "show", str(svc),
                     "-p", "ActiveState,SubState"],
                    timeout_s=15, scope_unit=None)
                out = (res.stdout_tail or b"").decode(
                    "utf-8", errors="replace")[:200]
                self._event("delegated service %s: %s"
                            % (svc, out.replace("\n", " ")))
            except Exception:
                self._event("delegated service %s: state unreadable;"
                            " recovery stays required" % svc)
        # Bounded recovery checks in a fresh supervised scope: probe
        # installation without mutating.
        try:
            ok, data, _err, _to = self._supervised_verify(
                min(120.0, timeout_s / 4.0 or 120.0))
            if ok and isinstance(data, dict):
                self._record_checks_dict(data)
                if str(data.get("version", "") or ""):
                    self.after_version = str(data.get("version", ""))
        except Exception:
            pass
        self._event("timeout recovery: possible partial installation;"
                    " recovery_required set")

    def _supervised_verify(self, timeout_s):
        # type: (float) -> Tuple[bool, Dict[str, Any], str, bool]
        """Verify phase in a supervised worker (N10). Returns
        (ok, data, error, timed_out) with data as plain dicts."""
        try:
            required = list(self._required_checks(
                getattr(self, "_plan_obj", None)))
        except Exception:
            required = []
        try:
            plan_dump = self._plan_dump(getattr(self, "_plan_obj", None))
        except Exception:
            plan_dump = {}
        return self._run_phase(
            "verify", {"plan": plan_dump, "required_checks": required},
            timeout_s)

    def _do_execute(self, plan, timeout_s):
        # type: (Any, float) -> Any
        # Single timeout owner: the coordinator deadline is the plan step
        # timeout. The mutation runs in a supervised worker process inside
        # the updating scope cgroup, so reparented children stay killable
        # and no timed-out Python continues (N10). Adapter timeout reports
        # funnel into the same hard path.
        try:
            plan_dump = self._plan_dump(plan)
        except Exception:
            plan_dump = {}
        activity_ack = bool((self.job or {}).get("ack", ""))
        ok, data, error, timed_out = self._run_phase(
            "execute", {"plan": plan_dump, "ack": activity_ack},
            timeout_s)
        if not ok and self._stop.is_set() and not timed_out:
            # Signal path only (genuine timeouts set timed_out): the
            # supervised phase already killed its scope on cancel.
            return None
        if timed_out:
            if self._stop.is_set():
                # Signal coincided with (or caused) the deadline: report
                # interrupted, never timeout-success confusion. Recovery
                # is still required either way.
                return None
            self._timed_out = True
            self._hard_timeout_recovery(timeout_s, "coordinator deadline")
            return None
        if not ok:
            if self._stop.is_set():
                return None
            # Worker-level failure (no result): treat as interrupted with
            # recovery unless the payload proves otherwise.
            self._event("execute worker failed: %s" % (error or "")[:300])
            return {"state": "interrupted", "error_code": "interrupted",
                    "error_detail": "execute worker failed: %s"
                    % (error or "")[:300],
                    "exit_code": 6, "timed_out": False,
                    "before_version": "", "after_version": ""}
        data = data if isinstance(data, dict) else {}
        if data.get("timed_out") or \
                str(data.get("error_code", "") or "") == "timeout":
            self._timed_out = True
            self._hard_timeout_recovery(
                timeout_s, "adapter timeout report")
            return None
        # F11: a mutation whose evidence Stream failed cannot succeed —
        # without durable evidence the outcome is unprovable. Return an
        # interrupted outcome for run() to terminalize (single finish).
        if data.get("evidence_durable", False) is False:
            self._event("execute evidence durability failed; no success "
                        "without durable evidence")
            return {"state": "interrupted", "error_code": "interrupted",
                    "error_detail": "execute evidence durability failed",
                    "exit_code": 6, "timed_out": False,
                    "before_version": self.before_version,
                    "after_version": self.after_version}
        return data

    def _do_verify(self, timeout_s, required_checks=None):
        # type: (float, object) -> Any
        try:
            plan_dump = self._plan_dump(getattr(self, "_plan_obj", None))
        except Exception:
            plan_dump = {}
        try:
            required = list(required_checks or [])
        except Exception:
            required = []
        ok, data, error, timed_out = self._run_phase(
            "verify", {"plan": plan_dump, "required_checks": required},
            timeout_s)
        if timed_out:
            self._timed_out = True
            self._event("verify timed out after %ds" % int(timeout_s))
            return None
        if not ok:
            if self._stop.is_set():
                return None
            raise RuntimeError("verify worker failed: %s"
                               % (error or "")[:300])
        if not isinstance(data, dict):
            return None
        # F11: unverifiable evidence is missing verification (never
        # success without it; the health_failed path below applies).
        if data.get("evidence_durable", False) is False:
            self._event("verify evidence durability failed")
            return None
        return data

    def _secrets_for_evidence(self):
        # type: () -> tuple
        """Known secrets for evidence sanitization (never logged)."""
        try:
            if self.log is not None:
                return tuple(getattr(self.log, "_secrets", ()) or ())
        except Exception:
            pass
        return _known_secrets()

    def _sanitize_evidence_str(self, value, limit):
        # type: (str, int) -> str
        try:
            from ..sanitize import sanitize_text
            return sanitize_text(value or "", self._secrets_for_evidence())[:limit]
        except Exception:
            self._evidence_failed = True
            return "[redaction failed]"

    def _record_checks_dict(self, data):
        # type: (Dict[str, Any]) -> None
        """Persist supervised verify checks (plain dicts, R11/N12).

        Every name/summary is sanitized before SQLite persistence. A
        sanitizer failure sets the sticky evidence flag (fail closed).
        """
        try:
            from ..sanitize import sanitize_text
            _secrets = tuple(self._secrets_for_evidence())
        except Exception:
            self._evidence_failed = True
            return
        self.checks = []
        try:
            items = data.get("checks", []) or []
        except Exception:
            items = []
        if not isinstance(items, list):
            items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                name = sanitize_text(
                    str(item.get("name", "")), _secrets)[:200]
            except Exception:
                self._evidence_failed = True
                continue
            try:
                result = str(item.get("result", "unknown"))
            except Exception:
                result = "unknown"
            if result not in ("pass", "fail", "unknown",
                              "not_applicable"):
                result = "unknown"
            try:
                mandatory = bool(item.get("mandatory", True))
            except Exception:
                mandatory = True
            try:
                summary = sanitize_text(
                    str(item.get("summary", "")), _secrets)[:1000]
            except Exception:
                self._evidence_failed = True
                continue
            self.checks.append({
                "name": name, "result": result,
                "mandatory": mandatory, "summary": summary,
            })
        try:
            assert self.conn is not None
            from ..schemas import utcnow_iso
            now = utcnow_iso()
        except Exception:
            now = ""
        if not now:
            self._evidence_failed = True
            return
        try:
            assert self.conn is not None
            for entry in self.checks:
                self.conn.execute(
                    "INSERT INTO checks(tool_id,job_id,name,result,mandatory,summary,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (self.tool_id, self.job_id, entry["name"],
                     entry["result"], 1 if entry["mandatory"] else 0,
                     entry["summary"], now))
            self.conn.commit()
        except Exception as exc:
            self._evidence_failed = True
            self._event("check persistence failed: %s" % str(exc)[:300])

    def _record_checks(self, verify_result):
        # type: (Any) -> None
        """Legacy object-shaped entry; normalizes to dicts (N10)."""
        try:
            if hasattr(verify_result, "model_dump"):
                data = verify_result.model_dump()
            elif isinstance(verify_result, dict):
                data = verify_result
            else:
                data = {"checks": [
                    {"name": getattr(item, "name", ""),
                     "result": getattr(item, "result", "unknown"),
                     "mandatory": getattr(item, "mandatory", True),
                     "summary": getattr(item, "summary", "")}
                    for item in (getattr(verify_result, "checks", [])
                                 or [])]}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        self._record_checks_dict(data)

    # -- receipts + finalize ---------------------------------------------------

    def _receipt_path(self):
        # type: () -> str
        settings = self._settings()
        log_dir = getattr(settings, "log_dir", "/var/lib/ega-update/logs")
        return os.path.join(log_dir, "%s.receipt.json" % self.job_id)

    def _write_receipt(self, state, final_seq=-1, recovery_disposition=""):
        # type: (str, int, str) -> bool
        """Atomically persist the bound v2 completion receipt (R08).

        Built via receipts.build_receipt so ALL string values are
        sanitized first. Any write failure sets the sticky evidence flag.
        """
        try:
            from ..receipts import build_receipt
        except Exception as exc:
            self._evidence_failed = True
            self.error_detail = ("%s; receipt builder unavailable: %s" % (
                self.error_detail, exc))[:2000]
            return False
        try:
            plan_d = dict(self.plan_row or {})
        except Exception:
            plan_d = {}
        try:
            expected = list(self._required_checks(getattr(
                self, "_plan_obj", None)))
        except Exception:
            expected = []
        try:
            receipt = build_receipt(
                self.job_id, self.tool_id, state,
                self.before_version, self.after_version,
                self.exit_code, self.error_code, self.checks,
                _now_iso(),
                error_detail=self.error_detail or "",
                backup_summary=self.backup_summary or "",
                log_truncated=bool(self.log.truncated) if self.log else False,
                plan_id=str(plan_d.get("id", "") or ""),
                plan_hash=str(plan_d.get("plan_hash", "") or ""),
                attempt_nonce=self.expected_nonce or "",
                release_path=self._release_path or "",
                target=str(plan_d.get("target", "") or ""),
                target_mode=str(plan_d.get("target_mode", "exact")
                               or "exact"),
                expected_checks=expected,
                installer_exit=self.installer_exit,
                install_outcome=self.install_outcome or "",
                actual_change=self.actual_change,
                before_commit=self._before_commit or "",
                after_commit=self._after_commit or "",
                cleanup_status="resolved",
                recovery_disposition=recovery_disposition,
                evidence_durable=not self._evidence_failed_now(),
                backup_evidence={"summary": self.backup_summary or ""},
                final_health={"passed": self.verify_passed,
                              "version": self.after_version or ""},
            )
        except Exception as exc:
            self._evidence_failed = True
            self.error_detail = ("%s; receipt build failed: %s" % (
                self.error_detail, exc))[:2000]
            return False
        path = self._receipt_path()
        try:
            parent = os.path.dirname(path)
            if parent and not os.path.exists(parent):
                os.makedirs(parent, mode=0o700, exist_ok=True)
            tmp = "%s.tmp-%d" % (path, os.getpid())
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(receipt, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            try:
                dir_fd = os.open(parent, os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
            return True
        except OSError as exc:
            self._evidence_failed = True
            self.error_detail = ("%s; receipt unwritable: %s" % (
                self.error_detail, exc))[:2000]
            return False

    def _evidence_failed_now(self):
        # type: () -> bool
        try:
            if self._evidence_failed:
                return True
            if self.log is not None and getattr(
                    self.log, "persist_failed", False):
                return True
        except Exception:
            pass
        return False

    def _finish(self, state, exit_code, error_code="", error_detail="",
                recovery_required=False):
        # type: (str, int, str, str, bool) -> int
        """Durable terminal record (R05/R08/R18): receipt first, then ONE
        atomic transition (state, recovery, versions, outcome, checks,
        event, reservation release) via tx.transition_tx. Commit failure
        preserves the receipt and returns INTERRUPTED without pretending
        the gate is released. Tools observation is updated from the
        completed verification whatever the outcome (R18)."""
        from ..tx import TxError, transition_tx

        if state == "succeeded" and self._evidence_failed_now():
            state = "interrupted"
            exit_code = EXIT_INTERRUPTED
            error_code = "storage_failure"
            error_detail = ("evidence persistence failed; %s"
                            % (error_detail or ""))[:2000]
            recovery_required = True
        self.exit_code = exit_code
        self.error_code = error_code
        self.error_detail = self._sanitize_evidence_str(
            error_detail or "", 2000)
        try:
            expected_req = list(self._required_checks(
                getattr(self, "_plan_obj", None)))
        except Exception:
            expected_req = []
        if self.log is not None:
            self._event("final: state=%s exit=%d error=%s %s" % (
                state, exit_code, error_code,
                (self.error_detail or "")[:300]))
            self.log.flush()
            if state == "succeeded" and self._evidence_failed_now():
                state = "interrupted"
                exit_code = EXIT_INTERRUPTED
                error_code = "storage_failure"
                self.error_detail = ("evidence persistence failed; %s"
                                     % (error_detail or ""))[:2000]
                recovery_required = True
                self.exit_code = exit_code
                self.error_code = error_code
        # final_log_seq publishes the durable cursor (R20): last persisted
        # seq after the final flush, before the log is closed.
        final_seq = -1
        try:
            if self.log is not None:
                final_seq = int(self.log.seq) - 1
        except (TypeError, ValueError):
            final_seq = -1
        if self.log is not None:
            self.log.close()
        if recovery_required:
            recovery_disposition = "required"
        elif state in ("failed", "health_failed", "interrupted"):
            recovery_disposition = "clear-pending-reconcile"
        else:
            recovery_disposition = "none"
        receipt_ok = self._write_receipt(
            state, final_seq, recovery_disposition)
        if state == "succeeded" and not receipt_ok:
            state = "interrupted"
            self.exit_code = exit_code = EXIT_INTERRUPTED
            self.error_code = error_code = "storage_failure"
            recovery_required = True
            recovery_disposition = "required"
            self._write_receipt(state, final_seq, recovery_disposition)
        update = {
            "error_code": error_code,
            "error_detail": self.error_detail,
            "exit_code": exit_code,
            "installer_exit": int(self.installer_exit or 0),
            "install_outcome": str(self.install_outcome or "")[:200],
            "actual_change": 1 if self.actual_change else 0,
            "after_version": self.after_version or "",
            "final_log_seq": int(final_seq),
            "recovery_required": 1 if recovery_required else 0,
            "unresolved": 0,
        }
        try:
            assert self.conn is not None
            transition_tx(
                self.conn, self.job_id, state, step=state,
                expect_states=[self._known_state],
                expect_nonce=self.expected_nonce or "",
                update=update, event=state,
                event_detail=("%s %s" % (
                    error_code, recovery_disposition))[:500],
                checks=[], tool_id=self.tool_id)
            self._known_state = state
        except TxError as exc:
            # Receipt is already durable; reconciliation repairs the row.
            # Never pretend admission is safe: leave unresolved set.
            try:
                self._event("terminal DB write failed: %s" % str(exc)[:300])
            except Exception:
                pass
            try:
                assert self.conn is not None
                self.conn.rollback()
            except Exception:
                pass
            try:
                self._release_lock()
            except Exception:
                pass
            return EXIT_INTERRUPTED
        # R18: current tool observation from the completed verification,
        # whatever the outcome (never leave old green as current).
        try:
            self._record_observation(state)
        except Exception:
            pass
        try:
            self._release_lock()
        except Exception:
            pass
        return exit_code

    def _record_observation(self, terminal_state):
        # type: (str) -> None
        """Persist current health/version/fingerprint from verification.

        Runs after every completed verification — including installer
        failure where bounded verification ran. Independent short
        transaction; failures must not disturb the terminal record.
        """
        assert self.conn is not None
        health = "unknown"
        detail = ""
        if terminal_state == "succeeded" and self.verify_passed:
            health = "healthy"
            detail = "verified version=%s" % (self.after_version or "")
        elif terminal_state == "health_failed":
            health = "unhealthy"
            detail = "verification failed: %s" % (
                self.error_detail or "")[:500]
        elif terminal_state == "failed":
            # Installer failed: keep version evidence but mark health by
            # what verification actually proved (else stale green).
            health = "degraded" if self.checks else "unknown"
            detail = "installer failed; health %s" % health
        else:
            return  # blocked/interrupted: no new observation claimed
        now = _now_iso()
        try:
            fp = ""
            try:
                inspection_fp = getattr(
                    self, "_last_inspection_fingerprint", "")
                fp = str(inspection_fp or "")
            except Exception:
                fp = ""
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "INSERT OR IGNORE INTO tools(id) VALUES(?)",
                (self.tool_id,))
            if fp:
                self.conn.execute(
                    "UPDATE tools SET observed_version=?, health=?,"
                    " health_detail=?, observation_time=?, updated_at=?,"
                    " last_success_at=CASE WHEN ?='healthy' THEN ?"
                    " ELSE last_success_at END,"
                    " last_attempt_at=?, last_attempt_error=?,"
                    " fingerprint=? WHERE id=?",
                    (self.after_version or self.before_version, health,
                     self._sanitize_evidence_str(detail, 1000), now, now,
                     health, now, now,
                     "" if health == "healthy" else self.error_detail[:500],
                     fp, self.tool_id))
            else:
                self.conn.execute(
                    "UPDATE tools SET observed_version=?, health=?,"
                    " health_detail=?, observation_time=?, updated_at=?,"
                    " last_success_at=CASE WHEN ?='healthy' THEN ?"
                    " ELSE last_success_at END,"
                    " last_attempt_at=?, last_attempt_error=? WHERE id=?",
                    (self.after_version or self.before_version, health,
                     self._sanitize_evidence_str(detail, 1000), now, now,
                     health, now, now,
                     "" if health == "healthy" else self.error_detail[:500],
                     self.tool_id))
            self.conn.commit()
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass

    # -- main --------------------------------------------------------------------

    def run(self):
        # type: () -> int
        try:
            self._connect()
        except Exception as exc:
            return self._fail_early("storage_failure",
                                    "database unavailable: %s" % str(exc)[:300])
        try:
            ok, detail = self._load_rows()
        except Exception as exc:
            return self._fail_early("storage_failure",
                                    "job load failed: %s" % str(exc)[:300])
        if not ok:
            return self._fail_early("invalid_request", detail)
        # H-01: verify the dispatch nonce before any mutation or tool touch.
        # Mismatch/empty exits 6 without touching the tool (no adapter calls,
        # no log, no lock, no DB writes beyond the read above).
        try:
            nonce_ok, nonce_detail = self._verify_nonce()
        except Exception:
            nonce_ok, nonce_detail = False, "nonce verification crashed"
        if not nonce_ok:
            return self._fail_early("interrupted", nonce_detail)
        # R03: atomically CONSUME the one-shot claim before opening logs
        # or acquiring resources. A duplicate same-nonce runner gets
        # rowcount 0 here and exits 6 without touching anything.
        try:
            from ..jobs import consume_attempt
            assert self.conn is not None
            consumed = consume_attempt(
                self.conn, self.job_id, self.expected_nonce or "")
        except Exception:
            consumed = False
        if not consumed:
            return self._fail_early(
                "interrupted",
                "attempt already consumed or not ours; refusing replay")
        try:
            self._known_state = str(
                (self.job or {}).get("state", "preflight") or "preflight")
        except Exception:
            self._known_state = "preflight"
        # G05: JobLog initialization must establish redaction safety
        # BEFORE any mutation can occur. A secrets-unavailable log (or
        # an unwritable log dir) blocks here with a retryable blocked
        # outcome — no mutation has run yet, so no recovery is needed.
        try:
            self._open_log()
        except Exception as exc:
            return self._finish("blocked", EXIT_BLOCKED, "storage_failure",
                                "log initialization failed; mutation "
                                "refused without durable evidence: %s"
                                % str(exc)[:300])
        self._install_signal_handlers()
        # Runner owns the execution lock for the full procedure.
        locked, lock_detail = self._acquire_execution_lock()
        if not locked:
            try:
                self._set_state("blocked", step="preflight", error_code="busy",
                                error_detail=lock_detail[:500], exit_code=EXIT_BLOCKED)
            except Exception:
                pass
            self._event("blocked: %s" % lock_detail)
            if self.log is not None:
                self.log.close()
            self._release_lock()
            return EXIT_BLOCKED
        # Disabled adapters never mutate.
        try:
            from ..adapters.registry import get_adapter

            self.adapter = get_adapter(self.tool_id)
        except KeyError as exc:
            return self._finish("blocked", EXIT_BLOCKED, "invalid_request",
                                str(exc)[:500])
        if not getattr(self.adapter, "enabled", True):
            return self._finish("blocked", EXIT_BLOCKED,
                                "install_method_unsupported",
                                "adapter for %s is disabled" % self.tool_id)
        # R02/R15: canonical owner env + release/config binding BEFORE any
        # probe that could contend. Mismatch blocks without mutation.
        try:
            env_ok, env_code, env_detail = self._validate_env_release()
        except Exception as exc:
            env_ok, env_code, env_detail = (
                False, "unavailable", "env validation crashed: %s" % exc)
        if not env_ok:
            self._event("env blocked: %s %s" % (env_code, env_detail))
            return self._finish("blocked", EXIT_BLOCKED, env_code,
                                env_detail)
        self._event("runner start tool=%s job=%s" % (self.tool_id, self.job_id))

        # Preflight runs before any mutation; DB failure here blocks.
        try:
            self._set_state("preflight", step="preflight")
        except Exception as exc:
            return self._finish("blocked", EXIT_BLOCKED, "storage_failure",
                                "preflight record unwritable: %s" % str(exc)[:300])
        # Immutable plan contract first (R15): all phase budgets come
        # from the row; fresh probes below may only invalidate it.
        plan = self._build_plan()
        timeouts = dict(plan.timeouts or {})
        for key, default in (("preflight", 120), ("backup", 600),
                             ("updating", 1800), ("verifying", 300)):
            try:
                timeouts[key] = int(timeouts.get(key, default))
            except (TypeError, ValueError):
                timeouts[key] = default
        self._fresh_before = ""
        # Preflight collection runs in a supervised worker (N10) under
        # the plan preflight deadline; policy decides caller-side.
        _pre_ok, _pre_bundle, _pre_err, _pre_to = self._run_phase(
            "preflight", {"plan": self._plan_dump(plan)},
            float(timeouts["preflight"]))
        if _pre_to:
            if self._stop.is_set():
                return self._finish(
                    "interrupted", EXIT_INTERRUPTED, "interrupted",
                    "runner stopped in preflight", recovery_required=False)
            self._hard_timeout_recovery(
                float(timeouts.get("preflight", 120)),
                "preflight deadline")
            return self._finish("blocked", EXIT_BLOCKED, "timeout",
                                "preflight timed out; retry with fresh plan")
        if not _pre_ok:
            if self._stop.is_set():
                return self._finish(
                    "interrupted", EXIT_INTERRUPTED, "interrupted",
                    "runner stopped in preflight", recovery_required=False)
            return self._finish("blocked", EXIT_BLOCKED, "unavailable",
                                "preflight collection failed: %s"
                                % (_pre_err or "")[:300])
        if not isinstance(_pre_bundle, dict) or \
                _pre_bundle.get("evidence_durable", False) is False:
            # F11: untrusted preflight collection is unusable (blocked,
            # retryable: no mutation has occurred).
            return self._finish("blocked", EXIT_BLOCKED, "unavailable",
                                "preflight evidence durability failed")
        try:
            pre_ok, pre_code, pre_detail = self._preflight(
                plan, _pre_bundle)
        except Exception as exc:
            return self._finish("blocked", EXIT_BLOCKED, "unavailable",
                                "preflight policy crashed: %s" % str(exc)[:300])
        if not pre_ok:
            self._event("preflight blocked: %s %s" % (pre_code, pre_detail))
            return self._finish("blocked", EXIT_BLOCKED, pre_code, pre_detail)
        # Persist the fresh before-version (coordinator-side write; the
        # supervised collection worker never touches the database).
        try:
            if self._fresh_before and \
                    self._fresh_before != self.before_version:
                assert self.conn is not None
                self.conn.execute(
                    "UPDATE jobs SET before_version=? WHERE id=?",
                    (self._fresh_before, self.job_id))
                self.conn.commit()
                self.before_version = self._fresh_before
        except Exception as exc:
            return self._finish("blocked", EXIT_BLOCKED, "storage_failure",
                                "before_version record unwritable: %s"
                                % str(exc)[:300])
        if self._stop.is_set():
            return self._finish("interrupted", EXIT_INTERRUPTED, "interrupted",
                                "signal before mutation", recovery_required=True)

        # Backup phase: failure blocks before mutation. Bounded by the
        # plan backup deadline inside its own scope cgroup (R06).
        try:
            self._set_state("backup", step="backup")
        except Exception as exc:
            return self._finish("blocked", EXIT_BLOCKED, "storage_failure",
                                "backup record unwritable: %s" % str(exc)[:300])
        try:
            backup_ok, backup_code, backup_detail = self._do_backup(
                plan, float(timeouts["backup"]))
        except Exception as exc:
            return self._finish("blocked", EXIT_BLOCKED, "backup_failed",
                                "backup crashed: %s" % str(exc)[:300])
        if not backup_ok:
            self._event("backup blocked: %s %s" % (backup_code, backup_detail))
            if backup_code == "interrupted":
                return self._finish(
                    "interrupted", EXIT_INTERRUPTED, "interrupted",
                    backup_detail, recovery_required=True)
            return self._finish("blocked", EXIT_BLOCKED, backup_code, backup_detail)
        if self._stop.is_set():
            return self._finish("interrupted", EXIT_INTERRUPTED, "interrupted",
                                "signal after backup, before mutation",
                                recovery_required=True)

        # Updating phase: the only mutating step. Persist the unresolved
        # marker atomically with the state (R05) so a crash here always
        # reconciles as possible-mutation.
        try:
            self._set_state("updating", step="updating",
                            extra={"unresolved": 1})
        except Exception as exc:
            # Backup is done but no mutation ran; still preserve receipt and
            # require reconciliation because backup state is ambiguous.
            return self._finish("interrupted", EXIT_INTERRUPTED, "storage_failure",
                                "updating record unwritable: %s" % str(exc)[:300],
                                recovery_required=True)
        # R28: remeasure AFTER backup, immediately before mutation — the
        # backup consumed space, so the preflight budget must be re-held.
        try:
            re_ok, re_code, re_detail = self._recheck_space(plan)
        except Exception as exc:
            re_ok, re_code, re_detail = (
                False, "disk_blocked", "space recheck crashed: %s" % exc)
        if not re_ok:
            self._event("space recheck blocked: %s %s"
                        % (re_code, re_detail))
            return self._finish("blocked", EXIT_BLOCKED, re_code, re_detail)
        self._plan_obj = plan
        try:
            exec_result = self._do_execute(plan, float(timeouts["updating"]))
        except Exception as exc:
            self._event("execute crashed: %s" % traceback.format_exc(limit=3)[-800:])
            return self._finish("interrupted", EXIT_INTERRUPTED, "interrupted",
                                "execute crashed: %s" % str(exc)[:300],
                                recovery_required=True)
        if self._stop.is_set() and exec_result is None and not self._timed_out:
            # Signal during mutation: the supervised phase already killed
            # its scope on cancel; terminalize interrupted with recovery.
            return self._finish("interrupted", EXIT_INTERRUPTED, "interrupted",
                                "signal during mutation; phase scope killed",
                                recovery_required=True)
        if exec_result is None:
            if self._timed_out:
                return self._finish("failed", EXIT_INSTALL_FAILED, "timeout",
                                    "hard timeout during update; possible partial installation",
                                    recovery_required=True)
            return self._finish("interrupted", EXIT_INTERRUPTED, "interrupted",
                                "runner stopped during mutation",
                                recovery_required=True)
        if not isinstance(exec_result, dict):
            return self._finish("interrupted", EXIT_INTERRUPTED,
                                "interrupted",
                                "execute returned no result",
                                recovery_required=True)
        self.after_version = str(exec_result.get("after_version", "") or "")
        exec_state = str(exec_result.get("state", "") or "")
        exec_code = str(exec_result.get("error_code", "") or "")
        exec_detail = str(exec_result.get("error_detail", "") or "")
        try:
            self.installer_exit = int(exec_result.get("exit_code", 0) or 0)
        except (TypeError, ValueError):
            self.installer_exit = 0
        self.install_outcome = str(exec_state or "")[:200]
        self._event("execute done state=%s error=%s before=%s after=%s" % (
            exec_state, exec_code,
            str(exec_result.get("before_version", "") or ""),
            self.after_version))
        if exec_state == "blocked":
            code = exec_code or "install_method_unsupported"
            return self._finish("blocked", EXIT_BLOCKED, code, exec_detail[:1000])
        if exec_state == "already_current":
            # Idempotent no-op: still requires verification, never counted as
            # upgrade evidence (actual_change stays False).
            self.actual_change = False
            pass
        elif exec_state == "interrupted":
            # Worker-level failure (crash, supervision loss, evidence
            # failure): mutation state unknown -> interrupted WITH
            # recovery, never a plain install failure.
            return self._finish("interrupted", EXIT_INTERRUPTED,
                                exec_code or "interrupted",
                                (exec_detail or "execute interrupted")[:1000],
                                recovery_required=True)
        elif exec_state not in ("succeeded",):
            self._event("install failed; running bounded recovery checks")
            try:
                rec_ok, rec_data, _rec_err, _rec_to = self._run_phase(
                    "verify",
                    {"plan": self._plan_dump(plan),
                     "required_checks": self._required_checks(plan)},
                    min(120.0, float(timeouts["verifying"])))
                if rec_ok and isinstance(rec_data, dict):
                    self._record_checks_dict(rec_data)
            except Exception:
                pass
            return self._finish("failed", EXIT_INSTALL_FAILED,
                                exec_code or "install_failed",
                                (exec_detail or "installer failed")[:1000])
        else:
            self.actual_change = True

        # Verifying phase: zero exit plus mandatory failure is health_failed.
        try:
            self._set_state("verifying", step="verifying")
        except Exception as exc:
            return self._finish("interrupted", EXIT_INTERRUPTED, "storage_failure",
                                "verifying record unwritable: %s" % str(exc)[:300],
                                recovery_required=True)
        try:
            verify_result = self._do_verify(float(timeouts["verifying"]),
                                            self._required_checks(plan))
        except Exception as exc:
            return self._finish("interrupted", EXIT_INTERRUPTED, "interrupted",
                                "verify crashed: %s" % str(exc)[:300],
                                recovery_required=True)
        if verify_result is None:
            if self._stop.is_set():
                return self._finish("interrupted", EXIT_INTERRUPTED, "interrupted",
                                    "signal during verify", recovery_required=True)
            return self._finish("health_failed", EXIT_VERIFY_FAILED, "health_failed",
                                "required verification missing; never success without it")
        if not isinstance(verify_result, dict):
            return self._finish("health_failed", EXIT_VERIFY_FAILED,
                                "health_failed",
                                "verification result malformed")
        self._record_checks_dict(verify_result)
        if str(verify_result.get("version", "") or ""):
            self.after_version = str(verify_result.get("version", ""))
        try:
            raw_checks = verify_result.get("checks", []) or []
            if not isinstance(raw_checks, list):
                raw_checks = []
        except Exception:
            raw_checks = []
        mandatory_bad = [
            c for c in raw_checks if isinstance(c, dict)
            and c.get("mandatory", True) and c.get("result", "") != "pass"]
        missing_version = not str(verify_result.get("version", "") or "")
        if not verify_result.get("passed", False) or mandatory_bad or missing_version:
            detail = str(verify_result.get("error_detail", "") or "") or \
                "mandatory checks: %s" % ", ".join(
                    "%s=%s" % (c.get("name", "?"), c.get("result", "?"))
                    for c in mandatory_bad
                    if isinstance(c, dict))[:500]
            return self._finish("health_failed", EXIT_VERIFY_FAILED, "health_failed",
                                detail[:1000])
        # P0-06 central exact-target: exact plans must land exactly on target,
        # regardless of adapter opinion.
        try:
            target_mode = getattr(plan, "target_mode", "")
            target = getattr(plan, "target", "")
        except Exception:
            target_mode, target = "", ""
        if _exact_target_mismatch(target_mode, target, self.after_version):
            return self._finish(
                "health_failed", EXIT_VERIFY_FAILED, "exact_target_mismatch",
                "exact target %r not observed (after=%r)"
                % (target, self.after_version))
        self._event("job succeeded before=%s after=%s%s" % (
            self.before_version, self.after_version,
            " (already-current; not upgrade evidence)" if exec_state == "already_current" else ""))
        self.verify_passed = True
        return self._finish("succeeded", EXIT_OK, "", "")

    def _required_checks(self, plan):
        # type: (Any) -> List[str]
        """Plan-bound mandatory diagnostics manifest (R24)."""
        try:
            req = list(getattr(plan, "required_checks", []) or [])
            return [str(r) for r in req if str(r).strip()]
        except Exception:
            return []

    def _recheck_space(self, plan, timeout_s=120.0):
        # type: (Any, float) -> Tuple[bool, str, str]
        """R28: re-hold the space budget after backup consumed space.

        Footprint collection runs supervised (N10): filesystem walks must
        never execute unbounded in the coordinator.
        """
        try:
            settings = self._settings()
            floor = int(getattr(settings, "disk_floor_bytes",
                                3 * 1024 * 1024 * 1024))
            reserve = int(getattr(settings, "reserve_bytes",
                                  1 * 1024 * 1024 * 1024))
        except (TypeError, ValueError):
            floor, reserve = 3 * 1024 * 1024 * 1024, 1024 * 1024 * 1024
        try:
            ok, bundle, error, timed_out = self._run_phase(
                "preflight", {"plan": self._plan_dump(plan)},
                max(30.0, float(timeout_s or 120.0)))
        except Exception as exc:
            return False, "disk_blocked", \
                "footprint recheck failed: %s" % str(exc)[:300]
        if timed_out:
            self._hard_timeout_recovery(
                max(30.0, float(timeout_s or 120.0)),
                "space recheck deadline")
            return False, "disk_blocked", \
                "footprint recheck timed out; recovery_required set"
        if not ok or not isinstance(bundle, dict):
            return False, "disk_blocked", \
                "footprint recheck failed: %s" % (error or "")[:300]
        footprint = bundle.get("footprint", {})
        if not isinstance(footprint, dict):
            footprint = {}
        need, per_fs, unknown = self._space_need(plan, footprint, floor,
                                                 reserve)
        if unknown:
            return False, "disk_blocked", unknown[:500]
        try:
            from ..adapters.registry import check_disk
            disk_ok, disk_detail, _per = check_disk(per_fs, need)
        except Exception as exc:
            return False, "disk_blocked", \
                "disk recheck failed: %s" % str(exc)[:300]
        if not disk_ok:
            return False, "disk_blocked", disk_detail[:500]
        return True, "", ""

    def _fail_early(self, code, detail):
        # type: (str, str) -> int
        """No DB/log context yet: report on stderr, never mutate."""
        try:
            sys.stderr.write("runner: %s %s\n" % (code, detail))
        except OSError:
            pass
        return EXIT_INVALID if code == "invalid_request" else EXIT_INTERRUPTED

    def _install_signal_handlers(self):
        # type: () -> None
        def _handle(signum, _frame):
            # Cancellation flows to supervised phases via self._stop,
            # passed as cancel_event (no separate flag; single model).
            self._stop.set()
            try:
                self._event("signal %d received; no retry, no browser cancel" % signum)
            except Exception:
                pass

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handle)
            except (OSError, ValueError):
                continue


def _now_iso():
    # type: () -> str
    # +00:00 (not Z) so Python 3.10 fromisoformat parses everywhere and
    # lexicographic string-expiry comparisons match the stored format.
    try:
        from ..schemas import utcnow_iso

        return utcnow_iso()
    except Exception:
        return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write("usage: ega-update-runner <job-id> <nonce>\n")
        return EXIT_INVALID
    if len(args) != 2:
        sys.stderr.write("runner: invalid argv (want <job-id> <nonce>)\n")
        return EXIT_INVALID
    job_id, nonce = args[0], args[1]
    if len(job_id) < 8 or not nonce:
        sys.stderr.write("runner: invalid job id or empty nonce\n")
        return EXIT_INVALID
    runner = Runner(job_id, nonce)
    try:
        return runner.run()
    except Exception:
        try:
            traceback.print_exc()
        except OSError:
            pass
        try:
            runner._event("runner crashed: %s" % traceback.format_exc(limit=5)[-1000:])
            return runner._finish("interrupted", EXIT_INTERRUPTED, "interrupted",
                                  "runner crashed before completion proof",
                                  recovery_required=True)
        except Exception:
            return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
