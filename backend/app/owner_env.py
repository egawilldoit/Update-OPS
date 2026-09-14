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

import json
import os
import re
import shutil
import stat
import struct
import sys
from typing import Any, Dict, List, Tuple

DEFAULT_RELEASE_LINK = "/opt/ega-update/current"

# Canonical owner-transient acceptance (W4-D8): ONE definition shared by
# the deployment readiness gate (install.sh and upgrade.sh consume only
# the gate exit code). The primitive launches the existing D1 probe in a
# real `systemd-run --user` transient unit as the configured tool owner
# and positively proves: user bus reachable; transient unit launched;
# effective owner identity correct; shared paths traversable; payload,
# config and secrets readable; typed result written and read back; unit
# termination positively observed. It never installs or updates any
# managed tool and never accepts an arbitrary shell command.
SYSTEMD_USER_RUN = "systemd-run --user"
# Canonical command shape (documentation): the launcher runs
# `python -m backend.app.owner_env probe ... --report-out <json>` inside a
# transient user unit; the parent verifies the report with the ONE
# owner_env verify machinery (verify_report; the CLI `verify` subcommand
# uses the same function) and positively queries unit termination.
TRANSIENT_ACCEPTANCE_PROFILE = "owner-transient-acceptance"
TRANSIENT_ACCEPTANCE_TIMEOUT_S = 60.0
TRANSIENT_ACCEPTANCE_RUNTIME_MAX_S = 30
TRANSIENT_ACCEPTANCE_REQUIRED_CHECKS = (
    "state_dir_traverse", "state_dir_write", "log_dir_write",
    "backup_dir_write", "config_dir_traverse", "config_file_read",
    "secrets_file_read", "payload_read", "result_write", "result_read",
)

# Documented default joint group shared by the API (ega-update) and the
# tool owner (ubuntu). Real deployments may override it via config
# (settings.shared_group / EGA_SHARED_GROUP); never hardcode beyond this
# documented default.
DEFAULT_OWNER_SHARED_GROUP = "ega-update"

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
        # H04 privilege truth: job execution AND authoritative probes
        # run WITHOUT NoNewPrivileges so the inventoried Hermes sudo
        # path (`sudo -n systemctl restart <unit>` under the narrow
        # sudoers allow-list) can elevate. With no_new_privs set,
        # setuid elevation is blocked and every Hermes system-unit
        # restart would fail. Probes run as transient SERVICES with the
        # same NNP-off property as the runner (never as inherited
        # scopes from the NNP-on dispatcher); phase scopes carry no NNP
        # property of their own — they inherit the runner service's
        # NNP-off context, recorded here as phase_privilege_source.
        # The API/worker services (which never elevate) keep
        # NoNewPrivileges=true; only owner execution is NNP-off. The
        # fingerprint describes this reality, not the reverse.
        "runner_no_new_privileges": "false",
        "probe_no_new_privileges": "false",
        "phase_privilege_source": "runner",
        "privilege_profile": "owner-exec-nnp-off",
        "manager_scope": "user",
        "config_identity": str(config_id or ""),
        "sudo_profile": _sudo_profile_id(inventory),
        # D1: the joint group the tool owner depends on for access to the
        # shared config/state paths. Recorded explicitly in the contract
        # so effective access is a declared invariant, not an assumption
        # about account-database membership (which the running user
        # manager may not reflect).
        "shared_group": resolve_shared_group(s, inventory),
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
# divergence is a defect. H04: job runner services AND authoritative
# probe services launch from this ONE property tuple — preview and
# apply execute under the same NNP-off owner profile. Phase scopes
# inherit the runner service context and carry no properties of their
# own, so they are not listed here.
TRANSIENT_EXEC_PROPERTIES = (
    ("KillMode", "control-group"),
    ("Restart", "no"),
    # F04/H04: explicit NoNewPrivileges=no. The default is already off,
    # but the fingerprint binds runner/probe_no_new_privileges=false,
    # so launch argv states it outright: owner execution must be able
    # to use the inventoried Hermes sudo path, which no_new_privs
    # would block (setuid elevation ignored -> sudo fails -> every
    # Hermes system restart becomes BLOCKED_RESTART_AUTHORITY).
    # Templates mirror this.
    ("NoNewPrivileges", "no"),
)

TRANSIENT_RUNNER_PROPERTIES = TRANSIENT_EXEC_PROPERTIES

TRANSIENT_PROBE_PROPERTIES = TRANSIENT_EXEC_PROPERTIES

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


def transient_probe_name(request_id):
    # type: (str) -> str
    """Transient probe service unit for one probe request (H04).

    ega-update-probe-<request-stem>.service on the ubuntu user
    manager. The stem keeps hex/dashes only (probe request ids are
    UUIDs); anything else fails closed with ValueError — the probe
    launcher refuses rather than guessing a unit name.
    """
    try:
        stem = "".join(
            ch for ch in str(request_id or "").replace("-", "")
            if ch.isalnum()).lower()[:32]
    except Exception:
        stem = ""
    if not stem:
        raise ValueError("probe request id unusable for a unit name")
    return "ega-update-probe-%s.service" % stem


