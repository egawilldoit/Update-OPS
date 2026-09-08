"""One transactional state-transition service (R05). Python 3.10 compatible.

Every job state change flows through transition_tx: a single explicit
BEGIN IMMEDIATE transaction persisting state, step, attempt ownership,
reservation disposition, recovery flag, error, versions, installation
outcome, checks, event, and evidence status together. Guards enforce
expected prior state and attempt nonce; commit failures propagate as
TxCommitError (callers must preserve receipts/evidence and must NOT treat
the admission gate as released).

Never hold the transaction across a subprocess: callers prepare all values
first, then call once.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

TERMINAL_STATES = ("succeeded", "blocked", "failed", "health_failed",
                   "interrupted")


class TxError(Exception):
    """Base state-transition failure."""


class TxGuardError(TxError):
    """Expected prior state / attempt did not hold; nothing was written."""


class TxCommitError(TxError):
    """Commit failed after writes; caller must assume unknown persistence."""


def _columns(conn):
    # type: (sqlite3.Connection) -> set
    try:
        rows = conn.execute("PRAGMA table_info(jobs)").fetchall()
        return {str(r["name"]) for r in rows}
    except Exception:
        return set()


def transition_tx(conn, job_id, to_state, step="", expect_states=None,
                  expect_nonce="", update=None, event=None,
                  event_detail="", checks=None, tool_id=""):
    # type: (...) -> Dict[str, Any]
    """Atomic guarded transition. Returns the new job row as a dict.

    update: extra {column: value} applied atomically (validated against
    the real jobs columns; unknown columns raise TxGuardError).
    checks: list of {name, result, mandatory, summary} inserted atomically.
    event: event_type string (default: to_state).
    """
    from .schemas import utcnow_iso

    if not job_id or not to_state:
        raise TxGuardError("job_id and to_state are required")
    expect = tuple(expect_states or ())
    extra = dict(update or {})
    now = utcnow_iso()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise TxGuardError("unknown job: %s" % job_id)
        current = dict(row)
        if expect and current.get("state") not in expect:
            conn.execute("ROLLBACK")
            raise TxGuardError(
                "expected state %r, found %r" % (list(expect),
                                                 current.get("state")))
        if expect_nonce:
            stored = current.get("dispatch_nonce", "") or ""
            if not stored or stored != expect_nonce:
                conn.execute("ROLLBACK")
                raise TxGuardError("attempt ownership mismatch")
        cols = _columns(conn)
        sets = ["state=?", "step=?"]
        args = [to_state, step or to_state]  # type: List[Any]
        if to_state == "preflight" and not current.get("started_at"):
            sets.append("started_at=?")
            args.append(now)
        if to_state in TERMINAL_STATES:
            sets.append("finished_at=?")
            args.append(now)
        for key, value in extra.items():
            if key in ("id", "state", "step"):
                conn.execute("ROLLBACK")
                raise TxGuardError("reserved column in update: %s" % key)
            if key not in cols:
                conn.execute("ROLLBACK")
                raise TxGuardError("unknown jobs column: %s" % key)
            sets.append("%s=?" % key)
            args.append(value)
        args.append(job_id)
        conn.execute("UPDATE jobs SET %s WHERE id=?" % ",".join(sets), args)
        etype = event or to_state
        conn.execute(
            "INSERT INTO events(job_id,created_at,event_type,detail)"
            " VALUES(?,?,?,?)",
            (job_id, now, str(etype)[:100],
             str(event_detail or "")[:1000]))
        for item in checks or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", ""))[:200]
            result = str(item.get("result", "unknown"))
            if result not in ("pass", "fail", "unknown",
                              "not_applicable"):
                result = "unknown"
            try:
                mandatory = 1 if item.get("mandatory", True) else 0
            except Exception:
                mandatory = 1
            summary = str(item.get("summary", ""))[:1000]
            conn.execute(
                "INSERT INTO checks(tool_id,job_id,name,result,mandatory,"
                "summary,created_at) VALUES(?,?,?,?,?,?,?)",
                (tool_id or current.get("tool_id", ""), job_id, name,
                 result, mandatory, summary, now))
        try:
            conn.execute("COMMIT")
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise TxCommitError("commit failed: %s" % exc)
        final = conn.execute("SELECT * FROM jobs WHERE id=?",
                             (job_id,)).fetchone()
        return dict(final) if final is not None else dict(current)
    except TxError:
        raise
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise TxCommitError("transition failed: %s" % exc)
