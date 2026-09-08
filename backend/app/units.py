"""Explicit systemd unit-state model (R04). Python 3.10 compatible.

Never reduce execution state to active/not-active. query_unit returns one of:
live | starting | stopping | confirmed_stopped | unknown, derived from bounded
``systemctl --user show`` evidence (exit status, ActiveState, SubState,
MainPID liveness, control-group emptiness, expected unit identity).
``unknown`` must never release an execution reservation or clear recovery.
"""
from __future__ import annotations

import os
import subprocess
from typing import Dict

LIVE = "live"
STARTING = "starting"
STOPPING = "stopping"
CONFIRMED_STOPPED = "confirmed_stopped"
UNKNOWN = "unknown"

_SHOW_PROPS = ("Id,ActiveState,SubState,MainPID,ControlGroup,"
               "FragmentPath,Result,ExecMainStatus")


def _show(unit, timeout_s=10):
    # type: (str, float) -> Dict[str, object]
    """Bounded read-only show. Never raises; ok=False on any failure."""
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", unit, "-p", _SHOW_PROPS],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=max(1.0, float(timeout_s)), shell=False)
    except Exception:
        return {"ok": False}
    if proc.returncode != 0:
        return {"ok": False,
                "stderr": (proc.stderr or "")[:300]}
    props = {}  # type: Dict[str, str]
    try:
        for line in (proc.stdout or "").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                props[key.strip()] = value.strip()
    except Exception:
        return {"ok": False}
    return {"ok": True, "props": props}


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


def _cgroup_empty(cgroup):
    # type: (str) -> object
    """True when the cgroup has no members, False when it does, None when
    the cgroup path cannot be inspected (must be treated as unknown)."""
    if not cgroup or cgroup == "-":
        return None
    rel = cgroup[1:] if cgroup.startswith("/") else cgroup
    for base in ("/sys/fs/cgroup",):
        procs = os.path.join(base, rel, "cgroup.procs")
        try:
            with open(procs, "r", encoding="utf-8") as fh:
                members = [line.strip() for line in fh if line.strip()]
            return len(members) == 0
        except FileNotFoundError:
            continue
        except NotADirectoryError:
            continue
        except OSError:
            return None
        except Exception:
            return None
    # Cgroup path gone: for a transient unit this means stopped; report
    # empty only when the unit itself is inactive (checked by caller).
    return None


def query_unit(unit, timeout_s=10):
    # type: (str, float) -> Dict[str, object]
    """Classify unit execution state. Pure read-only inspection."""
    info = {
        "state": UNKNOWN, "unit": unit or "", "active_state": "",
        "sub_state": "", "main_pid": 0, "cgroup": "",
        "identity_ok": False, "detail": "",
    }  # type: Dict[str, object]
    if not unit:
        info["detail"] = "no unit recorded"
        return info
    shown = _show(unit, timeout_s=timeout_s)
    if not shown.get("ok"):
        info["detail"] = "show failed: %s" % str(
            shown.get("stderr", "bus query failed"))[:200]
        return info
    props = shown.get("props", {})  # type: ignore[assignment]
    active = str(props.get("ActiveState", ""))
    sub = str(props.get("SubState", ""))
    try:
        main_pid = int(str(props.get("MainPID", "0") or "0"))
    except ValueError:
        main_pid = 0
    cgroup = str(props.get("ControlGroup", "") or "")
    shown_id = str(props.get("Id", "") or "")
    info["active_state"] = active
    info["sub_state"] = sub
    info["main_pid"] = main_pid
    info["cgroup"] = cgroup
    info["identity_ok"] = (shown_id == unit)
    if not info["identity_ok"]:
        info["detail"] = "identity mismatch: show=%r want=%r" % (
            shown_id, unit)
        info["state"] = UNKNOWN
        return info
    if active == "active":
        if sub in ("running",):
            info["state"] = LIVE
        elif sub.startswith("start") or sub in (
                "auto-restart", "condition", "reload"):
            info["state"] = STARTING
        elif sub.startswith("stop") or sub in (
                "stop-sigterm", "stop-sigkill", "stop-post",
                "final-sigterm", "final-sigkill"):
            info["state"] = STOPPING
        elif sub in ("exited", "dead"):
            # Active+exited transient: treat as stopping until the
            # manager reports inactive; never assume quiescence yet.
            info["state"] = STOPPING
        else:
            info["state"] = UNKNOWN
            info["detail"] = "unrecognized active substate: %r" % sub
        return info
    if active == "activating":
        info["state"] = STARTING
        return info
    if active == "deactivating":
        info["state"] = STOPPING
        return info
    if active in ("inactive", "failed"):
        empty = _cgroup_empty(cgroup)
        alive = _pid_alive(main_pid)
        if alive:
            info["state"] = UNKNOWN
            info["detail"] = "manager reports %s but MainPID %d is alive" % (
                active, main_pid)
            return info
        if empty is False:
            info["state"] = UNKNOWN
            info["detail"] = "manager reports %s but cgroup has members" % (
                active,)
            return info
        # Cgroup gone/uninspectable + MainPID dead + manager inactive:
        # confirmed stopped for a transient unit. (empty True also here.)
        info["state"] = CONFIRMED_STOPPED
        info["detail"] = "manager=%s sub=%s" % (active, sub)
        return info
    info["detail"] = "unrecognized ActiveState: %r" % active
    info["state"] = UNKNOWN
    return info


def is_live(info):
    # type: (Dict[str, object]) -> bool
    return info.get("state") == LIVE or info.get("state") == STARTING


def is_confirmed_stopped(info):
    # type: (Dict[str, object]) -> bool
    return info.get("state") == CONFIRMED_STOPPED