def build_probe_cmd(service, working_dir, env, python, request_id,
                    payload_path, result_path, stream_path, op,
                    deadline_s):
    # type: (str, str, Dict[str, str], str, str, str, str, str, str, float) -> List[str]
    """Exact systemd-run --user argv for one authoritative probe (H04).

    A transient SERVICE (never --scope): --wait so the coordinator
    supervises it exactly like a phase scope, --collect, the shared
    TRANSIENT_PROBE_PROPERTIES (KillMode=control-group, Restart=no,
    NoNewPrivileges=no — the same NNP-off owner profile as the job
    runner), allow-listed --setenv from the same canonical contract
    env, then the release interpreter running the probe worker.
    """
    if not service or not request_id:
        raise ValueError("service and request_id are required")
    for token in (working_dir, python, payload_path, result_path,
                  stream_path):
        if not token or not isinstance(token, str):
            raise ValueError("probe launch token missing")
        if "\0" in token:
            raise ValueError("probe launch token invalid")
    if "/" in service or " " in service:
        raise ValueError("probe service name invalid")
    cmd = ["systemd-run", "--user", "--wait", "--collect",
           "--unit=%s" % service,
           "--working-directory=%s" % working_dir]
    for key in TRANSIENT_SETENV_KEYS:
        try:
            value = (env or {}).get(key, "")
        except Exception:
            value = ""
        if value:
            cmd.append("--setenv=%s=%s" % (key, value))
    for prop, val in TRANSIENT_PROBE_PROPERTIES:
        cmd.append("--property=%s=%s" % (prop, val))
    cmd += [python, "-m", "backend.app.worker.phase_run",
            request_id, "probe",
            "--payload", payload_path, "--result", result_path,
            "--stream", stream_path,
            "--deadline-s", "%.1f" % max(1.0, float(deadline_s or 60.0))]
    if op:
        cmd += ["--op", str(op)]
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


# -- D1: effective owner-execution credentials ------------------------------
# The long-running ``user@<uid>.service`` keeps the supplementary-group
# vector it had when it started. Transient ``systemd-run --user`` units
# inherit that vector, so a tool owner added to the joint group AFTER the
# manager started cannot traverse the group-owned shared paths even
# though the account database lists the membership. Account membership
# (``usermod -aG``) is therefore NOT an effective guarantee.
#
# Do NOT "fix" this with ``--property=SupplementaryGroups=<group>``: on
# the deployed systemd the unprivileged user manager has CapEff=0 (no
# CAP_SETGID), so the forked unit's setgroups() fails EPERM and the unit
# exits EXIT_GROUP (216) -- or the setting is silently ignored. Access is
# made explicit at the filesystem layer instead: a named-user POSIX ACL
# grants the tool owner exactly the access the joint group was supposed
# to provide, without widening any group/other permission bit. The
# result is verified from the real transient execution identity.
_ACL_XATTR = "system.posix_acl_access"
_ACL_VERSION = 2
_ACL_USER_OBJ = 0x01
_ACL_USER = 0x02
_ACL_GROUP_OBJ = 0x04
_ACL_GROUP = 0x08
_ACL_MASK = 0x10
_ACL_OTHER = 0x20
_ACL_UNDEFINED = 0xFFFFFFFF
_GROUP_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")


def resolve_shared_group(settings=None, inventory=None, override=""):
    # type: (object, object, str) -> str
    """Resolve the joint group the tool owner depends on (D1).

    Precedence: explicit override, ``EGA_SHARED_GROUP``, the canonical
    settings value, an inventory value, then the documented default.
    An invalid/injection-shaped name never travels; it falls back to the
    default rather than becoming launch/file arguments.
    """
    candidates = [override]
    try:
        candidates.append(os.environ.get("EGA_SHARED_GROUP", ""))
    except Exception:
        pass
    try:
        candidates.append(str(getattr(settings, "shared_group", "") or ""))
    except Exception:
        pass
    try:
        if isinstance(inventory, dict):
            candidates.append(str(inventory.get("shared_group", "") or ""))
    except Exception:
        pass
    for value in candidates:
        try:
            value = str(value or "").strip()
        except Exception:
            continue
        if value and _GROUP_NAME_RE.match(value):
            return value
    return DEFAULT_OWNER_SHARED_GROUP


def _acl_parse(raw):
    # type: (bytes) -> Tuple[int, List[List[int]]]
    if len(raw) < 4:
        raise ValueError("acl too short")
    version, = struct.unpack_from("<I", raw, 0)
    entries = []  # type: List[List[int]]
    offset = 4
    while offset + 8 <= len(raw):
        tag, perm, eid = struct.unpack_from("<HHI", raw, offset)
        entries.append([int(tag), int(perm), int(eid)])
        offset += 8
    return int(version), entries


