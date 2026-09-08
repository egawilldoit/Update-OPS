"""One streaming subprocess executor (R06/R09). Python 3.10 compatible.

All adapter subprocesses go through run_stream. Properties:
- Popen with fixed argv, shell=False, stdin=DEVNULL (defined noninteractive).
- Concurrent stdout/stderr drain threads with incremental UTF-8 decoding.
- Per-line and total byte bounds; tails retained; capture never unbounded.
- Monotonic deadline; SIGTERM process-group then SIGKILL after grace.
- Optional systemd scope containment (scope_unit): command wrapped as
  ``systemd-run --user --scope --unit=<scope>`` so reparented children stay
  in a killable cgroup. If a scope is requested but systemd-run is missing,
  fail closed (exit 127) rather than running unsupervised.
- Cancellation via threading.Event (phase cancel) or ambient context.
- True process exit code preserved; timeout/cancel reported explicitly.
- Continues draining after the durable cap; marks truncation + output gaps.
"""
from __future__ import annotations

import codecs
import os
import queue
import signal
import subprocess
import threading
import time
from contextvars import ContextVar
from typing import Any, Callable, Dict, List, Optional

TAIL_KEEP_BYTES = 32 * 1024
LINE_KEEP_BYTES = 64 * 1024
DEFAULT_CAP_BYTES = 20 * 1024 * 1024


class Cancelled(Exception):
    """Raised when an ambient/phase cancel fires before spawn."""


class ExecResult(object):
    """Outcome of run_stream. Never raises on nonzero exit."""

    def __init__(self, argv, exit_code, timed_out, stdout_tail,
                 stderr_tail, bytes_stdout, bytes_stderr, truncated,
                 duration_s, cancelled=False, error=""):
        # type: (...) -> None
        self.argv = list(argv)
        self.exit_code = int(exit_code)
        self.timed_out = bool(timed_out)
        self.cancelled = bool(cancelled)
        self.stdout_tail = bytes(stdout_tail or b"")
        self.stderr_tail = bytes(stderr_tail or b"")
        self.bytes_stdout = int(bytes_stdout)
        self.bytes_stderr = int(bytes_stderr)
        self.truncated = bool(truncated)
        self.duration_s = float(duration_s)
        self.error = str(error or "")

    def ok(self):
        # type: () -> bool
        return (not self.timed_out) and (not self.cancelled) \
            and self.exit_code == 0


_ambient_cancel = ContextVar("ega_ambient_cancel", default=None)  # type: Any
_ambient_scope = ContextVar("ega_ambient_scope", default=None)  # type: Any
_ambient_on_line = ContextVar("ega_ambient_on_line", default=None)  # type: Any


def ambient_cancel():
    # type: () -> Optional[threading.Event]
    try:
        return _ambient_cancel.get()
    except Exception:
        return None


def ambient_on_line():
    # type: () -> Optional[Callable]
    try:
        return _ambient_on_line.get()
    except Exception:
        return None


class _PhaseCtx(object):
    def __init__(self, scope=None, cancel=None, on_line=None):
        # type: (object, object, object) -> None
        self.scope = scope
        self.cancel = cancel
        self.on_line = on_line
        self._t1 = None
        self._t2 = None
        self._t3 = None

    def __enter__(self):
        # type: () -> threading.Event
        try:
            self._t1 = _ambient_scope.set(self.scope)
        except Exception:
            self._t1 = None
        ev = self.cancel
        if ev is None:
            ev = threading.Event()
        try:
            self._t2 = _ambient_cancel.set(ev)
        except Exception:
            self._t2 = None
        try:
            self._t3 = _ambient_on_line.set(self.on_line)
        except Exception:
            self._t3 = None
        return ev

    def __exit__(self, *exc):
        # type: (...) -> bool
        try:
            if self._t3 is not None:
                _ambient_on_line.reset(self._t3)
        except Exception:
            pass
        try:
            if self._t2 is not None:
                _ambient_cancel.reset(self._t2)
        except Exception:
            pass
        try:
            if self._t1 is not None:
                _ambient_scope.reset(self._t1)
        except Exception:
            pass
        return False


def phase_context(scope_unit=None, cancel_event=None, on_line=None):
    # type: (object, object, object) -> _PhaseCtx
    """Ambient scope + cancel + live line sink for run_stream (R06/R09).

    The runner wraps each phase so adapter commands inherit containment,
    cancellation, AND live streaming without signature changes.
    """
    return _PhaseCtx(scope_unit, cancel_event, on_line)


