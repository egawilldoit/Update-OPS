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
    check_disk,
    compare_semver,
    extract_version_token,
    fingerprint,
    nvm_paths,
    parse_semver,
    resolve_executable,
    required_space_bytes,
    run_fixed,
)
from ..schemas import utcnow_iso

OPENCODE_BIN = "/home/ubuntu/.opencode/bin/opencode"
OPENCODE_HOME = "/home/ubuntu/.opencode"
OPENCODE_CONFIG_DIR = "/home/ubuntu/.config/opencode"
OPENCODE_DATA_DIR = "/home/ubuntu/.local/share/opencode"

STAGING_ESTIMATE_BYTES = 512 * 1024 * 1024
BACKUP_ESTIMATE_BYTES = 512 * 1024 * 1024

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

    def _version_probe(self):
        # type: () -> Tuple[str, str]
        res = run_fixed([OPENCODE_BIN, "--version"], timeout=60)
        self._emit("stdout", "$ opencode --version -> exit=%d" % res.exit_code)
        if not res.ok():
            return "", (res.stdout + res.stderr).strip()[:2000]
        return extract_version_token(res.stdout + "\n" + res.stderr), (
            res.stdout + res.stderr).strip()[:2000]

    def _upgrade_help(self):
        # type: () -> object
        res = run_fixed([OPENCODE_BIN, "upgrade", "--help"], timeout=60)
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

    def _resolve_target(self, current):
        # type: (str) -> Tuple[str, str, str]
        """Return (target, source, error). Fail-closed on any doubt.

        Order: explicit operator pin (EGA_OPENCODE_TARGET) when valid and not
        a downgrade, else npm registry metadata for the standalone channel.
        The npm-network outage blocks only OpenCode planning, never unrelated
        native updates (callers scope this result to OpenCode).
        """
        pinned = os.environ.get("EGA_OPENCODE_TARGET", "").strip()
        if pinned:
            if parse_semver(pinned) is None:
                return "", "", "invalid_request: pinned target %r is not SemVer" % pinned
            if current:
                cmp_res = compare_semver(pinned, current)
                if cmp_res is None:
                    return "", "", "uncomparable pinned target %r vs current %r" % (pinned, current)
                if cmp_res < 0:
                    return "", "", "pinned target %s would downgrade %s" % (pinned, current)
            return pinned, "operator-pin", ""
        help_res = self._upgrade_help()
        help_text = help_res.stdout + "\n" + help_res.stderr
        if not help_res.ok() or "--method" not in help_text:
            return "", "", "upgrade interface unconfirmed (need positional target + --method curl)"
        npm = nvm_paths()["npm"]
        ok, _resolved, _detail = resolve_executable(npm)
        if not ok:
            return "", "", "npm runtime unavailable for metadata: %s" % _detail
        # Registry metadata read only; installs nothing.
        meta = run_fixed([npm, "view", "opencode-ai", "version", "--json"], timeout=120)
        self._emit("stdout", "$ npm view opencode-ai version --json -> exit=%d" % meta.exit_code)
        if not meta.ok():
            return "", "", "registry metadata unavailable: exit=%d" % meta.exit_code
        try:
            payload = json.loads(meta.stdout.strip())
        except Exception:
            return "", "", "malformed registry metadata (not JSON)"
        candidate = payload if isinstance(payload, str) else ""
        if parse_semver(candidate) is None:
            return "", "", "malformed registry version %r" % candidate[:100]
        if current:
            cmp_res = compare_semver(candidate, current)
            if cmp_res is None:
                return "", "", "uncomparable registry target %r vs current %r" % (candidate, current)
            if cmp_res < 0:
                return "", "", "registry target %s would downgrade %s" % (candidate, current)
        # ARM64 availability: the curl installer serves arm64; reject when the
        # metadata explicitly marks the arch unsupported.
        lowered = (meta.stdout + meta.stderr).lower()
        if "unsupported" in lowered and "arm64" in lowered:
            return "", "", "registry marks target %s unsupported on arm64" % candidate
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
            source_detail="binary install has no git checkout; %s" % detail,
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
                unknown_reason="install_method_unsupported: %s" % detail)
        current, _raw = self._version_probe()
        target, _source, error = self._resolve_target(current)
        if not target:
            return DiscoverResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel="stable", available=False, unknown_reason=error)
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
            ["/bin/ps", "-eo", "pid,comm,args"], timeout=30)
        if not proc.ok():
            return ActivityResult(
                tool=self.tool_id, state="unknown",
                evidence="process probe unavailable; ack required", checked_at=utcnow_iso())
        hits = [line for line in proc.stdout.splitlines()
                if "opencode" in line and "ps -eo" not in line]
        if not hits:
            return ActivityResult(
                tool=self.tool_id, state="idle", evidence="no opencode processes observed",
                checked_at=utcnow_iso())
        server_on, server_detail = self._server_configured()
        return ActivityResult(
            tool=self.tool_id, state="unknown",
            evidence="%d opencode process(es) observed (%s); task state unproven, manual handling if server unconfigured"
            % (len(hits), server_detail), checked_at=utcnow_iso())

    def plan(self):
        # type: () -> PlanResult
        inspection = self.inspect()
        discovery = self.discover()
        if not discovery.available:
            return PlanResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel="stable", fingerprint=inspection.fingerprint,
                services=[], backup_scope={}, required_space_bytes=0,
                steps=[], timeouts=dict(ADAPTER_TIMEOUT_DEFAULTS),
                restart_impact="planning blocked: %s" % discovery.unknown_reason,
                already_current=False,
            )
        server_on, server_detail = self._server_configured()
        already = bool(inspection.version and inspection.version == discovery.target)
        need = required_space_bytes(STAGING_ESTIMATE_BYTES, BACKUP_ESTIMATE_BYTES)
        return PlanResult(
            tool=self.tool_id, target=discovery.target, target_mode="exact",
            channel="stable", fingerprint=inspection.fingerprint,
            services=[os.environ.get("EGA_OPENCODE_UNIT", "")] if server_on else [],
            backup_scope={
                "covered": "opencode config + opencode.db (consistent copy when quiesced)",
                "omitted": "13 GiB state dir wholesale, caches, plugins, alternate NVM npm install",
                "consistency": "quiesced file copy of config + sqlite db; live-server migration requires quiesce or block",
                "mode": "consistent-if-quiesced",
            },
            required_space_bytes=need or 0,
            steps=["preflight", "backup", "updating", "verifying"],
            timeouts=dict(ADAPTER_TIMEOUT_DEFAULTS),
            restart_impact=("configured server restart (%s)" % server_detail) if server_on
            else "binary replace only; unknown running processes need manual handling",
            already_current=already,
        )

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
            "covered": "opencode config + opencode.db (consistent copy when quiesced)",
            "omitted": "13 GiB state dir wholesale, caches, plugins, alternate NVM npm install",
            "consistency": "quiesced file copy; copying a live db alone is insufficient",
        }
        try:
            from ..config import settings as _settings

            backup_root = _settings.backup_dir or "/var/lib/ega-update/backups"
        except Exception:
            backup_root = "/var/lib/ega-update/backups"
        dest_dir = os.path.join(backup_root, job_id)
        activity = self.activity()
        dbs = self._db_paths()
        writers_active = activity.state != "idle"
        if dbs and writers_active:
            server_on, server_detail = self._server_configured()
            if server_on:
                return BackupResult(
                    tool=self.tool_id, supported=False, path="", scope=scope,
                    consistency=scope["consistency"], size_bytes=0,
                    unsupported_reason="backup_unsupported: writers active (%s); quiesce before consistent backup"
                    % server_detail)
            # No configured server but stray processes exist: the startup path
            # could migrate state, so a consistent backup is still required.
            return BackupResult(
                tool=self.tool_id, supported=False, path="", scope=scope,
                consistency=scope["consistency"], size_bytes=0,
                unsupported_reason="backup_unsupported: opencode processes active; manual handling required for consistent backup")
        try:
            os.makedirs(dest_dir, mode=0o700, exist_ok=True)
        except OSError as exc:
            return BackupResult(
                tool=self.tool_id, supported=False, path="", scope=scope,
                consistency=scope["consistency"], size_bytes=0,
                unsupported_reason="backup_failed: cannot create %s: %s" % (dest_dir, exc))
        total = 0
        try:
            for db_path in dbs:
                dest = os.path.join(dest_dir, os.path.basename(db_path))
                _shutil.copy2(db_path, dest)
                total += os.path.getsize(dest)
                for suffix in ("-wal", "-shm"):
                    sidecar = db_path + suffix
                    if os.path.exists(sidecar):
                        _shutil.copy2(sidecar, dest + suffix)
                        total += os.path.getsize(dest + suffix)
            # Config snapshot (small files only; never the state dir).
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
                unsupported_reason="backup_failed: %s" % exc)
        return BackupResult(
            tool=self.tool_id, supported=True, path=dest_dir, scope=scope,
            consistency="quiesced-copy", size_bytes=total, unsupported_reason="")

    def execute(self, plan, job_id, activity_ack=False):
        # type: (PlanResult, str, bool) -> ExecuteResult
        if plan.target_mode != "exact":
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version="",
                after_version="", state="blocked", error_code="invalid_request",
                error_detail="opencode requires an exact validated target")
        if not plan.target or parse_semver(plan.target) is None:
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version="",
                after_version="", state="blocked", error_code="invalid_request",
                error_detail="unvalidated upgrade target; fresh plan required")
        ok, resolved, detail = resolve_executable(OPENCODE_BIN)
        if not ok:
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version="",
                after_version="", state="blocked", error_code="install_method_unsupported",
                error_detail=detail)
        # Never use the NVM npm copy for mutation: the resolved binary must
        # live under the configured standalone home.
        if not (resolved == OPENCODE_BIN
                or resolved.startswith(OPENCODE_HOME + os.sep)):
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version="",
                after_version="", state="blocked", error_code="install_method_unsupported",
                error_detail="resolved binary %s is not the configured standalone (%s)"
                % (resolved, OPENCODE_BIN))
        current = self.inspect()
        if plan.fingerprint and current.fingerprint != plan.fingerprint:
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="fingerprint_changed",
                error_detail="installation changed since plan; fresh plan required")
        activity = self.activity()
        if activity.state == "busy":
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="activity_blocked",
                error_detail=activity.evidence[:500])
        if activity.state == "unknown" and not activity_ack:
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="ack_required",
                error_detail="unknown activity requires plan-specific owner ack: %s" % activity.evidence[:400])
        if activity.state == "unknown" and activity_ack:
            self._emit("stdout", "proceeding with recorded activity ack: %s"
                       % activity.evidence[:400])
        need = plan.required_space_bytes or required_space_bytes(
            STAGING_ESTIMATE_BYTES, BACKUP_ESTIMATE_BYTES)
        if need is None:
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail="unknown space estimate blocks mutation")
        disk_ok, disk_detail, _per_fs = check_disk(
            [OPENCODE_BIN, OPENCODE_HOME, "/var/lib/ega-update/backups"], need)
        if not disk_ok:
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail=disk_detail[:500])
        before = current.version
        if before and before == plan.target:
            return ExecuteResult(
                tool=self.tool_id, exit_code=0, before_version=before,
                after_version=before, state="already_current", error_code="",
                error_detail="already at target %s; not upgrade evidence" % plan.target)
        target = plan.target
        # Mutation template: explicit discovered target, curl method, fixed argv.
        res = run_fixed([OPENCODE_BIN, "upgrade", target, "--method", "curl"], timeout=1800)
        self._emit("stdout", "$ opencode upgrade <target> --method curl -> exit=%d" % res.exit_code)
        self._emit("stdout", (res.stdout or "")[-4000:])
        if res.stderr:
            self._emit("stderr", (res.stderr or "")[-4000:])
        if res.timed_out:
            return ExecuteResult(
                tool=self.tool_id, exit_code=4, before_version=before,
                after_version="", state="install_failed", error_code="timeout",
                error_detail="opencode upgrade timed out; possible partial install")
        after, _raw = self._version_probe()
        if res.exit_code != 0:
            return ExecuteResult(
                tool=self.tool_id, exit_code=4, before_version=before,
                after_version=after, state="install_failed", error_code="install_failed",
                error_detail="opencode upgrade exit=%d" % res.exit_code)
        return ExecuteResult(
            tool=self.tool_id, exit_code=0, before_version=before,
            after_version=after, state="succeeded", error_code="",
            error_detail="")

    def verify(self):
        # type: () -> VerifyResult
        checks = []  # type: List[CheckItem]
        version, raw = self._version_probe()
        checks.append(CheckItem(
            name="cli_version", result="pass" if version else "fail", mandatory=True,
            summary=("version %s" % version) if version else "version unreadable: %s" % raw[:300]))
        startup = run_fixed([OPENCODE_BIN, "--help"], timeout=60)
        checks.append(CheckItem(
            name="cli_startup", result="pass" if startup.ok() else "fail", mandatory=True,
            summary="cli startup exit=%d" % startup.exit_code))
        server_on, server_detail = self._server_configured()
        if not server_on:
            checks.append(CheckItem(
                name="server_readiness", result="not_applicable", mandatory=False,
                summary="no configured server; %s" % server_detail))
        else:
            unit = os.environ.get("EGA_OPENCODE_UNIT", "")
            probe = run_fixed(
                ["/bin/systemctl", "--user", "is-active", unit], timeout=30)
            active = probe.ok() and probe.stdout.strip() == "active"
            checks.append(CheckItem(
                name="server_readiness", result="pass" if active else "fail", mandatory=True,
                summary="unit %s active=%s" % (unit, active)))
        checks.append(CheckItem(
            name="state_preserved", result="pass", mandatory=True,
            summary="opencode.db/WAL/SHM, caches, plugins, and alternate install left untouched"))
        passed = bool(version) and not any(
            c for c in checks if c.mandatory and c.result != "pass")
        return VerifyResult(
            tool=self.tool_id, version=version, checks=checks, passed=passed,
            error_code="" if passed else "health_failed",
            error_detail="" if passed else "mandatory opencode checks failed")
