"""Authenticated /api/v1 routes (9 endpoints). Short transactions only.

- Every route requires Cloudflare Access JWT auth (deps.authenticate).
- Mutations (3x POST) additionally require JSON content-type + exact allowed
  Origin + X-CSRF-Token (deps.require_mutation_guards). CORS is disabled
  (no CORSMiddleware is ever added in main.py); never mutate via GET.
- All responses carry Cache-Control: no-store (errors via deps helpers,
  successes via _ok()).
- Errors use the {code,message,details,request_id} envelope; details never
  carry secrets (only safe identifiers such as tool/job/plan ids and
  truncated evidence strings).
- Parameterized SQL only (? placeholders). Write paths use a short
  BEGIN IMMEDIATE transaction and commit/rollback promptly; a transaction is
  never held across a subprocess call. POST /tools/{id}/check and POST
  /tools/{id}/plans run read-only adapter probes (inspect/discover/activity/
  plan) outside any DB transaction and persist via a short UPDATE/INSERT
  afterwards; they never install, download, or restart.
- POST /jobs delegates to the shared admission service
  (backend/app/admission.py: replay-first, gates, atomic reserve).
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import auth as auth_lib
from .. import jobs as jobs_lib
from .. import redaction as redaction_lib
from ..config import load_secret_values, settings
from ..db import connect
from ..schemas import utcnow_iso
from . import deps

router = APIRouter(prefix="/api/v1")

CANONICAL_ORDER = ("hermes", "opencode", "codex", "t3")
PLAN_STEPS_DEFAULT = ["preflight", "backup", "updating", "verifying"]
DISCOVERY_CACHE_S = 15 * 60
LOG_DEFAULT_LIMIT = 200
LOG_MAX_LIMIT = 1000
# 15-minute discovery coalesce: {tool_id: (snapshot_card, monotonic_ts)}.
# Snapshot is the last successfully persisted tool card; monotonic_ts comes
# from time.monotonic() so wall-clock jumps never extend the window.
_DISCOVERY_CACHE = {}  # type: Dict[str, Tuple[Dict[str, Any], float]]
# Per-tool locks coalesce concurrent duplicate checks with a short critical
# section (guard only cache/inflight bookkeeping, never adapter probes).
_DISCOVERY_LOCKS = {}  # type: Dict[str, threading.Lock]
_DISCOVERY_LOCKS_GUARD = threading.Lock()
_DISCOVERY_INFLIGHT = set()  # type: set


def _per_tool_lock(tool_id):
    # type: (str) -> threading.Lock
    with _DISCOVERY_LOCKS_GUARD:
        lock = _DISCOVERY_LOCKS.get(tool_id)
        if lock is None:
            lock = threading.Lock()
            _DISCOVERY_LOCKS[tool_id] = lock
        return lock


def _drained():
    # type: () -> bool
    """True when <state_dir>/drain exists (maintenance refuses new work).

    Reads only; never raises. Callers gate POST plans + POST jobs with
    503 {code: maintenance}; reads are unaffected.
    """
    try:
        base = settings.state_dir or "/var/lib/ega-update"
    except Exception:
        return False
    try:
        return os.path.exists(os.path.join(str(base), "drain"))
    except Exception:
        return False


def _health_from_verify(verify_result):
    # type: (Any) -> Tuple[str, str]
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
    # type: (Dict[str, Any]) -> Tuple[str, str]
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


# Explicit per-job cap marker emitted by worker/runner.py JobLog.
# Log readers match only this string (not a generic "truncat" substring) so
# per-line "…[truncated-line]" suffixes and user output containing "truncate"
# never falsely report per-job truncation.
LOG_TRUNCATION_MARKER = ("[truncated: per-job log cap reached; "
                         "draining without persisting]")


def _known_secrets():
    # type: () -> tuple
    """Known secrets for log re-redaction (G05: no silent degradation).

    Propagates SecretSourceError so the logs endpoint fails closed
    (503) instead of serving weakened re-redaction of external text.
    """
    return load_secret_values(settings)


def _safe_detail(text, limit=200):
    # type: (object, int) -> str
    """R11: exception-derived API detail is sanitized, never raw. A
    broken secret source yields a fixed safe literal (G05), never raw
    exception text and never a secondary crash inside error handling."""
    try:
        return sanitize_text(str(text or ""), _known_secrets())[:limit]
    except Exception:
        return "unavailable"


def _load_adapter(tool_id):
    # type: (str) -> Any
    """Lazily resolve the adapter for a tool id (no probes at import).

    Kept for owner-side (dispatcher/CLI/SSH) use. Request handlers must
    use the owner probe queue instead (R01): the API account cannot
    execute installation probes.
    """
    try:
        from backend.app.adapters import registry as adapter_registry
    except Exception:
        from ..adapters import registry as adapter_registry  # type: ignore[no-redef]
    return adapter_registry.get_adapter(tool_id)


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


async def _owner_probe(tool_id, op, timeout_s=25.0):
    # type: (str, str, float) -> Tuple[str, Dict[str, Any]]
    """Owner probe via the typed queue (R01/R16).

    Runs the blocking enqueue+wait in a worker thread so the event loop
    stays responsive during slow probes. Returns (status, payload) with
    status in ok|deferred|error|timeout.
    """
    try:
        from ..owner_probes import request_owner_probe
    except Exception:
        try:
            from backend.app.owner_probes import request_owner_probe  # type: ignore[no-redef]
        except Exception:
            return "error", {"reason": "probe boundary unavailable"}
    try:
        return await asyncio.to_thread(
            request_owner_probe, tool_id, op, timeout_s, "api")
    except Exception as exc:
        return "error", {"reason": "probe wait crashed: %s" % _safe_detail(exc, 200)}


def _ok(payload):
    # type: (Any) -> JSONResponse
    return JSONResponse(status_code=200, content=payload,
                        headers=dict(deps.NO_STORE))


def _created(payload):
    # type: (Any) -> JSONResponse
    return JSONResponse(status_code=201, content=payload,
                        headers=dict(deps.NO_STORE))


def _accepted(payload):
    # type: (Any) -> JSONResponse
    return JSONResponse(status_code=202, content=payload,
                        headers=dict(deps.NO_STORE))


def _db():
    # type: () -> sqlite3.Connection
    return connect(settings.db_path)


def _parse_iso(value):
    # type: (str) -> Optional[datetime]
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _authed(request, kind="read"):
    # type: (Request, str) -> Any
    """Common auth + kind-aware rate-limit gate ("read"|"write").

    Returns (claims, subject, rid) or an error JSONResponse (caller must
    `isinstance`-check). 429 carries Retry-After (R21)."""
    claims, err_resp, rid = deps.authenticate(request)
    if err_resp is not None:
        return err_resp
    subject = deps.subject_of(claims or {})
    bucket = kind if kind in ("read", "write") else "read"
    if not deps.check_rate_limit(subject or "anonymous", bucket):
        try:
            retry_s = deps.retry_after_s(subject or "anonymous", bucket)
        except Exception:
            retry_s = 60
        return deps.rate_limited_response(rid, retry_s)
    return claims, subject, rid


def _tool_response_row(tool_row, last_success, last_attempt):
    # type: (Any, str, str) -> Dict[str, Any]
    d = dict(tool_row) if tool_row is not None else {}
    return {
        "id": d.get("id", ""),
        "installed_version": d.get("observed_version", "") or "",
        "available_target": d.get("available_target", "") or "",
        "channel": d.get("channel", "") or "",
        "last_success": last_success or "",
        "last_attempt": last_attempt or "",
        "health": d.get("health", "") or "unknown",
        "health_detail": d.get("health_detail", "") or "",
        "checked_at": d.get("observation_time", "") or "",
        "discovery_error": d.get("discovery_error", "") or "",
        "install_identity": d.get("install_identity", "") or "",
        # R19: last successful observation vs last attempt are separate;
        # checked_at stays the last success so a failed check never
        # refreshes an old healthy timestamp.
        "last_success_at": d.get("last_success_at", "") or "",
        "attempted_at": d.get("last_attempt_at", "") or "",
        "attempt_error": d.get("last_attempt_error", "") or "",
    }


def _read_tool_card(conn, tool_id):
    # type: (sqlite3.Connection, str) -> Dict[str, Any]
    row = conn.execute("SELECT * FROM tools WHERE id=?",
                       (tool_id,)).fetchone()
    last_success = ""
    last_attempt = ""
    try:
        r = conn.execute(
            "SELECT finished_at FROM jobs WHERE tool_id=? AND state='succeeded'"
            " ORDER BY finished_at DESC LIMIT 1", (tool_id,)).fetchone()
        if r is not None:
            last_success = r["finished_at"] or ""
    except Exception:
        last_success = ""
    try:
        r = conn.execute(
            "SELECT created_at FROM jobs WHERE tool_id=? ORDER BY created_at DESC"
            " LIMIT 1", (tool_id,)).fetchone()
        if r is not None:
            last_attempt = r["created_at"] or ""
    except Exception:
        last_attempt = ""
    if row is None:
        return {
            "id": tool_id, "installed_version": "", "available_target": "",
            "channel": "", "last_success": last_success,
            "last_attempt": last_attempt, "health": "unknown",
            "health_detail": "", "checked_at": "", "discovery_error": "",
            "install_identity": "", "last_success_at": "",
            "attempted_at": "", "attempt_error": "",
        }
    return _tool_response_row(row, last_success, last_attempt)


def _job_view(row):
    # type: (Any) -> Dict[str, Any]
    d = dict(row)
    try:
        final_seq = int(d.get("final_log_seq", -1))
    except (TypeError, ValueError):
        final_seq = -1
    try:
        installer_exit = int(d.get("installer_exit", 0) or 0)
    except (TypeError, ValueError):
        installer_exit = 0
    return {
        "id": d.get("id", ""),
        "tool_id": d.get("tool_id", ""),
        "plan_id": d.get("plan_id", ""),
        "state": d.get("state", ""),
        "step": d.get("step", "") or "",
        "before_version": d.get("before_version", "") or "",
        "after_version": d.get("after_version", "") or "",
        "created_at": d.get("created_at", "") or "",
        "started_at": d.get("started_at", "") or "",
        "finished_at": d.get("finished_at", "") or "",
        "exit_code": int(d.get("exit_code", 0) or 0),
        "error_code": d.get("error_code", "") or "",
        "error_detail": d.get("error_detail", "") or "",
        "runner_unit": d.get("runner_unit", "") or "",
        "recovery_required": bool(d.get("recovery_required", 0)),
        "backup_summary": "",
        # R18 outcome separation (installer vs wrapper, actual change) and
        # R20 durable log cursor (-1 until the final flush).
        "installer_exit": installer_exit,
        "install_outcome": d.get("install_outcome", "") or "",
        "actual_change": bool(d.get("actual_change", 0)),
        "final_log_seq": final_seq,
    }


def _backup_summary(conn, job_id):
    # type: (sqlite3.Connection, str) -> str
    try:
        rows = conn.execute(
            "SELECT * FROM backups WHERE job_id=? ORDER BY completed_at",
            (job_id,)).fetchall()
    except Exception:
        return ""
    parts = []
    for r in rows:
        d = dict(r)
        scope = d.get("scope", "") or ""
        if len(scope) > 500:
            scope = scope[:500] + "…"
        parts.append("path=%s consistency=%s size_bytes=%s completed_at=%s"
                     " scope=%s" % (
                         d.get("path", ""), d.get("consistency", ""),
                         d.get("size_bytes", 0), d.get("completed_at", ""),
                         scope))
    return "; ".join(parts)


def _checks(conn, job_id):
    # type: (sqlite3.Connection, str) -> List[Dict[str, Any]]
    try:
        rows = conn.execute(
            "SELECT * FROM checks WHERE job_id=? ORDER BY id",
            (job_id,)).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        d = dict(r)
        try:
            mandatory = bool(int(d.get("mandatory", 1)))
        except Exception:
            mandatory = True
        out.append({
            "name": d.get("name", ""),
            "result": d.get("result", "unknown"),
            "mandatory": mandatory,
            "summary": d.get("summary", "") or "",
            "created_at": d.get("created_at", "") or "",
        })
    return out


def _log_path(job_id):
    # type: (str) -> str
    base = settings.log_dir or "/var/lib/ega-update/logs"
    # job_id is UUID-validated by callers, so no path traversal is possible.
    return os.path.join(base, job_id + ".jsonl")


def _read_log_page(job_id, after, limit):
    # type: (str, int, int) -> Dict[str, Any]
    # has_more = page continuation (more records beyond limit);
    # truncated = strictly storage-cap loss (per-job cap marker or file at
    # cap). Never conflate the two.
    path = _log_path(job_id)
    records = []  # type: List[Dict[str, Any]]
    truncated_marker = False
    secrets = _known_secrets()
    try:
        size = os.path.getsize(path)
        if size >= int(getattr(settings, "per_job_log_cap_bytes",
                               20 * 1024 * 1024)):
            truncated_marker = True
    except OSError:
        return {"records": [], "next_after": after, "truncated": False,
                "has_more": False}
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return {"records": [], "next_after": after, "truncated": False,
                "has_more": False}
    with fh:
        wanted_from = after
        collected = []  # type: List[Dict[str, Any]]
        has_more = False
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            try:
                seq = int(obj.get("seq", -1))
            except Exception:
                continue
            if seq < 0:
                continue
            line = str(obj.get("line", ""))
            # Render redacted text records only: strip terminal controls and
            # re-apply secret patterns plus known bare secret values
            # (defence in depth; writers already redact, but a crashed
            # writer must never leak via the reader).
            line = redaction_lib.strip_controls(line)
            line = redaction_lib.redact_text(line, secrets)
            if len(line) > redaction_lib.MAX_LINE:
                line = line[:redaction_lib.MAX_LINE] + "…[truncated-line]"
            stream = str(obj.get("stream", "stdout"))
            if stream not in ("stdout", "stderr", "event"):
                stream = "stdout"
            # Match only the explicit per-job cap marker emitted by the
            # runner; per-line "…[truncated-line]" suffixes or user output
            # containing "truncate" must not flag per-job truncation.
            if LOG_TRUNCATION_MARKER in line:
                truncated_marker = True
            if seq <= wanted_from:
                continue
            if len(collected) < limit:
                collected.append({
                    "seq": seq,
                    "ts": str(obj.get("ts", "")),
                    "stream": stream,
                    "line": line,
                })
            else:
                has_more = True
        next_after = collected[-1]["seq"] if collected else wanted_from
        return {"records": collected, "next_after": next_after,
                "truncated": bool(truncated_marker),
                "has_more": bool(has_more)}


# ---------------------------------------------------------------------------
# 1. GET /session — owner identity + CSRF token
# ---------------------------------------------------------------------------
@router.get("/session")
def get_session(request: Request):
    gated = _authed(request)
    if isinstance(gated, JSONResponse):
        return gated
    claims, subject, _rid = gated
    email = str((claims or {}).get("email", "") or subject or "")
    token = auth_lib.mint_csrf(subject or email, settings.csrf_secret)
    return _ok({"email": email, "csrf_token": token})


# ---------------------------------------------------------------------------
# 2. GET /tools — four cards, independent observations + freshness
# ---------------------------------------------------------------------------
@router.get("/tools")
def get_tools(request: Request):
    gated = _authed(request)
    if isinstance(gated, JSONResponse):
        return gated
    conn = _db()
    try:
        cards = [_read_tool_card(conn, tid) for tid in CANONICAL_ORDER]
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return _ok(cards)


# ---------------------------------------------------------------------------
# 3. POST /tools/{id}/check — bounded read-only refresh via real adapters
# ---------------------------------------------------------------------------
@router.post("/tools/{tool_id}/check")
async def post_tool_check(tool_id: str, request: Request):
    gated = _authed(request, kind="write")
    if isinstance(gated, JSONResponse):
        return gated
    claims, _subject, _rid = gated
    guard = deps.require_mutation_guards(request, claims or {})
    if guard is not None:
        return guard
    kind = deps.classify_tool_id(tool_id)
    if kind == "claude":
        return deps.error_envelope(
            410, "install_method_unsupported",
            "claude adapter is disabled (BLOCKED_INSTALL_OWNERSHIP)",
            "tool_id=claude")
    if kind == "unknown":
        return deps.error_envelope(
            404, "not_found", "unknown tool", "tool_id=%s" % tool_id[:32])

    def _labeled_cached(card, note):
        # type: (Dict[str, Any], str) -> Dict[str, Any]
        detail = (card.get("health_detail", "") or "")
        card["health_detail"] = ("%s | %s" % (detail, note)).strip(" |") \
            if detail else note
        if card.get("health") in ("healthy", "degraded", "unhealthy"):
            card["health"] = "stale"
        return card

    def _preserve_with_error(tool_id_inner, message):
        # type: (str, str) -> Dict[str, Any]
        # R19: on failure preserve the last observation AND its timestamp;
        # record only last_attempt_at + last_attempt_error (an old healthy
        # result must never look freshly healthy). Never invent health.
        now_inner = utcnow_iso()
        conn_inner = _db()
        try:
            try:
                conn_inner.execute("BEGIN IMMEDIATE")
                conn_inner.execute(
                    "INSERT OR IGNORE INTO tools(id) VALUES(?)",
                    (tool_id_inner,))
                conn_inner.execute(
                    "UPDATE tools SET discovery_error=?,"
                    " last_attempt_at=?, last_attempt_error=? WHERE id=?",
                    (str(message)[:500], now_inner, str(message)[:500],
                     tool_id_inner))
                conn_inner.commit()
            except Exception:
                try:
                    conn_inner.rollback()
                except Exception:
                    pass
        finally:
            try:
                conn_inner.close()
            except Exception:
                pass
        conn_out = _db()
        try:
            return _read_tool_card(conn_out, tool_id_inner)
        finally:
            try:
                conn_out.close()
            except Exception:
                pass

    # Validate ?force (cache bypass only; still respects the active-job
    # gate below). Only absent/"" , "0", "1" are accepted.
    try:
        _force_raw = request.query_params.get("force", "")
    except Exception:
        _force_raw = ""
    _force_raw = str(_force_raw or "").strip()
    if _force_raw in ("", "0"):
        _force = False
    elif _force_raw == "1":
        _force = True
    else:
        return deps.error_envelope(
            422, "invalid_request",
            "force must be 0 or 1", "")

    # Short read only: active-job / recovery gate + cached card. No
    # transaction is held across the adapter probes below.
    conn = _db()
    try:
        try:
            active = jobs_lib.active_job(conn)
        except Exception:
            active = None
        try:
            recovering = jobs_lib.recovery_blocked(conn)
        except Exception:
            recovering = False
        cached = _read_tool_card(conn, tool_id)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    # During any active mutation return the cached observation labeled
    # stale/updating and never launch a contending probe (SPEC section 10).
    # ?force still respects this gate: no probes while a job is active.
    if active is not None:
        avid = ""
        atool = ""
        try:
            avid = str(dict(active).get("id", "") or "")
            atool = str(dict(active).get("tool_id", "") or "")
        except Exception:
            avid = ""
            atool = ""
        note = ("updating — cached observation during active job"
                if atool == tool_id else
                "stale — cached observation while another update is active")
        if avid:
            note = "%s %s" % (note, avid[:8])
        return _ok(_labeled_cached(dict(cached), note))
    if recovering:
        return _ok(_labeled_cached(
            dict(cached), "stale — cached observation; recovery required"))

    # 15-minute discovery coalesce (H-10) with per-tool lock, short
    # critical section. Cache holds (snapshot_card, monotonic_ts);
    # fresh (<900s) skips adapter probes unless ?force=1. Concurrent
    # duplicates coalesce: contenders see the in-flight mark (or fail the
    # non-blocking lock) and return the cached DB card without probing.
    # The per-tool lock guards only cache/inflight bookkeeping, never the
    # adapter probes below.
    _lock = _per_tool_lock(tool_id)
    _got_lock = _lock.acquire(blocking=False)
    if not _got_lock:
        conn_coal = _db()
        try:
            return _ok(_read_tool_card(conn_coal, tool_id))
        finally:
            try:
                conn_coal.close()
            except Exception:
                pass
    _added_inflight = False
    try:
        # Short critical section: cache freshness + in-flight check.
        _cache_hit = False
        _coalesced = False
        if not _force:
            try:
                with _DISCOVERY_LOCKS_GUARD:
                    _entry = _DISCOVERY_CACHE.get(tool_id)
                    if _entry is not None:
                        _snap, _ts = _entry
                        if (time.monotonic() - float(_ts)) < float(
                                DISCOVERY_CACHE_S):
                            _cache_hit = True
                    if not _cache_hit:
                        if tool_id in _DISCOVERY_INFLIGHT:
                            _coalesced = True
                        else:
                            _DISCOVERY_INFLIGHT.add(tool_id)
                            _added_inflight = True
            except Exception:
                pass
            if _cache_hit:
                try:
                    _lock.release()
                except Exception:
                    pass
                conn_hit = _db()
                try:
                    return _ok(_read_tool_card(conn_hit, tool_id))
                finally:
                    try:
                        conn_hit.close()
                    except Exception:
                        pass
            if _coalesced:
                try:
                    _lock.release()
                except Exception:
                    pass
                conn_coal2 = _db()
                try:
                    return _ok(_read_tool_card(conn_coal2, tool_id))
                finally:
                    try:
                        conn_coal2.close()
                    except Exception:
                        pass
        else:
            # ?force=1 bypasses the cache but still registers in-flight so
            # concurrent forced duplicates coalesce.
            try:
                with _DISCOVERY_LOCKS_GUARD:
                    if tool_id in _DISCOVERY_INFLIGHT:
                        _coalesced = True
                    else:
                        _DISCOVERY_INFLIGHT.add(tool_id)
                        _added_inflight = True
            except Exception:
                pass
            if _coalesced:
                try:
                    _lock.release()
                except Exception:
                    pass
                conn_coal3 = _db()
                try:
                    return _ok(_read_tool_card(conn_coal3, tool_id))
                finally:
                    try:
                        conn_coal3.close()
                    except Exception:
                        pass
        # Release before the long adapter probes: short section ends here.
        try:
            _lock.release()
        except Exception:
            pass
        # No active job: refresh via the owner probe queue (R01). The API
        # account cannot execute installation probes; the dispatcher
        # (ubuntu) runs inspect/discover/activity/verify and returns typed
        # results. No transaction is held across the wait (bounded 25s in
        # a worker thread; the event loop stays responsive per R16).
        now_iso = utcnow_iso()
        _status, _payload = await _owner_probe(tool_id, "refresh", 25.0)
        if _status == "deferred":
            # Dispatcher deferred (mutation started meanwhile): cached.
            return _ok(_labeled_cached(
                dict(cached),
                "stale — probe deferred; update started"))
        if _status == "timeout":
            return _ok(_preserve_with_error(
                tool_id, "owner probe timeout after 25s; retry"))
        if _status != "ok":
            return _ok(_preserve_with_error(
                tool_id, str(_payload.get("reason", "probe failed"))[:500]))
        try:
            install_identity = str(
                _probe_field(_payload, "inspection",
                             "install_identity", "") or "")[:500]
            observed_version = str(
                _probe_field(_payload, "inspection", "version", "")
                or "")[:200]
            tool_fingerprint = str(
                _probe_field(_payload, "inspection", "fingerprint", "")
                or "")[:200]
            available_target = str(
                _probe_field(_payload, "discovery", "target", "")
                or "")[:200]
            channel = str(
                _probe_field(_payload, "inspection", "channel", "") or
                _probe_field(_payload, "discovery", "channel", "")
                or "")[:200]
            available = bool(
                _probe_field(_payload, "discovery", "available", False))
            unknown_reason = str(
                _probe_field(_payload, "discovery", "unknown_reason", "")
                or "")[:1000]
            discovery_error = "" if available else unknown_reason
            health, health_detail = _health_from_payload_verification(
                _payload.get("verification", {}))
        except Exception as exc:
            return _ok(_preserve_with_error(tool_id, exc))
        # Short write transaction only; the subprocess-backed probes above
        # are already finished so no transaction was held across them.
        # Re-check the single-slot gate so a job that started during the
        # probes wins and our observation does not clobber a mutation.
        conn2 = _db()
        try:
            try:
                conn2.execute("BEGIN IMMEDIATE")
                try:
                    raced = jobs_lib.active_job(conn2)
                except Exception:
                    raced = None
                try:
                    race_recovering = jobs_lib.recovery_blocked(conn2)
                except Exception:
                    race_recovering = False
                if raced is not None or race_recovering:
                    try:
                        conn2.rollback()
                    except Exception:
                        pass
                    conn_cached = _db()
                    try:
                        card = _read_tool_card(conn_cached, tool_id)
                    finally:
                        try:
                            conn_cached.close()
                        except Exception:
                            pass
                    if raced is not None:
                        avid = ""
                        atool = ""
                        try:
                            avid = str(dict(raced).get("id", "") or "")
                            atool = str(dict(raced).get("tool_id", "") or "")
                        except Exception:
                            avid = ""
                            atool = ""
                        note = ("updating — cached observation during active job"
                                if atool == tool_id else
                                "stale — cached observation while another"
                                " update is active")
                        if avid:
                            note = "%s %s" % (note, avid[:8])
                        return _ok(_labeled_cached(card, note))
                    return _ok(_labeled_cached(
                        card, "stale — cached observation; recovery required"))
                conn2.execute(
                    "INSERT OR IGNORE INTO tools(id) VALUES(?)", (tool_id,))
                conn2.execute(
                    "UPDATE tools SET install_identity=?, observed_version=?,"
                    " available_target=?, channel=?, fingerprint=?,"
                    " observation_time=?, discovery_error=?, health=?,"
                    " health_detail=?, updated_at=?, last_success_at=?,"
                    " last_attempt_at=?, last_attempt_error=? WHERE id=?",
                    (install_identity, observed_version, available_target,
                     channel, tool_fingerprint, now_iso, discovery_error,
                     health, health_detail, now_iso, now_iso, now_iso,
                     "", tool_id))
                conn2.commit()
            except Exception:
                try:
                    conn2.rollback()
                except Exception:
                    pass
                # Persist failure still returns the last cached observation
                # rather than inventing one.
                conn_cached = _db()
                try:
                    return _ok(_read_tool_card(conn_cached, tool_id))
                finally:
                    try:
                        conn_cached.close()
                    except Exception:
                        pass
        finally:
            try:
                conn2.close()
            except Exception:
                pass
        conn3 = _db()
        try:
            fresh_card = _read_tool_card(conn3, tool_id)
        finally:
            try:
                conn3.close()
            except Exception:
                pass
        # Refresh the 15-minute coalesce snapshot on success only; failures
        # preserve the last observation and stay retryable.
        try:
            with _DISCOVERY_LOCKS_GUARD:
                _DISCOVERY_CACHE[tool_id] = (dict(fresh_card),
                                             time.monotonic())
        except Exception:
            pass
        return _ok(fresh_card)
    finally:
        # Only the holder that registered in-flight clears it; coalesced
        # contenders and cache hits leave the holder's mark intact.
        if _added_inflight:
            try:
                with _DISCOVERY_LOCKS_GUARD:
                    _DISCOVERY_INFLIGHT.discard(tool_id)
            except Exception:
                pass
        try:
            _lock.release()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 4. POST /tools/{id}/plans — 5-minute server-owned preview, no installation
# ---------------------------------------------------------------------------
@router.post("/tools/{tool_id}/plans")
async def post_tool_plan(tool_id: str, request: Request):
    gated = _authed(request, kind="write")
    if isinstance(gated, JSONResponse):
        return gated
    claims, subject, _rid = gated
    guard = deps.require_mutation_guards(request, claims or {})
    if guard is not None:
        return guard
    kind = deps.classify_tool_id(tool_id)
    if kind == "claude":
        return deps.error_envelope(
            410, "install_method_unsupported",
            "claude adapter is disabled (BLOCKED_INSTALL_OWNERSHIP)",
            "tool_id=claude")
    if kind == "unknown":
        return deps.error_envelope(
            404, "not_found", "unknown tool", "tool_id=%s" % tool_id[:32])
    # Drain gate: when <state_dir>/drain exists, refuse new plans with
    # 503 maintenance (reads keep working). Checked before any probes.
    if _drained():
        return deps.error_envelope(
            503, "maintenance",
            "console is drained for maintenance; new plans are refused",
            "")
    # Plan gate (H-11): any nonterminal global job or recovery_required
    # refuses the plan with 409 WITHOUT invoking adapter probes.
    _gate_conn = _db()
    try:
        try:
            _gate_active = jobs_lib.active_job(_gate_conn)
        except Exception:
            _gate_active = None
        try:
            _gate_recovering = jobs_lib.recovery_blocked(_gate_conn)
        except Exception:
            _gate_recovering = False
    finally:
        try:
            _gate_conn.close()
        except Exception:
            pass
    if _gate_active is not None:
        return deps.error_envelope(
            409, "busy",
            "another update is already active", "")
    if _gate_recovering:
        return deps.error_envelope(
            409, "recovery_required",
            "recovery required; SSH reconcile must clear first", "")
    # Ack is never sent at plan time: unknown activity is recorded on the
    # plan (201) and enforced at POST /jobs with activity_ack=true. The
    # body is read bounded and ignored for ack purposes (R16).
    try:
        await deps.read_bounded_body(request)
    except ValueError:
        return deps.error_envelope(
            422, "invalid_request", "request body too large", "")
    except Exception:
        pass
    # Owner-side plan construction (R01/R15): the dispatcher (ubuntu)
    # runs activity()+plan() and returns typed results. The API never
    # installs, downloads, or restarts; the wait is bounded (worker
    # thread, R16). No transaction is held across the wait.
    _status, _payload = await _owner_probe(tool_id, "plan", 30.0)
    if _status == "deferred":
        return deps.error_envelope(
            409, "busy", "another update started; retry", "")
    if _status == "timeout":
        return deps.error_envelope(
            503, "unavailable", "owner plan probe timeout; retry", "")
    if _status != "ok":
        return deps.error_envelope(
            503, "unavailable",
            "could not build plan: %s"
            % str(_payload.get("reason", "probe failed"))[:200],
            "tool_id=%s" % tool_id[:32])
    _activity = _payload.get("activity", {})
    _planned = _payload.get("planned", {})
    if not isinstance(_activity, dict):
        _activity = {}
    if not isinstance(_planned, dict):
        return deps.error_envelope(
            503, "unavailable", "could not parse plan", "")
    try:
        fingerprint = str(
            _planned.get("fingerprint", "") or "").strip()
        target = str(_planned.get("target", "") or "").strip()
        target_mode = str(
            _planned.get("target_mode", "") or "").strip()
        channel = str(_planned.get("channel", "") or "")[:200]
        services_raw = _planned.get("services", []) or []
        backup_raw = _planned.get("backup_scope", {}) or {}
        required_space = int(
            _planned.get("required_space_bytes", 0) or 0)
        steps_raw = _planned.get("steps", []) or []
        restart_impact = str(
            _planned.get("restart_impact", "") or "")[:2000]
        activity_state = str(
            _activity.get("state", "unknown") or "unknown").strip()
        activity_evidence = str(
            _activity.get("evidence", "") or "")[:1000]
        activity_ts = str(_activity.get("checked_at", "") or "")
        install_identity = str(
            _planned.get("install_identity", "") or "")[:500]
    except Exception as exc:
        return deps.error_envelope(
            503, "unavailable",
            "could not parse plan: %s" % _safe_detail(exc),
            "tool_id=%s" % tool_id[:32])
    if activity_state not in ("idle", "busy", "unknown"):
        activity_state = "unknown"
    # Missing fingerprint can never yield a trustworthy plan (fail-closed).
    if not fingerprint:
        return deps.error_envelope(
            409, "stale_plan",
            "missing installation fingerprint; run check again",
            "tool_id=%s" % tool_id[:32])
    # Unavailable discovery (empty target / unknown mode) is stale, not a
    # plannable update: the caller must run check again after inventory.
    if not target or target_mode not in ("exact", "native_latest"):
        return deps.error_envelope(
            409, "stale_plan",
            "plan target unavailable; run check again",
            "tool_id=%s" % tool_id[:32])
    if activity_state == "busy":
        return deps.error_envelope(
            409, "activity_blocked",
            "tool reports active work; update blocked",
            activity_evidence[:300])
    # Unknown activity is allowed at plan time without ack (recorded on
    # the plan; ack is enforced at POST /jobs).
    try:
        services = [str(s)[:300] for s in list(services_raw)
                    if str(s).strip()]
    except Exception:
        services = []
    try:
        backup_scope = {str(k)[:200]: str(v)[:2000]
                        for k, v in dict(backup_raw).items()}
    except Exception:
        backup_scope = {}
    try:
        steps = [str(s)[:100] for s in list(steps_raw) if str(s).strip()]
    except Exception:
        steps = []
    if not steps:
        return deps.error_envelope(
            409, "stale_plan",
            "plan steps unavailable; run check again",
            "tool_id=%s" % tool_id[:32])
    # Fail-closed manifests (R15/R24): a plan without a mandatory-check
    # manifest or any space budget is not an executable contract.
    try:
        _required_checks = [str(c) for c in
                            (_planned.get("required_checks", []) or [])
                            if str(c).strip()]
    except Exception:
        _required_checks = []
    if not _required_checks:
        return deps.error_envelope(
            409, "stale_plan",
            "plan check manifest unavailable; run check again",
            "tool_id=%s" % tool_id[:32])
    try:
        _has_budget = bool(_planned.get("budgets") or
                           _planned.get("space_fs") or False)
    except Exception:
        _has_budget = False
    if required_space < 0:
        required_space = 0
    if required_space <= 0 and not _has_budget:
        return deps.error_envelope(
            409, "stale_plan",
            "plan space budget unavailable; run check again",
            "tool_id=%s" % tool_id[:32])
    # Immutable plan contract (R15): bind config hash + resolved release
    # now; execution re-checks them and blocks on drift instead of
    # silently mixing saved fields with fresh defaults.
    try:
        from ..inventory import config_identity
        from ..owner_env import resolve_release
    except Exception:
        try:
            from backend.app.inventory import config_identity  # type: ignore[no-redef]
            from backend.app.owner_env import resolve_release  # type: ignore[no-redef]
        except Exception:
            config_identity = None  # type: ignore[assignment]
            resolve_release = None  # type: ignore[assignment]
    try:
        _config_hash = config_identity(settings) if config_identity else ""
    except Exception:
        _config_hash = ""
    try:
        _release_path = resolve_release() if resolve_release else ""
    except Exception as exc:
        return deps.error_envelope(
            503, "unavailable",
            "release unresolvable: %s" % _safe_detail(exc), "")
    # N09: canonical owner-environment fingerprint bound into the plan;
    # execution rechecks it before mutation (preview==apply parity).
    try:
        from ..owner_env import canonical_fingerprint
        _env_fp = canonical_fingerprint(
            settings, None, _release_path) or ""
    except Exception:
        try:
            from backend.app.owner_env import canonical_fingerprint as _cf  # type: ignore[no-redef]
            _env_fp = _cf(settings, None, _release_path) or ""
        except Exception:
            _env_fp = ""
    if not _config_hash or not _release_path or not _env_fp:
        return deps.error_envelope(
            503, "unavailable",
            "configuration identity unprovable; retry", "")
    now = datetime.now(timezone.utc)
    try:
        ttl = int(getattr(settings, "plan_ttl_s", 300) or 300)
    except Exception:
        ttl = 300
    expires = now + timedelta(seconds=ttl)
    try:
        from ..plans import build_plan_row, insert_plan
    except Exception:
        try:
            from backend.app.plans import build_plan_row, insert_plan  # type: ignore[no-redef]
        except Exception:
            return deps.error_envelope(
                503, "unavailable", "plan store unavailable", "")
    try:
        _deadlines = dict(_planned.get("deadlines", {}) or {})
    except Exception:
        _deadlines = {}
    # Adapter-attached non-field extras (R15/R25/R27): daemon expectation
    # and manual-limitation flags ride in artifact_json + the response so
    # they are never silently dropped by model boundaries.
    _artifact = {}
    try:
        for _k in ("daemon_expected", "daemon_status_at_plan",
                   "manual_restart_limitation"):
            _v = _planned.get(_k, None)
            if _v is not None and not isinstance(_v, dict):
                _artifact[_k] = _v
    except Exception:
        _artifact = {}
    try:
        _row = build_plan_row(
            tool_id=tool_id, subject=subject or "",
            install_identity=install_identity, fingerprint=fingerprint,
            target=target, target_mode=target_mode, channel=channel,
            services=services,
            launch=_planned.get("launch", {}) or {},
            state_homes=_planned.get("state_homes", []) or [],
            backup_scope=backup_scope,
            backup_policy=_planned.get("backup_policy", {}) or {},
            required_probes=_planned.get("required_probes", []) or [],
            required_checks=_planned.get("required_checks", []) or [],
            budgets=_planned.get("budgets", {}) or {},
            space_fs=_planned.get("space_fs", {}) or {},
            steps=steps, deadlines=_deadlines,
            restart_impact=restart_impact,
            restart_detail=_planned.get("restart_detail", "") or "",
            activity_state=activity_state, activity_ts=activity_ts,
            activity_evidence=activity_evidence,
            required_space_bytes=required_space,
            config_hash=_config_hash, release_path=_release_path,
            created_at=now.isoformat(), expires_at=expires.isoformat(),
            artifact=_artifact, env_fingerprint=_env_fp)
    except Exception as exc:
        return deps.error_envelope(
            503, "unavailable",
            "could not assemble plan: %s" % _safe_detail(exc), "")
    plan_id = str(_row.get("id", ""))
    # Short write transaction only; probes above already finished.
    conn = _db()
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            insert_plan(conn, _row)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return deps.error_envelope(
                503, "unavailable", "could not persist plan",
                "tool_id=%s" % tool_id[:32])
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return _created({
        "id": plan_id,
        "tool_id": tool_id,
        "target": target,
        "target_mode": target_mode,
        "channel": channel,
        "fingerprint": fingerprint,
        "services": services,
        "backup_scope": backup_scope,
        "activity_state": activity_state,
        "activity_evidence": activity_evidence,
        "required_space_bytes": required_space,
        "steps": steps,
        "expires_at": expires.isoformat(),
        "restart_impact": restart_impact,
        "plan_version": 2,
        "config_hash": _config_hash,
        "release_path": _release_path,
        "env_fingerprint": _env_fp,
        "plan_hash": str(_row.get("plan_hash", "")),
        "artifact": dict(_artifact),
        "single_use": True,
    })


# ---------------------------------------------------------------------------
# 5. POST /jobs — plan_id + activity ack; Idempotency-Key required; 202
# ---------------------------------------------------------------------------
@router.post("/jobs")
async def post_job(request: Request):
    gated = _authed(request, kind="write")
    if isinstance(gated, JSONResponse):
        return gated
    claims, subject, _rid = gated
    guard = deps.require_mutation_guards(request, claims or {})
    if guard is not None:
        return guard
    # R14: authenticate + minimal shape first. Idempotency lookup comes
    # BEFORE every new-admission condition (drain, readiness, expiry,
    # fingerprint): a replay after a lost response returns the recorded
    # job instead of stale-plan/recovery/maintenance.
    idem_key = (request.headers.get("idempotency-key", "") or "").strip()
    if not idem_key:
        return deps.error_envelope(
            422, "invalid_request", "Idempotency-Key header is required", "")
    if len(idem_key) > 256:
        return deps.error_envelope(
            422, "invalid_request", "Idempotency-Key too long", "")
    try:
        raw_body = await deps.read_bounded_body(request)
    except ValueError:
        return deps.error_envelope(
            422, "invalid_request", "request body too large", "")
    except Exception:
        return deps.error_envelope(
            422, "invalid_request", "could not read request body", "")
    try:
        body = json.loads(raw_body.decode("utf-8") or "{}") \
            if raw_body else {}
    except Exception:
        return deps.error_envelope(
            422, "invalid_request", "malformed JSON body", "")
    if not isinstance(body, dict):
        return deps.error_envelope(
            422, "invalid_request", "malformed JSON body", "")
    plan_id = body.get("plan_id", "")
    ack = body.get("activity_ack", False)
    if not isinstance(plan_id, str) or not plan_id.strip():
        return deps.error_envelope(
            422, "invalid_request", "plan_id is required", "")
    plan_id = plan_id.strip()
    if not deps.is_valid_uuid(plan_id):
        return deps.error_envelope(
            422, "invalid_request", "plan_id must be a UUID", "")
    if not isinstance(ack, bool):
        return deps.error_envelope(
            422, "invalid_request", "activity_ack must be boolean", "")
    conn = _db()
    try:
        # Safe sweep of never-claimed reservations (accepted + past claim
        # deadline + empty nonce only; claimed rows untouched).
        try:
            jobs_lib.expire_stale_accepted(conn)
        except Exception:
            pass
        # Read-only plan identification for probe targeting (no gates;
        # admission re-validates everything authoritatively inside tx).
        try:
            from ..plans import (PlanNotFound, PlanInvalid, load_plan)
        except Exception:
            try:
                from backend.app.plans import (  # type: ignore[no-redef]
                    PlanNotFound, PlanInvalid, load_plan)
            except Exception:
                return deps.error_envelope(
                    503, "unavailable", "plan store unavailable", "")
        try:
            _plan_probe = load_plan(conn, plan_id)
        except PlanNotFound:
            return deps.error_envelope(
                404, "not_found", "unknown plan", "")
        except PlanInvalid as exc:
            return deps.error_envelope(
                409, "stale_plan", "plan invalid: %s" % _safe_detail(exc),
                "plan_id=%s" % plan_id[:8])
        except Exception:
            return deps.error_envelope(
                503, "unavailable", "plan store unavailable", "")
        _tool_probe = str(_plan_probe.get("tool_id", "") or "")
        if _tool_probe == "claude":
            return deps.error_envelope(
                410, "install_method_unsupported",
                "claude adapter is disabled", "")
        # Fresh authoritative owner evidence (bounded, off-loop) for the
        # shared admission service. Probes never mutate.
        _fp_status, _fp_payload = await _owner_probe(
            _tool_probe, "inspect", 20.0)
        if _fp_status != "ok":
            return deps.error_envelope(
                503, "unavailable",
                "installation revalidation unavailable; retry", "")
        _fresh_fp = str(_fp_payload.get("fingerprint", "") or "")
        # Config/release/env identities are compared inside the shared
        # admission service (pure local reads at its boundary).
        # One shared admission service (N14/N15): replay-first, gates,
        # atomic reserve + plan-consume + mutation lease. API and CLI
        # call the identical function with owner-gathered evidence.
        try:
            from ..admission import admit
        except Exception:
            try:
                from backend.app.admission import admit  # type: ignore[no-redef]
            except Exception:
                return deps.error_envelope(
                    503, "unavailable", "admission unavailable", "")
        try:
            _hb = jobs_lib.read_dispatcher_heartbeat(
                settings.state_dir, max_age_s=20)
        except Exception:
            _hb = {}
        job_id, created_new, err_code = admit(
            conn, subject or "", idem_key, plan_id, ack,
            _fresh_fp, bool(_hb), _drained())
        if err_code:
            if err_code == "conflict":
                return deps.error_envelope(
                    409, "conflict",
                    "Idempotency-Key already used with different payload",
                    "")
            if err_code == "not_found":
                return deps.error_envelope(
                    404, "not_found", "unknown plan", "")
            if err_code == "maintenance":
                return deps.error_envelope(
                    503, "maintenance",
                    "console is drained for maintenance; new jobs refused",
                    "")
            if err_code == "worker_unavailable":
                return deps.error_envelope(
                    503, "worker_unavailable",
                    "worker not ready; job not accepted", "")
            if err_code == "recovery_required":
                return deps.error_envelope(
                    409, "recovery_required",
                    "recovery required; SSH reconcile must clear first",
                    "")
            if err_code == "fingerprint_changed":
                return deps.error_envelope(
                    409, "fingerprint_changed",
                    "installation changed since plan; create a fresh plan",
                    "tool_id=%s" % _tool_probe[:32])
            if err_code == "config_changed":
                return deps.error_envelope(
                    409, "config_changed",
                    "configuration changed since plan; create a fresh plan",
                    "tool_id=%s" % _tool_probe[:32])
            if err_code == "activity_blocked":
                return deps.error_envelope(
                    409, "activity_blocked",
                    "tool reports active work; update blocked", "")
            if err_code == "ack_required":
                return deps.error_envelope(
                    409, "ack_required",
                    "unknown activity requires explicit acknowledgment",
                    "tool_id=%s" % _tool_probe[:32])
            if err_code == "disabled":
                return deps.error_envelope(
                    410, "install_method_unsupported",
                    "adapter disabled", "")
            if err_code == "invalid_request":
                return deps.error_envelope(
                    422, "invalid_request",
                    "plan not admissible; create a fresh plan", "")
            if err_code == "stale_plan":
                return deps.error_envelope(
                    409, "stale_plan",
                    "plan expired/used/invalid; create a fresh plan",
                    "plan_id=%s" % plan_id[:8])
            if err_code == "busy":
                return deps.error_envelope(
                    409, "busy",
                    "another update is already active", "")
            return deps.error_envelope(
                503, "unavailable", "could not reserve job", "")
        row = conn.execute("SELECT * FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        if row is None:
            return deps.error_envelope(
                503, "unavailable", "job reservation lost", "")
        view = _job_view(row)
        view["backup_summary"] = _backup_summary(conn, job_id)
        view["replayed"] = (not created_new)
        if created_new:
            return _accepted(view)
        return _ok(view)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 6. GET /jobs — paginated history, default 25, max 100
# ---------------------------------------------------------------------------
@router.get("/jobs")
def list_jobs(request: Request):
    gated = _authed(request)
    if isinstance(gated, JSONResponse):
        return gated
    qp = request.query_params
    try:
        limit = deps.parse_limit(qp.get("limit"), default=25, max_n=100)
    except (ValueError, TypeError):
        return deps.error_envelope(
            422, "invalid_request",
            "limit must be an integer 1..100", "")
    cursor = (qp.get("cursor", "") or "").strip()
    # R21 global active-job poll: ?active=true returns nonterminal rows
    # only (used for update gating independent of history/detail views).
    _active_raw = str(qp.get("active", "") or "").strip().lower()
    if _active_raw not in ("", "0", "false", "1", "true"):
        return deps.error_envelope(
            422, "invalid_request", "active must be 0/1", "")
    _active_only = _active_raw in ("1", "true")
    conn = _db()
    try:
        cursor_created = ""
        cursor_id = ""
        if cursor:
            if not deps.is_valid_uuid(cursor):
                return deps.error_envelope(
                    422, "invalid_request",
                    "cursor must be a job UUID", "")
            crow = conn.execute("SELECT * FROM jobs WHERE id=?",
                                (cursor,)).fetchone()
            if crow is None:
                return deps.error_envelope(
                    422, "invalid_request", "unknown cursor", "")
            cursor_created = dict(crow).get("created_at", "") or ""
            cursor_id = cursor
        if cursor:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE (created_at, id) < (?, ?)"
                + (" AND state IN (?,?,?,?,?)" if _active_only else "") +
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                ((cursor_created, cursor_id) +
                 (tuple(jobs_lib.NONTERMINAL) if _active_only else ()) +
                 (limit + 1,))).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs"
                + (" WHERE state IN (?,?,?,?,?)" if _active_only else "") +
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                ((tuple(jobs_lib.NONTERMINAL) if _active_only else ()) +
                 (limit + 1,))).fetchall()
        page = list(rows[:limit])
        if len(rows) > limit:
            next_cursor = dict(page[-1]).get("id", "") if page else ""
        else:
            next_cursor = ""
        jobs_out = []
        for r in page:
            v = _job_view(r)
            v["backup_summary"] = _backup_summary(conn, v["id"])
            jobs_out.append(v)
        return _ok({"jobs": jobs_out, "next_cursor": next_cursor})
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 7. GET /jobs/{id} — state, step, timings, checks, backup summary, errors
# ---------------------------------------------------------------------------
@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    gated = _authed(request)
    if isinstance(gated, JSONResponse):
        return gated
    if not deps.is_valid_uuid(job_id):
        return deps.error_envelope(
            422, "invalid_request", "job id must be a UUID", "")
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        if row is None:
            return deps.error_envelope(
                404, "not_found", "unknown job", "")
        view = _job_view(row)
        view["backup_summary"] = _backup_summary(conn, job_id)
        view["checks"] = _checks(conn, job_id)
        return _ok(view)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 8. GET /jobs/{id}/logs — ordered redacted records, next cursor, truncation
# ---------------------------------------------------------------------------
@router.get("/jobs/{job_id}/logs")
def get_job_logs(job_id: str, request: Request):
    gated = _authed(request)
    if isinstance(gated, JSONResponse):
        return gated
    if not deps.is_valid_uuid(job_id):
        return deps.error_envelope(
            422, "invalid_request", "job id must be a UUID", "")
    qp = request.query_params
    try:
        after = deps.parse_after(qp.get("after"), default=0)
    except (ValueError, TypeError):
        return deps.error_envelope(
            422, "invalid_request",
            "after must be an integer >= 0", "")
    try:
        limit = deps.parse_limit(qp.get("limit"), default=LOG_DEFAULT_LIMIT,
                                 max_n=LOG_MAX_LIMIT)
    except (ValueError, TypeError):
        return deps.error_envelope(
            422, "invalid_request",
            "limit must be an integer 1..%d" % LOG_MAX_LIMIT, "")
    conn = _db()
    try:
        row = conn.execute("SELECT id FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        if row is None:
            return deps.error_envelope(
                404, "not_found", "unknown job", "")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    # File read happens outside any DB transaction. Any failure here —
    # including a broken secret source for re-redaction (G05) — fails
    # closed with 503 rather than serving weakened evidence.
    try:
        page = _read_log_page(job_id, after, limit)
    except Exception:
        return deps.error_envelope(
            503, "unavailable", "log evidence unavailable", "")
    return _ok(page)


# ---------------------------------------------------------------------------
# 9. GET /health — authenticated API/database/worker readiness summary
# ---------------------------------------------------------------------------
@router.get("/health")
def get_health(request: Request):
    gated = _authed(request)
    if isinstance(gated, JSONResponse):
        return gated
    now_iso = utcnow_iso()
    database = "ok"
    recovery_required = False
    worker = "down"
    api = "ok"
    conn = None
    try:
        conn = _db()
        try:
            conn.execute("SELECT 1").fetchone()
        except Exception:
            database = "down"
        if database == "down":
            api = "degraded"
            worker = "down"
        else:
            try:
                recovery_required = jobs_lib.recovery_blocked(conn)
            except Exception:
                recovery_required = False
            # Worker readiness comes strictly from the dispatcher
            # heartbeat file (jobs.read_dispatcher_heartbeat): fresh
            # (<=20s) -> ok, stale/missing -> down. Never infer ok from
            # the absence of jobs.
            try:
                hb = jobs_lib.read_dispatcher_heartbeat(
                    settings.state_dir, max_age_s=20)
            except Exception:
                hb = {}
            worker = "ok" if hb else "down"
    except Exception:
        database = "down"
        api = "degraded"
        worker = "down"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return _ok({
        "api": api,
        "database": database,
        "worker": worker,
        "recovery_required": bool(recovery_required),
        "checked_at": now_iso,
    })
