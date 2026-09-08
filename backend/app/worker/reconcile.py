"""SSH-only reconcile command (R07). Python 3.10 compatible.

Usage:
  python -m backend.app.worker.reconcile --job-id <uuid> [--clear-recovery]
  [--db-path /var/lib/ega-update/state.db]

Single shared algorithm (reconcile_core.decide + units.query_unit +
receipts v2 binding + tx.transition_tx) — never a second recovery
algorithm. Observer self-exclusion via canonical unit hex (the observer
carries only the dashed UUID, never the unit hex). Receipts load as JSON
first and validate; NO unvalidated fallback promotion. Clearing recovery
additionally terminalizes abandoned nonterminal rows atomically and runs
a genuine bounded owner-side inspection for the report.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from backend.app.db import connect

DEFAULT_DB = "/var/lib/ega-update/state.db"


def _db_path(args_db):
    # type: (str) -> str
    if args_db:
        return args_db
    env = os.environ.get("EGA_DB_PATH", "")
    if env:
        return env
    cfg_file = os.environ.get("EGA_CONFIG_FILE", "/etc/ega-update/config.json")
    try:
        with open(cfg_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        val = data.get("db_path", "")
        if isinstance(val, str) and val:
            return val
    except Exception:
        pass
    return DEFAULT_DB


def _utcnow():
    # type: () -> str
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _genuine_inspection(tool_id):
    # type: (str) -> str
    """Bounded owner-side inspection for the report (R07).

    Runs as ubuntu over SSH: genuine adapter inspect, bounded, read-only.
    Never used for state transitions — only human-readable evidence.
    """
    try:
        from backend.app.adapters import registry as _registry
        adapter = _registry.get_adapter(tool_id)
    except Exception as exc:
        return "adapter unavailable: %s" % exc
    try:
        inspection = adapter.inspect()
    except Exception as exc:
        return "inspect failed: %s" % str(exc)[:200]
    try:
        return "version=%s fingerprint=%s identity=%s" % (
            getattr(inspection, "version", "") or "-",
            str(getattr(inspection, "fingerprint", "") or "")[:16],
            str(getattr(inspection, "install_identity", "") or "")[:80])
    except Exception:
        return "inspect unparseable"


def _delegated_proof(conn, job):
    # type: (object, dict) -> dict
    """Shared delegated-operation proof for SSH reconcile (G03).

    Same reconcile_core.delegated_quiescence() the dispatcher uses —
    one algorithm, not two. Prints per-service evidence for the
    operator and returns the {quiescent, evidence, reason} mapping
    decide() requires.
    """
    try:
        from backend.app import reconcile_core as _rc
        quiescent, evidence, reason = _rc.delegated_quiescence(conn, job)
    except Exception as exc:
        return {"quiescent": False, "evidence": [],
                "reason": "delegated proof crashed: %s" % exc}
    try:
        if evidence:
            print("delegated operations:")
            for item in evidence[:20]:
                print("  service=%s bus=%s state=%s %s" % (
                    item.get("service", "?"), item.get("bus", "?"),
                    item.get("state", "?"), item.get("detail", "")[:120]))
        else:
            print("delegated operations: %s" % (reason or "none bound"))
    except Exception:
        pass
    return {"quiescent": bool(quiescent), "evidence": evidence,
            "reason": reason}


def main(argv=None):
    # type: (list) -> int
    from backend.app import reconcile_core as _rc
    from backend.app import units as _units
    from backend.app.receipts import (apply_receipt, check_binding,
                                      load_receipt_file,
                                      validate_receipt)
    from backend.app.tx import TxError, transition_tx

    ap = argparse.ArgumentParser(
        description="SSH-only job reconcile (shared algorithm + gated "
                    "recovery clear).")
    ap.add_argument("--job-id", required=True,
                    help="Job UUID (required, incl. non-interactive use).")
    ap.add_argument("--clear-recovery", action="store_true",
                    help="Clear recovery_required ONLY after proving no "
                         "updater remains (terminalizes abandoned rows).")
    ap.add_argument("--db-path", default="",
                    help="SQLite path (default: EGA_DB_PATH / config / %s)."
                    % DEFAULT_DB)
    args = ap.parse_args(argv)
    if not args.job_id:
        ap.error("--job-id is required (refusing to run without it)")

    db_path = _db_path(args.db_path)
    try:
        conn = connect(db_path)
    except Exception as exc:
        print("ERROR: cannot open db: %s" % exc)
        return 2
    try:
        job = conn.execute("SELECT * FROM jobs WHERE id=?",
                           (args.job_id,)).fetchone()
    except Exception as exc:
        print("ERROR: cannot read job: %s" % exc)
        return 2
    if job is None:
        print("ERROR: unknown job id: %s" % args.job_id)
        return 2
    job_d = dict(job)
    unit = str(job_d.get("canonical_unit", "")
               or job_d.get("runner_unit", "") or "")
    if not unit:
        unit = _rc.canonical_unit(args.job_id)
    state = str(job_d.get("state", ""))
    recovery = bool(job_d.get("recovery_required", 0))
    print("job: %s tool=%s state=%s recovery_required=%s"
          % (job_d["id"], job_d.get("tool_id", ""), state, int(recovery)))
    print("runner_unit: %s" % unit)
    print("heartbeat (informational only, never clears recovery): %s"
          % (job_d.get("heartbeat", "") or "(none)"))

    info = _units.query_unit(unit, timeout_s=10)
    print("unit state: %s (active=%s sub=%s main_pid=%s)" % (
        info.get("state"), info.get("active_state"),
        info.get("sub_state"), info.get("main_pid")))
    if info.get("detail"):
        print("unit detail: %s" % info["detail"])

    # H03: structured process proof — an unprovable scan (None) holds
    # via decide(), exactly like surviving processes.
    try:
        _proof = _rc.prove_processes(_rc.unit_hex(args.job_id),
                                     args.job_id)
    except Exception:
        _proof = {"ok": False, "processes": [], "reason": "proof crashed"}
    try:
        procs = list(_proof.get("processes", []) or []) \
            if _proof.get("ok", False) else None
    except Exception:
        procs = None
    if procs:
        print("live execution-marked processes: %d (self excluded)"
              % len(procs))
        for item in procs[:20]:
            print("  pid=%s cmd=%s" % (item["pid"], item["cmdline"]))
    elif procs is None:
        print("live execution-marked processes: UNPROVABLE (%s)" % (
            (_proof.get("reason", "") or "scan failed")[:150]))
    else:
        print("live execution-marked processes: 0")

    # Receipt loads as JSON FIRST, then validates; never promoted without
    # validation and binding (R07: no unvalidated fallback).
    try:
        from backend.app.config import settings as _settings
        receipt_path = os.path.join(
            getattr(_settings, "log_dir", "/var/lib/ega-update/logs"),
            "%s.receipt.json" % args.job_id)
    except Exception:
        receipt_path = "/var/lib/ega-update/logs/%s.receipt.json" \
            % args.job_id
    ok, data, reason = load_receipt_file(receipt_path)
    receipt_view = None
    if ok and isinstance(data, dict):
        try:
            plan = conn.execute("SELECT * FROM plans WHERE id=?",
                                (job_d.get("plan_id", ""),)).fetchone()
            plan_row = dict(plan) if plan is not None else None
        except Exception:
            plan_row = None
        bound, why = check_binding(data, job_d, plan_row, args.job_id)
        if bound:
            receipt_view = dict(data)
            receipt_view["_valid"] = True
            print("validated bound receipt (job+plan+attempt): state=%s exit=%s" % (
                data.get("state", "?"), data.get("exit_code", "?")))
        else:
            print("receipt present but UNBOUND (%s); never applied" % why)
            ok = False
    else:
        print("receipt: %s" % (reason or "absent"))

    # H03: every job-owned phase scope must itself be confirmed
    # stopped before any receipt application or ownership release —
    # hex-less stray children are invisible to the process scan. A
    # non-quiescent scope forces the keep-unknown verdict below.
    _scopes_quiescent = False
    _scopes_reason = "scope proof not computed"
    if str(info.get("state", "")) == "confirmed_stopped":
        try:
            _scopes_quiescent, _scope_ev, _scopes_reason = \
                _rc.phase_scopes_quiescence(args.job_id)
        except Exception as exc:
            _scopes_quiescent = False
            _scopes_reason = "phase scope proof crashed: %s" % exc
        try:
            print("phase scopes: %s" % (
                "all confirmed stopped" if _scopes_quiescent
                else "HOLD (%s)" % (_scopes_reason or "?")[:150]))
            for item in (_scope_ev or [])[:8]:
                print("  scope=%s state=%s" % (
                    item.get("scope", "?"), item.get("state", "?")))
        except Exception:
            pass

    action, detail = _rc.decide(job_d, info, receipt_view, procs,
                                _delegated_proof(conn, job_d))
    if str(info.get("state", "")) == "confirmed_stopped" and \
            not _scopes_quiescent:
        action, detail = "keep-unknown", \
            "phase scopes unproven (%s); holding reservation" % (
                _scopes_reason or "scope proof missing")[:200]
        print("decision: %s — %s" % (action, detail))
    else:
        print("decision: %s — %s" % (action, detail))
    try:
        from backend.app.events import record_event
        if not record_event(
                conn, args.job_id, "reconcile",
                "unit=%s state=%s receipt=%s procs=%d decision=%s" % (
                    unit, info.get("state"), int(bool(receipt_view)),
                    len(procs), action)):
            print("WARN: could not record reconcile event")
    except Exception as exc:
        print("WARN: could not record reconcile event: %s" % exc)

    print("genuine owner inspection (read-only, bounded): %s"
          % _genuine_inspection(str(job_d.get("tool_id", ""))))

    if action in ("live", "starting", "stopping"):
        print("VERDICT: UNRESOLVED — execution still present; "
              "recovery_required stays set. Re-run after quiescence.")
        return 1
    if action == "keep-unknown":
        print("VERDICT: UNRESOLVED — unit state unknown; reservation and "
              "recovery gate stay. Investigate the user manager, then "
              "re-run. Never clear on heartbeat age.")
        return 1
    # Unit confirmed stopped from here on.
    if action == "apply-receipt" and receipt_view is not None:
        try:
            applied = apply_receipt(conn, receipt_view, args.job_id)
            print("applied bound receipt: state=%s" % applied)
            state = applied
        except ValueError as exc:
            print("ERROR: receipt apply refused: %s" % exc)
            return 1
        # H05: ONE canonical recovery decision — proven success is
        # resolved; mutation evidence alone never implies recovery.
        needs_recovery, _rec_reason = _rc.recovery_required_for_outcome(
            state, receipt_view, receipt_valid=True,
            unresolved=bool(job_d.get("unresolved", 0)),
            prior_state=str(job_d.get("state", "")),
            execution_quiescent=True)
        if needs_recovery and not recovery:
            try:
                from backend.app.events import record_event
                conn.execute(
                    "UPDATE jobs SET recovery_required=1 WHERE id=?",
                    (args.job_id,))
                if not record_event(
                        conn, args.job_id, "recovery_required",
                        "reconciled %s with mutation evidence" % applied):
                    raise OSError("event write failed")
                conn.commit()
                recovery = True
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                print("ERROR: could not set recovery: %s" % exc)
                return 1
        # F02: ownership release only after the proof above (confirmed
        # stopped unit + no execution-marked processes). Terminal state
        # alone never releases; failure stays loud (return 1).
        try:
            from backend.app.tx import release_ownership
            release_ownership(
                conn, args.job_id, expect_states=[applied],
                event="ownership_released",
                event_detail="ssh reconcile: unit confirmed stopped; "
                             "no updater processes")
            print("ownership released (mutation lease disposed)")
        except Exception as exc:
            print("ERROR: ownership release failed (lease still held; "
                  "admission stays blocked): %s" % exc)
            return 1
    elif action == "mark-interrupted":
        # H05: the canonical decision (receipt only when bound+valid).
        needs_recovery, _rec_reason = _rc.recovery_required_for_outcome(
            state, receipt_view, receipt_valid=bool(receipt_view),
            unresolved=bool(job_d.get("unresolved", 0)),
            prior_state=str(job_d.get("state", "")),
            execution_quiescent=True)
        if state in ("succeeded", "blocked", "failed", "health_failed",
                     "interrupted"):
            # Already terminal: prove-and-release only, no state change.
            pass
        else:
            try:
                transition_tx(
                    conn, args.job_id, "interrupted", step="interrupted",
                    expect_states=["accepted", "preflight", "backup",
                                   "updating", "verifying", "interrupted"],
                    update={"error_code": "interrupted",
                            "error_detail": "ssh reconcile: %s" % detail[:400],
                            "recovery_required": 1 if needs_recovery else 0,
                            "unresolved": 0},
                    event="interrupted", event_detail=detail[:500])
                print("terminalized abandoned job as interrupted "
                      "(recovery=%s)" % int(needs_recovery))
                state = "interrupted"
                recovery = recovery or needs_recovery
            except TxError as exc:
                print("ERROR: cannot terminalize: %s" % exc)
                return 1
        # F02: ownership release only after the proof above.
        try:
            from backend.app.tx import release_ownership
            release_ownership(
                conn, args.job_id, expect_states=[state],
                event="ownership_released",
                event_detail="ssh reconcile: unit confirmed stopped; "
                             "no updater processes")
            print("ownership released (mutation lease disposed)")
        except Exception as exc:
            print("ERROR: ownership release failed (lease still held; "
                  "admission stays blocked): %s" % exc)
            return 1

    if args.clear_recovery:
        if not recovery:
            print("VERDICT: no updater remains; recovery_required already "
                  "clear. Nothing to do.")
            return 0
        try:
            from backend.app.events import record_event
            conn.execute(
                "UPDATE jobs SET recovery_required=0 WHERE id=?",
                (args.job_id,))
            if not record_event(
                    conn, args.job_id, "recovered",
                    "ssh reconcile: no updater remains (unit=%s)" % unit):
                raise OSError("event write failed")
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            print("ERROR: failed to clear recovery: %s" % exc)
            return 1
        print("VERDICT: recovery_required CLEARED after proving no updater "
              "remains. Terminal history preserved; run Check again.")
        return 0

    if recovery or state in ("accepted", "preflight", "backup", "updating",
                             "verifying", "interrupted"):
        print("VERDICT: UNRESOLVED — no updater remains, but recovery/state "
              "needs an explicit `--clear-recovery` decision after "
              "inspecting the installation.")
        return 1
    print("VERDICT: resolved — no updater remains and no recovery block.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
