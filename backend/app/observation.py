"""Durable tool-observation application owned by the probe coordinator (W3).

HTTP request lifetime != probe lifetime. The dispatcher stores the typed
result in probe_results and then applies the tool observation (for ops
that carry one, `refresh`). The waiting HTTP route is a reader only.

Exactly-once contract:
- applied timestamps are DERIVED from the durable
  probe_results.finished_at (never from API wall clock);
- every apply runs a strict monotonic guard against the timestamps
  already stored on the tools row, so replay after an API/worker restart
  cannot apply the same result twice or bump observation state twice;
- probe_requests.result_id durably marks the finished_at token that has
  been processed, so reconciliation scans only genuinely unapplied
  results and a transient failure is retried without re-applying.

Independent facts (never collapsed into one status):
- tool observation   -> install_identity/observed_version/health/
                        observation_time/last_success_at (last good)
- latest attempt     -> last_attempt_at/last_attempt_error/discovery_error
- probe execution    -> probe_requests/probe_results
- stop proof         -> execution_leases (leases.py; TTL is never proof)

Python 3.10 compatible. Short explicit transactions only.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Optional


def _probe_field(payload, section, key, default=""):
    # type: (Dict[str, Any], str, str, object) -> Any
    """Read a field from an owner-probe payload section (dicts only)."""
    try:
        section_d = (payload or {}).get(section, {})
        if not isinstance(section_d, dict):
            return default
        value = section_d.get(key, default)
        return default if value is None else value
    except Exception:
        return default


def _health_from_verify(verify_result):
    # type: (Any) -> tuple
    """Map adapter verify() to (health, health_detail).

    pass -> healthy with detail; fail -> unhealthy (mandatory fail) or
    degraded (optional-only fail); unknown/no-evidence -> unknown.
    Never raises; detail is truncated safe text.
    """
    try:
        passed = bool(getattr(verify_result, "passed", False))
    except Exception:
        passed = False
    try:
        version = str(getattr(verify_result, "version", "") or "")[:200]
    except Exception:
        version = ""
    try:
        raw_checks = list(getattr(verify_result, "checks", []) or [])
    except Exception:
        raw_checks = []
    parts = []
    man_fail = False
    opt_fail = False
    man_unknown = False
    total_man = 0
    pass_man = 0
    for item in raw_checks:
        try:
            if isinstance(item, dict):
                name = str(item.get("name", "") or "")[:100]
                result = str(item.get("result", "unknown") or "unknown")
                mandatory = bool(item.get("mandatory", True))
            else:
                name = str(getattr(item, "name", "") or "")[:100]
                result = str(getattr(item, "result", "unknown")
                             or "unknown")
                mandatory = bool(getattr(item, "mandatory", True))
        except Exception:
            continue
        if result not in ("pass", "fail", "unknown", "not_applicable"):
            result = "unknown"
        parts.append("%s=%s" % (name or "check", result))
        if mandatory and result != "not_applicable":
            total_man += 1
            if result == "pass":
                pass_man += 1
        if result == "fail" and mandatory:
            man_fail = True
        elif result == "fail":
            opt_fail = True
        elif result == "unknown" and mandatory:
            man_unknown = True
    summary = "; ".join(parts)[:800]
    if version:
        prefix = "verify %s version=%s" % (
            "pass" if passed else "fail", version)
    else:
        prefix = "verify %s" % ("pass" if passed else "fail")
    if total_man:
        prefix = "%s (%d/%d mandatory pass)" % (prefix, pass_man, total_man)
    detail = ("%s: %s" % (prefix, summary)).strip(": ")[:1000]
    if passed:
        return "healthy", detail
    if man_fail:
        return "unhealthy", detail
    if opt_fail:
        return "degraded", detail
    if man_unknown:
        return "unknown", detail
    # No fail evidence but not passed (e.g. empty checks): unknown.
    return "unknown", detail


def _health_from_payload_verification(payload):
    # type: (Dict[str, Any]) -> tuple
    """Dict-shaped twin of _health_from_verify for owner-probe payloads."""

    class _Box(object):
        def __init__(self, data):
            # type: (Dict[str, Any]) -> None
            self._data = data if isinstance(data, dict) else {}

        def __getattr__(self, name):
            # type: (str) -> Any
            if name.startswith("_"):
                raise AttributeError(name)
            return self._data.get(name)

    boxes = []
    try:
        raw_checks = (payload or {}).get("checks", []) or []
    except Exception:
        raw_checks = []
    for item in raw_checks:
        boxes.append(_Box(item if isinstance(item, dict) else []))

    class _V(object):
        pass
    verification = _V()
    try:
        verification.passed = bool((payload or {}).get("passed", False))
        verification.version = str((payload or {}).get("version", "") or "")
        verification.error_detail = str(
            (payload or {}).get("error_detail", "") or "")
    except Exception:
        verification.passed = False
        verification.version = ""
        verification.error_detail = ""
    verification.checks = boxes
    return _health_from_verify(verification)


def refresh_observation(payload):
    # type: (Dict[str, Any]) -> Optional[Dict[str, Any]]
    """Parse a `refresh` probe payload into tools observation columns.

    Returns None when the payload is unusable (caller records an attempt
    and preserves the last good observation). Never raises.
    """
    try:
        p = payload if isinstance(payload, dict) else {}
        health, health_detail = _health_from_payload_verification(
            p.get("verification", {}))
        available = False
        try:
            available = bool(_probe_field(p, "discovery", "available", False))
        except Exception:
            available = False
        unknown_reason = ""
        if not available:
            unknown_reason = str(_probe_field(
                p, "discovery", "unknown_reason", "") or "")[:1000]
        channel = str(
            _probe_field(p, "inspection", "channel", "") or
            _probe_field(p, "discovery", "channel", "") or "")[:200]
        return {
            "install_identity": str(_probe_field(
                p, "inspection", "install_identity", "") or "")[:500],
            "observed_version": str(_probe_field(
                p, "inspection", "version", "") or "")[:200],
            "fingerprint": str(_probe_field(
                p, "inspection", "fingerprint", "") or "")[:200],
            "available_target": str(_probe_field(
                p, "discovery", "target", "") or "")[:200],
            "channel": channel,
            "discovery_error": unknown_reason,
            "health": str(health or "unknown")[:50],
            "health_detail": str(health_detail or "")[:1000],
        }
    except Exception:
        return None


def _parse_json(raw):
    # type: (object) -> Dict[str, Any]
    try:
        value = json.loads(raw) if raw else {}
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _mark_processed(conn, request_id, finished_at):
    # type: (sqlite3.Connection, str, str) -> bool
    """Record the durable applied marker for one result (own short tx)."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE probe_requests SET result_id=? WHERE id=?",
                     (finished_at, request_id))
        conn.execute("COMMIT")
        return True
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return False


