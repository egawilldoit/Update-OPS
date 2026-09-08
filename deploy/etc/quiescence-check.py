#!/usr/bin/env python3
"""Version-independent deployment quiescence controller (F07).

Used by install.sh/upgrade.sh BEFORE any candidate code is trusted and
without requiring the installed release to know the newest protocol:

  python3 deploy/etc/quiescence-check.py --config /etc/ega-update/config.json

Reads (stdlib only, no backend imports):
- config JSON directly (state_dir/db_path);
- the SQLite DB read-only (mode=ro; never creates WAL/locks as writer);
- job units via the tool-owner user manager AND the system manager;
- heartbeat file freshness;
- the admission drain file.

Proves ALL of: worker heartbeat fresh; no active/nonterminal job; no
unresolved or recovery-required job; every expected runner unit confirmed
stopped (live/starting/stopping/UNKNOWN all block); no stray job unit
active; no delegated service operation active or unprovable; admission
drain present (--require-drain, default true for deploy use).

Exit 0 (with JSON report) only when proven quiescent; exit 3 otherwise.
Unknown systemd/process state means NOT quiescent — never inferred from
missing DB rows alone. Never stops anything, never migrates, never
touches the installation.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_BLOCKED = 3

NONTERMINAL = ("accepted", "preflight", "backup", "updating", "verifying")
HEARTBEAT_MAX_AGE_S = 20


def _fail(reason, details=None):
    # type: (str, object) -> dict
    report = {"quiescent": False, "reasons": [reason], "details": details or {}}
    sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    return report


def _load_config(path):
    # type: (str) -> dict
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("config is not an object")
    return data


def _open_ro(db_path):
    # type: (str) -> object
    uri = "file:%s?mode=ro" % db_path
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn, name):
    # type: (object, str) -> bool
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchall()
        return len(rows) == 1
    except Exception:
        return False


class _InspectionError(Exception):
    """A required safety query failed: quiescence is unprovable."""


def _schema_version(conn):
    # type: (object) -> int
    """Read-only schema version (-1 when unreadable)."""
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
        if row is None:
            return -1
        return int(str(row["value"] or "0").strip() or 0)
    except Exception:
        return -1


def _updater_processes(hex_tokens):
    # type: (object) -> tuple
    """Execution-marked process scan (deploy mirror of the reconciler
    rule): cmdlines containing a job hex token AND a runner/updater
    marker, self excluded. Returns (ok, processes): ok False when the
    scan itself fails (unprovable, never empty).
    """
    try:
        me = os.getpid()
    except Exception:
        me = -1
    try:
        tokens = [str(t or "") for t in (hex_tokens or []) if str(t or "")]
    except Exception:
        return False, []
    if not tokens:
        return True, []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except Exception as exc:
        return False, [{"pid": -1,
                        "cmdline": "process scan failed: %s" % exc}]
    found = []
    for pid in pids:
        try:
            if int(pid) == me:
                continue
        except Exception:
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode(
                    "utf-8", errors="replace")
        except Exception:
            continue
        for token in tokens:
            if token not in cmd:
                continue
            if ("backend.app.worker.runner" in cmd
                    or "ega-update-job-" in cmd
                    or "systemd-run" in cmd):
                found.append({"pid": int(pid), "cmdline": cmd[:300]})
                break
    return True, found


def _job_rows(conn):
    # type: (object) -> list
    # G07: raises instead of returning [] — callers convert an
    # unreadable jobs table into "unprovable", never "empty".
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM jobs").fetchall()]
    except Exception as exc:
        raise _InspectionError("jobs unreadable: %s" % exc)


class _InspectionError(Exception):
    """A required safety query failed: quiescence is unprovable."""


def _owner_uid(tool_owner):
    # type: (str) -> int
    try:
        return pwd.getpwnam(tool_owner or "ubuntu").pw_uid
    except Exception:
        return -1


def _user_systemctl(tool_owner, uid, args, timeout_s=10):
    # type: (str, int, list, float) -> tuple
    """Run systemctl --user as the tool owner. Returns (rc, stdout, err).

    Any failure (sudo missing, no bus, timeout) is an explicit error —
    never interpreted as unit state.
    """
    runtime = "/run/user/%d" % uid if uid >= 0 else ""
    env_prefix = []
    if runtime:
        env_prefix = ["env", "XDG_RUNTIME_DIR=%s" % runtime,
                      "DBUS_SESSION_BUS_ADDRESS=unix:path=%s/bus" % runtime]
    cmd = ["sudo", "-u", tool_owner, "--"] + env_prefix + \
        ["systemctl", "--user"] + list(args)
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True,
                              timeout=max(1.0, float(timeout_s)),
                              shell=False, check=False)
        return proc.returncode, proc.stdout or "", (proc.stderr or "")[:300]
    except Exception as exc:
        return -1, "", "user systemctl failed: %s" % exc


def _system_systemctl(args, timeout_s=10):
    # type: (list, float) -> tuple
    try:
        proc = subprocess.run(
            ["systemctl"] + list(args), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            timeout=max(1.0, float(timeout_s)), shell=False, check=False)
        return proc.returncode, proc.stdout or "", (proc.stderr or "")[:300]
    except Exception as exc:
        return -1, "", "system systemctl failed: %s" % exc


def _parse_show(output):
    # type: (str) -> dict
    props = {}
    try:
        for line in (output or "").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                props[key.strip()] = value.strip()
    except Exception:
        pass
    return props


_SERVICE_SCOPES = ("user", "system")

_UNIT_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.@:-")


def parse_service_ref(value):
    # type: (object) -> tuple
    """Canonical delegated-service reference parser (H02 deploy mirror).

    Same contract as backend/app/reconcile_core.parse_service_ref:
    "scope:unit" or legacy bare "unit" (= user scope); anything else
    is ("","") — malformed entries are UNKNOWN and block, never
    guessed. Kept stdlib-only (no backend imports) by design.
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
        if scope not in _SERVICE_SCOPES:
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


