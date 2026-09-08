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
    """Allow-listed environment for the runner unit (R02).

    Only ALLOW_ENV_KEYS-derived values plus EGA_CONFIG_FILE/EGA_ATTEMPT_NONCE
    and owner Node paths. Never copies os.environ wholesale.
    """
    paths = dict(paths or {})
    node_bin = os.path.dirname(paths.get("node_path", "") or "")
    path_parts = []
    if node_bin:
        path_parts.append(node_bin)
    for fallback in ("/usr/local/bin", "/usr/bin", "/bin"):
        if fallback not in path_parts:
            path_parts.append(fallback)
    env = {
        "PATH": ":".join(path_parts),
        "HOME": paths.get("home", "") or "/home/ubuntu",
        "USER": paths.get("user", "") or "ubuntu",
        "LOGNAME": paths.get("user", "") or "ubuntu",
        "EGA_ATTEMPT_NONCE": nonce or "",
        "EGA_CONFIG_FILE": paths.get("config_path", "") or
                           "/etc/ega-update/config.json",
        "EGA_RELEASE_ROOT": paths.get("release_root", "") or "",
    }
    for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "LANG",
                "LC_ALL", "LC_CTYPE", "TZ"):
        try:
            val = os.environ.get(key, "")
        except Exception:
            val = ""
        if val:
            env[key] = val
    if isinstance(extra_bus, dict):
        for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
            if extra_bus.get(key):
                env[key] = str(extra_bus[key])
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
