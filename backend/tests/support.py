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
    """Build + INSERT a v2 immutable plan with live identities. Returns row."""
    from backend.app import plans as plans_lib

    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=600) if expires_future else \
        now - timedelta(seconds=600)
    cfg, rel, envfp = live_identities()
    row = plans_lib.build_plan_row(
        tool_id, subject, "display-identity-%s" % tool_id, fingerprint,
        target, target_mode, "test-channel", [], {}, [], {}, [],
        ["smoke"], {}, {}, ["preflight", "backup", "updating", "verifying"],
        {"preflight": 120, "backup": 600, "updating": 1800,
         "verifying": 300}, "none", "", activity_state,
        now.isoformat(), "evidence-%s" % activity_state, 1024, cfg, rel,
        now.isoformat(), exp.isoformat(), {}, envfp)
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