def _cgroup_empty(cgroup):
    # type: (str) -> object
    """True/False/None (unprovable). Root can read cgroupfs directly."""
    if not cgroup or cgroup == "-":
        return None
    rel = cgroup[1:] if cgroup.startswith("/") else cgroup
    procs = os.path.join("/sys/fs/cgroup", rel, "cgroup.procs")
    try:
        with open(procs, "r", encoding="utf-8") as fh:
            members = [line.strip() for line in fh if line.strip()]
        return len(members) == 0
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        return None
    except OSError:
        return None
    except Exception:
        return None


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
    try:
        with open("/proc/%d/stat" % pid, "r", encoding="utf-8") as fh:
            parts = fh.read().rsplit(")", 1)
            if len(parts) == 2 and parts[1].split():
                return parts[1].split()[0] != "Z"
    except Exception:
        pass
    return True


def classify_unit_state(show_rc, props, cgroup_empty, pid_alive):
    # type: (int, dict, object, bool) -> tuple
    """Conservative five-state classification (mirrors backend units.py).

    Returns (state, detail) with state in live|starting|stopping|
    confirmed_stopped|unknown. Only positive proof yields
    confirmed_stopped.
    """
    if show_rc != 0 and props.get("LoadState", "") != "not-found":
        return "unknown", "manager query failed"
    if props.get("LoadState", "") == "not-found":
        return "confirmed_stopped", "manager reports LoadState=not-found"
    active = str(props.get("ActiveState", ""))
    sub = str(props.get("SubState", ""))
    if active == "active":
        if sub == "running":
            return "live", "active/running"
        if sub.startswith("start") or sub in (
                "auto-restart", "condition", "reload"):
            return "starting", "active/%s" % sub
        return "stopping", "active/%s" % sub
    if active == "activating":
        return "starting", "activating"
    if active == "deactivating":
        return "stopping", "deactivating"
    if active in ("inactive", "failed"):
        try:
            pid = int(str(props.get("MainPID", "0") or "0"))
        except ValueError:
            pid = 0
        if pid_alive and _pid_alive(pid):
            return "unknown", "manager %s but MainPID alive" % active
        if cgroup_empty is False:
            return "unknown", "manager %s but cgroup has members" % active
        if cgroup_empty is True:
            return "confirmed_stopped", "manager=%s cgroup-empty" % active
        return "unknown", "manager=%s cgroup unprovable" % active
    return "unknown", "unrecognized ActiveState %r" % active