def _acl_serialize(version, entries):
    # type: (int, List[List[int]]) -> bytes
    out = struct.pack("<I", int(version))
    for tag, perm, eid in entries:
        out += struct.pack("<HHI", int(tag), int(perm), int(eid))
    return out


def _acl_from_mode(mode):
    # type: (int) -> List[List[int]]
    mode = int(mode) & 0o777
    return [
        [_ACL_USER_OBJ, (mode >> 6) & 0o7, _ACL_UNDEFINED],
        [_ACL_GROUP_OBJ, (mode >> 3) & 0o7, _ACL_UNDEFINED],
        [_ACL_OTHER, mode & 0o7, _ACL_UNDEFINED],
    ]


def _read_acl_entries(path):
    # type: (str) -> List[List[int]]
    raw = os.getxattr(path, _ACL_XATTR)
    _version, entries = _acl_parse(raw)
    return entries


def named_user_acl_perms(path, uid, groups=()):
    # type: (str, int, object) -> int
    """Effective permission bits a kernel access check would grant uid.

    Mirrors the POSIX order (owner -> named user -> group -> other) and
    the ACL mask. Returns 0 when the path is unreadable or grants
    nothing to the principal. Contents are never read.
    """
    try:
        uid = int(uid)
    except Exception:
        return 0
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    try:
        entries = _read_acl_entries(path)
    except OSError:
        entries = _acl_from_mode(stat.S_IMODE(st.st_mode))
    except Exception:
        entries = _acl_from_mode(stat.S_IMODE(st.st_mode))

    def _find(tag, eid=None):
        # type: (int, object) -> object
        for t, p, e in entries:
            if t == tag and (eid is None or e == eid):
                return p
        return None

    if st.st_uid == uid:
        return int(_find(_ACL_USER_OBJ) or 0)
    named = _find(_ACL_USER, uid)
    if named is not None:
        mask = _find(_ACL_MASK)
        return int(named if mask is None else (named & mask))
    try:
        group_set = set(int(g) for g in (groups or ()))
    except Exception:
        group_set = set()
    group_set.add(int(st.st_gid))
    mask = _find(_ACL_MASK)
    if st.st_gid in group_set:
        base = _find(_ACL_GROUP_OBJ) or 0
        return int(base if mask is None else (base & mask))
    for t, p, e in entries:
        if t == _ACL_GROUP and e in group_set:
            return int(p if mask is None else (p & mask))
    return int(_find(_ACL_OTHER) or 0)


def ensure_named_user_access(path, uid, perms):
    # type: (str, int, int) -> bool
    """Upsert a named-user POSIX ACL entry without widening anything.

    Returns True when the principal is granted ``perms``. The ACL mask is
    never raised above the existing group-class permissions, so a grant
    that would require widening group access is refused (False) instead
    of silently weakening the file. The visible group bits (the mask) are
    preserved. Idempotent.
    """
    try:
        uid = int(uid)
        perms = int(perms) & 0o7
    except Exception:
        return False
    if perms == 0:
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    try:
        entries = _read_acl_entries(path)
    except Exception:
        entries = _acl_from_mode(stat.S_IMODE(st.st_mode))
    mask = 0
    for tag, perm, _eid in entries:
        if tag in (_ACL_USER, _ACL_GROUP, _ACL_GROUP_OBJ):
            mask |= perm
    if perms & ~mask:
        return False
    entries = [e for e in entries
               if not (e[0] == _ACL_USER and e[2] == uid)
               and e[0] != _ACL_MASK]
    entries.append([_ACL_USER, perms, uid])
    order = {_ACL_USER_OBJ: 0, _ACL_USER: 1, _ACL_GROUP_OBJ: 2,
             _ACL_GROUP: 3, _ACL_MASK: 4, _ACL_OTHER: 5}
    entries.sort(key=lambda e: order.get(e[0], 9))
    pos = 0
    for i, entry in enumerate(entries):
        if entry[0] in (_ACL_GROUP_OBJ, _ACL_GROUP):
            pos = i + 1
    entries.insert(pos, [_ACL_MASK, mask, _ACL_UNDEFINED])
    try:
        os.setxattr(path, _ACL_XATTR,
                    _acl_serialize(_ACL_VERSION, entries))
    except OSError:
        return False
    return True


def _resolve_owner(owner="", settings=None):
    # type: (str, object) -> str
    name = str(owner or "")
    if not name:
        try:
            name = str(getattr(settings, "tool_owner", "") or "")
        except Exception:
            name = ""
    return name or "ubuntu"


