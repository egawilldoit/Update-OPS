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


SERVICE_SCOPES = ("user", "system")

_UNIT_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.@:-")


def parse_service_ref(value):
    # type: (object) -> Tuple[str, str]
    """Parse ONE canonical delegated-service reference (H02).

    Returns (scope, unit) with scope in {"user", "system"} and unit a
    non-empty valid systemd unit name. Accepts "scope:unit" and legacy
    bare "unit" (bare means user scope: that is how the console has
    always managed non-prefixed units, e.g. OpenCode's user service).
    Anything else — empty, unknown scope, empty unit, illegal unit
    characters, extra colons — returns ("", ""): malformed entries are
    UNKNOWN and block, never guessed.
    """
    try:
        text = str(value or "").strip()
    except Exception:
        return "", ""
    if not text:
        return "", ""
    scope = "user"
    unit = text
    if ":" in text:
        parts = text.split(":")
        if len(parts) != 2:
            return "", ""
        scope, unit = parts[0].strip(), parts[1].strip()
        if scope not in SERVICE_SCOPES:
            return "", ""
    if not unit:
        return "", ""
    try:
        if any(ch.isspace() for ch in unit):
            return "", ""
        if "/" in unit or "\\" in unit or "\0" in unit:
            return "", ""
        if any(ch not in _UNIT_CHARS for ch in unit):
            return "", ""
    except Exception:
        return "", ""
    return scope, unit


def job_processes(hex_token, full_id="", exclude_pids=()):
    # type: (str, str, object) -> List[Dict[str, Any]]
    """Legacy raw scan: best-effort list, [] on ANY failure (H03: this
    ambiguity is why callers must use prove_processes() instead)."""

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
    """Shared delegated-operation proof (G03, H02).

    Inspects every plan-bound service/delegated operation for the job
    through its DECLARED scope only (H02): a system-scoped service is
    queried solely on the system manager, a user-scoped one solely on
    the ubuntu user manager. No user-then-system fallback guessing — a
    scope-bound service already declares its authority domain, and
    querying the wrong manager turns real activity into false
    not-found/stopped evidence. Returns (quiescent, evidence, reason).

    - No services bound: (True, [], "no delegated services") — vacuous
      quiescence, explicitly recorded.
    - Every service confirmed stopped on its own manager:
      (True, evidence, "").
    - Anything live/starting/stopping/unknown, any query failure, any
      malformed service ref, or a missing/unreadable plan row:
      (False, evidence, reason).
    - Do NOT infer delegated quiescence from the parent runner having
      disappeared; only per-service proof counts.

    units_mod defaults to backend.app.units (imported lazily so this
    module stays import-light); tests inject fakes exposing
    query_unit / query_unit_system.
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
    query_user = getattr(units_mod, "query_unit", None)
    query_system = getattr(units_mod, "query_unit_system", None)
    evidence = []  # type: List[Dict[str, Any]]
    for raw in services[:20]:
        entry = {"service": str(raw or ""), "scope": "", "unit": "",
                 "bus": "", "state": "unknown",
                 "detail": ""}  # type: Dict[str, Any]
        # H02: structural parse first. A malformed ref is UNKNOWN and
        # blocks — the manager is never consulted with a guessed name.
        scope, unit = parse_service_ref(raw)
        if not scope or not unit:
            entry["detail"] = "malformed service reference"
            evidence.append(entry)
            continue
        entry["scope"] = scope
        entry["unit"] = unit
        entry["bus"] = scope
        query = query_user if scope == "user" else query_system
        if not callable(query):
            entry["detail"] = "%s manager query unavailable" % scope
            evidence.append(entry)
            continue
        try:
            info = query(unit, timeout_s=5)
        except Exception as exc:
            entry["detail"] = "query crashed: %s" % str(exc)[:150]
            evidence.append(entry)
            continue
        try:
            entry["state"] = str((info or {}).get("state", "unknown"))
            entry["detail"] = str(
                (info or {}).get("detail", "") or "")[:200]
        except Exception:
            entry["state"] = "unknown"
            entry["detail"] = "state unreadable"
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
