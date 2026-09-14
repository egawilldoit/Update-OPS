"""Typed owner-side probe boundary (R01/R16). Python 3.10 compatible.

The API (ega-update, ProtectHome) never executes installation probes. It
writes typed probe requests (tool_id + op only — never argv/paths/env) to
probe_requests; the dispatcher (ubuntu, full owner env) executes them via
adapters and writes typed results to probe_results. The API waits bounded
in a worker thread (never the event loop) and falls back to cached+stale.

W3: HTTP request lifetime != probe lifetime. await_probe is a PURE READER
(a wait timeout never mutates durable lifecycle); enqueue_probe coalesces
identical active requests durably in SQLite; the dispatcher owns result
storage, observation application (observation.py) and exclusion release.

Ops: inspect | discover | activity | plan | verify | refresh
(refresh = inspect+discover+activity+verify bundle for Check again).

Operation classification (F12) — exactly one authority:
- CACHE_ONLY_OPS: pure dashboard reads needing no lease (none of the
  current ops; reserved for future explicitly-safe reads).
- INSTALLATION_READ_OPS: every op reading mutable installation, service,
  or tool state (ALL current ops). Each acquires a bounded probe lease
  before touching the installation; no installation read may overlap
  mutation unless proven safe and documented here (none is).
- MUTATION_OPS / MAINTENANCE_OPS: job reservation+mutation and drain
  lifecycle (enforced via mutation leases + drain file, see leases.py
  and admission.py).

Tables live in migration 003; helpers tolerate their absence (return
unavailable) so older DBs fail closed instead of crashing.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Dict, Tuple

OPS = ("inspect", "discover", "activity", "plan", "verify", "refresh")
CACHE_ONLY_OPS = ()  # type: tuple
INSTALLATION_READ_OPS = ("inspect", "discover", "activity", "plan",
                         "verify", "refresh")
MUTATION_OPS = ("reserve", "mutate")
MAINTENANCE_OPS = ("drain",)
# Ops whose result carries a tool observation the coordinator applies
# durably (observation.apply_probe_result). For these, a result row being
# visible is NOT completion: request_owner_probe_handle waits boundedly
# for the durable applied marker and reports "pending" otherwise.
OBSERVATION_APPLIED_OPS = ("refresh",)
# Explicit small extra bound (seconds) for the applied-marker wait after a
# result becomes visible. Never PROBE_OP_TIMEOUT_S: application is a tiny
# idempotent transaction; a slow coordinator is polled via the durable id.
OBSERVATION_APPLY_WAIT_S = 2.0
# Legacy alias: every installation read contends (F12 collapsed the old
# narrow subset into INSTALLATION_READ_OPS).
CONTENDING_OPS = INSTALLATION_READ_OPS

# Serialized probe-result size contract (W3.2). probe_results.result_json
# is a bounded, VALID UTF-8 JSON document. PROBE_RESULT_MAX_BYTES is the
# serialized UTF-8 byte size of that document as stored (ensure_ascii=
# False). A result within the bound is stored verbatim with its original
# status; a result over the bound is REPLACED by a small typed error
# document (never truncated into invalid JSON).
PROBE_RESULT_MAX_BYTES = 200_000
_TOO_LARGE_REASON = "probe_result_too_large"
_SERIALIZATION_FAILED_REASON = "probe_result_serialization_failed"
_SERIALIZATION_FAILED_DOC = json.dumps(
    {"reason": _SERIALIZATION_FAILED_REASON}, sort_keys=True)


def _serialize_probe_result(payload):
    # type: (Any) -> str
    """Serialize one probe payload exactly as it will be stored."""
    return json.dumps(payload or {}, sort_keys=True, default=str,
                      ensure_ascii=False)


def _oversized_result_doc(actual_bytes):
    # type: (int) -> str
    """Small typed replacement for an over-bound result.

    Non-sensitive metadata only: no payload fragments, oversized fields,
    secrets, or raw environment data.
    """
    return json.dumps({"reason": _TOO_LARGE_REASON,
                       "max_bytes": PROBE_RESULT_MAX_BYTES,
                       "actual_bytes": int(actual_bytes)},
                      sort_keys=True, ensure_ascii=False)


def _utcnow():
    # type: () -> str
    try:
        from .schemas import utcnow_iso
        return utcnow_iso()
    except Exception:
        import datetime
        return datetime.datetime.now(
            datetime.timezone.utc).isoformat()


def _tables_present(conn):
    # type: (sqlite3.Connection) -> bool
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN"
            " ('probe_requests','probe_results')").fetchall()
        return len(row) == 2
    except Exception:
        return False


def enqueue_probe(conn, subject, tool_id, op, arg_json="{}", coalesce=True):
    # type: (sqlite3.Connection, str, str, str, str, bool) -> str
    """Insert a probe request (durable coalescing), or return the id of an
    identical request that is already queued/running.

    W3: the active-request lookup and the insert happen in ONE
    BEGIN IMMEDIATE transaction, so concurrent API instances (and an API
    restart) can never launch a redundant identical probe. Raises
    ValueError on bad op.
    """
    if op not in OPS:
        raise ValueError("unknown probe op: %s" % op)
    if not _tables_present(conn):
        raise ValueError("probe queue unavailable (migrate first)")
    request_id = str(uuid.uuid4())
    try:
        conn.execute("BEGIN IMMEDIATE")
        if coalesce:
            existing = conn.execute(
                "SELECT id FROM probe_requests WHERE tool_id=? AND op=?"
                " AND state IN ('queued','running') AND claim_deadline>?"
                " ORDER BY created_at LIMIT 1",
                (tool_id or "", op, _utcnow())).fetchone()
            if existing is not None:
                conn.execute("COMMIT")
                return str(existing["id"])
        conn.execute(
            "INSERT INTO probe_requests(id,subject,tool_id,op,arg_json,"
            "created_at,claim_deadline,state) VALUES(?,?,?,?,?,?,?,?)",
            (request_id, subject or "", tool_id, op,
             arg_json if isinstance(arg_json, str) else "{}",
             _utcnow(),
             _deadline(120), "queued"))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    return request_id


def _deadline(seconds):
    # type: (float) -> str
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=seconds)).isoformat()


def await_probe(conn, request_id, timeout_s=25.0):
    # type: (sqlite3.Connection, str, float) -> Tuple[str, Dict[str, Any]]
    """Poll for the result. Returns (status, payload).

    status: ok | deferred | error | timeout. Never raises on missing rows
    (timeout instead). Caller closes its own connection promptly; each poll
    is a short read.

    W3: this is a PURE READER. A wait timeout never mutates the durable
    request lifecycle — the dispatcher owns claim/execute/finish; the
    coordinator owns observation application. A timed-out waiter therefore
    cannot discard a probe that later completes successfully.
    """
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    while time.monotonic() < deadline:
        try:
            row = conn.execute(
                "SELECT pr.state, pr.owner, res.status, res.result_json"
                " FROM probe_requests pr LEFT JOIN probe_results res"
                " ON res.request_id=pr.id WHERE pr.id=?",
                (request_id,)).fetchone()
        except Exception:
            return "error", {"reason": "probe state unreadable"}
        if row is None:
            return "error", {"reason": "probe request lost"}
        try:
            state = str(row["state"] or "")
            status = str(row["status"] or "") if row["status"] else ""
            payload_raw = row["result_json"] or "{}"
        except Exception:
            return "error", {"reason": "probe row unreadable"}
        if status in ("ok", "deferred", "error"):
            try:
                payload = json.loads(payload_raw) if payload_raw else {}
                if not isinstance(payload, dict):
                    payload = {"value": payload}
            except Exception:
                payload = {}
            return status, payload
        if state in ("expired", "deferred"):
            return "deferred", {}
        time.sleep(0.25)
    return "timeout", {}


def observation_applied(conn, request_id):
    # type: (sqlite3.Connection, str) -> bool
    """Durable applied marker for one stored result (pure reader).

    True only when the coordinator has durably processed the visible
    result: probe_requests.result_id holds the finished_at token of
    probe_results (observation applied, or the latest attempt recorded).
    A visible result with this marker false is still pending.
    """
    if not request_id:
        return False
    try:
        row = conn.execute(
            "SELECT pr.result_id, res.finished_at FROM probe_requests pr"
            " JOIN probe_results res ON res.request_id=pr.id"
            " WHERE pr.id=?", (request_id,)).fetchone()
    except Exception:
        return False
    if row is None:
        return False
    try:
        finished_at = str(row["finished_at"] or "")
        return bool(finished_at) and str(row["result_id"] or "") == \
            finished_at
    except Exception:
        return False


def _await_observation_applied(conn, request_id, timeout_s):
    # type: (sqlite3.Connection, str, float) -> bool
    """Bounded wait for the coordinator's durable applied marker."""
    try:
        bound = max(0.05, float(timeout_s))
    except (TypeError, ValueError):
        bound = OBSERVATION_APPLY_WAIT_S
    deadline = time.monotonic() + bound
    while True:
        if observation_applied(conn, request_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def request_owner_probe(tool_id, op, timeout_s=25.0, subject="api"):
    # type: (str, str, float, str) -> Tuple[str, Dict[str, Any]]
    """API-side helper: enqueue + bounded wait. Returns (status, payload).

    Legacy 2-tuple wrapper; use request_owner_probe_handle when the
    durable request id is needed (POST /tools/{id}/check, W3)."""
    status, payload, _request_id = request_owner_probe_handle(
        tool_id, op, timeout_s=timeout_s, subject=subject)
    return status, payload


def request_owner_probe_handle(tool_id, op, timeout_s=25.0, subject="api",
                               coalesce=True, apply_wait_s=None):
    # type: (str, str, float, str, bool, float) -> Tuple[str, Dict[str, Any], str]
    """API-side helper: durable enqueue (coalescing) + bounded wait.

    Opens/closes its own DB connection (short transactions only). Must
    run in a worker thread, never the event loop. Returns
    (status, payload, request_id); request_id is the durable handle the
    caller can poll (GET /probes/{request_id}) after a timeout.

    RESULT VISIBLE != OBSERVATION APPLIED (W3.1): for an observation op
    (refresh), "ok" is returned only after the coordinator's durable
    applied marker exists (observation_applied). If the result becomes
    visible first, this waits the small explicit apply_wait_s bound
    (default OBSERVATION_APPLY_WAIT_S, never PROBE_OP_TIMEOUT_S) and then
    returns "pending" with the durable request_id instead of a false
    completion. Non-observation ops keep the legacy semantics.
    """
    if op not in OPS:
        return "error", {"reason": "unknown op"}, ""
    try:
        from .config import settings
        from .db import connect
    except Exception:
        return "error", {"reason": "config unavailable"}, ""
    try:
        conn = connect(settings.db_path)
    except Exception:
        return "error", {"reason": "database unavailable"}, ""
    try:
        try:
            request_id = enqueue_probe(conn, subject, tool_id, op,
                                       coalesce=coalesce)
        except ValueError as exc:
            return "error", {"reason": str(exc)[:300]}, ""
        except Exception:
            return "error", {"reason": "enqueue failed"}, ""
        status, payload = await_probe(conn, request_id, timeout_s=timeout_s)
        if status == "ok" and op in OBSERVATION_APPLIED_OPS:
            if apply_wait_s is None:
                apply_wait_s = OBSERVATION_APPLY_WAIT_S
            if not _await_observation_applied(conn, request_id, apply_wait_s):
                return "pending", {
                    "reason": "result stored; observation application"
                              " pending"}, request_id
        return status, payload, request_id
    finally:
        try:
            conn.close()
        except Exception:
            pass


def claim_probe(conn, owner):
    # type: (sqlite3.Connection, str) -> object
    """Dispatcher-side: atomically claim one queued, unexpired request."""
    if not _tables_present(conn):
        return None
    now = _utcnow()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM probe_requests WHERE state='queued'"
            " AND claim_deadline>? ORDER BY created_at LIMIT 1",
            (now,)).fetchone()
        if row is None:
            try:
                conn.execute("COMMIT")
            except Exception:
                pass
            return None
        cur = conn.execute(
            "UPDATE probe_requests SET state='running', owner=?"
            " WHERE id=? AND state='queued'", (owner, row["id"]))
        if cur.rowcount != 1:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            return None
        conn.execute("COMMIT")
        return row
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return None


