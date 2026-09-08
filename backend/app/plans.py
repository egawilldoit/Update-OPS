"""Immutable versioned plan contracts (R15). Python 3.10 compatible.

Plans are complete execution contracts persisted once (plan_version=2) and
never reconstructed from changing defaults. Fresh execution checks may
INVALIDATE a plan (blocked + reason); they may never silently alter it.

A plan binds: subject, tool, install identity, artifact fingerprint,
config hash, target/mode/channel, services, launch config, state homes,
backup scope+policy, required probes + checks manifest, budgets, space
per filesystem, steps, phase deadlines, restart impact/detail, activity
state+timestamp+evidence, immutable release, created/expiry, one-use
marker (used_at).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any, Dict, List

PLAN_VERSION = 2


class PlanInvalid(Exception):
    """Plan missing, expired, used, or UID/config mismatch."""


class PlanNotFound(Exception):
    """No such plan row."""


def _canon(obj):
    # type: (object) -> str
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      default=str)


def canonical_plan_hash(plan):
    # type: (Dict[str, Any]) -> str
    """Stable hash over the execution-relevant plan fields."""
    keys = ("tool_id", "subject", "install_identity", "fingerprint",
            "target", "target_mode", "channel", "services", "launch",
            "state_homes", "backup_scope", "backup_policy",
            "required_probes", "required_checks", "budgets", "space_fs",
            "steps", "deadlines", "config_hash", "release_path",
            "env_fingerprint")
    narrowed = {k: plan.get(k) for k in keys}
    return hashlib.sha256(_canon(narrowed).encode("utf-8")).hexdigest()


def build_plan_row(tool_id, subject, install_identity, fingerprint,
                   target, target_mode, channel, services, launch,
                   state_homes, backup_scope, backup_policy,
                   required_probes, required_checks, budgets, space_fs,
                   steps, deadlines, restart_impact, restart_detail,
                   activity_state, activity_ts, activity_evidence,
                   required_space_bytes, config_hash, release_path,
                   created_at, expires_at, artifact=None,
                   env_fingerprint=""):
    # type: (...) -> Dict[str, Any]
    """Assemble the full plan row dict (caller INSERTs it)."""
    row = {
        "id": str(uuid.uuid4()),
        "tool_id": str(tool_id or ""),
        "subject": str(subject or ""),
        "created_at": str(created_at or ""),
        "expires_at": str(expires_at or ""),
        "fingerprint": str(fingerprint or ""),
        "target": str(target or ""),
        "target_mode": str(target_mode or ""),
        "channel": str(channel or ""),
        "services": _canon(list(services or [])),
        "backup_scope": _canon(dict(backup_scope or {})),
        "activity_state": str(activity_state or "unknown"),
        "activity_evidence": str(activity_evidence or "")[:1000],
        "used_at": "",
        "plan_version": PLAN_VERSION,
        "install_identity": str(install_identity or "")[:500],
        "artifact_json": _canon(dict(artifact or {})),
        "config_hash": str(config_hash or ""),
        "launch_json": _canon(dict(launch or {})),
        "state_homes_json": _canon(list(state_homes or [])),
        "backup_policy_json": _canon(dict(backup_policy or {})),
        "required_probes_json": _canon(list(required_probes or [])),
        "budgets_json": _canon(dict(budgets or {})),
        "space_json": _canon(dict(space_fs or {})),
        "deadlines_json": _canon(dict(deadlines or {})),
        "restart_detail": str(restart_detail or "")[:2000],
        "activity_ts": str(activity_ts or ""),
        "release_path": str(release_path or ""),
        "required_space_bytes": int(required_space_bytes or 0),
        "required_checks_json": _canon(list(required_checks or [])),
        "restart_impact": str(restart_impact or "")[:2000],
        "env_fingerprint": str(env_fingerprint or ""),
        "steps_json": _canon(list(steps or [])),
    }
    # F01: exactly one canonical hash view. The initial hash input and the
    # load-time hash input are the same function of the same row — no
    # hand-maintained parallel field list that can drift (env_fingerprint
    # was previously omitted here but present at load time, rejecting
    # valid production plans as tampered).
    row["plan_hash"] = canonical_plan_hash(_hash_view(row))
    return row


PLAN_INSERT_COLS = (
    "id,tool_id,subject,created_at,expires_at,fingerprint,target,"
    "target_mode,channel,services,backup_scope,activity_state,"
    "activity_evidence,used_at,plan_version,install_identity,"
    "artifact_json,config_hash,plan_hash,launch_json,state_homes_json,"
    "backup_policy_json,required_probes_json,budgets_json,space_json,"
    "deadlines_json,restart_detail,activity_ts,release_path,"
    "required_space_bytes,required_checks_json,restart_impact,steps_json,"
    "env_fingerprint"
)


def insert_plan(conn, row):
    # type: (sqlite3.Connection, Dict[str, Any]) -> str
    """INSERT a built plan row in the caller's transaction. Returns id."""
    cols = [c.strip() for c in PLAN_INSERT_COLS.split(",")]
    vals = [row.get(c, "") for c in cols]
    placeholders = ",".join(["?"] * len(cols))
    conn.execute("INSERT INTO plans(%s) VALUES(%s)"
                 % (",".join(cols), placeholders), vals)
    return str(row.get("id", ""))


