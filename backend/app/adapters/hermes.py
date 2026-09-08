"""Hermes adapter: native updater with git + backup + unit checks (subagent B).

Wrapper /home/ubuntu/.local/bin/hermes, repo /home/ubuntu/.hermes/hermes-agent,
main branch. Capability probes (`update --help/--plan/--check`), git
cleanliness including untracked files (`status --porcelain=v1`,
`rev-parse HEAD`; dirty/unreadable blocks, never --force). Effective backup
mode/scope/receipt inspection (--backup only when supported plus storage
suffices; quick mode labeled limited, never silently downgraded). Native
restart handling plus receipt. Every inventoried gateway/serve unit verified
in the correct scope (system units need confirmed restart authority — block
before mutation when ubuntu lacks it, never prompt sudo mid-job). Final
commit/diagnostics/restart/running-version recorded; exit-zero alone is
never success.

Python 3.10 compatible. Fixed argv arrays, shell=False everywhere.
"""
from __future__ import annotations

import json
import os
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
    default_measure_footprint,
    extract_version_token,
    fingerprint,
    footprint_has_unknown,
    get_tool_inventory,
    measure_tree_bytes,
    mutation_timeout_for,
    plan_has_unknown_budget,
    resolve_executable,
    required_space_bytes,
    run_fixed,
    sanitize_evidence,
)
from ..schemas import utcnow_iso

HERMES_BIN = "/home/ubuntu/.local/bin/hermes"
HERMES_REPO = "/home/ubuntu/.hermes/hermes-agent"
HERMES_BRANCH = "main"
HERMES_VENV_HINTS = (
    "/home/ubuntu/.hermes/hermes-agent/.venv",
    "/home/ubuntu/.hermes/.venv",
)

# R28: no fixed invented staging/backup constants. Footprint derives from
# measure_footprint (repo .git + state homes, bounded walk 200k entries);
# full native backup requires trustworthy size + space + artifact validation
# AFTER mutation (path/scope/size from actual artifact). Floor comparison in
# runner, never adapters.

# Inventoried gateway/serve units: (scope, unit). Scope "system" needs
# confirmed restart authority before mutation. Override via
# EGA_HERMES_UNITS="system:hermes-gateway.service,user:hermes-serve.service".
DEFAULT_UNITS = (
    ("system", "hermes-gateway.service"),
    ("system", "hermes-serve.service"),
)


def _emit_noop(stream, line):
    # type: (str, str) -> None
    return None


def _parse_units():
    # type: () -> List[Tuple[str, str]]
    raw = os.environ.get("EGA_HERMES_UNITS", "").strip()
    if not raw:
        return list(DEFAULT_UNITS)
    units = []
    for token in raw.split(","):
        token = token.strip()
        if not token or ":" not in token:
            continue
        scope, name = token.split(":", 1)
        scope, name = scope.strip(), name.strip()
        if scope in ("system", "user") and name:
            units.append((scope, name))
    return units or list(DEFAULT_UNITS)


def _service_units_from_settings():
    # type: () -> List[Tuple[str, str]]
    """Inventoried Hermes units via settings.service_units (config-owned).

    Read defensively with defaults: settings.service_units is owned by the
    config contract and may be absent (this adapter never creates config
    keys). Accepted shapes: dict with hermes unit lists, or None. Any
    unparsable shape falls back to _parse_units() (EGA_HERMES_UNITS env +
    DEFAULT_UNITS). Never raises.
    """
    try:
        from ..config import settings as _settings

        raw = getattr(_settings, "service_units", None)
    except Exception:
        return _parse_units()
    try:
        if raw is None:
            return _parse_units()
        candidates = []  # type: List[object]
        if isinstance(raw, dict):
            for key in ("hermes", "hermes_units", "hermesUnits", "units"):
                val = raw.get(key)
                if isinstance(val, list):
                    candidates = val
                    break
            else:
                return _parse_units()
        elif isinstance(raw, list):
            candidates = raw
        else:
            return _parse_units()
        parsed = []
        for entry in candidates:
            if isinstance(entry, str) and ":" in entry:
                scope, _, name = entry.partition(":")
                scope, name = scope.strip(), name.strip()
                if scope in ("system", "user") and name:
                    parsed.append((scope, name))
            elif isinstance(entry, dict):
                scope = str(entry.get("scope", "")).strip()
                name = str(entry.get("name", "") or entry.get("unit", "")).strip()
                if scope in ("system", "user") and name:
                    parsed.append((scope, name))
        return parsed or _parse_units()
    except Exception:
        return _parse_units()


def _inventoried_units():
    # type: () -> List[Tuple[str, str]]
    """Single source for inventoried units: settings first, env fallback."""
    try:
        units = _service_units_from_settings()
        if units:
            return units
    except Exception:
        pass
    return _parse_units()


# Narrowly-scoped restart authority: ubuntu may run passwordless ONLY the
# exact systemctl commands for the inventoried Hermes units listed in
# deploy/etc/sudoers.d/ega-update-hermes.example (placeholder unit names
# there are inventory-gated and must match _inventoried_units()).
# Generic `sudo -n true` is NEVER used as authority proof.
_SUDO = "/usr/bin/sudo"
_SYSTEMCTL = "/bin/systemctl"


def _allowed_show_argv(unit):
    # type: (str) -> List[str]
    """Exact allow-list shape for the authority probe of one system unit."""
    return [_SUDO, "-n", _SYSTEMCTL, "--no-pager", "show", unit]


def _allowed_restart_argv(unit):
    # type: (str) -> List[str]
    """Exact allow-list shape for restarting one system unit."""
    return [_SUDO, "-n", _SYSTEMCTL, "restart", unit]


