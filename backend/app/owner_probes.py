"""Typed owner-side probe boundary (R01/R16). Python 3.10 compatible.

The API (ega-update, ProtectHome) never executes installation probes. It
writes typed probe requests (tool_id + op only — never argv/paths/env) to
probe_requests; the dispatcher (ubuntu, full owner env) executes them via
adapters and writes typed results to probe_results. The API waits bounded
in a worker thread (never the event loop) and falls back to cached+stale.

Ops: inspect | discover | activity | plan | verify | refresh
(refresh = inspect+discover+activity+verify bundle for Check again).
Contending ops (activity/plan/verify/refresh) are deferred while any
nonterminal job exists — probes must not race mutation.

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
CONTENDING_OPS = ("activity", "plan", "verify", "refresh")


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


def enqueue_probe(conn, subject, tool_id, op, arg_json="{}"):
    # type: (sqlite3.Connection, str, str, str, str) -> str
    """Insert a probe request. Raises ValueError on bad op."""
    if op not in OPS:
        raise ValueError("unknown probe op: %s" % op)
    if not _tables_present(conn):
        raise ValueError("probe queue unavailable (migrate first)")
    request_id = str(uuid.uuid4())
    try:
        conn.execute("BEGIN IMMEDIATE")
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
    try:
        conn.execute("UPDATE probe_requests SET state='expired'"
                     " WHERE id=? AND state IN ('queued','running')",
                     (request_id,))
        conn.commit()
    except Exception:
        pass
    return "timeout", {}


def request_owner_probe(tool_id, op, timeout_s=25.0, subject="api"):
    # type: (str, str, float, str) -> Tuple[str, Dict[str, Any]]
    """API-side helper: enqueue + bounded wait. Opens/closes its own DB
    connection (short reads only). Must run in a worker thread, never the
    event loop. Returns (status, payload)."""
    if op not in OPS:
        return "error", {"reason": "unknown op"}
    try:
        from .config import settings
        from .db import connect
    except Exception:
        return "error", {"reason": "config unavailable"}
    try:
        conn = connect(settings.db_path)
    except Exception:
        return "error", {"reason": "database unavailable"}
    try:
        try:
            request_id = enqueue_probe(conn, subject, tool_id, op)
        except ValueError as exc:
            return "error", {"reason": str(exc)[:300]}
        except Exception:
            return "error", {"reason": "enqueue failed"}
        return await_probe(conn, request_id, timeout_s=timeout_s)
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
    # type: (sqlite3.Connection, str, str, Dict[str, Any]) -> None
    """Dispatcher-side: store the typed result (payload pre-sanitized)."""
    if not _tables_present(conn):
        return
    try:
        raw = json.dumps(payload or {}, sort_keys=True, default=str)
    except Exception:
        raw, status = "{}", "error"
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR REPLACE INTO probe_results(request_id,status,"
            "result_json,finished_at) VALUES(?,?,?,?)",
            (request_id, status, raw[:200000], _utcnow()))
        conn.execute(
            "UPDATE probe_requests SET state='done' WHERE id=?",
            (request_id,))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def expire_probes(conn):
    # type: (sqlite3.Connection) -> int
    """Mark past-deadline queued/running probes expired. Returns count."""
    if not _tables_present(conn):
        return 0
    try:
        cur = conn.execute(
            "UPDATE probe_requests SET state='expired' WHERE state IN"
            " ('queued','running') AND claim_deadline<?", (_utcnow(),))
        conn.commit()
        return int(cur.rowcount or 0)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
