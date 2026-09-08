"""Supervised phase worker process (N10). Python 3.10 compatible.

Two halves:
- Worker (this module's main): runs ONE phase body in a coordinator-owned
  unit. Never touches the database.
- Coordinator (run_supervised_phase / run_supervised_probe, same module
  for single ownership): launches update phases inside per-phase
  transient scopes under the job runner, and authoritative probes
  inside transient probe SERVICES with the same NNP-off owner profile
  as the runner (H04 — never inherited scopes from the NNP-on
  dispatcher), tails sanitized stream live, enforces a monotonic
  deadline by killing the UNIT (never its own unit), proves
  quiescence, and returns the result.
  No phase Python outlives coordinator-declared termination: the worker
  is an OS process inside the killed cgroup, not a thread.
"""
Invoked ONLY by the coordinator inside a coordinator-owned scope:

  <venv-python> -m backend.app.worker.phase_run <job-id> <phase>
      --payload <json> --result <json> --stream <jsonl> [--op <op>]
      [--deadline-s <seconds>]

Phases: probe | preflight | backup | execute | verify.

The worker NEVER touches the database (no db import): it reconstructs
the adapter + plan from the payload, performs exactly one phase body,
appends sanitized stream lines for live tailing, and writes ONE atomic
result file. All persistence decisions stay coordinator-side.

Timeouts: the coordinator enforces the phase deadline by killing the
scope; the worker additionally arms a backstop alarm
(deadline+300s) so no worker outlives its coordinator's patience.
Secrets: loaded from the worker's own settings (same config file);
stream/result content is sanitized before writing.

Exits: 0 ok (result file authoritative), 2 invalid payload,
6 interrupted (signal; partial stream retained, no result guaranteed).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional

PHASES = ("probe", "preflight", "backup", "execute", "verify")

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_INTERRUPTED = 6

_interrupted = {"flag": False}


def _on_signal(signum, _frame):
    _interrupted["flag"] = True


def _utcnow():
    # type: () -> str
    try:
        from ..schemas import utcnow_iso
        return utcnow_iso()
    except Exception:
        import datetime
        return datetime.datetime.now(
            datetime.timezone.utc).isoformat()


class _StreamWriter(object):
    """Append-only sanitized stream file for coordinator tailing (F11).

    Sticky evidence_failed + evidence_failure_reason: sanitizer
    initialization/feed failure, log write failure, and flush failure
    are RECORDED, never silently ignored. The final phase result
    carries evidence_durable so the coordinator can refuse success.
    """

    def __init__(self, path, secrets, secrets_ok=True):
        # type: (str, tuple, bool) -> None
        self.path = path
        self._secrets = tuple(secrets or ())
        self.evidence_failed = False
        self.evidence_failure_reason = ""
        if not secrets_ok:
            self._fail("secret source unavailable")
        self._streams = {}
        try:
            from ..sanitize import SanitizingStream
            for name in ("stdout", "stderr", "event"):
                self._streams[name] = SanitizingStream(
                    tuple(secrets or ()))
        except Exception as exc:
            self._streams = {}
            self._fail("sanitizer initialization failed: %s" % exc)

    def _fail(self, reason):
        # type: (str) -> None
        if not self.evidence_failed:
            self.evidence_failed = True
            self.evidence_failure_reason = str(reason or "")[:300]

    def emit(self, stream, text):
        # type: (str, str) -> None
        if self.evidence_failed:
            return
        if stream not in ("stdout", "stderr", "event"):
            stream = "stdout"
        target = self._streams.get(stream)
        if target is None:
            self._fail("no sanitizer for stream %s" % stream)
            return
        try:
            chunk = text if text.endswith("\n") else text + "\n"
            lines = target.feed(chunk.encode("utf-8", errors="replace"))
        except Exception as exc:
            self._fail("sanitizer feed failed: %s" % exc)
            return
        for line in lines:
            self._write(stream, line)

    def _write(self, stream, line):
        # type: (str, str) -> None
        try:
            record = {"ts": _utcnow(), "stream": stream, "line": line}
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            self._fail("stream file write failed: %s" % exc)
        except Exception as exc:
            self._fail("stream record failed: %s" % exc)

    def flush_final(self):
        # type: () -> None
        for name, target in self._streams.items():
            try:
                lines = target.flush_final()
            except Exception as exc:
                self._fail("final flush failed: %s" % exc)
                continue
            for line in lines:
                self._write(name, line)