def _apply_attempt(conn, request_id, tool_id, reason, finished_at):
    # type: (sqlite3.Connection, str, str, str, str) -> bool
    """Record the latest failed attempt WITHOUT touching last-good
    observation fields. Marked processed atomically."""
    reason = str(reason or "probe failed")[:500]
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO tools(id) VALUES(?)",
                     (tool_id,))
        cur = conn.execute(
            "UPDATE tools SET discovery_error=?, last_attempt_at=?,"
            " last_attempt_error=? WHERE id=?"
            " AND (last_attempt_at='' OR last_attempt_at<?)",
            (reason, finished_at, reason, tool_id, finished_at))
        changed = cur.rowcount == 1
        conn.execute("UPDATE probe_requests SET result_id=? WHERE id=?",
                     (finished_at, request_id))
        conn.execute("COMMIT")
        return changed
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return False


def _apply_observation(conn, request_id, tool_id, payload, finished_at):
    # type: (sqlite3.Connection, str, str, Dict[str, Any], str) -> bool
    data = refresh_observation(payload)
    if data is None or not (data["install_identity"]
                            or data["observed_version"]
                            or data["fingerprint"]):
        # Unusable observation can never erase the last good state.
        return _apply_attempt(
            conn, request_id, tool_id,
            "probe observation unusable; last observation preserved",
            finished_at)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO tools(id) VALUES(?)",
                     (tool_id,))
        cur = conn.execute(
            "UPDATE tools SET install_identity=?, observed_version=?,"
            " available_target=?, channel=?, fingerprint=?,"
            " observation_time=?, discovery_error=?, health=?,"
            " health_detail=?, updated_at=?, last_success_at=?,"
            " last_attempt_at=?, last_attempt_error='' WHERE id=?"
            " AND (observation_time='' OR observation_time<?)",
            (data["install_identity"], data["observed_version"],
             data["available_target"], data["channel"], data["fingerprint"],
             finished_at, data["discovery_error"], data["health"],
             data["health_detail"], finished_at, finished_at, finished_at,
             tool_id, finished_at))
        changed = cur.rowcount == 1
        conn.execute("UPDATE probe_requests SET result_id=? WHERE id=?",
                     (finished_at, request_id))
        conn.execute("COMMIT")
        return changed
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return False