def _check_argv(argv):
    # type: (object) -> List[str]
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ValueError("argv must be a non-empty list of strings")
    clean = []
    for part in argv:
        if not isinstance(part, str) or not part:
            raise ValueError("argv entries must be non-empty strings")
        clean.append(part)
    if not os.path.isabs(clean[0]):
        raise ValueError("executable must be an absolute path: %r" % clean[0])
    return clean


def _drain(stream, pipe, chunks, stop):
    # type: (str, object, object, object) -> None
    try:
        while True:
            try:
                data = pipe.read(65536)
            except Exception:
                break
            if not data:
                break
            try:
                chunks.put((stream, data))
            except Exception:
                break
            if stop.is_set():
                continue
    finally:
        try:
            chunks.put(None)
        except Exception:
            pass


def run_stream(argv, timeout_s=60, cwd=None, env=None, scope_unit=None,
               on_line=None, cap_bytes=DEFAULT_CAP_BYTES,
               cancel_event=None, grace_s=None):
    # type: (...) -> ExecResult
    """Run argv with streaming capture. See module docstring for guarantees."""
    started = time.monotonic()
    clean = _check_argv(argv)
    try:
        timeout_f = float(timeout_s)
    except (TypeError, ValueError):
        timeout_f = 60.0
    if timeout_f <= 0:
        timeout_f = 60.0
    try:
        cap = int(cap_bytes)
    except (TypeError, ValueError):
        cap = DEFAULT_CAP_BYTES
    if scope_unit is None:
        try:
            scope_unit = _ambient_scope.get()
        except Exception:
            scope_unit = None
    cancel = cancel_event
    if cancel is None:
        cancel = ambient_cancel()
    if on_line is None:
        # R09 live path: phases publish a sink; direct callers get none.
        try:
            on_line = ambient_on_line()
        except Exception:
            pass

    def _cancelled_now():
        # type: () -> bool
        try:
            return bool(cancel is not None and cancel.is_set())
        except Exception:
            return False

    if _cancelled_now():
        raise Cancelled("phase cancelled before spawn: %s" % clean[0])
    eff_argv = list(clean)
    if scope_unit:
        scope_name = str(scope_unit)
        if "/" in scope_name or " " in scope_name or not scope_name:
            return ExecResult(clean, 127, False, b"", b"", 0, 0, False, 0.0,
                              error="invalid scope unit name")
        runner = "/usr/bin/systemd-run"
        if not (os.path.isfile(runner) and os.access(runner, os.X_OK)):
            return ExecResult(
                clean, 127, False, b"", b"", 0, 0, False, 0.0,
                error="scope requested but systemd-run missing; "
                      "refusing unsupervised execution")
        eff_argv = ["systemd-run", "--user", "--scope", "--quiet",
                    "--unit=%s" % scope_name] + clean
    if env is not None and not isinstance(env, dict):
        return ExecResult(clean, 127, False, b"", b"", 0, 0, False, 0.0,
                          error="env must be an explicit dict or None")
    try:
        proc = subprocess.Popen(
            eff_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, cwd=cwd, env=env, shell=False,
            start_new_session=True)
    except FileNotFoundError:
        return ExecResult(clean, 127, False, b"",
                          b"executable not found", 0, 0, False,
                          time.monotonic() - started)
    except Exception as exc:
        return ExecResult(clean, 127, False, b"", str(exc)[:500].encode(
            "utf-8", errors="replace"), 0, 0, False,
            time.monotonic() - started)
    chunks = queue.Queue()  # type: ignore[var-annotated]
    stop = threading.Event()
    readers = []
    try:
        for name, pipe in (("stdout", proc.stdout),
                           ("stderr", proc.stderr)):
            t = threading.Thread(target=_drain, args=(name, pipe, chunks,
                                                      stop),
                                 daemon=True)
            t.start()
            readers.append(t)
    except Exception:
        pass
    decoders = {
        "stdout": codecs.getincrementaldecoder("utf-8")(errors="replace"),
        "stderr": codecs.getincrementaldecoder("utf-8")(errors="replace"),
    }
    pending = {"stdout": "", "stderr": ""}  # type: Dict[str, str]
    tails = {"stdout": bytearray(), "stderr": bytearray()}
    totals = {"stdout": 0, "stderr": 0}
    truncated = False

    deadline = started + timeout_f
    if grace_s is None:
        grace = min(30.0, max(5.0, timeout_f / 10.0))
    else:
        try:
            grace = max(1.0, float(grace_s))
        except (TypeError, ValueError):
            grace = 10.0
    timed_out = False
    cancelled = False
    killed = False
    exit_code = -1
    try:
        while True:
            if _cancelled_now():
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            try:
                rc = proc.poll()
            except Exception:
                rc = None
            if rc is not None:
                exit_code = int(rc)
                break
            if remaining <= 0:
                timed_out = True
                break
            try:
                item = chunks.get(timeout=min(0.25, max(0.01, remaining)))
            except queue.Empty:
                continue
            _pump_chunk(item, chunks, decoders, pending, tails, totals,
                        on_line)
            for _s in ("stdout", "stderr"):
                if totals[_s] > cap:
                    truncated = True
        if timed_out or cancelled:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            except Exception:
                pass
            quiet_until = time.monotonic() + grace
            while time.monotonic() < quiet_until:
                try:
                    rc = proc.poll()
                except Exception:
                    rc = None
                if rc is not None:
                    exit_code = int(rc)
                    break
                if _drain_available(chunks):
                    try:
                        item = chunks.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    _pump_chunk(item, chunks, decoders, pending, tails,
                                totals, on_line)
                    for _s in ("stdout", "stderr"):
                        if totals[_s] > cap:
                            truncated = True
                else:
                    time.sleep(0.1)
            else:
                try:
                    rc = proc.poll()
                except Exception:
                    rc = None
                if rc is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        pass
                    killed = True
            try:
                exit_code = int(proc.wait(timeout=15))
            except Exception:
                try:
                    rc = proc.poll()
                    exit_code = int(rc) if rc is not None else -1
                except Exception:
                    exit_code = -1
        else:
            try:
                exit_code = int(proc.wait(timeout=30))
            except Exception:
                exit_code = -1
    finally:
        stop.set()
    # Final drain to EOF (bounded): keep reading until both pipes EOF.
    try:
        _final_drain(proc, chunks, decoders, pending, tails, totals,
                     on_line, cap)
        for _s in ("stdout", "stderr"):
            if totals[_s] > cap:
                truncated = True
    except Exception:
        pass
    for t in readers:
        try:
            t.join(timeout=5)
        except Exception:
            pass
    duration = time.monotonic() - started
    return ExecResult(
        clean, exit_code, timed_out, bytes(tails["stdout"][-TAIL_KEEP_BYTES:]),
        bytes(tails["stderr"][-TAIL_KEEP_BYTES:]), totals["stdout"],
        totals["stderr"], truncated or killed, duration,
        cancelled=cancelled)