def _load_settings():
    # type: () -> object
    from ..config import load_settings
    return load_settings()


def _adapter(tool_id):
    # type: (str) -> object
    from ..adapters import registry as _registry
    return _registry.get_adapter(tool_id)


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


def _build_plan(plan_dict):
    # type: (object) -> object
    from ..adapters.base import PlanResult
    if isinstance(plan_dict, dict):
        try:
            return PlanResult(**{k: v for k, v in plan_dict.items()
                                 if isinstance(k, str)})
        except Exception as exc:
            raise ValueError("plan unbuildable: %s" % exc)
    raise ValueError("plan payload missing")


def _run_probe(adapter, op):
    # type: (object, str) -> Dict[str, Any]
    if op == "inspect":
        return _dump(adapter.inspect())
    if op == "discover":
        return _dump(adapter.discover())
    if op == "activity":
        return _dump(adapter.activity())
    if op == "verify":
        return _dump(adapter.verify())
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
        return {"planned": _dump(planned),
                "activity": _dump(activity) if activity else {
                    "state": "unknown", "evidence": "activity failed",
                    "checked_at": _utcnow()},
                "inspection": _dump(inspection) if inspection else {}}
    if op == "refresh":
        inspection = adapter.inspect()
        out = {"inspection": _dump(inspection)}  # type: Dict[str, Any]
        try:
            out["discovery"] = _dump(adapter.discover())
        except Exception as exc:
            out["discovery"] = {
                "available": False,
                "unknown_reason": "discover failed: %s" % str(exc)[:300]}
        try:
            out["activity"] = _dump(adapter.activity())
        except Exception:
            out["activity"] = {"state": "unknown",
                               "evidence": "activity failed",
                               "checked_at": _utcnow()}
        try:
            out["verification"] = _dump(adapter.verify())
        except Exception as exc:
            out["verification"] = {
                "passed": False, "version": "",
                "error_detail": "verify failed: %s" % str(exc)[:300],
                "checks": []}
        return out
    raise ValueError("unknown probe op: %s" % op)


def _run_preflight(adapter, plan):
    # type: (object, object) -> Dict[str, Any]
    """Data collection only; policy decisions stay coordinator-side."""
    inspection = adapter.inspect()
    try:
        activity = adapter.activity()
    except Exception as exc:
        activity = None
        activity_error = str(exc)[:300]
    else:
        activity_error = ""
    try:
        footprint = adapter.measure_footprint(plan)
        if not isinstance(footprint, dict):
            footprint = {}
    except Exception as exc:
        footprint = {"__error__": str(exc)[:300]}
    return {"inspection": _dump(inspection),
            "activity": _dump(activity) if activity else {
                "state": "unknown",
                "evidence": "activity probe failed: %s" % activity_error,
                "checked_at": _utcnow()},
            "footprint": {str(k): v for k, v in footprint.items()}}


def _run_backup(adapter, job_id):
    # type: (object, str) -> Dict[str, Any]
    return _dump(adapter.backup(job_id))


def _run_execute(adapter, plan, job_id, ack):
    # type: (object, object, str, bool) -> Dict[str, Any]
    return _dump(adapter.execute(plan, job_id, activity_ack=bool(ack)))


def _run_verify(adapter, plan, required_checks):
    # type: (object, object, object) -> Dict[str, Any]
    try:
        result = adapter.verify(
            plan=plan, required_checks=list(required_checks or []))
    except TypeError:
        result = adapter.verify()
    return _dump(result)