def apply_probe_result(conn, request_id):
    # type: (sqlite3.Connection, str) -> bool
    """Apply one durably stored probe result to the tools observation.

    Coordinator-side only (dispatcher); idempotent by construction:
    returns True only when this call actually changed the tools row, and
    False on replay, stale results, deferred results, or non-refresh
    ops. The durable marker is set in the same transaction as the apply.
    """
    if not request_id:
        return False
    try:
        row = conn.execute(
            "SELECT pr.tool_id, pr.op, pr.result_id, res.status,"
            " res.result_json, res.finished_at"
            " FROM probe_results res JOIN probe_requests pr"
            " ON pr.id=res.request_id WHERE res.request_id=?",
            (request_id,)).fetchone()
    except Exception:
        return False
    if row is None:
        return False
    try:
        tool_id = str(row["tool_id"] or "")
        op = str(row["op"] or "")
        status = str(row["status"] or "")
        finished_at = str(row["finished_at"] or "")
        marked = str(row["result_id"] or "")
        raw = row["result_json"] or "{}"
    except Exception:
        return False
    if not tool_id or op != "refresh" or not finished_at:
        return False
    if marked == finished_at:
        return False
    payload = _parse_json(raw)
    if status == "ok":
        return _apply_observation(conn, request_id, tool_id, payload,
                                  finished_at)
    if status == "error":
        try:
            reason = str(payload.get("reason", "") or "probe failed")
        except Exception:
            reason = "probe failed"
        return _apply_attempt(conn, request_id, tool_id, reason,
                              finished_at)
    # deferred/unknown: nothing to apply but the result is processed.
    _mark_processed(conn, request_id, finished_at)
    return False


def reconcile_observations(conn, limit=200):
    # type: (sqlite3.Connection, int) -> int
    """Apply refresh results whose observation was never applied.

    Bounded scan of genuinely unapplied durable results (marker-based),
    oldest first so every pass makes forward progress. Returns the
    number of results actually applied.
    """
    try:
        bound = max(1, int(limit))
    except (TypeError, ValueError):
        bound = 200
    try:
        rows = conn.execute(
            "SELECT res.request_id FROM probe_results res JOIN probe_requests"
            " pr ON pr.id=res.request_id WHERE pr.op='refresh'"
            " AND pr.state='done'"
            " AND (pr.result_id='' OR pr.result_id<>res.finished_at)"
            " ORDER BY res.finished_at ASC LIMIT ?", (bound,)).fetchall()
    except Exception:
        return 0
    applied = 0
    for row in rows:
        try:
            if apply_probe_result(conn, str(row["request_id"])):
                applied += 1
        except Exception:
            continue
    return applied
