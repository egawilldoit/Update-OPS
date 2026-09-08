"""Shared reconciliation decision logic (R04/R07). Python 3.10 compatible.

One algorithm used by the dispatcher loop, the SSH reconcile command, and
the CLI. Decisions are explicit: live | starting | stopping keep the
reservation; confirmed_stopped reconciles receipts or marks interrupted;
unknown NEVER releases the reservation and never clears recovery.

Observer self-exclusion: process evidence matches the canonical unit hex
(uuid without dashes), which never appears in the observer's own argv
(the observer carries the dashed job UUID only).
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple


def unit_hex(job_id):
    # type: (str) -> str
    try:
        return str(job_id or "").replace("-", "")
    except Exception:
        return ""


def canonical_unit(job_id):
    # type: (str) -> str
    stem = unit_hex(job_id)
    if not stem:
        return ""
    return "ega-update-job-%s.service" % stem


def job_processes(hex_token, full_id="", exclude_pids=()):
    # type: (str, str, object) -> List[Dict[str, Any]]
    """Read-only /proc scan keyed on the unit hex + runner markers.

    Matches cmdlines containing the hex token AND an execution marker
    (runner module, systemd-run unit, or updater context) so the
    observer (which carries only the dashed UUID) never matches itself.
    Always excludes exclude_pids (default: own pid).
    """
    found = []  # type: List[Dict[str, Any]]
    try:
        me = os.getpid()
    except Exception:
        me = -1
    excluded = set(exclude_pids or ())
    excluded.add(me)
    if not hex_token:
        return found
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except Exception:
        return found
    for pid in pids:
        try:
            if int(pid) in excluded:
                continue
        except Exception:
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode(
                    "utf-8", errors="replace")
        except Exception:
            continue
        if hex_token not in cmd:
            continue
        if ("backend.app.worker.runner" in cmd
                or "ega-update-job-" in cmd
                or "systemd-run" in cmd):
            found.append({"pid": int(pid), "cmdline": cmd[:300]})
    return found


def decide(job, unit_info, receipt=None, procs=None):
    # type: (Dict[str, Any], Dict[str, Any], object, object) -> Tuple[str, str]
    """Return (action, detail). Actions: live | starting | stopping |
    apply-receipt | mark-interrupted | keep-unknown.

    - live/starting/stopping: keep reservation, touch heartbeat only.
    - apply-receipt: unit confirmed stopped AND receipt valid (caller
      validates binding + applies atomically).
    - mark-interrupted: unit confirmed stopped, no valid receipt.
    - keep-unknown: every other case (bus failure, identity mismatch,
      MainPID alive, cgroup members, ambiguous launch). Reservation and
      recovery gate stay.
    """
    state = str((unit_info or {}).get("state", "unknown"))
    if state == "live":
        return "live", "unit live"
    if state == "starting":
        return "starting", "unit starting; reservation held"
    if state == "stopping":
        return "stopping", "unit stopping; reservation held"
    if state == "confirmed_stopped":
        if isinstance(receipt, dict) and receipt.get("_valid"):
            return "apply-receipt", "unit stopped; valid receipt present"
        if procs:
            return "keep-unknown", \
                "unit stopped but %d execution-marked processes remain" \
                % len(procs)
        return "mark-interrupted", \
            "unit stopped without completion proof; never rerun"
    return "keep-unknown", "unit state %s; holding reservation" % state


def recovery_for(job_state, receipt_shows_mutation, unresolved):
    # type: (str, bool, bool) -> bool
    """Recovery required when mutation may have occurred."""
    if unresolved:
        return True
    if receipt_shows_mutation:
        return True
    return job_state in ("backup", "updating", "verifying")
