"""Claude adapter: disabled read-only inventory (subagent B owned).

Disabled by default (enabled=False). Inspects version, executable ownership,
and supported diagnostics only. The audited BLOCKED_INSTALL_OWNERSHIP state
(root-owned /usr/local/lib/node_modules/@anthropic-ai/claude-code) blocks
before any mutation: no sudo, migration, uninstall, or elevation is ever
attempted. This adapter never blocks four-tool acceptance.

Python 3.10 compatible. Fixed argv arrays, shell=False everywhere.
"""
from __future__ import annotations

import os
from typing import List, Tuple

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
    extract_version_token,
    fingerprint,
    resolve_executable,
    run_fixed,
)
from ..schemas import utcnow_iso

CLAUDE_BIN = "/usr/local/bin/claude"
CLAUDE_MODULE_DIR = "/usr/local/lib/node_modules/@anthropic-ai/claude-code"

BLOCK_REASON = ("BLOCKED_INSTALL_OWNERSHIP: %s is root-owned; no sudo, "
                "migration, uninstall, or elevation is attempted" % CLAUDE_MODULE_DIR)


def _emit_noop(stream, line):
    # type: (str, str) -> None
    return None


class ClaudeAdapter(Adapter):
    tool_id = "claude"
    enabled = False

    def __init__(self):
        # type: () -> None
        self._emit = _emit_noop

    def _ownership(self):
        # type: () -> Tuple[str, str]
        """Return (owner, detail) for the module dir and binary."""
        try:
            st = os.stat(CLAUDE_MODULE_DIR)
            owner = "%d:%d" % (st.st_uid, st.st_gid)
        except OSError as exc:
            try:
                st = os.stat(CLAUDE_BIN)
                owner = "%d:%d" % (st.st_uid, st.st_gid)
            except OSError:
                return "unknown", "claude paths unreadable: %s" % exc
            return owner, "module dir missing; binary owner=%s" % owner
        return owner, "module dir owner=%s" % owner

    def inspect(self):
        # type: () -> InspectResult
        ok, resolved, detail = resolve_executable(CLAUDE_BIN)
        version = ""
        if ok:
            res = run_fixed([CLAUDE_BIN, "--version"], timeout=60)
            if res.ok():
                version = extract_version_token(res.stdout + "\n" + res.stderr)
        owner, ownership_detail = self._ownership()
        fp = fingerprint(CLAUDE_BIN, resolved, version, owner, "disabled")
        return InspectResult(
            tool=self.tool_id,
            install_identity="claude-disabled:%s" % (resolved or CLAUDE_BIN),
            executable=CLAUDE_BIN,
            resolved_target=resolved,
            version=version,
            commit="",
            install_kind="npm-root-owned",
            owner=owner,
            source_clean="unknown",
            source_detail="disabled adapter; %s; %s" % (ownership_detail, detail),
            services=[],
            state_dirs=[],
            channel="disabled",
            fingerprint=fp,
        )

    def discover(self):
        # type: () -> DiscoverResult
        return DiscoverResult(
            tool=self.tool_id, target="", target_mode="unknown",
            channel="disabled", available=False,
            unknown_reason="%s; adapter disabled" % BLOCK_REASON)

    def activity(self):
        # type: () -> ActivityResult
        return ActivityResult(
            tool=self.tool_id, state="unknown",
            evidence="disabled adapter; activity not tracked",
            checked_at=utcnow_iso())

    def plan(self):
        # type: () -> PlanResult
        inspection = self.inspect()
        return PlanResult(
            tool=self.tool_id, target="", target_mode="unknown",
            channel="disabled", fingerprint=inspection.fingerprint,
            services=[], backup_scope={}, required_space_bytes=0,
            steps=[], timeouts=dict(ADAPTER_TIMEOUT_DEFAULTS),
            restart_impact="disabled: %s" % BLOCK_REASON,
            already_current=False,
        )

    def backup(self, job_id):
        # type: (str) -> BackupResult
        return BackupResult(
            tool=self.tool_id, supported=False, path="", scope={},
            consistency="", size_bytes=0,
            unsupported_reason="backup_unsupported: %s" % BLOCK_REASON)

    def execute(self, plan, job_id):
        # type: (PlanResult, str) -> ExecuteResult
        current = self.inspect()
        return ExecuteResult(
            tool=self.tool_id, exit_code=3, before_version=current.version,
            after_version="", state="blocked", error_code="install_method_unsupported",
            error_detail=BLOCK_REASON)

    def verify(self):
        # type: () -> VerifyResult
        inspection = self.inspect()
        checks = [
            CheckItem(
                name="version_probe",
                result="pass" if inspection.version else "unknown",
                mandatory=False,
                summary=("version %s" % inspection.version) if inspection.version
                else "version unreadable (read-only probe)"),
            CheckItem(
                name="ownership_block",
                result="fail" if inspection.owner.startswith("0:") else "unknown",
                mandatory=False,
                summary=BLOCK_REASON if inspection.owner.startswith("0:")
                else "ownership %s; mutation still disabled" % inspection.owner),
        ]  # type: List[CheckItem]
        diag = run_fixed([CLAUDE_BIN, "--help"], timeout=60) \
            if resolve_executable(CLAUDE_BIN)[0] else None
        if diag is not None:
            checks.append(CheckItem(
                name="cli_help", result="pass" if diag.ok() else "unknown",
                mandatory=False, summary="cli help exit=%d" % diag.exit_code))
        return VerifyResult(
            tool=self.tool_id, version=inspection.version, checks=checks,
            passed=True, error_code="",
            error_detail="disabled read-only adapter; does not block four-tool acceptance")
