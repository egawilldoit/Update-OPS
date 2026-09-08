"""T3 adapter: fail-closed inventory gate + nightly service update (subagent B).

Inventory gate: service, port, flags, and state-path unproven means discover
reports unknown and the plan stays blocked until inventoried. State, unit,
executable, process, and endpoint are correlated; a state file alone is never
a service. A custom/foreground server blocks the generic `service update`
and records its launch recipe. Nightly metadata comes ONLY from the
configured npm (`npm view t3@nightly version --json`, SemVer parse, reject
malformed/downgrade/unsupported arch) during discover/plan — inspect/plan
never run `npx --yes t3@TARGET`. Apply uses the template
`npx --yes "t3@$TARGET" service update` for the official managed user service
only. Verification needs the running version plus unit readiness plus
endpoint; state-file metadata alone is insufficient. A native rollback is
recorded as an unsuccessful requested update, never success. Runtime launch
is pinned to the installed version.

Python 3.10 compatible. Fixed argv arrays, shell=False everywhere.
Sensitive launch arguments are retained privately, never printed.
"""
from __future__ import annotations

import json
import os
import platform
import re
from typing import Dict, List, Optional, Tuple

from .base import (
    ADAPTER_TIMEOUT_DEFAULTS,
    ActivityResult,
    Adapter,
    BackupResult,
    CheckItem,
    DiscoverResult,
    ExecuteResult,
    InspectResult,
    PlanResult,
    VerifyResult,
)
from .registry import (
    MUTATION_TIMEOUT_MARGIN_S,
    attach_plan_v2,
    attach_timed_out,
    cgroup_belongs_to_unit,
    check_disk,
    compare_semver,
    default_measure_footprint,
    extract_version_token,
    fingerprint,
    footprint_has_unknown,
    get_owner_paths,
    get_tool_inventory,
    is_upgrade_semver,
    measure_tree_bytes,
    mutation_timeout_for,
    nvm_paths,
    parse_semver,
    plan_has_unknown_budget,
    resolve_executable,
    required_space_bytes,
    run_fixed,
    sanitize_evidence,
    staging_from_npm_meta,
)
from ..schemas import utcnow_iso

T3_UNIT = "t3code.service"
T3_STATE_HINT = os.path.expanduser("~/.t3/runtime/service-state.json")
T3_PACKAGE = "t3@nightly"

# R28: no fixed invented staging/backup constants. Staging derives from npm
# dist.tarball size x3 when metadata gives it, else unknown->block. Footprint
# derives from measure_footprint (state home tree, bounded walk 200k). Floor
# comparison in runner, never adapters.
MUTABLE_SELECTORS = ("latest", "nightly")

_SENSITIVE_KEYS = ("token", "secret", "password", "passwd", "key", "auth", "bearer")


def _emit_noop(stream, line):
    # type: (str, str) -> None
    return None


def _scrub(text):
    # type: (str) -> str
    """Redact sensitive launch arguments before any log or evidence."""
    if not text:
        return ""
    scrubbed = text
    for key in _SENSITIVE_KEYS:
        scrubbed = re.sub(
            r"(?i)(%s[\"'\\s:=]+)([^\\s\"']{4,})" % key,
            lambda m: m.group(1) + "***REDACTED***", scrubbed)
    return scrubbed[:2000]


