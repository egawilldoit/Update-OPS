"""Codex adapter: native standalone updater (subagent B owned).

Audited layout: /home/ubuntu/.local/bin/codex links into
/home/ubuntu/.codex/packages/standalone/releases/. Alternate
/usr/local/bin/codex is preserved untouched and never invoked for mutation.
Native `update` is latest-tracking: target_mode=native_latest, no invented
version flag. Daemon probes use `app-server daemon version/restart` only when
local help confirms the interface; structured output distinguishes
absent/healthy/stale/failed-probe and a probe error is never treated as
absence. Never starts an absent daemon, re-pairs remote, or deletes auth.
Retains release metadata without promising DB rollback. After update runs the
configured T3 integration checks when present, else records the coverage
limitation (a generic HTTP 200 is never labeled provider proof).

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
    EXIT_BLOCKED,
    EXIT_INSTALL_FAILED,
    EXIT_OK,
    MUTATION_TIMEOUT_MARGIN_S,
    attach_plan_v2,
    attach_timed_out,
    check_disk,
    default_measure_footprint,
    extract_version_token,
    fingerprint,
    footprint_has_unknown,
    mutation_timeout_for,
    plan_has_unknown_budget,
    resolve_executable,
    required_space_bytes,
    run_fixed,
    sanitize_evidence,
)
from ..schemas import utcnow_iso

CODEX_BIN = "/home/ubuntu/.local/bin/codex"
CODEX_HOME = "/home/ubuntu/.codex"
STANDALONE_DIR = "/home/ubuntu/.codex/packages/standalone/releases"
ALT_BIN = "/usr/local/bin/codex"

# R28: no fixed invented staging/backup constants. Footprint derives from
# measure_footprint (release metadata dir tree, bounded walk 200k entries);
# staging is the retained previous-release metadata (measured, not invented).
# The 3 GiB floor comparison lives in the runner, never in adapters.


def _emit_noop(stream, line):
    # type: (str, str) -> None
    return None


class CodexAdapter(Adapter):
    tool_id = "codex"
    enabled = True

    def __init__(self):
        # type: () -> None
        self._emit = _emit_noop

    # -- internal probes -------------------------------------------------

    def measure_footprint(self, plan=None):
        # type: (object) -> dict
        """Measured actuals {fs_path: bytes} (release metadata tree)."""
        try:
            trees = [STANDALONE_DIR]
            try:
                homes = getattr(plan, "state_homes", None)
                if isinstance(homes, list):
                    for home in homes:
                        if isinstance(home, str) and home and home not in trees:
                            trees.append(home)
            except Exception:
                pass
            return default_measure_footprint("codex", db_paths=[], tree_paths=trees)
        except Exception:
            return {"unknown:codex": -1}

    def _version_probe(self):
        # type: () -> Tuple[str, str]
        """Return (version, raw). Preserves exit status; '' on failure."""
        res = run_fixed([CODEX_BIN, "--version"], timeout=60, scope_unit=None)
        self._emit("stdout", "$ %s --version -> exit=%d" % (CODEX_BIN, res.exit_code))
        if not res.ok():
            return "", sanitize_evidence((res.stdout + res.stderr).strip()[:2000])
        token = extract_version_token(res.stdout + "\n" + res.stderr)
        return token, sanitize_evidence((res.stdout + res.stderr).strip()[:2000])

    def _update_help(self):
        # type: () -> object
        res = run_fixed([CODEX_BIN, "update", "--help"], timeout=60, scope_unit=None)
        self._emit("stdout", "$ codex update --help -> exit=%d" % res.exit_code)
        return res

    def _daemon_help(self):
        # type: () -> object
        res = run_fixed([CODEX_BIN, "app-server", "daemon", "--help"], timeout=60,
                        scope_unit=None)
        self._emit("stdout", "$ codex app-server daemon --help -> exit=%d" % res.exit_code)
        return res

    def _daemon_status(self):
        # type: () -> Tuple[str, str, str]
        """Return (state, version, detail).

        state in absent|healthy|stale|failed-probe|unknown-interface.
        Absence proof REQUIRES the CLI's structured status schema parsed
        successfully showing no daemon (e.g. JSON {"daemon": null} /
        {"running": false} / {"state": "absent"}). Broad substrings like
        "not found" alone are unknown (never absence proof). Nonempty output
        alone is never presence; unverifiable version is failed-probe/unknown,
        never healthy.
        """
        help_res = self._daemon_help()
        help_text = (help_res.stdout + "\n" + help_res.stderr)
        if not help_res.ok() or "version" not in help_text:
            return (
                "unknown-interface",
                "",
                sanitize_evidence(
                    "daemon interface unconfirmed by local help; no daemon probe attempted"),
            )
        res = run_fixed([CODEX_BIN, "app-server", "daemon", "version"], timeout=60,
                        scope_unit=None)
        self._emit("stdout", "$ codex app-server daemon version -> exit=%d" % res.exit_code)
        combined = (res.stdout + "\n" + res.stderr).strip()
        # Structured schema first: JSON must parse and explicitly show absence
        # or state/version. Plain-text substrings alone never prove absence.
        try:
            payload = json.loads(res.stdout.strip()) if res.stdout.strip() else None
        except Exception:
            payload = None
        if isinstance(payload, dict):
            lowered_keys = {str(k).lower(): v for k, v in payload.items()}
            # Explicit absence shapes.
            for key in ("daemon", "running", "active", "present", "exists"):
                if key in lowered_keys:
                    val = lowered_keys[key]
                    if val in (None, False, 0, "", "false", "no", "absent", "stopped"):
                        return "absent", "", sanitize_evidence(
                            "daemon structured status shows no daemon: %s" % combined[:800])
            state_raw = str(payload.get("state", "") or payload.get("status", "") or "")
            if state_raw.lower() in ("absent", "not_running", "not-running",
                                     "stopped", "missing", "none"):
                return "absent", "", sanitize_evidence(
                    "daemon structured state absent: %s" % combined[:800])
            if not res.ok():
                return "failed-probe", "", sanitize_evidence(
                    "daemon probe error (not absence): exit=%d %s" % (
                        res.exit_code, combined[:800]))
            version = str(payload.get("version", "") or "")
            if not version:
                version = extract_version_token(combined)
            if not version:
                return "failed-probe", "", sanitize_evidence(
                    "daemon structured output lacks verifiable version: %s" % combined[:800])
            if state_raw.lower() in ("stale", "outdated", "mismatch"):
                return "stale", sanitize_evidence(version), sanitize_evidence(
                    "daemon reports stale state: %s" % combined[:800])
            return "healthy", sanitize_evidence(version), sanitize_evidence(
                "daemon structured version ok: %s" % combined[:800])
        # Non-JSON: exit!=0 with only broad substrings is unknown (not absence).
        if not res.ok():
            return "failed-probe", "", sanitize_evidence(
                "daemon probe error (broad substrings alone never absence proof): exit=%d %s" % (
                    res.exit_code, combined[:800]))
        version = extract_version_token(combined)
        if not version and not combined:
            return "failed-probe", "", sanitize_evidence("daemon probe returned empty output")
        if not version:
            return "failed-probe", "", sanitize_evidence(
                "daemon version unverifiable (no structured version): %s" % combined[:800])
        return "healthy", sanitize_evidence(version), sanitize_evidence(
            "daemon version output: %s" % combined[:800])

    def _owner_of(self, path):
        # type: (str) -> str
        try:
            st = os.stat(path)
            return "%d:%d" % (st.st_uid, st.st_gid)
        except OSError:
            return "unknown"

    def _releases(self):
        # type: () -> List[str]
        try:
            entries = sorted(os.listdir(STANDALONE_DIR))
        except OSError:
            return []
        return [entry for entry in entries if entry and not entry.startswith(".")][:50]

    # -- Adapter interface -----------------------------------------------

    def inspect(self):
        # type: () -> InspectResult
        ok, resolved, detail = resolve_executable(CODEX_BIN)
        version, raw = self._version_probe() if ok else ("", "unresolvable executable")
        releases = self._releases()
        owner = self._owner_of(CODEX_BIN)
        install_kind = "standalone" if (
            ok and resolved.startswith(STANDALONE_DIR)) else ("unknown" if not ok else "standalone-unconfirmed")
        alt_note = ""
        try:
            if os.path.exists(ALT_BIN):
                alt_note = "; alternate %s preserved untouched" % ALT_BIN
        except Exception:
            alt_note = ""
        fp = fingerprint(CODEX_BIN, resolved, version, owner, install_kind)
        return InspectResult(
            tool=self.tool_id,
            install_identity="standalone:%s" % (resolved or CODEX_BIN),
            executable=CODEX_BIN,
            resolved_target=resolved,
            version=version,
            commit="",
            install_kind=install_kind,
            owner=owner,
            source_clean="unknown",
            source_detail=sanitize_evidence(
                "standalone install has no git checkout; %s%s" % (detail, alt_note)),
            # H02: "codex-daemon" is an app-server concept, NOT a systemd
            # unit, so it must never appear in delegated services (a
            # non-unit name would fail structural parsing at reconcile
            # time). Daemon liveness/version is proven by the
            # daemon_readiness/daemon_version_agreement required checks,
            # not by unit queries.
            services=[],
            state_dirs=[d for d in (CODEX_HOME, STANDALONE_DIR) if os.path.exists(d)],
            channel="standalone-latest",
            fingerprint=fp,
        )

    def discover(self):
        # type: () -> DiscoverResult
        ok, resolved, detail = resolve_executable(CODEX_BIN)
        if not ok:
            return DiscoverResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel="standalone-latest", available=False,
                unknown_reason=sanitize_evidence("install_method_unsupported: %s" % detail))
        help_res = self._update_help()
        help_text = help_res.stdout + "\n" + help_res.stderr
        if not help_res.ok():
            return DiscoverResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel="standalone-latest", available=False,
                unknown_reason=sanitize_evidence(
                    "native update interface unconfirmed: exit=%d" % help_res.exit_code))
        # Native updater is latest-tracking; never invent a version flag.
        # Record the observed candidate (current version) for disclosure.
        version, _raw = self._version_probe()
        return DiscoverResult(
            tool=self.tool_id, target="native-latest", target_mode="native_latest",
            channel="standalone-latest", available=True,
            unknown_reason="",
        )

    def _codex_processes(self):
        # type: () -> Tuple[int, str]
        """Count foreground Codex processes/sessions (fixed argv, shell=False).

        Returns (count, evidence). A probe failure yields (0, reason) and the
        caller treats activity as unknown, never idle.
        """
        proc = run_fixed(["/bin/ps", "-eo", "pid,comm,args"], timeout=30,
                         scope_unit=None)
        if not proc.ok():
            return 0, sanitize_evidence("process probe unavailable")
        hits = [line for line in proc.stdout.splitlines()
                if "codex" in line.lower() and "ps -eo" not in line]
        return len(hits), sanitize_evidence("%d codex process line(s) observed" % len(hits))

    def activity(self):
        # type: () -> ActivityResult
        state, daemon_version, detail = self._daemon_status()
        if state == "healthy" and daemon_version:
            return ActivityResult(
                tool=self.tool_id, state="unknown",
                evidence=sanitize_evidence(
                    "daemon healthy but task state unproven (%s); interruption risk disclosed" % detail[:500]),
                checked_at=utcnow_iso())
        if state == "absent":
            # Structured-schema absence only (broad substrings never reach
            # here as absent). Even structured absence is NOT idle proof when
            # foreground processes exist without daemon correlation.
            count, proc_evidence = self._codex_processes()
            if "unavailable" in proc_evidence:
                return ActivityResult(
                    tool=self.tool_id, state="unknown",
                    evidence=sanitize_evidence(
                        "daemon absent (structured) but %s; cannot prove idle (%s)" % (
                            proc_evidence, detail[:300])),
                    checked_at=utcnow_iso())
            if count > 0:
                return ActivityResult(
                    tool=self.tool_id, state="unknown",
                    evidence=sanitize_evidence(
                        "daemon absent (structured) but %s without healthy daemon correlation; foreground session unproven, never idle (%s)" % (
                            proc_evidence, detail[:300])),
                    checked_at=utcnow_iso())
            return ActivityResult(
                tool=self.tool_id, state="idle",
                evidence=sanitize_evidence(
                    "no daemon present (structured absence) and no codex processes observed (%s)" % detail[:500]),
                checked_at=utcnow_iso())
        return ActivityResult(
            tool=self.tool_id, state="unknown",
            evidence=sanitize_evidence(
                "codex activity unproven (%s); treat as unknown until ack" % detail[:500]),
            checked_at=utcnow_iso())

    def plan(self):
        # type: () -> PlanResult
        inspection = self.inspect()
        discovery = self.discover()
        daemon_state, daemon_ver, daemon_detail = self._daemon_status()
        daemon_expected = daemon_state in ("healthy", "stale", "failed-probe")
        if not discovery.available:
            _blocked = PlanResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel="standalone-latest", fingerprint=inspection.fingerprint,
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
                backup_policy={"mode": "retain-metadata"},
                required_probes=["cli_version", "cli_startup"],
                budgets={"unknown:codex": -1}, space_fs={},
                deadlines={}, restart_detail=sanitize_evidence(discovery.unknown_reason),
                activity_ts="", release_path="",
                required_checks=["cli_version", "cli_startup"],
                scope_unit=None, daemon_expected=daemon_expected,
                daemon_status_at_plan=sanitize_evidence(daemon_state))
            return _blocked
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

            affected = [CODEX_BIN, STANDALONE_DIR, "/var/lib/ega-update/backups"]
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
            budgets = {"unknown:codex": -1}
            space_fs = {}
        if budgets and all(isinstance(v, int) and v >= 0 for v in budgets.values()):
            try:
                need = max(int(v) for v in budgets.values())
            except Exception:
                need = 0
        else:
            need = 0
        required_checks = ["cli_version", "cli_startup",
                           "daemon_readiness", "daemon_version_agreement",
                           "release_metadata_retained"]
        plan_obj = PlanResult(
            tool=self.tool_id, target=discovery.target, target_mode="native_latest",
            channel="standalone-latest", fingerprint=inspection.fingerprint,
            services=list(inspection.services),
            backup_scope={
                "covered": sanitize_evidence(
                    "previous standalone release dir retained under %s (metadata only; never rollback capability)" % STANDALONE_DIR),
                "omitted": sanitize_evidence("auth/session data, alternate %s" % ALT_BIN),
                "consistency": sanitize_evidence(
                    "retain-previous-release-dir metadata only; no DB rollback promised, no automatic restore capability"),
                "mode": "retain-metadata",
            },
            required_space_bytes=need,
            steps=["preflight", "backup", "updating", "verifying"],
            timeouts=dict(ADAPTER_TIMEOUT_DEFAULTS),
            restart_impact=sanitize_evidence(
                "existing healthy/stale daemon (if present) may restart after CLI update; absent daemons are never started; failed-probe daemons are NOT eligible for automatic restart (manual repair required)"),
            already_current=False,
        )
        attach_plan_v2(
            plan_obj, install_identity=inspection.install_identity,
            artifact={"channel": "standalone-latest"},
            config_hash="", plan_hash="",
            launch={}, state_homes=list(inspection.state_dirs or []),
            backup_policy={"mode": "retain-metadata"},
            required_probes=list(required_checks),
            budgets=dict(budgets), space_fs=dict(space_fs),
            deadlines={}, restart_detail=sanitize_evidence(
                "daemon_expected=%s status_at_plan=%s" % (daemon_expected, daemon_state)),
            activity_ts="", release_path="",
            required_checks=list(required_checks),
            scope_unit=None, daemon_expected=daemon_expected,
            daemon_status_at_plan=sanitize_evidence(daemon_state))
        return plan_obj

    def backup(self, job_id):
        # type: (str) -> BackupResult
        releases = self._releases()
        scope = {
            "covered": sanitize_evidence(
                "standalone release metadata under %s (%d entries; metadata only, never rollback capability)" % (
                    STANDALONE_DIR, len(releases))),
            "omitted": sanitize_evidence("auth/session data, alternate binary, full home archive"),
            "consistency": sanitize_evidence(
                "retain-previous-release-dir metadata only; no DB rollback promised, no automatic restore capability"),
        }
        if not os.path.isdir(STANDALONE_DIR):
            return BackupResult(
                tool=self.tool_id, supported=False, path="", scope=scope,
                consistency=scope["consistency"], size_bytes=0,
                unsupported_reason=sanitize_evidence(
                    "backup_unsupported: standalone releases dir missing"))
        # Measured size (bounded walk) for evidence; unknown does not block
        # retain-metadata but is recorded.
        try:
            footprint = self.measure_footprint(None)
            size = sum(int(v) for v in footprint.values() if isinstance(v, int) and v >= 0)
        except Exception:
            size = 0
        return BackupResult(
            tool=self.tool_id, supported=True,
            path=sanitize_evidence("%s (retained in place; job %s)" % (STANDALONE_DIR, job_id)),
            scope=scope, consistency=scope["consistency"],
            size_bytes=size, unsupported_reason="")

    def execute(self, plan, job_id, activity_ack=False):
        # type: (PlanResult, str, bool) -> ExecuteResult
        # Frozen: use plan-provided target/timeouts/scope; fresh rechecks may
        # INVALIDATE (blocked) but never substitute new values.
        if plan.target_mode != "native_latest":
            _blocked = ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version="",
                after_version="", state="blocked", error_code="invalid_request",
                error_detail=sanitize_evidence("codex supports only target_mode=native_latest"))
            return attach_timed_out(_blocked, False)
        if plan_has_unknown_budget(plan):
            _cur0 = ""
            try:
                _cur0 = self.inspect().version
            except Exception:
                pass
            _bd0 = ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version=_cur0,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail=sanitize_evidence("unknown footprint blocks mutation (budgets -1)"))
            return attach_timed_out(_bd0, False)
        try:
            scope_unit = getattr(plan, "scope_unit", None)
        except Exception:
            scope_unit = None
        ok, resolved, detail = resolve_executable(CODEX_BIN)
        if not ok:
            _b1 = ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version="",
                after_version="", state="blocked", error_code="install_method_unsupported",
                error_detail=sanitize_evidence(detail))
            return attach_timed_out(_b1, False)
        if not resolved.startswith(STANDALONE_DIR):
            _b2 = ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version="",
                after_version="", state="blocked", error_code="install_method_unsupported",
                error_detail=sanitize_evidence(
                    "codex binary does not resolve under %s: %s" % (STANDALONE_DIR, resolved)))
            return attach_timed_out(_b2, False)
        current = self.inspect()
        if plan.fingerprint and current.fingerprint != plan.fingerprint:
            _bf = ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version=current.version,
                after_version="", state="blocked", error_code="fingerprint_changed",
                error_detail=sanitize_evidence("installation changed since plan; fresh plan required"))
            return attach_timed_out(_bf, False)
        activity = self.activity()
        if activity.state == "busy":
            _ba = ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version=current.version,
                after_version="", state="blocked", error_code="activity_blocked",
                error_detail=sanitize_evidence(activity.evidence[:500]))
            return attach_timed_out(_ba, False)
        if activity.state == "unknown" and not activity_ack:
            _bk = ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version=current.version,
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
                        tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version=current.version,
                        after_version="", state="blocked", error_code="disk_blocked",
                        error_detail=sanitize_evidence("unknown footprint blocks mutation"))
                    return attach_timed_out(_bu, False)
                need = sum(int(v) for v in fp_now.values() if isinstance(v, int) and v >= 0)
            except Exception:
                need = None
        if need is None:
            _bn = ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version=current.version,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail=sanitize_evidence("unknown space estimate blocks mutation"))
            return attach_timed_out(_bn, False)
        try:
            need_int = int(need)
        except (TypeError, ValueError):
            _bn2 = ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version=current.version,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail=sanitize_evidence("unknown space estimate blocks mutation"))
            return attach_timed_out(_bn2, False)
        disk_ok, disk_detail, _per_fs = check_disk(
            [CODEX_BIN, STANDALONE_DIR, "/var/lib/ega-update/backups"], need_int)
        if not disk_ok:
            _bd = ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version=current.version,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail=sanitize_evidence(disk_detail[:500]))
            return attach_timed_out(_bd, False)
        before = current.version
        daemon_before, daemon_ver_before, daemon_detail_before = self._daemon_status()
        mutation_to = mutation_timeout_for(plan, "updating", default=1800)
        res = run_fixed([CODEX_BIN, "update"], timeout=mutation_to, scope_unit=scope_unit)
        self._emit("stdout", "$ codex update -> exit=%d (mutation timeout %ds = step + %ds margin)"
                   % (res.exit_code, mutation_to, MUTATION_TIMEOUT_MARGIN_S))
        self._emit("stdout", sanitize_evidence((res.stdout or "")[-4000:]))
        if res.stderr:
            self._emit("stderr", sanitize_evidence((res.stderr or "")[-4000:]))
        if res.timed_out:
            _tout = ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_INSTALL_FAILED, before_version=before,
                after_version="", state="install_failed", error_code="timeout",
                error_detail=sanitize_evidence("codex update timed out; possible partial install"))
            return attach_timed_out(_tout, True)
        if res.exit_code != 0:
            after, _raw = self._version_probe()
            _fail = ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_INSTALL_FAILED, before_version=before,
                after_version=after, state="install_failed", error_code="install_failed",
                error_detail=sanitize_evidence("codex update exit=%d" % res.exit_code))
            return attach_timed_out(_fail, False)
        after, _raw = self._version_probe()
        combined = (res.stdout + "\n" + res.stderr).lower()
        already = ("already" in combined and ("latest" in combined or "up to date" in combined
                                              or "up-to-date" in combined)) or (after == before and before != "")
        # Already-current -> NO restart (never restart a current daemon).
        if already:
            _already = ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_OK, before_version=before,
                after_version=after, state="already_current", error_code="",
                error_detail=sanitize_evidence(
                    "native updater reports already-current; no restart attempted, not upgrade evidence"))
            return attach_timed_out(_already, False)
        # Restart ONLY when daemon confirmed present (healthy/stale via
        # structured schema) + version/state requires (daemon ver != after or
        # stale) + still exists at restart time + activity allows. Never start
        # an absent daemon; failed-probe never eligible (manual repair).
        if daemon_before == "failed-probe":
            self._emit("stdout", "failed_probe daemon NOT eligible for automatic restart; "
                                 "manual repair required before daemon restart (%s)" % sanitize_evidence(
                                     daemon_detail_before[:300]))
        elif daemon_before in ("healthy", "stale"):
            # Still-exists + version/state-requires recheck.
            daemon_now, daemon_ver_now, daemon_detail_now = self._daemon_status()
            if daemon_now not in ("healthy", "stale"):
                self._emit("stdout", "daemon no longer confirmed present (%s); skipping restart" % sanitize_evidence(
                    daemon_detail_now[:300]))
            elif daemon_ver_now and after and daemon_ver_now == after and daemon_now == "healthy":
                self._emit("stdout", "daemon version %s already matches CLI %s; no restart required" % (
                    sanitize_evidence(daemon_ver_now)[:80], sanitize_evidence(after)[:80]))
            else:
                # Activity allows: recheck activity before restart.
                act_now = self.activity()
                if act_now.state == "busy" or (act_now.state == "unknown" and not activity_ack):
                    self._emit("stdout", "activity %s blocks daemon restart; skipping (verify will fail until repaired)" % sanitize_evidence(
                        act_now.state))
                else:
                    help_text = self._daemon_help()
                    help_combined = help_text.stdout + "\n" + help_text.stderr
                    if help_text.ok() and "restart" in help_combined:
                        restart_res = run_fixed(
                            [CODEX_BIN, "app-server", "daemon", "restart"], timeout=300,
                            scope_unit=scope_unit)
                        self._emit("stdout", "$ codex app-server daemon restart -> exit=%d" % restart_res.exit_code)
                        if restart_res.exit_code != 0:
                            _rstart = ExecuteResult(
                                tool=self.tool_id, exit_code=EXIT_INSTALL_FAILED, before_version=before,
                                after_version=after, state="install_failed", error_code="install_failed",
                                error_detail=sanitize_evidence(
                                    "daemon restart failed after update: exit=%d" % restart_res.exit_code))
                            return attach_timed_out(_rstart, False)
        _done = ExecuteResult(
            tool=self.tool_id, exit_code=EXIT_OK, before_version=before,
            after_version=after, state="succeeded", error_code="",
            error_detail="")
        return attach_timed_out(_done, False)

    def verify(self, plan=None, required_checks=None, **kwargs):
        # type: (...) -> VerifyResult
        """Verify with daemon_expected binding (R25).

        plan may carry daemon_expected/daemon_status_at_plan (plan records
        them). Expected daemon missing/unprobable/unverifiable-version fails
        (never not_applicable). Only when the plan explicitly records
        daemon_expected=False does structured absence yield not_applicable.
        All summaries sanitized; required_checks covered by name.
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
        try:
            daemon_expected = getattr(plan, "daemon_expected", None) if plan is not None else None
        except Exception:
            daemon_expected = None
        # Default fail-closed: when expectation unknown, treat missing daemon
        # as expected (fail, not N/A) so absence never silently passes.
        if daemon_expected is None:
            daemon_expected = True
        else:
            try:
                daemon_expected = bool(daemon_expected)
            except Exception:
                daemon_expected = True
        checks = []  # type: List[CheckItem]
        version, raw = self._version_probe()
        checks.append(CheckItem(
            name="cli_version", result="pass" if version else "fail", mandatory=True,
            summary=sanitize_evidence(
                ("version %s" % version) if version else "version unreadable: %s" % raw[:300])))
        startup = run_fixed([CODEX_BIN, "--help"], timeout=60, scope_unit=None)
        checks.append(CheckItem(
            name="cli_startup", result="pass" if startup.ok() else "fail", mandatory=True,
            summary=sanitize_evidence("cli startup exit=%d" % startup.exit_code)))
        state, daemon_version, detail = self._daemon_status()
        if state == "unknown-interface":
            if daemon_expected:
                checks.append(CheckItem(
                    name="daemon_readiness", result="fail", mandatory=True,
                    summary=sanitize_evidence(
                        "expected daemon unprobable (interface unconfirmed); %s" % detail[:300])))
                checks.append(CheckItem(
                    name="daemon_version_agreement", result="fail", mandatory=True,
                    summary=sanitize_evidence("expected daemon version unverifiable; failing closed")))
            else:
                checks.append(CheckItem(
                    name="daemon_readiness", result="not_applicable", mandatory=False,
                    summary=sanitize_evidence("daemon interface unconfirmed; %s" % detail[:300])))
                checks.append(CheckItem(
                    name="daemon_version_agreement", result="not_applicable", mandatory=False,
                    summary=sanitize_evidence("no daemon interface to compare")))
        elif state == "absent":
            if daemon_expected:
                checks.append(CheckItem(
                    name="daemon_readiness", result="fail", mandatory=True,
                    summary=sanitize_evidence(
                        "expected daemon missing (structured absence); fail, never N/A: %s" % detail[:300])))
                checks.append(CheckItem(
                    name="daemon_version_agreement", result="fail", mandatory=True,
                    summary=sanitize_evidence("expected daemon absent; version unverifiable")))
            else:
                checks.append(CheckItem(
                    name="daemon_readiness", result="not_applicable", mandatory=False,
                    summary=sanitize_evidence(
                        "no daemon was present (structured absence); absent daemon never started")))
                checks.append(CheckItem(
                    name="daemon_version_agreement", result="not_applicable", mandatory=False,
                    summary=sanitize_evidence("no daemon to compare")))
        elif state == "failed-probe":
            checks.append(CheckItem(
                name="daemon_readiness", result="fail", mandatory=True,
                summary=sanitize_evidence("daemon probe failed (not absence): %s" % detail[:300])))
            checks.append(CheckItem(
                name="daemon_version_agreement", result="unknown", mandatory=True,
                summary=sanitize_evidence("daemon version unproven after failed probe")))
        elif state == "stale":
            checks.append(CheckItem(
                name="daemon_readiness", result="fail", mandatory=True,
                summary=sanitize_evidence("daemon stale after update: %s" % detail[:300])))
            checks.append(CheckItem(
                name="daemon_version_agreement", result="fail", mandatory=True,
                summary=sanitize_evidence("daemon %s disagrees with cli %s" % (daemon_version, version))))
        else:
            agree = bool(version and daemon_version and version == daemon_version)
            checks.append(CheckItem(
                name="daemon_readiness", result="pass", mandatory=True,
                summary=sanitize_evidence("daemon healthy: %s" % detail[:300])))
            if not daemon_version or not version:
                checks.append(CheckItem(
                    name="daemon_version_agreement", result="fail", mandatory=True,
                    summary=sanitize_evidence(
                        "daemon version unverifiable (cli=%s daemon=%s); failing closed" % (
                            version or "?", daemon_version or "?"))))
            else:
                checks.append(CheckItem(
                    name="daemon_version_agreement",
                    result="pass" if agree else "fail",
                    mandatory=True,
                    summary=sanitize_evidence("cli %s vs daemon %s" % (version or "?", daemon_version or "?"))))
        releases = self._releases()
        checks.append(CheckItem(
            name="release_metadata_retained",
            result="pass" if releases else "fail", mandatory=True,
            summary=sanitize_evidence(
                "%d standalone releases retained (metadata only; never rollback capability, no automatic restore)" % len(releases))))
        checks.append(self._t3_integration_check())
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
        mandatory_failed = [c for c in checks if c.mandatory and c.result == "fail"]
        mandatory_unknown = [c for c in checks if c.mandatory and c.result == "unknown"]
        passed = (not mandatory_failed) and (not mandatory_unknown) and bool(version)
        error_code = "" if passed else "health_failed"
        return VerifyResult(
            tool=self.tool_id, version=version, checks=checks, passed=passed,
            error_code=error_code,
            error_detail=sanitize_evidence("" if passed else "mandatory codex checks failed"))

    def _t3_integration_check(self):
        # type: () -> CheckItem
        """Disclosed manual coverage gap; generic HTTP never provider proof.

        Reachable-but-unproven is unknown mandatory False (manual acceptance
        required), never mandatory-fail, never pass. No endpoint is
        not_applicable mandatory False. Unreachable is unknown mandatory
        False with the reachability detail (still a gap, never provider
        failure proof).
        """
        endpoint = os.environ.get("EGA_T3_ENDPOINT", "").strip()
        if not endpoint:
            return CheckItem(
                name="t3_integration", result="not_applicable", mandatory=False,
                summary=sanitize_evidence(
                    "no configured T3 integration probe; coverage gap explicit, manual acceptance required for release"))
        try:
            import urllib.request

            request = urllib.request.Request(endpoint, method="GET")
            with urllib.request.urlopen(request, timeout=10) as response:
                status = getattr(response, "status", 200)
                response.read(65536)
        except Exception as exc:
            return CheckItem(
                name="t3_integration", result="unknown", mandatory=False,
                summary=sanitize_evidence(
                    "configured T3 endpoint unproven (%s); coverage gap explicit, manual acceptance required" % str(exc)[:200]))
        return CheckItem(
            name="t3_integration", result="unknown", mandatory=False,
            summary=sanitize_evidence(
                "T3 endpoint reachable (http %s) but no non-generating provider probe exists; coverage gap explicit, manual acceptance required" % status))
