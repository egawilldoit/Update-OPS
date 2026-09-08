"""Shared test support: admission-compatible fixtures (N14 test seam).

All reservations in tests go through backend/app/admission.py admit() —
the same single path production uses. Plans are built with live
identities (config hash, resolved release, env fingerprint) so admission
comparisons pass deterministically. A stable disposable release root
stands in for /opt/ega-update/current (resolve_release honors
EGA_RELEASE_ROOT). Write-only test artifacts; never executed here.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone


def test_release_root():
    # type: () -> str
    """Stable disposable release dir for resolve_release() in tests."""
    path = os.environ.get("EGA_RELEASE_ROOT", "") or \
        "/tmp/ega-update-test-release"
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    os.environ["EGA_RELEASE_ROOT"] = path
    return path


def live_identities():
    # type: () -> tuple
    """(config_hash, release_path, env_fingerprint) from live helpers."""
    from backend.app.inventory import config_identity
    from backend.app.owner_env import canonical_fingerprint, resolve_release

    test_release_root()
    cfg = config_identity()
    rel = resolve_release()
    envfp = canonical_fingerprint(None, None, rel)
    assert cfg and rel and envfp
    return cfg, rel, envfp


def v2_plan_row(conn, plan_id, tool_id="hermes", subject="owner@example.invalid",
                fingerprint="fp-test-1", target="9.9.9", target_mode="exact",
                activity_state="idle", expires_future=True):
    # type: (...) -> dict
    """Build + INSERT a v2 immutable plan with live identities. Returns row.

    Keyword arguments at the build call (F-audit fix): positional drift
    here once swapped steps/space and crashed required_space_bytes.
    """
    from backend.app import plans as plans_lib

    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=600) if expires_future else \
        now - timedelta(seconds=600)
    cfg, rel, envfp = live_identities()
    row = plans_lib.build_plan_row(
        tool_id=tool_id, subject=subject,
        install_identity="display-identity-%s" % tool_id,
        fingerprint=fingerprint, target=target, target_mode=target_mode,
        channel="test-channel", services=[], launch={}, state_homes=[],
        backup_scope={}, backup_policy={}, required_probes=[],
        required_checks=["smoke"], budgets={}, space_fs={},
        steps=["preflight", "backup", "updating", "verifying"],
        deadlines={"preflight": 120, "backup": 600, "updating": 1800,
                   "verifying": 300},
        restart_impact="none", restart_detail="", activity_state=activity_state,
        activity_ts=now.isoformat(),
        activity_evidence="evidence-%s" % activity_state,
        required_space_bytes=1024, config_hash=cfg, release_path=rel,
        created_at=now.isoformat(), expires_at=exp.isoformat(), artifact={},
        env_fingerprint=envfp)
    row["id"] = plan_id
    # F01: no manual plan_hash repair. build_plan_row() is the single
    # authority for the hash ("id" is not a hashed field); recomputing
    # here would conceal a production hash-view divergence.
    conn.execute("BEGIN IMMEDIATE")
    plans_lib.insert_plan(conn, row)
    conn.commit()
    try:
        conn.execute("INSERT OR IGNORE INTO tools(id) VALUES(?)", (tool_id,))
        conn.commit()
    except Exception:
        pass
    return row


def admit_new(conn, subject="owner@example.invalid", plan_id=None,
              tool_id="hermes", fingerprint="fp-test-1", ack=False,
              idem_key=None, activity_state="idle"):
    # type: (...) -> str
    """Reserve via shared admission (worker_ready, undrained). Returns job."""
    from backend.app.admission import admit

    if plan_id is None:
        plan_id = str(uuid.uuid4())
        v2_plan_row(conn, plan_id, tool_id=tool_id, subject=subject,
                    fingerprint=fingerprint,
                    activity_state=activity_state)
    if idem_key is None:
        idem_key = "k-%s" % uuid.uuid4().hex[:8]
    job_id, created, err = admit(conn, subject, idem_key, plan_id, ack,
                                 fingerprint, True, False)
    assert err == "" and created and job_id, err
    return job_id


def bound_receipt(conn, job_id, nonce, after=None, state="succeeded",
                  **overrides):
    # type: (...) -> dict
    """Build a receipt fully bound to the job's real plan row (job, tool,
    plan, nonce, hash, release, target, mode, manifest). Callers override
    individual fields to construct contradiction cases."""
    from backend.app import receipts as receipts_lib

    job = dict(conn.execute("SELECT * FROM jobs WHERE id=?",
                            (job_id,)).fetchone())
    plan = dict(conn.execute("SELECT * FROM plans WHERE id=?",
                             (job.get("plan_id", ""),)).fetchone())
    import json as _json
    try:
        manifest = list(_json.loads(
            plan.get("required_checks_json", "[]") or "[]"))
    except Exception:
        manifest = ["smoke"]
    target = str(plan.get("target", "") or "")
    if after is None:
        after = target
    params = {
        "plan_id": job.get("plan_id", ""),
        "plan_hash": plan.get("plan_hash", ""),
        "attempt_nonce": nonce,
        "release_path": plan.get("release_path", ""),
        "target": target,
        "target_mode": plan.get("target_mode", "exact") or "exact",
        "expected_checks": list(manifest),
        "installer_exit": 0,
        "install_outcome": "succeeded",
        "actual_change": True,
        "evidence_durable": True,
        "cleanup_status": "resolved",
        "recovery_disposition": "none",
    }
    params.update(overrides)
    checks = overrides.get("checks", [
        {"name": name, "result": "pass", "mandatory": True,
         "summary": "ok"} for name in manifest] or [
        {"name": "smoke", "result": "pass", "mandatory": True,
         "summary": "ok"}])
    return receipts_lib.build_receipt(
        job_id, job.get("tool_id", ""), state, "1.0.0", after, 0, "",
        checks, "2026-09-08T00:00:00+00:00", **params)
