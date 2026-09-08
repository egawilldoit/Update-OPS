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
- POST /jobs delegates single-slot reservation + idempotency to
  jobs.reserve_job.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
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
# Explicit per-job cap marker emitted by worker/runner.py JobLog._write_record.
# Log readers match only this string (not a generic "truncat" substring) so
# per-line "…[truncated-line]" suffixes and user output containing "truncate"
# never falsely report per-job truncation.
LOG_TRUNCATION_MARKER = ("[truncated: per-job log cap reached; "
                         "draining without persisting]")


def _known_secrets():
    # type: () -> tuple
    """Best-effort known secrets for log redaction; () when unavailable."""
    try:
        return load_secret_values(settings)
    except Exception:
        return ()


def _load_adapter(tool_id):
    # type: (str) -> Any
    """Lazily resolve the adapter for a tool id (no probes at import)."""
    try:
        from backend.app.adapters import registry as adapter_registry
    except Exception:
        from ..adapters import registry as adapter_registry  # type: ignore[no-redef]
    return adapter_registry.get_adapter(tool_id)


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


def _authed(request):
    # type: (Request) -> Any
    """Common auth + rate-limit gate. Returns (claims, subject, rid) or
    an error JSONResponse (caller must `isinstance`-check)."""
    claims, err_resp, rid = deps.authenticate(request)
    if err_resp is not None:
        return err_resp
    subject = deps.subject_of(claims or {})
    if not deps.check_rate_limit(subject or "anonymous"):
        return deps.rate_limited_response()
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
            "install_identity": "",
        }
    return _tool_response_row(row, last_success, last_attempt)


def _job_view(row):
    # type: (Any) -> Dict[str, Any]
    d = dict(row)
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
def post_tool_check(tool_id: str, request: Request):
    gated = _authed(request)
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
        # On exception preserve the last observation; record only
        # discovery_error + observation timestamps (never invent health).
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
                    " observation_time=?, updated_at=? WHERE id=?",
                    (str(message)[:500], now_inner, now_inner,
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
        # No active job: run the real read-only probes outside any
        # transaction (inspect/discover/activity + verify). Never invent
        # observations: every field comes from the adapter results.
        try:
            adapter = _load_adapter(tool_id)
        except KeyError:
            return deps.error_envelope(
                404, "not_found", "unknown tool", "tool_id=%s" % tool_id[:32])
        except Exception as exc:
            return _ok(_preserve_with_error(tool_id, exc))
        now_iso = utcnow_iso()
        try:
            inspection = adapter.inspect()
            discovery = adapter.discover()
            activity = adapter.activity()
            verification = adapter.verify()
        except Exception as exc:
            # Any probe failure preserves the last observation and records
            # discovery_error (fail-closed, no invented data).
            return _ok(_preserve_with_error(tool_id, exc))
        try:
            install_identity = str(
                getattr(inspection, "install_identity", "") or "")[:500]
            observed_version = str(
                getattr(inspection, "version", "") or "")[:200]
            tool_fingerprint = str(
                getattr(inspection, "fingerprint", "") or "")[:200]
            available_target = str(
                getattr(discovery, "target", "") or "")[:200]
            channel = str(
                getattr(inspection, "channel", "") or
                getattr(discovery, "channel", "") or "")[:200]
            available = bool(getattr(discovery, "available", False))
            unknown_reason = str(
                getattr(discovery, "unknown_reason", "") or "")[:1000]
            discovery_error = "" if available else unknown_reason
            health, health_detail = _health_from_verify(verification)
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
                    " health_detail=?, updated_at=? WHERE id=?",
                    (install_identity, observed_version, available_target,
                     channel, tool_fingerprint, now_iso, discovery_error,
                     health, health_detail, now_iso, tool_id))
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
    gated = _authed(request)
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
    # plan (201) and enforced at POST /jobs with activity_ack=true. Any
    # request body is ignored for ack purposes.
    try:
        await request.body()
    except Exception:
        pass
    # Read-only adapter probes outside any DB transaction. Never install,
    # download, or restart here; plan() is the read-only preview.
    try:
        adapter = _load_adapter(tool_id)
    except KeyError:
        return deps.error_envelope(
            404, "not_found", "unknown tool", "tool_id=%s" % tool_id[:32])
    except Exception as exc:
        return deps.error_envelope(
            503, "unavailable", "adapter unavailable: %s" % str(exc)[:200],
            "tool_id=%s" % tool_id[:32])
    try:
        activity = adapter.activity()
    except Exception as exc:
        # Unprovable activity degrades to unknown (recorded on the plan,
        # enforced at job time); planning itself stays 201-capable.
        class _UnknownActivity(object):
            state = "unknown"
            evidence = "activity probe failed: %s" % str(exc)[:500]
        activity = _UnknownActivity()
    try:
        planned = adapter.plan()
    except Exception as exc:
        return deps.error_envelope(
            503, "unavailable",
            "could not build plan: %s" % str(exc)[:200],
            "tool_id=%s" % tool_id[:32])
    try:
        fingerprint = str(getattr(planned, "fingerprint", "") or "").strip()
        target = str(getattr(planned, "target", "") or "").strip()
        target_mode = str(getattr(planned, "target_mode", "") or "").strip()
        channel = str(getattr(planned, "channel", "") or "")[:200]
        services_raw = getattr(planned, "services", []) or []
        backup_raw = getattr(planned, "backup_scope", {}) or {}
        required_space = int(
            getattr(planned, "required_space_bytes", 0) or 0)
        steps_raw = getattr(planned, "steps", []) or []
        restart_impact = str(
            getattr(planned, "restart_impact", "") or "")[:2000]
        activity_state = str(
            getattr(activity, "state", "unknown") or "unknown").strip()
        activity_evidence = str(
            getattr(activity, "evidence", "") or "")[:1000]
    except Exception as exc:
        return deps.error_envelope(
            503, "unavailable",
            "could not parse plan: %s" % str(exc)[:200],
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
    if required_space < 0:
        required_space = 0
    now = datetime.now(timezone.utc)
    try:
        ttl = int(getattr(settings, "plan_ttl_s", 300) or 300)
    except Exception:
        ttl = 300
    expires = now + timedelta(seconds=ttl)
    plan_id = str(uuid.uuid4())
    # Short write transaction only; probes above already finished.
    conn = _db()
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO plans(id,tool_id,subject,created_at,expires_at,"
                "fingerprint,target,target_mode,channel,services,backup_scope,"
                "activity_state,activity_evidence,used_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, tool_id, subject or "", now.isoformat(),
                 expires.isoformat(), fingerprint, target, target_mode,
                 channel, json.dumps(services), json.dumps(backup_scope),
                 activity_state, activity_evidence, ""))
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
    })


