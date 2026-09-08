"""SSH-only reconcile command (SPEC §8).

Usage:
  python -m backend.app.worker.reconcile --job-id <uuid> [--clear-recovery]
  [--db-path /var/lib/ega-update/state.db]

Inspects the recorded systemd runner unit (``systemctl is-active/show``),
process state (``/proc`` liveness scan), and installation probes (READ-ONLY
reads of the tools row), then prints a verdict.

Rules:
- Clears ``recovery_required`` ONLY with ``--clear-recovery`` AND after
  proving no updater remains (unit inactive + MainPID dead + no process
  carries the job id). NEVER on heartbeat age.
- Records every run in ``events`` (``reconcile`` / ``recovered``).
- Exits nonzero when unresolved.
- Requires ``--job-id``; refuses to run without it (including
  non-interactively). Run over SSH or the provider console only — never
  from the browser/API (the API cannot invoke this).

Python 3.10 compatible. Stdlib + backend.app.db only.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from backend.app.db import connect

DEFAULT_DB = "/var/lib/ega-update/state.db"
RECEIPT_TMPL = "/var/lib/ega-update/logs/%s.receipt.json"


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


def _run_systemctl(args):
    # type: (list) -> str
    try:
        proc = subprocess.run(
            ["systemctl"] + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=15, shell=False)
        return proc.stdout.strip()
    except Exception as exc:
        return "ERROR: %s" % exc


def _unit_state(unit):
    # type: (str) -> dict
    """Read-only inspection of the recorded runner unit."""
    active = _run_systemctl(["is-active", unit])
    show = _run_systemctl(["show", unit, "-p", "ActiveState,SubState,MainPID,ExecMainStatus,Result"])
    info = {"is_active": active, "show": show, "main_pid": 0}  # type: dict
    for line in show.splitlines():
        if line.startswith("MainPID="):
            try:
                info["main_pid"] = int(line.split("=", 1)[1])
            except ValueError:
                info["main_pid"] = 0
    return info


def _pid_alive(pid):
    # type: (int) -> bool
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    # Zombie check via /proc stat (state Z == dead for our purposes).
    try:
        with open("/proc/%d/stat" % pid, "r", encoding="utf-8") as fh:
            parts = fh.read().rsplit(")", 1)
            if len(parts) == 2 and parts[1].split():
                return parts[1].split()[0] != "Z"
    except Exception:
        pass
    return True


def _job_processes(job_id):
    # type: (str) -> list
    """Read-only /proc scan for processes still carrying the job id."""
    found = []
    short = job_id[:8]
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except Exception:
        return found
    for pid in pids:
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except Exception:
            continue
        if job_id in cmd or ("ega-update" in cmd and short in cmd):
            found.append({"pid": int(pid), "cmdline": cmd[:300]})
    return found


def _utcnow():
    # type: () -> str
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def main(argv=None):
    # type: (list) -> int
    ap = argparse.ArgumentParser(description="SSH-only job reconcile (read-only probes + gated recovery clear).")
    ap.add_argument("--job-id", required=True, help="Job UUID to reconcile (required, incl. non-interactive use).")
    ap.add_argument("--clear-recovery", action="store_true",
                    help="Clear recovery_required ONLY after proving no updater remains.")
    ap.add_argument("--db-path", default="", help="SQLite path (default: EGA_DB_PATH / config db_path / %s)." % DEFAULT_DB)
    args = ap.parse_args(argv)
    if not args.job_id:
        ap.error("--job-id is required (refusing to run without it)")

    db_path = _db_path(args.db_path)
    conn = connect(db_path)
    try:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (args.job_id,)).fetchone()
    except Exception as exc:
        print("ERROR: cannot read job: %s" % exc)
        return 2
    if job is None:
        print("ERROR: unknown job id: %s" % args.job_id)
        return 2

    unit = job["runner_unit"] or ""
    state = job["state"]
    recovery = bool(job["recovery_required"])
    heartbeat = job["heartbeat"] or ""
    print("job: %s tool=%s state=%s recovery_required=%s" % (job["id"], job["tool_id"], state, int(recovery)))
    print("runner_unit: %s" % (unit or "(none recorded)"))
    # Heartbeat is informational only — never a basis for clearing.
    print("heartbeat (informational only, never clears recovery): %s" % (heartbeat or "(none)"))

    unit_info = _unit_state(unit) if unit else {"is_active": "(no unit)", "show": "", "main_pid": 0}
    print("unit is-active: %s" % unit_info["is_active"])
    for line in str(unit_info["show"]).splitlines():
        print("unit show: %s" % line)
    main_pid = int(unit_info.get("main_pid") or 0)
    print("main_pid: %d alive=%s" % (main_pid, _pid_alive(main_pid) if main_pid else False))

    receipt = RECEIPT_TMPL % args.job_id
    receipt_ok = os.path.exists(receipt)
    print("completion receipt: %s present=%s" % (receipt, receipt_ok))

    leftovers = _job_processes(args.job_id)
    if leftovers:
        print("live job processes: %d" % len(leftovers))
        for item in leftovers[:20]:
            print("  pid=%s cmd=%s" % (item["pid"], item["cmdline"]))
    else:
        print("live job processes: 0")

    # Read-only installation probe: observed tool state, never mutated here.
    try:
        tool = conn.execute("SELECT * FROM tools WHERE id=?", (job["tool_id"],)).fetchone()
    except Exception:
        tool = None
    if tool is not None:
        print("tool probe (read-only): id=%s version=%s health=%s detail=%s" %
              (tool["id"], tool["observed_version"], tool["health"], (tool["health_detail"] or "")[:200]))
    else:
        print("tool probe (read-only): no tools row for %s" % job["tool_id"])

    unit_live = (unit_info["is_active"] == "active") or _pid_alive(main_pid) or bool(leftovers)
    now = _utcnow()
    try:
        conn.execute(
            "INSERT INTO events(job_id,created_at,event_type,detail) VALUES(?,?,?,?)",
            (args.job_id, now, "reconcile",
             "unit=%s active=%s receipt=%s procs=%d" % (unit or "-", unit_info["is_active"], int(receipt_ok), len(leftovers))))
        conn.commit()
    except Exception as exc:
        print("WARN: could not record reconcile event: %s" % exc)

    if unit_live:
        print("VERDICT: UNRESOLVED — runner/updater still present; recovery_required stays set. "
              "Do not start new jobs; do not clear on heartbeat age. Re-run after the unit exits.")
        return 1

    # No updater remains proven (unit inactive + MainPID dead + no job procs).
    if args.clear_recovery:
        if not recovery:
            print("VERDICT: no updater remains; recovery_required already clear. Nothing to do.")
            return 0
        try:
            conn.execute("UPDATE jobs SET recovery_required=0 WHERE id=?", (args.job_id,))
            conn.execute(
                "INSERT INTO events(job_id,created_at,event_type,detail) VALUES(?,?,?,?)",
                (args.job_id, _utcnow(), "recovered", "ssh reconcile: no updater remains (unit=%s)" % (unit or "-")))
            conn.commit()
        except Exception as exc:
            print("ERROR: failed to clear recovery: %s" % exc)
            return 1
        print("VERDICT: recovery_required CLEARED after proving no updater remains. "
              "Terminal job history is preserved; run a manual `Check again` after any SSH repair.")
        return 0

    if recovery or state in ("accepted", "preflight", "backup", "updating", "verifying", "interrupted"):
        print("VERDICT: UNRESOLVED — no updater remains, but recovery_required/state needs an explicit "
              "`--clear-recovery` decision. Inspect the installation, then re-run with --clear-recovery.")
        return 1
    print("VERDICT: resolved — no updater remains and no recovery block is set. "
          "Receipt present: %s." % receipt_ok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