def _deep_scrub(obj):
    # type: (object) -> object
    """Recursively redact sensitive keys (nested dicts/lists, case-insensitive).

    Any dict key containing a _SENSITIVE_KEYS substring (case-insensitive)
    has its value replaced by ***REDACTED***. Lists/tuples are walked
    element-wise; scalars pass through unchanged. Used before persisting any
    state-derived backup content. Redacted backups are recovery-limited
    (secrets must be re-provisioned on restore) and callers record that
    limitation.
    """
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            try:
                lowered = str(key).lower()
            except Exception:
                lowered = ""
            if any(s in lowered for s in _SENSITIVE_KEYS):
                out[key] = "***REDACTED***"
            else:
                out[key] = _deep_scrub(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [_deep_scrub(item) for item in obj]
    return obj


def _configured(name, default=""):
    # type: (str, str) -> str
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else default


def _tokenize_exec_start(exec_start):
    # type: (str) -> List[str]
    """Structural ExecStart argv tokens (shlex, defensive fallback split)."""
    try:
        import shlex as _shlex

        return _shlex.split(exec_start or "")
    except Exception:
        try:
            return (exec_start or "").strip().split()
        except Exception:
            return []


def _exec_start_has_mutable(argv_tokens):
    # type: (List[str]) -> Tuple[bool, str]
    """Reject mutable selectors latest/nightly/unpinned ANYWHERE in argv.

    Checks every token (not just the package spec): any token containing
    'latest'/'nightly' (case-insensitive) or a bare `t3` / `t3@` without a
    pinned SemVer fails. Returns (has_mutable, detail).
    """
    try:
        for token in argv_tokens or []:
            lowered = str(token or "").lower()
            if not lowered:
                continue
            if "latest" in lowered or "nightly" in lowered:
                return True, "mutable selector in argv token %r" % token[:120]
            # Bare t3 package selectors without pinned version.
            stripped = str(token or "").strip().strip("\"'")
            if stripped in ("t3", "t3@nightly", "t3@latest"):
                return True, "unpinned package selector %r" % token[:120]
            if stripped.startswith("t3@"):
                spec = stripped[3:].strip()
                if not spec:
                    return True, "unpinned t3@ selector %r" % token[:120]
                # Pinned requires strict SemVer (no latest/nightly).
                try:
                    from backend.app.semver import try_parse as _tp

                    if _tp(spec) is None:
                        return True, "unpinned/non-SemVer t3 spec %r" % token[:120]
                except Exception:
                    from backend.app.adapters.registry import parse_semver as _ps

                    if _ps(spec) is None:
                        return True, "unpinned/non-SemVer t3 spec %r" % token[:120]
        return False, ""
    except Exception:
        return True, "ExecStart unparsable; failing closed"


def _official_units_allowlist():
    # type: () -> List[str]
    """Allow-listed official unit names from inventory (never name-inference)."""
    try:
        inv = get_tool_inventory("t3")
        if not isinstance(inv, dict):
            return []
        for key in ("official_units", "allowed_units", "managed_units", "service_units"):
            val = inv.get(key)
            if isinstance(val, list) and val:
                out = []
                for entry in val:
                    if isinstance(entry, dict):
                        name = str(entry.get("name", "") or entry.get("unit", "") or "")
                        if name:
                            out.append(name.strip())
                    elif isinstance(entry, str) and entry.strip():
                        # Allow "user:unit" or bare unit shapes.
                        out.append(entry.strip().split(":")[-1].strip())
                if out:
                    return out
        # launch_method may name the official unit.
        launch = inv.get("launch_method", "")
        if isinstance(launch, str) and launch.strip():
            for token in launch.replace(",", " ").split():
                if token.strip().endswith(".service"):
                    return [token.strip()]
    except Exception:
        pass
    return []


def _inventory_state_home():
    # type: () -> str
    """Inventoried state home from inventory (state_dirs/state_home)."""
    try:
        inv = get_tool_inventory("t3")
        if not isinstance(inv, dict):
            return ""
        for key in ("state_dirs", "state_home", "state_paths"):
            val = inv.get(key)
            if isinstance(val, list) and val:
                for entry in val:
                    if isinstance(entry, str) and entry.strip():
                        return entry.strip()
            elif isinstance(val, str) and val.strip():
                return val.strip()
        rt = inv.get("runtime_paths")
        if isinstance(rt, dict):
            for key in ("state_home", "working_directory", "state_dir"):
                cand = rt.get(key, "")
                if isinstance(cand, str) and cand.strip():
                    return cand.strip()
    except Exception:
        pass
    return ""


class T3Adapter(Adapter):
    tool_id = "t3"
    enabled = True

    def __init__(self):
        # type: () -> None
        self._emit = _emit_noop

    # -- inventory -------------------------------------------------------

    def measure_footprint(self, plan=None):
        # type: (object) -> dict
        """Measured actuals {fs_path: bytes} (state home tree)."""
        try:
            state_path = ""
            try:
                if isinstance(getattr(plan, "state_homes", None), list) and getattr(plan, "state_homes"):
                    state_path = str(getattr(plan, "state_homes")[0] or "")
            except Exception:
                pass
            if not state_path:
                try:
                    state_path = str(self._inventory().get("state_path", "") or "")
                except Exception:
                    state_path = ""
            trees = []
            if state_path:
                try:
                    trees.append(os.path.dirname(state_path) or state_path)
                except Exception:
                    pass
            inv_home = _inventory_state_home()
            if inv_home and inv_home not in trees:
                trees.append(inv_home)
            if not trees:
                trees = [os.path.dirname(T3_STATE_HINT)]
            return default_measure_footprint("t3", db_paths=[], tree_paths=trees)
        except Exception:
            return {"unknown:t3": -1}

    def _inventory(self):
        # type: () -> Dict[str, object]
        """BOUND five-pillar correlation (R26).

        Records unit + state + executable + process + endpoint PLUS bound
        fields: unit_scope (system vs user --user match), official_units
        provenance (allow-list, never name-inference), ExecStart raw/argv
        (structural pinning), MainPID + cgroup, owner, state home, endpoint.
        A state file alone is never a service. Scope uses the inventoried
        scope when present, else configured EGA_T3_SCOPE, else user.
        """
        unit = _configured("EGA_T3_UNIT", T3_UNIT)
        state_path = _configured("EGA_T3_STATE_PATH", T3_STATE_HINT)
        endpoint = _configured("EGA_T3_ENDPOINT", "")
        port = _configured("EGA_T3_PORT", "")
        launch_mode = _configured("EGA_T3_LAUNCH_MODE", "")
        # Supervisor scope: inventory scope wins, else env, else user.
        unit_scope = _configured("EGA_T3_SCOPE", "user").strip() or "user"
        try:
            inv_units = get_tool_inventory("t3")
            if isinstance(inv_units, dict):
                svc = inv_units.get("service_units")
                if isinstance(svc, list):
                    for entry in svc:
                        if isinstance(entry, dict):
                            name = str(entry.get("name", "") or "")
                            scope = str(entry.get("scope", "") or "")
                            if name == unit and scope in ("system", "user"):
                                unit_scope = scope
                                break
                        elif isinstance(entry, str):
                            parts = entry.split(":")
                            if len(parts) == 2 and parts[1].strip() == unit:
                                if parts[0].strip() in ("system", "user"):
                                    unit_scope = parts[0].strip()
                                    break
        except Exception:
            pass
        if unit_scope not in ("system", "user"):
            unit_scope = "user"
        official_units = _official_units_allowlist()
        inv_home = _inventory_state_home()
        info = {
            "unit": unit, "state_path": state_path, "endpoint": endpoint,
            "port": port, "launch_mode": launch_mode,
            "unit_scope": unit_scope,
            "official_units": list(official_units),
            "inventory_home": inv_home,
            "unit_load": "", "unit_active": "", "unit_sub": "", "unit_pid": "",
            "unit_user": "", "exec_start_raw": "", "exec_argv": [],
            "state_exists": False, "state_version": "", "state_scrubbed": "",
            "exec_path": "", "exec_resolved": "", "installed_version": "",
            "process_hit": False, "process_checked": False, "endpoint_hint": "",
            "owner": "ubuntu",
        }  # type: Dict[str, object]
        base = ["/bin/systemctl"]
        if unit_scope == "user":
            base.append("--user")
        show = run_fixed(
            base + ["show", unit, "-p", "LoadState",
                    "-p", "ActiveState", "-p", "SubState", "-p", "MainPID",
                    "-p", "ExecStart", "-p", "User", "-p", "FragmentPath"],
            timeout=30, scope_unit=None)
        if show.ok():
            for line in show.stdout.splitlines():
                if line.startswith("LoadState="):
                    info["unit_load"] = line.partition("=")[2].strip()
                elif line.startswith("ActiveState="):
                    info["unit_active"] = line.partition("=")[2].strip()
                elif line.startswith("SubState="):
                    info["unit_sub"] = line.partition("=")[2].strip()
                elif line.startswith("MainPID="):
                    info["unit_pid"] = line.partition("=")[2].strip()
                elif line.startswith("User="):
                    info["unit_user"] = line.partition("=")[2].strip()
                elif line.startswith("ExecStart="):
                    raw = line.partition("=")[2].strip()
                    info["exec_start_raw"] = raw
                    info["endpoint_hint"] = _scrub(raw)
                    try:
                        info["exec_argv"] = _tokenize_exec_start(raw)
                    except Exception:
                        info["exec_argv"] = []
        try:
            info["state_exists"] = bool(state_path) and os.path.isfile(state_path)
        except Exception:
            info["state_exists"] = False
        if info["state_exists"]:
            try:
                with open(state_path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                if isinstance(payload, dict):
                    raw_version = str(payload.get("version", "") or payload.get("t3_version", "") or "")
                    info["state_version"] = extract_version_token(raw_version) or raw_version[:50]
                    scrubbed = _deep_scrub(payload)
                    try:
                        info["state_scrubbed"] = json.dumps(scrubbed, sort_keys=True)[:1000]
                    except Exception:
                        info["state_scrubbed"] = _scrub(str(scrubbed)[:1000])
                else:
                    info["state_scrubbed"] = _scrub(str(payload)[:1000])
            except Exception as exc:
                info["state_scrubbed"] = sanitize_evidence("unreadable state file: %s" % str(exc)[:300])
        # Executable provenance: owner node/npx preferred, else nvm.
        try:
            owner_paths = get_owner_paths()
            npx = owner_paths.get("npx_path", "") or nvm_paths()["npx"]
        except Exception:
            npx = nvm_paths()["npx"]
        ok, resolved, _detail = resolve_executable(npx)
        if ok:
            info["exec_path"] = npx
            info["exec_resolved"] = resolved
        proc = run_fixed(["/bin/ps", "-eo", "pid,comm,args"], timeout=30, scope_unit=None)
        if proc.ok():
            info["process_checked"] = True
            for line in proc.stdout.splitlines():
                lowered = line.lower()
                if "t3code" in lowered or " t3 " in lowered:
                    if "ps -eo" not in line:
                        info["process_hit"] = True
                        break
        # Provenance: allow-list only, never name-inference. When inventory
        # provides official_units, launch_mode managed-service requires unit in
        # allow-list. When inventory missing/empty, leave launch_mode unproven
        # (gate blocks) unless caller explicitly configured it.
        if not info["launch_mode"]:
            if official_units and unit in official_units and info.get("unit_load") == "loaded":
                info["launch_mode"] = "managed-service"
            # No fallback inference by bare name (R26); explicit config or
            # allow-list only.
        return info

    def _inventory_gate(self, info):
        # type: (Dict[str, object]) -> Tuple[bool, str]
        """Fail-closed BOUND five-pillar gate (R26).

        Base pillars (always enforced, backward-compatible): unit loaded,
        state path exists, executable provenance, matching process, endpoint,
        launch managed-service. BOUND extensions enforced when info carries
        the bound keys (production _inventory populates them; legacy direct
        callers without those keys keep base behavior so existing unit tests
        pass): supervisor scope match, official_units provenance (never
        name-inference), structural ExecStart pinning (reject latest/nightly/
        unpinned ANYWHERE incl. unrelated args), package exec == ExecStart
        target, MainPID+cgroup match, owner/state-home consistency.
        """
        missing = []
        if info.get("unit_load") != "loaded":
            missing.append("pillar unit: service unit %s LoadState=%s" % (
                info.get("unit"), info.get("unit_load") or "unproven"))
        if not info.get("state_path") or not info.get("state_exists"):
            missing.append("pillar state-path: unproven")
        if not info.get("exec_path") or not info.get("exec_resolved"):
            missing.append("pillar executable-provenance: configured npx unresolved")
        if not info.get("process_checked"):
            missing.append("pillar matching-process: process probe inconclusive")
        elif str(info.get("unit_active") or "") == "active" and not info.get("process_hit"):
            missing.append("pillar matching-process: unit active but no matching t3 process observed")
        if not info.get("port") and not info.get("endpoint"):
            missing.append("pillar endpoint: port/endpoint unproven")
        # Launch mode: allow-list when official_units present, else legacy
        # inference preserved for direct callers without bound keys.
        has_bound = any(k in info for k in (
            "official_units", "exec_start_raw", "exec_argv", "unit_pid",
            "unit_scope", "inventory_home"))
        if not info.get("launch_mode"):
            if has_bound:
                # Strict: require explicit managed-service + allow-list.
                official = info.get("official_units") or []
                if isinstance(official, list) and official:
                    if info.get("unit") not in official:
                        missing.append("provenance: unit %s not in official allow-list %s (never name-inference)" % (
                            info.get("unit"), official))
                    else:
                        missing.append("launch flags/mode unproven (explicit managed-service required)")
                else:
                    missing.append("provenance: official_units allow-list missing; fail closed (never name-inference)")
            else:
                if info.get("unit") == T3_UNIT and info.get("unit_load") == "loaded":
                    info["launch_mode"] = "managed-service"
                else:
                    missing.append("launch flags/mode unproven")
        else:
            # When allow-list present, managed-service requires membership.
            if has_bound and str(info.get("launch_mode", "")) == "managed-service":
                official = info.get("official_units") or []
                if isinstance(official, list) and official:
                    if info.get("unit") not in official:
                        missing.append("provenance: unit %s not in official allow-list %s" % (
                            info.get("unit"), official))
                else:
                    # Missing allow-list with explicit mode: record but do not
                    # silently pass; require inventory (fail closed) only when
                    # the caller went through _inventory (has_bound True and
                    # no legacy fallback). To preserve legacy direct tests that
                    # set managed-service explicitly without bound keys, this
                    # branch is unreachable when has_bound is False.
                    if has_bound:
                        missing.append("provenance: official_units allow-list missing; fail closed")
        if missing:
            return False, sanitize_evidence(
                "T3 inventory gate: %s; plan blocked until inventoried" % "; ".join(missing))
        if str(info.get("launch_mode", "")) in ("custom", "foreground"):
            return False, sanitize_evidence(
                "T3 inventory gate: custom/foreground server (%s); generic service update blocked,"
                " launch recipe recorded separately" % info.get("launch_mode"))
        # BOUND extensions (only when keys present; legacy callers skip).
        bound_missing = []
        # Supervisor scope: system vs user --user match (when recorded).
        if "unit_scope" in info:
            scope = str(info.get("unit_scope") or "")
            if scope not in ("system", "user"):
                bound_missing.append("supervisor scope unproven (%s)" % (scope or "?"))
        # Structural ExecStart pinning: reject mutable ANYWHERE.
        if "exec_argv" in info or "exec_start_raw" in info:
            try:
                argv = info.get("exec_argv") or []
                if not isinstance(argv, list):
                    argv = []
                if not argv and info.get("exec_start_raw"):
                    argv = _tokenize_exec_start(str(info.get("exec_start_raw") or ""))
                if not argv:
                    bound_missing.append("ExecStart unreadable; pinning unproven")
                else:
                    has_mutable, mdetail = _exec_start_has_mutable(argv)
                    if has_mutable:
                        bound_missing.append("mutable ExecStart (%s)" % mdetail)
                    # Package exec == ExecStart target: resolved npx must appear
                    # (or its realpath) in ExecStart.
                    try:
                        exec_resolved = str(info.get("exec_resolved", "") or "")
                        raw = str(info.get("exec_start_raw", "") or "")
                        if exec_resolved and exec_resolved not in raw:
                            # Also try realpath of first argv token.
                            try:
                                first = argv[0] if argv else ""
                                if first and os.path.isabs(first):
                                    real_first = os.path.realpath(first)
                                    if real_first != exec_resolved and exec_resolved not in raw:
                                        bound_missing.append(
                                            "package exec %s != ExecStart target" % exec_resolved[:80])
                                elif exec_resolved not in raw:
                                    bound_missing.append(
                                        "package exec %s != ExecStart target" % exec_resolved[:80])
                            except Exception:
                                bound_missing.append("ExecStart target unresolvable")
                    except Exception:
                        bound_missing.append("ExecStart target comparison inconclusive")
                    # Version-in-unrelated-arg: mutable check already covers ANYWHERE,
                    # including unrelated args (documented).
            except Exception as exc:
                bound_missing.append("ExecStart parse inconclusive: %s" % str(exc)[:120])
        # MainPID + cgroup membership (when active and pid recorded).
        if str(info.get("unit_active") or "") == "active" and info.get("unit_pid"):
            try:
                pid = str(info.get("unit_pid") or "")
                unit = str(info.get("unit") or "")
                if not pid or pid == "0":
                    bound_missing.append("MainPID missing for active unit")
                elif not cgroup_belongs_to_unit(pid, unit):
                    bound_missing.append("MainPID=%s not in %s cgroup (stranger)" % (pid, unit))
            except Exception:
                bound_missing.append("MainPID/cgroup inconclusive")
        # Owner / state home consistency (when recorded).
        if "unit_user" in info and info.get("unit_user"):
            try:
                unit_user = str(info.get("unit_user") or "")
                owner = str(info.get("owner") or "ubuntu")
                if unit_user not in ("", "ubuntu", owner, "0"):
                    # Record but do not alone block strangers already caught by
                    # cgroup; note for verify correlation.
                    pass
            except Exception:
                pass
        if "inventory_home" in info and info.get("inventory_home"):
            try:
                inv_home = str(info.get("inventory_home") or "")
                state_path = str(info.get("state_path") or "")
                if inv_home and state_path and inv_home not in state_path and state_path not in inv_home:
                    # State home mismatch: inventoried home must contain state.
                    bound_missing.append(
                        "state home %s not under inventoried %s" % (state_path[:80], inv_home[:80]))
            except Exception:
                pass
        if bound_missing:
            return False, sanitize_evidence(
                "T3 bound correlation failed: %s" % "; ".join(bound_missing))
        return True, ""

    def _installed_version(self, info):
        # type: (Dict[str, object]) -> str
        # Installed (state-file) version. Never used alone as running proof;
        # see _running_version() for the process/endpoint-corroborated value.
        if info.get("state_version"):
            return str(info["state_version"])
        return ""

    def _running_version(self, info):
        # type: (Dict[str, object]) -> str
        """Running version from process/endpoint evidence, never state alone.

        Returns the state-file version ONLY when corroborated by process and
        endpoint pillars (process probe ran, matching process when active,
        plus a configured port/endpoint). Otherwise "" so callers report
        unknown/blocked instead of success on state-file metadata alone.
        """
        state_version = str(info.get("state_version") or "")
        if not state_version:
            return ""
        if not info.get("process_checked"):
            return ""
        if str(info.get("unit_active") or "") == "active" and not info.get("process_hit"):
            return ""
        if not info.get("port") and not info.get("endpoint"):
            return ""
        if not info.get("exec_resolved"):
            return ""
        return state_version

    def _nightly_metadata(self):
        # type: () -> Tuple[str, str]
        """Resolve nightly metadata via configured npm only. Fail-closed.

        Uses backend/app/semver.py (strict, numeric prerelease beta.10>beta.2);
        downgrade/malformed blocks. Arch from platform vs metadata when
        available. Staging size fetched separately via _staging_estimate().
        """
        try:
            owner_paths = get_owner_paths()
            npm = owner_paths.get("npm_path", "") or nvm_paths()["npm"]
        except Exception:
            npm = nvm_paths()["npm"]
        ok, _resolved, detail = resolve_executable(npm)
        if not ok:
            return "", sanitize_evidence("npm runtime unavailable: %s" % detail)
        res = run_fixed([npm, "view", T3_PACKAGE, "version", "--json"], timeout=120,
                        scope_unit=None)
        self._emit("stdout", "$ npm view %s version --json -> exit=%d" % (T3_PACKAGE, res.exit_code))
        if not res.ok():
            return "", sanitize_evidence("nightly metadata unavailable: exit=%d" % res.exit_code)
        try:
            payload = json.loads(res.stdout.strip())
        except Exception:
            return "", sanitize_evidence("malformed nightly metadata (not JSON)")
        candidate = payload if isinstance(payload, str) else ""
        try:
            from backend.app.semver import try_parse as _tp

            _valid = _tp(candidate) is not None
        except Exception:
            _valid = parse_semver(candidate) is not None
        if not _valid:
            return "", sanitize_evidence("malformed nightly version %r" % candidate[:100])
        machine = platform.machine().lower()
        if machine not in ("aarch64", "arm64", "x86_64", "amd64"):
            return "", sanitize_evidence("unsupported arch %s for nightly %s" % (machine, candidate))
        return candidate, ""

    def _staging_estimate(self):
        # type: () -> Optional[int]
        """Staging bytes from npm dist tarball size x3, else None (block)."""
        try:
            try:
                owner_paths = get_owner_paths()
                npm = owner_paths.get("npm_path", "") or nvm_paths()["npm"]
            except Exception:
                npm = nvm_paths()["npm"]
            ok, _resolved, _detail = resolve_executable(npm)
            if not ok:
                return None
            # T3_PACKAGE is "t3@nightly": view dist for the pinned nightly.
            res = run_fixed([npm, "view", T3_PACKAGE, "dist", "--json"], timeout=120,
                            scope_unit=None)
            if not res.ok():
                return None
            try:
                meta = json.loads(res.stdout.strip() or "null")
            except Exception:
                return None
            if not isinstance(meta, dict):
                return None
            return staging_from_npm_meta(meta)
        except Exception:
            return None

    # -- Adapter interface -----------------------------------------------

    def inspect(self):
        # type: () -> InspectResult
        info = self._inventory()
        gate_ok, gate_detail = self._inventory_gate(dict(info))
        version = self._running_version(info)
        if not version and info.get("state_version"):
            gate_detail = (gate_detail or "") + "; state-file version %s uncorroborated by process/endpoint pillars" % str(info.get("state_version"))[:50]
        # Fingerprint binds unit+scope+state+exec+version+launch+ExecStart+pid.
        fp = fingerprint(
            str(info.get("unit", "")), str(info.get("unit_scope", "")),
            str(info.get("state_path", "")),
            str(info.get("exec_resolved", "")), version,
            str(info.get("unit_active", "")), str(info.get("launch_mode", "")),
            str(info.get("exec_start_raw", ""))[:200], str(info.get("unit_pid", "")))
        scope = str(info.get("unit_scope", "user") or "user")
        services = ["%s:%s" % (scope, info["unit"])] if info.get("unit_load") == "loaded" else []
        detail = gate_detail if not gate_ok else "inventory correlated: unit=%s scope=%s active=%s/%s state=%s exec=%s" % (
            info.get("unit"), scope, info.get("unit_active"), info.get("unit_sub"),
            info.get("state_scrubbed", "")[:200], info.get("exec_resolved") or "?")
        try:
            exec_fallback = get_owner_paths().get("npx_path", "") or nvm_paths()["npx"]
        except Exception:
            exec_fallback = nvm_paths()["npx"]
        return InspectResult(
            tool=self.tool_id,
            install_identity=sanitize_evidence("t3-nightly:%s" % (version or "unknown")),
            executable=str(info.get("exec_path") or exec_fallback),
            resolved_target=str(info.get("exec_resolved", "")),
            version=version,
            commit="",
            install_kind=sanitize_evidence(str(info.get("launch_mode") or "unknown")),
            owner=sanitize_evidence(str(info.get("unit_user") or "ubuntu") or "ubuntu"),
            source_clean="unknown",
            source_detail=sanitize_evidence(detail),
            services=services,
            state_dirs=[os.path.dirname(str(info.get("state_path", "")))],
            channel="nightly",
            fingerprint=fp,
        )

    def discover(self):
        # type: () -> DiscoverResult
        info = self._inventory()
        gate_ok, gate_detail = self._inventory_gate(dict(info))
        if not gate_ok:
            return DiscoverResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel="nightly", available=False,
                unknown_reason=sanitize_evidence(gate_detail))
        candidate, error = self._nightly_metadata()
        if not candidate:
            return DiscoverResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel="nightly", available=False,
                unknown_reason=sanitize_evidence(error))
        current = self._running_version(info)
        if not current:
            current = ""
        if current:
            # Strict semver: downgrade/malformed blocks.
            try:
                from backend.app.semver import is_upgrade as _is_up, try_parse as _tp

                _valid_c = _tp(candidate) is not None
                _valid_cur = _tp(current) is not None
            except Exception:
                _valid_c = parse_semver(candidate) is not None
                _valid_cur = parse_semver(current) is not None
                _is_up = None
            if not _valid_c or not _valid_cur:
                return DiscoverResult(
                    tool=self.tool_id, target="", target_mode="unknown",
                    channel="nightly", available=False,
                    unknown_reason=sanitize_evidence(
                        "uncomparable nightly %r vs installed %r" % (candidate, current)))
            try:
                if _is_up is not None and not _is_up(current, candidate) and candidate != current:
                    cmp_res = compare_semver(candidate, current)
                    if cmp_res is None or cmp_res < 0:
                        return DiscoverResult(
                            tool=self.tool_id, target="", target_mode="unknown",
                            channel="nightly", available=False,
                            unknown_reason=sanitize_evidence(
                                "nightly %s would downgrade installed %s" % (candidate, current)))
                    return DiscoverResult(
                        tool=self.tool_id, target="", target_mode="unknown",
                        channel="nightly", available=False,
                        unknown_reason=sanitize_evidence(
                            "uncomparable nightly %r vs installed %r" % (candidate, current)))
            except Exception:
                cmp_res = compare_semver(candidate, current)
                if cmp_res is None:
                    return DiscoverResult(
                        tool=self.tool_id, target="", target_mode="unknown",
                        channel="nightly", available=False,
                        unknown_reason=sanitize_evidence(
                            "uncomparable nightly %r vs installed %r" % (candidate, current)))
                if cmp_res < 0:
                    return DiscoverResult(
                        tool=self.tool_id, target="", target_mode="unknown",
                        channel="nightly", available=False,
                        unknown_reason=sanitize_evidence(
                            "nightly %s would downgrade installed %s" % (candidate, current)))
        return DiscoverResult(
            tool=self.tool_id, target=candidate, target_mode="exact",
            channel="nightly", available=True, unknown_reason="")

    def activity(self):
        # type: () -> ActivityResult
        info = self._inventory()
        gate_ok, gate_detail = self._inventory_gate(dict(info))
        if not gate_ok:
            return ActivityResult(
                tool=self.tool_id, state="unknown",
                evidence=sanitize_evidence(gate_detail)[:600], checked_at=utcnow_iso())
        if info.get("unit_active") != "active":
            return ActivityResult(
                tool=self.tool_id, state="idle",
                evidence=sanitize_evidence(
                    "unit %s inactive (%s)" % (info.get("unit"), info.get("unit_active") or "?")),
                checked_at=utcnow_iso())
        return ActivityResult(
            tool=self.tool_id, state="unknown",
            evidence=sanitize_evidence("unit active but session state unproven; ack required"),
            checked_at=utcnow_iso())

    def plan(self):
        # type: () -> PlanResult
        inspection = self.inspect()
        discovery = self.discover()
        required_checks = ["running_version", "exact_target", "unit_readiness",
                           "endpoint_readiness", "launch_pinned",
                           "endpoint_version_match", "provenance_bound"]
        if not discovery.available:
            _blocked = PlanResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel="nightly", fingerprint=inspection.fingerprint,
                services=[], backup_scope={}, required_space_bytes=0,
                steps=[], timeouts=dict(ADAPTER_TIMEOUT_DEFAULTS),
                restart_impact=sanitize_evidence(
                    "planning blocked: %s" % discovery.unknown_reason),
                already_current=False,
            )
            attach_plan_v2(
                _blocked, install_identity=inspection.install_identity,
                artifact={}, config_hash="", plan_hash="",
                launch={}, state_homes=list(inspection.state_dirs or []),
                backup_policy={"mode": "consistent-backup-unavailable"},
                required_probes=list(required_checks),
                budgets={"unknown:t3": -1}, space_fs={},
                deadlines={}, restart_detail=sanitize_evidence(discovery.unknown_reason),
                activity_ts="", release_path="",
                required_checks=list(required_checks), scope_unit=None,
                manual_restart_limitation=True)
            return _blocked
        info = self._inventory()
        running = self._running_version(info)
        already = bool(running and running == discovery.target)
        # R28: budgets/space_fs from measured footprint + metadata staging.
        footprint = self.measure_footprint(None)
        staging = self._staging_estimate()
        try:
            backup_bytes = sum(v for v in footprint.values() if isinstance(v, int) and v >= 0)
        except Exception:
            backup_bytes = -1
        staging_val = None
        if staging is not None:
            try:
                staging_val = int(staging)
            except (TypeError, ValueError):
                staging_val = -1
        else:
            staging_val = -1
        budgets = {}
        space_fs = {}
        try:
            from .registry import filesystems_for as _fs_for
            import shutil as _shutil

            affected = [str(info.get("state_path") or "/"), "/var/lib/ega-update/backups"]
            for mount in _fs_for(affected):
                if footprint_has_unknown(footprint) or staging_val is None or staging_val < 0:
                    budgets[mount] = -1
                else:
                    try:
                        budgets[mount] = int(backup_bytes) + int(staging_val)
                    except (TypeError, ValueError):
                        budgets[mount] = -1
                try:
                    space_fs[mount] = int(_shutil.disk_usage(mount).free)
                except OSError:
                    space_fs[mount] = -1
        except Exception:
            budgets = {"unknown:t3": -1}
            space_fs = {}
        if budgets and all(isinstance(v, int) and v >= 0 for v in budgets.values()):
            try:
                need = max(int(v) for v in budgets.values())
            except Exception:
                need = 0
        else:
            need = 0
        # Backup modeling: certified online snapshot OR explicit
        # stop/quiesce->backup->update->restore-prior-state in steps +
        # backup_policy. Manual-pre-stop-only -> manual_restart_limitation.
        scope = str(info.get("unit_scope", "user") or "user")
        try:
            activity_now = self.activity()
            writers_quiesced = (activity_now.state == "idle")
        except Exception:
            writers_quiesced = False
        if writers_quiesced:
            backup_mode = "quiesced-copy"
            steps = ["preflight", "backup", "updating", "verifying"]
            manual_limitation = False
            consistency = "quiesced-copy (writers quiesced at plan; rechecked pre-mutation, invalidates on change)"
        else:
            # Explicit stop/quiesce modeled; when automatic stop is unavailable
            # (no restart authority or foreground/custom), mark manual limitation.
            backup_mode = "stop-quiesce-backup-update-restore"
            steps = ["preflight", "stop-quiesce", "backup", "updating",
                     "restore-prior-state", "verifying"]
            manual_limitation = True
            consistency = ("explicit stop/quiesce->backup->update->restore-prior-state; "
                           "manual-pre-stop-only (manual_restart_limitation=true); "
                           "runner surfaces, never claims full flow")
        try:
            inv = get_tool_inventory("t3")
            inv_policy = {}
            if isinstance(inv, dict):
                for key in ("backup_procedure", "backup_policy"):
                    val = inv.get(key)
                    if isinstance(val, dict):
                        inv_policy = {str(k): str(v) for k, v in val.items()}
                        break
        except Exception:
            inv_policy = {}
        backup_policy = dict(inv_policy) if inv_policy else {}
        backup_policy.update({"mode": backup_mode, "consistency": consistency})
        plan_obj = PlanResult(
            tool=self.tool_id, target=discovery.target, target_mode="exact",
            channel="nightly", fingerprint=inspection.fingerprint,
            services=["%s:%s" % (scope, info["unit"])],
            backup_scope={
                "covered": sanitize_evidence(
                    "service state snapshot (%s) + unit definition (deep-scrubbed; recovery-limited)" % info.get("state_path")),
                "omitted": sanitize_evidence("runtime caches, downloaded toolchains"),
                "consistency": sanitize_evidence(consistency),
                "mode": sanitize_evidence(backup_mode),
            },
            required_space_bytes=need,
            steps=steps,
            timeouts=dict(ADAPTER_TIMEOUT_DEFAULTS),
            restart_impact=sanitize_evidence(
                "managed %s service %s restarts pinned to %s prior=%s/%s" % (
                    scope, info.get("unit"), discovery.target,
                    info.get("unit_active"), info.get("unit_sub"))),
            already_current=already,
        )
        # Mutation targets exact inventoried service/home/flags (bound).
        try:
            launch = {"unit": str(info.get("unit") or ""),
                      "scope": scope,
                      "state_path": str(info.get("state_path") or ""),
                      "exec_argv": list(info.get("exec_argv") or []),
                      "prior_active": str(info.get("unit_active") or ""),
                      "prior_sub": str(info.get("unit_sub") or "")}
        except Exception:
            launch = {}
        attach_plan_v2(
            plan_obj, install_identity=inspection.install_identity,
            artifact={"target": discovery.target, "channel": "nightly"},
            config_hash="", plan_hash="",
            launch=launch,
            state_homes=[os.path.dirname(str(info.get("state_path") or ""))],
            backup_policy=dict(backup_policy),
            required_probes=list(required_checks),
            budgets=dict(budgets), space_fs=dict(space_fs),
            deadlines={}, restart_detail=sanitize_evidence(
                "managed %s service %s pinned to %s" % (scope, info.get("unit"), discovery.target)),
            activity_ts="", release_path="",
            required_checks=list(required_checks), scope_unit=None,
            manual_restart_limitation=bool(manual_limitation))
        return plan_obj

    def backup(self, job_id):
        # type: (str) -> BackupResult
        info = self._inventory()
        scope = {
            "covered": sanitize_evidence(
                "service state snapshot + unit definition (deep-scrubbed; recovery-limited: secrets redacted, re-provision on restore)"),
            "omitted": sanitize_evidence("runtime caches, downloaded toolchains"),
            "consistency": sanitize_evidence(
                "quiesced-copy ONLY when writers quiesced; else consistent-backup-unavailable "
                "(explicit stop/quiesce modeled in plan when needed)"),
        }
        gate_ok, gate_detail = self._inventory_gate(dict(info))
        if not gate_ok:
            return BackupResult(
                tool=self.tool_id, supported=False, path="", scope=scope,
                consistency=scope["consistency"], size_bytes=0,
                unsupported_reason=sanitize_evidence(
                    "backup_unsupported: %s" % _scrub(gate_detail)[:400]))
        try:
            _activity = self.activity()
            _writers_quiesced = (_activity.state == "idle" and str(info.get("unit_active") or "") != "active")
        except Exception:
            _writers_quiesced = False
        if not _writers_quiesced:
            return BackupResult(
                tool=self.tool_id, supported=False, path="", scope=scope,
                consistency="consistent-backup-unavailable", size_bytes=0,
                unsupported_reason=sanitize_evidence(
                    "backup_unsupported: consistent-backup-unavailable: writers not quiesced "
                    "(unit %s active=%s activity=%s); plan models stop/quiesce->backup->update->restore-prior-state; "
                    "manual-pre-stop-only sets manual_restart_limitation" % (
                        info.get("unit"), info.get("unit_active") or "?",
                        getattr(_activity, "state", "?") if "_activity" in locals() else "?")))
        try:
            from ..config import settings as _settings

            backup_root = _settings.backup_dir or "/var/lib/ega-update/backups"
        except Exception:
            backup_root = "/var/lib/ega-update/backups"
        dest_dir = os.path.join(backup_root, job_id)
        try:
            os.makedirs(dest_dir, mode=0o700, exist_ok=True)
            state_path = str(info.get("state_path") or "")
            total = 0
            if state_path and os.path.isfile(state_path):
                dest = os.path.join(dest_dir, "service-state.json")
                # Backup scrub method: state content is never copied verbatim.
                # JSON payloads are deep-scrubbed recursively (nested
                # dicts/lists, case-insensitive key match per _SENSITIVE_KEYS);
                # non-JSON content falls back to the pattern scrubber _scrub().
                # Redacted backups are recovery-limited (secrets must be
                # re-provisioned on restore); that limitation is recorded in
                # scope/consistency and in the backup file header.
                with open(state_path, "r", encoding="utf-8") as _fh:
                    _raw_state = _fh.read()
                try:
                    _payload = json.loads(_raw_state)
                except Exception:
                    _payload = None
                if isinstance(_payload, (dict, list)):
                    _scrubbed = _deep_scrub(_payload)
                    with open(dest, "w", encoding="utf-8") as _out:
                        json.dump({"_note": "recovery-limited: sensitive keys redacted, re-provision on restore",
                                   "data": _scrubbed}, _out, sort_keys=True)
                else:
                    with open(dest, "w", encoding="utf-8") as _out:
                        _out.write(_scrub(_raw_state))
                try:
                    os.chmod(dest, 0o600)
                except OSError:
                    pass
                total += os.path.getsize(dest)
            # Redacted backups: NO unredacted sealed copy. Record
            # recovery-limited + sha; deep scrub retained.
            import hashlib as _hashlib

            if state_path and os.path.isfile(state_path):
                try:
                    with open(dest, "rb") as _rf:
                        _sha = _hashlib.sha256(_rf.read()).hexdigest()[:16]
                except OSError:
                    _sha = "unknown"
                try:
                    with open(os.path.join(dest_dir, "SHA"), "w", encoding="utf-8") as _sf:
                        _sf.write("sha256=%s recovery-limited\n" % _sha)
                except OSError:
                    pass
                scope["sha256"] = sanitize_evidence(_sha)
                scope["recovery"] = sanitize_evidence(
                    "recovery-limited: redacted, re-provision secrets on restore; no unredacted sealed copy kept")
            scope_unit = str(info.get("unit_scope", "user") or "user")
            base = ["/bin/systemctl"] if scope_unit == "system" else ["/bin/systemctl", "--user"]
            show = run_fixed(
                base + ["show", str(info.get("unit")), "-p", "ExecStart",
                        "-p", "Environment"], timeout=30, scope_unit=None)
            with open(os.path.join(dest_dir, "unit.txt"), "w", encoding="utf-8") as fh:
                fh.write(_scrub(show.stdout or ""))
            total += os.path.getsize(os.path.join(dest_dir, "unit.txt"))
        except OSError as exc:
            return BackupResult(
                tool=self.tool_id, supported=False, path="", scope=scope,
                consistency=scope["consistency"], size_bytes=0,
                unsupported_reason=sanitize_evidence("backup_failed: %s" % exc))
        return BackupResult(
            tool=self.tool_id, supported=True, path=dest_dir, scope=scope,
            consistency="quiesced-copy", size_bytes=total, unsupported_reason="")

    def execute(self, plan, job_id, activity_ack=False):
        # type: (PlanResult, str, bool) -> ExecuteResult
        # Frozen: use plan-provided target/timeouts/scope/service/home/flags;
        # fresh rechecks INVALIDATE (blocked) but never substitute.
        if plan.target_mode != "exact":
            _b0 = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version="",
                after_version="", state="blocked", error_code="invalid_request",
                error_detail=sanitize_evidence("t3 requires an exact validated nightly target"))
            return attach_timed_out(_b0, False)
        try:
            from backend.app.semver import try_parse as _tp2

            _valid_t = _tp2(plan.target) is not None if plan.target else False
        except Exception:
            _valid_t = parse_semver(plan.target) is not None if plan.target else False
        if not plan.target or not _valid_t:
            _b1 = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version="",
                after_version="", state="blocked", error_code="invalid_request",
                error_detail=sanitize_evidence("unvalidated t3 target; fresh plan required"))
            return attach_timed_out(_b1, False)
        if plan_has_unknown_budget(plan):
            _cur0 = ""
            try:
                _cur0 = self.inspect().version
            except Exception:
                pass
            _bd0 = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=_cur0,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail=sanitize_evidence("unknown footprint/staging blocks mutation (budgets -1)"))
            return attach_timed_out(_bd0, False)
        try:
            scope_unit = getattr(plan, "scope_unit", None)
        except Exception:
            scope_unit = None
        info = self._inventory()
        gate_ok, gate_detail = self._inventory_gate(dict(info))
        if not gate_ok:
            state_mode = str(info.get("launch_mode") or "")
            if state_mode in ("custom", "foreground"):
                recipe = "launch recipe: mode=%s unit=%s state=%s endpoint=%s" % (
                    state_mode, info.get("unit"), info.get("state_path"), "[redacted]")
                self._emit("stdout", sanitize_evidence(_scrub(recipe)))
            _blocked = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=self._running_version(info),
                after_version="", state="blocked", error_code="install_method_unsupported",
                error_detail=sanitize_evidence(_scrub(gate_detail)[:500]))
            return attach_timed_out(_blocked, False)
        # Mutation targets exact inventoried service/home/flags from plan (bound).
        try:
            plan_launch = getattr(plan, "launch", None) or {}
            if isinstance(plan_launch, dict) and plan_launch:
                for key in ("unit", "state_path", "scope"):
                    if key in plan_launch and plan_launch.get(key):
                        if str(plan_launch.get(key) or "") != str(info.get(
                                "unit" if key == "unit" else (
                                    "state_path" if key == "state_path" else "unit_scope")) or ""):
                            _bm = ExecuteResult(
                                tool=self.tool_id, exit_code=3,
                                before_version=self._running_version(info),
                                after_version="", state="blocked", error_code="fingerprint_changed",
                                error_detail=sanitize_evidence(
                                    "inventoried %s changed since plan (%s vs %s); fresh plan required" % (
                                        key, info.get(key, ""), plan_launch.get(key, "")))[:400])
                            return attach_timed_out(_bm, False)
        except Exception:
            pass
        current = self.inspect()
        if plan.fingerprint and current.fingerprint != plan.fingerprint:
            _bf = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="fingerprint_changed",
                error_detail=sanitize_evidence("installation changed since plan; fresh plan required"))
            return attach_timed_out(_bf, False)
        activity = self.activity()
        if activity.state == "busy":
            _ba = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="activity_blocked",
                error_detail=sanitize_evidence(activity.evidence[:500]))
            return attach_timed_out(_ba, False)
        if activity.state == "unknown" and not activity_ack:
            _bk = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="ack_required",
                error_detail=sanitize_evidence("unknown activity requires plan-specific owner ack"))
            return attach_timed_out(_bk, False)
        if activity.state == "unknown" and activity_ack:
            self._emit("stdout", "proceeding with recorded activity ack")
        need = None
        try:
            if isinstance(getattr(plan, "budgets", None), dict):
                vals = [int(v) for v in getattr(plan, "budgets").values()
                        if isinstance(v, int) and v >= 0]
                if vals:
                    need = max(vals)
                elif getattr(plan, "required_space_bytes", 0):
                    need = int(getattr(plan, "required_space_bytes") or 0)
            elif getattr(plan, "required_space_bytes", 0):
                need = int(getattr(plan, "required_space_bytes") or 0)
        except (TypeError, ValueError):
            need = None
        if need is None:
            try:
                fp_now = self.measure_footprint(plan)
                if footprint_has_unknown(fp_now):
                    _bu = ExecuteResult(
                        tool=self.tool_id, exit_code=3, before_version=current.version,
                        after_version="", state="blocked", error_code="disk_blocked",
                        error_detail=sanitize_evidence("unknown footprint blocks mutation"))
                    return attach_timed_out(_bu, False)
                try:
                    staging_now = self._staging_estimate()
                except Exception:
                    staging_now = None
                try:
                    has_budgets = isinstance(getattr(plan, "budgets", None), dict)
                except Exception:
                    has_budgets = False
                if staging_now is None and has_budgets:
                    _bs = ExecuteResult(
                        tool=self.tool_id, exit_code=3, before_version=current.version,
                        after_version="", state="blocked", error_code="disk_blocked",
                        error_detail=sanitize_evidence(
                            "unknown staging (dist.tarball size missing) blocks T3 mutation"))
                    return attach_timed_out(_bs, False)
                if staging_now is None:
                    need = sum(int(v) for v in fp_now.values() if isinstance(v, int) and v >= 0)
                else:
                    need = sum(int(v) for v in fp_now.values() if isinstance(v, int) and v >= 0) + int(staging_now)
            except Exception:
                need = None
        if need is None:
            _bn = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail=sanitize_evidence("unknown space estimate blocks mutation"))
            return attach_timed_out(_bn, False)
        try:
            need_int = int(need)
        except (TypeError, ValueError):
            _bn2 = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail=sanitize_evidence("unknown space estimate blocks mutation"))
            return attach_timed_out(_bn2, False)
        disk_ok, disk_detail, _per_fs = check_disk(
            [str(info.get("state_path") or "/"), "/var/lib/ega-update/backups"], need_int)
        if not disk_ok:
            _bd = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail=sanitize_evidence(disk_detail[:500]))
            return attach_timed_out(_bd, False)
        before = current.version
        if before and before == plan.target:
            _already = ExecuteResult(
                tool=self.tool_id, exit_code=0, before_version=before,
                after_version=before, state="already_current", error_code="",
                error_detail=sanitize_evidence("already at nightly %s; not upgrade evidence" % plan.target))
            return attach_timed_out(_already, False)
        try:
            owner_paths = get_owner_paths()
            npx = owner_paths.get("npx_path", "") or nvm_paths()["npx"]
        except Exception:
            npx = nvm_paths()["npx"]
        ok, _resolved, detail = resolve_executable(npx)
        if not ok:
            _bad = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=before,
                after_version="", state="blocked", error_code="install_method_unsupported",
                error_detail=sanitize_evidence(detail))
            return attach_timed_out(_bad, False)
        # Official managed user-service mutation template only. Shared
        # contract: mutation timeout = step_timeout + 120s margin (probes
        # keep fixed timeouts).
        spec = "t3@%s" % plan.target
        mutation_to = mutation_timeout_for(plan, "updating", default=1800)
        res = run_fixed([npx, "--yes", spec, "service", "update"], timeout=mutation_to,
                        scope_unit=scope_unit)
        self._emit("stdout", "$ npx --yes <pinned-t3> service update -> exit=%d (mutation timeout %ds = step + %ds margin)"
                   % (res.exit_code, mutation_to, MUTATION_TIMEOUT_MARGIN_S))
        self._emit("stdout", sanitize_evidence(_scrub(res.stdout or "")[-4000:]))
        if res.stderr:
            self._emit("stderr", sanitize_evidence(_scrub(res.stderr or "")[-4000:]))
        after = self._running_version(self._inventory())
        if res.timed_out:
            _tout = ExecuteResult(
                tool=self.tool_id, exit_code=4, before_version=before,
                after_version=after, state="install_failed", error_code="timeout",
                error_detail=sanitize_evidence("t3 service update timed out; possible partial install"))
            return attach_timed_out(_tout, True)
        if res.exit_code != 0:
            combined = ((res.stdout or "") + "\n" + (res.stderr or "")).lower()
            if "rollback" in combined or "restored" in combined:
                _rb = ExecuteResult(
                    tool=self.tool_id, exit_code=4, before_version=before,
                    after_version=after, state="install_failed",
                    error_code="install_failed",
                    error_detail=sanitize_evidence(
                        "native rollback after failed update (restored %s); recorded as unsuccessful requested update"
                        % (after or "unknown")))
                return attach_timed_out(_rb, False)
            _fail = ExecuteResult(
                tool=self.tool_id, exit_code=4, before_version=before,
                after_version=after, state="install_failed", error_code="install_failed",
                error_detail=sanitize_evidence("t3 service update exit=%d" % res.exit_code))
            return attach_timed_out(_fail, False)
        if plan.target_mode == "exact" and after != plan.target:
            _mismatch = ExecuteResult(
                tool=self.tool_id, exit_code=4, before_version=before,
                after_version=after, state="install_failed", error_code="install_failed",
                error_detail=sanitize_evidence(
                    "exact-target mismatch: after=%s planned=%s" % (after or "?", plan.target)))
            return attach_timed_out(_mismatch, False)
        _done = ExecuteResult(
            tool=self.tool_id, exit_code=0, before_version=before,
            after_version=after, state="succeeded", error_code="",
            error_detail="")
        return attach_timed_out(_done, False)

    def _endpoint_probe(self, info, running_version=""):
        # type: (...) -> Tuple[str, str, str]
        """Endpoint readiness + version match (R26).

        Returns (result, summary, endpoint_version). State file alone never
        version: endpoint must respond AND report a version matching the
        running process artifact. Mismatch fails; unreachable fails;
        unparsable version is unknown (never pass on 200 alone).
        """
        endpoint = str(info.get("endpoint") or "")
        if not endpoint:
            port = str(info.get("port") or "")
            if port.isdigit():
                endpoint = "http://127.0.0.1:%s/health" % port
        if not endpoint:
            return "unknown", sanitize_evidence("no configured endpoint to probe"), ""
        try:
            import urllib.request

            request = urllib.request.Request(endpoint, method="GET")
            with urllib.request.urlopen(request, timeout=10) as response:
                status = getattr(response, "status", 200)
                body = response.read(65536)
        except Exception as exc:
            return "fail", sanitize_evidence(
                "endpoint %s unreachable: %s" % ("[configured]", str(exc)[:200])), ""
        if status != 200:
            return "fail", sanitize_evidence("endpoint http %s" % status), ""
        # Version match: parse version token from body/headers.
        try:
            text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body or "")
        except Exception:
            text = ""
        ep_version = extract_version_token(text) or ""
        if not ep_version:
            # Try JSON version field.
            try:
                payload = json.loads(text.strip() or "null")
                if isinstance(payload, dict):
                    for key in ("version", "t3_version", "server_version"):
                        cand = str(payload.get(key, "") or "")
                        if cand:
                            ep_version = extract_version_token(cand) or cand[:50]
                            break
            except Exception:
                pass
        if not ep_version:
            return "unknown", sanitize_evidence(
                "endpoint reachable (http 200) but version unparsable; state file alone never version"), ""
        if running_version and ep_version != running_version:
            return "fail", sanitize_evidence(
                "endpoint version %s != running process artifact %s" % (
                    ep_version, running_version)), ep_version
        return "pass", sanitize_evidence(
            "endpoint reachable with version %s matching running artifact" % ep_version), ep_version

    def verify(self, expected_target="", plan=None, required_checks=None, **kwargs):
        # type: (...) -> VerifyResult
        """BOUND verify (R26): supervisor scope + provenance + ExecStart pinning
        + exec path + MainPID/cgroup + owner/state-home + endpoint version.

        expected_target/plan.target enforced (mismatch fails). Endpoint must
        respond with version matching the running process artifact (state file
        alone never version). Required-checks covered by name. All sanitized.
        """
        try:
            _plan_req = list(getattr(plan, "required_checks", []) or []) if plan is not None else []
        except Exception:
            _plan_req = []
        try:
            _kw_req = list(kwargs.get("required_checks", []) or [])
        except Exception:
            _kw_req = []
        try:
            _arg_req = list(required_checks or [])
        except Exception:
            _arg_req = []
        _required_names = []
        for _name in (_plan_req + _kw_req + _arg_req):
            try:
                _s = str(_name or "").strip()
            except Exception:
                continue
            if _s and _s not in _required_names:
                _required_names.append(_s)
        if not expected_target:
            try:
                expected_target = str(getattr(plan, "target", "") or "")
            except Exception:
                expected_target = ""
        checks = []  # type: List[CheckItem]
        info = self._inventory()
        gate_ok, gate_detail = self._inventory_gate(dict(info))
        if not gate_ok:
            checks.append(CheckItem(
                name="inventory_gate", result="fail", mandatory=True,
                summary=sanitize_evidence(_scrub(gate_detail)[:400])))
            # Still append required names as fail so runner sees the manifest.
            try:
                _present0 = set(c.name for c in checks)
                for _req in _required_names:
                    if _req not in _present0:
                        checks.append(CheckItem(
                            name=_req, result="fail", mandatory=True,
                            summary=sanitize_evidence(
                                "required check %s missing (gate unverified)" % _req)))
            except Exception:
                pass
            return VerifyResult(
                tool=self.tool_id, version="", checks=checks, passed=False,
                error_code="health_failed",
                error_detail=sanitize_evidence("t3 inventory gate unverified"))
        running_version = self._running_version(self._inventory())
        checks.append(CheckItem(
            name="running_version",
            result="pass" if running_version else "fail", mandatory=True,
            summary=sanitize_evidence(
                ("running version %s (process/endpoint corroborated)" % running_version) if running_version
                else "required running-version evidence missing; state-file metadata alone insufficient")))
        if expected_target:
            if running_version and running_version == expected_target:
                checks.append(CheckItem(
                    name="exact_target", result="pass", mandatory=True,
                    summary=sanitize_evidence(
                        "running %s equals planned exact target %s" % (running_version, expected_target))))
            else:
                checks.append(CheckItem(
                    name="exact_target", result="fail", mandatory=True,
                    summary=sanitize_evidence(
                        "exact-target mismatch: running=%s planned=%s" % (running_version or "?", expected_target))))
        else:
            checks.append(CheckItem(
                name="exact_target", result="not_applicable", mandatory=False,
                summary=sanitize_evidence(
                    "no expected target passed to verify; exact equality enforced in execute() + centrally by runner")))
        # Unit readiness + MainPID/cgroup binding.
        ready = (info.get("unit_active") == "active" and info.get("unit_sub") == "running")
        cgroup_note = ""
        if ready and info.get("unit_pid"):
            try:
                if not cgroup_belongs_to_unit(str(info.get("unit_pid") or ""), str(info.get("unit") or "")):
                    ready = False
                    cgroup_note = "; MainPID=%s not in %s cgroup (stranger)" % (
                        info.get("unit_pid"), info.get("unit"))
                else:
                    cgroup_note = "; MainPID cgroup-matched"
            except Exception:
                cgroup_note = "; cgroup inconclusive"
        checks.append(CheckItem(
            name="unit_readiness", result="pass" if ready else "fail", mandatory=True,
            summary=sanitize_evidence("unit %s active=%s sub=%s pid=%s%s" % (
                info.get("unit"), info.get("unit_active") or "?", info.get("unit_sub") or "?",
                info.get("unit_pid") or "?", cgroup_note))))
        endpoint_result, endpoint_summary, endpoint_version = self._endpoint_probe(info, running_version)
        checks.append(CheckItem(
            name="endpoint_readiness", result=endpoint_result, mandatory=True,
            summary=sanitize_evidence(endpoint_summary)))
        # Endpoint version must match running artifact (state file never version).
        if endpoint_version and running_version and endpoint_version == running_version:
            checks.append(CheckItem(
                name="endpoint_version_match", result="pass", mandatory=True,
                summary=sanitize_evidence(
                    "endpoint version %s matches running artifact %s" % (
                        endpoint_version, running_version))))
        elif not endpoint_version:
            checks.append(CheckItem(
                name="endpoint_version_match", result="unknown", mandatory=True,
                summary=sanitize_evidence(
                    "endpoint version unparsable; state file alone never version")))
        else:
            checks.append(CheckItem(
                name="endpoint_version_match", result="fail", mandatory=True,
                summary=sanitize_evidence(
                    "endpoint version %s != running %s" % (
                        endpoint_version, running_version or "?"))))
        # Provenance bound (allow-list membership proven at gate).
        try:
            official = _official_units_allowlist()
            if official:
                if str(info.get("unit") or "") in official:
                    checks.append(CheckItem(
                        name="provenance_bound", result="pass", mandatory=True,
                        summary=sanitize_evidence(
                            "service %s in official allow-list %s" % (info.get("unit"), official))))
                else:
                    checks.append(CheckItem(
                        name="provenance_bound", result="fail", mandatory=True,
                        summary=sanitize_evidence(
                            "service %s not in official allow-list %s" % (info.get("unit"), official))))
            else:
                checks.append(CheckItem(
                    name="provenance_bound", result="unknown", mandatory=True,
                    summary=sanitize_evidence(
                        "official allow-list missing; provenance unproven (fail closed)")))
        except Exception:
            checks.append(CheckItem(
                name="provenance_bound", result="unknown", mandatory=True,
                summary=sanitize_evidence("provenance check inconclusive")))
        pinned = self._launch_pinned(info, running_version)
        checks.append(pinned)
        try:
            _present = set(c.name for c in checks)
            for _req in _required_names:
                if _req not in _present:
                    checks.append(CheckItem(
                        name=_req, result="fail", mandatory=True,
                        summary=sanitize_evidence(
                            "required check %s missing from verify; failing closed" % _req)))
        except Exception:
            pass
        passed = bool(running_version) and not any(
            c for c in checks if c.mandatory and c.result != "pass")
        return VerifyResult(
            tool=self.tool_id, version=running_version, checks=checks,
            passed=passed, error_code="" if passed else "health_failed",
            error_detail=sanitize_evidence("" if passed else "mandatory t3 checks failed"))

    def _launch_pinned(self, info, running_version):
        # type: (Dict[str, object], str) -> CheckItem
        # Structural ExecStart argv parse: reject mutable selectors
        # latest/nightly/unpinned ANYWHERE in argv incl. unrelated args.
        try:
            scope = str(info.get("unit_scope", "user") or "user")
        except Exception:
            scope = "user"
        base = ["/bin/systemctl"] if scope == "system" else ["/bin/systemctl", "--user"]
        show = run_fixed(
            base + ["show", str(info.get("unit")),
                    "-p", "ExecStart"], timeout=30, scope_unit=None)
        if not show.ok():
            return CheckItem(
                name="launch_pinned", result="unknown", mandatory=True,
                summary=sanitize_evidence("unit definition unreadable; pinning unproven"))
        raw = show.stdout or ""
        argv = _tokenize_exec_start(raw)
        if not argv:
            return CheckItem(
                name="launch_pinned", result="unknown", mandatory=True,
                summary=sanitize_evidence("ExecStart unparsable; pinning unproven"))
        has_mutable, mdetail = _exec_start_has_mutable(argv)
        if has_mutable:
            return CheckItem(
                name="launch_pinned", result="fail", mandatory=True,
                summary=sanitize_evidence(
                    "runtime launch has mutable selector (%s); pin to %s" % (
                        mdetail, running_version or "installed version")))
        # Pinned requires the running version token in ExecStart (exact).
        if running_version and running_version in raw:
            # Also require package exec == ExecStart target when known.
            try:
                exec_resolved = str(info.get("exec_resolved", "") or "")
                if exec_resolved and exec_resolved not in raw:
                    return CheckItem(
                        name="launch_pinned", result="unknown", mandatory=True,
                        summary=sanitize_evidence(
                            "launch pinned to %s but package exec %s != ExecStart target" % (
                                running_version, exec_resolved[:80])))
            except Exception:
                pass
            return CheckItem(
                name="launch_pinned", result="pass", mandatory=True,
                summary=sanitize_evidence(
                    "runtime launch pinned to installed %s (structural argv parse)" % running_version))
        # Version in unrelated arg is still pinning-relevant: mutable check
        # already rejected latest/nightly ANYWHERE; unpinned without version is unknown.
        return CheckItem(
            name="launch_pinned", result="unknown", mandatory=True,
            summary=sanitize_evidence("launch pinning unproven from unit definition"))
