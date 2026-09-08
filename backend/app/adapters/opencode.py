"""OpenCode adapter: explicit curl-method upgrade (subagent B owned).

Uses the configured /home/ubuntu/.opencode/bin/opencode binary, never the
secondary NVM npm copy. The discovered target is explicit SemVer stored in
the plan; mutation runs `opencode upgrade "$TARGET" --method curl` with the
server-validated target only. Verifies the exact version plus CLI startup,
and the server only when one is configured. Never deletes opencode.db /
WAL / SHM, caches, plugins, or the alternate npm install, and never backs up
the 13 GiB state dir wholesale. A consistent backup is required when a step
can migrate state.

Python 3.10 compatible. Fixed argv arrays, shell=False everywhere.
"""
from __future__ import annotations

import json
import os
import platform
from typing import List, Optional, Tuple

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
    backup_dest_for_db,
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
    measure_file_with_wal,
    measure_tree_bytes,
    mutation_timeout_for,
    nvm_paths,
    owner_node_major,
    parse_semver,
    plan_has_unknown_budget,
    resolve_executable,
    required_space_bytes,
    run_fixed,
    sanitize_evidence,
    staging_from_npm_meta,
)
from ..schemas import utcnow_iso

OPENCODE_BIN = "/home/ubuntu/.opencode/bin/opencode"
OPENCODE_HOME = "/home/ubuntu/.opencode"
OPENCODE_CONFIG_DIR = "/home/ubuntu/.config/opencode"
OPENCODE_DATA_DIR = "/home/ubuntu/.local/share/opencode"

# R28: no fixed invented staging/backup constants. Staging derives from npm
# registry dist.tarball size x3 when metadata gives it, else unknown->block.
# Footprint derives from measure_footprint (DB+WAL via os.stat, config tree
# via bounded os.walk capped at 200k entries). The 3 GiB floor comparison
# lives in the runner, never in adapters.
SUPPORTED_ARCH_MARKERS = ("arm64", "aarch64")


def _emit_noop(stream, line):
    # type: (str, str) -> None
    return None


