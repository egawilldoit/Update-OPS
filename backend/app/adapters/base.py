"""Adapter interface (SPEC §5). Frozen — concrete adapters implement, not alter.

Each fixed adapter implements inspect/discover/activity/plan/backup/execute/
verify. Browser requests never supply executable paths, env vars, service
names, URLs, or argv. Subprocesses use fixed argument arrays, shell=False.
Python 3.10 compatible.
"""
from __future__ import annotations

import abc
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class InspectResult(BaseModel):
    tool: str
    install_identity: str = ""
    executable: str = ""
    resolved_target: str = ""
    version: str = ""
    commit: str = ""
    install_kind: str = ""
    owner: str = ""
    source_clean: str = "unknown"  # clean|dirty|unknown
    source_detail: str = ""
    services: List[str] = Field(default_factory=list)
    state_dirs: List[str] = Field(default_factory=list)
    channel: str = ""
    fingerprint: str = ""


class DiscoverResult(BaseModel):
    tool: str
    target: str = ""
    target_mode: str = "exact"  # exact|native_latest|unknown
    channel: str = ""
    available: bool = False
    unknown_reason: str = ""


class ActivityResult(BaseModel):
    tool: str
    state: str = "unknown"  # idle|busy|unknown
    evidence: str = ""
    checked_at: str = ""


class PlanResult(BaseModel):
    tool: str
    target: str = ""
    target_mode: str = "exact"
    channel: str = ""
    fingerprint: str = ""
    services: List[str] = Field(default_factory=list)
    backup_scope: Dict[str, str] = Field(default_factory=dict)
    required_space_bytes: int = 0
    steps: List[str] = Field(default_factory=list)
    timeouts: Dict[str, int] = Field(default_factory=dict)
    restart_impact: str = ""
    already_current: bool = False


class BackupResult(BaseModel):
    tool: str
    supported: bool = False
    path: str = ""
    scope: Dict[str, str] = Field(default_factory=dict)
    consistency: str = ""
    size_bytes: int = 0
    unsupported_reason: str = ""


class ExecuteResult(BaseModel):
    tool: str
    exit_code: int = 0
    before_version: str = ""
    after_version: str = ""
    state: str = ""  # succeeded|already_current|install_failed|...
    error_code: str = ""
    error_detail: str = ""
    timed_out: bool = False


class CheckItem(BaseModel):
    name: str
    result: str = "unknown"  # pass|fail|unknown|not_applicable
    mandatory: bool = True
    summary: str = ""


class VerifyResult(BaseModel):
    tool: str
    version: str = ""
    checks: List[CheckItem] = Field(default_factory=list)
    passed: bool = False
    error_code: str = ""
    error_detail: str = ""


class Adapter(abc.ABC):
    tool_id: str = ""
    enabled: bool = True

    @abc.abstractmethod
    def inspect(self):
        # type: () -> InspectResult
        raise NotImplementedError

    @abc.abstractmethod
    def discover(self):
        # type: () -> DiscoverResult
        raise NotImplementedError

    @abc.abstractmethod
    def activity(self):
        # type: () -> ActivityResult
        raise NotImplementedError

    @abc.abstractmethod
    def plan(self):
        # type: () -> PlanResult
        raise NotImplementedError

    @abc.abstractmethod
    def backup(self, job_id):
        # type: (str) -> BackupResult
        raise NotImplementedError

    @abc.abstractmethod
    def execute(self, plan, job_id, activity_ack=False):
        # type: (PlanResult, str, bool) -> ExecuteResult
        raise NotImplementedError

    @abc.abstractmethod
    def verify(self):
        # type: () -> VerifyResult
        raise NotImplementedError


ADAPTER_TIMEOUT_DEFAULTS = {
    "preflight": 120,
    "backup": 600,
    "updating": 1800,
    "verifying": 300,
}