def _write_result(path, ok, kind, data):
    # type: (str, bool, str, Dict[str, Any]) -> bool
    # F11: result sanitization without the known-secret source fails
    # instead of persisting weaker-redacted evidence. Any write failure
    # returns False (coordinator treats a missing result as interrupted).
    try:
        from ..sanitize import sanitize_json
        try:
            from ..config import load_secret_values, settings
            secrets = load_secret_values(settings)
            secrets_ok = True
        except Exception:
            secrets = ()
            secrets_ok = False
        if not secrets_ok:
            return False
        clean = sanitize_json(
            {"ok": bool(ok), "kind": kind, "data": data or {},
             "ts": _utcnow()}, secrets)
    except Exception:
        return False
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.exists(parent):
            os.makedirs(parent, mode=0o700, exist_ok=True)
        tmp = "%s.tmp-%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, sort_keys=True, default=str)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _write_fixed_result(path, phase, error_code, error_detail):
    # type: (str, str, str, str) -> bool
    """Write a fixed-literal error result with NO external strings.

    Used exactly when sanitization is unavailable: every string below
    is a coordinator literal, so no secret source is required to write
    it safely. Returns False when even this cannot be persisted (the
    coordinator then treats the missing result as interrupted).
    """
    try:
        payload = {"ok": False, "kind": str(phase or ""),
                   "data": {"error_code": str(error_code or ""),
                            "error_detail": str(error_detail or "")[:500],
                            "evidence_durable": False},
                   "ts": _utcnow()}
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.exists(parent):
            os.makedirs(parent, mode=0o700, exist_ok=True)
        tmp = "%s.tmp-%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def main(argv=None):
    # type: (object) -> int
    ap = argparse.ArgumentParser(description="Update-OPS phase worker")
    ap.add_argument("job_id")
    ap.add_argument("phase", choices=PHASES)
    ap.add_argument("--payload", required=True)
    ap.add_argument("--result", required=True)
    ap.add_argument("--stream", required=True)
    ap.add_argument("--op", default="")
    ap.add_argument("--deadline-s", type=float, default=0.0)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else 2
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (OSError, ValueError):
            continue
    try:
        deadline = float(args.deadline_s or 0.0)
    except (TypeError, ValueError):
        deadline = 0.0
    if deadline > 0:
        # Backstop: no worker outlives coordinator patience + margin.
        try:
            signal.alarm(int(deadline + 300))
        except Exception:
            pass
    try:
        with open(args.payload, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return EXIT_INVALID
    if not isinstance(payload, dict):
        return EXIT_INVALID
    tool_id = str(payload.get("tool_id", "") or "")
    if not tool_id or not args.job_id:
        return EXIT_INVALID
    try:
        settings = _load_settings()
    except Exception:
        return EXIT_INVALID
    # F03 parity proof: recompute the canonical contract from THIS
    # process's context (scope env inherited from the coordinator) and
    # refuse when it differs from the coordinator's expectation. Preview
    # and apply provably share one environment instead of assuming it.
    try:
        from ..owner_env import (build_owner_contract,
                                 contract_fingerprint)
        own_fp = contract_fingerprint(build_owner_contract(settings))
    except Exception:
        own_fp = ""
    expected_fp = ""
    try:
        expected_fp = str(payload.get("env_fingerprint", "") or "")
    except Exception:
        expected_fp = ""
    if not own_fp or not expected_fp or own_fp != expected_fp:
        _write_result(args.result, False, args.phase,
                      {"error_code": "invalid_request",
                       "error_detail": "owner environment mismatch: "
                                       "phase worker contract differs from "
                                       "coordinator expectation"})
        return EXIT_OK
    secrets_ok = True
    try:
        from ..config import load_secret_values
        secrets = load_secret_values(settings)
    except Exception:
        secrets = ()
        secrets_ok = False
    # F11: a mutating or completion-evidence phase without the required
    # known-secret source fails closed — never persist tool output with
    # secrets=() and hope. Probes fail safe with an error result too.
    if not secrets_ok and args.phase in ("execute", "backup", "verify",
                                         "preflight", "probe"):
        _write_fixed_result(
            args.result, args.phase,
            "interrupted" if args.phase in ("execute", "verify")
            else "unavailable",
            "secret source unavailable: evidence cannot be trusted")
        return EXIT_OK
    writer = _StreamWriter(args.stream, secrets,
                           secrets_ok=secrets_ok)

    def _emit(stream, text):
        # type: (str, str) -> None
        try:
            writer.emit(stream, text)
        except Exception:
            pass

    try:
        adapter = _adapter(tool_id)
    except Exception:
        writer.flush_final()
        return EXIT_INVALID
    if not getattr(adapter, "enabled", True):
        _write_result(args.result, False, args.phase,
                      {"error_code": "install_method_unsupported",
                       "error_detail": "adapter disabled"})
        writer.flush_final()
        return EXIT_OK
    try:
        adapter._emit = _emit  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        if args.phase == "probe":
            data = _run_probe(adapter, args.op or
                              str(payload.get("op", "")))
        elif args.phase == "preflight":
            data = _run_preflight(
                adapter, _build_plan(payload.get("plan")))
        elif args.phase == "backup":
            data = _run_backup(adapter, args.job_id)
        elif args.phase == "execute":
            plan = _build_plan(payload.get("plan"))
            data = _run_execute(adapter, plan, args.job_id,
                                bool(payload.get("ack", False)))
        elif args.phase == "verify":
            plan = _build_plan(payload.get("plan"))
            data = _run_verify(adapter, plan,
                               payload.get("required_checks", []))
        else:
            return EXIT_INVALID
    except ValueError as exc:
        _write_result(args.result, False, args.phase,
                      {"error_code": "invalid_request",
                       "error_detail": str(exc)[:500]})
        writer.flush_final()
        return EXIT_OK
    except Exception as exc:
        _write_result(args.result, False, args.phase,
                      {"error_code": "interrupted",
                       "error_detail": "phase crashed: %s" % str(exc)[:500]})
        writer.flush_final()
        return EXIT_OK
    finally:
        try:
            writer.flush_final()
        except Exception:
            pass
    if _interrupted["flag"]:
        return EXIT_INTERRUPTED
    # F11: communicate evidence durability. A mutating/completion phase
    # whose stream evidence failed can never yield success downstream.
    try:
        durable = not bool(writer.evidence_failed)
    except Exception:
        durable = False
    if isinstance(data, dict):
        try:
            data["evidence_durable"] = bool(durable)
        except Exception:
            pass
    else:
        data = {"evidence_durable": bool(durable)}
    if not durable:
        _write_result(args.result, False, args.phase,
                      {"error_code": "interrupted" if args.phase in (
                          "execute", "verify") else "backup_failed"
                          if args.phase == "backup" else "unavailable",
                       "error_detail": "phase evidence durability failed: "
                                       "%s" % str(getattr(
                                           writer, "evidence_failure_reason",
                                           "") or "")[:300],
                       "evidence_durable": False})
        return EXIT_OK
    _write_result(args.result, True, args.phase, data)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())