def load_plan(conn, plan_id):
    # type: (sqlite3.Connection, str) -> Dict[str, Any]
    """Load + structurally validate a plan. Raises PlanNotFound/PlanInvalid."""
    row = conn.execute("SELECT * FROM plans WHERE id=?",
                       (plan_id,)).fetchone()
    if row is None:
        raise PlanNotFound("unknown plan: %s" % plan_id)
    plan = dict(row)
    if int(plan.get("plan_version", 1) or 1) != PLAN_VERSION:
        raise PlanInvalid("unsupported plan_version %r; create a fresh plan"
                          % (plan.get("plan_version"),))
    for key in ("tool_id", "subject", "fingerprint", "target",
                "target_mode", "plan_hash", "config_hash", "release_path",
                "env_fingerprint"):
        if not plan.get(key):
            raise PlanInvalid("plan missing %s; create a fresh plan" % key)
    if plan.get("target_mode") not in ("exact", "native_latest"):
        raise PlanInvalid("plan target_mode invalid; create a fresh plan")
    try:
        recomputed = canonical_plan_hash(_hash_view(plan))
    except Exception:
        raise PlanInvalid("plan hash view unreadable; create a fresh plan")
    if recomputed != plan.get("plan_hash"):
        raise PlanInvalid("plan tampered (hash mismatch); "
                          "create a fresh plan")
    return plan


def _hash_view(plan):
    # type: (Dict[str, Any]) -> Dict[str, Any]
    def _loads(value, default):
        # type: (object, object) -> object
        try:
            if isinstance(value, str) and value:
                return json.loads(value)
        except Exception:
            pass
        return default
    return {
        "tool_id": plan.get("tool_id", ""),
        "subject": plan.get("subject", ""),
        "install_identity": plan.get("install_identity", ""),
        "fingerprint": plan.get("fingerprint", ""),
        "target": plan.get("target", ""),
        "target_mode": plan.get("target_mode", ""),
        "channel": plan.get("channel", ""),
        "services": _loads(plan.get("services"), []),
        "launch": _loads(plan.get("launch_json"), {}),
        "state_homes": _loads(plan.get("state_homes_json"), []),
        "backup_scope": _loads(plan.get("backup_scope"), {}),
        "backup_policy": _loads(plan.get("backup_policy_json"), {}),
        "required_probes": _loads(plan.get("required_probes_json"), []),
        "required_checks": _loads(plan.get("required_checks_json"), []),
        "budgets": _loads(plan.get("budgets_json"), {}),
        "space_fs": _loads(plan.get("space_json"), {}),
        "steps": _loads(plan.get("steps_json"),
                         _loads(plan.get("steps", ""), [])),
        "deadlines": _loads(plan.get("deadlines_json"), {}),
        "config_hash": plan.get("config_hash", ""),
        "release_path": plan.get("release_path", ""),
        "env_fingerprint": plan.get("env_fingerprint", ""),
    }


def plan_json_fields(plan):
    # type: (Dict[str, Any]) -> Dict[str, List[str]]
    """Names of JSON columns (for readers that decode them)."""
    return {"lists": ["services", "state_homes_json", "required_probes_json",
                      "required_checks_json", "steps_json"],
            "objects": ["backup_scope", "launch_json", "backup_policy_json",
                        "budgets_json", "space_json", "deadlines_json",
                        "artifact_json"]}