def query_user_unit(tool_owner, uid, unit):
    # type: (str, int, str) -> tuple
    rc, out, err = _user_systemctl(
        tool_owner, uid,
        ["show", unit, "-p",
         "Id,ActiveState,SubState,MainPID,ControlGroup,LoadState"], 10)
    if rc != 0 and "not-found" not in (out or ""):
        props = _parse_show(out)
        if props.get("LoadState", "") != "not-found":
            return "unknown", "user bus query failed: %s" % err
    props = _parse_show(out)
    if unit and props.get("Id", "") not in ("", unit) and \
            props.get("LoadState", "") != "not-found":
        return "unknown", "identity mismatch"
    try:
        pid = int(str(props.get("MainPID", "0") or "0"))
    except ValueError:
        pid = 0
    return classify_unit_state(
        rc, props, _cgroup_empty(str(props.get("ControlGroup", "") or "")),
        _pid_alive(pid))


def query_system_unit(unit):
    # type: (str) -> tuple
    rc, out, err = _system_systemctl(
        ["show", unit, "-p",
         "Id,ActiveState,SubState,MainPID,ControlGroup,LoadState"], 10)
    if rc != 0 and "not-found" not in (out or ""):
        props = _parse_show(out)
        if props.get("LoadState", "") != "not-found":
            return "unknown", "system bus query failed: %s" % err
    props = _parse_show(out)
    try:
        pid = int(str(props.get("MainPID", "0") or "0"))
    except ValueError:
        pid = 0
    return classify_unit_state(
        rc, props, _cgroup_empty(str(props.get("ControlGroup", "") or "")),
        _pid_alive(pid))


def list_user_job_units(tool_owner, uid, prefix="ega-update-job-"):
    # type: (str, int, str) -> tuple
    """(units, error): units never trusted when error is non-empty."""
    rc, out, err = _user_systemctl(
        tool_owner, uid,
        ["list-units", "%s*" % prefix, "--all", "--no-legend",
         "--no-pager"], 10)
    if rc != 0:
        return [], "unit enumeration failed: %s" % err
    found = []
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) < 4 or prefix not in parts[0]:
            continue
        found.append({"unit": parts[0], "active_state": parts[2],
                      "sub_state": parts[3]})
    return found, ""


def _heartbeat_fresh(state_dir, max_age_s=20):
    # type: (str, float) -> bool
    try:
        with open(os.path.join(state_dir, "dispatcher.heartbeat"), "r",
                  encoding="utf-8") as fh:
            data = json.load(fh)
        ts = datetime.fromisoformat(str(data.get("ts", "")))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return 0 <= age <= max_age_s
    except Exception:
        return False


