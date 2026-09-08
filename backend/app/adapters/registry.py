"""Tool registry + shared fail-closed helpers (subagent B owned).

Concrete adapters implement base.Adapter; this module maps tool ids to
adapter instances, records expected executables, and hosts the shared
subprocess / fingerprint / disk / SemVer helpers so per-adapter files stay
thin. No live probes run at import time.

All subprocesses use fixed argument arrays with shell=False. Browser input
never supplies paths, env, service names, URLs, or argv. Configured absolute
paths are validated before any mutation. The configured NVM runtime is used
for node/npm/npx; shell profiles are never sourced and HOME/CODEX_HOME are
never repurposed.
Python 3.10 compatible.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from typing import Dict, List, Optional, Tuple

TOOL_IDS = ("hermes", "opencode", "codex", "t3")
OPTIONAL_DISABLED = ("claude",)

# Script exit codes (spec_scripts.md S4) reused by runner + adapters.
EXIT_OK = 0
EXIT_INVALID = 2
EXIT_BLOCKED = 3
EXIT_INSTALL_FAILED = 4
EXIT_VERIFY_FAILED = 5
EXIT_INTERRUPTED = 6

SCHEMA_VERSION = 1

# Disk policy: spec_scripts.md S10 supersedes SPEC S9 example.
DISK_FLOOR_BYTES = 3 * 1024 * 1024 * 1024
DISK_RESERVE_BYTES = 1 * 1024 * 1024 * 1024

# Bound per-stream capture for probes so a chatty helper cannot OOM the worker.
CAPTURE_LIMIT_BYTES = 256 * 1024

# Shared contract: adapter mutation subprocess timeout = step_timeout +
# MUTATION_TIMEOUT_MARGIN_S (120s). Applies to mutation calls ONLY (the
# single mutating subprocess per adapter execute()); read-only probes keep
# their fixed bounded timeouts and never add this margin. The runner still
# enforces its own hard per-step ceiling centrally; adapters enforce their
# own margin locally.
MUTATION_TIMEOUT_MARGIN_S = 120


def mutation_timeout(step_timeout, default=1800):
    # type: (object, int) -> int
    """Mutation timeout for adapter execute(): step_timeout + margin.

    Fail-closed to default + margin when the step value is missing or
    non-numeric. Callers pass plan.timeouts.get(step, default).
    Python 3.10 compatible.
    """
    try:
        base = int(step_timeout)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        try:
            base = int(default)
        except (TypeError, ValueError):
            base = 1800
    return base + MUTATION_TIMEOUT_MARGIN_S


def mutation_timeout_for(plan, step="updating", default=1800):
    # type: (object, str, int) -> int
    """Extract step timeout from a PlanResult (or mapping) + margin."""
    try:
        timeouts = getattr(plan, "timeouts", None)
        if isinstance(timeouts, dict) and step in timeouts:
            return mutation_timeout(timeouts.get(step), default=default)
        if isinstance(plan, dict):
            nested = plan.get("timeouts", {})
            if isinstance(nested, dict) and step in nested:
                return mutation_timeout(nested.get(step), default=default)
    except Exception:
        pass
    return mutation_timeout(default, default=default)


def attach_timed_out(result, timed_out):
    # type: (object, bool) -> object
    """Attach timed_out flag to an ExecuteResult (registry-owned result path).

    base.ExecuteResult carries a real timed_out field; this helper assigns it
    (with a dynamic fallback) so adapter code has one call site for both.
    """
    try:
        object.__setattr__(result, "timed_out", bool(timed_out))
    except Exception:
        try:
            result.__dict__["timed_out"] = bool(timed_out)  # type: ignore[attr-defined]
        except Exception:
            pass
    return result

EXPECTED_EXECUTABLES = {
    "hermes": "/home/ubuntu/.local/bin/hermes",
    "opencode": "/home/ubuntu/.opencode/bin/opencode",
    "codex": "/home/ubuntu/.local/bin/codex",
    "t3": "/home/ubuntu/.nvm/versions/node/v24.18.0/bin/npx",
    "claude": "/usr/local/bin/claude",
}

NVM_NODE = "/home/ubuntu/.nvm/versions/node/v24.18.0/bin/node"
NVM_NPM = "/home/ubuntu/.nvm/versions/node/v24.18.0/bin/npm"
NVM_NPX = "/home/ubuntu/.nvm/versions/node/v24.18.0/bin/npx"

_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$"
)


class ProcResult(object):
    """Bounded result of a fixed-argv subprocess probe."""

    def __init__(self, argv, exit_code, stdout, stderr, timed_out=False):
        # type: (List[str], int, str, str, bool) -> None
        self.argv = list(argv)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out

    def ok(self):
        # type: () -> bool
        return (not self.timed_out) and self.exit_code == 0


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
        raise ValueError("executable must be an absolute path: %r" % (clean[0],))
    return clean


def run_fixed(argv, timeout=60, cwd=None):
    # type: (List[str], int, Optional[str]) -> ProcResult
    """Run a fixed argument array with shell=False.

    Never raises on nonzero exit; preserves the exit status and parses the
    full bounded output. Missing executable and timeouts are reported in the
    result (exit 127 / timed_out=True), never as an unhandled exception, so
    callers can distinguish absence from failure.
    """
    argv = _check_argv(argv)
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"")[:CAPTURE_LIMIT_BYTES]
        err = (exc.stderr or b"")[:CAPTURE_LIMIT_BYTES]
        return ProcResult(
            argv, 124,
            out.decode("utf-8", errors="replace"),
            err.decode("utf-8", errors="replace"),
            timed_out=True,
        )
    except FileNotFoundError as exc:
        return ProcResult(argv, 127, "", "missing executable: %s" % exc)
    except OSError as exc:
        return ProcResult(argv, 127, "", "spawn failed: %s" % exc)
    out = (proc.stdout or b"")[:CAPTURE_LIMIT_BYTES]
    err = (proc.stderr or b"")[:CAPTURE_LIMIT_BYTES]
    return ProcResult(
        argv, proc.returncode,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


def resolve_executable(path):
    # type: (str) -> Tuple[bool, str, str]
    """Validate a configured absolute path. Returns (ok, resolved, detail)."""
    if not isinstance(path, str) or not os.path.isabs(path):
        return False, "", "not an absolute path: %r" % (path,)
    try:
        st = os.stat(path)
    except OSError as exc:
        return False, "", "stat failed for %s: %s" % (path, exc)
    if not stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode):
        # stat follows symlinks; non-regular targets (dirs, sockets) are unsupported.
        if not stat.S_ISREG(st.st_mode):
            return False, "", "not a regular file: %s" % path
    if not os.access(path, os.X_OK):
        return False, "", "not executable: %s" % path
    try:
        resolved = os.path.realpath(path)
    except OSError as exc:
        return False, "", "readlink failed for %s: %s" % (path, exc)
    owner = "%d:%d" % (st.st_uid, st.st_gid)
    return True, resolved, "owner=%s mode=%o" % (owner, stat.S_IMODE(st.st_mode))


def fingerprint(*parts):
    # type: (*str) -> str
    h = hashlib.sha256()
    for part in parts:
        h.update((part or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def required_space_bytes(staging_estimate, backup_estimate, floor=None, reserve=None):
    # type: (Optional[int], Optional[int], Optional[int], Optional[int]) -> Optional[int]
    """max(floor, staging + backup + reserve). None estimate blocks (None)."""
    fl = DISK_FLOOR_BYTES if floor is None else floor
    rv = DISK_RESERVE_BYTES if reserve is None else reserve
    if staging_estimate is None or backup_estimate is None:
        return None
    try:
        need = int(staging_estimate) + int(backup_estimate) + int(rv)
    except (TypeError, ValueError):
        return None
    return max(int(fl), need)


def filesystems_for(paths):
    # type: (List[str]) -> List[str]
    """Unique existing ancestor directories covering each affected path."""
    seen = []
    for path in paths:
        probe = path if isinstance(path, str) and path else "/"
        if not os.path.isabs(probe):
            probe = "/"
        cur = probe
        while cur and not os.path.exists(cur):
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        if not cur:
            cur = "/"
        if cur not in seen:
            seen.append(cur)
    return seen or ["/"]


def check_disk(paths, required_bytes):
    # type: (List[str], Optional[int]) -> Tuple[bool, str, List[Dict[str, object]]]
    """Check free space on every affected FS. Unknown estimates block.

    Returns (ok, detail, per_fs). ok=False with 'unknown' in detail means the
    estimate or measurement was unavailable and the caller must block.
    """
    if required_bytes is None:
        return False, "unknown space estimate blocks mutation", []
    per_fs = []
    try:
        need = int(required_bytes)
    except (TypeError, ValueError):
        return False, "unknown space estimate blocks mutation", []
    for mount in filesystems_for(paths):
        try:
            usage = shutil.disk_usage(mount)
        except OSError as exc:
            return False, "unknown free space on %s blocks mutation: %s" % (mount, exc), per_fs
        per_fs.append({"path": mount, "free": usage.free, "total": usage.total})
        if usage.free < need:
            return False, "disk_blocked: %s free %d < required %d" % (mount, usage.free, need), per_fs
    return True, "free space ok on %d fs for required %d" % (len(per_fs), need), per_fs


def parse_semver(text):
    # type: (str) -> Optional[Tuple[int, int, int, str, str]]
    """Strict SemVer parse. Returns None on malformed input (fail-closed)."""
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if cleaned[:1].lower() == "v":
        cleaned = cleaned[1:]
    match = _SEMVER_RE.match(cleaned)
    if not match:
        return None
    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
    return (major, minor, patch, match.group(4) or "", match.group(5) or "")


def compare_semver(left, right):
    # type: (str, str) -> Optional[int]
    """-1/0/1 on valid pairs; None when either side is malformed."""
    parsed_l = parse_semver(left)
    parsed_r = parse_semver(right)
    if parsed_l is None or parsed_r is None:
        return None
    for idx in (0, 1, 2):
        if parsed_l[idx] != parsed_r[idx]:
            return -1 if parsed_l[idx] < parsed_r[idx] else 1
    # Release outranks prerelease; prerelease comparison is lexical and only
    # used to reject downgrades conservatively.
    if parsed_l[3] == parsed_r[3]:
        return 0
    if not parsed_l[3]:
        return 1
    if not parsed_r[3]:
        return -1
    return -1 if parsed_l[3] < parsed_r[3] else (1 if parsed_l[3] > parsed_r[3] else 0)


def extract_version_token(text):
    # type: (str) -> str
    """First SemVer-like token in bounded probe output, else ''."""
    if not text:
        return ""
    for token in re.findall(r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", text):
        if parse_semver(token) is not None:
            cleaned = token[1:] if token[:1] == "v" else token
            return cleaned
    return ""


def nvm_paths():
    # type: () -> Dict[str, str]
    """Configured NVM runtime paths; settings override the audited defaults."""
    try:
        from ..config import settings as _settings

        return {
            "node": _settings.node_path or NVM_NODE,
            "npm": _settings.npm_path or NVM_NPM,
            "npx": _settings.npx_path or NVM_NPX,
        }
    except Exception:
        return {"node": NVM_NODE, "npm": NVM_NPM, "npx": NVM_NPX}


def validate_nvm_runtime():
    # type: () -> Tuple[bool, str]
    """Confirm the configured node/npm/npx absolute paths exist and execute."""
    paths = nvm_paths()
    for key in ("node", "npm", "npx"):
        ok, resolved, detail = resolve_executable(paths[key])
        if not ok:
            return False, "nvm %s invalid (%s): %s" % (key, paths[key], detail)
    return True, "nvm runtime ok: %s" % json.dumps(paths, sort_keys=True)


def get_adapter(tool_id):
    # type: (str) -> object
    """Return the adapter instance for a tool id. Raises KeyError otherwise."""
    if tool_id == "hermes":
        from .hermes import HermesAdapter

        return HermesAdapter()
    if tool_id == "opencode":
        from .opencode import OpenCodeAdapter

        return OpenCodeAdapter()
    if tool_id == "codex":
        from .codex import CodexAdapter

        return CodexAdapter()
    if tool_id == "t3":
        from .t3 import T3Adapter

        return T3Adapter()
    if tool_id == "claude":
        from .claude import ClaudeAdapter

        return ClaudeAdapter()
    raise KeyError("invalid_request: unknown tool %r" % (tool_id,))


def build_registry():
    # type: () -> Dict[str, object]
    """Fresh adapter instances keyed by tool id (claude included, disabled)."""
    return {
        "hermes": get_adapter("hermes"),
        "opencode": get_adapter("opencode"),
        "codex": get_adapter("codex"),
        "t3": get_adapter("t3"),
        "claude": get_adapter("claude"),
    }


TOOL_REGISTRY = None  # type: Optional[Dict[str, object]]


def registry():
    # type: () -> Dict[str, object]
    """Lazily built registry so importing this module never runs probes."""
    global TOOL_REGISTRY
    if TOOL_REGISTRY is None:
        TOOL_REGISTRY = build_registry()
    return TOOL_REGISTRY


def four_tool_registry():
    # type: () -> Dict[str, object]
    reg = registry()
    return {key: reg[key] for key in TOOL_IDS if key in reg}
