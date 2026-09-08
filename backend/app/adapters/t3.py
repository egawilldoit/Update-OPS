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
    attach_timed_out,
    check_disk,
    compare_semver,
    extract_version_token,
    fingerprint,
    mutation_timeout_for,
    nvm_paths,
    parse_semver,
    resolve_executable,
    required_space_bytes,
    run_fixed,
)
from ..schemas import utcnow_iso

T3_UNIT = "t3code.service"
T3_STATE_HINT = os.path.expanduser("~/.t3/runtime/service-state.json")
T3_PACKAGE = "t3@nightly"

STAGING_ESTIMATE_BYTES = 512 * 1024 * 1024
BACKUP_ESTIMATE_BYTES = 256 * 1024 * 1024

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


class T3Adapter(Adapter):
    tool_id = "t3"
    enabled = True

    def __init__(self):
        # type: () -> None
        self._emit = _emit_noop

    # -- inventory -------------------------------------------------------

    def _inventory(self):
        # type: () -> Dict[str, object]
        """Correlate unit + state path + executable + process + endpoint.

        All five pillars are recorded on every call; _inventory_gate()
        requires each one. A state file alone is never a service.
        """
        unit = _configured("EGA_T3_UNIT", T3_UNIT)
        state_path = _configured("EGA_T3_STATE_PATH", T3_STATE_HINT)
        endpoint = _configured("EGA_T3_ENDPOINT", "")
        port = _configured("EGA_T3_PORT", "")
        launch_mode = _configured("EGA_T3_LAUNCH_MODE", "")  # managed-service|custom|foreground|""
        info = {
            "unit": unit, "state_path": state_path, "endpoint": endpoint,
            "port": port, "launch_mode": launch_mode,
            "unit_scope": "user",
            "unit_load": "", "unit_active": "", "unit_sub": "", "unit_pid": "",
            "state_exists": False, "state_version": "", "state_scrubbed": "",
            "exec_path": "", "exec_resolved": "", "installed_version": "",
            "process_hit": False, "process_checked": False, "endpoint_hint": "",
        }  # type: Dict[str, object]
        show = run_fixed(
            ["/bin/systemctl", "--user", "show", unit, "-p", "LoadState",
             "-p", "ActiveState", "-p", "SubState", "-p", "MainPID",
             "-p", "ExecStart"],
            timeout=30)
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
                elif line.startswith("ExecStart="):
                    info["endpoint_hint"] = _scrub(line.partition("=")[2].strip())
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
                    # Deep-scrub nested dicts/lists before any evidence use.
                    scrubbed = _deep_scrub(payload)
                    try:
                        info["state_scrubbed"] = json.dumps(scrubbed, sort_keys=True)[:1000]
                    except Exception:
                        info["state_scrubbed"] = _scrub(str(scrubbed)[:1000])
                else:
                    info["state_scrubbed"] = _scrub(str(payload)[:1000])
            except Exception as exc:
                info["state_scrubbed"] = "unreadable state file: %s" % str(exc)[:300]
        # Installed executable provenance: configured npx; PATH winners are
        # never used for mutation.
        npx = nvm_paths()["npx"]
        ok, resolved, _detail = resolve_executable(npx)
        if ok:
            info["exec_path"] = npx
            info["exec_resolved"] = resolved
        proc = run_fixed(["/bin/ps", "-eo", "pid,comm,args"], timeout=30)
        if proc.ok():
            info["process_checked"] = True
            for line in proc.stdout.splitlines():
                lowered = line.lower()
                if "t3code" in lowered or " t3 " in lowered:
                    if "ps -eo" not in line:
                        info["process_hit"] = True
                        break
        if not info["launch_mode"]:
            # Conservatively infer managed-service only for the official unit
            # when it is provably loaded; anything else stays unproven and the
            # gate blocks. Inference lives here (not in the gate) so every
            # caller, including install_kind reporting, sees the same value.
            if info["unit"] == T3_UNIT and info["unit_load"] == "loaded":
                info["launch_mode"] = "managed-service"
        return info

    def _inventory_gate(self, info):
        # type: (Dict[str, object]) -> Tuple[bool, str]
        """Fail-closed five-pillar gate: every pillar proven before planning.

        Pillars (each recorded on info): (1) unit LoadState=loaded,
        (2) state path exists, (3) executable provenance (configured npx
        resolved), (4) matching process evidence (process probe ran; when the
        unit is active a matching process must be observed), (5) endpoint
        (port or endpoint configured). Launch mode must additionally be
        managed-service. Any missing pillar yields unknown/blocked, never
        success.
        """
        missing = []
        # Pillar 1: unit.
        if info.get("unit_load") != "loaded":
            missing.append("pillar unit: service unit %s LoadState=%s" % (
                info.get("unit"), info.get("unit_load") or "unproven"))
        # Pillar 2: state path.
        if not info.get("state_path") or not info.get("state_exists"):
            missing.append("pillar state-path: unproven")
        # Pillar 3: executable provenance.
        if not info.get("exec_path") or not info.get("exec_resolved"):
            missing.append("pillar executable-provenance: configured npx unresolved")
        # Pillar 4: matching process.
        if not info.get("process_checked"):
            missing.append("pillar matching-process: process probe inconclusive")
        elif str(info.get("unit_active") or "") == "active" and not info.get("process_hit"):
            missing.append("pillar matching-process: unit active but no matching t3 process observed")
        # Pillar 5: endpoint.
        if not info.get("port") and not info.get("endpoint"):
            missing.append("pillar endpoint: port/endpoint unproven")
        if not info.get("launch_mode"):
            # Infer conservatively: a loaded user unit with ExecStart implies
            # managed-service only when the unit is the official one.
            if info.get("unit") == T3_UNIT and info.get("unit_load") == "loaded":
                info["launch_mode"] = "managed-service"
            else:
                missing.append("launch flags/mode unproven")
        if missing:
            return False, "T3 inventory gate: %s; plan blocked until inventoried" % "; ".join(missing)
        if str(info.get("launch_mode", "")) in ("custom", "foreground"):
            return False, ("T3 inventory gate: custom/foreground server (%s); generic service update blocked,"
                           " launch recipe recorded separately" % info.get("launch_mode"))
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
        """Resolve nightly metadata via configured npm only. Fail-closed."""
        npm = nvm_paths()["npm"]
        ok, _resolved, detail = resolve_executable(npm)
        if not ok:
            return "", "npm runtime unavailable: %s" % detail
        res = run_fixed([npm, "view", T3_PACKAGE, "version", "--json"], timeout=120)
        self._emit("stdout", "$ npm view %s version --json -> exit=%d" % (T3_PACKAGE, res.exit_code))
        if not res.ok():
            return "", "nightly metadata unavailable: exit=%d" % res.exit_code
        try:
            payload = json.loads(res.stdout.strip())
        except Exception:
            return "", "malformed nightly metadata (not JSON)"
        candidate = payload if isinstance(payload, str) else ""
        if parse_semver(candidate) is None:
            return "", "malformed nightly version %r" % candidate[:100]
        machine = platform.machine().lower()
        if machine not in ("aarch64", "arm64", "x86_64", "amd64"):
            return "", "unsupported arch %s for nightly %s" % (machine, candidate)
        return candidate, ""

    # -- Adapter interface -----------------------------------------------

    def inspect(self):
        # type: () -> InspectResult
        info = self._inventory()
        gate_ok, gate_detail = self._inventory_gate(dict(info))
        # Running version requires process/endpoint corroboration; a state
        # file alone never yields a version here.
        version = self._running_version(info)
        if not version and info.get("state_version"):
            gate_detail = (gate_detail or "") + "; state-file version %s uncorroborated by process/endpoint pillars" % str(info.get("state_version"))[:50]
        fp = fingerprint(
            str(info.get("unit", "")), str(info.get("state_path", "")),
            str(info.get("exec_resolved", "")), version,
            str(info.get("unit_active", "")), str(info.get("launch_mode", "")))
        services = ["user:%s" % info["unit"]] if info.get("unit_load") == "loaded" else []
        detail = gate_detail if not gate_ok else "inventory correlated: unit=%s active=%s/%s state=%s exec=%s" % (
            info.get("unit"), info.get("unit_active"), info.get("unit_sub"),
            info.get("state_scrubbed", "")[:200], info.get("exec_resolved") or "?")
        return InspectResult(
            tool=self.tool_id,
            install_identity="t3-nightly:%s" % (version or "unknown"),
            executable=str(info.get("exec_path") or nvm_paths()["npx"]),
            resolved_target=str(info.get("exec_resolved", "")),
            version=version,
            commit="",
            install_kind=str(info.get("launch_mode") or "unknown"),
            owner="ubuntu",
            source_clean="unknown",
            source_detail=_scrub(detail),
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
                channel="nightly", available=False, unknown_reason=gate_detail)
        candidate, error = self._nightly_metadata()
        if not candidate:
            return DiscoverResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel="nightly", available=False, unknown_reason=error)
        current = self._running_version(info)
        # When the running version is uncorroborated (no process/endpoint
        # evidence), fall back to no current for downgrade comparison but
        # never treat the state file alone as a running proof downstream.
        if not current:
            current = ""
        if current:
            cmp_res = compare_semver(candidate, current)
            if cmp_res is None:
                return DiscoverResult(
                    tool=self.tool_id, target="", target_mode="unknown",
                    channel="nightly", available=False,
                    unknown_reason="uncomparable nightly %r vs installed %r" % (candidate, current))
            if cmp_res < 0:
                return DiscoverResult(
                    tool=self.tool_id, target="", target_mode="unknown",
                    channel="nightly", available=False,
                    unknown_reason="nightly %s would downgrade installed %s" % (candidate, current))
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
                evidence=_scrub(gate_detail)[:600], checked_at=utcnow_iso())
        # Service running never equals idle session; without a session API the
        # state is unknown unless the unit is provably inactive.
        if info.get("unit_active") != "active":
            return ActivityResult(
                tool=self.tool_id, state="idle",
                evidence="unit %s inactive (%s)" % (info.get("unit"), info.get("unit_active") or "?"),
                checked_at=utcnow_iso())
        return ActivityResult(
            tool=self.tool_id, state="unknown",
            evidence="unit active but session state unproven; ack required",
            checked_at=utcnow_iso())

    def plan(self):
        # type: () -> PlanResult
        inspection = self.inspect()
        discovery = self.discover()
        if not discovery.available:
            return PlanResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel="nightly", fingerprint=inspection.fingerprint,
                services=[], backup_scope={}, required_space_bytes=0,
                steps=[], timeouts=dict(ADAPTER_TIMEOUT_DEFAULTS),
                restart_impact="planning blocked: %s" % discovery.unknown_reason,
                already_current=False,
            )
        info = self._inventory()
        running = self._running_version(info)
        already = bool(running and running == discovery.target)
        need = required_space_bytes(STAGING_ESTIMATE_BYTES, BACKUP_ESTIMATE_BYTES)
        return PlanResult(
            tool=self.tool_id, target=discovery.target, target_mode="exact",
            channel="nightly", fingerprint=inspection.fingerprint,
            services=["user:%s" % info["unit"]],
            backup_scope={
                "covered": "service state snapshot (%s) + unit definition (deep-scrubbed; recovery-limited)" % info.get("state_path"),
                "omitted": "runtime caches, downloaded toolchains",
                "consistency": "quiesced-copy ONLY when writers quiesced (unit stopped or manager-confirmed idle); else consistent-backup-unavailable",
                "mode": "consistent-if-quiesced",
            },
            required_space_bytes=need or 0,
            steps=["preflight", "backup", "updating", "verifying"],
            timeouts=dict(ADAPTER_TIMEOUT_DEFAULTS),
            restart_impact="managed user service %s restarts pinned to %s" % (
                info.get("unit"), discovery.target),
            already_current=already,
        )

    def backup(self, job_id):
        # type: (str) -> BackupResult
        info = self._inventory()
        scope = {
            "covered": "service state snapshot + unit definition (deep-scrubbed; recovery-limited: secrets redacted, re-provision on restore)",
            "omitted": "runtime caches, downloaded toolchains",
            "consistency": "quiesced-copy ONLY when writers quiesced; else consistent-backup-unavailable",
        }
        gate_ok, gate_detail = self._inventory_gate(dict(info))
        if not gate_ok:
            return BackupResult(
                tool=self.tool_id, supported=False, path="", scope=scope,
                consistency=scope["consistency"], size_bytes=0,
                unsupported_reason="backup_unsupported: %s" % _scrub(gate_detail)[:400])
        # Quiescence gate: quiesced-copy ONLY when writers were actually
        # quiesced (unit stopped/inactive or manager-confirmed idle via
        # activity()). Otherwise consistent-backup-unavailable -> blocked
        # when a consistent backup is required.
        try:
            _activity = self.activity()
            _writers_quiesced = (_activity.state == "idle" and str(info.get("unit_active") or "") != "active")
        except Exception:
            _writers_quiesced = False
        if not _writers_quiesced:
            return BackupResult(
                tool=self.tool_id, supported=False, path="", scope=scope,
                consistency="consistent-backup-unavailable", size_bytes=0,
                unsupported_reason="backup_unsupported: consistent-backup-unavailable: writers not quiesced "
                                   "(unit %s active=%s activity=%s); stop unit or confirm idle before consistent backup"
                % (info.get("unit"), info.get("unit_active") or "?",
                   getattr(_activity, "state", "?") if "_activity" in locals() else "?"))
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
            show = run_fixed(
                ["/bin/systemctl", "--user", "show", str(info.get("unit")), "-p", "ExecStart",
                 "-p", "Environment"], timeout=30)
            with open(os.path.join(dest_dir, "unit.txt"), "w", encoding="utf-8") as fh:
                fh.write(_scrub(show.stdout or ""))
            total += os.path.getsize(os.path.join(dest_dir, "unit.txt"))
        except OSError as exc:
            return BackupResult(
                tool=self.tool_id, supported=False, path="", scope=scope,
                consistency=scope["consistency"], size_bytes=0,
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
                error_detail="t3 requires an exact validated nightly target")
        if not plan.target or parse_semver(plan.target) is None:
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version="",
                after_version="", state="blocked", error_code="invalid_request",
                error_detail="unvalidated t3 target; fresh plan required")
        info = self._inventory()
        gate_ok, gate_detail = self._inventory_gate(dict(info))
        if not gate_ok:
            state_mode = str(info.get("launch_mode") or "")
            if state_mode in ("custom", "foreground"):
                recipe = "launch recipe: mode=%s unit=%s state=%s endpoint=%s" % (
                    state_mode, info.get("unit"), info.get("state_path"), "[redacted]")
                self._emit("stdout", _scrub(recipe))
            _blocked = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=self._running_version(info),
                after_version="", state="blocked", error_code="install_method_unsupported",
                error_detail=_scrub(gate_detail)[:500])
            return attach_timed_out(_blocked, False)
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
                error_detail="unknown activity requires plan-specific owner ack")
        if activity.state == "unknown" and activity_ack:
            self._emit("stdout", "proceeding with recorded activity ack")
        need = plan.required_space_bytes or required_space_bytes(
            STAGING_ESTIMATE_BYTES, BACKUP_ESTIMATE_BYTES)
        if need is None:
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail="unknown space estimate blocks mutation")
        disk_ok, disk_detail, _per_fs = check_disk(
            [str(info.get("state_path") or "/"), "/var/lib/ega-update/backups"], need)
        if not disk_ok:
            return ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=current.version,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail=disk_detail[:500])
        before = current.version
        if before and before == plan.target:
            _already = ExecuteResult(
                tool=self.tool_id, exit_code=0, before_version=before,
                after_version=before, state="already_current", error_code="",
                error_detail="already at nightly %s; not upgrade evidence" % plan.target)
            return attach_timed_out(_already, False)
        npx = nvm_paths()["npx"]
        ok, _resolved, detail = resolve_executable(npx)
        if not ok:
            _bad = ExecuteResult(
                tool=self.tool_id, exit_code=3, before_version=before,
                after_version="", state="blocked", error_code="install_method_unsupported",
                error_detail=detail)
            return attach_timed_out(_bad, False)
        # Official managed user-service mutation template only. Shared
        # contract: mutation timeout = step_timeout + 120s margin (probes
        # keep fixed timeouts).
        spec = "t3@%s" % plan.target
        mutation_to = mutation_timeout_for(plan, "updating", default=1800)
        res = run_fixed([npx, "--yes", spec, "service", "update"], timeout=mutation_to)
        self._emit("stdout", "$ npx --yes <pinned-t3> service update -> exit=%d (mutation timeout %ds = step + %ds margin)"
                   % (res.exit_code, mutation_to, MUTATION_TIMEOUT_MARGIN_S))
        self._emit("stdout", _scrub(res.stdout or "")[-4000:])
        if res.stderr:
            self._emit("stderr", _scrub(res.stderr or "")[-4000:])
        after = self._running_version(self._inventory())
        if res.timed_out:
            _tout = ExecuteResult(
                tool=self.tool_id, exit_code=4, before_version=before,
                after_version=after, state="install_failed", error_code="timeout",
                error_detail="t3 service update timed out; possible partial install")
            return attach_timed_out(_tout, True)
        if res.exit_code != 0:
            combined = ((res.stdout or "") + "\n" + (res.stderr or "")).lower()
            if "rollback" in combined or "restored" in combined:
                _rb = ExecuteResult(
                    tool=self.tool_id, exit_code=4, before_version=before,
                    after_version=after, state="install_failed",
                    error_code="install_failed",
                    error_detail="native rollback after failed update (restored %s); recorded as unsuccessful requested update"
                    % (after or "unknown"))
                return attach_timed_out(_rb, False)
            _fail = ExecuteResult(
                tool=self.tool_id, exit_code=4, before_version=before,
                after_version=after, state="install_failed", error_code="install_failed",
                error_detail="t3 service update exit=%d" % res.exit_code)
            return attach_timed_out(_fail, False)
        # Exact final target equality: for target_mode exact the running
        # version MUST equal the planned target, else verification failure
        # (runner enforces centrally; adapters enforce their own check).
        if plan.target_mode == "exact" and after != plan.target:
            _mismatch = ExecuteResult(
                tool=self.tool_id, exit_code=4, before_version=before,
                after_version=after, state="install_failed", error_code="install_failed",
                error_detail="exact-target mismatch: after=%s planned=%s" % (after or "?", plan.target))
            return attach_timed_out(_mismatch, False)
        _done = ExecuteResult(
            tool=self.tool_id, exit_code=0, before_version=before,
            after_version=after, state="succeeded", error_code="",
            error_detail="")
        return attach_timed_out(_done, False)

    def _endpoint_probe(self, info):
        # type: (Dict[str, object]) -> Tuple[str, str]
        endpoint = str(info.get("endpoint") or "")
        if not endpoint:
            port = str(info.get("port") or "")
            if port.isdigit():
                endpoint = "http://127.0.0.1:%s/health" % port
        if not endpoint:
            return "unknown", "no configured endpoint to probe"
        try:
            import urllib.request

            request = urllib.request.Request(endpoint, method="GET")
            with urllib.request.urlopen(request, timeout=10) as response:
                status = getattr(response, "status", 200)
                response.read(65536)
        except Exception as exc:
            return "fail", "endpoint %s unreachable: %s" % ("[configured]", str(exc)[:200])
        if status == 200:
            return "pass", "endpoint reachable (http 200; readiness only)"
        return "fail", "endpoint http %s" % status

    def verify(self, expected_target=""):
        # type: (str) -> VerifyResult
        """Verify running version + unit + endpoint + pinning + exact target.

        expected_target (optional): when the caller knows the planned exact
        target, equality is enforced here (mismatch is failure). Without it
        the exact-target check records the runner-central + execute-local
        enforcement as not_applicable. Running version always requires
        process/endpoint corroboration, never the state file alone.
        """
        checks = []  # type: List[CheckItem]
        info = self._inventory()
        gate_ok, gate_detail = self._inventory_gate(dict(info))
        if not gate_ok:
            checks.append(CheckItem(
                name="inventory_gate", result="fail", mandatory=True,
                summary=_scrub(gate_detail)[:400]))
            return VerifyResult(
                tool=self.tool_id, version="", checks=checks, passed=False,
                error_code="health_failed", error_detail="t3 inventory gate unverified")
        running_version = self._running_version(self._inventory())
        checks.append(CheckItem(
            name="running_version",
            result="pass" if running_version else "fail", mandatory=True,
            summary=("running version %s (process/endpoint corroborated)" % running_version) if running_version
            else "required running-version evidence missing; state-file metadata alone insufficient"))
        if expected_target:
            if running_version and running_version == expected_target:
                checks.append(CheckItem(
                    name="exact_target", result="pass", mandatory=True,
                    summary="running %s equals planned exact target %s" % (running_version, expected_target)))
            else:
                checks.append(CheckItem(
                    name="exact_target", result="fail", mandatory=True,
                    summary="exact-target mismatch: running=%s planned=%s" % (running_version or "?", expected_target)))
        else:
            checks.append(CheckItem(
                name="exact_target", result="not_applicable", mandatory=False,
                summary="no expected target passed to verify; exact equality enforced in execute() + centrally by runner"))
        ready = (info.get("unit_active") == "active" and info.get("unit_sub") == "running")
        checks.append(CheckItem(
            name="unit_readiness", result="pass" if ready else "fail", mandatory=True,
            summary="unit %s active=%s sub=%s" % (
                info.get("unit"), info.get("unit_active") or "?", info.get("unit_sub") or "?")))
        endpoint_result, endpoint_summary = self._endpoint_probe(info)
        checks.append(CheckItem(
            name="endpoint_readiness", result=endpoint_result, mandatory=True,
            summary=endpoint_summary))
        pinned = self._launch_pinned(info, running_version)
        checks.append(pinned)
        passed = bool(running_version) and not any(
            c for c in checks if c.mandatory and c.result != "pass")
        return VerifyResult(
            tool=self.tool_id, version=running_version, checks=checks,
            passed=passed, error_code="" if passed else "health_failed",
            error_detail="" if passed else "mandatory t3 checks failed")

    def _launch_pinned(self, info, running_version):
        # type: (Dict[str, object], str) -> CheckItem
        show = run_fixed(
            ["/bin/systemctl", "--user", "show", str(info.get("unit")),
             "-p", "ExecStart"], timeout=30)
        exec_start = _scrub(show.stdout or "")
        if not show.ok():
            return CheckItem(
                name="launch_pinned", result="unknown", mandatory=True,
                summary="unit definition unreadable; pinning unproven")
        if running_version and running_version in (show.stdout or ""):
            return CheckItem(
                name="launch_pinned", result="pass", mandatory=True,
                summary="runtime launch pinned to installed %s" % running_version)
        if "latest" in (show.stdout or "").lower() or "nightly" in (show.stdout or "").lower():
            return CheckItem(
                name="launch_pinned", result="fail", mandatory=True,
                summary="runtime launch resolves latest/nightly on restart; pin to %s"
                % (running_version or "installed version"))
        return CheckItem(
            name="launch_pinned", result="unknown", mandatory=True,
            summary="launch pinning unproven from unit definition")
