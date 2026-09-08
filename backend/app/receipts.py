"""Durable completion receipts v2 (R08). Python 3.10 compatible.

A receipt binds: schema version, job ID, tool ID, plan ID, normalized
plan hash, attempt nonce, immutable console release, target + mode,
expected mandatory-check manifest, installer exit code, installation
outcome, before/after version+commit, cleanup status, recovery
disposition, evidence durability, backup evidence, final health checks,
completion timestamp.

Success requires ALL of: installer exit 0, successful/already-current
outcome explicitly represented, known final version, exact-target
agreement for exact adapters, every expected mandatory check present
AND passing, evidence durable, cleanup resolved.

Binding is checked against the DB row AND the selecting filename
(check_binding). Application is atomic via tx.transition_tx; commit
errors propagate (never silently ignored).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

RECEIPT_SCHEMA_VERSION = 2

TERMINAL_RECEIPT_STATES = (
    "succeeded",
    "blocked",
    "failed",
    "health_failed",
    "interrupted",
)

CHECK_RESULTS = ("pass", "fail", "unknown", "not_applicable")

# Explicit installation-outcome vocabulary (F04). Only these values may
# accompany state == succeeded; anything else (including "", "none",
# "failed", "install_failed") contradicts success. This is the complete
# allowed set — do not accept arbitrary strings.
SUCCESS_OUTCOMES = ("succeeded", "already_current")

# Explicit recovery-disposition vocabulary for successful receipts
# (G06). Normal successful runner output uses exactly "none". Empty,
# unknown, pending, required, or garbage values are rejected — success
# must state its recovery disposition, not omit it. Non-success states
# keep their own explicit values (required, clear-pending-reconcile)
# and are NOT constrained to this set.
RECOVERY_RESOLVED_VALUES = ("none",)


def _known_secrets():
    # type: () -> tuple
    # G05: no silent degradation. A configured-but-broken secret source
    # raises SecretSourceError so receipt generation fails instead of
    # emitting raw external text.
    from .config import load_secret_values, settings

    values = load_secret_values(settings)
    return tuple(v for v in (values or ()) if v)


def _redact_str(value):
    # type: (object) -> object
    # N12: receipt generation fails rather than persisting raw values.
    if not isinstance(value, str) or not value:
        return value
    from .sanitize import sanitize_text
    return sanitize_text(value, _known_secrets())


def _strict_int(value, field):
    # type: (object, str) -> int
    """Strict integer parsing (N02): zero is valid, missing/null/malformed
    raise ValueError. Never `int(value or -1)` — that corrupts real zeros
    and masks absent fields. Success fails closed on malformed input."""
    if value is None:
        raise ValueError("%s missing" % field)
    if isinstance(value, bool):
        raise ValueError("%s must be an integer" % field)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("%s must be an integer" % field)
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("%s missing" % field)
        try:
            return int(text, 10)
        except ValueError:
            raise ValueError("%s malformed: %r" % (field, value[:50]))
    raise ValueError("%s has unsupported type" % field)


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
                  log_truncated=False, plan_id="", plan_hash="",
                  attempt_nonce="", release_path="", target="",
                  target_mode="exact", expected_checks=None,
                  installer_exit=0, install_outcome="",
                  actual_change=False, before_commit="",
                  after_commit="", cleanup_status="",
                  recovery_disposition="", evidence_durable=True,
                  backup_evidence=None, final_health=None):
    # type: (...) -> Dict[str, Any]
    """Build a fully-bound redacted receipt dict (every string sanitized).

    Exit values use strict parsing (F04): a malformed explicit
    exit_code/installer_exit raises ValueError instead of silently
    becoming zero.
    """
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
        code = _strict_int(exit_code, "exit_code")
    except ValueError as exc:
        raise ValueError("receipt build: %s" % exc)
    try:
        inst_code = _strict_int(installer_exit, "installer_exit")
    except ValueError as exc:
        raise ValueError("receipt build: %s" % exc)
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "job_id": _redact_str(str(job_id or "")),
        "tool_id": _redact_str(str(tool_id or "")),
        "tool": _redact_str(str(tool_id or "")),
        "plan_id": _redact_str(str(plan_id or "")),
        "plan_hash": _redact_str(str(plan_hash or "")),
        "attempt_nonce": _redact_str(str(attempt_nonce or "")),
        "release_path": _redact_str(str(release_path or "")),
        "target": _redact_str(str(target or "")),
        "target_mode": _redact_str(str(target_mode or "exact")),
        "state": _redact_str(str(state or "")),
        "before_version": _redact_str(str(before_version or "")),
        "before_commit": _redact_str(str(before_commit or "")),
        "after_version": _redact_str(str(after_version or "")),
        "after_commit": _redact_str(str(after_commit or "")),
        "exit_code": code,
        "installer_exit": inst_code,
        "install_outcome": _redact_str(str(install_outcome or "")),
        "actual_change": bool(actual_change),
        "error_code": _redact_str(str(error_code or "")),
        "error_detail": _redact_str(str(error_detail or ""))[:2000],
        "checks": norm_checks,
        "expected_checks": sorted({
            str(c) for c in (expected_checks or []) if str(c)}),
        "ts": _redact_str(str(ts or "")),
        "finished_at": _redact_str(str(ts or "")),
        "backup_summary": _redact_str(str(backup_summary or ""))[:2000],
        "backup_evidence": _redact_json(backup_evidence),
        "cleanup_status": _redact_str(str(cleanup_status or "")),
        "recovery_disposition": _redact_str(
            str(recovery_disposition or "")),
        "evidence_durable": bool(evidence_durable),
        "final_health": _redact_json(final_health),
        "log_truncated": bool(log_truncated),
    }


def _redact_json(obj):
    # type: (object) -> object
    if obj is None:
        return None
    try:
        from .sanitize import sanitize_json

        return sanitize_json(obj, _known_secrets())
    except Exception:
        return None


def validate_receipt(data):
    # type: (object) -> Tuple[bool, str]
    """Structural validation. Returns (ok, reason); '' when valid.

    Does NOT check DB binding (see check_binding) or success semantics
    beyond structural coherence.
    """
    if not isinstance(data, dict):
        return False, "receipt must be an object"
    if data.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        return False, "unsupported schema_version: %r" % (
            data.get("schema_version"),)
    for key in ("job_id", "tool_id", "plan_id", "plan_hash",
                "attempt_nonce", "release_path", "target"):
        val = data.get(key, "")
        if not isinstance(val, str) or not val:
            return False, "%s missing" % key
    if not data.get("tool_id") and not data.get("tool"):
        return False, "tool_id missing"
    state = data.get("state", "")
    if state not in TERMINAL_RECEIPT_STATES:
        return False, "invalid state: %r" % (state,)
    if data.get("target_mode", "") not in ("exact", "native_latest"):
        return False, "invalid target_mode"
    checks = data.get("checks", [])
    if not isinstance(checks, list):
        return False, "checks must be a list"
    for item in checks:
        if not isinstance(item, dict):
            return False, "check entry must be an object"
        if item.get("result", "") not in CHECK_RESULTS:
            return False, "check %r has invalid result" % (
                item.get("name", ""))
    expected = data.get("expected_checks", [])
    if not isinstance(expected, list):
        return False, "expected_checks must be a list"
    if state == "succeeded":
        # F04 contradiction-free success: EVERY success field must agree.
        # Any single contradiction fails the receipt (fail closed).
        if not isinstance(data.get("after_version", ""), str) or \
                not data.get("after_version", ""):
            return False, "after_version required for succeeded"
        try:
            if _strict_int(data.get("exit_code", "missing"),
                           "exit_code") != 0:
                return False, "succeeded requires exit_code == 0"
        except ValueError as exc:
            return False, "succeeded: %s" % exc
        try:
            if _strict_int(data.get("installer_exit"), "installer_exit") \
                    != 0:
                return False, "succeeded requires installer_exit == 0"
        except ValueError as exc:
            return False, "succeeded: %s" % exc
        if str(data.get("install_outcome", "") or "") not in \
                SUCCESS_OUTCOMES:
            return False, "succeeded requires install_outcome in %r; got %r" \
                % (list(SUCCESS_OUTCOMES),
                   str(data.get("install_outcome", ""))[:50])
        if not data.get("evidence_durable", False):
            return False, "succeeded requires evidence_durable"
        if str(data.get("cleanup_status", "") or "") != "resolved":
            return False, "succeeded requires cleanup_status == resolved"
        if str(data.get("recovery_disposition", "") or "") not in \
                RECOVERY_RESOLVED_VALUES:
            return False, "succeeded requires recovery_disposition in " \
                "%r; got %r" % (
                    list(RECOVERY_RESOLVED_VALUES),
                    str(data.get("recovery_disposition", ""))[:50])
        if data.get("target_mode") == "exact" and \
                data.get("after_version") != data.get("target"):
            return False, "exact target not observed"
        for name in expected:
            matches = [c for c in checks
                       if isinstance(c, dict)
                       and str(c.get("name", "")) == str(name)]
            if not matches:
                return False, "expected mandatory check missing: %s" % name
            for match in matches:
                # A receipt cannot weaken mandatory=true to false (F04):
                # expected checks must be mandatory AND passing.
                if not match.get("mandatory", False):
                    return False, \
                        "expected check not mandatory: %s" % name
                if match.get("result") != "pass":
                    return False, \
                        "expected mandatory check not passing: %s" % name
    ts_raw = data.get("ts", "") or data.get("finished_at", "")
    if _parse_ts(ts_raw) is None:
        return False, "ts unparseable or missing"
    return True, ""


def check_binding(data, job_row, plan_row=None, filename_job_id=""):
    # type: (Dict[str, Any], Dict[str, Any], object, str) -> Tuple[bool, str]
    """Verify receipt == filename == DB job == immutable plan (N03).

    Binds: filename job ID, receipt job ID, DB job ID, tool ID, plan ID,
    attempt nonce, plan hash, immutable release path, target, target
    mode, and the expected mandatory-check manifest. A receipt cannot
    weaken the plan (mandatory=true must stay true and passing for
    success). plan_row None (missing plan) fails closed.
    """
    try:
        rid = str(data.get("job_id", ""))
        if filename_job_id and rid != str(filename_job_id):
            return False, "receipt job does not match filename"
        if rid != str(job_row.get("id", "")):
            return False, "receipt job does not match DB row"
        if str(data.get("tool_id", "") or data.get("tool", "")) != \
                str(job_row.get("tool_id", "")):
            return False, "receipt tool does not match DB row"
        if str(data.get("plan_id", "")) != str(job_row.get("plan_id", "")):
            return False, "receipt plan does not match DB row"
        stored_nonce = str(job_row.get("dispatch_nonce", "") or "")
        if not stored_nonce or \
                str(data.get("attempt_nonce", "")) != stored_nonce:
            return False, "receipt attempt does not match DB row"
        if not isinstance(plan_row, dict) or not plan_row:
            return False, "immutable plan row unavailable"
        if str(data.get("plan_hash", "")) != \
                str(plan_row.get("plan_hash", "") or "") or \
                not plan_row.get("plan_hash"):
            return False, "receipt plan hash does not match plan row"
        if str(data.get("release_path", "")) != \
                str(plan_row.get("release_path", "") or "") or \
                not plan_row.get("release_path"):
            return False, "receipt release does not match plan row"
        if str(data.get("target", "")) != \
                str(plan_row.get("target", "") or ""):
            return False, "receipt target does not match plan row"
        if str(data.get("target_mode", "")) != \
                str(plan_row.get("target_mode", "") or ""):
            return False, "receipt target mode does not match plan row"
        try:
            import json as _json
            plan_required = list(_json.loads(
                plan_row.get("required_checks_json", "[]") or "[]"))
        except Exception:
            return False, "plan check manifest unreadable"
        receipt_expected = data.get("expected_checks", [])
        if not isinstance(receipt_expected, list):
            return False, "receipt check manifest malformed"
        for name in plan_required:
            if str(name) not in [str(x) for x in receipt_expected]:
                return False, \
                    "receipt drops plan-required check: %s" % str(name)[:100]
        if str(data.get("state", "")) == "succeeded":
            by_name = {}
            for item in data.get("checks", []) or []:
                if isinstance(item, dict) and item.get("name"):
                    by_name[str(item.get("name"))] = item
            for name in plan_required:
                match = by_name.get(str(name))
                if match is None:
                    return False, \
                        "plan-required check missing: %s" % str(name)[:100]
                if not match.get("mandatory", False):
                    return False, \
                        "plan-required check weakened to non-mandatory: %s" \
                        % str(name)[:100]
                if match.get("result") != "pass":
                    return False, \
                        "plan-required check not passing: %s" % str(name)[:100]
        if str(data.get("state", "")) == "succeeded":
            if str(data.get("cleanup_status", "") or "") != "resolved":
                return False, "cleanup not resolved"
            if not data.get("evidence_durable", False):
                return False, "evidence not durable"
            try:
                if _strict_int(data.get("exit_code", "missing"),
                               "exit_code") != 0:
                    return False, "succeeded requires exit_code == 0"
            except ValueError as exc:
                return False, "succeeded: %s" % exc
            if str(data.get("install_outcome", "") or "") not in \
                    SUCCESS_OUTCOMES:
                return False, "succeeded requires install_outcome in %r" \
                    % (list(SUCCESS_OUTCOMES),)
            if str(data.get("recovery_disposition", "") or "") not in \
                    RECOVERY_RESOLVED_VALUES:
                return False, "succeeded requires recovery_disposition " \
                    "in %r" % (list(RECOVERY_RESOLVED_VALUES),)
    except Exception as exc:
        return False, "binding check crashed: %s" % exc
    return True, ""


def load_receipt_file(path):
    # type: (str) -> Tuple[bool, object, str]
    """Load + structurally validate a receipt file.

    Returns (ok, data-or-{}, reason). Never raises, never invents state.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            import json as _json
            data = _json.load(fh)
    except Exception as exc:
        return False, {}, "receipt unreadable: %s" % exc
    ok, reason = validate_receipt(data)
    if not ok:
        return False, {}, reason
    return True, data, ""


