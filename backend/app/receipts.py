"""Durable completion receipts: build / validate / apply.

A receipt is the recovery source of truth. The runner writes one atomically
via build_receipt (every string value redacted first); the dispatcher applies
valid on-disk receipts via apply_receipt instead of re-running work.

Python 3.10 compatible. No execution, no subprocesses.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

RECEIPT_SCHEMA_VERSION = 1

TERMINAL_RECEIPT_STATES = (
    "succeeded",
    "blocked",
    "failed",
    "health_failed",
    "interrupted",
)

CHECK_RESULTS = ("pass", "fail", "unknown", "not_applicable")


def _known_secrets():
    # type: () -> tuple
    try:
        from .config import load_secret_values, settings

        values = load_secret_values(settings)
        return tuple(v for v in (values or ()) if v)
    except Exception:
        return ()


def _redact_str(value):
    # type: (object) -> object
    if not isinstance(value, str) or not value:
        return value
    try:
        from .redaction import redact_text

        return redact_text(value, _known_secrets())
    except Exception:
        return value


def _parse_ts(raw):
    # type: (object) -> Any
    """Parse an ISO-8601 timestamp (accepting trailing Z). None when bad."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def build_receipt(job_id, tool_id, state, before_version, after_version,
                  exit_code, error_code, checks, ts,
                  error_detail="", backup_summary="",
                  log_truncated=False):
    # type: (...) -> Dict[str, Any]
    """Build a redacted receipt dict. Every string value is redacted first."""
    norm_checks = []  # type: List[Dict[str, Any]]
    for item in checks or []:
        if not isinstance(item, dict):
            continue
        norm_checks.append({
            "name": _redact_str(str(item.get("name", ""))),
            "result": str(item.get("result", "unknown")),
            "mandatory": bool(item.get("mandatory", True)),
            "summary": _redact_str(str(item.get("summary", "")))[:1000],
        })
    try:
        code = int(exit_code)
    except (TypeError, ValueError):
        code = 0
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "job_id": _redact_str(str(job_id or "")),
        "tool_id": _redact_str(str(tool_id or "")),
        # Read-compat alias of tool_id (runner historically wrote "tool").
        "tool": _redact_str(str(tool_id or "")),
        "state": _redact_str(str(state or "")),
        "before_version": _redact_str(str(before_version or "")),
        "after_version": _redact_str(str(after_version or "")),
        "exit_code": code,
        "error_code": _redact_str(str(error_code or "")),
        "error_detail": _redact_str(str(error_detail or ""))[:2000],
        "checks": norm_checks,
        "ts": _redact_str(str(ts or "")),
        # Read-compat alias of ts.
        "finished_at": _redact_str(str(ts or "")),
        "backup_summary": _redact_str(str(backup_summary or "")),
        "log_truncated": bool(log_truncated),
    }
    return receipt


def validate_receipt(data):
    # type: (object) -> Tuple[bool, str]
    """Return (ok, reason); reason is '' when valid."""
    if not isinstance(data, dict):
        return False, "receipt must be an object"
    if data.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        return False, "unsupported schema_version: %r" % (data.get("schema_version"),)
    job_id = data.get("job_id", "")
    if not isinstance(job_id, str) or not job_id:
        return False, "job_id missing"
    tool_id = data.get("tool_id", "") or data.get("tool", "")
    if not isinstance(tool_id, str) or not tool_id:
        return False, "tool_id missing"
    state = data.get("state", "")
    if state not in TERMINAL_RECEIPT_STATES:
        return False, "invalid state: %r" % (state,)
    if state == "succeeded":
        after = data.get("after_version", "")
        if not isinstance(after, str) or not after:
            return False, "after_version required for succeeded"
    checks = data.get("checks", [])
    if not isinstance(checks, list):
        return False, "checks must be a list"
    for item in checks:
        if not isinstance(item, dict):
            return False, "check entry must be an object"
        mandatory = item.get("mandatory", True)
        if mandatory:
            result = item.get("result", "")
            if result not in CHECK_RESULTS:
                return False, "mandatory check %r has invalid result %r" % (
                    item.get("name", ""), result)
    ts_raw = data.get("ts", "") or data.get("finished_at", "")
    if _parse_ts(ts_raw) is None:
        return False, "ts unparseable or missing"
    return True, ""


def _receipt_tool_id(data):
    # type: (Dict[str, Any]) -> str
    tool_id = data.get("tool_id", "") or data.get("tool", "")
    return str(tool_id or "")


def _receipt_ts(data):
    # type: (Dict[str, Any]) -> str
    ts = data.get("ts", "") or data.get("finished_at", "")
    return str(ts or "")


def apply_receipt(conn, data):
    # type: (sqlite3.Connection, Dict[str, Any]) -> str
    """Idempotently apply a valid receipt: terminal state + checks + events.

    Returns the receipt state. Raises ValueError on invalid receipts or
    unknown jobs. Re-applying the same receipt writes nothing new.
    """
    ok, reason = validate_receipt(data)
    if not ok:
        raise ValueError("invalid receipt: %s" % reason)
    job_id = str(data.get("job_id", ""))
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise ValueError("unknown job: %s" % job_id)
    state = str(data.get("state", ""))
    tool_id = _receipt_tool_id(data)
    after_version = str(data.get("after_version", "") or "")
    try:
        exit_code = int(data.get("exit_code", 0))
    except (TypeError, ValueError):
        exit_code = 0
    error_code = str(data.get("error_code", "") or "")
    error_detail = str(data.get("error_detail", "") or "")[:2000]
    ts = _receipt_ts(data)
    checks = data.get("checks", []) or []

    existing_event = conn.execute(
        "SELECT seq FROM events WHERE job_id=? AND event_type='receipt_applied'"
        " AND detail=? LIMIT 1", (job_id, ts)).fetchone()
    job = dict(row)
    already_terminal_same = (
        job.get("state") == state and (job.get("finished_at") or "") == ts
    )
    if already_terminal_same and existing_event is not None:
        existing = conn.execute(
            "SELECT name FROM checks WHERE job_id=?", (job_id,)).fetchall()
        existing_names = {str(r["name"]) for r in existing}
        receipt_names = {str(c.get("name", "")) for c in checks
                         if isinstance(c, dict)}
        if existing_names >= receipt_names:
            return state

    conn.execute(
        "UPDATE jobs SET state=?, step=?, after_version=?, exit_code=?,"
        " error_code=?, error_detail=?, finished_at=? WHERE id=?",
        (state, state, after_version, exit_code, error_code,
         error_detail, ts, job_id))
    existing = conn.execute(
        "SELECT name FROM checks WHERE job_id=?", (job_id,)).fetchall()
    existing_names = {str(r["name"]) for r in existing}
    try:
        from .schemas import utcnow_iso

        now = utcnow_iso()
    except Exception:
        now = ts
    for item in checks:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        if name in existing_names:
            continue
        result = str(item.get("result", "unknown"))
        mandatory = 1 if item.get("mandatory", True) else 0
        summary = str(item.get("summary", ""))[:1000]
        conn.execute(
            "INSERT INTO checks(tool_id,job_id,name,result,mandatory,summary,"
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (tool_id, job_id, name, result, mandatory, summary, now))
        existing_names.add(name)
    if existing_event is None:
        conn.execute(
            "INSERT INTO events(job_id,created_at,event_type,detail)"
            " VALUES(?,?,?,?)", (job_id, now, "receipt_applied", ts))
    try:
        conn.commit()
    except Exception:
        pass
    return state