# -- coordinator ------------------------------------------------------------

def _scope_argv(scope, release, config_path, venv_python, job_id, phase,
                payload_path, result_path, stream_path, op, deadline_s,
                env=None):
    # type: (...) -> List[str]
    """Exact scope-launch argv (fixed values only, never user input).

    The scope environment is the canonical owner contract (F03): the
    same allow-listed keys the runner launch carries (minus the attempt
    nonce — workers never consume attempts). Probes and phases share it.
    """
    for token in (scope, release, config_path, venv_python, job_id,
                  phase, payload_path, result_path, stream_path):
        if not token or not isinstance(token, str):
            raise ValueError("scope launch token missing")
        if "\0" in token:
            raise ValueError("scope launch token invalid")
    if "/" in scope or " " in scope:
        raise ValueError("scope unit name invalid")
    try:
        from ..owner_env import TRANSIENT_SETENV_KEYS
        allow = tuple(TRANSIENT_SETENV_KEYS)
    except Exception:
        allow = ("EGA_CONFIG_FILE", "EGA_RELEASE_ROOT", "PATH", "HOME",
                 "USER", "LOGNAME", "XDG_RUNTIME_DIR",
                 "DBUS_SESSION_BUS_ADDRESS", "LANG", "LC_ALL", "LC_CTYPE",
                 "TZ")
    argv = ["systemd-run", "--user", "--scope", "--quiet",
            "--unit=%s" % scope,
            "--working-directory=%s" % release,
            "--setenv=EGA_CONFIG_FILE=%s" % config_path,
            "--setenv=EGA_RELEASE_ROOT=%s" % release]
    try:
        env_map = dict(env or {})
    except Exception:
        env_map = {}
    for key in allow:
        if key in ("EGA_CONFIG_FILE", "EGA_RELEASE_ROOT",
                   "EGA_ATTEMPT_NONCE"):
            continue
        try:
            value = env_map.get(key, "")
        except Exception:
            value = ""
        if value:
            argv.append("--setenv=%s=%s" % (key, value))
    argv += [venv_python, "-m", "backend.app.worker.phase_run",
             job_id, phase,
             "--payload", payload_path, "--result", result_path,
             "--stream", stream_path,
             "--deadline-s", "%.1f" % max(1.0, float(deadline_s or 60.0))]
    if op:
        argv += ["--op", str(op)]
    return argv