class HermesAdapter(Adapter):
    tool_id = "hermes"
    enabled = True

    def __init__(self):
        # type: () -> None
        self._emit = _emit_noop

    # -- probes --------------------------------------------------------

    def measure_footprint(self, plan=None):
        # type: (object) -> dict
        """Measured actuals {fs_path: bytes} (repo .git + state homes)."""
        try:
            trees = [os.path.join(HERMES_REPO, ".git"), HERMES_REPO,
                     "/home/ubuntu/.hermes"]
            try:
                homes = getattr(plan, "state_homes", None)
                if isinstance(homes, list):
                    for home in homes:
                        if isinstance(home, str) and home and home not in trees:
                            trees.append(home)
            except Exception:
                pass
            dbs = []
            return default_measure_footprint("hermes", db_paths=dbs, tree_paths=trees)
        except Exception:
            return {"unknown:hermes": -1}

    def _venv_path(self):
        # type: () -> str
        """Resolved venv for identity binding (inventory preferred)."""
        try:
            inv = get_tool_inventory("hermes")
            if isinstance(inv, dict):
                for key in ("runtime_paths", "runtime", "venv", "paths"):
                    val = inv.get(key)
                    if isinstance(val, dict):
                        for sub in ("venv", "virtualenv", "state_home", "working_directory"):
                            cand = val.get(sub, "")
                            if isinstance(cand, str) and cand and os.path.exists(cand):
                                return cand
                    elif isinstance(val, str) and val and os.path.exists(val) and "venv" in val:
                        return val
        except Exception:
            pass
        for hint in HERMES_VENV_HINTS:
            try:
                if os.path.exists(hint):
                    return hint
            except Exception:
                continue
        return ""

    def _expected_remote(self):
        # type: () -> str
        """Inventory expected_remote (source_status.remote / remote_url)."""
        try:
            inv = get_tool_inventory("hermes")
            if not isinstance(inv, dict):
                return ""
            for key in ("source_status", "source", "git", "repo"):
                val = inv.get(key)
                if isinstance(val, dict):
                    for sub in ("remote", "remote_url", "url", "origin"):
                        cand = val.get(sub, "")
                        if isinstance(cand, str) and cand.strip():
                            return cand.strip()
            for key in ("expected_remote", "remote", "remote_url"):
                cand = inv.get(key, "")
                if isinstance(cand, str) and cand.strip():
                    return cand.strip()
        except Exception:
            pass
        return ""

    def _git_remote(self):
        # type: () -> Tuple[str, str]
        """Return (remote_url, error). Every git exit checked by callers."""
        res = run_fixed(["/usr/bin/git", "-C", HERMES_REPO, "remote", "get-url", "origin"],
                        timeout=60, scope_unit=None)
        if not res.ok():
            return "", "remote get-url failed exit=%d" % res.exit_code
        url = (res.stdout.strip().splitlines()[0].strip()
               if res.stdout.strip() else "")
        if not url:
            return "", "remote URL empty"
        return url, ""

    def _version_probe(self):
        # type: () -> Tuple[str, str]
        res = run_fixed([HERMES_BIN, "--version"], timeout=60, scope_unit=None)
        self._emit("stdout", "$ hermes --version -> exit=%d" % res.exit_code)
        if not res.ok():
            return "", sanitize_evidence((res.stdout + res.stderr).strip()[:2000])
        token = extract_version_token(res.stdout + "\n" + res.stderr)
        if not token:
            for line in (res.stdout + "\n" + res.stderr).splitlines():
                if line.strip():
                    token = line.strip()[:100]
                    break
        return token, sanitize_evidence((res.stdout + res.stderr).strip()[:2000])

    def _capabilities(self):
        # type: () -> Dict[str, object]
        caps = {"help": False, "plan": False, "check": False,
                "yes": False, "backup": False, "detail": ""}
        help_res = run_fixed([HERMES_BIN, "update", "--help"], timeout=60, scope_unit=None)
        self._emit("stdout", "$ hermes update --help -> exit=%d" % help_res.exit_code)
        if not help_res.ok():
            caps["detail"] = sanitize_evidence("update --help exit=%d" % help_res.exit_code)
            return caps
        caps["help"] = True
        text = help_res.stdout + "\n" + help_res.stderr
        caps["plan"] = "--plan" in text
        caps["check"] = "--check" in text
        caps["yes"] = "--yes" in text
        caps["backup"] = "--backup" in text
        caps["detail"] = sanitize_evidence(text[:2000])
        return caps

    def _git_state(self):
        # type: () -> Tuple[str, str, str]
        """Return (cleanliness, head, detail). EVERY git exit checked.

        Any git failure blocks (never defaults branch to main). Remote URL
        matched vs inventory expected_remote when inventory provides one;
        mismatch blocks. Identity digest binds wrapper+repo+venv+owner+remote
        +branch+clean in inspect().
        """
        head_res = run_fixed(["/usr/bin/git", "-C", HERMES_REPO, "rev-parse", "HEAD"],
                             timeout=60, scope_unit=None)
        if not head_res.ok():
            return "unknown", "", sanitize_evidence(
                "rev-parse failed exit=%d; unreadable git state blocks" % head_res.exit_code)
        head = head_res.stdout.strip().split()[0] if head_res.stdout.strip() else ""
        if not head:
            return "unknown", "", sanitize_evidence("rev-parse empty HEAD; blocks")
        status_res = run_fixed(
            ["/usr/bin/git", "-C", HERMES_REPO, "status", "--porcelain=v1"],
            timeout=60, scope_unit=None)
        if not status_res.ok():
            return "unknown", head, sanitize_evidence(
                "status --porcelain=v1 failed exit=%d; unreadable git state blocks" % status_res.exit_code)
        branch_res = run_fixed(
            ["/usr/bin/git", "-C", HERMES_REPO, "rev-parse", "--abbrev-ref", "HEAD"],
            timeout=60, scope_unit=None)
        if not branch_res.ok():
            return "unknown", head, sanitize_evidence(
                "branch probe failed exit=%d; never defaulting to %s, blocks" % (
                    branch_res.exit_code, HERMES_BRANCH))
        branch = branch_res.stdout.strip().split()[0] if branch_res.stdout.strip() else ""
        if not branch:
            return "unknown", head, sanitize_evidence("branch empty; blocks, never defaults")
        remote_url, remote_err = self._git_remote()
        if not remote_url:
            return "unknown", head, sanitize_evidence(
                "remote URL unproven (%s); blocks" % remote_err)
        expected = self._expected_remote()
        if expected and remote_url.strip() != expected.strip():
            return "dirty", head, sanitize_evidence(
                "remote mismatch: got %r expected %r; blocks" % (
                    remote_url[:120], expected[:120]))
        dirty_lines = [ln for ln in status_res.stdout.splitlines() if ln.strip()]
        if dirty_lines:
            return "dirty", head, sanitize_evidence(
                "dirty checkout (%d entries incl. untracked); --force never used" % len(dirty_lines))
        if branch != HERMES_BRANCH:
            return "dirty", head, sanitize_evidence(
                "unexpected branch %r (expected %s)" % (branch, HERMES_BRANCH))
        venv = self._venv_path()
        return "clean", head, sanitize_evidence(
            "clean on %s at %s remote=%s venv=%s" % (
                branch, head[:12], remote_url[:80], venv or "?"))

    def _owner_of(self, path):
        # type: (str) -> str
        try:
            st = os.stat(path)
            return "%d:%d" % (st.st_uid, st.st_gid)
        except OSError:
            return "unknown"

    def _unit_show(self, scope, unit):
        # type: (str, str) -> Dict[str, str]
        base = ["/bin/systemctl"]
        if scope == "user":
            base.append("--user")
        res = run_fixed(
            base + ["show", unit, "-p", "LoadState", "-p", "ActiveState",
                    "-p", "SubState", "-p", "MainPID", "-p", "User",
                    "-p", "ExecStart"],
            timeout=30, scope_unit=None)
        info = {"LoadState": "", "ActiveState": "", "SubState": "",
                "MainPID": "", "User": "", "ExecStart": ""}
        if not res.ok():
            return info
        for line in res.stdout.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                if key in info:
                    info[key] = value.strip()
        return info

    def _inventory_allowed_restart_argv(self):
        # type: () -> List[List[str]]
        """Inventory-allowed precise restart argv list (fail-closed)."""
        try:
            inv = get_tool_inventory("hermes")
            if not isinstance(inv, dict):
                return []
            for key in ("allowed_restart_argv", "restart_argv",
                        "allowed_commands", "restart_commands"):
                val = inv.get(key)
                if isinstance(val, list) and val:
                    out = []
                    for entry in val:
                        if isinstance(entry, list) and entry:
                            out.append([str(x) for x in entry])
                        elif isinstance(entry, str) and entry.strip():
                            out.append(entry.strip().split())
                    if out:
                        return out
            # service_units with restart_authority allow-list shape.
            units = inv.get("service_units")
            if isinstance(units, list):
                out2 = []
                for entry in units:
                    if isinstance(entry, dict):
                        name = str(entry.get("name", "") or "")
                        scope = str(entry.get("scope", "") or "")
                        auth = str(entry.get("restart_authority", "") or "")
                        if name and scope == "system" and "allow" in auth.lower():
                            out2.append(_allowed_restart_argv(name))
                if out2:
                    return out2
        except Exception:
            pass
        return []

    def _has_system_restart_authority(self, units=None):
        # type: (object) -> Tuple[bool, str]
        """Verify exact noninteractive permission for PRECISE restart argv.

        sudo -n show proves NOTHING for restart authority. For each
        inventoried system unit the precise restart argv
        `sudo -n /bin/systemctl restart <unit>` is compared against the
        inventory-allowed restart argv list when inventory provides one
        (mismatch yields BLOCKED_RESTART_AUTHORITY without running sudo),
        then permission is verified WITHOUT restarting via
        `sudo -n -l <restart argv>` (list-only, never restarts). Missing
        authority blocks before mutation (install_method_unsupported with
        BLOCKED_RESTART_AUTHORITY detail). Never uses generic sudo proof.
        """
        try:
            if os.geteuid() == 0:
                return True, sanitize_evidence("running as root")
        except AttributeError:
            pass
        try:
            check_units = list(units) if units is not None else _inventoried_units()
        except Exception:
            check_units = _parse_units()
        system_units = [u for s, u in check_units if s == "system"]
        if not system_units:
            return True, sanitize_evidence("no system units in inventory; no elevated authority needed")
        allowed = set()
        for _u in system_units:
            allowed.add(tuple(_allowed_restart_argv(_u)))
        try:
            inv_allowed = self._inventory_allowed_restart_argv()
            if inv_allowed:
                allowed = set(tuple(a) for a in inv_allowed)
        except Exception:
            pass
        for _u in system_units:
            restart_argv = _allowed_restart_argv(_u)
            if tuple(restart_argv) not in allowed:
                return False, sanitize_evidence(
                    "BLOCKED_RESTART_AUTHORITY: precise restart argv for %s not in inventory allow-list" % _u)
            # Verify without restarting: sudo -n -l <precise restart argv>.
            list_argv = [_SUDO, "-n", "-l", _SYSTEMCTL, "restart", _u]
            probe = run_fixed(list_argv, timeout=15, scope_unit=None)
            if not probe.ok():
                return False, sanitize_evidence(
                    "BLOCKED_RESTART_AUTHORITY: no confirmed passwordless authority for precise restart %s "
                    "(sudo -n -l exit=%d); refusing to prompt sudo mid-job; install sudoers snippet per RUNBOOK)" % (
                        " ".join(restart_argv), probe.exit_code))
            combined = (probe.stdout + "\n" + probe.stderr)
            if _u not in combined and "restart" not in combined.lower():
                return False, sanitize_evidence(
                    "BLOCKED_RESTART_AUTHORITY: sudo -n -l does not list precise restart for %s; blocks" % _u)
        return True, sanitize_evidence(
            "precise restart authority confirmed for %d system unit(s) via sudo -n -l (show proves nothing)" % len(system_units))

    def _hermes_processes(self):
        # type: () -> Tuple[int, str]
        """Bounded ps for hermes foreground/session evidence."""
        proc = run_fixed(["/bin/ps", "-eo", "pid,comm,args"], timeout=30,
                         scope_unit=None)
        if not proc.ok():
            return 0, sanitize_evidence("process probe unavailable")
        hits = [line for line in proc.stdout.splitlines()
                if "hermes" in line.lower() and "ps -eo" not in line]
        return len(hits), sanitize_evidence("%d hermes process line(s) observed" % len(hits))

    def _update_plan_probe(self):
        # type: () -> Tuple[str, str]
        caps = self._capabilities()
        if not caps.get("plan"):
            return "", sanitize_evidence("update --plan unsupported locally")
        res = run_fixed([HERMES_BIN, "update", "--plan"], timeout=300, scope_unit=None)
        self._emit("stdout", "$ hermes update --plan -> exit=%d" % res.exit_code)
        combined = (res.stdout + "\n" + res.stderr).strip()
        if not res.ok():
            return "", sanitize_evidence("update --plan exit=%d" % res.exit_code)
        return sanitize_evidence(combined[:2000]), ""

    # -- Adapter interface -----------------------------------------------

    def _effective_backup_policy(self):
        # type: () -> Dict[str, str]
        """Effective native policy: config keys if present, else caps+inventory."""
        # Config file keys (hermes config) take precedence when present.
        for cfg in ("/home/ubuntu/.hermes/config.json",
                    os.path.join(HERMES_REPO, "config.json"),
                    "/home/ubuntu/.config/hermes/config.json"):
            try:
                if os.path.isfile(cfg):
                    with open(cfg, "r", encoding="utf-8") as fh:
                        payload = fh.read(65536)
                    try:
                        data = json.loads(payload) if payload.strip() else {}
                    except Exception:
                        data = {}
                    if isinstance(data, dict):
                        for key in ("backup", "backup_policy", "backupPolicy"):
                            val = data.get(key)
                            if isinstance(val, dict) and val:
                                out = {}
                                for k, v in val.items():
                                    try:
                                        out[str(k)] = str(v)
                                    except Exception:
                                        continue
                                if out:
                                    return out
                            elif isinstance(val, str) and val.strip():
                                return {"mode": val.strip()}
            except OSError:
                continue
            except Exception:
                continue
        try:
            inv = get_tool_inventory("hermes")
            if isinstance(inv, dict):
                for key in ("backup_procedure", "backup_policy", "backup"):
                    val = inv.get(key)
                    if isinstance(val, dict) and val:
                        out2 = {}
                        for k, v in val.items():
                            try:
                                out2[str(k)] = str(v)
                            except Exception:
                                continue
                        if out2:
                            return out2
        except Exception:
            pass
        try:
            caps = self._capabilities()
            mode = "full" if caps.get("backup") else "quick-limited"
        except Exception:
            mode = "quick-limited"
        return {"mode": mode}

    def inspect(self):
        # type: () -> InspectResult
        ok, resolved, detail = resolve_executable(HERMES_BIN)
        version, _raw = self._version_probe() if ok else ("", "unresolvable wrapper")
        cleanliness, head, git_detail = self._git_state()
        owner = self._owner_of(HERMES_BIN)
        # Identity binds wrapper resolved target + repo root + venv + owner +
        # remote URL match + exact branch + clean status (R22).
        venv = self._venv_path()
        remote_url, _rerr = self._git_remote()
        if not remote_url:
            remote_url = "unknown-remote"
        fp = fingerprint(HERMES_BIN, resolved, HERMES_REPO, venv, owner,
                         remote_url, head, cleanliness, version)
        units = ["%s:%s" % (scope, unit) for scope, unit in _inventoried_units()]
        state_dirs = [d for d in ("/home/ubuntu/.hermes", HERMES_REPO) if os.path.exists(d)]
        return InspectResult(
            tool=self.tool_id,
            install_identity=sanitize_evidence(
                "hermes-native:%s@%s remote=%s venv=%s" % (
                    (resolved or HERMES_BIN), head[:12] if head else "?",
                    remote_url[:60], venv or "?")),
            executable=HERMES_BIN,
            resolved_target=resolved,
            version=version,
            commit=head,
            install_kind="git-native",
            owner=owner,
            source_clean=cleanliness,
            source_detail=sanitize_evidence(git_detail),
            services=units,
            state_dirs=state_dirs,
            channel=HERMES_BRANCH,
            fingerprint=fp,
        )

    def discover(self):
        # type: () -> DiscoverResult
        ok, _resolved, detail = resolve_executable(HERMES_BIN)
        if not ok:
            return DiscoverResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel=HERMES_BRANCH, available=False,
                unknown_reason=sanitize_evidence("install_method_unsupported: %s" % detail))
        if not os.path.isdir(HERMES_REPO):
            return DiscoverResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel=HERMES_BRANCH, available=False,
                unknown_reason=sanitize_evidence("hermes repo missing at %s" % HERMES_REPO))
        caps = self._capabilities()
        if not caps.get("help") or not caps.get("yes"):
            return DiscoverResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel=HERMES_BRANCH, available=False,
                unknown_reason=sanitize_evidence(
                    "supported noninteractive procedure unconfirmed: %s" % caps.get("detail", "")[:300]))
        planned, error = self._update_plan_probe()
        if error or not planned:
            return DiscoverResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel=HERMES_BRANCH, available=False,
                unknown_reason=sanitize_evidence(
                    "update --plan probe failed: %s" % (error or "empty plan output")[:300]))
        target = "main-latest"
        for line in planned.splitlines():
            token = line.strip()
            if len(token) == 40 and all(c in "0123456789abcdef" for c in token.lower()):
                target = token
                break
        return DiscoverResult(
            tool=self.tool_id, target=target, target_mode="native_latest",
            channel=HERMES_BRANCH, available=True, unknown_reason="")

    def activity(self):
        # type: () -> ActivityResult
        # Configured units + foreground/session evidence (bounded ps for
        # hermes processes); missing task evidence -> unknown (never idle
        # without proof). Capability flags honored: update --check only when
        # advertised.
        busy_units = []
        unknown_units = []
        for scope, unit in _inventoried_units():
            info = self._unit_show(scope, unit)
            if info.get("LoadState") not in ("loaded",):
                unknown_units.append("%s:%s(%s)" % (scope, unit, info.get("LoadState") or "no-loadstate"))
            elif info.get("ActiveState") == "active" and info.get("SubState") == "running":
                unknown_units.append("%s:%s(running-unproven-task)" % (scope, unit))
        count, ps_evidence = self._hermes_processes()
        if "unavailable" in ps_evidence:
            unknown_units.append("ps-unavailable")
        elif count > 0:
            unknown_units.append("foreground(%s)" % ps_evidence[:80])
        try:
            caps = self._capabilities()
            check_advertised = bool(caps.get("check"))
        except Exception:
            check_advertised = False
            caps = {}
        check_text = ""
        check_ok = False
        if check_advertised:
            check_res = run_fixed([HERMES_BIN, "update", "--check"], timeout=300,
                                  scope_unit=None)
            check_text = sanitize_evidence((check_res.stdout + "\n" + check_res.stderr).strip()[:500])
            check_ok = bool(check_res.ok())
        else:
            check_text = sanitize_evidence("update --check not advertised; skipped (capability-gated)")
        if busy_units:
            return ActivityResult(
                tool=self.tool_id, state="busy",
                evidence=sanitize_evidence("busy: %s" % "; ".join(busy_units)[:800]),
                checked_at=utcnow_iso())
        if unknown_units or (check_advertised and not check_ok):
            return ActivityResult(
                tool=self.tool_id, state="unknown",
                evidence=sanitize_evidence(
                    "gateway/task state unproven (%s; check=%s); ack required"
                    % ("; ".join(unknown_units)[:400], check_text[:200])),
                checked_at=utcnow_iso())
        # Idle requires: no unknown units, no foreground processes, and (when
        # advertised) a passing --check. Missing task evidence stays unknown.
        if count > 0 or "unavailable" in ps_evidence:
            return ActivityResult(
                tool=self.tool_id, state="unknown",
                evidence=sanitize_evidence(
                    "task evidence missing (ps=%s; check=%s); ack required" % (
                        ps_evidence[:200], check_text[:200])),
                checked_at=utcnow_iso())
        return ActivityResult(
            tool=self.tool_id, state="idle",
            evidence=sanitize_evidence("gateways idle; %s" % check_text[:300]),
            checked_at=utcnow_iso())

    def plan(self):
        # type: () -> PlanResult
        inspection = self.inspect()
        discovery = self.discover()
        required_checks = ["final_commit", "cli_diagnostics", "gateway_running_commit",
                           "running_version"]
        try:
            for _scope, _unit in _inventoried_units():
                required_checks.append("unit:%s:%s" % (_scope, _unit))
        except Exception:
            pass
        # doctor is mandatory when the CLI advertises it; missing later is
        # fail/unknown per manifest, never silent drop (verify enforces).
        try:
            _help_probe = run_fixed([HERMES_BIN, "doctor", "--help"], timeout=60,
                                    scope_unit=None)
            if _help_probe.ok():
                required_checks.append("doctor")
        except Exception:
            pass
        if not discovery.available:
            _blocked = PlanResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel=HERMES_BRANCH, fingerprint=inspection.fingerprint,
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
                backup_policy=self._effective_backup_policy(),
                required_probes=list(required_checks),
                budgets={"unknown:hermes": -1}, space_fs={},
                deadlines={}, restart_detail=sanitize_evidence(discovery.unknown_reason),
                activity_ts="", release_path="",
                required_checks=list(required_checks), scope_unit=None)
            return _blocked
        policy = self._effective_backup_policy()
        backup_mode = str(policy.get("mode", "") or ("full" if self._capabilities().get("backup") else "quick-limited"))
        if backup_mode not in ("full", "quick-limited") and "full" in backup_mode:
            backup_mode = "full"
        elif backup_mode not in ("full", "quick-limited"):
            backup_mode = "quick-limited" if "quick" in backup_mode else backup_mode
        footprint = self.measure_footprint(None)
        try:
            backup_bytes = sum(v for v in footprint.values() if isinstance(v, int) and v >= 0)
        except Exception:
            backup_bytes = -1
        budgets = {}
        space_fs = {}
        try:
            from .registry import filesystems_for as _fs_for
            import shutil as _shutil

            affected = [HERMES_BIN, HERMES_REPO, "/var/lib/ega-update/backups"]
            for mount in _fs_for(affected):
                if footprint_has_unknown(footprint):
                    budgets[mount] = -1
                else:
                    try:
                        budgets[mount] = int(backup_bytes)
                    except (TypeError, ValueError):
                        budgets[mount] = -1
                try:
                    space_fs[mount] = int(_shutil.disk_usage(mount).free)
                except OSError:
                    space_fs[mount] = -1
        except Exception:
            budgets = {"unknown:hermes": -1}
            space_fs = {}
        if budgets and all(isinstance(v, int) and v >= 0 for v in budgets.values()):
            try:
                need = max(int(v) for v in budgets.values())
            except Exception:
                need = 0
        else:
            need = 0
        inventoried = _inventoried_units()
        system_units = [unit for scope, unit in inventoried if scope == "system"]
        authority_ok, authority_detail = self._has_system_restart_authority(inventoried)
        restart_impact = sanitize_evidence(
            "native restart handling; gateway/serve units %s" % ", ".join(
                "%s:%s" % (scope, unit) for scope, unit in inventoried))
        if system_units and not authority_ok:
            restart_impact = sanitize_evidence(
                "%s BLOCKED_RESTART_AUTHORITY: %s" % (restart_impact, authority_detail))
        # Precise restart argv recorded (planning verifies without restarting).
        try:
            precise = ["; ".join(_allowed_restart_argv(u)) for _s, u in inventoried if _s == "system"]
        except Exception:
            precise = []
        plan_obj = PlanResult(
            tool=self.tool_id, target=discovery.target, target_mode="native_latest",
            channel=HERMES_BRANCH, fingerprint=inspection.fingerprint,
            services=["%s:%s" % (scope, unit) for scope, unit in inventoried],
            backup_scope={
                "covered": sanitize_evidence(
                    "repo state + gateway config (effective policy %s; full requires trustworthy size+space+artifact validation AFTER mutation)" % backup_mode),
                "omitted": sanitize_evidence("hermes home wholesale"),
                "consistency": sanitize_evidence(
                    "native --backup full executes inside mutation; backup phase alone is not a full backup; --backup flag/output keyword never proof"),
                "mode": sanitize_evidence(backup_mode),
            },
            required_space_bytes=need,
            steps=["preflight", "backup", "updating", "verifying"],
            timeouts=dict(ADAPTER_TIMEOUT_DEFAULTS),
            restart_impact=restart_impact,
            already_current=False,
        )
        attach_plan_v2(
            plan_obj, install_identity=inspection.install_identity,
            artifact={"target": discovery.target, "channel": HERMES_BRANCH},
            config_hash="", plan_hash="",
            launch={"precise_restart_argv": precise},
            state_homes=list(inspection.state_dirs or []),
            backup_policy=dict(policy),
            required_probes=list(required_checks),
            budgets=dict(budgets), space_fs=dict(space_fs),
            deadlines={}, restart_detail=restart_impact,
            activity_ts="", release_path="",
            required_checks=list(required_checks), scope_unit=None)
        return plan_obj

    def backup(self, job_id):
        # type: (str) -> BackupResult
        # Backup phase records capability + metadata ONLY. A requested full
        # backup is performed natively INSIDE the mutation command; this phase
        # never claims a full backup. Full requires trustworthy size (measure
        # repo .git + state homes) + space; artifact validation happens AFTER
        # mutation (path/scope/size from actual artifact, never flag/keyword).
        policy = self._effective_backup_policy()
        mode = str(policy.get("mode", "") or "")
        if not mode:
            try:
                caps = self._capabilities()
                mode = "full" if caps.get("backup") else "quick-limited"
            except Exception:
                mode = "quick-limited"
        try:
            from ..config import settings as _settings

            backup_root = _settings.backup_dir or "/var/lib/ega-update/backups"
        except Exception:
            backup_root = "/var/lib/ega-update/backups"
        dest_dir = os.path.join(backup_root, job_id)
        scope = {
            "covered": sanitize_evidence(
                "repo HEAD + status snapshot, gateway config capability+metadata (effective policy %s; full executes natively inside mutation)" % mode),
            "omitted": sanitize_evidence("hermes home wholesale"),
            "consistency": sanitize_evidence(
                "git metadata snapshot + native capability record; backup phase alone is not a full backup"),
            "mode": sanitize_evidence(
                ("%s (limited state protection, not full rollback)" % mode)
                if mode != "full" else "full-requested (native backup inside mutation; artifact validation AFTER mutation required)"),
        }
        # Full requires trustworthy size + space now (fail-closed).
        if mode.startswith("full"):
            try:
                footprint = self.measure_footprint(None)
                if footprint_has_unknown(footprint):
                    return BackupResult(
                        tool=self.tool_id, supported=False, path="", scope=scope,
                        consistency=scope["consistency"], size_bytes=0,
                        unsupported_reason=sanitize_evidence(
                            "backup_unsupported: full requires trustworthy size but footprint unknown"))
                size_est = sum(int(v) for v in footprint.values() if isinstance(v, int) and v >= 0)
                from .registry import filesystems_for as _fs_for, check_disk as _check_disk

                mounts = _fs_for([HERMES_REPO, dest_dir])
                disk_ok, disk_detail, _pf = _check_disk(mounts, size_est)
                if not disk_ok:
                    return BackupResult(
                        tool=self.tool_id, supported=False, path="", scope=scope,
                        consistency=scope["consistency"], size_bytes=0,
                        unsupported_reason=sanitize_evidence("disk_blocked: %s" % disk_detail[:300]))
            except Exception as exc:
                return BackupResult(
                    tool=self.tool_id, supported=False, path="", scope=scope,
                    consistency=scope["consistency"], size_bytes=0,
                    unsupported_reason=sanitize_evidence("backup_unsupported: size/space unproven: %s" % str(exc)[:200]))
        cleanliness, head, git_detail = self._git_state()
        if cleanliness != "clean":
            return BackupResult(
                tool=self.tool_id, supported=False, path="", scope=scope,
                consistency=scope["consistency"], size_bytes=0,
                unsupported_reason=sanitize_evidence("backup_unsupported: %s" % git_detail))
        try:
            os.makedirs(dest_dir, mode=0o700, exist_ok=True)
            with open(os.path.join(dest_dir, "hermes-pre-update.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write("head=%s\nclean=%s\ndetail=%s\npolicy=%s\n" % (
                    head, cleanliness, git_detail, mode))
            size = os.path.getsize(os.path.join(dest_dir, "hermes-pre-update.txt"))
        except OSError as exc:
            return BackupResult(
                tool=self.tool_id, supported=False, path="", scope=scope,
                consistency=scope["consistency"], size_bytes=0,
                unsupported_reason=sanitize_evidence("backup_failed: %s" % exc))
        return BackupResult(
            tool=self.tool_id, supported=True, path=dest_dir, scope=scope,
            consistency=scope["consistency"], size_bytes=size,
            unsupported_reason="")

    def _native_backup_artifact(self, job_id, before_head):
        # type: (str, str) -> Tuple[bool, str, int]
        """Validate native backup artifact AFTER mutation (R23).

        --backup in argv or 'backup' substring in output is NOT proof.
        Proof requires an actual artifact: path/scope/size recorded from the
        filesystem (new files under the job backup dir or tool-reported
        structured path). Returns (ok, path, size).
        """
        try:
            from ..config import settings as _settings

            backup_root = _settings.backup_dir or "/var/lib/ega-update/backups"
        except Exception:
            backup_root = "/var/lib/ega-update/backups"
        dest_dir = os.path.join(backup_root, job_id)
        try:
            if not os.path.isdir(dest_dir):
                return False, "", 0
            # Any new regular file with recent mtime and non-zero size counts
            # when its content references the pre-update HEAD or backup scope.
            import time as _time

            now = _time.time()
            best_path = ""
            best_size = 0
            for root, _dirs, files in os.walk(dest_dir):
                for name in files:
                    full = os.path.join(root, name)
                    try:
                        st = os.stat(full)
                        if not st or st.st_size <= 0:
                            continue
                        # Artifact must be recent (within 2h of mutation).
                        if now - float(getattr(st, "st_mtime", 0)) > 7200:
                            continue
                        if st.st_size > best_size:
                            best_size = int(st.st_size)
                            best_path = full
                    except OSError:
                        continue
            if best_path and best_size > 0:
                return True, sanitize_evidence(best_path), int(best_size)
            return False, "", 0
        except Exception:
            return False, "", 0

    def execute(self, plan, job_id, activity_ack=False):
        # type: (PlanResult, str, bool) -> ExecuteResult
        # Frozen: use plan-provided values; fresh rechecks INVALIDATE (blocked)
        # but never substitute.
        if plan.target_mode == "exact":
            _mismatch = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version="",
                after_version="", state="blocked", error_code="invalid_request",
                error_detail=sanitize_evidence(
                    "exact-target mismatch: hermes supports only target_mode=native_latest"))
            return attach_timed_out(_mismatch, False)
        if plan_has_unknown_budget(plan):
            _cur0 = ""
            try:
                _cur0 = self.inspect().version
            except Exception:
                pass
            _bd0 = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=_cur0,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail=sanitize_evidence("unknown footprint blocks mutation (budgets -1)"))
            return attach_timed_out(_bd0, False)
        try:
            scope_unit = getattr(plan, "scope_unit", None)
        except Exception:
            scope_unit = None
        ok, resolved, detail = resolve_executable(HERMES_BIN)
        if not ok:
            _b1 = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version="",
                after_version="", state="blocked", error_code="install_method_unsupported",
                error_detail=sanitize_evidence(detail))
            return attach_timed_out(_b1, False)
        current = self.inspect()
        if plan.fingerprint and current.fingerprint != plan.fingerprint:
            _bf = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="fingerprint_changed",
                error_detail=sanitize_evidence("installation changed since plan; fresh plan required"))
            return attach_timed_out(_bf, False)
        if current.source_clean != "clean":
            _bg = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="git_dirty",
                error_detail=sanitize_evidence(current.source_detail[:500]))
            return attach_timed_out(_bg, False)
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
                error_detail=sanitize_evidence(
                    "unknown activity requires plan-specific owner ack: %s" % activity.evidence[:400]))
            return attach_timed_out(_bk, False)
        if activity.state == "unknown" and activity_ack:
            self._emit("stdout", "proceeding with recorded activity ack: %s"
                       % sanitize_evidence(activity.evidence[:400]))
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
                need = sum(int(v) for v in fp_now.values() if isinstance(v, int) and v >= 0)
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
            [HERMES_BIN, HERMES_REPO, "/var/lib/ega-update/backups"], need_int)
        if not disk_ok:
            _bd = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail=sanitize_evidence(disk_detail[:500]))
            return attach_timed_out(_bd, False)
        caps = self._capabilities()
        if not caps.get("yes"):
            _by = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="install_method_unsupported",
                error_detail=sanitize_evidence("noninteractive --yes unsupported locally"))
            return attach_timed_out(_by, False)
        # Capability flags honored: --check already gated in activity(); here
        # --backup only when advertised (plan policy must match).
        inventoried = _inventoried_units()
        system_units = [(s, u) for s, u in inventoried if s == "system"]
        if system_units:
            authority_ok, authority_detail = self._has_system_restart_authority(inventoried)
            if not authority_ok:
                _blocked = ExecuteResult(
                    tool=self.tool_id, exit_code=3, before_version=current.version,
                    after_version="", state="blocked",
                    error_code="install_method_unsupported",
                    error_detail=sanitize_evidence(
                        "BLOCKED_RESTART_AUTHORITY: %s" % authority_detail))
                return attach_timed_out(_blocked, False)
        try:
            backup_mode = str((plan.backup_scope or {}).get("mode", "") or "")
            if not backup_mode:
                backup_mode = str(self._effective_backup_policy().get("mode", "quick-limited"))
        except Exception:
            backup_mode = "quick-limited"
        # Execute uses ONLY the authorized path: native updater path must
        # match the inventoried wrapper; else blocked.
        try:
            if resolved != HERMES_BIN and not resolved.startswith("/home/ubuntu/.hermes"):
                _bp = ExecuteResult(
                    tool=self.tool_id, exit_code=3, before_version=current.version,
                    after_version="", state="blocked", error_code="install_method_unsupported",
                    error_detail=sanitize_evidence(
                        "native updater path %s does not match authorized %s; blocked" % (
                            resolved, HERMES_BIN)))
                return attach_timed_out(_bp, False)
        except Exception:
            pass
        argv = [HERMES_BIN, "update", "--yes"]
        if backup_mode.startswith("full"):
            if not caps.get("backup"):
                _bb = ExecuteResult(
                    tool=self.tool_id, exit_code=3, before_version=current.version,
                    after_version="", state="blocked", error_code="backup_unsupported",
                    error_detail=sanitize_evidence(
                        "full backup requested but native --backup unsupported; refusing silent downgrade"))
                return attach_timed_out(_bb, False)
            argv.append("--backup")
        before = current.version
        before_head = current.commit
        mutation_to = mutation_timeout_for(plan, "updating", default=1800)
        res = run_fixed(argv, timeout=mutation_to, scope_unit=scope_unit)
        self._emit("stdout", "$ hermes update --yes%s -> exit=%d (mutation timeout %ds = step + %ds margin)"
                   % (" --backup" if "--backup" in argv else "", res.exit_code,
                      mutation_to, MUTATION_TIMEOUT_MARGIN_S))
        self._emit("stdout", sanitize_evidence((res.stdout or "")[-4000:]))
        if res.stderr:
            self._emit("stderr", sanitize_evidence((res.stderr or "")[-4000:]))
        if res.timed_out:
            _tout = ExecuteResult(
                tool=self.tool_id, exit_code=4, before_version=before,
                after_version="", state="install_failed", error_code="timeout",
                error_detail=sanitize_evidence("hermes update timed out; possible partial install"))
            return attach_timed_out(_tout, True)
        if res.exit_code != 0:
            after_probe, _raw = self._version_probe()
            _fail = ExecuteResult(
                tool=self.tool_id, exit_code=4, before_version=before,
                after_version=after_probe, state="install_failed",
                error_code="install_failed",
                error_detail=sanitize_evidence("hermes update exit=%d" % res.exit_code))
            return attach_timed_out(_fail, False)
        # Full-backup artifact validation AFTER mutation (R23): path/scope/size
        # from the ACTUAL artifact; flag/output keyword is NOT proof.
        if backup_mode.startswith("full"):
            ok_art, art_path, art_size = self._native_backup_artifact(job_id, before_head or "")
            if not ok_art:
                after_probe, _raw = self._version_probe()
                _bfail = ExecuteResult(
                    tool=self.tool_id, exit_code=4, before_version=before,
                    after_version=after_probe, state="install_failed",
                    error_code="backup_failed",
                    error_detail=sanitize_evidence(
                        "full backup requested but no native artifact found (path/scope/size unproven); "
                        "flag/output keyword is not proof; refusing to claim full backup"))
                return attach_timed_out(_bfail, False)
            self._emit("stdout", sanitize_evidence(
                "native backup artifact validated: %s size=%d" % (art_path, art_size))[:400])
        after_inspection = self.inspect()
        self._emit("stdout", sanitize_evidence(
            "hermes before=%s@%s after=%s@%s" % (
                before, (before_head or "")[:12], after_inspection.version,
                (after_inspection.commit or "")[:12])))
        _done = ExecuteResult(
            tool=self.tool_id, exit_code=0, before_version=before,
            after_version=after_inspection.version, state="succeeded",
            error_code="", error_detail="")
        return attach_timed_out(_done, False)

    def _service_commit(self):
        # type: () -> Tuple[str, str]
        """Service-reported commit/version (structured, never CLI alone)."""
        # Prefer `hermes gateway status --json` / `serve status` when advertised;
        # fall back to version probe (marked uncorroborated by callers).
        for argv in ([HERMES_BIN, "gateway", "status", "--json"],
                     [HERMES_BIN, "serve", "status", "--json"]):
            try:
                res = run_fixed(argv, timeout=60, scope_unit=None)
            except Exception:
                continue
            if not res.ok():
                continue
            try:
                payload = json.loads(res.stdout.strip() or "null")
            except Exception:
                continue
            if isinstance(payload, dict):
                for key in ("commit", "head", "sha", "version"):
                    val = str(payload.get(key, "") or "").strip()
                    if val:
                        return val[:40], sanitize_evidence("service %s=%s" % (key, val[:40]))
        return "", sanitize_evidence("no structured service commit available")

    def verify(self, plan=None, required_checks=None, **kwargs):
        # type: (...) -> VerifyResult
        """Verify with BOUND correlation (R24).

        Correlates unit User/ExecStart/MainPID-cgroup/repo artifact +
        service-reported commit/version; adds mandatory
        `gateway_running_commit` bound in required_checks. Missing
        diagnostics later fail/unknown per manifest, never silent drop.
        Required-checks coverage enforced by name. All summaries sanitized.
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
        # Manifest: doctor expected when help advertises it.
        try:
            _help_now = run_fixed([HERMES_BIN, "doctor", "--help"], timeout=60,
                                  scope_unit=None)
            _doctor_advertised = bool(_help_now.ok())
        except Exception:
            _doctor_advertised = False
        checks = []  # type: List[CheckItem]
        inspection = self.inspect()
        checks.append(CheckItem(
            name="final_commit", result="pass" if inspection.commit else "fail",
            mandatory=True,
            summary=sanitize_evidence(
                ("commit %s" % inspection.commit[:12]) if inspection.commit
                else "final commit unreadable")))
        diag = run_fixed([HERMES_BIN, "--version"], timeout=60, scope_unit=None)
        checks.append(CheckItem(
            name="cli_diagnostics", result="pass" if diag.ok() else "fail",
            mandatory=True, summary=sanitize_evidence(
                "diagnostics exit=%d version=%s" % (diag.exit_code, inspection.version or "?"))))
        # Doctor: advertised -> mandatory; missing later is fail/unknown, never drop.
        if _doctor_advertised or "doctor" in _required_names:
            try:
                doctor = run_fixed([HERMES_BIN, "doctor"], timeout=300, scope_unit=None)
                checks.append(CheckItem(
                    name="doctor", result="pass" if doctor.ok() else "fail",
                    mandatory=True, summary=sanitize_evidence(
                        "doctor exit=%d" % doctor.exit_code)))
            except Exception as exc:
                checks.append(CheckItem(
                    name="doctor", result="unknown", mandatory=True,
                    summary=sanitize_evidence("doctor probe inconclusive: %s" % str(exc)[:200])))
        all_running = True
        unit_infos = {}
        for scope, unit in _inventoried_units():
            info = self._unit_show(scope, unit)
            unit_infos[(scope, unit)] = info
            running = (info.get("LoadState") == "loaded"
                       and info.get("ActiveState") == "active"
                       and info.get("SubState") == "running")
            # MainPID-cgroup binding when running.
            cgroup_ok = True
            cgroup_detail = ""
            if running:
                pid = str(info.get("MainPID", "") or "")
                if not pid or pid == "0":
                    cgroup_ok = False
                    cgroup_detail = "MainPID missing for running unit"
                elif not cgroup_belongs_to_unit(pid, unit):
                    cgroup_ok = False
                    cgroup_detail = "MainPID=%s not in %s cgroup (stranger)" % (pid, unit)
                # ExecStart must reference the inventoried binary; User must match owner.
                exec_start = str(info.get("ExecStart", "") or "")
                if HERMES_BIN not in exec_start and HERMES_REPO not in exec_start:
                    cgroup_ok = False
                    cgroup_detail = (cgroup_detail + "; ExecStart does not reference %s" % HERMES_BIN).strip("; ")
                unit_user = str(info.get("User", "") or "")
                if unit_user and unit_user not in (inspection.owner, "ubuntu", "0:0"):
                    # Owner mismatch does not alone fail the unit check but is
                    # recorded for gateway_running_commit binding below.
                    cgroup_detail = (cgroup_detail + "; User=%s vs owner=%s" % (
                        unit_user, inspection.owner)).strip("; ")
            result = "pass" if (running and cgroup_ok) else "fail"
            checks.append(CheckItem(
                name="unit:%s:%s" % (scope, unit),
                result=result, mandatory=True,
                summary=sanitize_evidence(
                    "%s active=%s sub=%s pid=%s user=%s %s" % (
                        unit, info.get("ActiveState") or "?", info.get("SubState") or "?",
                        info.get("MainPID") or "?", info.get("User") or "?",
                        cgroup_detail or "cgroup-matched"))))
            all_running = all_running and (running and cgroup_ok)
        # gateway_running_commit: service commit/version bound to repo artifact
        # + running units (R24, mandatory, in required_checks).
        service_commit, service_detail = self._service_commit()
        repo_head = inspection.commit or ""
        if service_commit and repo_head and service_commit[:12] == repo_head[:12] and all_running:
            checks.append(CheckItem(
                name="gateway_running_commit", result="pass", mandatory=True,
                summary=sanitize_evidence(
                    "service commit %s matches repo %s; units running cgroup-matched (%s)" % (
                        service_commit[:12], repo_head[:12], service_detail[:120]))))
        elif not service_commit:
            checks.append(CheckItem(
                name="gateway_running_commit", result="unknown", mandatory=True,
                summary=sanitize_evidence(
                    "service commit unproven (%s); repo=%s running=%s; unknown, never silent" % (
                        service_detail[:200], repo_head[:12] or "?", all_running))))
        else:
            checks.append(CheckItem(
                name="gateway_running_commit", result="fail", mandatory=True,
                summary=sanitize_evidence(
                    "service commit %s vs repo %s mismatch or units not running (%s)" % (
                        service_commit[:12], repo_head[:12] or "?", service_detail[:160]))))
        checks.append(CheckItem(
            name="running_version", result="pass" if (inspection.version and all_running) else "fail",
            mandatory=True,
            summary=sanitize_evidence(
                "running version evidence version=%s gateways_running=%s"
                % (inspection.version or "?", all_running))))
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
        passed = bool(inspection.commit) and not any(
            c for c in checks if c.mandatory and c.result != "pass")
        return VerifyResult(
            tool=self.tool_id, version=inspection.version, checks=checks,
            passed=passed, error_code="" if passed else "health_failed",
            error_detail=sanitize_evidence("" if passed else "mandatory hermes checks failed"))