def _access_paths(settings, paths, state_dir, log_dir, backup_dir,
                  config_dir, config_file, secrets_file, inventory_file):
    # type: (object, object, str, str, str, str, str, str, str) -> Dict[str, str]
    base = {}  # type: Dict[str, str]
    try:
        if isinstance(paths, dict):
            base.update({str(k): str(v) for k, v in paths.items()
                         if isinstance(v, str)})
    except Exception:
        base = {}
    if settings is not None:
        try:
            for key in ("secrets_file", "inventory_file", "tool_owner"):
                if not base.get(key):
                    value = getattr(settings, key, "")
                    if isinstance(value, str) and value:
                        base[key] = value
        except Exception:
            pass
        try:
            if not base.get("state_dir"):
                for key, value in (resolved_paths(settings) or {}).items():
                    if isinstance(value, str) and value:
                        base.setdefault(key, value)
        except Exception:
            pass

    def _pick(explicit, key, default):
        # type: (str, str, str) -> str
        if explicit:
            return str(explicit)
        if base.get(key):
            return str(base[key])
        return default

    state = _pick(state_dir, "state_dir", "/var/lib/ega-update")
    log = _pick(log_dir, "log_dir", os.path.join(state, "logs"))
    backup = _pick(backup_dir, "backup_dir", os.path.join(state, "backups"))
    cfg = _pick(config_file, "config_path",
                "/etc/ega-update/config.json")
    etc = str(config_dir or base.get("config_dir", "")
              or os.path.dirname(cfg) or "/etc/ega-update")
    secrets = _pick(secrets_file, "secrets_file",
                    os.path.join(etc, "secrets.env"))
    inventory = _pick(inventory_file, "inventory_file",
                      os.path.join(etc, "inventory.json"))
    return {"state_dir": state, "log_dir": log, "backup_dir": backup,
            "config_dir": etc, "config_file": cfg,
            "secrets_file": secrets, "inventory_file": inventory}


def _group_gid(name):
    # type: (str) -> object
    try:
        import grp as _grp
        return int(_grp.getgrnam(name).gr_gid)
    except Exception:
        return None


def _grantable_uid(owner):
    # type: (str) -> object
    try:
        import pwd as _pwd
        return int(_pwd.getpwnam(owner).pw_uid)
    except Exception:
        return None


def provision_owner_access(settings=None, paths=None, owner="", group="",
                           state_dir="", log_dir="", backup_dir="",
                           config_dir="", config_file="", secrets_file="",
                           inventory_file=""):
    # type: (...) -> Dict[str, Any]
    """Provision the tool owner's EFFECTIVE access to the shared paths (D1).

    Grants a named-user POSIX ACL (never a wider chmod) for the tool
    owner on the state/log/backup directories and the config directory
    and its runtime files. Idempotent; safe to run on a fresh install and
    on an install whose user manager predates the group assignment.
    Returns a structured, redacted report (paths/labels only).
    """
    group = resolve_shared_group(settings, None, group)
    owner = _resolve_owner(owner, settings)
    uid = _grantable_uid(owner)
    report = {"ok": False, "owner": owner, "uid": uid, "group": group,
              "applied": [], "errors": []}  # type: Dict[str, Any]
    if uid is None:
        report["errors"].append("owner account unresolvable: %s" % owner)
        return report
    p = _access_paths(settings, paths, state_dir, log_dir, backup_dir,
                      config_dir, config_file, secrets_file,
                      inventory_file)
    targets = (
        ("state_dir", p["state_dir"], 0o7, True, True),
        ("log_dir", p["log_dir"], 0o7, True, True),
        ("backup_dir", p["backup_dir"], 0o7, True, True),
        ("config_dir", p["config_dir"], 0o5, True, True),
        ("config_file", p["config_file"], 0o4, True, False),
        ("secrets_file", p["secrets_file"], 0o4, True, False),
        ("inventory_file", p["inventory_file"], 0o4, False, False),
    )
    for label, path, perms, required, is_dir in targets:
        if not path:
            if required:
                report["errors"].append("%s path missing" % label)
            continue
        if not os.path.lexists(path):
            if required:
                report["errors"].append("%s missing: %s" % (label, path))
            continue
        if is_dir and not os.path.isdir(path):
            report["errors"].append("%s not a directory: %s"
                                    % (label, path))
            continue
        if not is_dir and os.path.isdir(path):
            report["errors"].append("%s is a directory: %s"
                                    % (label, path))
            continue
        if ensure_named_user_access(path, uid, perms):
            report["applied"].append({"label": label, "path": path,
                                      "perms": "0%o" % perms})
        else:
            report["errors"].append(
                "cannot grant %s to %s without widening permissions"
                % (label, owner))
    report["ok"] = not report["errors"]
    return report


def _write_probe(directory):
    # type: (str) -> bool
    """Actually create+remove one uniquely named file in directory."""
    if not directory or not os.path.isdir(directory):
        return False
    target = os.path.join(
        directory, ".ega-credcheck-%d-%s" % (os.getpid(),
                                             os.urandom(4).hex()))
    fd = None
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        fd = None
        os.unlink(target)
        return True
    except OSError:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if os.path.lexists(target):
                os.unlink(target)
        except OSError:
            pass


def _read_probe(path):
    # type: (str) -> bool
    """Open a file for read without consuming any contents."""
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as fh:
            fh.read(0)
        return True
    except OSError:
        return False