def _kill_scope_wait_empty(scope, grace_s=10.0):
    # type: (str, float) -> bool
    """SIGTERM scope, grace, SIGKILL, prove quiescent. True when empty."""
    import subprocess as _sp

    for sig in ("SIGTERM", "SIGKILL"):
        try:
            _sp.run(["systemctl", "--user", "kill", "--kill-whom=all",
                     "--signal=%s" % sig, scope],
                    stdout=_sp.PIPE, stderr=_sp.PIPE, timeout=15,
                    shell=False, check=False)
        except Exception:
            pass
        try:
            from .. import units as _units
            info = _units.query_unit(scope, timeout_s=5)
            if str(info.get("state", "")) == "confirmed_stopped":
                return True
        except Exception:
            pass
        try:
            time.sleep(min(2.0, max(0.5, float(grace_s) / 2.0)))
        except Exception:
            pass
    try:
        from .. import units as _units
        info = _units.query_unit(scope, timeout_s=5)
        return str(info.get("state", "")) == "confirmed_stopped"
    except Exception:
        return False


def _write_launch_files(payload, payload_path, result_path,
                        stream_path):
    # type: (dict, str, str, str) -> str
    """Atomically write the worker payload; clear stale result/stream.

    Returns "" on success, else a failure reason (fail closed).
    """
    try:
        parent = os.path.dirname(os.path.abspath(payload_path))
        if parent and not os.path.exists(parent):
            os.makedirs(parent, mode=0o700, exist_ok=True)
        tmp = "%s.tmp-%d" % (payload_path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, default=str)
        os.replace(tmp, payload_path)
        for stale in (result_path, stream_path):
            try:
                if os.path.exists(stale):
                    os.remove(stale)
            except OSError:
                pass
    except OSError as exc:
        return "launch files unwritable: %s" % exc
    except Exception as exc:
        return "payload unbuildable: %s" % exc
    return ""