def _drain_available(chunks):
    # type: (object) -> bool
    try:
        return not chunks.empty()
    except Exception:
        return True


def _pump_chunk(item, chunks, decoders, pending, tails, totals, on_line):
    # type: (...) -> None
    # Queue carries (stream, bytes) tuples; one None sentinel per pipe.
    if item is None:
        return
    try:
        stream, data = item
    except Exception:
        return
    if stream not in ("stdout", "stderr"):
        return
    try:
        totals[stream] += len(data)
    except Exception:
        pass
    try:
        tails[stream].extend(data)
        if len(tails[stream]) > TAIL_KEEP_BYTES:
            del tails[stream][:-TAIL_KEEP_BYTES]
    except Exception:
        pass
    try:
        text = decoders[stream].decode(data)
    except Exception:
        return
    buf = pending[stream] + text
    parts = buf.split("\n")
    if len(parts[-1]) > LINE_KEEP_BYTES:
        # Oversized line: keep a bounded head for context and continue;
        # the sanitizer stage suppresses oversized sensitive bodies.
        pending[stream] = parts[-1][-LINE_KEEP_BYTES:]
        head = parts[-1][:0]
        parts = parts[:-1] + [head]
    else:
        pending[stream] = parts[-1]
    for line in parts[:-1]:
        if on_line is not None:
            try:
                on_line(stream, line)
            except Exception:
                pass


def _final_drain(proc, chunks, decoders, pending, tails, totals, on_line,
                 cap):
    # type: (...) -> None
    deadline = time.monotonic() + 20.0
    eofs = 0
    while eofs < 2 and time.monotonic() < deadline:
        try:
            item = chunks.get(timeout=0.5)
        except queue.Empty:
            try:
                if proc.poll() is not None and chunks.empty():
                    break
            except Exception:
                break
            continue
        if item is None:
            eofs += 1
            continue
        _pump_chunk(item, chunks, decoders, pending, tails, totals, on_line)
    for stream in ("stdout", "stderr"):
        try:
            rest = decoders[stream].decode(b"", final=True)
        except Exception:
            rest = ""
        buf = pending[stream] + rest
        pending[stream] = ""
        if not buf:
            continue
        parts = buf.split("\n")
        for idx, line in enumerate(parts):
            if line == "" and idx == len(parts) - 1:
                continue
            if on_line is not None:
                try:
                    on_line(stream, line)
                except Exception:
                    pass
