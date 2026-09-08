"""Shared reconciliation decision logic (R04/R07, F02/G02/G03, H02/H03).

One algorithm used by the dispatcher loop, the SSH reconcile command, and
the CLI. Reconciliation ordering (G02 — receipt never overrides a
surviving updater):
  1. inspect canonical unit;
  2. inspect every job-owned phase scope (H03 — stray children that
     carry no hex marker are invisible to the process scan, so each
     scope unit itself must be confirmed stopped);
  3. inspect execution-marked processes via a structured proof (H03);
  4. inspect delegated operations (shared delegated_quiescence proof);
  5. only if all execution proof is quiescent: evaluate receipt;
  6. then reconcile outcome;
  7. then release mutation ownership (separate proven step).

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


def prove_processes(hex_token, full_id="", exclude_pids=()):
    # type: (str, str, object) -> Dict[str, Any]
    """Structured execution-process proof (H03).

    Returns {"ok": bool, "processes": [...], "reason": str}.

    - ok True + processes []: the scan COMPLETED and nothing matched —
      genuine absence.
    - ok True + processes [..]: survivors; each entry carries pid +
      cmdline evidence.
    - ok False: the scan is UNPROVABLE and must hold the reservation:
      /proc enumeration failed, or a live PID's cmdline could not be
      read for any reason OTHER than the process having exited
      (FileNotFoundError/ProcessLookupError/NotADirectoryError mean it
      is gone and cannot be a survivor; PermissionError or any other
      read failure means a process EXISTS whose membership we cannot
      rule out, so absence is unproven). Callers must treat ok False
      exactly like surviving processes — never like [].

    Matches cmdlines containing the hex token AND an execution marker
    (runner module, systemd-run unit, or updater context) so the
    observer (which carries only the dashed UUID) never matches itself.
    Always excludes exclude_pids (default: own pid).
    """
    try:
        excluded = set(exclude_pids or ())
    except Exception:
        excluded = set()
    try:
        import os as _os_mod
        excluded.add(_os_mod.getpid())
    except Exception:
        pass
    if not hex_token:
        return {"ok": True, "processes": [], "reason": "no token"}
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except Exception as exc:
        return {"ok": False, "processes": [],
                "reason": "process enumeration unavailable: %s"
                % str(exc)[:150]}
    found = []  # type: List[Dict[str, Any]]
    for pid in pids:
        try:
            if int(pid) in excluded:
                continue
        except Exception:
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as fh:
                raw = fh.read()
        except FileNotFoundError:
            continue
        except ProcessLookupError:
            continue
        except NotADirectoryError:
            continue
        except PermissionError as exc:
            return {"ok": False, "processes": [],
                    "reason": "process %s unreadable (permission): %s"
                    % (pid, str(exc)[:120])}
        except Exception as exc:
            # Any other read failure on an existing PID leaves
            # membership unprovable: hold, never assume absence.
            return {"ok": False, "processes": [],
                    "reason": "process %s unreadable: %s"
                    % (pid, str(exc)[:120])}
        try:
            cmd = raw.replace(b"\0", b" ").decode(
                "utf-8", errors="replace")
        except Exception:
            return {"ok": False, "processes": [],
                    "reason": "process %s cmdline undecodable" % pid}
        if hex_token not in cmd:
            continue
        if ("backend.app.worker.runner" in cmd
                or "ega-update-job-" in cmd
                or "systemd-run" in cmd):
            found.append({"pid": int(pid), "cmdline": cmd[:300]})
    return {"ok": True, "processes": found,
            "reason": "" if found else "no execution-marked processes"}


def job_processes(hex_token, full_id="", exclude_pids=()):
    # type: (str, str, object) -> List[Dict[str, Any]]
    """Legacy raw scan: best-effort list of matches.

    H03: an unprovable scan and a completed empty scan BOTH surface as
    [] here, so reconcile paths must use prove_processes() instead and
    treat ok False as a hold. Kept for backward-compatible callers that
    already fail closed on ambiguity.
    """
    try:
        proof = prove_processes(hex_token, full_id, exclude_pids)
    except Exception:
        return []
    try:
        return list((proof or {}).get("processes", []) or [])
    except Exception:
        return []


# Job phases that run inside coordinator-owned transient scopes
# (H03). Must equal set(phase_run.PHASES) - {"probe"}: probe scopes are
# owned by probe requests, not jobs. A regression test pins the
# equality so the two definitions cannot drift.
_JOB_PHASES = ("preflight", "backup", "execute", "verify")


def expected_phase_scopes(job_id):
    # type: (str) -> List[str]
    """Every transient scope unit a job can own (H03).

    Scope names derive from owner_env.transient_scope_name; a scope
    that was never created queries as not-found (confirmed stopped),
    so enumerating the full phase set is always safe. Returns [] only
    when the job id itself is unusable (callers then hold).
    """
    try:
        from .owner_env import transient_scope_name
    except Exception:
        return []
    try:
        stem = unit_hex(job_id)
        if not stem:
            return []
        names = []
        for phase in _JOB_PHASES:
            try:
                names.append(transient_scope_name(stem, phase))
            except Exception:
                return []
        return names
    except Exception:
        return []


def phase_scopes_quiescence(job_id, units_mod=None):
    # type: (str, object) -> Tuple[bool, List[Dict[str, Any]], str]
    """Prove every job-owned phase scope is confirmed stopped (H03).

    A stray scope child (e.g. a spawned tool that outlived its worker)
    carries no hex marker, so the process scan cannot see it — only the
    scope unit state proves it gone. Returns
    (quiescent, evidence, reason); any live/starting/stopping/unknown
    scope, any query failure, or an unbuildable scope list blocks.

    units_mod defaults to backend.app.units (imported lazily so this
    module stays import-light); tests inject fakes exposing query_unit.
    """
    if units_mod is None:
        try:
            from . import units as _units_mod
            units_mod = _units_mod
        except Exception:
            return False, [], "unit model unavailable"
    query = getattr(units_mod, "query_unit", None)
    if not callable(query):
        return False, [], "scope query unavailable"
    try:
        scopes = expected_phase_scopes(job_id)
    except Exception as exc:
        return False, [], "scope list unbuildable: %s" % str(exc)[:150]
    if not scopes:
        return False, [], "scope list unbuildable: empty job id"
    evidence = []  # type: List[Dict[str, Any]]
    for scope in scopes:
        try:
            info = query(scope, timeout_s=5)
        except Exception as exc:
            evidence.append({"scope": scope, "state": "unknown",
                             "detail": "query crashed: %s" % str(exc)[:150]})
            continue
        try:
            state = str((info or {}).get("state", "unknown"))
            detail = str((info or {}).get("detail", "") or "")[:200]
        except Exception:
            state, detail = "unknown", "state unreadable"
        evidence.append({"scope": scope, "state": state,
                         "detail": detail})
    for entry in evidence:
        if entry.get("state") != "confirmed_stopped":
            return False, evidence, \
                "phase scope %s is %s" % (
                    entry.get("scope", "?"),
                    entry.get("state", "unknown"))
    return True, evidence, ""


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


def recovery_required_for_outcome(state, receipt=None,
                                    receipt_valid=False, unresolved=False,
                                    prior_state="",
                                    execution_quiescent=False):
    # type: (str, object, bool, bool, str, bool) -> Tuple[bool, str]
    """One canonical reconciliation recovery decision (H05).

    Mutation occurrence alone NEVER implies recovery: a fully proven
    successful terminal outcome with full quiescence is resolved
    (recovery_required=0), otherwise a single success would poison
    admission for every later update. shows_mutation() answers only
    "may mutation have occurred"; THIS answers "is manual
    recovery/review required". Returns (required, reason).

    Policy (all callers — dispatcher, SSH, boot — share it):
    - unresolved prior flag: True (prior uncertainty survives).
    - valid bound receipt with state=succeeded, disposition none,
      applied state=succeeded, execution proven quiescent: False.
      already_current is a success outcome (SUCCESS_OUTCOMES) under
      the same requirements. Any contradiction (disposition not
      none, state mismatch, unproven quiescence): True.
    - valid receipt with state=blocked (mutation positively never
      started): False.
    - disposition required, or mutation evidence on a non-success
      outcome: True.
    - prior/current stage inside the mutation window
      (backup/updating/verifying) without resolving success proof:
      True (interrupted during mutation defaults conservative).
    - positively pre-mutation anchor (accepted/preflight/blocked)
      with no mutation evidence and no required disposition: False.
    - anything else unknown/contradictory: True (fail closed).
    """
    try:
        current = str(state or "")
    except Exception:
        current = ""
    try:
        anchor = str(prior_state or current or "")
    except Exception:
        anchor = current
    if bool(unresolved):
        return True, "unresolved flag set; prior uncertainty survives"
    try:
        valid = bool(receipt_valid) and isinstance(receipt, dict)
    except Exception:
        valid = False
    try:
        rstate = str((receipt or {}).get("state", "") or "") \
            if valid else ""
        disp = str((receipt or {}).get(
            "recovery_disposition", "") or "") if valid else ""
    except Exception:
        rstate, disp = "", ""
    mutated = False
    if valid:
        try:
            from .receipts import shows_mutation as _shows
            mutated = bool(_shows(receipt))
        except Exception:
            mutated = False
    if valid and rstate == "succeeded" and current == "succeeded":
        if disp != "none":
            return True, \
                "success receipt contradicts recovery disposition " \
                "%r" % disp[:50]
        if not execution_quiescent:
            return True, \
                "success without proven execution quiescence"
        return False, \
            "proven success: valid bound receipt, disposition none, " \
            "execution quiescent"
    if valid and rstate == "blocked" and current == "blocked" \
            and disp != "required":
        return False, "blocked before mutation; nothing to recover"
    if disp == "required":
        return True, "receipt requires recovery"
    if valid and mutated and rstate != "succeeded":
        return True, "non-success outcome with mutation evidence"
    if anchor in ("backup", "updating", "verifying"):
        return True, \
            "outcome unresolved inside mutation window (%s)" % anchor
    if anchor in ("accepted", "preflight", "blocked") and \
            not mutated and disp != "required":
        return False, \
            "positively pre-mutation (%s) without mutation evidence" \
            % anchor
    return True, "unknown/contradictory outcome; failing closed"


def recovery_for(job_state, receipt_shows_mutation, unresolved):
    # type: (str, bool, bool) -> bool
    """Legacy recovery helper (H05: single interpretation preserved).

    Thin wrapper over recovery_required_for_outcome() so the old
    call signature keeps its pinned semantics: unresolved always
    requires; the mutation-window states require; pre-mutation states
    without mutation evidence do not. New code must call the canonical
    function directly with the proven outcome.
    """
    try:
        required, _reason = recovery_required_for_outcome(
            job_state, receipt=None, receipt_valid=False,
            unresolved=bool(unresolved), prior_state=job_state,
            execution_quiescent=False)
    except Exception:
        return True
    if bool(receipt_shows_mutation):
        # Mutation evidence without a resolving success proof: the
        # canonical function (no receipt here) already requires for
        # window states; for pre-mutation anchors evidence
        # contradicts the anchor, so require as well.
        try:
            if str(job_state or "") in (
                    "accepted", "preflight", "blocked"):
                return True
        except Exception:
            return True
    return bool(required)