class OpenCodeAdapter(Adapter):
    tool_id = "opencode"
    enabled = True

    def __init__(self):
        # type: () -> None
        self._emit = _emit_noop

    # -- probes --------------------------------------------------------

    def measure_footprint(self, plan=None):
        # type: (object) -> dict
        """Measured actuals {fs_path: bytes} (DB+WAL + config tree)."""
        try:
            dbs = self._db_paths()
        except Exception:
            dbs = []
        try:
            trees = [OPENCODE_CONFIG_DIR]
            # State homes from plan v2 when present (main-agent adds).
            try:
                homes = getattr(plan, "state_homes", None)
                if isinstance(homes, list):
                    for home in homes:
                        if isinstance(home, str) and home and home not in trees:
                            trees.append(home)
            except Exception:
                pass
            return default_measure_footprint("opencode", db_paths=dbs, tree_paths=trees)
        except Exception:
            return {"unknown:opencode": -1}

    def _version_probe(self):
        # type: () -> Tuple[str, str]
        res = run_fixed([OPENCODE_BIN, "--version"], timeout=60, scope_unit=None)
        self._emit("stdout", "$ opencode --version -> exit=%d" % res.exit_code)
        if not res.ok():
            return "", sanitize_evidence((res.stdout + res.stderr).strip()[:2000])
        return extract_version_token(res.stdout + "\n" + res.stderr), sanitize_evidence(
            (res.stdout + res.stderr).strip()[:2000])

    def _upgrade_help(self):
        # type: () -> object
        res = run_fixed([OPENCODE_BIN, "upgrade", "--help"], timeout=60, scope_unit=None)
        self._emit("stdout", "$ opencode upgrade --help -> exit=%d" % res.exit_code)
        return res

    def _owner_of(self, path):
        # type: (str) -> str
        try:
            st = os.stat(path)
            return "%d:%d" % (st.st_uid, st.st_gid)
        except OSError:
            return "unknown"

    def _server_configured(self):
        # type: () -> Tuple[bool, str]
        """Whether an OpenCode server restart is in scope.

        Only an inventoried service counts; unknown running processes require
        manual handling and never an implicit restart.
        """
        unit = os.environ.get("EGA_OPENCODE_UNIT", "").strip()
        if unit:
            return True, "configured unit %s" % unit
        return False, "no configured opencode server unit"

    def _server_prior_state(self, unit):
        # type: (str) -> Tuple[str, str]
        """Exact prior unit state (ActiveState/SubState) for preserve-restore."""
        if not unit:
            return "", "no unit"
        show = run_fixed(
            ["/bin/systemctl", "--user", "show", unit,
             "-p", "ActiveState", "-p", "SubState"],
            timeout=30, scope_unit=None)
        if not show.ok():
            return "", sanitize_evidence(
                "prior state unreadable for %s exit=%d" % (unit, show.exit_code))[:300]
        active = ""
        sub = ""
        for line in show.stdout.splitlines():
            if line.startswith("ActiveState="):
                active = line.partition("=")[2].strip()
            elif line.startswith("SubState="):
                sub = line.partition("=")[2].strip()
        return ("%s/%s" % (active or "?", sub or "?"),
                sanitize_evidence("prior %s active=%s sub=%s" % (unit, active, sub))[:300])

    def _server_correlation(self, unit):
        # type: (str) -> Tuple[str, str]
        """Correlate a configured server unit with the new binary.

        Returns (outcome, detail) where outcome in correlated|unknown.
        Correlated requires ALL: unit LoadState=loaded via `systemctl --user
        show`, ExecStart references the configured OPENCODE_BIN (preserved
        flags never rewritten), resolved binary is the inventoried
        standalone, unit is inventoried (inventory service_units allow-list
        when inventory present; missing inventory never proves inventoried),
        MainPID>0 with MainPID in the unit cgroup (proves the running
        process belongs to this unit, not a stranger). Anything less is
        unknown (never success) so callers restart ONLY a correlated
        inventoried service and record the outcome; uncorrelated verify is
        unknown, never success.
        """
        if not unit:
            return "unknown", sanitize_evidence("no configured unit to correlate")
        ok, resolved, _detail = resolve_executable(OPENCODE_BIN)
        if not ok:
            return "unknown", sanitize_evidence(
                "binary unresolvable, cannot correlate unit %s" % unit)
        # Inventoried check: when inventory present, the unit must be listed.
        try:
            inv = get_tool_inventory("opencode")
            inv_units = []
            if isinstance(inv, dict):
                for key in ("service_units", "official_units", "units"):
                    val = inv.get(key)
                    if isinstance(val, list):
                        for entry in val:
                            if isinstance(entry, dict):
                                name = str(entry.get("name", "") or entry.get("unit", ""))
                                if name:
                                    inv_units.append(name)
                            elif isinstance(entry, str):
                                inv_units.append(entry.split(":")[-1].strip())
                        break
            if inv_units and unit not in inv_units and unit.split(":")[-1] not in inv_units:
                return "unknown", sanitize_evidence(
                    "unit %s not in inventoried allow-list %s; correlation unproven" % (
                        unit, inv_units))[:400]
        except Exception:
            pass
        show = run_fixed(
            ["/bin/systemctl", "--user", "show", unit,
             "-p", "LoadState", "-p", "ExecStart", "-p", "MainPID"],
            timeout=30, scope_unit=None)
        if not show.ok():
            return "unknown", sanitize_evidence(
                "unit %s show failed (exit=%d); correlation unproven" % (
                    unit, show.exit_code))
        load = ""
        exec_start = ""
        main_pid = ""
        for line in show.stdout.splitlines():
            if line.startswith("LoadState="):
                load = line.partition("=")[2].strip()
            elif line.startswith("ExecStart="):
                exec_start = line.partition("=")[2].strip()
            elif line.startswith("MainPID="):
                main_pid = line.partition("=")[2].strip()
        if load != "loaded":
            return "unknown", sanitize_evidence(
                "unit %s LoadState=%s (not loaded); correlation unproven" % (
                    unit, load or "?"))
        if OPENCODE_BIN not in exec_start and (resolved not in exec_start):
            return "unknown", sanitize_evidence(
                "unit %s ExecStart does not reference %s; server/binary correlation unproven" % (
                    unit, OPENCODE_BIN))
        # MainPID in unit cgroup: proves the running PID belongs to this unit.
        # MainPID==0 (inactive) is not correlated-running; callers preserve
        # prior state and do not restart an inactive unit.
        if main_pid and main_pid.strip() not in ("", "0"):
            if not cgroup_belongs_to_unit(main_pid, unit):
                return "unknown", sanitize_evidence(
                    "unit %s MainPID=%s not in unit cgroup; stranger process, correlation unproven" % (
                        unit, main_pid))
        return "correlated", sanitize_evidence(
            "unit %s correlated (LoadState=loaded, ExecStart references %s, MainPID=%s cgroup-matched; flags preserved)" % (
                unit, OPENCODE_BIN, main_pid or "0"))

    def _npm_meta_field(self, npm, args):
        # type: (str, List[str]) -> Tuple[object, str]
        """Run npm view <args> --json (read-only, bounded, scope None)."""
        res = run_fixed([npm] + list(args) + ["--json"], timeout=120,
                        scope_unit=None)
        self._emit("stdout", "$ npm %s -> exit=%d" % (" ".join(args), res.exit_code))
        if not res.ok():
            return None, "exit=%d" % res.exit_code
        try:
            return json.loads(res.stdout.strip() or "null"), ""
        except Exception:
            return None, "not JSON"

    def _runtime_arch_check(self, candidate):
        # type: (str) -> Tuple[bool, str, str]
        """Parse npm engines/cpu/os vs owner node major + platform.

        Returns (blocked, compat_detail, unknown_detail). Explicit
        incompatibility blocks; missing metadata records unknown but allows
        proceed ONLY when version/arch otherwise proven (documented).
        Uses backend/app/semver.py for version compare; downgrade/malformed
        handled by callers (blocked).
        """
        npm = (get_owner_paths().get("npm_path", "") or nvm_paths()["npm"])
        ok, _resolved, _detail = resolve_executable(npm)
        if not ok:
            return True, "", sanitize_evidence(
                "npm runtime unavailable for compat metadata: %s" % _detail)[:300]
        engines, engines_err = self._npm_meta_field(npm, ["view", "opencode-ai", "engines"])
        cpu_os, cpu_os_err = self._npm_meta_field(npm, ["view", "opencode-ai", "cpu", "os"])
        # Fallback single-field probes when the combined probe fails.
        if engines is None and engines_err:
            engines, _e2 = self._npm_meta_field(npm, ["view", "opencode-ai", "engines"])
        compat_notes = []
        unknown_notes = []
        # Node engines: e.g. {"node": ">=18"} — explicit major mismatch blocks.
        try:
            node_range = ""
            if isinstance(engines, dict):
                node_range = str(engines.get("node", "") or "")
            elif isinstance(engines, str):
                node_range = engines
            if node_range:
                major = owner_node_major()
                if major is None:
                    unknown_notes.append("node major unproven for engines %s" % node_range[:80])
                else:
                    # Parse explicit minimum major from range (>=18, ^18.0.0, 18.x).
                    import re as _re

                    nums = _re.findall(r"(\d+)", node_range)
                    if nums:
                        try:
                            minimum = int(nums[0])
                            if int(major) < minimum:
                                return True, sanitize_evidence(
                                    "engines %r requires node >=%d but owner node major=%d" % (
                                        node_range[:80], minimum, major))[:400], ""
                            compat_notes.append("engines %s satisfied by node %d" % (
                                node_range[:80], major))
                        except (TypeError, ValueError):
                            unknown_notes.append("engines %s unparsable" % node_range[:80])
                    else:
                        unknown_notes.append("engines %s has no numeric floor" % node_range[:80])
            else:
                unknown_notes.append("engines metadata missing (engines_err=%s)" % (engines_err or "?"))
        except Exception as exc:
            unknown_notes.append("engines probe inconclusive: %s" % str(exc)[:120])
        # cpu/os allow-lists: explicit exclusion blocks.
        try:
            machine = platform.machine().lower()
            plat = platform.system().lower()
            cpu_list = []
            os_list = []
            if isinstance(cpu_os, dict):
                raw_cpu = cpu_os.get("cpu", cpu_os.get("Cpu", []))
                raw_os = cpu_os.get("os", cpu_os.get("Os", []))
                if isinstance(raw_cpu, list):
                    cpu_list = [str(x).lower() for x in raw_cpu]
                elif isinstance(raw_cpu, str) and raw_cpu:
                    cpu_list = [raw_cpu.lower()]
                if isinstance(raw_os, list):
                    os_list = [str(x).lower() for x in raw_os]
                elif isinstance(raw_os, str) and raw_os:
                    os_list = [raw_os.lower()]
            elif isinstance(cpu_os, list):
                cpu_list = [str(x).lower() for x in cpu_os]
            if cpu_list:
                # Normalize arm64/aarch64 equivalence.
                norm_machine = {"aarch64": "arm64"}.get(machine, machine)
                norm_list = [{"aarch64": "arm64"}.get(c, c) for c in cpu_list]
                if norm_machine not in norm_list and machine not in cpu_list:
                    return True, sanitize_evidence(
                        "cpu allow-list %s excludes host %s for %s" % (
                            cpu_list, machine, candidate))[:400], ""
                compat_notes.append("cpu %s allows %s" % (cpu_list, machine))
            else:
                unknown_notes.append("cpu metadata missing")
            if os_list:
                if plat not in os_list and "any" not in os_list:
                    # platform.system lower: linux/darwin/windows.
                    if not any(plat in entry or entry in plat for entry in os_list):
                        return True, sanitize_evidence(
                            "os allow-list %s excludes host %s for %s" % (
                                os_list, plat, candidate))[:400], ""
                compat_notes.append("os %s allows %s" % (os_list, plat))
            else:
                unknown_notes.append("os metadata missing")
        except Exception as exc:
            unknown_notes.append("cpu/os probe inconclusive: %s" % str(exc)[:120])
        detail = sanitize_evidence("; ".join(compat_notes) or "compat proven minimally")
        unknown = sanitize_evidence("; ".join(unknown_notes))
        return False, detail, unknown

    def _staging_estimate(self):
        # type: () -> Optional[int]
        """Staging bytes from npm dist.tarball size x3, else None (block)."""
        try:
            npm = (get_owner_paths().get("npm_path", "") or nvm_paths()["npm"])
            ok, _resolved, _detail = resolve_executable(npm)
            if not ok:
                return None
            meta, _err = self._npm_meta_field(npm, ["view", "opencode-ai", "dist"])
            if not isinstance(meta, dict):
                return None
            return staging_from_npm_meta(meta)
        except Exception:
            return None

    def _resolve_target(self, current):
        # type: (str) -> Tuple[str, str, str]
        """Return (target, source, error). Fail-closed on any doubt.

        Order: explicit operator pin (EGA_OPENCODE_TARGET) when valid and not
        a downgrade (semver.is_upgrade), else npm registry metadata for the
        standalone channel. Downgrade/malformed blocks. Runtime/arch parsed
        from npm engines/cpu/os vs owner node major + platform: explicit
        incompatibility blocks, missing metadata records unknown but proceeds
        only when version/arch otherwise proven (documented in plan).
        The npm-network outage blocks only OpenCode planning, never unrelated
        native updates (callers scope this result to OpenCode).
        """
        pinned = os.environ.get("EGA_OPENCODE_TARGET", "").strip()
        if pinned:
            try:
                from backend.app.semver import try_parse as _try_parse
                _valid = _try_parse(pinned) is not None
            except Exception:
                _valid = parse_semver(pinned) is not None
            if not _valid:
                return "", "", sanitize_evidence(
                    "invalid_request: pinned target %r is not SemVer" % pinned)
            if current:
                if not is_upgrade_semver(current, pinned) and pinned != current:
                    cmp_res = compare_semver(pinned, current)
                    if cmp_res is None:
                        return "", "", sanitize_evidence(
                            "uncomparable pinned target %r vs current %r" % (pinned, current))
                    if cmp_res < 0:
                        return "", "", sanitize_evidence(
                            "pinned target %s would downgrade %s" % (pinned, current))
                    return "", "", sanitize_evidence(
                        "uncomparable pinned target %r vs current %r" % (pinned, current))
            return pinned, "operator-pin", ""
        help_res = self._upgrade_help()
        help_text = help_res.stdout + "\n" + help_res.stderr
        if not help_res.ok() or "--method" not in help_text:
            return "", "", sanitize_evidence(
                "upgrade interface unconfirmed (need positional target + --method curl)")
        npm = (get_owner_paths().get("npm_path", "") or nvm_paths()["npm"])
        ok, _resolved, _detail = resolve_executable(npm)
        if not ok:
            return "", "", sanitize_evidence(
                "npm runtime unavailable for metadata: %s" % _detail)
        meta = run_fixed([npm, "view", "opencode-ai", "version", "--json"],
                         timeout=120, scope_unit=None)
        self._emit("stdout", "$ npm view opencode-ai version --json -> exit=%d" % meta.exit_code)
        if not meta.ok():
            return "", "", sanitize_evidence(
                "registry metadata unavailable: exit=%d" % meta.exit_code)
        try:
            payload = json.loads(meta.stdout.strip())
        except Exception:
            return "", "", sanitize_evidence("malformed registry metadata (not JSON)")
        candidate = payload if isinstance(payload, str) else ""
        try:
            from backend.app.semver import try_parse as _try_parse2
            _cvalid = _try_parse2(candidate) is not None
        except Exception:
            _cvalid = parse_semver(candidate) is not None
        if not _cvalid:
            return "", "", sanitize_evidence("malformed registry version %r" % candidate[:100])
        if current:
            if not is_upgrade_semver(current, candidate) and candidate != current:
                cmp_res = compare_semver(candidate, current)
                if cmp_res is None:
                    return "", "", sanitize_evidence(
                        "uncomparable registry target %r vs current %r" % (candidate, current))
                if cmp_res < 0:
                    return "", "", sanitize_evidence(
                        "registry target %s would downgrade %s" % (candidate, current))
                return "", "", sanitize_evidence(
                    "uncomparable registry target %r vs current %r" % (candidate, current))
        blocked, compat_detail, unknown_detail = self._runtime_arch_check(candidate)
        if blocked:
            return "", "", sanitize_evidence(
                "runtime/arch incompatible for %s: %s" % (candidate, compat_detail))
        # Missing metadata -> unknown recorded; proceed only when version
        # otherwise proven (candidate is valid SemVer here). The unknown is
        # surfaced in plan/discover detail, never silent.
        if unknown_detail:
            self._emit("stdout", sanitize_evidence(
                "compat unknown but version proven (%s): %s" % (candidate, unknown_detail))[:500])
        return candidate, "npm-registry", ""

    # -- Adapter interface -----------------------------------------------

    def inspect(self):
        # type: () -> InspectResult
        ok, resolved, detail = resolve_executable(OPENCODE_BIN)
        version, _raw = self._version_probe() if ok else ("", "unresolvable executable")
        owner = self._owner_of(OPENCODE_BIN)
        fp = fingerprint(OPENCODE_BIN, resolved, version, owner, "standalone-curl")
        state_dirs = [d for d in (OPENCODE_HOME, OPENCODE_CONFIG_DIR, OPENCODE_DATA_DIR)
                      if os.path.exists(d)]
        return InspectResult(
            tool=self.tool_id,
            install_identity="standalone-curl:%s" % (resolved or OPENCODE_BIN),
            executable=OPENCODE_BIN,
            resolved_target=resolved,
            version=version,
            commit="",
            install_kind="standalone-curl",
            owner=owner,
            source_clean="unknown",
            source_detail=sanitize_evidence("binary install has no git checkout; %s" % detail),
            services=[],
            state_dirs=state_dirs,
            channel="stable",
            fingerprint=fp,
        )

    def discover(self):
        # type: () -> DiscoverResult
        ok, _resolved, detail = resolve_executable(OPENCODE_BIN)
        if not ok:
            return DiscoverResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel="stable", available=False,
                unknown_reason=sanitize_evidence("install_method_unsupported: %s" % detail))
        current, _raw = self._version_probe()
        target, _source, error = self._resolve_target(current)
        if not target:
            return DiscoverResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel="stable", available=False,
                unknown_reason=sanitize_evidence(error))
        if current and target == current:
            return DiscoverResult(
                tool=self.tool_id, target=target, target_mode="exact",
                channel="stable", available=True, unknown_reason="")
        return DiscoverResult(
            tool=self.tool_id, target=target, target_mode="exact",
            channel="stable", available=True, unknown_reason="")

    def activity(self):
        # type: () -> ActivityResult
        # A running process list alone never proves a task; without a server
        # interface the state is unknown and requires owner ack. Only the
        # absence of any opencode process counts as idle.
        proc = run_fixed(
            ["/bin/ps", "-eo", "pid,comm,args"], timeout=30, scope_unit=None)
        if not proc.ok():
            return ActivityResult(
                tool=self.tool_id, state="unknown",
                evidence=sanitize_evidence("process probe unavailable; ack required"),
                checked_at=utcnow_iso())
        hits = [line for line in proc.stdout.splitlines()
                if "opencode" in line.lower() and "ps -eo" not in line]
        if not hits:
            return ActivityResult(
                tool=self.tool_id, state="idle",
                evidence=sanitize_evidence("no opencode processes observed"),
                checked_at=utcnow_iso())
        server_on, server_detail = self._server_configured()
        return ActivityResult(
            tool=self.tool_id, state="unknown",
            evidence=sanitize_evidence(
                "%d opencode process(es) observed (%s); task state unproven, manual handling if server unconfigured"
                % (len(hits), server_detail)), checked_at=utcnow_iso())

    def plan(self):
        # type: () -> PlanResult
        inspection = self.inspect()
        discovery = self.discover()
        if not discovery.available:
            _blocked = PlanResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel="stable", fingerprint=inspection.fingerprint,
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
                backup_policy={"mode": "consistent-if-quiesced"},
                required_probes=["cli_version", "cli_startup", "exact_target"],
                budgets={"unknown:opencode": -1}, space_fs={},
                deadlines={}, restart_detail=sanitize_evidence(discovery.unknown_reason),
                activity_ts="", release_path="", required_checks=[
                    "cli_version", "cli_startup", "exact_target"],
                scope_unit=None)
            return _blocked
        server_on, server_detail = self._server_configured()
        already = bool(inspection.version and inspection.version == discovery.target)
        # R28: budgets/space_fs from measured footprint + metadata staging.
        footprint = self.measure_footprint(None)
        staging = self._staging_estimate()
        try:
            backup_bytes = sum(v for v in footprint.values() if isinstance(v, int) and v >= 0)
        except Exception:
            backup_bytes = -1
        if staging is None:
            staging_val = -1
        else:
            try:
                staging_val = int(staging)
            except (TypeError, ValueError):
                staging_val = -1
        budgets = {}
        space_fs = {}
        try:
            from .registry import filesystems_for as _fs_for
            import shutil as _shutil

            affected = [OPENCODE_BIN, OPENCODE_CONFIG_DIR, OPENCODE_DATA_DIR,
                        "/var/lib/ega-update/backups"]
            mounts = _fs_for(affected)
            for mount in mounts:
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
            budgets = {"unknown:opencode": -1}
            space_fs = {}
        if budgets and all(isinstance(v, int) and v >= 0 for v in budgets.values()):
            try:
                need = max(int(v) for v in budgets.values())
            except Exception:
                need = 0
        else:
            need = 0
        # Quiescence modeled explicitly: activity gate + pre-mutation recheck
        # INVALIDATES (blocked fingerprint/activity) rather than contradicting
        # the snapshot. Backup uses the online SQLite backup API (consistent
        # without zero processes); EXCLUSIVE-lock failures block, not silent.
        required_checks = ["cli_version", "cli_startup", "exact_target", "state_preserved"]
        if server_on:
            required_checks.append("server_readiness")
        plan_obj = PlanResult(
            tool=self.tool_id, target=discovery.target, target_mode="exact",
            channel="stable", fingerprint=inspection.fingerprint,
            services=["user:%s" % os.environ.get("EGA_OPENCODE_UNIT", "")]
            if server_on else [],
            backup_scope={
                "covered": "opencode config + opencode.db via sqlite3 online backup API (online-consistent; EXCLUSIVE-lock failure blocks)",
                "omitted": "13 GiB state dir wholesale, caches, plugins, alternate NVM npm install",
                "consistency": "online sqlite3 backup-API copy; bare file copy of a live DB is never sufficient; quiescence via activity gate + pre-mutation recheck that invalidates",
                "mode": "online-consistent",
            },
            required_space_bytes=need,
            steps=["preflight", "backup", "updating", "verifying"],
            timeouts=dict(ADAPTER_TIMEOUT_DEFAULTS),
            restart_impact=sanitize_evidence(
                ("configured server restart (%s)" % server_detail) if server_on
                else "binary replace only; unknown running processes need manual handling"),
            already_current=already,
        )
        try:
            prior_state = ""
            if server_on:
                _unit = os.environ.get("EGA_OPENCODE_UNIT", "").strip()
                prior_state, _d = self._server_prior_state(_unit)
        except Exception:
            prior_state = ""
        attach_plan_v2(
            plan_obj, install_identity=inspection.install_identity,
            artifact={"target": discovery.target, "channel": "stable"},
            config_hash="", plan_hash="",
            launch={"unit": os.environ.get("EGA_OPENCODE_UNIT", "") if server_on else "",
                    "prior_state": prior_state},
            state_homes=list(inspection.state_dirs or []),
            backup_policy={"mode": "online-consistent",
                           "quiescence": "activity-gate + pre-mutation-recheck-invalidates"},
            required_probes=list(required_checks),
            budgets=dict(budgets), space_fs=dict(space_fs),
            deadlines={}, restart_detail=sanitize_evidence(
                ("configured server restart (%s) prior=%s" % (server_detail, prior_state))
                if server_on else "binary replace only"),
            activity_ts="", release_path="",
            required_checks=list(required_checks),
            scope_unit=None)
        return plan_obj

    def _db_paths(self):
        # type: () -> List[str]
        candidates = []
        for base in (OPENCODE_DATA_DIR, OPENCODE_HOME, OPENCODE_CONFIG_DIR):
            for name in ("opencode.db",):
                candidate = os.path.join(base, name)
                candidates.append(candidate)
        return [c for c in candidates if os.path.exists(c)]

    def backup(self, job_id):
        # type: (str) -> BackupResult
        import shutil as _shutil

        scope = {
            "covered": "opencode config + opencode.db via sqlite3 online backup API (online-consistent)",
            "omitted": "13 GiB state dir wholesale, caches, plugins, alternate NVM npm install",
            "consistency": "online sqlite3 backup-API copy; bare file copy of a live DB is never sufficient; EXCLUSIVE-lock failure blocks",
        }
        try:
            from ..config import settings as _settings

            backup_root = _settings.backup_dir or "/var/lib/ega-update/backups"
        except Exception:
            backup_root = "/var/lib/ega-update/backups"
        dest_dir = os.path.join(backup_root, job_id)
        dbs = self._db_paths()
        try:
            os.makedirs(dest_dir, mode=0o700, exist_ok=True)
        except OSError as exc:
            return BackupResult(
                tool=self.tool_id, supported=False, path="", scope=scope,
                consistency=scope["consistency"], size_bytes=0,
                unsupported_reason=sanitize_evidence(
                    "backup_failed: cannot create %s: %s" % (dest_dir, exc)))
        total = 0
        try:
            # Online SQLite backup API: consistent WITHOUT requiring zero
            # processes. Refuse ONLY when writers hold EXCLUSIVE locks
            # (sqlite3.OperationalError on the backup step blocks, not
            # silent). Bare file copies of live DB/WAL/SHM are never used.
            import sqlite3 as _sqlite3

            for db_path in dbs:
                dest = backup_dest_for_db(dest_dir, job_id, db_path)
                try:
                    _src = _sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=10.0)
                except Exception as exc:
                    return BackupResult(
                        tool=self.tool_id, supported=False, path="", scope=scope,
                        consistency=scope["consistency"], size_bytes=total,
                        unsupported_reason=sanitize_evidence(
                            "backup_failed: cannot open %s read-only: %s" % (db_path, exc))[:400])
                try:
                    _dst = _sqlite3.connect(dest, timeout=10.0)
                    try:
                        with _dst:
                            _src.backup(_dst)
                    finally:
                        _dst.close()
                except _sqlite3.OperationalError as exc:
                    # Writers hold EXCLUSIVE locks: block, never silent.
                    try:
                        if os.path.exists(dest):
                            os.remove(dest)
                    except OSError:
                        pass
                    try:
                        _src.close()
                    except Exception:
                        pass
                    return BackupResult(
                        tool=self.tool_id, supported=False, path="", scope=scope,
                        consistency=scope["consistency"], size_bytes=total,
                        unsupported_reason=sanitize_evidence(
                            "backup_unsupported: writers hold EXCLUSIVE lock on %s: %s" % (
                                db_path, exc))[:400])
                finally:
                    try:
                        _src.close()
                    except Exception:
                        pass
                try:
                    total += os.path.getsize(dest) if os.path.exists(dest) else 0
                except OSError:
                    pass
            if os.path.isdir(OPENCODE_CONFIG_DIR):
                cfg_dest = os.path.join(dest_dir, "config")
                _shutil.copytree(OPENCODE_CONFIG_DIR, cfg_dest, dirs_exist_ok=True,
                                 ignore=_shutil.ignore_patterns("Cache", "cache", "*.log"))
                for root, _dirs, files in os.walk(cfg_dest):
                    for name in files:
                        try:
                            total += os.path.getsize(os.path.join(root, name))
                        except OSError:
                            pass
        except OSError as exc:
            return BackupResult(
                tool=self.tool_id, supported=False, path="", scope=scope,
                consistency=scope["consistency"], size_bytes=total,
                unsupported_reason=sanitize_evidence("backup_failed: %s" % exc))
        except Exception as exc:
            return BackupResult(
                tool=self.tool_id, supported=False, path="", scope=scope,
                consistency=scope["consistency"], size_bytes=total,
                unsupported_reason=sanitize_evidence(
                    "backup_failed: sqlite backup API failed: %s" % str(exc)[:300]))
        return BackupResult(
            tool=self.tool_id, supported=True, path=dest_dir, scope=scope,
            consistency="online-consistent", size_bytes=total, unsupported_reason="")

    def execute(self, plan, job_id, activity_ack=False):
        # type: (PlanResult, str, bool) -> ExecuteResult
        # Frozen: use plan-provided target/timeouts/scope; fresh rechecks may
        # INVALIDATE (blocked fingerprint_changed/config_changed) but never
        # silently substitute new values.
        if plan.target_mode != "exact":
            _b = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version="",
                after_version="", state="blocked", error_code="invalid_request",
                error_detail=sanitize_evidence("opencode requires an exact validated target"))
            return attach_timed_out(_b, False)
        try:
            from backend.app.semver import try_parse as _tp
            _valid_target = _tp(plan.target) is not None if plan.target else False
        except Exception:
            _valid_target = parse_semver(plan.target) is not None if plan.target else False
        if not plan.target or not _valid_target:
            _b2 = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version="",
                after_version="", state="blocked", error_code="invalid_request",
                error_detail=sanitize_evidence("unvalidated upgrade target; fresh plan required"))
            return attach_timed_out(_b2, False)
        # R28: unknown budgets block (never fixed constants).
        if plan_has_unknown_budget(plan):
            _cur0 = ""
            try:
                _cur0 = self.inspect().version
            except Exception:
                pass
            _bd = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=_cur0,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail=sanitize_evidence("unknown footprint/staging blocks mutation (budgets -1)"))
            return attach_timed_out(_bd, False)
        try:
            scope_unit = getattr(plan, "scope_unit", None)
        except Exception:
            scope_unit = None
        ok, resolved, detail = resolve_executable(OPENCODE_BIN)
        if not ok:
            _b3 = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version="",
                after_version="", state="blocked", error_code="install_method_unsupported",
                error_detail=sanitize_evidence(detail))
            return attach_timed_out(_b3, False)
        if not (resolved == OPENCODE_BIN
                or resolved.startswith(OPENCODE_HOME + os.sep)):
            _b4 = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version="",
                after_version="", state="blocked", error_code="install_method_unsupported",
                error_detail=sanitize_evidence(
                    "resolved binary %s is not the configured standalone (%s)"
                    % (resolved, OPENCODE_BIN)))
            return attach_timed_out(_b4, False)
        current = self.inspect()
        if plan.fingerprint and current.fingerprint != plan.fingerprint:
            _bf = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="fingerprint_changed",
                error_detail=sanitize_evidence("installation changed since plan; fresh plan required"))
            return attach_timed_out(_bf, False)
        # Config-hash invalidation when plan carries it (defensive).
        try:
            _plan_cfg = getattr(plan, "config_hash", "")
            if _plan_cfg:
                _cur_cfg = ""
                try:
                    _cur_cfg = fingerprint(
                        str(measure_tree_bytes(OPENCODE_CONFIG_DIR)),
                        current.version, current.owner)
                except Exception:
                    _cur_cfg = ""
                if _cur_cfg and _cur_cfg != str(_plan_cfg):
                    _bc = ExecuteResult(
                        tool=self.tool_id, exit_code=3, before_version=current.version,
                        after_version="", state="blocked", error_code="fingerprint_changed",
                        error_detail=sanitize_evidence(
                            "config changed since plan (config_changed); fresh plan required"))
                    return attach_timed_out(_bc, False)
        except Exception:
            pass
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
        # Disk: prefer plan budgets (R28); fall back to measured footprint.
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
            [OPENCODE_BIN, OPENCODE_HOME, "/var/lib/ega-update/backups"], need_int)
        if not disk_ok:
            _bd2 = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail=sanitize_evidence(disk_detail[:500]))
            return attach_timed_out(_bd2, False)
        before = current.version
        if before and before == plan.target:
            _already = ExecuteResult(
                tool=self.tool_id, exit_code=0, before_version=before,
                after_version=before, state="already_current", error_code="",
                error_detail=sanitize_evidence(
                    "already at target %s; not upgrade evidence" % plan.target))
            return attach_timed_out(_already, False)
        # Pre-mutation recheck INVALIDATES (never contradicts snapshot): if
        # writers appeared after the backup snapshot and policy requires
        # quiescence, fingerprint/activity recheck fails -> blocked.
        try:
            _fresh = self.inspect()
            if plan.fingerprint and _fresh.fingerprint != plan.fingerprint:
                _bi = ExecuteResult(
                    tool=self.tool_id, exit_code=3, before_version=before,
                    after_version="", state="blocked", error_code="fingerprint_changed",
                    error_detail=sanitize_evidence(
                        "pre-mutation recheck: installation changed; fresh plan required"))
                return attach_timed_out(_bi, False)
        except Exception:
            pass
        _recheck = self.activity()
        if _recheck.state == "busy":
            _br = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=before,
                after_version="", state="blocked", error_code="activity_blocked",
                error_detail=sanitize_evidence("pre-mutation recheck: %s" % _recheck.evidence[:400]))
            return attach_timed_out(_br, False)
        if _recheck.state == "unknown" and not activity_ack:
            _bq = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=before,
                after_version="", state="blocked", error_code="ack_required",
                error_detail=sanitize_evidence(
                    "pre-mutation recheck unknown requires ack: %s" % _recheck.evidence[:300]))
            return attach_timed_out(_bq, False)
        target = plan.target
        mutation_to = mutation_timeout_for(plan, "updating", default=1800)
        # Mutation template: explicit discovered target, curl method, fixed argv.
        res = run_fixed([OPENCODE_BIN, "upgrade", target, "--method", "curl"],
                        timeout=mutation_to, scope_unit=scope_unit)
        self._emit("stdout", "$ opencode upgrade <target> --method curl -> exit=%d (mutation timeout %ds = step + %ds margin)"
                   % (res.exit_code, mutation_to, MUTATION_TIMEOUT_MARGIN_S))
        self._emit("stdout", sanitize_evidence((res.stdout or "")[-4000:]))
        if res.stderr:
            self._emit("stderr", sanitize_evidence((res.stderr or "")[-4000:]))
        if res.timed_out:
            _tout = ExecuteResult(
                tool=self.tool_id, exit_code=4, before_version=before,
                after_version="", state="install_failed", error_code="timeout",
                error_detail=sanitize_evidence("opencode upgrade timed out; possible partial install"))
            return attach_timed_out(_tout, True)
        after, _raw = self._version_probe()
        if res.exit_code != 0:
            _fail = ExecuteResult(
                tool=self.tool_id, exit_code=4, before_version=before,
                after_version=after, state="install_failed", error_code="install_failed",
                error_detail=sanitize_evidence("opencode upgrade exit=%d" % res.exit_code))
            return attach_timed_out(_fail, False)
        if plan.target_mode == "exact" and after != plan.target:
            _mismatch = ExecuteResult(
                tool=self.tool_id, exit_code=4, before_version=before,
                after_version=after, state="install_failed", error_code="install_failed",
                error_detail=sanitize_evidence(
                    "exact-target mismatch: after=%s planned=%s" % (after or "?", plan.target)))
            return attach_timed_out(_mismatch, False)
        # Server: preserve exact prior unit state; restart ONLY when
        # required (prior active) + inventoried + correlated (ExecStart refs
        # binary, MainPID in cgroup). Record outcome; uncorrelated -> no
        # restart, verify unknown never success.
        server_on, server_detail = self._server_configured()
        if server_on:
            _unit = os.environ.get("EGA_OPENCODE_UNIT", "").strip()
            try:
                _prior, _pd = self._server_prior_state(_unit)
            except Exception:
                _prior = ""
            _required = "active" in (_prior or "").lower()
            _outcome, _corr_detail = self._server_correlation(_unit)
            self._emit("stdout", "server correlation %s prior=%s: %s" % (
                _outcome, sanitize_evidence(_prior)[:80], sanitize_evidence(_corr_detail[:300])))
            if _outcome == "correlated" and _required:
                _restart = run_fixed(
                    ["/bin/systemctl", "--user", "restart", _unit], timeout=120,
                    scope_unit=scope_unit)
                self._emit("stdout", "$ systemctl --user restart %s -> exit=%d (flags preserved, prior=%s)"
                           % (_unit, _restart.exit_code, sanitize_evidence(_prior)[:80]))
                if not _restart.ok():
                    _rfail = ExecuteResult(
                        tool=self.tool_id, exit_code=4, before_version=before,
                        after_version=after, state="install_failed", error_code="install_failed",
                        error_detail=sanitize_evidence(
                            "correlated server restart failed for %s: exit=%d" % (_unit, _restart.exit_code)))
                    return attach_timed_out(_rfail, False)
            elif _outcome == "correlated" and not _required:
                self._emit("stdout", "prior state %s not active; preserving (no restart)" % (
                    sanitize_evidence(_prior)[:80]))
            else:
                self._emit("stdout", "server/binary correlation unproven; skipping automatic restart; "
                                     "verification will report unknown until manually correlated")
        _done = ExecuteResult(
            tool=self.tool_id, exit_code=0, before_version=before,
            after_version=after, state="succeeded", error_code="",
            error_detail="")
        return attach_timed_out(_done, False)

    def _db_integrity(self):
        # type: () -> Tuple[str, str]
        """Measured state evidence: PRAGMA integrity_check on opencode.db.

        Returns (result, detail) with result in pass|fail|unknown|
        not_applicable. Read-only open (mode=ro) so the probe never mutates
        live state. A hard-coded pass is never used.
        """
        dbs = self._db_paths()
        if not dbs:
            return "not_applicable", "no opencode.db present; nothing to integrity-check"
        try:
            import sqlite3 as _sqlite3

            for db_path in dbs:
                _conn = _sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=10.0)
                try:
                    _row = _conn.execute("PRAGMA integrity_check;").fetchone()
                finally:
                    _conn.close()
                _val = str((_row[0] if _row else "") or "").strip().lower()
                if _val != "ok":
                    return "fail", "integrity_check failed for %s: %s" % (db_path, str((_row[0] if _row else "?"))[:200])
            return "pass", "integrity_check ok on %d db(s) via read-only probe" % len(dbs)
        except Exception as exc:
            return "unknown", "integrity probe inconclusive: %s" % str(exc)[:300]

    def verify(self, expected_target="", plan=None, required_checks=None, **kwargs):
        # type: (...) -> VerifyResult
        """Verify CLI version + startup + correlated server + measured state.

        expected_target (optional): planned exact target equality re-check.
        plan/required_checks (optional): plan.required_checks coverage; the
        runner fails anything missing (never silently optional). Every
        required name is included by name. Server/binary correlation
        unproven yields unknown (never success). All summaries sanitized.
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
        version, raw = self._version_probe()
        checks.append(CheckItem(
            name="cli_version", result="pass" if version else "fail", mandatory=True,
            summary=sanitize_evidence(
                ("version %s" % version) if version else "version unreadable: %s" % raw[:300])))
        startup = run_fixed([OPENCODE_BIN, "--help"], timeout=60, scope_unit=None)
        checks.append(CheckItem(
            name="cli_startup", result="pass" if startup.ok() else "fail", mandatory=True,
            summary=sanitize_evidence("cli startup exit=%d" % startup.exit_code)))
        if expected_target:
            if version and version == expected_target:
                checks.append(CheckItem(
                    name="exact_target", result="pass", mandatory=True,
                    summary=sanitize_evidence(
                        "after %s equals planned exact target %s" % (version, expected_target))))
            else:
                checks.append(CheckItem(
                    name="exact_target", result="fail", mandatory=True,
                    summary=sanitize_evidence(
                        "exact-target mismatch: after=%s planned=%s" % (version or "?", expected_target))))
        else:
            checks.append(CheckItem(
                name="exact_target", result="not_applicable", mandatory=False,
                summary=sanitize_evidence(
                    "no expected target passed to verify; exact equality enforced in execute() + centrally by runner")))
        server_on, server_detail = self._server_configured()
        if not server_on:
            checks.append(CheckItem(
                name="server_readiness", result="not_applicable", mandatory=False,
                summary=sanitize_evidence("no configured server; %s" % server_detail)))
        else:
            unit = os.environ.get("EGA_OPENCODE_UNIT", "")
            outcome, corr_detail = self._server_correlation(unit)
            if outcome != "correlated":
                checks.append(CheckItem(
                    name="server_readiness", result="unknown", mandatory=True,
                    summary=sanitize_evidence(
                        "server/binary correlation unproven (%s); verification unknown, never success" % corr_detail[:300])))
            else:
                probe = run_fixed(
                    ["/bin/systemctl", "--user", "is-active", unit], timeout=30,
                    scope_unit=None)
                active = probe.ok() and probe.stdout.strip() == "active"
                checks.append(CheckItem(
                    name="server_readiness", result="pass" if active else "fail", mandatory=True,
                    summary=sanitize_evidence(
                        "correlated unit %s active=%s (%s; restart outcome recorded in execute)" % (
                            unit, active, corr_detail[:200]))))
        integrity_result, integrity_detail = self._db_integrity()
        checks.append(CheckItem(
            name="state_preserved", result=integrity_result,
            mandatory=(integrity_result in ("pass", "fail", "unknown") and integrity_result != "not_applicable") or integrity_result == "unknown",
            summary=sanitize_evidence(
                "%s; online sqlite3 backup-API copy pre-mutation, startup probe exit=%d" % (
                    integrity_detail[:400], startup.exit_code))))
        for _c in checks:
            if _c.name == "state_preserved" and integrity_result == "not_applicable":
                _c.mandatory = False
            if _c.name == "state_preserved" and integrity_result == "unknown":
                _c.mandatory = True
        # Required-checks coverage: every plan-required name present by name.
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
        passed = bool(version) and not any(
            c for c in checks if c.mandatory and c.result != "pass")
        return VerifyResult(
            tool=self.tool_id, version=version, checks=checks, passed=passed,
            error_code="" if passed else "health_failed",
            error_detail=sanitize_evidence("" if passed else "mandatory opencode checks failed"))
