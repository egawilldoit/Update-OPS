"""Canonical owner runtime environment contract (R02). Python 3.10 compatible.

One explicit environment for preview probes AND update execution:
UID/GID, HOME, PATH, release root, working directory, interpreter,
Node/npm/npx, config path, inventory, user-bus address, approved values,
required service authority. Allow-list only: never import an interactive
shell environment, never pass unrelated secrets.

The release pointer (/opt/ega-update/current) is resolved ONCE per job to
an immutable release path bound to the job/plan (jobs.release_path,
plans.release_path); preview and apply must agree or the plan is invalid.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

DEFAULT_RELEASE_LINK = "/opt/ega-update/current"

# Allow-listed environment keys for owner-side processes. Everything else
# is dropped. Secrets travel only via explicit config paths, never env.
ALLOW_ENV_KEYS = (
    "PATH", "HOME", "USER", "LOGNAME", "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "EGA_CONFIG_FILE", "EGA_ATTEMPT_NONCE", "EGA_RELEASE_ROOT",
)


def resolve_release(link_path=""):
    # type: (str) -> str
    """Resolve the release link to an immutable absolute release path.

    Raises ValueError when unresolvable (fail closed; callers block).
    EGA_RELEASE_ROOT overrides only for disposable verification.
    """
    override = os.environ.get("EGA_RELEASE_ROOT", "")
    if override:
        root = os.path.abspath(override)
        if os.path.isdir(root):
            return root
        raise ValueError("EGA_RELEASE_ROOT is not a directory: %s"
                         % override)
    link = link_path or DEFAULT_RELEASE_LINK
    try:
        root = os.path.realpath(link)
    except Exception as exc:
        raise ValueError("release link unresolvable: %s" % exc)
    if not os.path.isdir(root):
        raise ValueError("release root is not a directory: %s" % root)
    return root


def resolved_paths(settings=None, inventory=None):
    # type: (object, object) -> Dict[str, str]
    """Frozen contract consumed by adapters (fail-closed values only).

    Returns release_root, venv_python, node_path, npm_path, npx_path,
    config_path, state_dir, log_dir, backup_dir, uid, gid, home, user.
    Raises ValueError when the release cannot be resolved.
    """
    try:
        from .config import settings as _defaults
    except Exception:
        _defaults = None
    s = settings if settings is not None else _defaults
    release_root = resolve_release()
    venv_python = os.path.join(release_root, "venv", "bin", "python")
    out = {
        "release_root": release_root,
        "venv_python": venv_python,
        "node_path": "",
        "npm_path": "",
        "npx_path": "",
        "config_path": os.environ.get("EGA_CONFIG_FILE", "") or
                       "/etc/ega-update/config.json",
        "state_dir": "/var/lib/ega-update",
        "log_dir": "/var/lib/ega-update/logs",
        "backup_dir": "/var/lib/ega-update/backups",
        "uid": "",
        "gid": "",
        "home": "",
        "user": "",
    }
    try:
        if s is not None:
            out["node_path"] = str(getattr(s, "node_path", "") or "")
            out["npm_path"] = str(getattr(s, "npm_path", "") or "")
            out["npx_path"] = str(getattr(s, "npx_path", "") or "")
            out["state_dir"] = str(getattr(s, "state_dir", "") or
                                   out["state_dir"])
            out["log_dir"] = str(getattr(s, "log_dir", "") or
                                 out["log_dir"])
            out["backup_dir"] = str(getattr(s, "backup_dir", "") or
                                    out["backup_dir"])
            out["user"] = str(getattr(s, "tool_owner", "") or "ubuntu")
    except Exception:
        pass
    try:
        import pwd as _pwd
        try:
            pw = _pwd.getpwnam(out["user"] or "ubuntu")
            out["uid"] = str(pw.pw_uid)
            out["gid"] = str(pw.pw_gid)
            out["home"] = str(pw.pw_dir or "")
        except KeyError:
            pass
    except Exception:
        pass
    return out


def validate_executables(paths):
    # type: (Dict[str, str]) -> Tuple[bool, List[str]]
    """Check required executables exist+execute. Returns (ok, missing)."""
    missing = []
    for key in ("venv_python", "node_path", "npm_path", "npx_path"):
        candidate = (paths or {}).get(key, "")
        try:
            ok = bool(candidate) and os.path.isfile(candidate) and \
                os.access(candidate, os.X_OK)
        except Exception:
            ok = False
        if not ok:
            missing.append("%s=%s" % (key, candidate))
    return (len(missing) == 0), missing


def build_job_env(nonce, paths=None, extra_bus=None):
    # type: (str, object, object) -> Dict[str, str]
    """Allow-listed environment for the runner unit (R02, F03).

    Derived from the single canonical contract (build_owner_contract),
    never reconstructed here: preview and apply share it exactly.
    extra_bus merges existence-resolved bus values (dispatcher side).
    """
    try:
        merged_source = dict(_environ_mapping(None))
    except Exception:
        merged_source = {}
    if isinstance(extra_bus, dict):
        for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
            try:
                if extra_bus.get(key):
                    merged_source[key] = str(extra_bus[key])
            except Exception:
                continue
    try:
        from .config import settings as _defaults
    except Exception:
        _defaults = None
    _release = ""
    try:
        _release = str((paths or {}).get("release_root", "") or "")
    except Exception:
        _release = ""
    try:
        contract = build_owner_contract(
            _defaults, None, _release, merged_source)
    except Exception:
        contract = {}
    if paths:
        # Honor explicitly supplied resolved paths (tests, tooling).
        try:
            for key, ckey in (("node_path", "node"),
                              ("npm_path", "npm"),
                              ("npx_path", "npx"),
                              ("home", "home"), ("user", "user"),
                              ("config_path", "config")):
                val = (paths or {}).get(key, "")
                if val:
                    contract[ckey] = str(val)
            node_bin = os.path.dirname(
                str((paths or {}).get("node_path", "") or ""))
            if node_bin:
                parts = [node_bin] + [
                    p for p in ["/usr/local/bin", "/usr/bin", "/bin"]
                    if p != node_bin]
                contract["path"] = ":".join(parts)
        except Exception:
            pass
    return contract_env(contract, nonce)


LOCALE_KEYS = ("LANG", "LC_ALL", "LC_CTYPE", "TZ")

BUS_KEYS = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


def _environ_mapping(source=None):
    # type: (object) -> Dict[str, str]
    """Read-only snapshot of the relevant process environment."""
    if isinstance(source, dict):
        return {str(k): str(v) for k, v in source.items()}
    try:
        import os as _os
        return dict(_os.environ)
    except Exception:
        return {}


def build_owner_contract(settings=None, inventory=None, release_path="",
                         env_source=None):
    # type: (object, object, str, object) -> Dict[str, str]
    """One canonical owner execution contract (F03).

    Every execution-relevant property in ONE typed structure, derived
    once from canonical sources — never reconstructed per module:
    uid/gid/user/home (passwd), PATH (node bin + fixed fallbacks),
    config file, release root, venv/node/npm/npx paths, XDG runtime +
    DBus address, locale/TZ, NoNewPrivileges policy, systemd manager
    scope, and the approved sudo/service-authority profile identifier.

    env_source supplies bus/locale values (default: this process's
    environ). The dispatcher passes its environ (+ existence-resolved
    bus); scoped workers inherit the contract through --setenv and
    therefore resolve the identical contract from their own environ.
    All values are strings; missing values are "" (explicit, comparable).
    """
    try:
        from .config import settings as _defaults
    except Exception:
        _defaults = None
    s = settings if settings is not None else _defaults
    environ = _environ_mapping(env_source)
    try:
        paths = resolved_paths(s, inventory)
    except Exception:
        paths = {}
    if release_path:
        release = str(release_path)
    else:
        try:
            release = resolve_release()
        except Exception:
            release = ""
    user = str(paths.get("user", "") or "ubuntu")
    uid = str(paths.get("uid", "") or "")
    gid = str(paths.get("gid", "") or "")
    home = str(paths.get("home", "") or "")
    node_bin = ""
    try:
        import os as _os
        node_bin = _os.path.dirname(paths.get("node_path", "") or "")
    except Exception:
        node_bin = ""
    path_parts = []
    if node_bin:
        path_parts.append(node_bin)
    for fallback in ("/usr/local/bin", "/usr/bin", "/bin"):
        if fallback not in path_parts:
            path_parts.append(fallback)
    try:
        bus = systemd_user_bus(user)
    except Exception:
        bus = {}
    # Existence-resolved bus wins when present (dispatcher side); the
    # scoped worker side inherits the same values through --setenv and
    # therefore computes the identical contract from its environ.
    xdg = str(bus.get("XDG_RUNTIME_DIR", "") or environ.get(
        "XDG_RUNTIME_DIR", "") or "")
    dbus = str(bus.get("DBUS_SESSION_BUS_ADDRESS", "") or environ.get(
        "DBUS_SESSION_BUS_ADDRESS", "") or "")
    try:
        from .inventory import config_identity
        config_id = config_identity(s, inventory)
    except Exception:
        config_id = ""
    contract = {
        "uid": uid,
        "gid": gid,
        "user": user,
        "home": home,
        "path": ":".join(path_parts),
        "config": str(paths.get("config_path", "") or environ.get(
            "EGA_CONFIG_FILE", "") or "/etc/ega-update/config.json"),
        "release": release,
        "venv_python": str(paths.get("venv_python", "") or ""),
        "node": str(paths.get("node_path", "") or ""),
        "npm": str(paths.get("npm_path", "") or ""),
        "npx": str(paths.get("npx_path", "") or ""),
        "xdg_runtime_dir": xdg,
        "dbus_address": dbus,
        "lang": str(environ.get("LANG", "") or ""),
        "lc_all": str(environ.get("LC_ALL", "") or ""),
        "lc_ctype": str(environ.get("LC_CTYPE", "") or ""),
        "tz": str(environ.get("TZ", "") or ""),
        "no_new_privileges": "true",
        "manager_scope": "user",
        "config_identity": str(config_id or ""),
        "sudo_profile": _sudo_profile_id(inventory),
    }
    return contract


def _sudo_profile_id(inventory=None):
    # type: (object) -> str
    """Approved sudo/service-authority profile identifier (F03).

    Canonical hash of the inventoried per-tool service units
    (supervisor scope + restart authority) and launch methods — the
    exact privilege surface preview and apply must share. Empty
    inventory yields a deterministic hash of empties (downstream gates
    fail closed on uninventoried tools regardless).
    """
    import hashlib
    import json

    try:
        from .inventory import get_tool_inventory
        subset = {}
        for tool_id in ("hermes", "opencode", "codex", "t3"):
            try:
                entry = get_tool_inventory(tool_id)
            except Exception:
                entry = {}
            if not isinstance(entry, dict):
                entry = {}
            subset[tool_id] = {
                "service_units": entry.get("service_units", []),
                "launch_method": entry.get("launch_method", ""),
            }
        raw = json.dumps(subset, sort_keys=True, default=str).encode(
            "utf-8")
        return "sha256:%s" % hashlib.sha256(raw).hexdigest()
    except Exception:
        return "unavailable"


def contract_fingerprint(contract):
    # type: (Dict[str, str]) -> str
    """Hash the actual canonical execution contract (F03).

    Fingerprints the real contract dict — not a conceptual subset — so
    any execution-relevant difference (HOME, PATH, bus, locale,
    executables, privilege profile, release) invalidates the plan.
    """
    import hashlib
    import json

    try:
        narrowed = {str(k): str(v or "") for k, v in
                    (contract or {}).items()}
        raw = json.dumps(narrowed, sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()
    except Exception:
        return ""


def contract_env(contract, nonce=""):
    # type: (Dict[str, str], str) -> Dict[str, str]
    """Environment dictionary derived from ONE contract (F03).

    Used for runner launch arguments AND probe launch arguments alike.
    Only contract keys travel (plus the attempt nonce); never the
    ambient process environment wholesale.
    """
    contract = dict(contract or {})
    env = {
        "PATH": contract.get("path", "") or "/usr/local/bin:/usr/bin:/bin",
        "HOME": contract.get("home", "") or "/home/ubuntu",
        "USER": contract.get("user", "") or "ubuntu",
        "LOGNAME": contract.get("user", "") or "ubuntu",
        "EGA_CONFIG_FILE": contract.get("config", "") or
                           "/etc/ega-update/config.json",
        "EGA_RELEASE_ROOT": contract.get("release", "") or "",
        "XDG_RUNTIME_DIR": contract.get("xdg_runtime_dir", "") or "",
        "DBUS_SESSION_BUS_ADDRESS": contract.get("dbus_address", "") or "",
        "LANG": contract.get("lang", "") or "",
        "LC_ALL": contract.get("lc_all", "") or "",
        "LC_CTYPE": contract.get("lc_ctype", "") or "",
        "TZ": contract.get("tz", "") or "",
    }
    # Drop empties except PATH/HOME/USER/LOGNAME (always materialized
    # above); systemd --setenv skips them identically on both paths.
    env = {k: v for k, v in env.items()
           if v or k in ("PATH", "HOME", "USER", "LOGNAME")}
    if nonce:
        env["EGA_ATTEMPT_NONCE"] = str(nonce)
    return env


def systemd_user_bus(user="ubuntu"):
    # type: (str) -> Dict[str, str]
    """User-bus address for systemd-run --user from a system service.

    Enabling linger starts the user manager; the bus socket is
    /run/user/<uid>/bus. Returns {} when the uid is unresolvable (the
    launcher then fails closed with an explicit event, never an
    interactive login dependency).
    """
    try:
        import pwd as _pwd
        uid = _pwd.getpwnam(user or "ubuntu").pw_uid
    except Exception:
        return {}
    runtime = "/run/user/%d" % uid
    bus = os.path.join(runtime, "bus")
    out = {}  # type: Dict[str, str]
    try:
        if os.path.isdir(runtime):
            out["XDG_RUNTIME_DIR"] = runtime
        if os.path.exists(bus):
            out["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%s" % bus
    except Exception:
        pass
    return out


def env_fingerprint(env):
    # type: (Dict[str, str]) -> str
    """Stable hash of the canonical env allow-list (binds preview==apply)."""
    import hashlib
    import json

    try:
        narrowed = {k: env.get(k, "") for k in ALLOW_ENV_KEYS
                    if k in (env or {})}
        raw = json.dumps(narrowed, sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()
    except Exception:
        return ""


# -- canonical transient launch contract (one source, N-wave-3 cleanup) ----
# Dispatcher, templates, docs, and tests derive from these values. Any
# divergence is a defect: exactly one mechanism launches job runners.

TRANSIENT_RUNNER_PROPERTIES = (
    ("KillMode", "control-group"),
    ("Restart", "no"),
)

TRANSIENT_SETENV_KEYS = (
    "EGA_CONFIG_FILE",
    "EGA_ATTEMPT_NONCE",
    "EGA_RELEASE_ROOT",
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
)

RUNNER_MODULE_ARGV = ["-m", "backend.app.worker.runner"]


def build_transient_cmd(unit, working_dir, env, python, job_id, nonce):
    # type: (str, str, Dict[str, str], str, str, str) -> List[str]
    """Exact systemd-run --user argv for one job runner (single source).

    user manager only, --collect, canonical unit, resolved working dir,
    allow-listed --setenv, KillMode=control-group, Restart=no, then the
    release interpreter running the runner module with job+nonce.
    """
    if not unit or not job_id or not nonce:
        raise ValueError("unit, job_id, and nonce are required")
    cmd = ["systemd-run", "--user", "--collect", "--unit=%s" % unit,
           "--working-directory=%s" % working_dir]
    for key in TRANSIENT_SETENV_KEYS:
        try:
            value = (env or {}).get(key, "")
        except Exception:
            value = ""
        if value:
            cmd.append("--setenv=%s=%s" % (key, value))
    for prop, val in TRANSIENT_RUNNER_PROPERTIES:
        cmd.append("--property=%s=%s" % (prop, val))
    cmd += [python, "-m", "backend.app.worker.runner", job_id, nonce]
    return cmd


def transient_scope_name(job_hex, phase):
    # type: (str, str) -> str
    """Per-phase scope unit inside the job unit (R06/N10)."""
    stem = str(job_hex or "").replace("-", "")
    return "ega-update-job-%s-%s.scope" % (stem, phase)


def canonical_fingerprint(settings=None, inventory=None, release_path=""):
    # type: (object, object, str) -> str
    """Environment fingerprint comparable across processes (F03).

    Thin compatibility wrapper: builds the single canonical owner
    execution contract and hashes it. All callers (plan creation,
    admission, runner mutation boundary) therefore bind the FULL
    contract — HOME, PATH, bus, locale, executables, privilege
    profile — not a conceptual subset.
    """
    try:
        return contract_fingerprint(
            build_owner_contract(settings, inventory,
                                 release_path or ""))
    except Exception:
        return ""
