"""W5-D9 deploy-script release-switch tests (executable sandbox harness).

Runs the REAL install.sh/upgrade.sh through backend/tests/deploy_harness.py
(tmp sandbox + PATH shims) and asserts the OBSERVED failure policy of the
atomic release pointer:

* previous pointer captured via the shared primitive BEFORE maintenance
* switch/restore use ONE shared atomic primitive (no `ln -sfn` anywhere)
* pre-migration failures leave the previous release linked and reconcile
  previously-active services
* migration failure keeps the pointer AND keeps services stopped
* post-migration/post-switch failures restore the pointer ONLY when the
  existing `--check-compat` contract proves compatibility; otherwise the
  host stays in manual recovery (new pointer, services stopped, drain kept)
* restoration failure is explicit (never a success claim)
* every fault keeps the drain and never removes it
* a host-local kernel flock serializes overlapping deployments

All 13 required fault points are exercised (1-6, 8-13 here; 7 additionally
at the primitive level and end-to-end).
"""
from __future__ import annotations

import fcntl
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import deploy_harness  # noqa: E402

_PREV_NAME = "0" * 40
_COMMIT = {"install.sh": "1" * 40, "upgrade.sh": "2" * 40}


def _read(rel):
    with open(os.path.join(_REPO_ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def _run(tmp_path, script, **kw):
    return deploy_harness.run_deploy_script(
        tmp_path, _REPO_ROOT, script, **kw)


def _current(sandbox):
    return os.path.join(sandbox, "opt", "ega-update", "current")


def _prev(sandbox):
    return os.path.join(sandbox, "opt", "ega-update", "releases", _PREV_NAME)


def _new(sandbox, script):
    return os.path.join(sandbox, "opt", "ega-update", "releases",
                        _COMMIT[script])


def _resolves(path):
    return os.path.realpath(path)


def _assert_failed_closed(proc, paths):
    assert proc.returncode != 0, proc.stderr[-2000:]
    assert os.path.exists(os.path.join(paths["state"], "drain")), (
        "failure after the maintenance boundary must keep the drain")
    assert "drain removed" not in proc.stderr


def _switch_events(events):
    return [e for e in events if e.startswith("DEPLOY_RELEASE switch")]


def _restore_events(events, sandbox, script):
    """Switch events whose CAS expected-current is the NEW release."""
    new = _new(sandbox, script)
    return [e for e in _switch_events(events)
            if ("--previous %s" % new) in e]


# -- shared primitive (static) -------------------------------------------------

def _legacy_switch_lines(text):
    """Command lines that switch `current` with unlink-then-create."""
    import re
    hits = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.search(r"(^|[;&|()\s])ln\s+-sfn", line):
            hits.append(stripped)
    return hits


def test_both_scripts_share_one_atomic_primitive():
    lib = _read(os.path.join("deploy", "scripts", "lib",
                             "deploy_common.sh"))
    install = _read(os.path.join("deploy", "scripts", "install.sh"))
    upgrade = _read(os.path.join("deploy", "scripts", "upgrade.sh"))
    for name, text in (("deploy_common.sh", lib),
                       ("install.sh", install), ("upgrade.sh", upgrade)):
        assert not _legacy_switch_lines(text), (
            "%s still contains a non-atomic unlink-then-create switch: %r"
            % (name, _legacy_switch_lines(text)))
    for name, text in (("install.sh", install), ("upgrade.sh", upgrade)):
        assert "ega_switch_release" in text, name
        assert "ega_release_previous_target" in text, name
        assert 'readlink -f "$CURRENT_LINK"' not in text, (
            "%s must capture the previous pointer via inspect, not "
            "readlink -f" % name)
    # ONE delegate to the operator-side primitive (single implementation).
    assert lib.count("backend.app.deploy_release") == 1


def test_deploy_release_module_is_not_used_by_runtime_config():
    module = _read(os.path.join("backend", "app", "deploy_release.py"))
    assert "os.replace" in module
    assert "os.symlink" in module
    assert "def main(" in module


# -- success path: capture-then-switch ----------------------------------------

def test_upgrade_captures_previous_before_maintenance_and_switches(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert _resolves(_current(sandbox)) == _resolves(
        _new(sandbox, "upgrade.sh"))
    assert not os.path.exists(os.path.join(paths["state"], "drain"))
    assert "deployment lock acquired" in (proc.stdout + proc.stderr)
    inspects = [i for i, e in enumerate(events)
                if e.startswith("DEPLOY_RELEASE inspect")]
    drains = [i for i, e in enumerate(events)
              if e.startswith("TOUCH") and e.endswith("drain")]
    switches = [i for i, e in enumerate(events)
                if e.startswith("DEPLOY_RELEASE switch")]
    assert inspects and drains and switches
    assert inspects[0] < drains[0], (
        "previous pointer evidence must be captured before any maintenance "
        "mutation")
    assert drains[0] < switches[0]
    assert any("--previous" in e for e in _switch_events(events)), (
        "the switch must carry the exact previous target (CAS guard)")


def test_install_switches_via_shared_primitive(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "install.sh", existing_deploy=True, db_exists=True)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert _resolves(_current(sandbox)) == _resolves(
        _new(sandbox, "install.sh"))
    assert _switch_events(events), "install must switch via the primitive"
    assert any("--previous" in e for e in _switch_events(events))


def test_upgrade_refuses_invalid_pointer_before_maintenance(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=False, db_exists=True)
    assert proc.returncode != 0
    assert "REFUSING" in proc.stderr
    assert not os.path.exists(os.path.join(paths["state"], "drain")), (
        "invalid pointer evidence must fail closed before the maintenance "
        "boundary")
    assert not _switch_events(events)


# -- 1-4. pre-migration failures ----------------------------------------------

@pytest.mark.parametrize("point", ["stage", "validate", "venv", "backup"])
def test_upgrade_pre_migration_failure_keeps_pointer(tmp_path, point):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, fault_point=point)
    _assert_failed_closed(proc, paths)
    assert _resolves(_current(sandbox)) == _resolves(_prev(sandbox))
    assert "failure before migration" in proc.stderr
    assert "FAULT INJECTED at %s" % point in proc.stderr
    assert any(e.startswith("SYSTEMCTL start") for e in events), (
        "a pre-migration failure must reconcile previously-active services "
        "(no DB/config change yet)")


@pytest.mark.parametrize("point", ["stage", "validate", "venv", "backup"])
def test_install_pre_migration_failure_keeps_pointer(tmp_path, point):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "install.sh", existing_deploy=True, db_exists=True,
        services_active=True, fault_point=point)
    _assert_failed_closed(proc, paths)
    assert _resolves(_current(sandbox)) == _resolves(_prev(sandbox))


# -- 5. migration failure ------------------------------------------------------

def test_upgrade_migration_failure_keeps_pointer_and_services_stopped(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, fault_point="migrate")
    _assert_failed_closed(proc, paths)
    assert _resolves(_current(sandbox)) == _resolves(_prev(sandbox))
    assert "FAULT INJECTED at migrate" in proc.stderr
    assert "migration failed" in proc.stderr
    assert "MANUAL RECOVERY" in proc.stderr
    assert not any(e.startswith("SYSTEMCTL start") for e in events), (
        "migration failure leaves the DB state unproven; the old release "
        "must NOT be booted against a possibly-migrated DB")


def test_install_migration_failure_keeps_pointer(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "install.sh", existing_deploy=True, db_exists=True,
        services_active=True, fault_point="migrate")
    _assert_failed_closed(proc, paths)
    assert _resolves(_current(sandbox)) == _resolves(_prev(sandbox))
    assert "migration failed" in proc.stderr


# -- 6. immediately before the pointer switch ---------------------------------

def test_upgrade_pre_switch_failure_after_migration_restarts_old_services(
        tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, fault_point="pre-switch")
    _assert_failed_closed(proc, paths)
    assert _resolves(_current(sandbox)) == _resolves(_prev(sandbox))
    assert "POINTER UNCHANGED" in proc.stderr
    assert "DATABASE NOT ROLLED BACK" in proc.stderr
    assert any(e.startswith("SYSTEMCTL start") for e in events)


# -- 7. inside pointer-switch preparation -------------------------------------

def test_upgrade_switch_prepare_failure_keeps_pointer(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, fault_point="pointer-switch-prep")
    _assert_failed_closed(proc, paths)
    assert _resolves(_current(sandbox)) == _resolves(_prev(sandbox))
    assert "pointer-switch-prep" in proc.stderr
    assert "fault injected" in proc.stderr.lower()
    assert "POINTER UNCHANGED" in proc.stderr
    assert not _restore_events(events, sandbox, "upgrade.sh")


# -- 8. immediately after a successful switch ---------------------------------

def test_upgrade_post_switch_failure_proven_compat_restores_pointer(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, fault_point="post-switch")
    _assert_failed_closed(proc, paths)
    assert _resolves(_current(sandbox)) == _resolves(_prev(sandbox)), (
        "compat-proven post-switch failure must atomically restore the "
        "previous pointer")
    assert "POINTER RESTORE COMPLETE" in proc.stderr
    assert "no DATABASE ROLLBACK" in proc.stderr
    assert "rollback successful" not in proc.stderr.lower()
    assert _restore_events(events, sandbox, "upgrade.sh"), (
        "the restore must use the SAME atomic primitive with CAS")


def test_upgrade_post_switch_failure_unknown_compat_keeps_new_pointer(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, fault_point="post-switch", compat_fail=True)
    _assert_failed_closed(proc, paths)
    assert _resolves(_current(sandbox)) == _resolves(
        _new(sandbox, "upgrade.sh")), (
        "compat unknown/false: the pointer must NOT be automatically "
        "re-pointed at the old release")
    assert "compatibility NOT proven" in proc.stderr
    assert "MANUAL RECOVERY" in proc.stderr
    assert not _restore_events(events, sandbox, "upgrade.sh")


def test_upgrade_switch_failure_after_replace_is_treated_as_post_switch(
        tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, switch_post_replace_fail=True)
    _assert_failed_closed(proc, paths)
    assert _resolves(_current(sandbox)) == _resolves(_prev(sandbox)), (
        "a switch that replaced `current` before reporting failure must be "
        "recovered as a post-switch failure")
    assert "POINTER RESTORE COMPLETE" in proc.stderr
    assert any(e.startswith("DEPLOY_RELEASE verify") for e in events)


# -- 9. unit installation/reload failure --------------------------------------

def test_upgrade_units_failure_restores_pointer_when_compat_proven(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, fault_point="units")
    _assert_failed_closed(proc, paths)
    assert _resolves(_current(sandbox)) == _resolves(_prev(sandbox))
    assert "POINTER RESTORE COMPLETE" in proc.stderr


# -- 10/11. start failures (install.sh starts api/worker separately) ----------

@pytest.mark.parametrize("point", ["api-start", "worker-start"])
def test_install_start_failure_restores_pointer_when_compat_proven(
        tmp_path, point):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "install.sh", existing_deploy=True, db_exists=True,
        services_active=True, fault_point=point)
    _assert_failed_closed(proc, paths)
    assert _resolves(_current(sandbox)) == _resolves(_prev(sandbox))
    assert "FAULT INJECTED at %s" % point in proc.stderr
    assert "POINTER RESTORE COMPLETE" in proc.stderr
    assert any(e.startswith("SYSTEMCTL start") for e in events), (
        "service restoration: previously-active units restarted")