def shows_mutation(data):
    # type: (object) -> bool
    """True when the receipt indicates mutation may have begun."""
    if not isinstance(data, dict):
        return False
    try:
        if str(data.get("after_version") or ""):
            return True
        if str(data.get("install_outcome") or "") not in ("", "none"):
            return True
        checks = data.get("checks", [])
        if isinstance(checks, list) and len(checks) > 0:
            return True
        if str(data.get("backup_summary") or ""):
            return True
        if str(data.get("state") or "") in (
                "backup", "updating", "verifying", "failed",
                "interrupted", "health_failed", "succeeded"):
            return True
    except Exception:
        return False
    return False


def apply_receipt(conn, data, filename_job_id=""):
    # type: (sqlite3.Connection, Dict[str, Any], str) -> str
    """Atomically apply a bound, valid receipt. Returns state.

    Raises ValueError on invalid/unbound receipts. Idempotent: re-applying
    the same receipt writes nothing new. Commit errors propagate as
    tx.TxCommitError (callers must NOT treat the gate as released).
    """
    from .tx import transition_tx, TxError

    ok, reason = validate_receipt(data)
    if not ok:
        raise ValueError("invalid receipt: %s" % reason)
    job_id = str(data.get("job_id", ""))
    row = conn.execute("SELECT * FROM jobs WHERE id=?",
                       (job_id,)).fetchone()
    if row is None:
        raise ValueError("unknown job: %s" % job_id)
    job = dict(row)
    try:
        plan = conn.execute("SELECT * FROM plans WHERE id=?",
                            (job.get("plan_id", ""),)).fetchone()
        plan_row = dict(plan) if plan is not None else None
    except Exception:
        plan_row = None
    bound, why = check_binding(data, job, plan_row, filename_job_id or job_id)
    if not bound:
        raise ValueError("unbound receipt: %s" % why)
    state = str(data.get("state", ""))
    # Terminal resolved rows are history: only an unresolved row (or the
    # same state) may be rewritten by a receipt. A resolved terminal row
    # with a DIFFERENT proven outcome is a contradiction for manual
    # review, never a silent overwrite.
    try:
        resolved_terminal = bool(job.get("finished_at")) and \
            str(job.get("state", "")) in TERMINAL_RECEIPT_STATES and \
            not int(job.get("unresolved", 0) or 0)
    except Exception:
        resolved_terminal = False
    if resolved_terminal and str(job.get("state", "")) != state:
        raise ValueError(
            "receipt contradicts resolved terminal state %s with %s"
            % (job.get("state", ""), state))
    if job.get("state") == state and job.get("finished_at"):
        # Already terminal in the same state: ensure checks present, then
        # return without duplicating history.
        existing = conn.execute(
            "SELECT name FROM checks WHERE job_id=?", (job_id,)).fetchall()
        existing_names = {str(r["name"]) for r in existing}
        receipt_names = {str(c.get("name", "")) for c in
                         (data.get("checks", []) or [])
                         if isinstance(c, dict)}
        if existing_names >= receipt_names:
            return state
    try:
        exit_code = _strict_int(data.get("exit_code", 0), "exit_code")
    except ValueError as exc:
        raise ValueError("invalid receipt: %s" % exc)
    try:
        installer_exit = _strict_int(
            data.get("installer_exit", 0), "installer_exit")
    except ValueError as exc:
        raise ValueError("invalid receipt: %s" % exc)
    update = {
        "after_version": str(data.get("after_version", "") or ""),
        "exit_code": exit_code,
        "installer_exit": installer_exit,
        "install_outcome": str(data.get("install_outcome", "") or "")[:200],
        "actual_change": 1 if data.get("actual_change") else 0,
        "error_code": str(data.get("error_code", "") or "")[:200],
        "error_detail": str(data.get("error_detail", "") or "")[:2000],
        "finished_at": str(data.get("ts", "") or
                           data.get("finished_at", "")),
        "recovery_required": 1 if str(
            data.get("recovery_disposition", "")) == "required" else
        int(job.get("recovery_required", 0) or 0),
    }
    checks = []
    for item in data.get("checks", []) or []:
        if isinstance(item, dict):
            checks.append(item)
    try:
        new_row = transition_tx(
            conn, job_id, state, step=state, update=update,
            event="receipt_applied",
            event_detail=str(data.get("ts", ""))[:200],
            checks=checks,
            tool_id=str(data.get("tool_id", "")))
    except TxError as exc:
        raise ValueError("receipt apply failed: %s" % exc)
    return str(new_row.get("state", state))