def execution_credentials_probe(settings=None, paths=None, owner="",
                                group="", state_dir="", log_dir="",
                                backup_dir="", config_dir="",
                                config_file="", secrets_file="",
                                inventory_file="", payload_path="",
                                result_path="", stream_path="", nonce=""):
    # type: (...) -> Dict[str, Any]
    """Verify the ACTUAL execution identity and access (D1).

    Reports effective uid/gid/supplementary groups plus real read/write
    access to the config source, probe payload, and result/stream
    directories. When ``result_path`` is given, a typed result document is
    actually written and read back with ``nonce`` so result
    writability/readability is proven, not inferred. Contains only
    booleans, ids, labels, and paths -- never file contents, secret
    values, or environment dumps. ``ok`` is True only when every required
    access is effective in THIS process.
    """
    owner = _resolve_owner(owner, settings)
    shared_group = resolve_shared_group(settings, None, group)
    try:
        import pwd as _pwd
        user = _pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        user = owner
    try:
        groups = sorted(int(g) for g in os.getgroups())
    except Exception:
        groups = []
    shared_gid = _group_gid(shared_group)
    p = _access_paths(settings, paths, state_dir, log_dir, backup_dir,
                      config_dir, config_file, secrets_file,
                      inventory_file)
    report = {
        "ok": False,
        "profile": "owner-exec-effective-access",
        "uid": os.getuid(),
        "euid": os.geteuid(),
        "gid": os.getgid(),
        "egid": os.getegid(),
        "groups": groups,
        "user": user,
        "shared_group": shared_group,
        "shared_gid": shared_gid,
        "in_shared_group": bool(shared_gid is not None
                                and shared_gid in groups),
        "checks": {},
        "reasons": [],
    }  # type: Dict[str, Any]

    def _access(path, mode):
        # type: (str, int) -> bool
        try:
            return bool(os.access(path, mode, effective_ids=True))
        except TypeError:
            try:
                return bool(os.access(path, mode))
            except Exception:
                return False
        except Exception:
            return False

    def _add(label, path, required, check):
        # type: (str, str, bool, object) -> None
        entry = {"path": path or "", "required": bool(required),
                 "ok": False}
        try:
            entry["ok"] = bool(check())
        except Exception:
            entry["ok"] = False
        report["checks"][label] = entry

    _add("state_dir_traverse", p["state_dir"], True,
         lambda: _access(p["state_dir"], os.X_OK))
    _add("state_dir_write", p["state_dir"], True,
         lambda: _write_probe(p["state_dir"]))
    _add("log_dir_write", p["log_dir"], True,
         lambda: _write_probe(p["log_dir"]))
    _add("backup_dir_write", p["backup_dir"], True,
         lambda: _write_probe(p["backup_dir"]))
    _add("config_dir_traverse", p["config_dir"], True,
         lambda: _access(p["config_dir"], os.X_OK))
    _add("config_file_read", p["config_file"], True,
         lambda: _read_probe(p["config_file"]))
    _add("secrets_file_read", p["secrets_file"], True,
         lambda: _read_probe(p["secrets_file"]))
    inventory_present = bool(p["inventory_file"]) and \
        os.path.exists(p["inventory_file"])
    _add("inventory_file_read", p["inventory_file"],
         inventory_present,
         lambda: _read_probe(p["inventory_file"])
         if inventory_present else True)
    if payload_path:
        _add("payload_read", payload_path, True,
             lambda: _read_probe(payload_path))
    result_dir = os.path.dirname(result_path) or p["log_dir"]
    stream_dir = os.path.dirname(stream_path) or p["log_dir"]
    _add("result_dir_write", result_dir, True,
         lambda: _write_probe(result_dir))
    _add("stream_dir_write", stream_dir, True,
         lambda: _write_probe(stream_dir))
    if result_path:
        # Positive typed round trip: write a nonce-bound result document
        # and read it back under THIS identity (never infer from modes).
        result_nonce = str(nonce or "")
        typed = {"profile": "owner-exec-effective-access",
                 "uid": os.getuid(), "nonce": result_nonce}
        written = [False]

        def _write_typed_result():
            # type: () -> bool
            try:
                with open(result_path, "w", encoding="utf-8") as fh:
                    json.dump(typed, fh, sort_keys=True)
                written[0] = True
                return True
            except Exception:
                written[0] = False
                return False

        def _read_typed_result():
            # type: () -> bool
            if not written[0]:
                return False
            try:
                with open(result_path, "r", encoding="utf-8") as fh:
                    back = json.load(fh)
                return bool(isinstance(back, dict)
                            and str(back.get("nonce", "")) == result_nonce
                            and int(back.get("uid", -1) or -1)
                            == os.getuid())
            except Exception:
                return False

        _add("result_write", result_path, True, _write_typed_result)
        _add("result_read", result_path, True, _read_typed_result)
    report["reasons"] = [
        "%s failed (%s)" % (label, entry.get("path", ""))
        for label, entry in report["checks"].items()
        if entry.get("required") and not entry.get("ok")]
    report["ok"] = not report["reasons"]
    return report


