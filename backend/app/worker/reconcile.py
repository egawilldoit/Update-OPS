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


def main(argv=None):
    # type: (list) -> int
    from backend.app import reconcile_core as _rc
    from backend.app import units as _units
    from backend.app.receipts import (apply_receipt, check_binding,
                                      load_receipt_file, shows_mutation,
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

    try:
        procs = _rc.job_processes(_rc.unit_hex(args.job_id), args.job_id)
    except Exception:
        procs = []
    if procs:
        print("live execution-marked processes: %d (self excluded)"
              % len(procs))
        for item in procs[:20]:
            print("  pid=%s cmd=%s" % (item["pid"], item["cmdline"]))
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

    action, detail = _rc.decide(job_d, info, receipt_view, procs)
    print("decision: %s — %s" % (action, detail))
    try:
        conn.execute(
            "INSERT INTO events(job_id,created_at,event_type,detail)"
            " VALUES(?,?,?,?)",
            (args.job_id, _utcnow(), "reconcile",
             "unit=%s state=%s receipt=%s procs=%d decision=%s" % (
                 unit, info.get("state"), int(bool(receipt_view)),
                 len(procs), action)))
        conn.commit()
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
        needs_recovery = _rc.recovery_for(
            state, shows_mutation(receipt_view),
            bool(job_d.get("unresolved", 0)))
        if str(receipt_view.get("recovery_disposition", "")) == "required":
            needs_recovery = True
        if needs_recovery and not recovery:
            try:
                conn.execute(
                    "UPDATE jobs SET recovery_required=1 WHERE id=?",
                    (args.job_id,))
                conn.execute(
                    "INSERT INTO events(job_id,created_at,event_type,"
                    "detail) VALUES(?,?,?,?)",
                    (args.job_id, _utcnow(), "recovery_required",
                     "reconciled %s with mutation evidence" % applied))
                conn.commit()
                recovery = True
            except Exception as exc:
                print("ERROR: could not set recovery: %s" % exc)
                return 1
    elif action == "mark-interrupted":
        needs_recovery = _rc.recovery_for(
            state, False, bool(job_d.get("unresolved", 0)))
        try:
            transition_tx(
                conn, args.job_id, "interrupted", step="interrupted",
                expect_states=["accepted", "preflight", "backup",
                               "updating", "verifying", "interrupted"],
                update={"error_code": "interrupted",
                        "error_detail": "ssh reconcile: %s" % detail[:400],
                        "recovery_required": 1 if needs_recovery else 0,
                        "unresolved": 0},
                event="interrupted", event_detail=detail[:500],
                release_mutation=True)
            print("terminalized abandoned job as interrupted "
                  "(recovery=%s)" % int(needs_recovery))
            state = "interrupted"
            recovery = recovery or needs_recovery
        except TxError as exc:
            print("ERROR: cannot terminalize: %s" % exc)
            return 1

    if args.clear_recovery:
        if not recovery:
            print("VERDICT: no updater remains; recovery_required already "
                  "clear. Nothing to do.")
            return 0
        try:
            conn.execute(
                "UPDATE jobs SET recovery_required=0 WHERE id=?",
                (args.job_id,))
            conn.execute(
                "INSERT INTO events(job_id,created_at,event_type,detail)"
                " VALUES(?,?,?,?)",
                (args.job_id, _utcnow(), "recovered",
                 "ssh reconcile: no updater remains (unit=%s)" % unit))
            conn.commit()
        except Exception as exc:
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
