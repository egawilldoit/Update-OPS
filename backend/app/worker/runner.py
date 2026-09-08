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
- Backup runs via adapter.backup; failure blocks before mutation.
- Execute runs via adapter.execute under the runner-owned hard deadline. Any
  timeout (thread deadline, ProcResult.timed_out, ExecuteResult.timed_out, or
  error_code=='timeout') funnels into the hard-timeout path: SIGTERM/SIGKILL
  the tree, best-effort ``systemctl --user stop/kill`` of the runner's own
  unit, /proc survivor verification, bounded read-only recovery checks, then
  terminal failed/interrupted + recovery_required whenever completion is not
  fully proved.
- Verify runs via adapter.verify. Installer nonzero exit yields failed even
  when the old version stays healthy. Zero exit plus mandatory check failure
  yields health_failed. Exact-target plans (target_mode=='exact') yield
  health_failed/exact_target_mismatch when after_version != plan target,
  regardless of adapter opinion. Missing required verification never yields
  success.
- stdout/stderr stream through redaction.StreamRedactor with per-job sequence
  numbers starting at 1, JSONL flush at least every second, and a 20 MiB/job
  cap (persisting stops but pipes keep draining, with the unchanged
  truncation marker). A partial final record after a crash is tolerated. A
  persisted redacted completion receipt (receipts.build_receipt) is required;
  success is never returned without durable evidence: JobLog, check, and
  receipt write failures set a sticky flag that finalize maps to
  interrupted/storage_failure.