def _supervise_launched(proc, unit, unit_kind, result_path, stream_path,
                        deadline, emit, cancel_event):
    # type: (object, str, str, str, str, float, object, object) -> tuple
    """Supervise one launched worker process to completion (N10, H03).

    Shared by phase scopes and probe services: live stream tailing,
    monotonic deadline, cancel support, scope/service kill + proven
    quiescence on timeout, H03 exit proof (the unit must be confirmed
    stopped before any result delivers — a unit that will not empty is
    a timeout), then result-file delivery. Returns
    (ok, data, error, timed_out) with the phase-runner contract.
    unit_kind names the unit in quiescence messages ("phase scope" or
    "probe service").
    """
    offset = [0]
    timed_out = False
    cancelled = False

    def _tail():
        # type: () -> None
        try:
            with open(stream_path, "rb") as fh:
                fh.seek(offset[0])
                chunk = fh.read(256 * 1024)
                offset[0] += len(chunk)
        except OSError:
            return
        except Exception:
            return
        try:
            text = chunk.decode("utf-8", errors="replace")
        except Exception:
            return
        for raw in text.split("\n"):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
                line = str(record.get("line", ""))
                stream = str(record.get("stream", "stdout"))
            except Exception:
                continue
            if stream not in ("stdout", "stderr", "event"):
                stream = "stdout"
            try:
                emit(stream, line)
            except Exception:
                continue

    grace = min(30.0, max(5.0, deadline / 10.0))
    started = time.monotonic()
    while True:
        if cancel_event is not None:
            try:
                if cancel_event.is_set():
                    cancelled = True
                    break
            except Exception:
                pass
        try:
            rc = proc.poll()
        except Exception:
            rc = None
        if rc is not None:
            break
        if time.monotonic() - started > deadline:
            timed_out = True
            break
        _tail()
        time.sleep(0.25)
    if timed_out or cancelled:
        if _kill_scope_wait_empty(unit, grace):
            _tail()
        else:
            _tail()
            return False, {}, \
                "%s not quiescent after kill" % unit_kind, True
        try:
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        _tail()
        if cancelled:
            return False, {}, "phase cancelled", True
        return False, {}, "phase deadline exceeded", True
    try:
        rc = proc.wait(timeout=30)
    except Exception:
        rc = -1
    _tail()
    try:
        _, err = proc.communicate(timeout=5)
    except Exception:
        err = b""
    # H03: completion must PROVE unit exit, not assume it. The worker
    # process ended, but stray unit children (which carry no hex
    # marker, so no later scan can see them) would otherwise leak into
    # the next phase or past terminalization. A unit that will not
    # empty is a timeout: survivors exist, so the caller must keep
    # recovery, exactly as on deadline expiry.
    try:
        from .. import units as _units_mod
        _unit_info = _units_mod.query_unit(unit, timeout_s=5)
        _unit_state = str(
            (_unit_info or {}).get("state", "unknown"))
    except Exception:
        _unit_state = "unknown"
    if _unit_state != "confirmed_stopped":
        if _kill_scope_wait_empty(unit, grace):
            _tail()
        else:
            _tail()
            return False, {}, \
                "%s not quiescent after completion" % unit_kind, True
    if int(rc or 0) not in (0,):
        detail = ""
        try:
            detail = (err or b"").decode("utf-8",
                                         errors="replace")[:500]
        except Exception:
            pass
        # A nonzero worker exit with a valid result file still
        # delivers data (e.g. probe-level failures); missing result
        # is what fails.
        try:
            with open(result_path, "r", encoding="utf-8") as fh:
                result = json.load(fh)
        except Exception:
            return False, {}, \
                "phase worker exit=%s %s" % (rc, detail), False
        if not isinstance(result, dict) or "ok" not in result:
            return False, {}, \
                "phase worker exit=%s %s" % (rc, detail), False
        if result.get("ok"):
            return True, result.get("data", {}) or {}, "", False
        data = result.get("data", {}) or {}
        return False, data, str(
            data.get("error_detail", data.get(
                "error_code", "phase failed")))[:500], False
    try:
        with open(result_path, "r", encoding="utf-8") as fh:
            result = json.load(fh)
    except Exception:
        return False, {}, "phase result missing", False
    if not isinstance(result, dict) or "ok" not in result:
        return False, {}, "phase result malformed", False
    if result.get("ok"):
        return True, result.get("data", {}) or {}, "", False
    data = result.get("data", {}) or {}
    return False, data, str(
        data.get("error_detail", data.get(
            "error_code", "phase failed")))[:500], False


