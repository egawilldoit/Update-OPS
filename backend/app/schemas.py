"""API + adapter data contracts (Pydantic 2). Frozen; subagents import, not edit.

Python 3.10 compatible.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field

ToolId = Literal["hermes", "opencode", "codex", "t3", "claude"]
JobState = Literal[
    "accepted", "preflight", "backup", "updating", "verifying",
    "succeeded", "blocked", "failed", "health_failed", "interrupted",
]
CheckResult = Literal["pass", "fail", "unknown", "not_applicable"]
ActivityState = Literal["idle", "busy", "unknown"]


def utcnow_iso():
    # type: () -> str
    return datetime.now(timezone.utc).isoformat()


class ErrorBody(BaseModel):
    code: str
    message: str
    details: str = ""
    request_id: str = ""


class ToolCard(BaseModel):
    id: ToolId
    installed_version: str = ""
    available_target: str = ""
    channel: str = ""
    last_success: str = ""
    last_attempt: str = ""
    health: Literal["healthy", "degraded", "unhealthy", "unknown", "stale"] = "unknown"
    health_detail: str = ""
    checked_at: str = ""
    discovery_error: str = ""
    install_identity: str = ""


class PlanRequest(BaseModel):
    pass


class PlanView(BaseModel):
    id: str
    tool_id: ToolId
    target: str
    target_mode: Literal["exact", "native_latest"] = "exact"
    channel: str = ""
    fingerprint: str = ""
    services: List[str] = Field(default_factory=list)
    backup_scope: Dict[str, str] = Field(default_factory=dict)
    activity_state: ActivityState = "unknown"
    activity_evidence: str = ""
    required_space_bytes: int = 0
    steps: List[str] = Field(default_factory=list)
    expires_at: str = ""
    restart_impact: str = ""


class JobCreate(BaseModel):
    plan_id: str
    activity_ack: bool = False


class JobView(BaseModel):
    id: str
    tool_id: ToolId
    plan_id: str
    state: JobState
    step: str = ""
    before_version: str = ""
    after_version: str = ""
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    exit_code: int = 0
    error_code: str = ""
    error_detail: str = ""
    runner_unit: str = ""
    recovery_required: bool = False
    backup_summary: str = ""


class CheckView(BaseModel):
    name: str
    result: CheckResult
    mandatory: bool = True
    summary: str = ""
    created_at: str = ""


class JobDetail(JobView):
    checks: List[CheckView] = Field(default_factory=list)


class LogRecord(BaseModel):
    seq: int
    ts: str
    stream: Literal["stdout", "stderr", "event"] = "stdout"
    line: str = ""


class LogPage(BaseModel):
    records: List[LogRecord] = Field(default_factory=list)
    next_after: int = 0
    truncated: bool = False


class HistoryPage(BaseModel):
    jobs: List[JobView] = Field(default_factory=list)
    next_cursor: str = ""


class SessionView(BaseModel):
    email: str
    csrf_token: str


class HealthView(BaseModel):
    api: Literal["ok", "degraded", "down"] = "ok"
    database: Literal["ok", "down"] = "ok"
    worker: Literal["ok", "stale", "down"] = "down"
    recovery_required: bool = False
    checked_at: str = ""