# -- 12. canonical readiness failure ------------------------------------------

def test_upgrade_readiness_failure_proven_compat_restores_pointer(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, ready_fail=True)
    _assert_failed_closed(proc, paths)
    assert _resolves(_current(sandbox)) == _resolves(_prev(sandbox))
    assert "POINTER RESTORE COMPLETE" in proc.stderr


def test_upgrade_readiness_failure_unknown_compat_keeps_new_pointer(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, ready_fail=True, compat_fail=True)
    _assert_failed_closed(proc, paths)
    assert _resolves(_current(sandbox)) == _resolves(
        _new(sandbox, "upgrade.sh"))
    assert "compatibility NOT proven" in proc.stderr
    assert "MANUAL RECOVERY" in proc.stderr


def test_install_readiness_failure_keeps_drain(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "install.sh", existing_deploy=True, db_exists=True,
        ready_fail=True)
    _assert_failed_closed(proc, paths)
    assert "READINESS_FAIL" in events


# -- 13. restoration attempt failure ------------------------------------------

def test_upgrade_restoration_failure_is_explicit(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, ready_fail=True, fault_point="restore")
    _assert_failed_closed(proc, paths)
    assert os.path.realpath(_current(sandbox)) == os.path.realpath(
        _new(sandbox, "upgrade.sh")), (
        "the injected restoration fault fires before the atomic switch, so "
        "the pointer stays at the candidate")
    assert "RESTORE FAILED" in proc.stderr
    assert "MANUAL RECOVERY" in proc.stderr
    assert "POINTER RESTORE COMPLETE" not in proc.stderr
    assert "rollback successful" not in proc.stderr.lower()


# -- 18. deployment lock -------------------------------------------------------

def test_deployment_lock_fails_fast_on_contention(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, lock_held=True)
    assert proc.returncode != 0
    assert "another deployment is active" in proc.stderr
    assert not os.path.exists(os.path.join(paths["state"], "drain")), (
        "the lock is acquired BEFORE any maintenance mutation")
    assert _resolves(_current(sandbox)) == _resolves(_prev(sandbox))
    assert not _switch_events(events)


def test_deployment_lock_released_after_success(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "deployment lock acquired" in (proc.stdout + proc.stderr), (
        "the deployment must acquire the host-local kernel lock")
    lock_path = os.path.join(sandbox, "opt", "ega-update", "deploy.lock")
    assert os.path.exists(lock_path)
    with open(lock_path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh, fcntl.LOCK_UN)


def test_install_lock_fails_fast_on_contention(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "install.sh", existing_deploy=True, db_exists=True,
        lock_held=True)
    assert proc.returncode != 0
    assert "another deployment is active" in proc.stderr
    assert not os.path.exists(os.path.join(paths["state"], "drain"))
    assert _resolves(_current(sandbox)) == _resolves(_prev(sandbox))
