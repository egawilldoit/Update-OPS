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
import subprocess
import sys
import threading
import time
import traceback
import uuid
from typing import Any, Dict, List, Optional, Tuple

TERMINATE_GRACE_S = 30

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_BLOCKED = 3
EXIT_INSTALL_FAILED = 4
EXIT_VERIFY_FAILED = 5
EXIT_INTERRUPTED = 6


# -- process tree termination (systemd-first + /proc-verify) ------------------
# The dispatcher launches each job as its own user-manager unit with
# KillMode=control-group, so systemd reaps the unit cgroup on exit. The
# runner additionally terminates its own tree directly (SIGTERM then SIGKILL)
# and, on hard timeout, best-effort ``systemctl --user stop/kill`` of its own
# recorded unit. Nothing is assumed dead: survivors are verified with a
# read-only /proc scan of the job process tree before recovery checks run.

def _descendant_pids(root):
    # type: (int) -> List[int]
    """All live descendants of root via /proc (read-only scan)."""
    try:
        entries = [e for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return []
    children = {}  # type: Dict[int, List[int]]
    for entry in entries:
        try:
            with open("/proc/%s/stat" % entry, "r") as fh:
                parts = fh.read().rsplit(")", 1)
            ppid = int(parts[1].split()[1])
            pid = int(entry)
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(pid)
    out = []
    stack = list(children.get(root, []))
    while stack:
        pid = stack.pop()
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def _signal_tree(sig):
    # type: (int) -> None
    for pid in _descendant_pids(os.getpid()):
        try:
            os.kill(pid, sig)
        except OSError:
            continue


def terminate_tree(grace_s=TERMINATE_GRACE_S):
    # type: (float) -> None
    """SIGTERM the job tree, then SIGKILL survivors after grace.

    Systemd-first: the runner unit uses KillMode=control-group so the unit
    cgroup is reaped on runner exit; this direct signalling covers native
    updater descendants promptly. Callers must verify with a /proc scan
    afterwards; killing only the shell is never treated as proof that
    mutation stopped.
    """
    _signal_tree(signal.SIGTERM)
    deadline = time.time() + max(1.0, float(grace_s))
    while time.time() < deadline:
        if not _descendant_pids(os.getpid()):
            return
        time.sleep(1.0)
    _signal_tree(signal.SIGKILL)


def _systemd_terminate_unit(unit):
    # type: (str) -> None
    """Best-effort ``systemctl --user stop/kill`` of the runner's own unit.

    Fixed argv, shell=False, bounded timeouts. Never raises; failures are
    reported by the later /proc verification, never assumed away.
    """
    if not unit:
        return
    for args in (["systemctl", "--user", "stop", unit],
                 ["systemctl", "--user", "kill", unit]):
        try:
            subprocess.run(
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=15, shell=False, check=False)
        except Exception:
            continue


def _no_live_descendants():
    # type: () -> bool
    """True when the /proc scan finds no live descendants of this process."""
    return not _descendant_pids(os.getpid())


def _job_processes_alive(job_id):
    # type: (str) -> list
    """Read-only /proc scan for processes still carrying the job id."""
    found = []
    short = (job_id or "")[:8]
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except Exception:
        return found
    for pid in pids:
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode(
                    "utf-8", errors="replace")
        except Exception:
            continue
        if (job_id and job_id in cmd) or (
                short and "ega-update" in cmd and short in cmd):
            found.append({"pid": int(pid), "cmdline": cmd[:300]})
    return found


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
    """Per-job ordered redacted JSONL log with cap + truncation marker."""

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
        try:
            from ..redaction import StreamRedactor

            self._redactors = {
                "stdout": StreamRedactor(secrets),
                "stderr": StreamRedactor(secrets),
                "event": StreamRedactor(secrets),
            }
        except Exception:
            self._redactors = {}

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
        """Redact and persist (or drain-only past the cap). Thread-safe."""
        if stream not in ("stdout", "stderr", "event"):
            stream = "stdout"
        with self._lock:
            if self._fh is None:
                return
            redactor = self._redactors.get(stream)
            lines = []
            if redactor is not None:
                try:
                    chunk = text if text.endswith("\n") else text + "\n"
                    lines = redactor.feed(chunk.encode("utf-8", errors="replace"))
                except Exception:
                    lines = []
            if not lines:
                try:
                    from ..redaction import redact_text, strip_controls

                    lines = [redact_text(strip_controls(text), self._secrets)]
                except Exception:
                    lines = [text[:8000]]
            for line in lines:
                self._write_record(stream, line)

    def flush(self):
        # type: () -> None
        with self._lock:
            if self._fh is not None:
                try:
                    tails = {}
                    for name, redactor in self._redactors.items():
                        try:
                            for line in redactor.flush():
                                tails.setdefault(name, []).append(line)
                        except Exception:
                            continue
                    for name, lines in tails.items():
                        for line in lines:
                            self._write_record(name, line)
                    self._fh.flush()
                except OSError:
                    self.persist_failed = True
                self._last_flush = time.time()

    def close(self):
        # type: () -> None
        try:
            self.flush()
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
        self.error_code = "interrupted"
        self.error_detail = ""
        self.checks = []  # type: List[Dict[str, Any]]
        self.backup_summary = ""
        self.verify_passed = False

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
        try:
            assert self.conn is not None
            row = self.conn.execute(
                "SELECT runner_unit FROM jobs WHERE id=?",
                (self.job_id,)).fetchone()
            if row is not None:
                return str(row["runner_unit"] or "")
        except Exception:
            pass
        return "ega-update-job-%s.service" % self.job_id[:8]

    def emit(self, stream, line):
        # type: (str, str) -> None
        if self.log is not None:
            self.log.emit(stream, line)

    def _event(self, text):
        # type: (str) -> None
        self.emit("event", text)

    def _set_state(self, state, step="", error_code="", error_detail="",
                   exit_code=None, after_version=None):
        # type: (...) -> None
        """Short DB transaction; never held across a subprocess."""
        from ..jobs import transition

        assert self.conn is not None
        transition(self.conn, self.job_id, state, step=step,
                   error_code=error_code, error_detail=error_detail,
                   exit_code=exit_code, after_version=after_version)
        self.conn.commit()
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

    def _build_plan(self, fresh):
        # type: (Any) -> Any
        """Trusted server plan from the DB row, with fresh timeouts/space."""
        from ..adapters.base import PlanResult

        assert self.plan_row is not None
        try:
            services = json.loads(self.plan_row.get("services") or "[]")
        except Exception:
            services = []
        try:
            backup_scope = json.loads(self.plan_row.get("backup_scope") or "{}")
        except Exception:
            backup_scope = {}
        timeouts = dict(getattr(fresh, "timeouts", {}) or {})
        required = int(getattr(fresh, "required_space_bytes", 0) or 0)
        return PlanResult(
            tool=self.tool_id,
            target=self.plan_row.get("target", ""),
            target_mode=self.plan_row.get("target_mode", "exact"),
            channel=self.plan_row.get("channel", ""),
            fingerprint=self.plan_row.get("fingerprint", ""),
            services=list(services) if isinstance(services, list) else [],
            backup_scope=dict(backup_scope) if isinstance(backup_scope, dict) else {},
            required_space_bytes=required,
            steps=list(getattr(fresh, "steps", []) or []),
            timeouts=timeouts,
            restart_impact=getattr(fresh, "restart_impact", ""),
            already_current=bool(getattr(fresh, "already_current", False)),
        )

    # -- preflight ---------------------------------------------------------

    def _preflight(self):
        # type: () -> Tuple[bool, str, str]
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
        # H-02: overwrite jobs.before_version with the fresh preflight
        # inspect version before any mutation (reservation-time observed
        # versions may be stale).
        try:
            fresh_before = getattr(inspection, "version", "") or ""
            if fresh_before and fresh_before != self.before_version:
                self.conn.execute(
                    "UPDATE jobs SET before_version=? WHERE id=?",
                    (fresh_before, self.job_id))
                self.conn.commit()
                self.before_version = fresh_before
        except Exception as exc:
            return False, "storage_failure", \
                "before_version record unwritable: %s" % str(exc)[:300]
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
        # 5. Disk floor on every affected filesystem.
        try:
            fresh = self.adapter.plan()
        except Exception as exc:
            return False, "unavailable", "plan probe failed: %s" % str(exc)[:300]
        settings = self._settings()
        floor = int(getattr(settings, "disk_floor_bytes", 3 * 1024 * 1024 * 1024))
        need = int(getattr(fresh, "required_space_bytes", 0) or 0)
        if need <= 0:
            return False, "disk_blocked", "unknown space estimate blocks mutation"
        need = max(floor, need)
        try:
            from ..adapters.registry import check_disk

            paths = list(getattr(inspection, "state_dirs", []) or [])
            if getattr(inspection, "executable", ""):
                paths.append(inspection.executable)
            paths.append(getattr(settings, "backup_dir", "/var/lib/ega-update/backups"))
            paths.append(getattr(settings, "log_dir", "/var/lib/ega-update/logs"))
            disk_ok, disk_detail, _per_fs = check_disk(paths, need)
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

    def _do_backup(self):
        # type: () -> Tuple[bool, str, str]
        assert self.conn is not None
        try:
            result = self.adapter.backup(self.job_id)
        except Exception as exc:
            return False, "backup_failed", "backup raised: %s" % str(exc)[:400]
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

    def _call_in_thread(self, fn, timeout_s):
        # type: (Any, float) -> Tuple[bool, Any, str]
        """Run fn in a thread with a hard deadline.

        Returns (finished, value_or_exc, status) where status is ok|timeout|
        error. On timeout the caller must terminate the job tree gracefully
        then forcibly after grace; this helper only reports the deadline.
        """
        box = {}  # type: Dict[str, Any]

        def _target():
            try:
                box["value"] = fn()
            except BaseException as exc:  # noqa: BLE001 - must surface adapter crashes
                box["error"] = exc

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        deadline = time.time() + max(1.0, float(timeout_s))
        while thread.is_alive():
            if self._stop.is_set():
                return False, None, "interrupted"
            remaining = deadline - time.time()
            if remaining <= 0:
                return False, None, "timeout"
            thread.join(min(1.0, remaining))
            self._heartbeat()
            if self.log is not None:
                self.log.flush()
        if "error" in box:
            return False, box["error"], "error"
        return True, box.get("value"), "ok"

    def _hard_timeout_recovery(self, timeout_s, reason):
        # type: (float, str) -> None
        """Single-owner hard-timeout path: terminate, systemd stop/kill own
        unit (best effort), /proc-verify no survivors, bounded read-only
        recovery checks. Callers terminalize failed/interrupted with
        recovery_required whenever completion is not fully proved."""
        self._timed_out = True
        self._event("hard timeout (%s) after %ds; SIGTERM job tree,"
                    " grace %ds then SIGKILL; best-effort systemctl --user"
                    " stop/kill own unit"
                    % (reason, int(timeout_s), TERMINATE_GRACE_S))
        try:
            terminate_tree(TERMINATE_GRACE_S)
        except Exception:
            pass
        try:
            _systemd_terminate_unit(self._own_unit())
        except Exception:
            pass
        try:
            if not _no_live_descendants():
                self._event("timeout verify: descendants alive after"
                            " terminate; treating as unresolved")
            leftovers = _job_processes_alive(self.job_id)
            if leftovers:
                self._event("timeout verify: %d job processes still carry"
                            " job id; treating as unresolved"
                            % len(leftovers))
        except Exception:
            pass
        # Bounded recovery checks: probe installation without mutating.
        try:
            recovery = self._call_in_thread(
                lambda: self.adapter.verify(),
                min(120.0, timeout_s / 4.0 or 120.0))
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
        # timeout. Adapters run their mutating run_fixed call with
        # step_timeout + registry.MUTATION_TIMEOUT_MARGIN_S so this deadline
        # always fires first; any adapter timeout report funnels here too.
        activity_ack = bool((self.job or {}).get("ack", ""))
        finished, value, status = self._call_in_thread(
            lambda: self.adapter.execute(plan, self.job_id, activity_ack=activity_ack),
            timeout_s)
        if status == "timeout":
            self._hard_timeout_recovery(timeout_s, "runner deadline")
            return None
        if status == "interrupted":
            return None
        if status == "error":
            raise value
        if _is_timeout_result(value):
            self._hard_timeout_recovery(timeout_s, "adapter timeout report")
            return None
        return value

    def _do_verify(self, timeout_s):
        # type: (float) -> Any
        finished, value, status = self._call_in_thread(
            lambda: self.adapter.verify(), timeout_s)
        if status == "timeout":
            self._event("verify timed out after %ds" % int(timeout_s))
            return None
        if status == "interrupted":
            return None
        if status == "error":
            raise value
        return value

    def _record_checks(self, verify_result):
        # type: (Any) -> None
        self.checks = []
        for item in getattr(verify_result, "checks", []) or []:
            entry = {
                "name": getattr(item, "name", ""),
                "result": getattr(item, "result", "unknown"),
                "mandatory": bool(getattr(item, "mandatory", True)),
                "summary": getattr(item, "summary", "")[:1000],
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

    def _write_receipt(self, state):
        # type: (str) -> bool
        """Atomically persist the redacted completion receipt. Never raw output.

        Built via receipts.build_receipt so ALL string values are redacted
        first (known secrets + patterns). Any write failure sets the sticky
        evidence flag.
        """
        try:
            from ..receipts import build_receipt
        except Exception as exc:
            self._evidence_failed = True
            self.error_detail = ("%s; receipt builder unavailable: %s" % (
                self.error_detail, exc))[:2000]
            return False
        try:
            receipt = build_receipt(
                self.job_id, self.tool_id, state,
                self.before_version, self.after_version,
                self.exit_code, self.error_code, self.checks,
                _now_iso(),
                error_detail=self.error_detail or "",
                backup_summary=self.backup_summary or "",
                log_truncated=bool(self.log.truncated) if self.log else False,
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
        """Durable terminal record: receipt first, then DB. Returns exit code.

        Any sticky evidence failure (JobLog/check/receipt writes) maps a
        would-be success to interrupted/storage_failure: never success
        without durable evidence.
        """
        from ..jobs import set_recovery

        if state == "succeeded" and self._evidence_failed_now():
            state = "interrupted"
            exit_code = EXIT_INTERRUPTED
            error_code = "storage_failure"
            error_detail = ("evidence persistence failed; %s"
                            % (error_detail or ""))[:2000]
            recovery_required = True
        self.exit_code = exit_code
        self.error_code = error_code
        self.error_detail = error_detail
        if self.log is not None:
            self._event("final: state=%s exit=%d error=%s %s" % (
                state, exit_code, error_code, (error_detail or "")[:300]))
            self.log.flush()
            if state == "succeeded" and self._evidence_failed_now():
                # Final flush failed: same mapping, before any receipt.
                state = "interrupted"
                exit_code = EXIT_INTERRUPTED
                error_code = "storage_failure"
                error_detail = ("evidence persistence failed; %s"
                                % (error_detail or ""))[:2000]
                recovery_required = True
                self.exit_code = exit_code
                self.error_code = error_code
                self.error_detail = error_detail
        receipt_ok = self._write_receipt(state)
        if state == "succeeded" and not receipt_ok:
            # Never success without a durable completion record.
            state = "interrupted"
            self.exit_code = exit_code = EXIT_INTERRUPTED
            self.error_code = error_code = "storage_failure"
            recovery_required = True
            self._write_receipt(state)
        try:
            assert self.conn is not None
            self._set_state(state, step=state, error_code=error_code,
                            error_detail=(error_detail or "")[:2000],
                            exit_code=exit_code,
                            after_version=self.after_version or None)
            if recovery_required:
                set_recovery(self.conn, self.job_id, True)
                self.conn.commit()
            # Best-effort tools observation (independent from job history).
            try:
                if self.after_version and state == "succeeded":
                    self.conn.execute(
                        "UPDATE tools SET observed_version=?, updated_at=? WHERE id=?",
                        (self.after_version, _now_iso(), self.tool_id))
                    self.conn.commit()
            except Exception:
                pass
        except Exception as exc:
            # DB write failure during execution: preserve receipt, require
            # reconciliation; a pre-mutation failure would already have blocked.
            self._event("terminal DB write failed: %s" % str(exc)[:300])
            self._write_receipt(state)
            try:
                assert self.conn is not None
                self.conn.rollback()
            except Exception:
                pass
            return EXIT_INTERRUPTED
        finally:
            if self.log is not None:
                self.log.close()
            self._release_lock()
        return exit_code

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
        try:
            self.adapter._emit = self.emit  # type: ignore[attr-defined]
        except Exception:
            pass
        self._event("runner start tool=%s job=%s" % (self.tool_id, self.job_id))

        # Preflight runs before any mutation; DB failure here blocks.
        try:
            self._set_state("preflight", step="preflight")
        except Exception as exc:
            return self._finish("blocked", EXIT_BLOCKED, "storage_failure",
                                "preflight record unwritable: %s" % str(exc)[:300])
        try:
            pre_ok, pre_code, pre_detail = self._preflight()
        except Exception as exc:
            return self._finish("blocked", EXIT_BLOCKED, "unavailable",
                                "preflight probe crashed: %s" % str(exc)[:300])
        if not pre_ok:
            self._event("preflight blocked: %s %s" % (pre_code, pre_detail))
            return self._finish("blocked", EXIT_BLOCKED, pre_code, pre_detail)
        if self._stop.is_set():
            return self._finish("interrupted", EXIT_INTERRUPTED, "interrupted",
                                "signal before mutation", recovery_required=True)

        # Fresh timeouts/space for the trusted DB plan.
        try:
            fresh = self.adapter.plan()
        except Exception as exc:
            return self._finish("blocked", EXIT_BLOCKED, "unavailable",
                                "plan probe failed: %s" % str(exc)[:300])
        plan = self._build_plan(fresh)
        timeouts = dict(plan.timeouts or {})
        for key, default in (("preflight", 120), ("backup", 600),
                             ("updating", 1800), ("verifying", 300)):
            try:
                timeouts[key] = int(timeouts.get(key, default))
            except (TypeError, ValueError):
                timeouts[key] = default

        # Backup phase: failure blocks before mutation.
        try:
            self._set_state("backup", step="backup")
        except Exception as exc:
            return self._finish("blocked", EXIT_BLOCKED, "storage_failure",
                                "backup record unwritable: %s" % str(exc)[:300])
        try:
            backup_ok, backup_code, backup_detail = self._do_backup()
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

        # Updating phase: the only mutating step.
        try:
            self._set_state("updating", step="updating")
        except Exception as exc:
            # Backup is done but no mutation ran; still preserve receipt and
            # require reconciliation because backup state is ambiguous.
            return self._finish("interrupted", EXIT_INTERRUPTED, "storage_failure",
                                "updating record unwritable: %s" % str(exc)[:300],
                                recovery_required=True)
        try:
            exec_result = self._do_execute(plan, float(timeouts["updating"]))
        except Exception as exc:
            self._event("execute crashed: %s" % traceback.format_exc(limit=3)[-800:])
            return self._finish("interrupted", EXIT_INTERRUPTED, "interrupted",
                                "execute crashed: %s" % str(exc)[:300],
                                recovery_required=True)
        if self._stop.is_set() and exec_result is None and not self._timed_out:
            terminate_tree(5)
            return self._finish("interrupted", EXIT_INTERRUPTED, "interrupted",
                                "signal during mutation; updater tree terminated",
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
        self._event("execute done state=%s error=%s before=%s after=%s" % (
            exec_state, exec_code, getattr(exec_result, "before_version", ""),
            self.after_version))
        if exec_state == "blocked":
            code = exec_code or "install_method_unsupported"
            return self._finish("blocked", EXIT_BLOCKED, code, exec_detail[:1000])
        if exec_state == "already_current":
            # Idempotent no-op: still requires verification, never counted as
            # upgrade evidence.
            pass
        elif exec_state not in ("succeeded",):
            self._event("install failed; running bounded recovery checks")
            try:
                recovery = self._do_verify(min(120.0, float(timeouts["verifying"])))
                if recovery is not None:
                    self._record_checks(recovery)
            except Exception:
                pass
            return self._finish("failed", EXIT_INSTALL_FAILED,
                                exec_code or "install_failed",
                                (exec_detail or "installer failed")[:1000])

        # Verifying phase: zero exit plus mandatory failure is health_failed.
        try:
            self._set_state("verifying", step="verifying")
        except Exception as exc:
            return self._finish("interrupted", EXIT_INTERRUPTED, "storage_failure",
                                "verifying record unwritable: %s" % str(exc)[:300],
                                recovery_required=True)
        try:
            verify_result = self._do_verify(float(timeouts["verifying"]))
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
        return self._finish("succeeded", EXIT_OK, "", "")

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
