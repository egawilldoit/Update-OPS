"""Packaged release CLI (R12/R13). Python 3.10 compatible.

Invoked ONLY as ``<release>/venv/bin/python -m backend.app.cli ...`` from
the staged release root (deploy anchors CWD + interpreter; never ambient
python3, never heredoc). JSON envelope on stdout, human diagnostics on
stderr, process exit EXACTLY 0/2/3/4/5/6 (thin scripts use exec so codes
propagate bit-identically).

Commands: inspect | plan | verify (read-only; this CLI runs as the tool
owner over SSH, so direct adapter calls are legitimate here — unlike the
restricted API account) | apply (reserve + observe canonical dispatch;
NEVER runs the runner directly) | status (quiescence for deploy) |
retention | reconcile.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_BLOCKED = 3
EXIT_INSTALL_FAILED = 4
EXIT_VERIFY_FAILED = 5
EXIT_INTERRUPTED = 6

STATE_TO_EXIT = {
    "succeeded": EXIT_OK,
    "blocked": EXIT_BLOCKED,
    "failed": EXIT_INSTALL_FAILED,
    "health_failed": EXIT_VERIFY_FAILED,
    "interrupted": EXIT_INTERRUPTED,
    "accepted": EXIT_INTERRUPTED,
    "preflight": EXIT_INTERRUPTED,
    "backup": EXIT_INTERRUPTED,
    "updating": EXIT_INTERRUPTED,
    "verifying": EXIT_INTERRUPTED,
}


def _utcnow():
    # type: () -> str
    try:
        from .schemas import utcnow_iso
        return utcnow_iso()
    except Exception:
        import datetime
        return datetime.datetime.now(
            datetime.timezone.utc).isoformat()


def _emit_envelope(tool, action, job_id="", state="", exit_mapped=0,
                   error_code="", detail=""):
    # type: (...) -> None
    # N12: diagnostic detail that cannot be sanitized becomes a fixed
    # safe message — never raw exception strings that may carry secrets.
    # Shape is preserved (dicts stay dicts for the machine-readable
    # quiescence schema); only the failure path collapses to a string.
    try:
        from .sanitize import sanitize_json
        detail = sanitize_json(detail, ())
    except Exception:
        detail = "detail withheld: sanitization failed"
    payload = {
        "schema_version": 1,
        "tool": tool or "",
        "action": action or "",
        "job_id": job_id or "",
        "state": state or "",
        "exit_code_mapped": int(exit_mapped),
        "error_code": error_code or "",
        "detail": detail if isinstance(detail, str) else str(detail),
        "ts": _utcnow(),
    }
    try:
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        sys.stdout.flush()
    except OSError:
        pass


def _err(text):
    # type: (str) -> None
    try:
        sys.stderr.write("%s\n" % text)
    except OSError:
        pass


def _load_settings():
    # type: () -> object
    from .config import load_settings
    return load_settings()


def _adapter(tool_id):
    # type: (str) -> object
    from .adapters import registry as _registry
    return _registry.get_adapter(tool_id)


def _model_dump(model):
    # type: (object) -> dict
    try:
        if hasattr(model, "model_dump"):
            data = model.model_dump()
        else:
            data = dict(model)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def cmd_inspect(args):
    # type: (object) -> int
    try:
        adapter = _adapter(args.tool)
    except KeyError:
        _emit_envelope(args.tool, "inspect", error_code="invalid_request",
                       detail="unknown tool", exit_mapped=EXIT_INVALID)
        return EXIT_INVALID
    if not getattr(adapter, "enabled", True):
        _emit_envelope(args.tool, "inspect",
                       error_code="install_method_unsupported",
                       detail="adapter disabled", exit_mapped=EXIT_BLOCKED)
        return EXIT_BLOCKED
    try:
        result = adapter.inspect()
    except Exception as exc:
        _emit_envelope(args.tool, "inspect", error_code="unavailable",
                       detail="inspect failed: %s" % exc,
                       exit_mapped=EXIT_INTERRUPTED)
        return EXIT_INTERRUPTED
    _emit_envelope(args.tool, "inspect", state="ok",
                   detail=_model_dump(result), exit_mapped=EXIT_OK)
    return EXIT_OK


def cmd_verify(args):
    # type: (object) -> int
    try:
        adapter = _adapter(args.tool)
    except KeyError:
        _emit_envelope(args.tool, "verify", error_code="invalid_request",
                       detail="unknown tool", exit_mapped=EXIT_INVALID)
        return EXIT_INVALID
    if not getattr(adapter, "enabled", True):
        _emit_envelope(args.tool, "verify",
                       error_code="install_method_unsupported",
                       detail="adapter disabled", exit_mapped=EXIT_BLOCKED)
        return EXIT_BLOCKED
    try:
        result = adapter.verify()
    except Exception as exc:
        _emit_envelope(args.tool, "verify", error_code="unavailable",
                       detail="verify failed: %s" % exc,
                       exit_mapped=EXIT_INTERRUPTED)
        return EXIT_INTERRUPTED
    data = _model_dump(result)
    code = EXIT_OK if data.get("passed") else EXIT_VERIFY_FAILED
    _emit_envelope(args.tool, "verify",
                   state="passed" if data.get("passed") else "failed",
                   error_code="" if data.get("passed") else "health_failed",
                   detail=data, exit_mapped=code)
    return code


def cmd_plan(args):
    # type: (object) -> int
    _emit_envelope(args.tool, "plan", error_code="invalid_request",
                   detail="server-owned plans are built via POST "
                          "/api/v1/tools/{id}/plans; CLI plan is read-only "
                          "preview of adapter intent",
                   exit_mapped=EXIT_INVALID)
    try:
        adapter = _adapter(args.tool)
    except KeyError:
        return EXIT_INVALID
    if not getattr(adapter, "enabled", True):
        return EXIT_BLOCKED
    try:
        planned = adapter.plan()
        activity = adapter.activity()
    except Exception as exc:
        _emit_envelope(args.tool, "plan", error_code="unavailable",
                       detail="plan probe failed: %s" % exc,
                       exit_mapped=EXIT_INTERRUPTED)
        return EXIT_INTERRUPTED
    _emit_envelope(args.tool, "plan", state="preview",
                   detail={"plan": _model_dump(planned),
                           "activity": _model_dump(activity)},
                   exit_mapped=EXIT_OK)
    return EXIT_OK


def cmd_apply(args):
    # type: (object) -> int
    """Reserve + observe canonical dispatch. Never runs the runner."""
    from . import jobs as _jobs
    from .db import connect

    try:
        settings = _load_settings()
    except Exception as exc:
        _emit_envelope(args.tool, "apply", error_code="invalid_request",
                       detail="config unavailable: %s" % exc,
                       exit_mapped=EXIT_INVALID)
        return EXIT_INVALID
    plan_id = (args.plan_id or "").strip()
    if not plan_id:
        _emit_envelope(args.tool, "apply", error_code="invalid_request",
                       detail="--plan-id is required",
                       exit_mapped=EXIT_INVALID)
        return EXIT_INVALID
    key = (args.idempotency_key or "").strip() or str(uuid.uuid4())
    ack = bool(args.ack)
    try:
        conn = connect(settings.db_path)
    except Exception as exc:
        _emit_envelope(args.tool, "apply", error_code="unavailable",
                       detail="database unavailable: %s" % exc,
                       exit_mapped=EXIT_INTERRUPTED)
        return EXIT_INTERRUPTED
    try:
        # One shared admission service (N14): identical semantics to the
        # API path. Fresh owner evidence comes from the probe queue
        # (dispatcher canonical env — never this SSH login environment).
        from .admission import admit
        from .owner_probes import await_probe, enqueue_probe
        try:
            subject = ",".join(
                getattr(settings, "owner_emails", []) or [])
            if not subject:
                _prow = conn.execute(
                    "SELECT subject FROM plans WHERE id=?",
                    (plan_id,)).fetchone()
                subject = str(dict(_prow).get("subject", "")) \
                    if _prow else ""
        except Exception:
            subject = ""
        try:
            request_id = enqueue_probe(conn, subject, "", "inspect")
        except Exception:
            request_id = ""
        _fp_status, _fp_payload = ("error", {})
        _tool_probe = ""
        if request_id:
            try:
                plan_probe = conn.execute(
                    "SELECT tool_id FROM plans WHERE id=?",
                    (plan_id,)).fetchone()
                _tool_probe = str(dict(plan_probe).get("tool_id", "")
                                  ) if plan_probe else ""
            except Exception:
                _tool_probe = ""
            if _tool_probe:
                try:
                    conn.execute(
                        "UPDATE probe_requests SET tool_id=? WHERE id=?",
                        (_tool_probe, request_id))
                    conn.commit()
                except Exception:
                    pass
                _fp_status, _fp_payload = await_probe(
                    conn, request_id, timeout_s=30.0)
        if _fp_status != "ok":
            _emit_envelope(args.tool, "apply", error_code="unavailable",
                           detail="installation revalidation unavailable",
                           exit_mapped=EXIT_INTERRUPTED)
            return EXIT_INTERRUPTED
        try:
            _hb = _jobs.read_dispatcher_heartbeat(
                getattr(settings, "state_dir", ""), max_age_s=20)
        except Exception:
            _hb = {}
        try:
            _drained = os.path.exists(os.path.join(
                str(getattr(settings, "state_dir", "") or ""), "drain"))
        except Exception:
            _drained = False
        job_id, created_new, err = admit(
            conn, subject, key, plan_id, ack,
            str(_fp_payload.get("fingerprint", "") or ""),
            bool(_hb), bool(_drained))
        if err:
            _emit_envelope(args.tool or _tool_probe, "apply",
                           error_code=err,
                           detail="admission refused: %s" % err,
                           exit_mapped=EXIT_BLOCKED
                           if err not in ("invalid_request", "not_found")
                           else EXIT_INVALID)
            return EXIT_BLOCKED \
                if err not in ("invalid_request", "not_found") \
                else EXIT_INVALID
        if not created_new:
            _emit_envelope(_tool_probe, "apply", job_id=job_id,
                           state="replayed", exit_mapped=EXIT_OK,
                           detail="idempotent replay")
            return EXIT_OK
        wait_s = max(0, int(args.wait_secs or 0))
        deadline = time.monotonic() + wait_s if wait_s else None
        last_state = "accepted"
        while True:
            try:
                row = conn.execute(
                    "SELECT state, error_code FROM jobs WHERE id=?",
                    (job_id,)).fetchone()
            except Exception:
                row = None
            if row is None:
                _emit_envelope(args.tool or _tool_probe, "apply", job_id=job_id,
                               state="unknown", error_code="unavailable",
                               detail="reservation lost",
                               exit_mapped=EXIT_INTERRUPTED)
                return EXIT_INTERRUPTED
            last_state = str(row["state"] or "")
            if last_state in ("succeeded", "blocked", "failed",
                              "health_failed", "interrupted"):
                code = STATE_TO_EXIT.get(last_state, EXIT_INTERRUPTED)
                _emit_envelope(args.tool or _tool_probe, "apply", job_id=job_id,
                               state=last_state,
                               error_code=str(row["error_code"] or ""),
                               exit_mapped=code,
                               detail="canonical dispatch completed")
                return code
            if deadline is not None and time.monotonic() >= deadline:
                _emit_envelope(args.tool or _tool_probe, "apply", job_id=job_id,
                               state=last_state,
                               detail="wait budget exhausted; job continues"
                                      " under dispatcher",
                               exit_mapped=EXIT_OK)
                return EXIT_OK
            time.sleep(2.0)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def cmd_status(args):
    # type: (object) -> int
    """Quiescence report for deploy scripts (N01/R33). Read-only.

    Canonical schema (see backend/app/quiescence.py — the ONLY shape
    deploy scripts consume):
      detail{worker_alive, active_job, unresolved_jobs, recovery_jobs,
             unresolved_units, live_units, delegated_operations, drain,
             quiescent, reasons}
    --require-quiescent exits 0 only when proven quiescent, 3 otherwise,
    so deployment relies on the process exit, not shell JSON parsing.
    """
    from . import jobs as _jobs
    from .db import connect
    from .quiescence import QUIESCENCE_SCHEMA_VERSION, assess_quiescence

    try:
        settings = _load_settings()
    except Exception as exc:
        _emit_envelope("", "status", error_code="invalid_request",
                       detail="config unavailable: %s" % exc,
                       exit_mapped=EXIT_INVALID)
        return EXIT_INVALID
    try:
        conn = connect(settings.db_path)
    except Exception as exc:
        _emit_envelope("", "status", error_code="unavailable",
                       detail="database unavailable: %s" % exc,
                       exit_mapped=EXIT_INTERRUPTED)
        return EXIT_INTERRUPTED
    try:
        assessment = assess_quiescence(conn, settings)
    except Exception as exc:
        _emit_envelope("", "status", error_code="unavailable",
                       detail="quiescence assessment crashed: %s" % exc,
                       exit_mapped=EXIT_INTERRUPTED)
        return EXIT_INTERRUPTED
    finally:
        try:
            conn.close()
        except Exception:
            pass
    assessment = dict(assessment)
    assessment["schema_version"] = QUIESCENCE_SCHEMA_VERSION
    quiescent = bool(assessment.get("quiescent", False))
    if getattr(args, "require_quiescent", False):
        if quiescent:
            _emit_envelope("", "status", state="quiescent",
                           detail=assessment, exit_mapped=EXIT_OK)
            return EXIT_OK
        _emit_envelope(
            "", "status", state="blocked", error_code="not_quiescent",
            detail=assessment, exit_mapped=EXIT_BLOCKED)
        return EXIT_BLOCKED
    if getattr(args, "require_ready", False):
        if bool(assessment.get("worker_alive", False)):
            _emit_envelope("", "status", state="ready",
                           detail=assessment, exit_mapped=EXIT_OK)
            return EXIT_OK
        _emit_envelope(
            "", "status", state="blocked", error_code="worker_not_ready",
            detail=assessment, exit_mapped=EXIT_BLOCKED)
        return EXIT_BLOCKED
    _emit_envelope("", "status", state="ok", detail=assessment,
                   exit_mapped=EXIT_OK)
    return EXIT_OK


def cmd_retention(args):
    # type: (object) -> int
    try:
        from .db import connect
        from .retention import run_retention
    except Exception as exc:
        _emit_envelope("", "retention", error_code="unavailable",
                       detail="retention unavailable: %s" % exc,
                       exit_mapped=EXIT_INTERRUPTED)
        return EXIT_INTERRUPTED
    try:
        settings = _load_settings()
        conn = connect(settings.db_path)
    except Exception as exc:
        _emit_envelope("", "retention", error_code="unavailable",
                       detail="database unavailable: %s" % exc,
                       exit_mapped=EXIT_INTERRUPTED)
        return EXIT_INTERRUPTED
    try:
        report = run_retention(conn, settings)
    except Exception as exc:
        _emit_envelope("", "retention", error_code="interrupted",
                       detail="retention failed: %s" % exc,
                       exit_mapped=EXIT_INTERRUPTED)
        return EXIT_INTERRUPTED
    finally:
        try:
            conn.close()
        except Exception:
            pass
    _emit_envelope("", "retention", state="ok", detail=report,
                   exit_mapped=EXIT_OK)
    return EXIT_OK


def cmd_reconcile(args):
    # type: (object) -> int
    from .worker import reconcile as _reconcile

    argv = ["--job-id", args.job_id or ""]
    if args.clear_recovery:
        argv.append("--clear-recovery")
    if args.db_path:
        argv.extend(["--db-path", args.db_path])
    try:
        return int(_reconcile.main(argv))
    except SystemExit as exc:
        try:
            return int(exc.code or 0)
        except (TypeError, ValueError):
            return EXIT_INVALID
    except Exception as exc:
        _err("reconcile crashed: %s" % exc)
        return EXIT_INTERRUPTED


def build_parser():
    # type: () -> object
    ap = argparse.ArgumentParser(
        description="Update-OPS owner CLI (release-local interpreter only)")
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("inspect", "plan", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--tool", required=True)
        p.set_defaults(func={"inspect": cmd_inspect, "plan": cmd_plan,
                             "verify": cmd_verify}[name])
    p = sub.add_parser("apply")
    p.add_argument("--tool", default="")
    p.add_argument("--plan-id", default="")
    p.add_argument("--ack", action="store_true")
    p.add_argument("--no-ack", dest="ack", action="store_false")
    p.add_argument("--idempotency-key", default="")
    p.add_argument("--wait-secs", type=int, default=3600)
    p.set_defaults(func=cmd_apply)
    p = sub.add_parser("status")
    p.add_argument("--require-quiescent", action="store_true",
                   help="exit 0 only when quiescence is proven, else 3")
    p.add_argument("--require-ready", action="store_true",
                   help="exit 0 only when the worker is alive, else 3")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("retention")
    p.set_defaults(func=cmd_retention)
    p = sub.add_parser("reconcile")
    p.add_argument("--job-id", default="")
    p.add_argument("--clear-recovery", action="store_true")
    p.add_argument("--db-path", default="")
    p.set_defaults(func=cmd_reconcile)
    return ap


def main(argv=None):
    # type: (object) -> int
    ap = build_parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) \
            else EXIT_INVALID
    try:
        return int(args.func(args))
    except Exception as exc:
        _err("cli crashed: %s" % exc)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
