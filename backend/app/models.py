"""Typed SQLite row helpers. Thin mapping over db.py tables; no subprocess use."""
from __future__ import annotations

from typing import Any, Dict


def row_to_dict(row):
    # type: (Any) -> Dict[str, Any]
    return dict(row) if row is not None else {}


def check_state(health, checked_at_iso, now_iso, stale_after_s=300):
    # type: (str, str, str, int) -> str
    """Health older than 5 minutes is labeled stale (SPEC §11)."""
    if not checked_at_iso or not health or health == "unknown":
        return "unknown"
    try:
        from datetime import datetime
        checked = datetime.fromisoformat(checked_at_iso)
        now = datetime.fromisoformat(now_iso)
        if (now - checked).total_seconds() > stale_after_s:
            return "stale"
    except Exception:
        return "unknown"
    return health