- A DB write failure before mutation blocks. During execution the redacted
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
# Phase work runs inside per-phase scope units (executor phase_context);
# the runner (coordinator, in the parent job unit) kills scopes — never its
# own unit. Reparented children stay in the scope cgroup, so scope-kill +
# quiescence verify (units.query_unit) is proof; PPID scans are not used
# for termination (R06).


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
    object is always passed explicitly (never a bare call). Import-guarded
    with an empty fallback so there is no hard dependency.
    """
    try:
        from ..config import load_secret_values as _loader
        from ..config import settings as _settings

        values = _loader(_settings)
        return tuple(v for v in (values or ()) if v)
    except Exception:
        return ()


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
        self._cancel = threading.Event()
        self._release_path = ""
        self._before_commit = ""
        self._after_commit = ""
        # Live-line digest window for tail-summary dedup (R09).
        try:
            import collections as _collections
            self._seen_live = _collections.deque(maxlen=4000)
        except Exception:
            self._seen_live = []  # type: ignore[assignment]

    # -- setup ----------------------------------------------------------

    def _settings(self):
        # type: () -> Any
        from ..config import settings

        return settings

    def _connect(self):
        # type: () -> None
        from ..db import connect, migrate

        settings = self._settings()
        self.conn = connect(settings.db_path)
        migrate(self.conn)

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
        settings = self._settings()
        log_dir = getattr(settings, "log_dir", "/var/lib/ega-update/logs")
        cap = int(getattr(settings, "per_job_log_cap_bytes", 20 * 1024 * 1024))
        self.log = JobLog(
            os.path.join(log_dir, "%s.jsonl" % self.job_id), cap,
            secrets=_known_secrets())
        try:
            self.log.open()
        except Exception:
            self._evidence_failed = True
            try:
                self.log.close()
            except Exception:
                pass
            self.log = None

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

    def _preflight(self, plan):
        # type: (Any) -> Tuple[bool, str, str]
        """Return (ok, error_code, detail). No tool mutation here."""
        assert self.conn is not None and self.plan_row is not None
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
        try:
            inspection = self.adapter.inspect()
        except Exception as exc:
            return False, "unavailable", "inspect probe failed: %s" % str(exc)[:300]
        # 3. Fingerprint: changed installation needs a fresh plan.
        expected_fp = self.plan_row.get("fingerprint", "")
        if expected_fp and inspection.fingerprint != expected_fp:
            return False, "fingerprint_changed", \
                "installation changed since plan; fresh plan required"
        try:
            self._last_inspection_fingerprint = str(
                getattr(inspection, "fingerprint", "") or "")
        except Exception:
            self._last_inspection_fingerprint = ""
        # H-02: capture the fresh preflight inspect version for the
        # main thread to persist (this method may run under a phase
        # deadline thread; DB writes stay on the caller thread).
        try:
            fresh_before = getattr(inspection, "version", "") or ""
            if fresh_before and fresh_before != self.before_version:
                self._fresh_before = fresh_before
        except Exception:
            pass
        # 4. Activity incl. recorded ack.
        try:
            activity = self.adapter.activity()
        except Exception as exc:
            return False, "ack_required", "activity probe failed: %s" % str(exc)[:300]
        ack = (self.job or {}).get("ack", "")
        if activity.state == "busy":
            return False, "activity_blocked", activity.evidence[:500]
        if activity.state == "unknown" and not ack:
            return False, "ack_required", \
                "unknown activity requires plan-specific owner ack: %s" % activity.evidence[:400]
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
        try:
            footprint = self.adapter.measure_footprint(plan)
        except Exception as exc:
            return False, "disk_blocked", \
                "footprint probe failed: %s" % str(exc)[:300]
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
        if self.tool_id == "hermes" and inspection.source_clean != "clean":
            return False, "git_dirty", inspection.source_detail[:500]
        # 7. Install-method support.
        kind = getattr(inspection, "install_kind", "")
        if (not getattr(inspection, "executable", "")) or kind in ("", "unknown"):
            return False, "install_method_unsupported", \
                "unsupported installation: kind=%s %s" % (kind, inspection.source_detail[:200])
        # 8. Backup capability (read-only signals; the backup itself runs next).
        backup_dir = getattr(settings, "backup_dir", "/var/lib/ega-update/backups")
        if not os.path.isdir(backup_dir):
            try:
                os.makedirs(backup_dir, mode=0o700, exist_ok=True)
            except OSError as exc:
                return False, "backup_unsupported", \
                    "backup dir unavailable: %s" % exc
        if not os.access(backup_dir, os.W_OK):
            return False, "backup_unsupported", "backup dir not writable"
        if not getattr(fresh, "steps", None):
            detail = getattr(fresh, "restart_impact", "") or "planning blocked"
            code = "backup_unsupported" if "backup" in detail.lower() else "stale_plan"
            return False, code, detail[:500]
        if self.tool_id == "hermes" and "BLOCKED" in (getattr(fresh, "restart_impact", "") or ""):
            return False, "install_method_unsupported", \
                getattr(fresh, "restart_impact", "")[:500]
        return True, "", ""

    # -- phases --------------------------------------------------------------

    def _do_backup(self, timeout_s):
        # type: (float) -> Tuple[bool, str, str]
        assert self.conn is not None
        scope = self._scope_name("backup")
        finished, value, status = self._call_in_thread(
            lambda: self.adapter.backup(self.job_id), timeout_s,
            scope_name=scope, phase="backup")
        if status == "timeout":
            self._hard_timeout_recovery(timeout_s, "backup deadline", scope)
            return False, "timeout", \
                "backup timed out; recovery_required set"
        if status == "interrupted":
            return False, "interrupted", "runner stopped during backup"
        if status == "error":
            return False, "backup_failed", \
                "backup raised: %s" % str(value)[:400]
        result = value
        if not result.supported:
            reason = result.unsupported_reason or "backup unsupported"
            code = "backup_unsupported" if "backup_unsupported" in reason else "backup_failed"
            if reason.startswith("backup_failed"):
                code = "backup_failed"
            return False, code, reason[:500]
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO backups(id,job_id,path,scope,consistency,"
                "size_bytes,completed_at) VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), self.job_id, result.path,
                 json.dumps(result.scope), result.consistency,
                 int(result.size_bytes or 0), _now_iso()))
            self.conn.commit()
        except Exception as exc:
            return False, "storage_failure", "backup record unwritable: %s" % str(exc)[:300]
        self.backup_summary = "%s (%s)" % (result.path, result.consistency)
        self._event("backup ok: %s" % self.backup_summary)
        return True, "", ""

    def _scope_name(self, phase):
        # type: (str) -> str
        try:
            from ..reconcile_core import unit_hex
            stem = unit_hex(self.job_id)
        except Exception:
            stem = ""
        if not stem:
            return ""
        return "ega-update-job-%s-%s.scope" % (stem, phase)

    def _kill_scope(self, scope):
        # type: (str) -> None
        """Terminate a phase scope cgroup (coordinator survives: it runs
        in the parent job unit, never inside the killed scope)."""
        if not scope:
            return
        try:
            from ..executor import run_stream
        except Exception:
            return
        for sig in ("SIGTERM", "SIGKILL"):
            try:
                run_stream(["/usr/bin/systemctl", "--user", "kill",
                            "--kill-whom=all", "--signal=%s" % sig, scope],
                           timeout_s=15, scope_unit=None)
            except Exception:
                continue
            try:
                if self._scope_quiescent(scope):
                    return
            except Exception:
                continue
            time.sleep(2.0)

    def _scope_quiescent(self, scope):
        # type: (str) -> bool
        """True when the scope unit is confirmed stopped with no members."""
        try:
            from .. import units as _units
            info = _units.query_unit(scope, timeout_s=5)
            return str(info.get("state", "")) == "confirmed_stopped"
        except Exception:
            return False

    def _live_on_line(self, stream, line):
        # type: (str, str) -> None
        """R09 live sink: executor lines land in the log within ~1s while
        the process still runs. Records a content digest so the adapter's
        post-hoc tail summaries do not duplicate the same bytes."""
        try:
            import hashlib as _hashlib
            digest = _hashlib.sha1(
                line.encode("utf-8", errors="replace")).hexdigest()
            try:
                self._seen_live.append(digest)
            except Exception:
                pass
        except Exception:
            pass
        self.emit(stream, line)

    def _phase_sink(self, stream, text):
        # type: (str, str) -> None
        """Adapter _emit sink: drops bytes already streamed live (bounded
        tail summaries), keeps novel status lines. Content is never lost:
        dropped lines are byte-identical to lines already persisted."""
        try:
            import hashlib as _hashlib
            seen = set(self._seen_live)
        except Exception:
            seen = set()
            _hashlib = None  # type: ignore[assignment]
        try:
            parts = str(text or "").split("\n")
        except Exception:
            return
        for line in parts:
            try:
                digest = _hashlib.sha1(
                    line.encode("utf-8", errors="replace")).hexdigest() \
                    if _hashlib is not None else None
            except Exception:
                digest = None
            if digest is not None and digest in seen:
                continue
            self.emit(stream, line)

    def _call_in_thread(self, fn, timeout_s, scope_name="", phase=""):
        # type: (Any, float, str, str) -> Tuple[bool, Any, str]
        """Run fn in a thread with a real monotonic deadline (R06).

        The phase runs inside executor.phase_context(scope, cancel): every
        adapter subprocess inherits containment + cancellation, so a
        deadline stops WORK (not just reporting). On timeout the scope
        cgroup is killed and quiescence verified before recovery.
        Returns (finished, value_or_exc, status ok|timeout|error|
        interrupted).
        """
        from ..executor import phase_context

        box = {}  # type: Dict[str, Any]
        cancel = threading.Event()

        def _target():
            try:
                with phase_context(scope_name or None, cancel,
                                   self._live_on_line):
                    box["value"] = fn()
            except BaseException as exc:  # noqa: BLE001 - surface crashes
                box["error"] = exc

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        deadline = time.monotonic() + max(1.0, float(timeout_s))
        while thread.is_alive():
            if self._stop.is_set() or self._cancel.is_set():
                cancel.set()
                if scope_name:
                    self._kill_scope(scope_name)
                thread.join(timeout=30)
                return False, None, "interrupted"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                cancel.set()
                if scope_name:
                    self._kill_scope(scope_name)
                # Bounded join: the thread may linger only if adapter code
                # ignores cancellation between commands; all its
                # subprocesses are already terminated via scope+cancel.
                thread.join(timeout=30)
                return False, None, "timeout"
            thread.join(min(1.0, remaining))
            self._heartbeat()
            if self.log is not None:
                self.log.flush()
        if "error" in box:
            return False, box["error"], "error"
        return True, box.get("value"), "ok"

    def _hard_timeout_recovery(self, timeout_s, reason, scope_name=""):
        # type: (float, str, str) -> None
        """Runner-owned hard-timeout path (R06): the phase scope cgroup is
        already killed by _call_in_thread; here verify quiescence, inspect
        delegated service states from the plan, run bounded read-only
        recovery checks, and leave recovery_required set. The coordinator
        (this process, in the parent job unit) is never killed: it must
        persist the outcome. Callers terminalize failed/interrupted."""
        self._timed_out = True
        self._event("hard timeout (%s) after %ds; phase scope %s killed;"
                    " verifying quiescence"
                    % (reason, int(timeout_s), scope_name or "n/a"))
        try:
            if scope_name and not self._scope_quiescent(scope_name):
                self._event("timeout verify: scope %s not quiescent;"
                            " treating as unresolved" % scope_name)
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
        # Bounded recovery checks: probe installation without mutating.
        try:
            recovery = self._call_in_thread(
                lambda: self.adapter.verify(),
                min(120.0, timeout_s / 4.0 or 120.0),
                scope_name=self._scope_name("recover"),
                phase="recover")
            if recovery[0] and recovery[1] is not None:
                self._record_checks(recovery[1])
                self.after_version = getattr(
                    recovery[1], "version", "") or self.after_version
        except Exception:
            pass
        self._event("timeout recovery: possible partial installation;"
                    " recovery_required set")

    def _do_execute(self, plan, timeout_s):
        # type: (Any, float) -> Any
        # Single timeout owner: the runner deadline is the adapter plan step
        # timeout. Adapters run their mutating call with step_timeout +
        # registry.MUTATION_TIMEOUT_MARGIN_S so this deadline always fires
        # first; any adapter timeout report funnels here too. The mutation
        # runs inside the updating scope cgroup (plan.scope_unit) so
        # reparented children stay killable (R06).
        try:
            plan.scope_unit = self._scope_name("updating")
        except Exception:
            pass
        scope = self._scope_name("updating")
        activity_ack = bool((self.job or {}).get("ack", ""))
        finished, value, status = self._call_in_thread(
            lambda: self.adapter.execute(plan, self.job_id, activity_ack=activity_ack),
            timeout_s, scope_name=scope, phase="updating")
        if status == "timeout":
            self._hard_timeout_recovery(timeout_s, "runner deadline", scope)
            return None
        if status == "interrupted":
            return None
        if status == "error":
            raise value
        if _is_timeout_result(value):
            self._hard_timeout_recovery(timeout_s, "adapter timeout report",
                                        scope)
            return None
        return value

    def _do_verify(self, timeout_s, required_checks=None):
        # type: (float, object) -> Any
        scope = self._scope_name("verifying")
        finished, value, status = self._call_in_thread(
            lambda: self._adapter_verify(required_checks), timeout_s,
            scope_name=scope, phase="verifying")
        if status == "timeout":
            self._event("verify timed out after %ds" % int(timeout_s))
            return None
        if status == "interrupted":
            return None
        if status == "error":
            raise value
        return value

    def _adapter_verify(self, required_checks=None):
        # type: (object) -> Any
        """Verify with plan manifest when the adapter supports it (R15/R24:
        missing required diagnostics fail instead of silently dropping)."""
        try:
            return self.adapter.verify(
                plan=getattr(self, "_plan_obj", None),
                required_checks=list(required_checks or []))
        except TypeError:
            return self.adapter.verify()
        except Exception:
            raise

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

    def _record_checks(self, verify_result):
        # type: (Any) -> None
        # R11: every check summary is sanitized before SQLite persistence
        # (a daemon response reflected here must never persist credentials).
        try:
            from ..sanitize import sanitize_text
            _secrets = tuple(self._secrets_for_evidence())
        except Exception:
            sanitize_text = None  # type: ignore[assignment]
            _secrets = ()
        self.checks = []
        for item in getattr(verify_result, "checks", []) or []:
            try:
                name = str(getattr(item, "name", ""))[:200]
            except Exception:
                name = ""
            try:
                result = str(getattr(item, "result", "unknown"))
            except Exception:
                result = "unknown"
            if result not in ("pass", "fail", "unknown",
                              "not_applicable"):
                result = "unknown"
            try:
                mandatory = bool(getattr(item, "mandatory", True))
            except Exception:
                mandatory = True
            try:
                summary = str(getattr(item, "summary", ""))
            except Exception:
                summary = ""
            if sanitize_text is not None:
                try:
                    name = sanitize_text(name, _secrets)[:200]
                    summary = sanitize_text(summary, _secrets)[:1000]
                except Exception:
                    self._evidence_failed = True
                    name, summary = "[redaction failed]", ""
            else:
                self._evidence_failed = True
                name, summary = "[redaction failed]", ""
                continue
            entry = {
                "name": name,
                "result": result,
                "mandatory": mandatory,
                "summary": summary,
            }
            self.checks.append(entry)
        try:
            assert self.conn is not None
            for entry in self.checks:
                self.conn.execute(
                    "INSERT INTO checks(tool_id,job_id,name,result,mandatory,summary,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (self.tool_id, self.job_id, entry["name"], entry["result"],
                     1 if entry["mandatory"] else 0, entry["summary"], _now_iso()))
            self.conn.commit()
        except Exception as exc:
            self._evidence_failed = True
            self._event("check persistence failed: %s" % str(exc)[:300])

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
        self._open_log()
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
        try:
            # Adapter evidence sink: drops bytes already streamed live,
            # keeps novel status lines (R09, no content loss/duplication).
            self.adapter._emit = self._phase_sink  # type: ignore[attr-defined]
        except Exception:
            pass
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
        # Preflight itself runs under the plan preflight deadline in its
        # own scope (R06): every phase gets a real monotonic deadline.
        _pre_scope = self._scope_name("preflight")
        _finished, _pre, _pre_status = self._call_in_thread(
            lambda: self._preflight(plan), float(timeouts["preflight"]),
            scope_name=_pre_scope, phase="preflight")
        if _pre_status == "timeout":
            self._hard_timeout_recovery(
                float(timeouts.get("preflight", 120)),
                "preflight deadline", _pre_scope)
            return self._finish("blocked", EXIT_BLOCKED, "timeout",
                                "preflight timed out; retry with fresh plan")
        if _pre_status == "interrupted":
            return self._finish("interrupted", EXIT_INTERRUPTED,
                                "interrupted", "runner stopped in preflight",
                                recovery_required=False)
        if _pre_status == "error":
            return self._finish("blocked", EXIT_BLOCKED, "unavailable",
                                "preflight probe crashed: %s" % str(_pre)[:300])
        try:
            pre_ok, pre_code, pre_detail = _pre
        except Exception:
            return self._finish("blocked", EXIT_BLOCKED, "unavailable",
                                "preflight result unreadable")
        if not pre_ok:
            self._event("preflight blocked: %s %s" % (pre_code, pre_detail))
            return self._finish("blocked", EXIT_BLOCKED, pre_code, pre_detail)
        # Persist the fresh before-version on the caller thread.
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
                float(timeouts["backup"]))
        except Exception as exc:
            return self._finish("blocked", EXIT_BLOCKED, "backup_failed",
                                "backup crashed: %s" % str(exc)[:300])
        if not backup_ok:
            self._event("backup blocked: %s %s" % (backup_code, backup_detail))
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
            self._kill_scope(self._scope_name("updating"))
            return self._finish("interrupted", EXIT_INTERRUPTED, "interrupted",
                                "signal during mutation; updater scope killed",
                                recovery_required=True)
        if exec_result is None:
            if self._timed_out:
                return self._finish("failed", EXIT_INSTALL_FAILED, "timeout",
                                    "hard timeout during update; possible partial installation",
                                    recovery_required=True)
            return self._finish("interrupted", EXIT_INTERRUPTED, "interrupted",
                                "runner stopped during mutation",
                                recovery_required=True)
        self.after_version = getattr(exec_result, "after_version", "") or ""
        exec_state = getattr(exec_result, "state", "")
        exec_code = getattr(exec_result, "error_code", "")
        exec_detail = getattr(exec_result, "error_detail", "")
        try:
            self.installer_exit = int(
                getattr(exec_result, "exit_code", 0) or 0)
        except (TypeError, ValueError):
            self.installer_exit = 0
        self.install_outcome = str(exec_state or "")[:200]
        self._event("execute done state=%s error=%s before=%s after=%s" % (
            exec_state, exec_code, getattr(exec_result, "before_version", ""),
            self.after_version))
        if exec_state == "blocked":
            code = exec_code or "install_method_unsupported"
            return self._finish("blocked", EXIT_BLOCKED, code, exec_detail[:1000])
        if exec_state == "already_current":
            # Idempotent no-op: still requires verification, never counted as
            # upgrade evidence (actual_change stays False).
            self.actual_change = False
            pass
        elif exec_state not in ("succeeded",):
            self._event("install failed; running bounded recovery checks")
            try:
                recovery = self._do_verify(
                    min(120.0, float(timeouts["verifying"])),
                    self._required_checks(plan))
                if recovery is not None:
                    self._record_checks(recovery)
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
        self._record_checks(verify_result)
        self.after_version = getattr(verify_result, "version", "") or self.after_version
        mandatory_bad = [c for c in (getattr(verify_result, "checks", []) or [])
                         if getattr(c, "mandatory", True) and getattr(c, "result", "") != "pass"]
        missing_version = not (getattr(verify_result, "version", "") or "")
        if not getattr(verify_result, "passed", False) or mandatory_bad or missing_version:
            detail = getattr(verify_result, "error_detail", "") or \
                "mandatory checks: %s" % ", ".join(
                    "%s=%s" % (getattr(c, "name", "?"), getattr(c, "result", "?"))
                    for c in mandatory_bad)[:500]
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

    def _recheck_space(self, plan):
        # type: (Any) -> Tuple[bool, str, str]
        """R28: re-hold the space budget after backup consumed space."""
        try:
            settings = self._settings()
            floor = int(getattr(settings, "disk_floor_bytes",
                                3 * 1024 * 1024 * 1024))
            reserve = int(getattr(settings, "reserve_bytes",
                                  1 * 1024 * 1024 * 1024))
        except (TypeError, ValueError):
            floor, reserve = 3 * 1024 * 1024 * 1024, 1024 * 1024 * 1024
        try:
            footprint = self.adapter.measure_footprint(plan)
        except Exception as exc:
            return False, "disk_blocked", \
                "footprint recheck failed: %s" % str(exc)[:300]
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
            self._stop.set()
            try:
                self._cancel.set()
            except Exception:
                pass
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
