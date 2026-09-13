"""T3 plan/runner lifecycle agreement (D7; policy B: fail closed).

The T3 adapter may only advertise an automatic update lifecycle that the
runner actually executes: preflight -> backup -> updating -> verifying.
The runner has no stop/quiesce phase, no prior-state restore, and the
owner holds no scoped stop/start authority; accordingly:
- a unit that is not provably inactive is a blocked plan (classified
  consistent-backup-unavailable, manual pre-stop required), and
- backup refuses whenever writers are not provably quiesced, and
- the runner refuses any plan advertising lifecycle steps it cannot run.

All subprocess/systemd touchpoints are faked; nothing here starts, stops,
restarts, or updates a real T3 unit. Python 3.10 compatible.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import backend.app.adapters.t3 as t3_mod
from backend.app import db as db_lib
from backend.app import jobs as jobs_lib
from backend.app.adapters import registry as registry_lib
from backend.app.config import settings as settings_lib
from backend.app.worker import phase_run as phase_run_lib
from backend.app.worker import runner as runner_lib

import support as support_lib

FOUR_STEPS = ["preflight", "backup", "updating", "verifying"]
UNEXECUTABLE_STEPS = ["preflight", "stop-quiesce", "backup", "updating",
                      "restore-prior-state", "verifying"]


class _FakeProc(object):
    def __init__(self, exit_code=0, stdout="", stderr="", timed_out=False):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out

    def ok(self):
        return (not self.timed_out) and self.exit_code == 0


class _FakeActivity(object):
    def __init__(self, state, evidence="hermetic activity evidence"):
        self.tool = "t3"
        self.state = state
        self.evidence = evidence
        self.checked_at = "2026-09-13T00:00:00+00:00"


class _FakeInspect(object):
    fingerprint = "fp-t3"
    install_identity = "t3-nightly:1.2.3"
    state_dirs = ["/tmp/ega-t3-test"]
    version = "1.2.3"


class _FakeDiscover(object):
    available = True
    target = "1.3.0"
    target_mode = "exact"
    unknown_reason = ""


def _lifecycle_blob(plan):
    """All lifecycle-claim text from a plan (no model_dump: exec_argv is
    a list attached to the str-typed launch field)."""
    return json.dumps({
        "steps": list(plan.steps),
        "launch": dict(getattr(plan, "launch", {}) or {}),
        "backup_policy": dict(getattr(plan, "backup_policy", {}) or {}),
        "restart_impact": str(plan.restart_impact or ""),
        "restart_detail": str(getattr(plan, "restart_detail", "") or ""),
        "manual_restart_limitation": getattr(
            plan, "manual_restart_limitation", None),
    }, default=str)


def _t3_info(**overrides):
    info = {
        "unit": t3_mod.T3_UNIT,
        "state_path": "/tmp/ega-t3-test/service-state.json",
        "endpoint": "http://127.0.0.1:65000/health",
        "port": "65000",
        "launch_mode": "managed-service",
        "unit_load": "loaded",
        "unit_active": "inactive",
        "unit_sub": "dead",
        "unit_pid": "",
        "unit_user": "ubuntu",
        "state_exists": True,
        "state_version": "1.2.3",
        "exec_path": "/x/npx",
        "exec_resolved": "/x/npx",
        "process_hit": False,
        "process_checked": True,
        "unit_scope": "user",
        "official_units": [t3_mod.T3_UNIT],
        "inventory_home": "/tmp/ega-t3-test",
        "exec_start_raw": "/x/npx --yes t3@1.2.3 service update",
        "exec_argv": ["/x/npx", "--yes", "t3@1.2.3", "service", "update"],
    }
    info.update(overrides)
    return info


def _plan_adapter(monkeypatch, info, activity=None):
    """T3Adapter whose probes are hermetic; activity stays the real method
    unless a fake is supplied (to model busy/unknown activity)."""
    adapter = t3_mod.T3Adapter()
    monkeypatch.setattr(adapter, "_inventory", lambda: dict(info))
    monkeypatch.setattr(adapter, "inspect", lambda: _FakeInspect())
    monkeypatch.setattr(adapter, "discover", lambda: _FakeDiscover())
    monkeypatch.setattr(adapter, "_running_version", lambda _i: "1.2.3")
    monkeypatch.setattr(adapter, "measure_footprint", lambda _p: {"/tmp/ega-t3-test": 128})
    monkeypatch.setattr(adapter, "_staging_estimate", lambda: 4096)
    if activity is not None:
        monkeypatch.setattr(adapter, "activity", lambda: activity)
    monkeypatch.setattr(t3_mod, "get_tool_inventory", lambda _tool: {})
    monkeypatch.setattr(registry_lib, "filesystems_for",
                        lambda _paths: ["/"])
    return adapter


def _fresh_db(tmp_path, name="t3.db"):
    conn = db_lib.connect(str(tmp_path / name))
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.commit()
    return conn


def _t3_plan_row(conn, steps=None, target="1.3.0"):
    from datetime import datetime, timedelta, timezone

    from backend.app import plans as plans_lib

    now = datetime.now(timezone.utc)
    cfg, rel, envfp = support_lib.live_identities()
    row = plans_lib.build_plan_row(
        tool_id="t3", subject="owner@example.invalid",
        install_identity="display-identity-t3",
        fingerprint="fp-test-1", target=target, target_mode="exact",
        channel="test-channel", services=[], launch={}, state_homes=[],
        backup_scope={}, backup_policy={}, required_probes=[],
        required_checks=["smoke"], budgets={}, space_fs={},
        steps=list(FOUR_STEPS if steps is None else steps),
        deadlines={"preflight": 120, "backup": 600, "updating": 1800,
                   "verifying": 300},
        restart_impact="none", restart_detail="", activity_state="idle",
        activity_ts=now.isoformat(), activity_evidence="idle",
        required_space_bytes=1024, config_hash=cfg, release_path=rel,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=600)).isoformat(),
        artifact={}, env_fingerprint=envfp)
    conn.execute("BEGIN IMMEDIATE")
    plans_lib.insert_plan(conn, row)
    conn.commit()
    try:
        conn.execute("INSERT OR IGNORE INTO tools(id) VALUES(?)", ("t3",))
        conn.commit()
    except Exception:
        pass
    return row


class _FakeSupervisor(object):
    def __init__(self, results):
        self.calls = []
        self.results = results

    def __call__(self, tool_id, job_id, phase, payload_extra, timeout_s,
                 settings, log_dir, emit, op="", cancel_event=None, env=None):
        self.calls.append(phase)
        return self.results[phase]


def _bundle(activity_state="idle"):
    return {
        "inspection": {
            "fingerprint": "fp-test-1", "version": "1.2.3",
            "executable": "/x/npx", "install_kind": "managed-service",
            "source_clean": "clean",
        },
        "activity": {"state": activity_state, "evidence": "hermetic"},
        "footprint": {"/tmp/ega-t3-test": 1024},
        "evidence_durable": True,
    }


def _ok_results(tmp_path, after="1.3.0"):
    return {
        "preflight": (True, _bundle(), "", False),
        "backup": (True, {
            "supported": True, "path": str(tmp_path / "backup"),
            "scope": {"mode": "quiesced-copy"},
            "consistency": "quiesced-copy", "size_bytes": 10,
            "evidence_durable": True}, "", False),
        "execute": (True, {
            "state": "succeeded", "error_code": "", "error_detail": "",
            "exit_code": 0, "before_version": "1.2.3",
            "after_version": after, "timed_out": False,
            "evidence_durable": True}, "", False),
        "verify": (True, {
            "version": after, "passed": True,
            "checks": [{"name": "smoke", "result": "pass",
                        "mandatory": True, "summary": "ok"}],
            "error_detail": "", "evidence_durable": True}, "", False),
    }


def _run_t3_job(tmp_path, monkeypatch, results, steps=None):
    conn = _fresh_db(tmp_path)
    row = _t3_plan_row(conn, steps=steps)
    jid = support_lib.admit_new(
        conn, plan_id=row["id"], tool_id="t3", fingerprint="fp-test-1",
        idem_key="k-%s" % uuid.uuid4().hex[:8])
    nonce = "n-%s" % uuid.uuid4().hex[:8]
    assert jobs_lib.claim_with_nonce(conn, jid, nonce) is True
    conn.close()
    db_path = str(tmp_path / "t3.db")
    logs = str(tmp_path / "logs")
    os.makedirs(logs, exist_ok=True)
    monkeypatch.setattr(settings_lib, "db_path", db_path)
    monkeypatch.setattr(settings_lib, "log_dir", logs)
    monkeypatch.setattr(settings_lib, "state_dir", str(tmp_path))
    monkeypatch.setattr(settings_lib, "backup_dir", str(tmp_path / "backups"))
    monkeypatch.setattr(settings_lib, "disk_floor_bytes", 1)
    monkeypatch.setattr(settings_lib, "reserve_bytes", 1)
    monkeypatch.setattr(runner_lib.Runner, "_validate_env_release",
                        lambda self: (True, "", ""))
    monkeypatch.setattr(registry_lib, "check_disk",
                        lambda _paths, _need: (True, "ok", []))
    sup = _FakeSupervisor(results)
    monkeypatch.setattr(phase_run_lib, "run_supervised_phase", sup)
    rc = runner_lib.Runner(jid, nonce).run()
    check = db_lib.connect(db_path)
    check.row_factory = sqlite3.Row
    job = dict(check.execute("SELECT * FROM jobs WHERE id=?",
                             (jid,)).fetchone())
    plan = dict(check.execute("SELECT * FROM plans WHERE id=?",
                              (job["plan_id"],)).fetchone())
    check.close()
    return rc, job, sup.calls, json.loads(plan["steps_json"])


def _receipt(logs, jid):
    path = os.path.join(logs, "%s.receipt.json" % jid)
    assert os.path.isfile(path), path
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# 1/2. plan state matrix
# ---------------------------------------------------------------------------

def test_plan_initially_active_t3_blocks_without_lifecycle_claims(monkeypatch):
    info = _t3_info(unit_active="active", unit_sub="running",
                    process_hit=True)
    adapter = _plan_adapter(monkeypatch, info)
    plan = adapter.plan()
    assert plan.steps == []
    assert getattr(plan, "manual_restart_limitation", None) is True
    assert plan.backup_policy.get("mode") == "consistent-backup-unavailable"
    blob = _lifecycle_blob(plan)
    assert "stop-quiesce" not in blob
    assert "restore-prior-state" not in blob
    assert "quiesced" in plan.restart_impact.lower()
    assert "planning blocked" in plan.restart_impact.lower()


def test_plan_busy_activity_blocks(monkeypatch):
    info = _t3_info(unit_active="active", unit_sub="running",
                    process_hit=True)
    adapter = _plan_adapter(monkeypatch, info,
                            activity=_FakeActivity("busy"))
    plan = adapter.plan()
    assert plan.steps == []
    assert "busy" in plan.restart_impact.lower()
    assert "stop-quiesce" not in _lifecycle_blob(plan)


def test_plan_transitional_stop_states_block(monkeypatch):
    # Stop status unknown/deactivating/activating must never yield an
    # automatic-update plan: only positive inactive proof does.
    for state in ("activating", "deactivating", "reloading"):
        info = _t3_info(unit_active=state, unit_sub=state,
                        process_hit=state == "deactivating")
        adapter = _plan_adapter(monkeypatch, info)
        plan = adapter.plan()
        assert plan.steps == [], state
        assert getattr(plan, "manual_restart_limitation", None) is True, state


def test_plan_initially_inactive_t3_advertises_executable_lifecycle(monkeypatch):
    adapter = _plan_adapter(monkeypatch, _t3_info())
    plan = adapter.plan()
    assert plan.steps == FOUR_STEPS
    assert plan.backup_policy.get("mode") == "quiesced-copy"
    assert getattr(plan, "manual_restart_limitation", None) is False
    # Prior inactive state is recorded, never falsely restored: no
    # stop/start/restore step is advertised (policy B equivalence).
    assert plan.launch.get("prior_active") == "inactive"
    blob = _lifecycle_blob(plan)
    assert "stop-quiesce" not in blob
    assert "restore-prior-state" not in blob
    phases, bad = runner_lib._phases_for_steps(plan.steps)
    assert bad == []
    assert phases == ["preflight", "backup", "execute", "verify"]


# ---------------------------------------------------------------------------
# 3/4/5. backup gate: active and transitional units refuse; inactive copies
# ---------------------------------------------------------------------------

def test_backup_active_refuses_consistent_backup_unavailable(monkeypatch):
    info = _t3_info(unit_active="active", unit_sub="running",
                    process_hit=True)
    adapter = _plan_adapter(monkeypatch, info)
    res = adapter.backup("job-lifecycle-1")
    assert res.supported is False
    assert "backup_unsupported" in res.unsupported_reason
    assert "consistent-backup-unavailable" in res.unsupported_reason
    assert "stop-quiesce" not in res.unsupported_reason
    assert "restore-prior-state" not in res.unsupported_reason


def test_backup_transitional_unit_refuses(monkeypatch):
    adapter = _plan_adapter(
        monkeypatch, _t3_info(unit_active="deactivating",
                              unit_sub="deactivating"))
    res = adapter.backup("job-lifecycle-2")
    assert res.supported is False
    assert "consistent-backup-unavailable" in res.unsupported_reason


def test_backup_inactive_is_quiesced_copy(monkeypatch, tmp_path):
    adapter = _plan_adapter(monkeypatch, _t3_info())
    monkeypatch.setattr(settings_lib, "backup_dir", str(tmp_path / "b"))
    calls = {"n": 0}

    def _fake_run(argv, timeout=30, cwd=None, scope_unit=None, **kw):
        calls["n"] += 1
        return _FakeProc(0, "ExecStart=/x/npx --yes t3@1.2.3 service update\n",
                         "")

    monkeypatch.setattr(t3_mod, "run_fixed", _fake_run)
    res = adapter.backup("job-lifecycle-3")
    assert res.supported is True
    assert res.consistency == "quiesced-copy"
    assert os.path.isfile(os.path.join(res.path, "unit.txt"))
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# stop/restart/restore operations do not exist to fail (policy B)
# ---------------------------------------------------------------------------

def test_t3_execute_never_stops_starts_or_restarts_unit(monkeypatch):
    adapter = _plan_adapter(monkeypatch, _t3_info())
    plan = adapter.plan()
    assert plan.steps == FOUR_STEPS
    recorded = []

    def _fake_run(argv, timeout=60, cwd=None, scope_unit=None, **kw):
        recorded.append(list(argv))
        return _FakeProc(0, "updated", "")

    monkeypatch.setattr(t3_mod, "run_fixed", _fake_run)
    monkeypatch.setattr(t3_mod, "check_disk",
                        lambda _paths, _need: (True, "ok", []))
    monkeypatch.setattr(t3_mod, "resolve_executable",
                        lambda _p: (True, "/x/npx", "ok"))
    monkeypatch.setattr(t3_mod, "get_owner_paths",
                        lambda: {"npx_path": "/x/npx"})
    monkeypatch.setattr(adapter, "_running_version",
                        lambda _i: "1.3.0")
    res = adapter.execute(plan, "job-lifecycle-4", activity_ack=True)
    assert res.state == "succeeded"
    assert res.after_version == "1.3.0"
    assert recorded == [["/x/npx", "--yes", "t3@1.3.0",
                         "service", "update"]]
    for argv in recorded:
        joined = " ".join(argv).lower()
        assert "systemctl" not in joined
        assert "stop" not in joined and "restart" not in joined
        assert "start" not in joined


def test_execute_failure_is_install_failed_never_success(monkeypatch):
    adapter = _plan_adapter(monkeypatch, _t3_info())
    plan = adapter.plan()
    monkeypatch.setattr(
        t3_mod, "run_fixed",
        lambda argv, timeout=60, cwd=None, scope_unit=None, **kw:
        _FakeProc(1, "update failed", "exit 1"))
    monkeypatch.setattr(t3_mod, "check_disk",
                        lambda _paths, _need: (True, "ok", []))
    monkeypatch.setattr(t3_mod, "resolve_executable",
                        lambda _p: (True, "/x/npx", "ok"))
    monkeypatch.setattr(t3_mod, "get_owner_paths",
                        lambda: {"npx_path": "/x/npx"})
    monkeypatch.setattr(adapter, "_running_version", lambda _i: "1.2.3")
    res = adapter.execute(plan, "job-lifecycle-5", activity_ack=True)
    assert res.state == "install_failed"
    assert res.error_code == "install_failed"
    assert res.after_version == "1.2.3"


# ---------------------------------------------------------------------------
# 6/7/8. runner failure matrix (backup/update/verify) via the real run loop
# ---------------------------------------------------------------------------

def test_runner_backup_failure_blocks_before_mutation(tmp_path, monkeypatch):
    results = _ok_results(tmp_path)
    results["backup"] = (True, {
        "supported": False,
        "unsupported_reason": "backup_unsupported: consistent-backup-"
                              "unavailable: unit not provably inactive",
        "consistency": "consistent-backup-unavailable",
        "evidence_durable": True}, "", False)
    rc, job, calls, _steps = _run_t3_job(tmp_path, monkeypatch, results)
    assert rc == runner_lib.EXIT_BLOCKED
    assert job["state"] == "blocked"
    assert job["error_code"] == "backup_unsupported"
    assert int(job["recovery_required"]) == 0
    assert "execute" not in calls and "verify" not in calls


def test_runner_update_failure_is_failed_not_success(tmp_path, monkeypatch):
    results = _ok_results(tmp_path)
    results["execute"] = (True, {
        "state": "install_failed", "error_code": "install_failed",
        "error_detail": "t3 service update exit=1", "exit_code": 4,
        "before_version": "1.2.3", "after_version": "1.3.0",
        "timed_out": False, "evidence_durable": True}, "", False)
    rc, job, calls, _steps = _run_t3_job(tmp_path, monkeypatch, results)
    assert rc == runner_lib.EXIT_INSTALL_FAILED
    assert job["state"] == "failed"
    assert job["error_code"] == "install_failed"
    assert job["after_version"] == "1.3.0"
    assert "verify" in calls
    receipt = _receipt(str(tmp_path / "logs"), job["id"])
    assert receipt["state"] == "failed"


def test_runner_verify_failure_is_health_failed(tmp_path, monkeypatch):
    results = _ok_results(tmp_path)
    results["verify"] = (True, {
        "version": "1.3.0", "passed": False,
        "checks": [{"name": "smoke", "result": "fail",
                    "mandatory": True, "summary": "endpoint down"}],
        "error_detail": "mandatory checks failed",
        "evidence_durable": True}, "", False)
    rc, job, _calls, _steps = _run_t3_job(tmp_path, monkeypatch, results)
    assert rc == runner_lib.EXIT_VERIFY_FAILED
    assert job["state"] == "health_failed"
    assert job["error_code"] == "health_failed"
    assert job["after_version"] == "1.3.0"


# ---------------------------------------------------------------------------
# 12. recovery evidence after a partial/timed-out failure
# ---------------------------------------------------------------------------

def test_runner_hard_timeout_sets_recovery_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_lib, "_job_processes_alive", lambda _jid: [])
    results = _ok_results(tmp_path)
    results["execute"] = (False, {}, "phase deadline exceeded", True)
    rc, job, _calls, _steps = _run_t3_job(tmp_path, monkeypatch, results)
    assert rc == runner_lib.EXIT_INSTALL_FAILED
    assert job["state"] == "failed"
    assert job["error_code"] == "timeout"
    assert int(job["recovery_required"]) == 1
    receipt = _receipt(str(tmp_path / "logs"), job["id"])
    assert receipt["recovery_disposition"] == "required"


# ---------------------------------------------------------------------------
# acceptance: the same plan the runner consumes drives the same phases
# ---------------------------------------------------------------------------

def test_runner_executes_exactly_the_plan_steps(tmp_path, monkeypatch):
    adapter = _plan_adapter(monkeypatch, _t3_info())
    plan = adapter.plan()
    assert plan.steps == FOUR_STEPS
    # The runner consumes the persisted plan row (steps_json) built from
    # the same adapter plan the user/API would persist.
    rc, job, calls, persisted_steps = _run_t3_job(
        tmp_path, monkeypatch, _ok_results(tmp_path),
        steps=list(plan.steps))
    assert persisted_steps == plan.steps
    assert rc == runner_lib.EXIT_OK
    assert job["state"] == "succeeded"
    assert job["after_version"] == "1.3.0"
    phases, bad = runner_lib._phases_for_steps(plan.steps)
    assert bad == []
    assert phases == ["preflight", "backup", "execute", "verify"]
    assert calls[0] == "preflight"
    assert set(calls) <= {"preflight", "backup", "execute", "verify"}
    mutation_order = [c for c in calls
                      if c in ("backup", "execute", "verify")]
    assert mutation_order == ["backup", "execute", "verify"]


def test_runner_refuses_plan_with_unexecutable_lifecycle_steps(tmp_path,
                                                               monkeypatch):
    rc, job, calls, persisted_steps = _run_t3_job(
        tmp_path, monkeypatch, _ok_results(tmp_path),
        steps=list(UNEXECUTABLE_STEPS))
    assert persisted_steps == UNEXECUTABLE_STEPS
    assert rc == runner_lib.EXIT_BLOCKED
    assert job["state"] == "blocked"
    assert job["error_code"] == "install_method_unsupported"
    assert "stop-quiesce" in job["error_detail"]
    assert calls == ["preflight"]
    assert int(job["recovery_required"]) == 0


def test_runner_busy_activity_blocks_before_backup(tmp_path, monkeypatch):
    results = _ok_results(tmp_path)
    results["preflight"] = (True, _bundle(activity_state="busy"), "", False)
    rc, job, calls, _steps = _run_t3_job(tmp_path, monkeypatch, results)
    assert rc == runner_lib.EXIT_BLOCKED
    assert job["state"] == "blocked"
    assert job["error_code"] == "activity_blocked"
    assert "backup" not in calls and "execute" not in calls
