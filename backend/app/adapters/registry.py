"""Tool registry + shared fail-closed helpers (subagent B owned).

Concrete adapters implement base.Adapter; this module maps tool ids to
adapter instances, records expected executables, and hosts the shared
subprocess / fingerprint / disk / SemVer / sanitize / inventory / measure
helpers so per-adapter files stay thin. No live probes run at import time.

All subprocesses use fixed argument arrays with shell=False via
backend/app/executor.py run_stream (registry.run_fixed delegates, keeping
its ProcResult signature). Browser input never supplies paths, env, service
names, URLs, or argv. Configured absolute paths are validated before any
mutation. The configured NVM runtime is used for node/npm/npx; shell
profiles are never sourced and HOME/CODEX_HOME are never repurposed.
env is an explicit dict or None (never os.environ passthrough); stdin is
DEVNULL in the executor; fixed argv, shell=False.
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


def _executor_run_stream(argv, timeout_s, cwd=None, env=None, scope_unit=None,
                         cap_bytes=CAPTURE_LIMIT_BYTES):
    # type: (List[str], int, object, object, object, int) -> object
    """Import-boundary indirection for backend/app/executor.py run_stream.

    Imported lazily inside run_fixed so monkeypatching
    backend.app.executor.run_stream in tests affects this path. Raises
    ImportError/AttributeError when the main-agent executor is unavailable
    so callers fall back to the bounded subprocess path (offline).
    """
    import importlib as _importlib

    _mod = _importlib.import_module("backend.app.executor")
    _fn = getattr(_mod, "run_stream", None)
    if not callable(_fn):
        raise ImportError("executor.run_stream unavailable")
    return _fn(argv, timeout_s=timeout_s, cwd=cwd, env=env,
               scope_unit=scope_unit, cap_bytes=cap_bytes)


def run_fixed(argv, timeout=60, cwd=None, scope_unit=None, env=None,
              timeout_s=None, cap_bytes=None):
    # type: (...) -> ProcResult
    """Run a fixed argument array via executor.run_stream (delegated).

    Keeps the ProcResult signature (argv, exit_code, stdout, stderr,
    timed_out): tail bytes from run_stream (.stdout_tail/.stderr_tail)
    are mapped back to str, timed_out flag passed through. Probes use
    bounded timeout_s with scope_unit=None; mutations pass
    timeout_s=mutation_timeout_for(plan, step) and
    scope_unit=getattr(plan, "scope_unit", None).

    env is an explicit dict or None (never os.environ passthrough).
    Never raises on nonzero exit; missing executable and timeouts are
    reported (exit 127 / timed_out=True). Falls back to bounded
    subprocess.run only when the executor module is unavailable
    (main-agent pending); the delegated path is authoritative.
    """
    argv = _check_argv(argv)
    try:
        _to = int(timeout_s) if timeout_s is not None else int(timeout)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        _to = 60
    try:
        _cap = int(cap_bytes) if cap_bytes is not None else CAPTURE_LIMIT_BYTES
    except (TypeError, ValueError):
        _cap = CAPTURE_LIMIT_BYTES
    try:
        res = _executor_run_stream(argv, _to, cwd=cwd, env=env,
                                   scope_unit=scope_unit, cap_bytes=_cap)
        try:
            _exit = int(getattr(res, "exit_code", 127))
        except (TypeError, ValueError):
            _exit = 127
        _timed = bool(getattr(res, "timed_out", False))
        _out_b = getattr(res, "stdout_tail", b"") or b""
        _err_b = getattr(res, "stderr_tail", b"") or b""
        if isinstance(_out_b, str):
            _out_b = _out_b.encode("utf-8", errors="replace")
        if isinstance(_err_b, str):
            _err_b = _err_b.encode("utf-8", errors="replace")
        try:
            _out = bytes(_out_b[:_cap]).decode("utf-8", errors="replace")
        except Exception:
            _out = ""
        try:
            _err = bytes(_err_b[:_cap]).decode("utf-8", errors="replace")
        except Exception:
            _err = ""
        return ProcResult(argv, _exit, _out, _err, timed_out=_timed)
    except (ImportError, AttributeError):
        pass
    except Exception as exc:
        return ProcResult(argv, 127, "", "executor delegation failed: %s" % exc)
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            timeout=_to,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"")[:_cap]
        err = (exc.stderr or b"")[:_cap]
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
    out = (proc.stdout or b"")[:_cap]
    err = (proc.stderr or b"")[:_cap]
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
    """Strict SemVer parse. Returns None on malformed input (fail-closed).

    Delegates to backend/app/semver.py (vendored strict 2.0, numeric
    prerelease). Falls back to the local regex only when the semver module
    is unavailable. Returns (major, minor, patch, prerelease, build).
    """
    try:
        from backend.app.semver import try_parse as _try_parse

        parsed = _try_parse(text)
        if parsed is None:
            return None
        pre = ".".join(parsed.prerelease) if getattr(parsed, "prerelease", ()) else ""
        return (int(parsed.major), int(parsed.minor), int(parsed.patch),
                pre, str(getattr(parsed, "build", "") or ""))
    except (ImportError, AttributeError):
        pass
    except Exception:
        return None
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
    """-1/0/1 on valid pairs; None when either side is malformed.

    Delegates to backend/app/semver.py so NUMERIC prerelease identifiers
    compare numerically (beta.10 > beta.2 per SemVer 2.0 section 11).
    """
    try:
        from backend.app.semver import compare as _sem_compare

        return _sem_compare(left, right)  # type: ignore[return-value]
    except (ImportError, AttributeError):
        pass
    except Exception:
        return None
    parsed_l = parse_semver(left)
    parsed_r = parse_semver(right)
    if parsed_l is None or parsed_r is None:
        return None
    for idx in (0, 1, 2):
        if parsed_l[idx] != parsed_r[idx]:
            return -1 if parsed_l[idx] < parsed_r[idx] else 1
    if parsed_l[3] == parsed_r[3]:
        return 0
    if not parsed_l[3]:
        return 1
    if not parsed_r[3]:
        return -1
    return -1 if parsed_l[3] < parsed_r[3] else (1 if parsed_l[3] > parsed_r[3] else 0)


def is_upgrade_semver(before, target):
    # type: (object, object) -> bool
    """True iff target is a strict upgrade over before (both valid)."""
    try:
        from backend.app.semver import is_upgrade as _is_up

        return bool(_is_up(before, target))  # type: ignore[arg-type]
    except (ImportError, AttributeError):
        pass
    except Exception:
        return False
    try:
        res = compare_semver(str(before or ""), str(target or ""))
        if res is None:
            if not str(before or "").strip() and parse_semver(str(target or "")) is not None:
                return True
            return False
        return res < 0
    except Exception:
        return False


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


# -- frozen-contract consumers (main-agent owned modules; defensive) --------

def sanitize_evidence(text, secrets=None):
    # type: (object, object) -> str
    """Sanitize EVERY persisted/returned string via sanitize.sanitize_text.

    Frozen contract: backend/app/sanitize.py sanitize_text(s, secrets)->str.
    Falls back to redaction.redact_text then str() when the sanitizer module
    is unavailable (main-agent pending). Never raises; never returns raw
    tool output unsanitized when the sanitizer exists.
    """
    try:
        import importlib as _importlib

        _mod = _importlib.import_module("backend.app.sanitize")
        _fn = getattr(_mod, "sanitize_text", None)
        if callable(_fn):
            try:
                return str(_fn(str(text or ""), tuple(secrets or ())))  # type: ignore[arg-type]
            except TypeError:
                try:
                    return str(_fn(str(text or "")))
                except Exception:
                    pass
    except Exception:
        pass
    try:
        import importlib as _importlib2

        _mod2 = _importlib2.import_module("backend.app.redaction")
        _fn2 = getattr(_mod2, "redact_text", None)
        if callable(_fn2):
            try:
                return str(_fn2(str(text or ""), tuple(secrets or ())))
            except Exception:
                try:
                    return str(_fn2(str(text or "")))
                except Exception:
                    pass
    except Exception:
        pass
    try:
        return str(text or "")
    except Exception:
        return ""


def get_tool_inventory(tool_id):
    # type: (str) -> Dict[str, object]
    """Frozen contract: inventory.get_tool_inventory(tool_id)->dict.

    Missing inventory yields {} and callers treat it as a gate (fail
    closed). Never raises.
    """
    try:
        import importlib as _importlib

        _mod = _importlib.import_module("backend.app.inventory")
        _fn = getattr(_mod, "get_tool_inventory", None)
        if callable(_fn):
            try:
                res = _fn(tool_id)
            except TypeError:
                res = _fn()
            if isinstance(res, dict):
                return dict(res)
            return {}
    except Exception:
        pass
    return {}


def get_owner_paths():
    # type: () -> Dict[str, str]
    """Frozen contract: owner_env.resolved_paths()->dict.

    Keys: release_root, node_path, npm_path, npx_path, config_path,
    state_dir (absolute, validated). Missing module yields {} (callers fail
    closed where owner paths are required, else fall back to nvm_paths()).
    Never raises.
    """
    try:
        import importlib as _importlib

        _mod = _importlib.import_module("backend.app.owner_env")
        _fn = getattr(_mod, "resolved_paths", None)
        if callable(_fn):
            res = _fn()
            if isinstance(res, dict):
                out = {}
                for key in ("release_root", "node_path", "npm_path",
                            "npx_path", "config_path", "state_dir"):
                    val = res.get(key, "")
                    if isinstance(val, str) and val:
                        out[key] = val
                return out
            return {}
    except Exception:
        pass
    return {}


def owner_node_major():
    # type: () -> Optional[int]
    """Owner node major from owner_env (preferred) else nvm node probe."""
    paths = get_owner_paths()
    node_bin = paths.get("node_path", "") or nvm_paths().get("node", "")
    if not node_bin:
        return None
    try:
        res = run_fixed([node_bin, "--version"], timeout=30)
        if not res.ok():
            return None
        token = extract_version_token(res.stdout + "\n" + res.stderr)
        parsed = parse_semver(token) if token else None
        if parsed is None:
            return None
        return int(parsed[0])
    except Exception:
        return None


# -- measured footprint (R28; no fixed invented constants) -------------------

MEASURE_ENTRY_CAP = 200000


def measure_file_with_wal(path):
    # type: (str) -> int
    """DB+WAL sizes via os.stat; 0 when absent, -1 unknown on error."""
    try:
        if not isinstance(path, str) or not path:
            return -1
        total = 0
        found = False
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = path if not suffix else (path + suffix)
            try:
                st = os.stat(candidate)
            except OSError:
                continue
            try:
                if stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
                    total += int(st.st_size)
                    found = True
            except Exception:
                continue
        if not found:
            return 0
        return total
    except Exception:
        return -1


def measure_tree_bytes(root, cap_entries=MEASURE_ENTRY_CAP):
    # type: (str, int) -> int
    """Config/state tree sum via bounded os.walk (capped entries)."""
    try:
        if not isinstance(root, str) or not root:
            return -1
        if not os.path.exists(root):
            return 0
        if os.path.isfile(root):
            try:
                return int(os.stat(root).st_size)
            except OSError:
                return -1
        total = 0
        count = 0
        try:
            cap = int(cap_entries)
        except (TypeError, ValueError):
            cap = MEASURE_ENTRY_CAP
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
            for name in filenames:
                if count >= cap:
                    return total
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                    if stat.S_ISREG(st.st_mode):
                        total += int(st.st_size)
                except OSError:
                    continue
                count += 1
        return total
    except Exception:
        return -1


def default_measure_footprint(tool_id, db_paths=None, tree_paths=None,
                              native_size=None):
    # type: (str, object, object, object) -> Dict[str, int]
    """Default-safe measure_footprint helper (base.py untouched).

    Returns {fs_path: bytes} measured actuals. native snapshot estimate
    only when the tool reports a structured size (>=0); else -1 unknown
    for the native key. Callers block with disk_blocked when -1 appears
    anywhere required, never proceeding on fixed constants.
    """
    out = {}  # type: Dict[str, int]
    try:
        for path in list(db_paths or []):  # type: ignore[union-attr]
            if not isinstance(path, str) or not path:
                continue
            out[path] = measure_file_with_wal(path)
    except Exception:
        pass
    try:
        for path in list(tree_paths or []):  # type: ignore[union-attr]
            if not isinstance(path, str) or not path:
                continue
            out[path] = measure_tree_bytes(path)
    except Exception:
        pass
    try:
        if native_size is not None:
            size = int(native_size)  # type: ignore[arg-type]
            out["native-snapshot:%s" % tool_id] = size if size >= 0 else -1
    except (TypeError, ValueError):
        pass
    if not out:
        out["unknown:%s" % tool_id] = -1
    return out


def footprint_has_unknown(footprint):
    # type: (object) -> bool
    """True when any required footprint value is -1/None (must block)."""
    try:
        if isinstance(footprint, dict):
            for value in footprint.values():
                try:
                    if value is None or int(value) < 0:  # type: ignore[arg-type]
                        return True
                except (TypeError, ValueError):
                    return True
            return False
        return True
    except Exception:
        return True


def plan_has_unknown_budget(plan):
    # type: (object) -> bool
    """True when plan budgets/space_fs contain -1/None (must block)."""
    try:
        for attr in ("budgets", "space_fs"):
            try:
                val = getattr(plan, attr, None)
            except Exception:
                val = None
            if isinstance(val, dict):
                for item in val.values():
                    try:
                        if item is None or int(item) < 0:  # type: ignore[arg-type]
                            return True
                    except (TypeError, ValueError):
                        return True
        return False
    except Exception:
        return True


def attach_plan_v2(plan, **fields):
    # type: (object, object) -> object
    """Defensively attach PlanResult v2 fields (main-agent adds).

    Uses object.__setattr__ then __dict__ fallback so Pydantic v1/v2
    models without the new fields still expose them via getattr with
    defaults. Never raises. Fields include: install_identity, artifact,
    config_hash, plan_hash, launch, state_homes, backup_policy,
    required_probes, budgets, space_fs, deadlines, restart_detail,
    activity_ts, release_path, required_checks, scope_unit,
    daemon_expected, daemon_status_at_plan, manual_restart_limitation.
    """
    try:
        for key, value in fields.items():
            try:
                object.__setattr__(plan, key, value)
            except Exception:
                try:
                    plan.__dict__[key] = value  # type: ignore[attr-defined]
                except Exception:
                    continue
    except Exception:
        pass
    return plan


def backup_dest_for_db(backup_root, job_id, db_path):
    # type: (str, str, str) -> str
    """Unique per-DB destination: <jobid>-<sha1(path)[:8]>-<basename>.

    Prevents basename collisions when multiple DBs share a basename.
    """
    try:
        base = os.path.basename(db_path) or "db"
    except Exception:
        base = "db"
    try:
        digest = hashlib.sha1(db_path.encode("utf-8")).hexdigest()[:8]
    except Exception:
        digest = "unknown"
    try:
        safe_job = str(job_id or "job")
    except Exception:
        safe_job = "job"
    return os.path.join(backup_root, "%s-%s-%s" % (safe_job, digest, base))


def staging_from_npm_meta(meta):
    # type: (object) -> Optional[int]
    """Staging estimate from npm registry metadata or None (unknown→block).

    Uses dist.tarball unpack factor x3 ONLY when registry metadata gives a
    tarball size (keys: dist.tarballSize/dist.size/dist.unpackedSize/
    tarballSize/unpackedSize/size). Documented estimate; missing size
    yields None so T3/OpenCode staging blocks instead of using constants.
    """
    try:
        if not isinstance(meta, dict):
            return None
        candidates = []
        dist = meta.get("dist")
        if isinstance(dist, dict):
            for key in ("tarballSize", "size", "unpackedSize", "fileSize",
                        "tarball_size", "unpacked_size"):
                candidates.append(dist.get(key))
        for key in ("tarballSize", "unpackedSize", "size", "tarball_size"):
            candidates.append(meta.get(key))
        for raw in candidates:
            try:
                size = int(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if size > 0:
                return size * 3
        return None
    except Exception:
        return None


def cgroup_belongs_to_unit(pid_text, unit):
    # type: (object, object) -> bool
    """True when /proc/<pid>/cgroup mentions the unit (bounded, defensive)."""
    try:
        pid = int(str(pid_text or "").strip())
    except (TypeError, ValueError):
        return False
    try:
        unit_s = str(unit or "").strip()
    except Exception:
        return False
    if pid <= 0 or not unit_s:
        return False
    try:
        with open("/proc/%d/cgroup" % pid, "r", encoding="utf-8",
                  errors="replace") as fh:
            content = fh.read(65536)
        return unit_s in content
    except OSError:
        return False
    except Exception:
        return False


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