def finish_probe(conn, request_id, status, payload):
    # type: (sqlite3.Connection, str, str, Dict[str, Any]) -> bool
    """Dispatcher-side: store the typed result (payload pre-sanitized).

    The stored document is always valid UTF-8 JSON, bounded by
    PROBE_RESULT_MAX_BYTES serialized bytes and never truncated: an
    over-bound result is replaced by the small typed
    `probe_result_too_large` error document with status "error", and a
    serialization failure by `probe_result_serialization_failed`.

    Returns True only when the result is durably stored and the request
    marked done (one transaction). Observation application is a separate
    coordinator step (observation.apply_probe_result) so a crash between
    the two is recoverable from durable state.
    """
    if not _tables_present(conn):
        return False
    try:
        raw = _serialize_probe_result(payload)
    except Exception:
        # A payload that cannot be serialized is never stored as ``{}``
        # (which would pretend to describe the original result): the
        # typed error is the honest outcome.
        raw, status = _SERIALIZATION_FAILED_DOC, "error"
    else:
        actual_bytes = len(raw.encode("utf-8"))
        if actual_bytes > PROBE_RESULT_MAX_BYTES:
            # Fail closed: never cut serialized JSON. The oversized
            # payload is replaced by the small typed error document.
            status = "error"
            raw = _oversized_result_doc(actual_bytes)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR REPLACE INTO probe_results(request_id,status,"
            "result_json,finished_at) VALUES(?,?,?,?)",
            (request_id, status, raw, _utcnow()))
        conn.execute(
            "UPDATE probe_requests SET state='done' WHERE id=?",
            (request_id,))
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def expire_probes(conn):
    # type: (sqlite3.Connection) -> int
    """Mark past-deadline QUEUED probes expired. Returns count.

    W3: a claimed/running probe is owned by the dispatcher until it
    finishes or restart reconciliation resolves it; wall-clock expiry is
    never allowed to discard a running execution (the execution lease,
    proof-based, bounds exclusion — not this sweep).
    """
    if not _tables_present(conn):
        return 0
    try:
        cur = conn.execute(
            "UPDATE probe_requests SET state='expired' WHERE state='queued'"
            " AND claim_deadline<?", (_utcnow(),))
        conn.commit()
        return int(cur.rowcount or 0)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