def verify_report(report):
    # type: (object) -> Tuple[bool, List[str]]
    """Verify one probe/acceptance report. ONE definition (CLI + deploy)."""
    if isinstance(report, dict) and report.get("ok") is True:
        return True, []
    if isinstance(report, dict):
        reasons = report.get("reasons", [])
        if isinstance(reasons, list):
            return False, [str(r) for r in reasons][:10]
    return False, ["malformed credential report"]


def _default_command_runner(argv, env=None, timeout_s=60.0):
    # type: (object, object, object) -> Dict[str, Any]
    """Bounded subprocess boundary (never a persistent shell)."""
    import subprocess

    try:
        proc = subprocess.run(
            list(argv), env=env, capture_output=True, text=True,
            timeout=max(1.0, float(timeout_s)),
            stdin=subprocess.DEVNULL)
        return {"returncode": int(proc.returncode),
                "stdout": proc.stdout or "", "stderr": proc.stderr or ""}
    except Exception as exc:
        return {"returncode": 255, "stdout": "",
                "stderr": type(exc).__name__}


def _bus_reachable(socket_path, timeout_s=2.0):
    # type: (str, float) -> bool
    """Positive user-bus proof: connect to the manager's socket."""
    try:
        import socket as _socket

        path = str(socket_path or "")
        if not path or not os.path.exists(path):
            return False
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        try:
            sock.settimeout(max(0.1, float(timeout_s)))
            sock.connect(path)
            return True
        finally:
            sock.close()
    except Exception:
        return False


def _su_transient_argv(owner, runtime_dir, command):
    # type: (str, str, object) -> List[str]
    """Run a bounded command as the owner with the user-manager bus set.

    The command list is shell-quoted and passed as ONE `su -c` string so
    no unquoted token can split or inject; the bus env is explicit (the
    already-running user manager's vector, not a fresh login)."""
    import shlex

    inner = " ".join(
        [("XDG_RUNTIME_DIR=%s" % shlex.quote(str(runtime_dir or "")))] +
        [shlex.quote(str(part)) for part in command])
    return ["su", "-s", "/bin/bash", str(owner), "-c", inner]


def _acceptance_launch_command(unit, release, python, owner, group, paths,
                               payload_path, result_path, stream_path,
                               report_path, nonce, runtime_max_s, timeout_s):
    # type: (...) -> List[str]
    import shlex

    return (["timeout", str(int(timeout_s))] + shlex.split(SYSTEMD_USER_RUN)
            + ["--wait", "--collect", "--quiet",
               "--unit=%s" % unit,
               "--property=RuntimeMaxSec=%d" % int(runtime_max_s),
               "--working-directory=%s" % release,
               str(python), "-m", "backend.app.owner_env", "probe",
               "--owner", str(owner), "--group", str(group),
               "--state-dir", paths["state_dir"],
               "--log-dir", paths["log_dir"],
               "--backup-dir", paths["backup_dir"],
               "--config-dir", paths["config_dir"],
               "--config-file", paths["config_file"],
               "--secrets-file", paths["secrets_file"],
               "--inventory-file", paths["inventory_file"],
               "--payload", payload_path, "--result", result_path,
               "--stream", stream_path, "--nonce", nonce,
               "--report-out", report_path])


def _unit_termination_proven(proc):
    # type: (object) -> Tuple[bool, str]
    """Positive termination proof from `systemctl --user show`.

    Proven only when the unit is inactive, or already unloaded after a
    successful `--wait` run (not-found is impossible for a unit that was
    just launched). Any active/activating/failed/unparseable state fails.
    """
    try:
        code = int((proc or {}).get("returncode", 255))
    except Exception:
        code = 255
    stdout = str((proc or {}).get("stdout", "") or "")
    stderr = str((proc or {}).get("stderr", "") or "")
    fields = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    active = fields.get("ActiveState", "")
    sub = fields.get("SubState", "")
    if code == 0:
        if active == "inactive":
            return True, "%s/%s" % (active, sub or "dead")
        return False, active or "unknown"
    lowered = (stdout + " " + stderr).lower()
    if "not found" in lowered or "could not be found" in lowered:
        return True, "not-found"
    return False, "unproven"


