"""Canonical deployment readiness assessment (W4-D8). Python 3.10.

ONE machine-readable readiness definition, consumed by ONE CLI gate
(``python -m backend.app.cli status --require-ready``). install.sh and
upgrade.sh consume only that gate's exit code through the shared shell
helper (``deploy/scripts/lib/deploy_common.sh``); there is no second
definition.

Mandatory stages (each independently provable; never collapsed into one
"ready"):

  1. ``api_service_identity`` — the ACTUAL API service identity can read
     config, resolve csrf.secret, open/read the database, and traverse/
     write the required runtime paths. Proven by running an identity probe
     UNDER that identity (runuser/su/direct when already that uid), never
     from ownership/permission strings.
  2. ``api_security_boundary`` — the configured loopback listener is
     reachable and an unauthenticated request is REJECTED (401/403). A
     rejection here is evidence of auth enforcement only and never makes
     the overall assessment ready by itself.
  3. ``worker_process`` — dispatcher heartbeat is current (the existing
     quiescence/status worker signal).
  4. ``probe_executor`` — the required W3.1 ProbeWorker published a fresh
     durable ready marker of its own; the dispatcher heartbeat is not
     accepted as evidence.
  5. ``owner_transient_execution`` — the canonical D1 transient owner
     acceptance primitive (owner_env.transient_acceptance) proves a real
     user-manager transient round trip: bus reachable, unit launched,
     effective owner identity, shared-path/payload/config access, typed
     result written/read, and positive unit termination.

Failure semantics: non-zero exit, failed stage named in machine-readable
output, no secret contents (booleans/ids/paths only), no success claim.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from typing import Any, Dict, List

READINESS_SCHEMA_VERSION = 1
READINESS_PROFILE = "deployment-readiness"
API_IDENTITY_PROFILE = "api-service-identity-probe"
DEFAULT_API_USER = "ega-update"
DEFAULT_API_UNIT = "ega-update-api.service"
IDENTITY_PROBE_TIMEOUT_S = 30.0
HTTP_PROBE_TIMEOUT_S = 3.0

MANDATORY_STAGES = (
    "api_service_identity",
    "api_security_boundary",
    "worker_process",
    "probe_executor",
    "owner_transient_execution",
)


def _settings_or_default(settings=None):
    # type: (object) -> object
    if settings is not None:
        return settings
    from .config import settings as defaults
    return defaults


def _stage(ok, reasons=None, evidence=None):
    # type: (bool, object, object) -> Dict[str, Any]
    return {"ok": bool(ok), "reasons": [str(r) for r in (reasons or [])],
            "evidence": dict(evidence or {})}


# -- stage 1: API service identity -------------------------------------------

def _default_unit_user_query(unit):
    # type: (str) -> str
    try:
        import subprocess

        proc = subprocess.run(
            ["systemctl", "show", unit, "--property=User", "--value"],
            capture_output=True, text=True, timeout=5)
        user = (proc.stdout or "").strip()
        return user if user and user != "root" else ""
    except Exception:
        return ""


def resolve_api_user(settings=None, unit_query=None):
    # type: (object, object) -> str
    """Resolve the ACTUAL API service identity.

    Precedence: the installed api unit's User= (authoritative, queried
    only when running as root), the configured api_user, the documented
    default. Never returns root.
    """
    s = _settings_or_default(settings)
    try:
        units = getattr(s, "service_units", None) or {}
        unit = str(units.get("api", "") or DEFAULT_API_UNIT)
    except Exception:
        unit = DEFAULT_API_UNIT
    query = unit_query
    if query is None and os.geteuid() == 0:
        query = _default_unit_user_query
    if query is not None:
        try:
            user = str(query(unit) or "").strip()
        except Exception:
            user = ""
        if user and user != "root":
            return user
    try:
        cfg_user = str(getattr(s, "api_user", "") or "").strip()
    except Exception:
        cfg_user = ""
    return cfg_user or DEFAULT_API_USER


def api_identity_probe(settings=None, config_file=""):
    # type: (object, str) -> Dict[str, Any]
    """Effective-access probe; MUST run under the API service identity.

    Loads config, resolves csrf.secret, actually opens the DB, and
    exercises the required runtime paths. Emits booleans/ids/paths only —
    never secret contents.
    """
    from .owner_env import execution_credentials_probe
    from .readiness import is_placeholder

    out = {"profile": API_IDENTITY_PROFILE, "ok": False, "uid": os.getuid(),
           "euid": os.geteuid(), "user": "", "checks": {},
           "reasons": []}  # type: Dict[str, Any]
    try:
        import pwd
        out["user"] = pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        pass
    reasons = out["reasons"]  # type: List[str]
    checks = out["checks"]  # type: Dict[str, Dict[str, Any]]
    try:
        if settings is None:
            from .config import load_settings
            s = load_settings()
        else:
            s = settings
    except Exception as exc:
        reasons.append("config unavailable (%s)" % type(exc).__name__)
        return out
    cfg = str(config_file or os.environ.get("EGA_CONFIG_FILE", "") or "")
    if config_file:
        try:
            os.environ["EGA_CONFIG_FILE"] = str(config_file)
        except Exception:
            pass
    checks["config_file_read"] = {
        "path": cfg,
        "ok": bool(cfg) and os.path.isfile(cfg)
        and os.access(cfg, os.R_OK)}
    csrf = str(getattr(s, "csrf_secret", "") or "")
    csrf_file = str(getattr(s, "csrf_secret_file", "") or "")
    if is_placeholder(csrf) and csrf_file:
        # Mirror config load_settings: resolve the csrf.secret file
        # fallback under THIS identity (the value is never emitted).
        try:
            with open(csrf_file, "r", encoding="utf-8",
                      errors="replace") as fh:
                for line in fh.read().splitlines():
                    stripped = line.strip()
                    if stripped:
                        csrf = stripped
                        break
        except OSError:
            pass
    csrf_resolved = not is_placeholder(csrf)
    checks["csrf_secret_resolved"] = {"path": "", "ok": bool(csrf_resolved)}
    if csrf_file:
        checks["csrf_secret_file_read"] = {
            "path": csrf_file,
            "ok": bool(os.path.isfile(csrf_file)
                       and os.access(csrf_file, os.R_OK))}
    try:
        state_dir = str(getattr(s, "state_dir", "") or "")
        log_dir = str(getattr(s, "log_dir", "") or "")
        backup_dir = str(getattr(s, "backup_dir", "") or "")
        secrets_file = str(getattr(s, "secrets_file", "") or "")
        inventory_file = str(getattr(s, "inventory_file", "") or "")
        cfg_dir = os.path.dirname(cfg) if cfg else ""
        path_report = execution_credentials_probe(
            settings=s, state_dir=state_dir, log_dir=log_dir,
            backup_dir=backup_dir, config_dir=cfg_dir, config_file=cfg,
            secrets_file=secrets_file, inventory_file=inventory_file)
        for key, entry in (path_report.get("checks") or {}).items():
            if key in ("config_file_read", "result_dir_write",
                       "stream_dir_write", "inventory_file_read"):
                continue
            checks[key] = {"path": str((entry or {}).get("path", "")),
                           "ok": bool((entry or {}).get("ok"))}
    except Exception as exc:
        reasons.append("runtime path probe crashed (%s)"
                       % type(exc).__name__)
    db_path = str(getattr(s, "db_path", "") or "")
    db_ok = False
    try:
        import sqlite3

        conn = None
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True,
                                   timeout=5)
        except Exception:
            conn = sqlite3.connect(db_path, timeout=5)
        try:
            row = conn.execute("SELECT 1").fetchone()
            db_ok = bool(row and int(row[0]) == 1)
        finally:
            conn.close()
    except Exception:
        db_ok = False
    checks["db_open"] = {"path": db_path, "ok": bool(db_ok)}
    checks["db_read"] = {"path": db_path, "ok": bool(db_ok)}
    required = ("config_file_read", "csrf_secret_resolved",
                "state_dir_traverse", "state_dir_write", "log_dir_write",
                "backup_dir_write", "config_dir_traverse",
                "secrets_file_read", "db_open", "db_read")
    for name in required:
        entry = checks.get(name) or {}
        if not bool(entry.get("ok")):
            reasons.append("api identity: %s failed (%s)"
                           % (name, entry.get("path", "")))
    out["ok"] = not reasons
    return out


def _default_command_runner(argv, env=None, timeout_s=30.0):
    # type: (object, object, object) -> Dict[str, Any]
    from . import owner_env
    return owner_env._default_command_runner(argv, env, timeout_s)


def _run_probe_as_identity(api_user, uid, argv, env):
    # type: (str, int, list, dict) -> Dict[str, Any]
    if os.geteuid() == uid:
        return _default_command_runner(argv, env, IDENTITY_PROBE_TIMEOUT_S)
    if os.geteuid() != 0:
        return {"returncode": 255, "stdout": "",
                "stderr": "cannot switch identity without root"}
    import shutil

    runuser = shutil.which("runuser")
    if runuser:
        wrapped = [runuser, "-u", api_user, "--"] + list(argv)
    else:
        wrapped = ["su", "-s", "/bin/sh", api_user, "-c",
                   " ".join(shlex.quote(str(a)) for a in argv)]
    return _default_command_runner(wrapped, env, IDENTITY_PROBE_TIMEOUT_S)


def stage_api_service_identity(settings=None, runner=None, unit_query=None):
    # type: (object, object, object) -> Dict[str, Any]
    s = _settings_or_default(settings)
    user = resolve_api_user(s, unit_query)
    try:
        import pwd
        uid = int(pwd.getpwnam(user).pw_uid)
    except Exception:
        uid = None
    evidence = {"user": user, "uid": uid, "checks": {}}
    if uid is None:
        return _stage(False, ["api service identity unresolvable: %s" % user],
                      evidence)
    cfg = os.environ.get("EGA_CONFIG_FILE", "") or \
        "/etc/ega-update/config.json"
    if not cfg:
        return _stage(False,
                      ["EGA_CONFIG_FILE is not set; cannot prove the API "
                       "service identity"], evidence)
    argv = [sys.executable, "-m", "backend.app.deployment_readiness",
            "api-identity-probe", "--config-file", cfg]
    env = dict(os.environ)
    env["EGA_CONFIG_FILE"] = cfg
    try:
        from .owner_env import resolve_release
        release = resolve_release()
    except Exception:
        release = ""
    if release:
        env["PYTHONPATH"] = release + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = None
    if runner is not None:
        try:
            proc = runner(argv, env) or {}
        except Exception:
            proc = {}
    else:
        proc = _run_probe_as_identity(user, uid, argv, env)
    payload = {}
    try:
        for line in reversed(str(proc.get("stdout", "")).splitlines()):
            line = line.strip()
            if line.startswith("{"):
                payload = json.loads(line)
                break
    except Exception:
        payload = {}
    evidence["checks"] = {
        str(k): bool((v or {}).get("ok"))
        for k, v in (payload.get("checks") or {}).items()}
    reasons = []
    ok = bool(int(proc.get("returncode", 255)) == 0
              and isinstance(payload, dict) and payload.get("ok") is True
              and int(payload.get("uid", -1) or -1) == uid)
    if not ok:
        reasons.append("api identity probe exited %s"
                       % proc.get("returncode", 255))
        for entry in (payload.get("reasons") or [])[:5]:
            reasons.append(str(entry))
        if not payload:
            reasons.append("api identity probe produced no structured report")
    return _stage(ok, reasons, evidence)


# -- stage 2: API security boundary ------------------------------------------

def _default_http_probe(host, port):
    # type: (str, int) -> Dict[str, Any]
    import urllib.error
    import urllib.request

    url = "http://%s:%d/api/v1/health" % (host, port)
    try:
        with urllib.request.urlopen(url, timeout=HTTP_PROBE_TIMEOUT_S) as resp:
            return {"reachable": True,
                    "status": int(getattr(resp, "status", 0) or 0)}
    except urllib.error.HTTPError as exc:
        return {"reachable": True, "status": int(exc.code or 0)}
    except Exception:
        return {"reachable": False, "status": 0}


def stage_api_security_boundary(settings=None, http_probe=None):
    # type: (object, object) -> Dict[str, Any]
    s = _settings_or_default(settings)
    try:
        host = str(getattr(s, "listen_host", "") or "")
    except Exception:
        host = ""
    try:
        port = int(getattr(s, "listen_port", 0) or 0)
    except Exception:
        port = 0
    evidence = {"listen_host": host, "listen_port": port,
                "reachable": False, "http_status": 0,
                "unauthenticated_rejected": False}
    if host not in ("127.0.0.1", "localhost", "::1"):
        return _stage(False, ["listen_host is not loopback: %s" % host],
                      evidence)
    if not (0 < port <= 65535):
        return _stage(False, ["listen_port invalid: %s" % port], evidence)
    probe = http_probe or _default_http_probe
    try:
        res = probe(host, port) or {}
    except Exception:
        res = {"reachable": False, "status": 0}
    reachable = bool(res.get("reachable"))
    try:
        status = int(res.get("status", 0) or 0)
    except Exception:
        status = 0
    evidence["reachable"] = reachable
    evidence["http_status"] = status
    if not reachable:
        return _stage(False, ["loopback API unreachable on %s:%d"
                              % (host, port)], evidence)
    if status not in (401, 403):
        return _stage(False,
                      ["unauthenticated request returned %s "
                       "(expected 401/403)" % status], evidence)
    evidence["unauthenticated_rejected"] = True
    return _stage(True, [], evidence)


# -- stage 3: worker process --------------------------------------------------

def stage_worker_process(settings=None, quiescence=None):
    # type: (object, object) -> Dict[str, Any]
    q = quiescence if isinstance(quiescence, dict) else {}
    ok = bool(q.get("worker_alive", False))
    return _stage(
        ok, [] if ok else ["worker heartbeat stale/missing"],
        {"worker_alive": ok, "quiescent": bool(q.get("quiescent", False)),
         "active_job": str(q.get("active_job", "") or "")})


# -- stage 4: required probe executor (W3.1 durable marker) --------------------

def stage_probe_executor(settings=None, heartbeat_probe=None):
    # type: (object, object) -> Dict[str, Any]
    s = _settings_or_default(settings)
    try:
        state_dir = str(getattr(s, "state_dir", "") or "")
    except Exception:
        state_dir = ""
    marker = {}
    if heartbeat_probe is not None:
        try:
            marker = heartbeat_probe() or {}
        except Exception:
            marker = {}
    else:
        try:
            from .worker.dispatch import read_probe_worker_heartbeat
            marker = read_probe_worker_heartbeat(state_dir)
        except Exception:
            marker = {}
    ok = bool(isinstance(marker, dict) and marker.get("ready") is True)
    evidence = {
        "heartbeat_present": bool(marker),
        "age_s": (marker or {}).get("age_s"),
        "max_age_s": (marker or {}).get("max_age_s"),
        "path": str((marker or {}).get("path", "")),
    }
    return _stage(ok, [] if ok else [
        "required probe executor ready marker absent/stale"], evidence)


# -- stage 5: canonical owner transient acceptance ----------------------------

def stage_owner_transient_execution(settings=None, transient_runner=None):
    # type: (object, object) -> Dict[str, Any]
    s = _settings_or_default(settings)
    runner = transient_runner
    if runner is None:
        from . import owner_env
        runner = lambda: owner_env.transient_acceptance(settings=s)  # noqa
    try:
        result = runner() or {}
    except Exception as exc:
        return _stage(False, ["transient acceptance crashed (%s)"
                              % type(exc).__name__])
    reasons = [str(r) for r in (result.get("reasons") or [])][:10]
    evidence = {
        "owner": str(result.get("owner", "")),
        "uid": result.get("uid"),
        "bus_reachable": bool(result.get("bus_reachable")),
        "unit": str(result.get("unit", "")),
        "launch_exit": result.get("launch_exit"),
        "report_ok": bool(result.get("report_ok")),
        "identity_ok": bool(result.get("identity_ok")),
        "result_ok": bool(result.get("result_ok")),
        "unit_terminated": bool(result.get("unit_terminated")),
        "unit_state": str(result.get("unit_state", "")),
        "checks": {str(k): bool(v)
                   for k, v in (result.get("checks") or {}).items()},
    }
    return _stage(bool(result.get("ok")), reasons, evidence)


# -- aggregate ----------------------------------------------------------------

def assess_deployment_readiness(settings=None, quiescence=None,
                                identity_runner=None, http_probe=None,
                                probe_executor_probe=None,
                                transient_runner=None):
    # type: (...) -> Dict[str, Any]
    s = _settings_or_default(settings)
    stages = {
        "api_service_identity": stage_api_service_identity(
            s, runner=identity_runner),
        "api_security_boundary": stage_api_security_boundary(
            s, http_probe=http_probe),
        "worker_process": stage_worker_process(s, quiescence=quiescence),
        "probe_executor": stage_probe_executor(
            s, heartbeat_probe=probe_executor_probe),
        "owner_transient_execution": stage_owner_transient_execution(
            s, transient_runner=transient_runner),
    }  # type: Dict[str, Dict[str, Any]]
    failed = [name for name in MANDATORY_STAGES
              if not stages[name].get("ok")]
    reasons = []  # type: List[str]
    for name in failed:
        for entry in (stages[name].get("reasons") or [])[:5]:
            reasons.append("%s: %s" % (name, entry))
    return {
        "schema_version": READINESS_SCHEMA_VERSION,
        "profile": READINESS_PROFILE,
        "ready": not failed,
        "mandatory_stages": list(MANDATORY_STAGES),
        "stages": stages,
        "failed_stages": failed,
        "reasons": reasons,
    }


def main(argv=None):
    # type: (object) -> int
    """Readiness module CLI (identity probe child + VM acceptance assess).

    ``api-identity-probe`` runs in the calling identity (the deploy gate
    invokes it under the API service identity). ``assess`` runs the full
    assessment in the current identity (VM acceptance harness only).
    """
    import argparse

    ap = argparse.ArgumentParser(prog="backend.app.deployment_readiness")
    sub = ap.add_subparsers(dest="command", required=True)
    p_identity = sub.add_parser("api-identity-probe")
    p_identity.add_argument("--config-file", default="")
    p_assess = sub.add_parser("assess")
    p_assess.add_argument("--config-file", default="")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else 2
    if getattr(args, "config_file", ""):
        os.environ["EGA_CONFIG_FILE"] = str(args.config_file)
    if args.command == "api-identity-probe":
        report = api_identity_probe(config_file=args.config_file)
        sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
        return 0 if report.get("ok") else 1
    from . import db as db_lib
    from .quiescence import assess_quiescence

    settings = None
    try:
        from .config import load_settings
        settings = load_settings()
        conn = db_lib.connect(settings.db_path)
        try:
            assessment = assess_quiescence(conn, settings)
        finally:
            conn.close()
    except Exception as exc:
        sys.stderr.write("readiness assessment unavailable: %s\n"
                         % type(exc).__name__)
        return 1
    result = assess_deployment_readiness(settings, quiescence=assessment)
    result["quiescence"] = assessment
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0 if result.get("ready") else 3


if __name__ == "__main__":
    raise SystemExit(main())
