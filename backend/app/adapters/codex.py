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
    attach_timed_out,
    check_disk,
    extract_version_token,
    fingerprint,
    mutation_timeout_for,
    resolve_executable,
    required_space_bytes,
    run_fixed,
)
from ..schemas import utcnow_iso

CODEX_BIN = "/home/ubuntu/.local/bin/codex"
CODEX_HOME = "/home/ubuntu/.codex"
STANDALONE_DIR = "/home/ubuntu/.codex/packages/standalone/releases"
ALT_BIN = "/usr/local/bin/codex"

# Conservative staging estimate for one standalone release payload plus
# extraction scratch; backup covers retained release metadata (small).
STAGING_ESTIMATE_BYTES = 1 * 1024 * 1024 * 1024
BACKUP_ESTIMATE_BYTES = 256 * 1024 * 1024


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

    def _version_probe(self):
        # type: () -> Tuple[str, str]
        """Return (version, raw). Preserves exit status; '' on failure."""
        res = run_fixed([CODEX_BIN, "--version"], timeout=60)
        self._emit("stdout", "$ %s --version -> exit=%d" % (CODEX_BIN, res.exit_code))
        if not res.ok():
            return "", (res.stdout + res.stderr).strip()[:2000]
        token = extract_version_token(res.stdout + "\n" + res.stderr)
        return token, (res.stdout + res.stderr).strip()[:2000]

    def _update_help(self):
        # type: () -> object
        res = run_fixed([CODEX_BIN, "update", "--help"], timeout=60)
        self._emit("stdout", "$ codex update --help -> exit=%d" % res.exit_code)
        return res

    def _daemon_help(self):
        # type: () -> object
        res = run_fixed([CODEX_BIN, "app-server", "daemon", "--help"], timeout=60)
        self._emit("stdout", "$ codex app-server daemon --help -> exit=%d" % res.exit_code)
        return res

    def _daemon_status(self):
        # type: () -> Tuple[str, str, str]
        """Return (state, version, detail).

        state in absent|healthy|stale|failed-probe|unknown-interface.
        Parses structured output only; nonempty output alone is never presence.
        """
        help_res = self._daemon_help()
        help_text = (help_res.stdout + "\n" + help_res.stderr)
        if not help_res.ok() or "version" not in help_text:
            return (
                "unknown-interface",
                "",
                "daemon interface unconfirmed by local help; no daemon probe attempted",
            )
        res = run_fixed([CODEX_BIN, "app-server", "daemon", "version"], timeout=60)
        self._emit("stdout", "$ codex app-server daemon version -> exit=%d" % res.exit_code)
        combined = (res.stdout + "\n" + res.stderr).strip()
        if not res.ok():
            lowered = combined.lower()
            # Explicit absence markers only; any other error is failed-probe.
            if any(marker in lowered for marker in (
                    "no daemon", "not running", "no such", "not found",
                    "daemon not", "absent")):
                return "absent", "", "daemon version probe reports absence: %s" % combined[:1000]
            return "failed-probe", "", "daemon probe error (not absence): exit=%d %s" % (
                res.exit_code, combined[:1000])
        version = extract_version_token(combined)
        try:
            payload = json.loads(res.stdout.strip()) if res.stdout.strip() else None
        except Exception:
            payload = None
        if isinstance(payload, dict):
            version = str(payload.get("version", "") or version)
            state = str(payload.get("state", "") or payload.get("status", "") or "healthy")
            if state.lower() in ("stale", "outdated", "mismatch"):
                return "stale", version, "daemon reports stale state: %s" % combined[:1000]
            return "healthy", version, "daemon structured version ok: %s" % combined[:1000]
        if not version and not combined:
            return "failed-probe", "", "daemon probe returned empty output"
        # Plain-text version with exit zero counts as healthy only when the
        # help-confirmed interface answered; version agreement is checked later.
        return "healthy", version, "daemon version output: %s" % combined[:1000]

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
            source_detail="standalone install has no git checkout; %s%s" % (detail, alt_note),
            services=["codex-daemon"] if self._daemon_status()[0] in (
                "healthy", "stale", "failed-probe") else [],
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
                unknown_reason="install_method_unsupported: %s" % detail)
        help_res = self._update_help()
        help_text = help_res.stdout + "\n" + help_res.stderr
        if not help_res.ok():
            return DiscoverResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel="standalone-latest", available=False,
                unknown_reason="native update interface unconfirmed: exit=%d" % help_res.exit_code)
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
        proc = run_fixed(["/bin/ps", "-eo", "pid,comm,args"], timeout=30)
        if not proc.ok():
            return 0, "process probe unavailable"
        hits = [line for line in proc.stdout.splitlines()
                if "codex" in line.lower() and "ps -eo" not in line]
        return len(hits), "%d codex process line(s) observed" % len(hits)

    def activity(self):
        # type: () -> ActivityResult
        state, daemon_version, detail = self._daemon_status()
        if state == "healthy" and daemon_version:
            # Daemon answering does not prove a task is idle; without a task
            # API the activity state stays unknown and requires owner ack.
            return ActivityResult(
                tool=self.tool_id, state="unknown",
                evidence="daemon healthy but task state unproven (%s); interruption risk disclosed" % detail[:500],
                checked_at=utcnow_iso())
        if state == "absent":
            # Daemon absence is NOT proof of idle: a foreground Codex
            # process/session without a healthy daemon correlation means the
            # activity state is unknown (never idle) until manually acked.
            count, proc_evidence = self._codex_processes()
            if "unavailable" in proc_evidence:
                return ActivityResult(
                    tool=self.tool_id, state="unknown",
                    evidence="daemon absent but %s; cannot prove idle (%s)" % (proc_evidence, detail[:300]),
                    checked_at=utcnow_iso())
            if count > 0:
                return ActivityResult(
                    tool=self.tool_id, state="unknown",
                    evidence="daemon absent but %s without healthy daemon correlation; foreground session unproven, never idle (%s)" % (proc_evidence, detail[:300]),
                    checked_at=utcnow_iso())
            return ActivityResult(
                tool=self.tool_id, state="idle",
                evidence="no daemon present and no codex processes observed (%s)" % detail[:500],
                checked_at=utcnow_iso())
        return ActivityResult(
            tool=self.tool_id, state="unknown",
            evidence="codex activity unproven (%s); treat as unknown until ack" % detail[:500],
            checked_at=utcnow_iso())

    def plan(self):
        # type: () -> PlanResult
        inspection = self.inspect()
        discovery = self.discover()
        if not discovery.available:
            return PlanResult(
                tool=self.tool_id, target="", target_mode="unknown",
                channel="standalone-latest", fingerprint=inspection.fingerprint,
                services=[], backup_scope={}, required_space_bytes=0,
                steps=[], timeouts=dict(ADAPTER_TIMEOUT_DEFAULTS),
                restart_impact="planning blocked: %s" % discovery.unknown_reason,
                already_current=False,
            )
        need = required_space_bytes(STAGING_ESTIMATE_BYTES, BACKUP_ESTIMATE_BYTES)
        return PlanResult(
            tool=self.tool_id, target=discovery.target, target_mode="native_latest",
            channel="standalone-latest", fingerprint=inspection.fingerprint,
            services=list(inspection.services),
            backup_scope={
                "covered": "previous standalone release dir retained under %s (metadata only; never rollback capability)" % STANDALONE_DIR,
                "omitted": "auth/session data, alternate %s" % ALT_BIN,
                "consistency": "retain-previous-release-dir metadata only; no DB rollback promised, no automatic restore capability",
                "mode": "retain-metadata",
            },
            required_space_bytes=need or 0,
            steps=["preflight", "backup", "updating", "verifying"],
            timeouts=dict(ADAPTER_TIMEOUT_DEFAULTS),
            restart_impact="existing healthy/stale daemon (if present) may restart after CLI update; absent daemons are never started; failed-probe daemons are NOT eligible for automatic restart (manual repair required)",
            already_current=False,
        )

    def backup(self, job_id):
        # type: (str) -> BackupResult
        releases = self._releases()
        scope = {
            "covered": "standalone release metadata under %s (%d entries; metadata only, never rollback capability)" % (STANDALONE_DIR, len(releases)),
            "omitted": "auth/session data, alternate binary, full 3.7 GiB home archive",
            "consistency": "retain-previous-release-dir metadata only; no DB rollback promised, no automatic restore capability",
        }
        if not os.path.isdir(STANDALONE_DIR):
            return BackupResult(
                tool=self.tool_id, supported=False, path="", scope=scope,
                consistency=scope["consistency"], size_bytes=0,
                unsupported_reason="backup_unsupported: standalone releases dir missing")
        return BackupResult(
            tool=self.tool_id, supported=True,
            path="%s (retained in place; job %s)" % (STANDALONE_DIR, job_id),
            scope=scope, consistency=scope["consistency"],
            size_bytes=0, unsupported_reason="")

    def execute(self, plan, job_id, activity_ack=False):
        # type: (PlanResult, str, bool) -> ExecuteResult
        # SPEC S6 rechecks at execution: fingerprint, activity, disk,
        # install-method support. Stale plan or changed installation needs a
        # fresh plan; busy blocks regardless of ack; unknown requires recorded
        # ack (the runner passes the job's ack; a bare call defaults to False
        # and treats unknown as ack_required).
        if plan.target_mode != "native_latest":
            _blocked = ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version="",
                after_version="", state="blocked", error_code="invalid_request",
                error_detail="codex supports only target_mode=native_latest")
            return attach_timed_out(_blocked, False)
        ok, resolved, detail = resolve_executable(CODEX_BIN)
        if not ok:
            return ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version="",
                after_version="", state="blocked", error_code="install_method_unsupported",
                error_detail=detail)
        if not resolved.startswith(STANDALONE_DIR):
            return ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version="",
                after_version="", state="blocked", error_code="install_method_unsupported",
                error_detail="codex binary does not resolve under %s: %s" % (STANDALONE_DIR, resolved))
        current = self.inspect()
        if plan.fingerprint and current.fingerprint != plan.fingerprint:
            return ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version=current.version,
                after_version="", state="blocked", error_code="fingerprint_changed",
                error_detail="installation changed since plan; fresh plan required")
        activity = self.activity()
        if activity.state == "busy":
            return ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version=current.version,
                after_version="", state="blocked", error_code="activity_blocked",
                error_detail=activity.evidence[:500])
        if activity.state == "unknown" and not activity_ack:
            return ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version=current.version,
                after_version="", state="blocked", error_code="ack_required",
                error_detail="unknown activity requires plan-specific owner ack: %s" % activity.evidence[:400])
        if activity.state == "unknown" and activity_ack:
            self._emit("stdout", "proceeding with recorded activity ack: %s"
                       % activity.evidence[:400])
        need = plan.required_space_bytes or required_space_bytes(
            STAGING_ESTIMATE_BYTES, BACKUP_ESTIMATE_BYTES)
        if need is None:
            return ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version=current.version,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail="unknown space estimate blocks mutation")
        disk_ok, disk_detail, _per_fs = check_disk(
            [CODEX_BIN, STANDALONE_DIR, "/var/lib/ega-update/backups"], need)
        if not disk_ok:
            return ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_BLOCKED, before_version=current.version,
                after_version="", state="blocked", error_code="disk_blocked",
                error_detail=disk_detail[:500])
        before = current.version
        daemon_before, _daemon_ver, _daemon_detail = self._daemon_status()
        # Shared contract: mutation subprocess timeout = step_timeout +
        # registry.MUTATION_TIMEOUT_MARGIN_S (120s). Probes above keep fixed
        # timeouts; only this mutating `codex update` adds the margin.
        mutation_to = mutation_timeout_for(plan, "updating", default=1800)
        # Native update path: no version flag, fixed argv, shell=False.
        res = run_fixed([CODEX_BIN, "update"], timeout=mutation_to)
        self._emit("stdout", "$ codex update -> exit=%d (mutation timeout %ds = step + %ds margin)"
                   % (res.exit_code, mutation_to, MUTATION_TIMEOUT_MARGIN_S))
        self._emit("stdout", (res.stdout or "")[-4000:])
        if res.stderr:
            self._emit("stderr", (res.stderr or "")[-4000:])
        if res.timed_out:
            _tout = ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_INSTALL_FAILED, before_version=before,
                after_version="", state="install_failed", error_code="timeout",
                error_detail="codex update timed out; possible partial install")
            return attach_timed_out(_tout, True)
        if res.exit_code != 0:
            after, _raw = self._version_probe()
            _fail = ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_INSTALL_FAILED, before_version=before,
                after_version=after, state="install_failed", error_code="install_failed",
                error_detail="codex update exit=%d" % res.exit_code)
            return attach_timed_out(_fail, False)
        after, _raw = self._version_probe()
        combined = (res.stdout + "\n" + res.stderr).lower()
        already = ("already" in combined and ("latest" in combined or "up to date" in combined
                                              or "up-to-date" in combined)) or (after == before and before != "")
        # Restart ONLY a confirmed healthy/stale daemon after activity checks,
        # if required for the new CLI. Never start an absent daemon. A
        # failed-probe daemon is NOT eligible for automatic restart: report
        # failed_probe, block the restart, and require manual repair (verify()
        # will fail until the daemon is repaired).
        if daemon_before == "failed-probe":
            self._emit("stdout", "failed_probe daemon NOT eligible for automatic restart; "
                                 "manual repair required before daemon restart (%s)" % _daemon_detail[:300])
        elif daemon_before in ("healthy", "stale"):
            help_text = self._daemon_help()
            help_combined = help_text.stdout + "\n" + help_text.stderr
            if help_text.ok() and "restart" in help_combined:
                restart_res = run_fixed(
                    [CODEX_BIN, "app-server", "daemon", "restart"], timeout=300)
                self._emit("stdout", "$ codex app-server daemon restart -> exit=%d" % restart_res.exit_code)
                if restart_res.exit_code != 0:
                    _rstart = ExecuteResult(
                        tool=self.tool_id, exit_code=EXIT_INSTALL_FAILED, before_version=before,
                        after_version=after, state="install_failed", error_code="install_failed",
                        error_detail="daemon restart failed after update: exit=%d" % restart_res.exit_code)
                    return attach_timed_out(_rstart, False)
        if already:
            _already = ExecuteResult(
                tool=self.tool_id, exit_code=EXIT_OK, before_version=before,
                after_version=after, state="already_current", error_code="",
                error_detail="native updater reports already-current; not upgrade evidence")
            return attach_timed_out(_already, False)
        _done = ExecuteResult(
            tool=self.tool_id, exit_code=EXIT_OK, before_version=before,
            after_version=after, state="succeeded", error_code="",
            error_detail="")
        return attach_timed_out(_done, False)

    def verify(self):
        # type: () -> VerifyResult
        checks = []  # type: List[CheckItem]
        version, raw = self._version_probe()
        checks.append(CheckItem(
            name="cli_version", result="pass" if version else "fail", mandatory=True,
            summary=("version %s" % version) if version else "version unreadable: %s" % raw[:300]))
        startup = run_fixed([CODEX_BIN, "--help"], timeout=60)
        checks.append(CheckItem(
            name="cli_startup", result="pass" if startup.ok() else "fail", mandatory=True,
            summary="cli startup exit=%d" % startup.exit_code))
        state, daemon_version, detail = self._daemon_status()
        if state == "unknown-interface":
            checks.append(CheckItem(
                name="daemon_readiness", result="not_applicable", mandatory=False,
                summary="daemon interface unconfirmed; %s" % detail[:300]))
            checks.append(CheckItem(
                name="daemon_version_agreement", result="not_applicable", mandatory=False,
                summary="no daemon interface to compare"))
        elif state == "absent":
            checks.append(CheckItem(
                name="daemon_readiness", result="not_applicable", mandatory=False,
                summary="no daemon was present; absent daemon never started"))
            checks.append(CheckItem(
                name="daemon_version_agreement", result="not_applicable", mandatory=False,
                summary="no daemon to compare"))
        elif state == "failed-probe":
            checks.append(CheckItem(
                name="daemon_readiness", result="fail", mandatory=True,
                summary="daemon probe failed (not absence): %s" % detail[:300]))
            checks.append(CheckItem(
                name="daemon_version_agreement", result="unknown", mandatory=True,
                summary="daemon version unproven after failed probe"))
        elif state == "stale":
            checks.append(CheckItem(
                name="daemon_readiness", result="fail", mandatory=True,
                summary="daemon stale after update: %s" % detail[:300]))
            checks.append(CheckItem(
                name="daemon_version_agreement", result="fail", mandatory=True,
                summary="daemon %s disagrees with cli %s" % (daemon_version, version)))
        else:
            agree = bool(version and daemon_version and version == daemon_version)
            checks.append(CheckItem(
                name="daemon_readiness", result="pass", mandatory=True,
                summary="daemon healthy: %s" % detail[:300]))
            checks.append(CheckItem(
                name="daemon_version_agreement",
                result="pass" if agree else ("unknown" if not daemon_version or not version else "fail"),
                mandatory=True,
                summary="cli %s vs daemon %s" % (version or "?", daemon_version or "?")))
        releases = self._releases()
        checks.append(CheckItem(
            name="release_metadata_retained",
            result="pass" if releases else "fail", mandatory=True,
            summary="%d standalone releases retained (metadata only; never rollback capability, no automatic restore)" % len(releases)))
        checks.append(self._t3_integration_check())
        mandatory_failed = [c for c in checks if c.mandatory and c.result == "fail"]
        mandatory_unknown = [c for c in checks if c.mandatory and c.result == "unknown"]
        passed = (not mandatory_failed) and (not mandatory_unknown) and bool(version)
        error_code = "" if passed else "health_failed"
        return VerifyResult(
            tool=self.tool_id, version=version, checks=checks, passed=passed,
            error_code=error_code,
            error_detail="" if passed else "mandatory codex checks failed")

    def _t3_integration_check(self):
        # type: () -> CheckItem
        """Report T3 integration coverage gap; never claim provider proof.

        There is no real non-generating provider probe in this adapter, so a
        generic HTTP 200 and any t3-substring/body match are NEVER treated as
        integration evidence. A reachable endpoint is at most unknown with an
        explicit coverage gap; release acceptance requires the documented
        manual check.
        """
        endpoint = os.environ.get("EGA_T3_ENDPOINT", "").strip()
        if not endpoint:
            return CheckItem(
                name="t3_integration", result="not_applicable", mandatory=False,
                summary="no configured T3 integration probe; coverage gap explicit, manual integration check required for release acceptance")
        try:
            import urllib.request

            request = urllib.request.Request(endpoint, method="GET")
            with urllib.request.urlopen(request, timeout=10) as response:
                status = getattr(response, "status", 200)
                response.read(65536)
        except NameError:
            return CheckItem(
                name="t3_integration", result="unknown", mandatory=False,
                summary="integration probe unavailable; coverage gap explicit, manual check required")
        except Exception as exc:
            return CheckItem(
                name="t3_integration", result="fail", mandatory=True,
                summary="configured T3 endpoint unreachable: %s" % str(exc)[:300])
        # Reachability alone is never provider proof: report the gap.
        return CheckItem(
            name="t3_integration", result="unknown", mandatory=True,
            summary="T3 endpoint reachable (http %s) but no non-generating provider probe exists; coverage gap explicit, manual integration check required" % status)