def transient_acceptance(settings=None, paths=None, owner="", group="",
                         state_dir="", log_dir="", backup_dir="",
                         config_dir="", config_file="", secrets_file="",
                         inventory_file="", work_dir="", work_root="",
                         bus_path="", release_root="", venv_python="",
                         command_runner=None, timeout_s=None,
                         runtime_max_s=None, keep_work=False):
    # type: (...) -> Dict[str, Any]
    """Canonical owner transient round-trip acceptance (W4-D8).

    Deterministic, read-only w.r.t. managed tools, bounded, secret-safe.
    The report contains booleans/ids/paths only; probe report reasons
    never include file contents. This is the ONE primitive behind
    `cli status --require-ready` stage `owner_transient_execution`
    (install.sh and upgrade.sh consume only the gate exit code) and the
    `owner_env accept` CLI (VM acceptance harness).
    """
    result = {
        "ok": False, "profile": TRANSIENT_ACCEPTANCE_PROFILE,
        "owner": "", "uid": None, "bus_path": "", "bus_reachable": False,
        "unit": "", "launch_exit": None, "report_ok": False,
        "identity_ok": False, "result_ok": False,
        "unit_terminated": False, "unit_state": "", "checks": {},
        "reasons": [],
    }  # type: Dict[str, Any]
    reasons = result["reasons"]  # type: List[str]
    owner = _resolve_owner(owner, settings)
    result["owner"] = owner
    uid = _grantable_uid(owner)
    if uid is None:
        reasons.append("owner account unresolvable")
        return result
    result["uid"] = uid
    try:
        bound = float(timeout_s) if timeout_s is not None \
            else TRANSIENT_ACCEPTANCE_TIMEOUT_S
    except (TypeError, ValueError):
        bound = TRANSIENT_ACCEPTANCE_TIMEOUT_S
    try:
        runtime_max = int(runtime_max_s) if runtime_max_s is not None \
            else TRANSIENT_ACCEPTANCE_RUNTIME_MAX_S
    except (TypeError, ValueError):
        runtime_max = TRANSIENT_ACCEPTANCE_RUNTIME_MAX_S
    runner = command_runner or _default_command_runner
    bus = str(bus_path or "") or ("/run/user/%d/bus" % uid)
    result["bus_path"] = bus
    if not _bus_reachable(bus):
        reasons.append("user manager bus unavailable (%s)" % bus)
        return result
    result["bus_reachable"] = True
    p = _access_paths(settings, paths, state_dir, log_dir, backup_dir,
                      config_dir, config_file, secrets_file,
                      inventory_file)
    try:
        resolved = resolved_paths(settings)
    except Exception:
        resolved = {}
    release = str(release_root or resolved.get("release_root", "")
                  or DEFAULT_RELEASE_LINK)
    python = str(venv_python or resolved.get("venv_python", "")
                 or sys.executable)
    created_work = False
    work = str(work_dir or "")
    if not work:
        try:
            import tempfile
            work = tempfile.mkdtemp(
                prefix="ega-owner-acceptance-",
                dir=str(work_root or "") or None)
            created_work = True
        except Exception as exc:
            reasons.append("acceptance work dir unavailable (%s)"
                           % type(exc).__name__)
            return result
    try:
        os.makedirs(work, mode=0o700, exist_ok=True)
        os.chmod(work, 0o700)
        if os.geteuid() == 0:
            os.chown(work, uid, uid)
    except Exception:
        pass
    nonce = os.urandom(8).hex()
    unit = "ega-update-accept-%d-%s.service" % (
        os.getpid(), os.urandom(4).hex())
    result["unit"] = unit
    payload_path = os.path.join(work, "payload.json")
    result_path = os.path.join(work, "result.json")
    stream_path = os.path.join(work, "stream.jsonl")
    report_path = os.path.join(work, "report.json")
    try:
        with open(payload_path, "w", encoding="utf-8") as fh:
            json.dump({"profile": TRANSIENT_ACCEPTANCE_PROFILE,
                       "nonce": nonce, "uid": uid}, fh, sort_keys=True)
        os.chmod(payload_path, 0o644)
        if os.geteuid() == 0:
            os.chown(payload_path, uid, uid)
    except Exception as exc:
        reasons.append("cannot stage acceptance payload (%s)"
                       % type(exc).__name__)
        return result
    launch = _acceptance_launch_command(
        unit, release, python, owner, resolve_shared_group(
            settings, None, group), p, payload_path, result_path,
        stream_path, report_path, nonce, runtime_max, bound)
    proc = runner(_su_transient_argv(owner, os.path.dirname(bus), launch),
                  None, bound)
    try:
        launch_exit = int(proc.get("returncode", 255))
    except Exception:
        launch_exit = 255
    result["launch_exit"] = launch_exit
    if launch_exit != 0:
        reasons.append("transient unit launch failed (exit %s)" % launch_exit)
    report = {}
    try:
        with open(report_path, "r", encoding="utf-8") as fh:
            report = json.load(fh)
    except Exception:
        report = {}
    report_ok, report_reasons = verify_report(report)
    result["report_ok"] = bool(report_ok)
    if not report_ok:
        reasons.append("transient acceptance report not ok")
        for entry in report_reasons[:5]:
            reasons.append("report: %s" % entry)
    report_checks = report.get("checks", {}) if isinstance(report, dict) \
        else {}
    checks = {}  # type: Dict[str, bool]
    for name in TRANSIENT_ACCEPTANCE_REQUIRED_CHECKS:
        entry = (report_checks or {}).get(name)
        checks[name] = bool(isinstance(entry, dict) and entry.get("ok"))
    result["checks"] = checks
    failed_checks = [name for name in TRANSIENT_ACCEPTANCE_REQUIRED_CHECKS
                     if not checks[name]]
    for name in failed_checks:
        reasons.append("effective access check failed: %s" % name)
    identity_ok = False
    if isinstance(report, dict):
        try:
            identity_ok = (int(report.get("uid", -1) or -1) == uid
                           and str(report.get("user", "")) == owner)
        except Exception:
            identity_ok = False
    result["identity_ok"] = bool(identity_ok)
    if not identity_ok:
        reasons.append("effective owner identity mismatch")
    result_ok = False
    try:
        with open(result_path, "r", encoding="utf-8") as fh:
            written = json.load(fh)
        result_ok = bool(
            isinstance(written, dict)
            and str(written.get("nonce", "")) == nonce
            and int(written.get("uid", -1) or -1) == uid)
    except Exception:
        result_ok = False
    result["result_ok"] = bool(result_ok)
    if not result_ok:
        reasons.append("transient result not readable/writable")
    if launch_exit == 0:
        term = runner(_su_transient_argv(
            owner, os.path.dirname(bus),
            ["systemctl", "--user", "show",
             "--property=ActiveState,SubState,Result", unit]), None, 10.0)
        proven, state = _unit_termination_proven(term)
        result["unit_terminated"] = bool(proven)
        result["unit_state"] = state
        if not proven:
            reasons.append(
                "transient unit termination not positively proven (%s)"
                % state)
    else:
        result["unit_state"] = "launch-failed"
    result["ok"] = bool(
        result["bus_reachable"] and launch_exit == 0
        and result["report_ok"] and result["identity_ok"]
        and result["result_ok"] and result["unit_terminated"]
        and not failed_checks)
    if created_work and not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    return result