# ---------------------------------------------------------------------------
# 5. POST /jobs — plan_id + activity ack; Idempotency-Key required; 202
# ---------------------------------------------------------------------------
@router.post("/jobs")
async def post_job(request: Request):
    gated = _authed(request)
    if isinstance(gated, JSONResponse):
        return gated
    claims, subject, _rid = gated
    guard = deps.require_mutation_guards(request, claims or {})
    if guard is not None:
        return guard
    # Drain gate: refuse new jobs with 503 maintenance (reads unaffected).
    if _drained():
        return deps.error_envelope(
            503, "maintenance",
            "console is drained for maintenance; new jobs are refused",
            "")
    idem_key = (request.headers.get("idempotency-key", "") or "").strip()
    if not idem_key:
        return deps.error_envelope(
            422, "invalid_request", "Idempotency-Key header is required", "")
    if len(idem_key) > 256:
        return deps.error_envelope(
            422, "invalid_request", "Idempotency-Key too long", "")
    try:
        raw_body = await request.body()
    except Exception:
        return deps.error_envelope(
            422, "invalid_request", "could not read request body", "")
    if len(raw_body) > int(getattr(settings, "body_limit_bytes",
                                   256 * 1024) or 256 * 1024):
        return deps.error_envelope(
            422, "invalid_request", "request body too large", "")
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
        plan = conn.execute("SELECT * FROM plans WHERE id=?",
                            (plan_id,)).fetchone()
        if plan is None:
            return deps.error_envelope(
                404, "not_found", "unknown plan", "")
        plan_d = dict(plan)
        tool_id = plan_d.get("tool_id", "") or ""
        if tool_id == "claude":
            return deps.error_envelope(
                410, "install_method_unsupported",
                "claude adapter is disabled", "")
        # Expiry: plans live 300s (settings.plan_ttl_s); stale -> 409.
        exp = _parse_iso(plan_d.get("expires_at", "") or "")
        now = datetime.now(timezone.utc)
        if exp is None or exp <= now:
            return deps.error_envelope(
                409, "stale_plan", "plan expired; create a fresh plan",
                "plan_id=%s" % plan_id[:8])
        # Fingerprint recheck before mutation (SPEC section 6).
        # Compare against tools.fingerprint (adapter fingerprint persisted
        # at check/plan time); install_identity is a human-readable
        # display string and is never used for this comparison.
        tool_row = conn.execute("SELECT * FROM tools WHERE id=?",
                                (tool_id,)).fetchone()
        current_fp = ""
        if tool_row is not None:
            current_fp = dict(tool_row).get("fingerprint", "") or ""
        planned_fp = plan_d.get("fingerprint", "") or ""
        if current_fp and planned_fp and current_fp != planned_fp:
            return deps.error_envelope(
                409, "fingerprint_changed",
                "installation changed since plan; create a fresh plan",
                "tool_id=%s" % tool_id[:32])
        # Activity gate: busy blocks; unknown requires recorded ack.
        activity = plan_d.get("activity_state", "unknown") or "unknown"
        if activity == "busy":
            evidence = (plan_d.get("activity_evidence", "") or "")[:300]
            return deps.error_envelope(
                409, "activity_blocked",
                "tool reports active work; update blocked", evidence)
        if activity == "unknown" and not ack:
            return deps.error_envelope(
                409, "ack_required",
                "unknown activity requires explicit acknowledgment",
                "tool_id=%s" % tool_id[:32])
        # Single-slot reservation + idempotency in one short transaction.
        try:
            conn.execute("BEGIN IMMEDIATE")
            job_id, created_new, err_code = jobs_lib.reserve_job(
                conn, tool_id, plan_id, subject or "", idem_key, ack)
            if err_code:
                try:
                    conn.rollback()
                except Exception:
                    pass
                if err_code == "recovery_required":
                    return deps.error_envelope(
                        409, "recovery_required",
                        "recovery required; SSH reconcile must clear first",
                        "")
                if err_code == "conflict":
                    return deps.error_envelope(
                        409, "conflict",
                        "Idempotency-Key already used with different payload",
                        "")
                return deps.error_envelope(
                    409, "busy",
                    "another update is already active", "")
            if created_new:
                try:
                    conn.execute("UPDATE plans SET used_at=? WHERE id=?",
                                 (utcnow_iso(), plan_id))
                except Exception:
                    pass
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
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
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                (cursor_created, cursor_id, limit + 1)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit + 1,)).fetchall()
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
    # File read happens outside any DB transaction.
    page = _read_log_page(job_id, after, limit)
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
