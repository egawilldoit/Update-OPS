"""Shared reconciliation decision logic (R04/R07, F02/G02/G03).

One algorithm used by the dispatcher loop, the SSH reconcile command, and
the CLI. Reconciliation ordering (G02 — receipt never overrides a
surviving updater):
  1. inspect canonical unit;
  2. inspect execution-marked processes;
  3. inspect delegated operations (shared delegated_quiescence proof);
  4. only if all execution proof is quiescent: evaluate receipt;
  5. then reconcile outcome;
  6. then release mutation ownership (separate proven step).

Unknown at any proof step holds the reservation and the recovery gate.
Never reruns.

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


def delegated_quiescence(conn, job, units_mod=None):
    # type: (object, Dict[str, Any], object) -> Tuple[bool, List[Dict[str, Any]], str]
    """Shared delegated-operation proof (G03).

    Inspects every plan-bound service/delegated operation for the job:
    exact named unit, correct manager tried in order (user, then system
    when the user bus cannot prove the state), ActiveState/SubState/
    MainPID/cgroup evidence where appropriate. Returns
    (quiescent, evidence, reason).

    - No services bound: (True, [], "no delegated services") — vacuous
      quiescence, explicitly recorded.
    - Every service confirmed stopped: (True, evidence, "").
    - Anything live/starting/stopping/unknown, any query failure, or a
      missing/unreadable plan row: (False, evidence, reason).
    - Do NOT infer delegated quiescence from the parent runner having
      disappeared; only per-service proof counts.

    units_mod defaults to backend.app.units (imported lazily so this
    module stays import-light); tests inject fakes.
    """
    if units_mod is None:
        try:
            from . import units as _units_mod
            units_mod = _units_mod
        except Exception:
            return False, [], "unit model unavailable"
    try:
        job_id = str((job or {}).get("id", "") or "")
        plan_id = str((job or {}).get("plan_id", "") or "")
    except Exception:
        return False, [], "job unreadable"
    services = []  # type: List[str]
    try:
        plan = conn.execute("SELECT services FROM plans WHERE id=?",
                            (plan_id,)).fetchone()
    except Exception as exc:
        return False, [], "plan lookup failed: %s" % str(exc)[:150]
    if plan is None:
        return False, [], "immutable plan row missing"
    try:
        import json as _json
        raw_services = _json.loads(plan["services"] or "[]")
        if isinstance(raw_services, list):
            services = [str(s or "") for s in raw_services if str(s or "")]
    except Exception as exc:
        return False, [], "plan services unreadable: %s" % str(exc)[:150]
    if not services:
        return True, [], "no delegated services"
    evidence = []  # type: List[Dict[str, Any]]
    for service in services[:20]:
        entry = {"service": service, "bus": "", "state": "unknown",
                 "detail": ""}  # type: Dict[str, Any]
        proved = False
        for bus, query in (("user", getattr(units_mod, "query_unit", None)),
                           ("system",
                            getattr(units_mod, "query_unit_system", None))):
            if not callable(query):
                continue
            try:
                info = query(service, timeout_s=5)
            except Exception as exc:
                entry["detail"] = "query crashed: %s" % str(exc)[:150]
                continue
            try:
                state = str((info or {}).get("state", "unknown"))
                detail = str((info or {}).get("detail", "") or "")[:200]
            except Exception:
                state, detail = "unknown", "state unreadable"
            if bus == "user" and state == "unknown" and \
                    "identity mismatch" not in detail:
                # Unknown on the user bus may mean system scope: try it
                # before concluding (either bus proving live blocks).
                continue
            entry["bus"] = bus
            entry["state"] = state
            entry["detail"] = detail
            proved = True
            break
        if not proved:
            entry["detail"] = entry["detail"] or \
                "no manager could prove service state"
        evidence.append(entry)
    for entry in evidence:
        if entry.get("state") != "confirmed_stopped":
            return False, evidence, \
                "delegated operation %s is %s" % (
                    entry.get("service", "?"),
                    entry.get("state", "unknown"))
    return True, evidence, ""


def decide(job, unit_info, receipt=None, procs=None, delegated=None):
    # type: (Dict[str, Any], Dict[str, Any], object, object, object) -> Tuple[str, str]
    """Return (action, detail). Actions: live | starting | stopping |
    apply-receipt | mark-interrupted | keep-unknown.

    G02 ordering — a valid receipt proves OUTCOME, never quiescence:
    - live/starting/stopping: keep reservation, touch heartbeat only.
    - unit not confirmed stopped: keep-unknown (never assume).
    - surviving execution-marked processes (or unprovable process
      scan, procs=None): keep-unknown even with a perfect receipt.
    - delegated operations not proven quiescent (delegated None or
      quiescent False): keep-unknown even with a perfect receipt.
    - apply-receipt: unit confirmed stopped AND no processes AND
      delegated quiescent AND receipt valid (caller validates binding
      + applies atomically).
    - mark-interrupted: all execution proof quiescent, no valid
      receipt. Never reruns.
    """
    state = str((unit_info or {}).get("state", "unknown"))
    if state == "live":
        return "live", "unit live"
    if state == "starting":
        return "starting", "unit starting; reservation held"
    if state == "stopping":
        return "stopping", "unit stopping; reservation held"
    if state != "confirmed_stopped":
        return "keep-unknown", "unit state %s; holding reservation" % state
    # Unit is confirmed stopped. Execution-marked processes override ANY
    # receipt from here on (G02): a valid receipt proves outcome, never
    # that the updater is gone. An unprovable scan (None) also holds.
    if procs is None:
        return "keep-unknown", \
            "process proof unavailable; holding reservation"
    try:
        remaining = list(procs or [])
    except Exception:
        return "keep-unknown", \
            "process evidence unreadable; holding reservation"
    if remaining:
        return "keep-unknown", \
            "unit stopped but %d execution-marked processes remain" \
            % len(remaining)
    # Delegated operations override receipts the same way (G02/G03).
    if not isinstance(delegated, dict) or \
            delegated.get("quiescent", False) is not True:
        try:
            reason = str((delegated or {}).get("reason", "") or
                         "delegated proof missing")
        except Exception:
            reason = "delegated proof missing"
        return "keep-unknown", \
            "delegated operations unproven (%s); holding reservation" \
            % reason[:200]
    if isinstance(receipt, dict) and receipt.get("_valid"):
        return "apply-receipt", \
            "execution quiescent (unit stopped, no processes, delegated " \
            "stopped); valid receipt present"
    return "mark-interrupted", \
        "execution quiescent without completion proof; never rerun"


def recovery_for(job_state, receipt_shows_mutation, unresolved):
    # type: (str, bool, bool) -> bool
    """Recovery required when mutation may have occurred."""
    if unresolved:
        return True
    if receipt_shows_mutation:
        return True
    return job_state in ("backup", "updating", "verifying")