def main(argv=None):
    # type: (object) -> int
    """Owner-execution credential CLI (stdlib-only; deploy scripts use it).

    Subcommands:
      provision  Apply the explicit effective-access contract (ACLs).
      probe      Emit a redacted effective-credential/access report.
      verify     Exit 0 only when a probe report says ok.
      accept     Run the canonical owner transient acceptance round trip
                 (user bus -> transient unit -> identity/path/result
                 proof -> positive termination) and print its report.
    """
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="backend.app.owner_env")
    sub = ap.add_subparsers(dest="command", required=True)

    def _common(parser):
        # type: (object) -> None
        parser.add_argument("--owner", default="")
        parser.add_argument("--group", default="")
        parser.add_argument("--state-dir", default="")
        parser.add_argument("--log-dir", default="")
        parser.add_argument("--backup-dir", default="")
        parser.add_argument("--config-dir", default="")
        parser.add_argument("--config-file", default="")
        parser.add_argument("--secrets-file", default="")
        parser.add_argument("--inventory-file", default="")

    p_provision = sub.add_parser("provision")
    _common(p_provision)
    p_probe = sub.add_parser("probe")
    _common(p_probe)
    p_probe.add_argument("--payload", default="")
    p_probe.add_argument("--result", default="")
    p_probe.add_argument("--stream", default="")
    p_probe.add_argument("--nonce", default="")
    p_probe.add_argument("--report-out", default="")
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--report", required=True)
    p_accept = sub.add_parser("accept")
    _common(p_accept)
    p_accept.add_argument("--bus", default="")
    p_accept.add_argument("--release-root", default="")
    p_accept.add_argument("--venv-python", default="")
    p_accept.add_argument("--timeout", type=float, default=None)
    p_accept.add_argument("--runtime-max-sec", type=int, default=None)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else 1

    if args.command == "verify":
        try:
            with open(args.report, "r", encoding="utf-8") as fh:
                report = json.load(fh)
        except Exception as exc:
            sys.stderr.write("credential report unreadable: %s\n" % exc)
            return 1
        ok, reasons = verify_report(report)
        if ok:
            sys.stdout.write("ok\n")
            return 0
        summary = {"reasons": reasons}
        sys.stderr.write("credential report not ok: %s\n"
                         % json.dumps(summary, sort_keys=True))
        return 1

    kwargs = {
        "owner": args.owner, "group": args.group,
        "state_dir": args.state_dir, "log_dir": args.log_dir,
        "backup_dir": args.backup_dir, "config_dir": args.config_dir,
        "config_file": args.config_file, "secrets_file": args.secrets_file,
        "inventory_file": args.inventory_file,
    }
    if args.command == "provision":
        report = provision_owner_access(**kwargs)
        sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
        return 0 if report.get("ok") else 1

    if args.command == "accept":
        report = transient_acceptance(
            **kwargs, bus_path=args.bus, release_root=args.release_root,
            venv_python=args.venv_python, timeout_s=args.timeout,
            runtime_max_s=args.runtime_max_sec)
        sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
        return 0 if report.get("ok") else 1

    report = execution_credentials_probe(
        **kwargs, payload_path=args.payload, result_path=args.result,
        stream_path=args.stream, nonce=args.nonce)
    if args.report_out:
        try:
            parent = os.path.dirname(os.path.abspath(args.report_out))
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            tmp = "%s.tmp-%d" % (args.report_out, os.getpid())
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(report, fh, sort_keys=True)
            os.replace(tmp, args.report_out)
        except OSError as exc:
            sys.stderr.write("cannot write credential report: %s\n" % exc)
            return 1
    sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
