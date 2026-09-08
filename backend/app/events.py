"""Central safe event persistence helper (F11). Python 3.10 compatible.

All diagnostic event writes go through record_event(): detail is
sanitized with the single known-secret source; sanitizer failure
persists a fixed safe marker (never raw data); a failed event write
itself fails the call loudly (returns False) instead of pretending.

This helper never raises.
"""
from __future__ import annotations

import sqlite3

SUPPRESSED_EVENT_DETAIL = "[event suppressed: sanitization failed]"


def record_event(conn, job_id, event_type, detail=""):
    # type: (sqlite3.Connection, str, str, str) -> bool
    """Persist one sanitized event. True on success, False on any failure
    (callers decide whether that failure is fatal for their operation)."""
    try:
        from .config import load_secret_values, settings
        from .sanitize import sanitize_text
        secrets = load_secret_values(settings)
    except Exception:
        secrets = None
    if secrets is None:
        clean = SUPPRESSED_EVENT_DETAIL
    else:
        try:
            clean = sanitize_text(str(detail or ""), secrets)[:1000]
        except Exception:
            clean = SUPPRESSED_EVENT_DETAIL
    try:
        from .schemas import utcnow_iso
        now = utcnow_iso()
    except Exception:
        return False
    try:
        conn.execute(
            "INSERT INTO events(job_id,created_at,event_type,detail)"
            " VALUES(?,?,?,?)",
            (str(job_id or ""), now, str(event_type or "")[:100], clean))
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