def run_supervised_phase(tool_id, job_id, phase, payload_extra,
                         timeout_s, settings, log_dir, emit,
                         op="", cancel_event=None, env=None):
    # type: (...) -> tuple
    """Run one phase in a coordinator-owned scope (N10, F03).

    env is the canonical owner contract environment (owner_env.contract_env
    of build_owner_contract): probes and phases launch with it identically.
    The expected environment fingerprint travels in the payload; the
    worker recomputes it from its own context and refuses on mismatch,
    proving preview/apply parity instead of assuming it.

    Returns (ok, data, error, timed_out):
    - ok True + data: worker completed AND its scope proved empty;
      data is the sanitized result dict.
    - timed_out True: deadline hit, or the scope would not empty after
      the worker ended (H03 — survivors exist, caller must keep
      recovery); scope killed + proven empty (or proven NON-empty ->
      caller must keep recovery). No worker Python survives: it is an
      OS process in the killed cgroup.
    - ok False: launch/refusal/result failure with error reason.
    Stream lines are delivered to emit(stream, line) live while the
    worker runs and drained fully afterwards. Payload/result/stream
    files are removed afterwards (best effort).
    """
    import subprocess as _sp

    try:
        deadline = max(5.0, float(timeout_s))
    except (TypeError, ValueError):
        deadline = 60.0
    try:
        from ..owner_env import (resolve_release, resolved_paths,
                                 transient_scope_name)
        from ..reconcile_core import unit_hex
    except Exception as exc:
        return False, {}, "owner env contract: %s" % exc, False
    try:
        paths = resolved_paths(settings)
        release = paths.get("release_root", "")
        venv_python = paths.get("venv_python", "")
        config_path = paths.get("config_path", "") or \
            os.environ.get("EGA_CONFIG_FILE", "") or \
            "/etc/ega-update/config.json"
        if not release or not venv_python:
            return False, {}, "owner paths unresolvable", False
    except ValueError as exc:
        return False, {}, "release unresolvable: %s" % exc, False
    except Exception as exc:
        return False, {}, "owner paths: %s" % exc, False
    try:
        scope = transient_scope_name(unit_hex(job_id), phase)
    except Exception:
        return False, {}, "scope name unbuildable", False
    uid = str(job_id or "").replace("/", "_")
    payload_path = os.path.join(log_dir, "%s.%s.payload.json" % (uid, phase))
    result_path = os.path.join(log_dir, "%s.%s.result.json" % (uid, phase))
    stream_path = os.path.join(log_dir, "%s.%s.stream" % (uid, phase))
    try:
        from ..owner_env import (build_owner_contract, contract_env,
                                 contract_fingerprint)
        if not isinstance(env, dict) or not env:
            env = contract_env(build_owner_contract(settings))
        expected_fp = contract_fingerprint(
            build_owner_contract(settings))
    except Exception as exc:
        return False, {}, "owner contract unbuildable: %s" % exc, False
    if not expected_fp:
        return False, {}, "owner contract fingerprint empty", False
    payload = {"tool_id": tool_id, "job_id": job_id, "phase": phase,
               "op": op or "", "env_fingerprint": expected_fp}
    try:
        if isinstance(payload_extra, dict):
            for key, value in payload_extra.items():
                if isinstance(key, str):
                    payload[key] = value
    except Exception:
        return False, {}, "payload unbuildable", False
    _files_error = _write_launch_files(payload, payload_path,
                                         result_path, stream_path)
    if _files_error:
        return False, {}, "phase files: %s" % _files_error, False
    try:
        argv = _scope_argv(scope, release, config_path, venv_python,
                           job_id, phase, payload_path, result_path,
                           stream_path, op, deadline, env)
    except ValueError as exc:
        return False, {}, "scope launch refused: %s" % exc, False
    if not (os.path.isfile("/usr/bin/systemd-run")
            and os.access("/usr/bin/systemd-run", os.X_OK)):
        return False, {}, \
            "systemd-run missing; refusing unsupervised phase", False
    try:
        proc = _sp.Popen(argv, stdout=_sp.DEVNULL, stderr=_sp.PIPE,
                         stdin=_sp.DEVNULL, cwd=release, shell=False,
                         start_new_session=True)
    except Exception as exc:
        return False, {}, "phase spawn failed: %s" % exc, False
    try:
        return _supervise_launched(proc, scope, "phase scope",
                                   result_path, stream_path, deadline,
                                   emit, cancel_event)
    finally:
        for path in (payload_path, result_path, stream_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass


def run_supervised_probe(tool_id, request_id, payload_extra,
                         timeout_s, settings, log_dir, emit,
                         op="", env=None):
    # type: (...) -> tuple
    """Run one authoritative owner probe in a transient SERVICE (H04).

    The probe service launches from the SAME canonical owner contract
    and the SAME shared service properties (KillMode=control-group,
    Restart=no, NoNewPrivileges=no) as the job runner service, so
    preview observes the identical privilege/restart-authority reality
    as apply — never an inherited scope from the NNP-on dispatcher.
    Phase workers stay scopes under the runner (their parent already
    carries the intended profile); probe/request scopes are never
    used for authoritative reads.

    Returns (ok, data, error, timed_out) with the same contract as
    run_supervised_phase, including the H03 exit proof: a probe
    service that will not stop prevents successful completion.
    Stream lines are delivered to emit() live; payload/result/stream
    files are removed afterwards (best effort).
    """
    import subprocess as _sp

    try:
        deadline = max(5.0, float(timeout_s))
    except (TypeError, ValueError):
        deadline = 60.0
    try:
        from ..owner_env import (build_probe_cmd, resolved_paths,
                                 transient_probe_name)
    except Exception as exc:
        return False, {}, "owner env contract: %s" % exc, False
    try:
        paths = resolved_paths(settings)
        release = paths.get("release_root", "")
        venv_python = paths.get("venv_python", "")
        config_path = paths.get("config_path", "") or \
            os.environ.get("EGA_CONFIG_FILE", "") or \
            "/etc/ega-update/config.json"
        if not release or not venv_python:
            return False, {}, "owner paths unresolvable", False
    except ValueError as exc:
        return False, {}, "release unresolvable: %s" % exc, False
    except Exception as exc:
        return False, {}, "owner paths: %s" % exc, False
    try:
        service = transient_probe_name(request_id or "")
    except ValueError as exc:
        return False, {}, "probe service refused: %s" % exc, False
    except Exception as exc:
        return False, {}, "probe service unbuildable: %s" % exc, False
    uid = str(request_id or "").replace("/", "_")
    payload_path = os.path.join(log_dir, "%s.probe.payload.json" % uid)
    result_path = os.path.join(log_dir, "%s.probe.result.json" % uid)
    stream_path = os.path.join(log_dir, "%s.probe.stream" % uid)
    try:
        from ..owner_env import (build_owner_contract, contract_env,
                                 contract_fingerprint)
        if not isinstance(env, dict) or not env:
            env = contract_env(build_owner_contract(settings))
        expected_fp = contract_fingerprint(
            build_owner_contract(settings))
    except Exception as exc:
        return False, {}, "owner contract unbuildable: %s" % exc, False
    if not expected_fp:
        return False, {}, "owner contract fingerprint empty", False
    payload = {"tool_id": tool_id, "job_id": request_id,
               "phase": "probe", "op": op or "",
               "env_fingerprint": expected_fp}
    try:
        if isinstance(payload_extra, dict):
            for key, value in payload_extra.items():
                if isinstance(key, str):
                    payload[key] = value
    except Exception:
        return False, {}, "payload unbuildable", False
    _files_error = _write_launch_files(payload, payload_path,
                                       result_path, stream_path)
    if _files_error:
        return False, {}, "probe files: %s" % _files_error, False
    try:
        argv = build_probe_cmd(service, release, env, venv_python,
                               request_id, payload_path, result_path,
                               stream_path, op, deadline)
    except ValueError as exc:
        return False, {}, "probe launch refused: %s" % exc, False
    if not (os.path.isfile("/usr/bin/systemd-run")
            and os.access("/usr/bin/systemd-run", os.X_OK)):
        return False, {}, \
            "systemd-run missing; refusing unsupervised probe", False
    try:
        proc = _sp.Popen(argv, stdout=_sp.DEVNULL, stderr=_sp.PIPE,
                         stdin=_sp.DEVNULL, cwd=release, shell=False,
                         start_new_session=True)
    except Exception as exc:
        return False, {}, "probe spawn failed: %s" % exc, False
    try:
        return _supervise_launched(proc, service, "probe service",
                                   result_path, stream_path, deadline,
                                   emit, None)
    finally:
        for path in (payload_path, result_path, stream_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