def assess(config_path, require_drain=True):
    # type: (str, bool) -> dict
    """Full quiescence proof. Never raises; unknown => not quiescent."""
    reasons = []
    try:
        config = _load_config(config_path)
    except Exception as exc:
        return {"quiescent": False, "reasons": ["config unreadable: %s" % exc],
                "details": {}}
    state_dir = str(config.get("state_dir", "") or "/var/lib/ega-update")
    db_path = str(config.get("db_path", "") or
                  os.path.join(state_dir, "state.db"))
    tool_owner = str(config.get("tool_owner", "") or "ubuntu")
    uid = _owner_uid(tool_owner)
    details = {}
    worker_alive = _heartbeat_fresh(state_dir)
    if not worker_alive:
        reasons.append("worker heartbeat stale/missing")
    details["worker_alive"] = worker_alive
    try:
        conn = _open_ro(db_path)
    except Exception as exc:
        reasons.append("database unreadable: %s" % exc)
        details["database"] = "unreadable"
        return {"quiescent": False, "reasons": reasons, "details": details}
    # G07: every required query below either yields positive proof or
    # appends an "unprovable" reason. No failure path defaults to empty.
    try:
        jobs = _job_rows(conn)
    except _InspectionError as exc:
        reasons.append(str(exc))
        details["jobs"] = "unreadable"
        try:
            conn.close()
        except Exception:
            pass
        return {"quiescent": False, "reasons": reasons, "details": details}
    active = [j for j in jobs if str(j.get("state", "")) in NONTERMINAL]
    details["active_job"] = str(active[0].get("id", "")) if active else ""
    if active:
        reasons.append("active job %s (%s)" % (
            details["active_job"][:8], active[0].get("state", "")))
    for key, label in (("unresolved", "unresolved jobs"),
                       ("recovery", "recovery-required jobs")):
        if key == "unresolved":
            ids = [str(j.get("id", "")) for j in jobs
                   if int(j.get("unresolved", 0) or 0)]
        else:
            ids = [str(j.get("id", "")) for j in jobs
                   if int(j.get("recovery_required", 0) or 0)]
        details[key + "_jobs"] = ids
        if ids:
            reasons.append("%s: %s" % (
                label, ",".join(i[:8] for i in ids[:5])))
    # Held mutation leases (G08): independently fatal, even when the DB
    # job row is terminal. Missing execution_leases table is explicit:
    # pre-lease schemas (version < 4, proven via schema_meta) have no
    # lease mechanism, so vacuous truth is sound there; anything else
    # (newer schema, unreadable ledger/version) blocks.
    leased = []
    leases_unprovable = False
    try:
        if not _table_exists(conn, "execution_leases"):
            version = _schema_version(conn)
            if version < 0:
                raise _InspectionError(
                    "schema version unreadable: lease state unprovable")
            if version >= 4:
                raise _InspectionError(
                    "execution_leases table missing on schema v%d" % version)
            details["leases"] = "pre-lease schema (v%d)" % version
        else:
            leased = [str(r["job_id"] or "") for r in conn.execute(
                "SELECT job_id FROM execution_leases WHERE kind='mutation'"
                " AND released_at=''").fetchall()]
            leased = [jid for jid in leased if jid]
    except _InspectionError as exc:
        reasons.append(str(exc))
        leases_unprovable = True
        leased = []
    except Exception as exc:
        reasons.append("lease query failed: %s" % exc)
        leases_unprovable = True
        leased = []
    details["held_leases"] = leased
    if leases_unprovable:
        pass  # reason already recorded; quiescence decided below.
    if leased:
        reasons.append("unreleased mutation leases: %s" % ",".join(
            jid[:8] for jid in leased[:5]))
    leased_set = set(leased)
    # Expected units: nonterminal/unresolved/recovery jobs plus any job
    # holding an unreleased mutation lease (terminal-but-owned counts).
    units = []
    for job in jobs:
        try:
            state = str(job.get("state", ""))
            interesting = state in NONTERMINAL or \
                int(job.get("unresolved", 0) or 0) or \
                int(job.get("recovery_required", 0) or 0) or \
                str(job.get("id", "")) in leased_set
            if not interesting:
                continue
            unit = str(job.get("canonical_unit", "")
                       or job.get("runner_unit", "") or "")
            if not unit:
                stem = str(job.get("id", "") or "").replace("-", "")
                unit = "ega-update-job-%s.service" % stem if stem else ""
            if unit and unit not in units:
                units.append(unit)
        except Exception:
            continue
    live_units = []
    for unit in units:
        state, detail = query_user_unit(tool_owner, uid, unit)
        if state != "confirmed_stopped":
            live_units.append({"unit": unit, "state": state,
                               "detail": detail[:200]})
    stray, list_err = list_user_job_units(tool_owner, uid)
    if list_err:
        reasons.append("unit enumeration unproven: %s" % list_err[:200])
        live_units.append({"unit": "<enumeration>", "state": "unknown",
                           "detail": list_err[:200]})
    else:
        known = set(units)
        for entry in stray:
            if entry["unit"] in known:
                continue
            if entry["active_state"] not in ("inactive",) or \
                    entry["sub_state"] not in ("dead",):
                live_units.append({"unit": entry["unit"],
                                   "state": "stray-%s/%s" % (
                                       entry["active_state"],
                                       entry["sub_state"]),
                                   "detail": "no DB job references it"})
    details["live_units"] = live_units
    if live_units:
        reasons.append("runner units not quiescent: %s" % ",".join(
            str(u.get("unit", "?"))[-12:] for u in live_units[:5]))
    # Delegated operations for jobs under scrutiny: nonterminal,
    # unresolved, recovery-required, AND terminal-but-owned (held
    # lease) jobs (G08). A plan lookup/parse failure for a scrutiny
    # job is unprovable delegated state and blocks (never vacuous).
    delegated = []
    scrutiny_unprovable = False
    for job in jobs:
        try:
            state = str(job.get("state", ""))
            jid = str(job.get("id", ""))
            if state not in NONTERMINAL and \
                    not int(job.get("unresolved", 0) or 0) and \
                    not int(job.get("recovery_required", 0) or 0) and \
                    jid not in leased_set:
                continue
            try:
                plan = conn.execute(
                    "SELECT services FROM plans WHERE id=?",
                    (job.get("plan_id", ""),)).fetchone()
            except Exception as exc:
                reasons.append(
                    "delegated proof unprovable for job %s: %s"
                    % (jid[:8], exc))
                scrutiny_unprovable = True
                continue
            services = []
            if plan is None:
                reasons.append(
                    "delegated proof unprovable for job %s: plan row "
                    "missing" % jid[:8])
                scrutiny_unprovable = True
                continue
            try:
                services = list(json.loads(plan["services"] or "[]"))
            except Exception:
                reasons.append(
                    "delegated proof unprovable for job %s: services "
                    "unreadable" % jid[:8])
                scrutiny_unprovable = True
                continue
            for service in services[:20]:
                raw = str(service or "")
                if not raw:
                    continue
                # H02: structural parse first; scope directs the exact
                # manager (system services query ONLY the system
                # manager). Malformed refs and unprovable states block.
                scope, unit = parse_service_ref(raw)
                if not scope or not unit:
                    delegated.append(
                        {"service": raw, "bus": "-", "scope": "",
                         "state": "unknown",
                         "detail": "malformed service reference",
                         "job": jid[:8]})
                    continue
                if scope == "system":
                    entry_state, entry_detail = query_system_unit(unit)
                    bus = "system"
                else:
                    entry_state, entry_detail = query_user_unit(
                        tool_owner, uid, unit)
                    bus = "user"
                delegated.append(
                    {"service": raw, "bus": bus, "scope": scope,
                     "state": entry_state, "detail": entry_detail[:200],
                     "job": jid[:8]})
        except Exception as exc:
            reasons.append("delegated proof unprovable: %s" % exc)
            scrutiny_unprovable = True
            continue
    try:
        conn.close()
    except Exception:
        pass
    details["delegated_operations"] = [
        e for e in delegated if e.get("state") != "confirmed_stopped"]
    for entry in delegated:
        if entry.get("state") not in ("confirmed_stopped",):
            reasons.append("delegated operation %s (%s) is %s" % (
                entry.get("service", "?"), entry.get("job", "?"),
                entry.get("state", "unknown")))
            break
    # Execution-marked processes for scrutiny jobs (G08 shared pattern:
    # unit hex + runner markers, self excluded). An unlistable /proc or
    # any surviving match blocks; absence must be proved, not assumed.
    try:
        hexes = []
        for job in jobs:
            try:
                jid = str(job.get("id", "") or "")
                state = str(job.get("state", ""))
                if not jid:
                    continue
                if state in NONTERMINAL or \
                        int(job.get("unresolved", 0) or 0) or \
                        int(job.get("recovery_required", 0) or 0) or \
                        jid in leased_set:
                    token = jid.replace("-", "")
                    if token:
                        hexes.append(token)
            except Exception:
                continue
        procs_ok, procs = _updater_processes(hexes)
    except Exception as exc:
        procs_ok, procs = False, [
            {"pid": -1, "cmdline": "process proof crashed: %s" % exc}]
    details["updater_processes"] = procs
    if not procs_ok:
        reasons.append("execution-marked process state unprovable")
    elif procs:
        reasons.append("execution-marked processes alive: %s" % ",".join(
            str(p.get("pid", "?")) for p in procs[:5]))
    drain_path = os.path.join(state_dir, "drain")
    drain = os.path.exists(drain_path)
    details["drain"] = bool(drain)
    if require_drain and not drain:
        reasons.append("admission drain absent")
    quiescent = (worker_alive and not details["active_job"]
                 and not details.get("unresolved_jobs")
                 and not details.get("recovery_jobs")
                 and not leased and not leases_unprovable
                 and not live_units
                 and not details["delegated_operations"]
                 and not scrutiny_unprovable
                 and procs_ok and not procs
                 and (drain or not require_drain))
    return {"quiescent": bool(quiescent), "reasons": reasons,
            "details": details}


def main(argv=None):
    # type: (object) -> int
    ap = argparse.ArgumentParser(
        description="Update-OPS deploy quiescence controller")
    ap.add_argument("--config", required=True)
    ap.add_argument("--require-drain", dest="require_drain",
                    action="store_true", default=True)
    ap.add_argument("--no-require-drain", dest="require_drain",
                    action="store_false")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else 2
    try:
        report = assess(args.config, require_drain=args.require_drain)
    except Exception as exc:
        sys.stdout.write(json.dumps(
            {"quiescent": False,
             "reasons": ["assessment crashed: %s" % exc],
             "details": {}}) + "\n")
        return 3
    sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    return 0 if report.get("quiescent") else 3


if __name__ == "__main__":
    raise SystemExit(main())
