"""W6 deployment failure/acceptance matrix (behavioral, end-to-end).

Runs the REAL install.sh / upgrade.sh through ``deploy_harness.py`` (tmp
sandbox, real filesystem/symlink/SQLite-path semantics, PATH shims for
host commands) and asserts the OBSERVED state machine:

  READ-ONLY PRECHECK -> MAINTENANCE BOUNDARY (drain) -> QUIESCENCE ->
  STOP+PROVE STOPPED -> HOST/RUNTIME MUTATION -> RELEASE PREPARATION ->
  BACKUP -> VALIDATION -> MIGRATION -> ATOMIC POINTER SWITCH -> UNITS ->
  SERVICE START -> CANONICAL READINESS -> SUCCESS (drain policy)

Primary evidence is the observed end state: process exit codes, drain
file state, symlink resolution, SQLite-path artifacts, service-shim
state, recorded command sequence, and the restore/rollback message
vocabulary. Scenario IDs (A1..A30, B, C, D, E, F, G, H) map to the W6
failure-state table (deploy/tests/FAILURE-MATRIX.md).

No host is touched: every path is redirected into tmp_path and every
mutating external command is shimmed. Rebooting is not simulated; the
reboot matrix is specified in the doc and constrained by the structural
boot-path checks in section G.
"""
from __future__ import annotations

import fcntl
import os
import re
import stat
import subprocess
import sys
import threading

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import deploy_harness  # noqa: E402

_PREV = "0" * 40
_COMMIT = {"install.sh": "1" * 40, "upgrade.sh": "2" * 40}
_RACE = "3" * 40

_READINESS_STAGES = [
    "api_service_identity",
    "api_security_boundary",
    "worker_process",
    "probe_executor",
    "owner_transient_execution",
]


# -- helpers ------------------------------------------------------------------

def _run(tmp_path, script, **kw):
    return deploy_harness.run_deploy_script(
        tmp_path, _REPO_ROOT, script, **kw)


def _out(proc):
    return proc.stdout + proc.stderr


def _current(sandbox):
    return os.path.join(sandbox, "opt", "ega-update", "current")


def _release(sandbox, name):
    return os.path.join(sandbox, "opt", "ega-update", "releases", name)


def _prev(sandbox):
    return _release(sandbox, _PREV)


def _new(sandbox, script):
    return _release(sandbox, _COMMIT[script])


def _race(sandbox):
    return _release(sandbox, _RACE)


def _resolves(path):
    return os.path.realpath(path)


def _drain(paths):
    return os.path.exists(os.path.join(paths["state"], "drain"))


def _read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _unit_state(sandbox, unit):
    statedir = os.path.join(sandbox, ".systemctl-state.d")
    path = os.path.join(statedir, unit)
    if os.path.exists(path):
        return _read(path).strip()
    legacy = os.path.join(sandbox, ".systemctl-state")
    if os.path.exists(legacy):
        return _read(legacy).strip()
    return "inactive"


def _has(events, needle):
    return any(needle in event for event in events)


def _count(events, needle):
    return sum(1 for event in events if needle in event)


def _first(events, needle):
    for index, event in enumerate(events):
        if needle in event:
            return index
    return -1


def _last(events, needle):
    for index in range(len(events) - 1, -1, -1):
        if needle in events[index]:
            return index
    return -1


def _switch_events(events):
    return [event for event in events
            if event.startswith("DEPLOY_RELEASE switch")]


def _restore_switch_events(events, script):
    """Switch events whose CAS expected-current is the candidate release
    (the forward switch carries --previous <previous>, the restore carries
    --previous <candidate>)."""
    commit = _COMMIT[script]
    hits = []
    for event in _switch_events(events):
        match = re.search(r"--previous (\S+)", event)
        if match is None:
            continue
        if match.group(1).rstrip("/").endswith(commit):
            hits.append(event)
    return hits


def _assert_failed_closed(proc, paths):
    assert proc.returncode != 0, _out(proc)[-3000:]
    assert _drain(paths), "failure must keep the drain"
    assert "drain removed" not in _out(proc)


def _assert_stopped_then_no_restart(events):
    stop = _last(events, "SYSTEMCTL stop")
    assert stop != -1
    assert _first(events[stop:], "SYSTEMCTL start") == -1, (
        "services must remain stopped after this failure")
    assert _first(events[stop:], "SYSTEMCTL restart") == -1


# =============================================================================
# A. Existing deployment (upgrade) — 30 scenarios
# =============================================================================

@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
def test_a01_lock_held_fails_before_any_maintenance_mutation(
        tmp_path, script):
    proc, sandbox, events, env, paths = _run(
        tmp_path, script, existing_deploy=True, db_exists=True,
        services_active=True, lock_held=True)
    assert proc.returncode != 0
    assert "another deployment is active" in _out(proc)
    assert not _drain(paths)
    assert not _has(events, "TOUCH")
    assert not _has(events, "QUIESCE")
    assert not _has(events, "SYSTEMCTL stop")
    assert not _has(events, "VENV")
    assert not _has(events, "RELEASE_PY -m backend.app.db")
    assert not _switch_events(events)
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
@pytest.mark.parametrize("mode", ["file", "dir", "escape"])
def test_a02_invalid_pointer_precheck_fails_closed_before_maintenance(
        tmp_path, script, mode):
    proc, sandbox, events, env, paths = _run(
        tmp_path, script, existing_deploy=True, db_exists=True,
        services_active=True, current_invalid=mode)
    assert proc.returncode != 0
    assert ("REFUSING" in _out(proc)) or ("FAILED" in _out(proc)), (
        _out(proc)[-2000:])
    assert not _drain(paths)
    assert not _has(events, "TOUCH")
    assert not _has(events, "SYSTEMCTL stop")
    assert not _has(events, "VENV")
    assert not _switch_events(events)
    if mode == "file":
        assert _read(paths["current"]) == "manual state\n"
    elif mode == "dir":
        assert os.path.isdir(paths["current"])
        assert not os.path.islink(paths["current"])
    else:
        assert os.path.realpath(paths["current"]) != _resolves(
            _prev(sandbox))


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
def test_a02b_unparsable_config_is_precheck(tmp_path, script):
    proc, sandbox, events, env, paths = _run(
        tmp_path, script, existing_deploy=True, db_exists=True,
        services_active=True, config_parse_fail=True)
    assert proc.returncode != 0
    assert "REFUSING: config parse failed" in _out(proc), _out(proc)[-2000:]
    assert _has(events, "CONFIG_PARSE_FAIL")
    assert not _drain(paths)
    assert not _has(events, "SYSTEMCTL stop")
    assert not _switch_events(events)
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
def test_a03_drain_creation_failure_aborts_before_maintenance_mutation(
        tmp_path, script):
    proc, sandbox, events, env, paths = _run(
        tmp_path, script, existing_deploy=True, db_exists=True,
        services_active=True, drain_create_fail=True)
    assert proc.returncode != 0
    assert "cannot create drain" in _out(proc)
    assert _has(events, "TOUCH_FAIL")
    assert not _drain(paths)
    assert not _has(events, "QUIESCE")
    assert not _has(events, "SYSTEMCTL stop")
    assert not _has(events, "VENV")
    assert not _has(events, "PROVISION")
    assert not _switch_events(events)
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
def test_a04_quiescence_unprovable_keeps_drain_and_mutation_boundary(
        tmp_path, script):
    proc, sandbox, events, env, paths = _run(
        tmp_path, script, existing_deploy=True, db_exists=True,
        services_active=True, quiesce_fail=True)
    assert proc.returncode != 0
    _assert_failed_closed(proc, paths)
    assert "quiescence timeout" in _out(proc)
    assert _has(events, "TOUCH") and _has(events, "QUIESCE_FAIL")
    assert not _has(events, "SYSTEMCTL stop")
    assert not _has(events, "PROVISION")
    assert not _has(events, "USERADD")
    assert not _has(events, "VENV")
    assert not _has(events, "ARCHIVE_VALIDATE")
    assert not _has(events, "PIP")
    assert not _has(events, "RELEASE_PY -m backend.app.db")
    assert not _has(events, "DEPLOY_RELEASE switch")
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))


def test_a05_host_runtime_prep_fails_after_drain_install(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "install.sh", existing_deploy=True, db_exists=True,
        services_active=True, provision_fail=True)
    _assert_failed_closed(proc, paths)
    assert "owner-execution effective access could not be provisioned" \
        in _out(proc)
    assert _has(events, "PROVISION_FAIL")
    assert not _has(events, "VENV")
    assert not _has(events, "RELEASE_PY -m backend.app.db")
    assert not _has(events, "DEPLOY_RELEASE switch")
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))


def test_a05b_host_runtime_secret_source_failure_upgrade(tmp_path):
    """upgrade.sh must not proceed when the secrets.env host mutation
    cannot be performed: release validation blocks the switch."""
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, secrets_env_present=False, etc_readonly=True)
    try:
        _assert_failed_closed(proc, paths)
        assert "stage validation blocked" in _out(proc)
        assert _has(events, "VALIDATE_BLOCKED_SECRETS")
        assert not _has(events, "RELEASE_PY -m backend.app.db migrate")
        assert not _has(events, "DEPLOY_RELEASE switch")
        assert _resolves(paths["current"]) == _resolves(_prev(sandbox))
        assert "Permission denied" in _out(proc), (
            "the host-prep mutation really failed (read-only /etc)")
    finally:
        os.chmod(paths["etc"], 0o755)


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
def test_a06_archive_staging_failure_keeps_everything(tmp_path, script):
    proc, sandbox, events, env, paths = _run(
        tmp_path, script, existing_deploy=True, db_exists=True,
        services_active=True, fault_point="stage")
    _assert_failed_closed(proc, paths)
    assert "FAULT INJECTED at stage" in _out(proc)
    assert not _has(events, "ARCHIVE_VALIDATE")
    assert not _has(events, "VENV")
    assert not _has(events, "DEPLOY_RELEASE switch")
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
def test_a07_archive_validation_failure_keeps_everything(tmp_path, script):
    proc, sandbox, events, env, paths = _run(
        tmp_path, script, existing_deploy=True, db_exists=True,
        services_active=True, archive_validate_fail=True)
    _assert_failed_closed(proc, paths)
    assert "archive validation blocked" in _out(proc)
    assert _has(events, "ARCHIVE_VALIDATE_FAIL")
    assert not _has(events, "VENV")
    assert not _has(events, "PIP")
    assert not _has(events, "DEPLOY_RELEASE switch")
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
def test_a08_venv_failure_keeps_everything(tmp_path, script):
    proc, sandbox, events, env, paths = _run(
        tmp_path, script, existing_deploy=True, db_exists=True,
        services_active=True, fault_point="venv")
    _assert_failed_closed(proc, paths)
    assert "FAULT INJECTED at venv" in _out(proc)
    assert not _has(events, "PIP")
    assert not _has(events, "DEPLOY_RELEASE switch")
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
def test_a08b_dependency_install_failure_keeps_everything(tmp_path, script):
    proc, sandbox, events, env, paths = _run(
        tmp_path, script, existing_deploy=True, db_exists=True,
        services_active=True, pip_fail=True)
    _assert_failed_closed(proc, paths)
    assert "requirements install failed" in _out(proc)
    assert _has(events, "PIP_FAIL")
    assert not _has(events, "DEPLOY_RELEASE switch")
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
def test_a09_frontend_release_artifact_missing_keeps_everything(
        tmp_path, script):
    proc, sandbox, events, env, paths = _run(
        tmp_path, script, existing_deploy=True, db_exists=True,
        services_active=True, missing_frontend=True)
    _assert_failed_closed(proc, paths)
    assert "missing built frontend" in _out(proc)
    assert _has(events, "TAR_MISSING_FRONTEND")
    assert not _has(events, "VENV")
    assert not _has(events, "DEPLOY_RELEASE switch")
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
def test_a10_release_validation_failure_keeps_everything(tmp_path, script):
    proc, sandbox, events, env, paths = _run(
        tmp_path, script, existing_deploy=True, db_exists=True,
        services_active=True, fault_point="validate")
    _assert_failed_closed(proc, paths)
    assert "FAULT INJECTED at validate" in _out(proc)
    assert not _has(events, "RELEASE_PY -m backend.app.db migrate")
    assert not _has(events, "DEPLOY_RELEASE switch")
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
def test_a11_db_backup_failure_blocks_migration(tmp_path, script):
    proc, sandbox, events, env, paths = _run(
        tmp_path, script, existing_deploy=True, db_exists=True,
        services_active=True, fault_point="backup")
    _assert_failed_closed(proc, paths)
    assert "FAULT INJECTED at backup" in _out(proc)
    assert "backup failed" in _out(proc)
    assert not _has(events, "RELEASE_PY -m backend.app.db migrate")
    assert not _has(events, "DEPLOY_RELEASE switch")
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
def test_a12_migration_failure_services_stopped_manual_no_restore(
        tmp_path, script):
    proc, sandbox, events, env, paths = _run(
        tmp_path, script, existing_deploy=True, db_exists=True,
        services_active=True, fault_point="migrate")
    _assert_failed_closed(proc, paths)
    assert "FAULT INJECTED at migrate" in _out(proc)
    assert "migration failed" in _out(proc)
    assert "MANUAL RECOVERY" in _out(proc)
    assert "POINTER RESTORE" not in _out(proc)
    assert not _has(events, "DEPLOY_RELEASE switch")
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))
    _assert_stopped_then_no_restart(events)


@pytest.mark.parametrize("compat,expect_restart", [
    (False, True), (True, False)])
def test_a13_post_migration_pre_switch_failure_compat_gated(
        tmp_path, compat, expect_restart):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, fault_point="pre-switch",
        compat_fail=compat)
    _assert_failed_closed(proc, paths)
    assert "FAULT INJECTED at pre-switch" in _out(proc)
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))
    assert not _switch_events(events)
    if expect_restart:
        assert "POINTER UNCHANGED" in _out(proc)
        assert "DATABASE NOT ROLLED BACK" in _out(proc)
        assert _has(events, "SYSTEMCTL start")
    else:
        assert "compatibility NOT proven" in _out(proc)
        assert "services NOT restarted" in _out(proc)
        assert not _has(events, "SYSTEMCTL start")


def test_a14_pointer_switch_preparation_failure_cleans_temp(
        tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, fault_point="pointer-switch-prep")
    _assert_failed_closed(proc, paths)
    assert "pointer-switch-prep" in _out(proc)
    assert "POINTER UNCHANGED" in _out(proc)
    assert not _restore_switch_events(events, "upgrade.sh")
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))
    prefix = paths["prefix"]
    leftovers = [name for name in os.listdir(prefix) if ".tmp." in name]
    assert leftovers == [], (
        "failed switch preparation must clean its temporary pointer: %r"
        % leftovers)


def test_a15_cas_race_fails_closed_without_overwriting_other_actor(
        tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, cas_race=True)
    _assert_failed_closed(proc, paths)
    assert _has(events, "CAS_RACE_SWITCH")
    assert "current pointer changed since inspection" in _out(proc)
    assert "atomic release switch failed" in _out(proc)
    assert _resolves(paths["current"]) == _resolves(_race(sandbox)), (
        "the racing actor's pointer must not be overwritten")
    assert _resolves(paths["current"]) != _resolves(
        _new(sandbox, "upgrade.sh"))
    # the failed switch carried the stale expected-current; never a
    # successful re-point at the candidate
    assert _count(events, "DEPLOY_RELEASE switch") >= 1
    assert not _has(events, "POINTER RESTORE COMPLETE")


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
def test_a16_primitive_replace_failure_reports_pointer_state(
        tmp_path, script, monkeypatch, capsys):
    """os.replace failure: the primitive must report current_replaced=False
    and leave `current` byte-identical (no missing-pointer window, no
    temp leftovers). The script-level recovery is covered by A14."""
    import backend.app.deploy_release as mod

    sandbox = tmp_path / "sandbox"
    releases = sandbox / "releases"
    releases.mkdir(parents=True)
    old = releases / _PREV
    new = releases / _COMMIT[script]
    for path in (old, new):
        path.mkdir()
        os.chmod(str(path), 0o755)
    current = sandbox / "current"
    os.symlink(str(old), str(current))

    def boom(src, dst):
        raise OSError(13, "simulated replace failure")

    monkeypatch.setattr(mod.os, "replace", boom)
    rc = mod.main(["switch", "--current", str(current), "--target", str(new),
                   "--releases-root", str(releases),
                   "--previous", str(old)])
    captured = capsys.readouterr()
    assert rc == mod.EXIT_SWITCH, captured.err
    assert os.readlink(str(current)) == str(old), (
        "a failed replace must leave the previous pointer intact")
    assert not [n for n in os.listdir(str(sandbox)) if ".tmp." in n]
    payload = captured.out.strip()
    assert '"current_replaced": false' in payload
    assert '"stage": "replace"' in payload
    assert "atomic replace failed" in captured.err


def test_a17_post_switch_verification_failure_treated_as_post_switch(
        tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, switch_post_replace_fail=True)
    _assert_failed_closed(proc, paths)
    assert _has(events, "DEPLOY_RELEASE_SWITCH_POST_REPLACE_FAIL")
    assert _has(events, "DEPLOY_RELEASE verify")
    assert "POINTER RESTORE COMPLETE" in _out(proc)
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))


def test_a17b_post_switch_verification_failure_compat_unknown(
        tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, switch_post_replace_fail=True,
        compat_fail=True)
    _assert_failed_closed(proc, paths)
    assert "compatibility NOT proven" in _out(proc)
    assert "MANUAL RECOVERY" in _out(proc)
    assert _resolves(paths["current"]) == _resolves(
        _new(sandbox, "upgrade.sh"))


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
@pytest.mark.parametrize("unit", ["ega-update-api", "ega-update-worker"])
def test_a18_service_start_failure_after_switch_restores_when_compat(
        tmp_path, script, unit):
    proc, sandbox, events, env, paths = _run(
        tmp_path, script, existing_deploy=True, db_exists=True,
        services_active=True, start_fail_units=[unit])
    _assert_failed_closed(proc, paths)
    assert _has(events, "SYSTEMCTL_FAIL")
    assert "POINTER RESTORE COMPLETE" in _out(proc), _out(proc)[-2500:]
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))
    assert _restore_switch_events(events, script)


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
def test_a18b_service_start_failure_compat_unknown_stays_new(
        tmp_path, script):
    proc, sandbox, events, env, paths = _run(
        tmp_path, script, existing_deploy=True, db_exists=True,
        services_active=True, start_fail_units=["ega-update-api"],
        compat_fail=True)
    _assert_failed_closed(proc, paths)
    assert "compatibility NOT proven" in _out(proc)
    assert "MANUAL RECOVERY" in _out(proc)
    assert _resolves(paths["current"]) == _resolves(_new(sandbox, script))
    assert not _restore_switch_events(events, script)
    assert _unit_state(sandbox, "ega-update-api") != "active"


@pytest.mark.parametrize("stage", _READINESS_STAGES)
def test_a19_readiness_stage_failures_block_and_restore_when_compat(
        tmp_path, stage):
    """A19-A23: each canonical readiness stage alone is sufficient to
    fail the deployment (401/403 never sells success)."""
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, ready_fail_stages=[stage])
    _assert_failed_closed(proc, paths)
    assert _has(events, "READINESS_FAIL_STAGES %s" % stage)
    assert stage in _out(proc)
    assert "POINTER RESTORE COMPLETE" in _out(proc)
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))
    assert _restore_switch_events(events, "upgrade.sh")


def test_a24_full_readiness_failure_compat_proven_restores_pointer_only(
        tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, ready_fail=True)
    _assert_failed_closed(proc, paths)
    assert _has(events, "READINESS_FAIL")
    message = _out(proc)
    assert "POINTER RESTORE COMPLETE" in message
    assert "no DATABASE ROLLBACK" in message
    assert "the DB migrated by" in message and "is NOT restored" in message
    assert "SERVICE RESTORATION: restarting units active before maintenance" \
        in message
    assert "rollback successful" not in message.lower()
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))
    assert _restore_switch_events(events, "upgrade.sh")
    # W11/B2: the failed attempt already started the candidate units, so
    # the restoration path must RESTART them so the restored pointer's
    # code is actually loaded (a no-op `start` left pointer=old with the
    # NEW processes still running).
    restore_at = _last(events, "DEPLOY_RELEASE switch")
    assert _last(events, "SYSTEMCTL restart ega-update-api") > restore_at
    assert _last(events, "SYSTEMCTL restart ega-update-worker") > restore_at
    assert not [i for i, event in enumerate(events)
                if i > restore_at and "SYSTEMCTL start ega-update-" in event]
    assert _has(events, "SYSTEMCTL_RESOLVED ega-update-api %s"
                % _resolves(_prev(sandbox)))
    assert _count(events, "DEPLOY_RELEASE switch") >= 2


@pytest.mark.parametrize("mode", ["compat_fail", "compat_error"])
def test_a25_compat_not_proven_keeps_new_pointer_stopped_drained(
        tmp_path, mode):
    kw = {mode: True}
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, ready_fail=True, **kw)
    _assert_failed_closed(proc, paths)
    assert "compatibility NOT proven" in _out(proc)
    assert "MANUAL RECOVERY REQUIRED" in _out(proc)
    assert "POINTER RESTORE COMPLETE" not in _out(proc)
    assert _resolves(paths["current"]) == _resolves(
        _new(sandbox, "upgrade.sh"))
    assert not _restore_switch_events(events, "upgrade.sh")
    last = _last(events, "SYSTEMCTL")
    assert last != -1 and events[last].startswith("SYSTEMCTL stop")
    assert _unit_state(sandbox, "ega-update-api") == "stopped"
    assert _unit_state(sandbox, "ega-update-worker") == "stopped"


def test_a26_pointer_restoration_failure_is_loud_manual_recovery(
        tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, ready_fail=True, fault_point="restore")
    _assert_failed_closed(proc, paths)
    assert "FAULT INJECTED at restore" in _out(proc)
    assert "RESTORE FAILED" in _out(proc)
    assert "MANUAL RECOVERY" in _out(proc)
    assert "POINTER RESTORE COMPLETE" not in _out(proc)
    assert "rollback successful" not in _out(proc).lower()
    assert _resolves(paths["current"]) == _resolves(
        _new(sandbox, "upgrade.sh"))
    assert _unit_state(sandbox, "ega-update-api") == "stopped"


@pytest.mark.parametrize("unit", ["ega-update-api", "ega-update-worker"])
def test_a27_service_restoration_failure_after_restore_is_not_success(
        tmp_path, unit):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, ready_fail=True,
        start_fail_units=[unit])
    _assert_failed_closed(proc, paths)
    assert "POINTER RESTORE COMPLETE" in _out(proc)
    assert "SERVICE RESTORATION" in _out(proc)
    assert _has(events, "SYSTEMCTL_FAIL")
    assert "done:" not in _out(proc)
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))
    assert _unit_state(sandbox, unit) != "active"


def test_a28_successful_upgrade_with_deployment_created_drain(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True)
    assert proc.returncode == 0, _out(proc)[-3000:]
    assert _resolves(paths["current"]) == _resolves(
        _new(sandbox, "upgrade.sh"))
    assert not _drain(paths)
    assert "drain removed" in _out(proc)
    assert "deployment lock acquired" in _out(proc)
    assert _has(events, "LOCK_HELD_DURING_MUTATION")
    assert not _has(events, "LOCK_FREE_DURING_MUTATION")
    assert _has(events, "READINESS_OK")


def test_a29_successful_upgrade_with_preexisting_drain_keeps_it(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, preexisting_drain=True)
    assert proc.returncode == 0, _out(proc)[-3000:]
    assert _drain(paths), "a pre-existing operator drain must be kept"
    assert not any(event.startswith("RM ") and event.endswith("drain")
                   for event in events)
    assert "pre-existing drain kept" in _out(proc)
    assert _resolves(paths["current"]) == _resolves(
        _new(sandbox, "upgrade.sh"))


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
def test_a30_repeated_invocation_after_success_is_fail_fast(
        tmp_path, script):
    first, sandbox, events1, env, paths = _run(
        tmp_path, script, existing_deploy=True, db_exists=True,
        services_active=True)
    assert first.returncode == 0, _out(first)[-3000:]
    assert _resolves(paths["current"]) == _resolves(_new(sandbox, script))

    second, sandbox2, events2, env2, paths2 = _run(
        tmp_path, script, existing_deploy=True, db_exists=True,
        services_active=True, preserve_pointer=True)
    assert sandbox2 == sandbox
    assert second.returncode != 0
    assert "already exists" in _out(second), _out(second)[-2000:]
    assert not _drain(paths), (
        "a repeated invocation must refuse BEFORE the maintenance boundary "
        "(no drain created)")
    assert not _has(events2, "TOUCH"), (
        "a repeated invocation must not create a drain")
    assert not _has(events2, "SYSTEMCTL stop"), (
        "a repeated invocation must not stop running services")
    assert not _has(events2, "RELEASE_PY -m backend.app.db")
    assert not _has(events2, "VENV")
    assert not _switch_events(events2)
    assert _resolves(paths["current"]) == _resolves(_new(sandbox, script))
    assert _unit_state(sandbox, "ega-update-api") == "active"


# =============================================================================
# B. Fresh install — 12 success sub-scenarios + 4 failure classes
# =============================================================================

def test_b_fresh_install_success_state_machine(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "install.sh", existing_deploy=False, db_exists=False)
    assert proc.returncode == 0, _out(proc)[-3000:]

    # no previous current / no previous release
    assert "previous release: <none>" in _out(proc)
    assert not _has(events, "DEPLOY_RELEASE inspect")
    switches = _switch_events(events)
    assert len(switches) == 1
    assert "--previous" not in switches[0]

    # first atomic current creation (B09)
    assert os.path.islink(paths["current"])
    assert _resolves(paths["current"]) == _resolves(
        _new(sandbox, "install.sh"))

    # user/group bootstrap (B05)
    assert _has(events, "USERADD")
    assert _has(events, "USERMOD")

    # runtime path prep + owner execution access (B06)
    assert _has(events, "PROVISION")

    # release validation and initial migration (B07/B08)
    assert _has(events, "VALIDATE_RELEASE")
    assert _has(events, "RELEASE_PY -m backend.app.db migrate")
    assert not _has(events, "RELEASE_PY -m backend.app.db backup"), (
        "a fresh install has no previous DB to back up")
    validate = _first(events, "VALIDATE_RELEASE")
    migrate = _first(events, "RELEASE_PY -m backend.app.db migrate")
    assert validate != -1 and migrate != -1 and validate < migrate

    # unit installation (B10)
    systemd_dir = os.path.join(paths["systemd"], "system")
    for unit in ("ega-update-api.service", "ega-update-worker.service",
                 "ega-update-runner@.service"):
        assert os.path.exists(os.path.join(systemd_dir, unit)), unit
    assert os.path.exists(os.path.join(
        systemd_dir, "ega-update-api.service.d", "10-port.conf"))
    assert os.path.exists(os.path.join(
        systemd_dir, "ega-update-worker.service.d", "10-user-bus.conf"))

    # service startup + canonical readiness (B11/B12)
    assert _has(events, "SYSTEMCTL start ega-update-api")
    assert _has(events, "SYSTEMCTL start ega-update-worker")
    assert _unit_state(sandbox, "ega-update-api") == "active"
    assert _unit_state(sandbox, "ega-update-worker") == "active"
    assert _has(events, "READINESS_OK")

    # success drain policy: fresh installs never create a drain
    assert not _drain(paths)
    assert not _has(events, "QUIESCE")
    assert "done: %s" % _COMMIT["install.sh"] in _out(proc)


def test_b05b_existing_api_account_is_not_recreated(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "install.sh", existing_deploy=True, db_exists=True,
        user_exists=True)
    assert proc.returncode == 0, _out(proc)[-2000:]
    assert not _has(events, "USERADD"), (
        "an existing service account must not be recreated")
    assert _has(events, "PROVISION")


def test_b_fresh_failure_before_first_pointer(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "install.sh", existing_deploy=False, db_exists=False,
        fault_point="validate")
    assert proc.returncode != 0
    assert not os.path.lexists(paths["current"]), (
        "a failed fresh install must not leave a current pointer")
    assert not _has(events, "DEPLOY_RELEASE switch")
    assert not _has(events, "SYSTEMCTL start")
    assert "POINTER RESTORE" not in _out(proc)
    assert not _drain(paths)


def test_b_fresh_failure_after_migration_before_pointer(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "install.sh", existing_deploy=False, db_exists=False,
        fault_point="pre-switch")
    assert proc.returncode != 0
    assert _has(events, "RELEASE_PY -m backend.app.db migrate")
    assert not os.path.lexists(paths["current"])
    assert not _has(events, "DEPLOY_RELEASE switch")
    assert not _has(events, "SYSTEMCTL start")
    assert "compatibility NOT proven" in _out(proc)
    assert "MANUAL RECOVERY" in _out(proc)
    assert "POINTER RESTORE COMPLETE" not in _out(proc)


def test_b_fresh_failure_after_first_pointer_before_readiness(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "install.sh", existing_deploy=False, db_exists=False,
        ready_fail=True)
    assert proc.returncode != 0
    assert _resolves(paths["current"]) == _resolves(
        _new(sandbox, "install.sh"))
    assert "compatibility NOT proven" in _out(proc)
    assert "MANUAL RECOVERY" in _out(proc)
    assert "POINTER RESTORE COMPLETE" not in _out(proc)
    assert not _restore_switch_events(events, "install.sh")
    assert _unit_state(sandbox, "ega-update-api") == "stopped"
    assert _unit_state(sandbox, "ega-update-worker") == "stopped"


def test_b_fresh_readiness_failure_on_first_install(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "install.sh", existing_deploy=False, db_exists=False,
        ready_fail_stages=["api_service_identity"])
    assert proc.returncode != 0
    assert _has(events, "READINESS_FAIL_STAGES api_service_identity")
    assert "api_service_identity" in _out(proc)
    assert _has(events, "SYSTEMCTL stop")
    assert _resolves(paths["current"]) == _resolves(
        _new(sandbox, "install.sh"))
    assert "done:" not in _out(proc)


# =============================================================================
# C. Lock / race (real kernel flock on a real tmp filesystem)
# =============================================================================

def test_c02_kernel_flock_released_on_exit_no_stale_metadata(tmp_path):
    lib = os.path.join(_REPO_ROOT, "deploy", "scripts", "lib",
                       "deploy_common.sh")
    lock = tmp_path / "deploy.lock"

    def attempt():
        return subprocess.run(
            ["/bin/bash", "-c",
             'source "$1" && ega_acquire_deploy_lock "$2" probe',
             "_", lib, str(lock)],
            capture_output=True, text=True, timeout=30)

    handle = open(str(lock), "a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = attempt()
        assert held.returncode != 0
        assert "another deployment is active" in held.stderr
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()

    first = attempt()
    assert first.returncode == 0, first.stderr
    assert lock.exists()
    assert _read(str(lock)) == "", (
        "the lock carries no PID/metadata for stale-lock cleanup")
    second = attempt()
    assert second.returncode == 0, (
        "the kernel must release the lock on exit without cleanup")
    missing = tmp_path / "no-such-dir" / "deploy.lock"
    refused = subprocess.run(
        ["/bin/bash", "-c",
         'source "$1" && ega_acquire_deploy_lock "$2" probe',
         "_", lib, str(missing)],
        capture_output=True, text=True, timeout=30)
    assert refused.returncode != 0
    assert "lock directory missing" in refused.stderr


# =============================================================================
# D. Atomic pointer (real temporary filesystem)
# =============================================================================

class _PointerLayout:
    def __init__(self, tmp_path):
        self.root = tmp_path / "releases"
        self.root.mkdir(parents=True, exist_ok=True)
        self.old = self.root / _PREV
        self.new = self.root / _COMMIT["upgrade.sh"]
        for path in (self.old, self.new):
            path.mkdir()
            os.chmod(str(path), 0o755)
        self.current = tmp_path / "current"
        os.symlink(str(self.old), str(self.current))

    def run(self, *args, env_extra=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = _REPO_ROOT
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, "-m", "backend.app.deploy_release"]
            + [str(a) for a in args],
            capture_output=True, text=True, timeout=60, env=env)

    def switch(self, target, previous=None, env_extra=None):
        args = ["switch", "--current", str(self.current),
                "--target", str(target), "--releases-root", str(self.root)]
        if previous is not None:
            args += ["--previous", str(previous)]
        return self.run(*args, env_extra=env_extra)


def test_d01_bidirectional_switch_never_exposes_missing_or_partial_current(
        tmp_path):
    layout = _PointerLayout(tmp_path)
    expected = {os.path.realpath(str(layout.old)),
                os.path.realpath(str(layout.new))}
    stop = threading.Event()
    missing = []
    unexpected = []

    def reader():
        while not stop.is_set():
            try:
                target = os.readlink(str(layout.current))
            except OSError:
                missing.append(1)
                continue
            resolved = os.path.realpath(target)
            if resolved not in expected:
                unexpected.append(resolved)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for index in range(20):
            target = layout.old if index % 2 == 0 else layout.new
            proc = layout.switch(target)
            assert proc.returncode == 0, proc.stderr
    finally:
        stop.set()
        thread.join(timeout=5)
    assert missing == [], (
        "reader observed a missing `current` (%d times)" % len(missing))
    assert unexpected == [], unexpected


def test_d02_pointer_target_rejections_leave_current_untouched(tmp_path):
    layout = _PointerLayout(tmp_path)
    before = os.readlink(str(layout.current))

    outside = tmp_path / "outside"
    outside.mkdir()
    relative = os.path.relpath(str(layout.new), str(layout.current.parent))
    symlink_candidate = layout.root / "alias"
    os.symlink(str(layout.new), str(symlink_candidate))
    writable = layout.root / "writable"
    writable.mkdir()
    os.chmod(str(writable), 0o777)
    plain = layout.root / "plain"
    plain.write_text("x\n", encoding="utf-8")

    cases = [(outside, 4), (relative, 4), (symlink_candidate, 4),
             (writable, 4), (plain, 4), (layout.root / ("f" * 40), 4)]
    for target, expected_rc in cases:
        proc = layout.switch(target)
        assert proc.returncode == expected_rc, (target, proc.stderr)
        assert os.readlink(str(layout.current)) == before, target
    stray = [n for n in os.listdir(str(tmp_path)) if ".tmp." in n]
    assert stray == []


def test_d02b_pointer_rejects_unexpected_current_type(tmp_path):
    layout = _PointerLayout(tmp_path)
    os.unlink(str(layout.current))
    layout.current.write_text("manual\n", encoding="utf-8")
    proc = layout.run("inspect", "--current", str(layout.current),
                      "--releases-root", str(layout.root))
    assert proc.returncode == 3
    assert proc.stderr.strip()
    os.unlink(str(layout.current))
    os.mkdir(str(layout.current))
    proc = layout.run("inspect", "--current", str(layout.current),
                      "--releases-root", str(layout.root))
    assert proc.returncode == 3


def test_d02c_pointer_rejects_escaping_current_target(tmp_path):
    layout = _PointerLayout(tmp_path)
    outside = tmp_path / "outside-release"
    outside.mkdir()
    os.unlink(str(layout.current))
    os.symlink(str(outside), str(layout.current))
    proc = layout.run("inspect", "--current", str(layout.current),
                      "--releases-root", str(layout.root))
    assert proc.returncode == 3
    os.unlink(str(layout.current))
    os.symlink(os.path.join(str(layout.root), "..", "..", "outside-release"),
               str(layout.current))
    proc = layout.run("inspect", "--current", str(layout.current),
                      "--releases-root", str(layout.root))
    assert proc.returncode == 3


def test_d02d_already_current_switch_is_idempotent(tmp_path):
    layout = _PointerLayout(tmp_path)
    before = os.lstat(str(layout.current)).st_ino
    proc = layout.switch(layout.old)
    assert proc.returncode == 0, proc.stderr
    assert '"already_current": true' in proc.stdout
    assert os.lstat(str(layout.current)).st_ino == before


def test_d03_parent_dir_fsync_is_best_effort_boundary(
        tmp_path, monkeypatch, capsys):
    """ATOMIC VISIBILITY (same-directory os.replace) is the V1 promise.
    CRASH DURABILITY additionally relies on a parent-directory fsync that
    is intentionally best-effort: when it fails the switch still commits
    (the failure is swallowed, no false success is claimed about disk
    durability)."""
    import backend.app.deploy_release as mod

    layout = _PointerLayout(tmp_path)
    calls = []
    real_fsync = os.fsync

    def tracking_fsync(fd):
        calls.append(os.fstat(fd))
        return real_fsync(fd)

    monkeypatch.setattr(mod.os, "fsync", tracking_fsync)
    rc = mod.main(["switch", "--current", str(layout.current),
                   "--target", str(layout.new),
                   "--releases-root", str(layout.root)])
    capsys.readouterr()
    assert rc == 0
    assert calls, "the success path must fsync the pointer parent dir"
    assert all(stat.S_ISDIR(info.st_mode) for info in calls)
    assert os.path.realpath(str(layout.current)) == os.path.realpath(
        str(layout.new))

    def failing_fsync(fd):
        raise OSError(5, "simulated EIO")

    layout2 = _PointerLayout(tmp_path / "second")
    monkeypatch.setattr(mod.os, "fsync", failing_fsync)
    rc = mod.main(["switch", "--current", str(layout2.current),
                   "--target", str(layout2.new),
                   "--releases-root", str(layout2.root)])
    capsys.readouterr()
    assert rc == 0, (
        "directory fsync is best-effort in V1; the atomic rename already "
        "committed and a false failure would be worse than the durability "
        "boundary")
    assert os.path.realpath(str(layout2.current)) == os.path.realpath(
        str(layout2.new))


# =============================================================================
# E. Migration compatibility matrix (A..F)
# =============================================================================

def test_e1_migration_not_started_pointer_unchanged_services_may_restart(
        tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, fault_point="validate")
    _assert_failed_closed(proc, paths)
    assert not _has(events, "RELEASE_PY -m backend.app.db migrate")
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))
    assert _has(events, "SYSTEMCTL start")
    assert "failure before migration" in _out(proc)
    assert "previously-active services restarted" in _out(proc)
    assert "DATABASE NOT ROLLED BACK" not in _out(proc)


def test_e2_migration_failed_never_boots_old_code_unproven(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, fault_point="migrate")
    _assert_failed_closed(proc, paths)
    assert "database state unproven" in _out(proc)
    assert not _has(events, "SYSTEMCTL start")
    assert "POINTER RESTORE" not in _out(proc)


def test_e3_migration_ok_compat_true_pointer_restore_permitted(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, ready_fail=True)
    _assert_failed_closed(proc, paths)
    assert "POINTER RESTORE COMPLETE" in _out(proc)
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))


def test_e4_migration_ok_compat_false_restore_forbidden(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, ready_fail=True, compat_fail=True)
    _assert_failed_closed(proc, paths)
    assert "compatibility NOT proven" in _out(proc)
    assert "pointer NOT restored" in _out(proc)
    assert _resolves(paths["current"]) == _resolves(
        _new(sandbox, "upgrade.sh"))
    assert not _restore_switch_events(events, "upgrade.sh")


def test_e5_compat_check_unavailable_restore_forbidden(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, ready_fail=True, compat_error=True)
    _assert_failed_closed(proc, paths)
    assert _has(events, "VALIDATE_COMPAT_ERROR")
    assert "compatibility NOT proven" in _out(proc)
    assert "MANUAL RECOVERY REQUIRED" in _out(proc)
    assert _resolves(paths["current"]) == _resolves(
        _new(sandbox, "upgrade.sh"))


def test_e6_pointer_restored_service_restoration_fails_manual(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, ready_fail=True,
        start_fail_units=["ega-update-worker", "ega-update-api"])
    _assert_failed_closed(proc, paths)
    assert "POINTER RESTORE COMPLETE" in _out(proc)
    assert _has(events, "SYSTEMCTL_FAIL")
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))
    assert "done:" not in _out(proc)
    assert "inspect, reconcile" in _out(proc), (
        "restoration failure must leave an explicit manual-reconcile trace")


def test_e7_message_vocabulary_distinguishes_restore_terms(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, ready_fail=True)
    _assert_failed_closed(proc, paths)
    message = _out(proc)
    assert "POINTER RESTORE COMPLETE" in message
    assert "no DATABASE ROLLBACK" in message
    assert "is NOT restored" in message
    assert "SERVICE RESTORATION" in message
    # never the forbidden conflation
    assert "database rollback complete" not in message.lower()
    assert "full rollback" not in message.lower()


# =============================================================================
# F. Unit / drop-in artifact boundary after a pointer restore
# =============================================================================

def test_f1_units_copied_from_candidate_are_not_reverted(tmp_path):
    """Documented V1 boundary: automatic recovery reverts the POINTER only.
    Units copied from the candidate remain; restart runs the restored
    pointer's code with candidate-template units (assumed compatible)."""
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, ready_fail=True, unit_content_from_commit=True)
    _assert_failed_closed(proc, paths)
    assert _resolves(paths["current"]) == _resolves(_prev(sandbox))
    worker_unit = os.path.join(paths["systemd"], "system",
                               "ega-update-worker.service")
    assert os.path.exists(worker_unit)
    assert _COMMIT["upgrade.sh"] in _read(worker_unit), (
        "the copied unit artifact still embeds the candidate release")
    assert _PREV not in _read(worker_unit)
    # the port drop-in is rendered from config, not from the release
    dropin = os.path.join(paths["systemd"], "system",
                          "ega-update-api.service.d", "10-port.conf")
    assert os.path.exists(dropin)
    assert "EGA_LISTEN_PORT=8771" in _read(dropin)
    runbook = _read(os.path.join(_REPO_ROOT, "docs", "RUNBOOK.md"))
    assert "release-versioned systemd unit files" in runbook, (
        "the V1 unit-artifact boundary must stay documented")
    assert "V1 boundary" in runbook


def test_f2_unit_templates_are_pointer_relative_and_compatible():
    """The V1 assumption behind F1: every shipped unit resolves its code
    through /opt/ega-update/current (or is documentation-only), so the
    restored pointer's code runs with the installed units."""
    unit_dir = os.path.join(_REPO_ROOT, "systemd")
    unit_files = []
    for dirpath, _dirnames, filenames in os.walk(unit_dir):
        for name in filenames:
            if name.endswith(".service"):
                unit_files.append(os.path.join(dirpath, name))
    assert unit_files
    for path in unit_files:
        for line in _read(path).splitlines():
            if not (line.startswith("ExecStart=")
                    or line.startswith("ExecStartPre=")):
                continue
            assert "/opt/ega-update/releases/" not in line, (
                "%s pins a versioned release path: %s" % (path, line))
            if "venv/bin/" in line:
                assert "/opt/ega-update/current/" in line, (
                    "%s does not resolve code through current: %s"
                    % (path, line))
            for forbidden in ("install.sh", "upgrade.sh",
                              "deploy_release", "db migrate"):
                assert forbidden not in line, (path, line)


# =============================================================================
# G. Reboot recovery: no boot path guesses a migration/pointer operation
# =============================================================================

def test_g_no_unit_performs_deployment_or_migration_on_boot():
    unit_dir = os.path.join(_REPO_ROOT, "systemd")
    offenders = []
    for dirpath, _dirnames, filenames in os.walk(unit_dir):
        for name in filenames:
            if not name.endswith(".service"):
                continue
            path = os.path.join(dirpath, name)
            for line in _read(path).splitlines():
                if not (line.startswith("ExecStart=")
                        or line.startswith("ExecStartPre=")):
                    continue
                if any(token in line for token in
                       ("install.sh", "upgrade.sh", "deploy_release",
                        "backend.app.db migrate", "backend.app.db backup",
                        "systemctl switch")):
                    offenders.append((path, line))
    assert offenders == [], (
        "no boot path may run migration/backup/pointer operations: %r"
        % offenders)


def test_g_services_validate_schema_at_startup_not_migrate():
    main = _read(os.path.join(_REPO_ROOT, "backend", "app", "main.py"))
    assert "validate_schema(conn)" in main
    assert "migrate" not in main, (
        "service startup validates the schema; it never migrates")
    dispatch = _read(os.path.join(_REPO_ROOT, "backend", "app", "worker",
                                  "dispatch.py"))
    assert "validate_schema" in dispatch


def test_g_failed_migration_state_never_claims_or_guesses_success(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, fault_point="migrate")
    _assert_failed_closed(proc, paths)
    assert "MANUAL RECOVERY REQUIRED" in _out(proc)
    assert "migration-recovery" in _out(proc)
    assert "POINTER RESTORE COMPLETE" not in _out(proc)
    assert "done:" not in _out(proc)


# =============================================================================
# H. Success acceptance evidence
# =============================================================================

def test_h_success_requires_state_machine_evidence_not_just_exit_zero(
        tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True)
    assert proc.returncode == 0, _out(proc)[-3000:]
    message = _out(proc)

    # lock held during the mutation window
    assert "deployment lock acquired" in message
    assert _has(events, "LOCK_HELD_DURING_MUTATION")
    assert not _has(events, "LOCK_FREE_DURING_MUTATION")

    # drain + quiescence + proven stopped before any host/release mutation
    drain_touch = _first(events, "TOUCH") if _has(events, "TOUCH") else -1
    quiesce = _first(events, "QUIESCE")
    stop = _first(events, "SYSTEMCTL stop")
    venv = _first(events, "VENV")
    assert -1 not in (drain_touch, quiesce, stop, venv)
    assert drain_touch < quiesce < stop < venv

    # backup before migration; migration known before the switch
    backup = _first(events, "RELEASE_PY -m backend.app.db backup")
    migrate = _first(events, "RELEASE_PY -m backend.app.db migrate")
    switch = _first(events, "DEPLOY_RELEASE switch")
    assert -1 not in (backup, migrate, switch)
    assert backup < migrate < switch

    # atomic switch: exactly one switch through the shared primitive
    assert _count(events, "DEPLOY_RELEASE switch") == 1
    assert "--previous" in _switch_events(events)[0]

    # services started as intended
    assert _has(events, "SYSTEMCTL restart ega-update-api")
    assert _has(events, "SYSTEMCTL restart ega-update-worker")
    assert _unit_state(sandbox, "ega-update-api") == "active"
    assert _unit_state(sandbox, "ega-update-worker") == "active"

    # all D8 readiness stages green
    assert _has(events, "READINESS_OK")

    # exact deployed release identified
    assert _resolves(paths["current"]) == _resolves(
        _new(sandbox, "upgrade.sh"))
    assert "done: %s" % _COMMIT["upgrade.sh"] in message

    # no fault hook active
    assert "FAULT INJECTED" not in message
    assert not env.get("EGA_DEPLOY_FAULT_POINT")

    # final drain policy: removed only after readiness and only if we
    # created it
    remove = max((index for index, event in enumerate(events)
                  if event.startswith("RM ") and event.endswith("drain")),
                 default=-1)
    ready = _first(events, "READINESS_OK")
    assert remove != -1 and ready < remove
    assert not _drain(paths)


def test_h_unknown_fault_point_is_inert(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, fault_point="not-a-real-point")
    assert proc.returncode == 0, _out(proc)[-2000:]
    assert "FAULT INJECTED" not in _out(proc)
    assert not _drain(paths)
    assert _resolves(paths["current"]) == _resolves(
        _new(sandbox, "upgrade.sh"))


def test_h_fault_hook_is_inert_unless_exact_point(tmp_path):
    lib = os.path.join(_REPO_ROOT, "deploy", "scripts", "lib",
                       "deploy_common.sh")
    script = 'source "$1"; ega_maybe_fail "$2" probe; echo "rc=$?"'
    base_env = dict(os.environ)

    def probe(point):
        env = dict(base_env)
        env.pop("EGA_DEPLOY_FAULT_POINT", None)
        if point is not None:
            env["EGA_DEPLOY_FAULT_POINT"] = point
        return subprocess.run(
            ["/bin/bash", "-c", script, "_", lib, "some-point"],
            capture_output=True, text=True, timeout=30, env=env)

    unset = probe(None)
    assert unset.returncode == 0 and "rc=0" in unset.stdout
    assert "FAULT INJECTED" not in unset.stderr
    other = probe("unrelated-point")
    assert other.returncode == 0 and "rc=0" in other.stdout
    assert "FAULT INJECTED" not in other.stderr
    exact = probe("some-point")
    assert "rc=1" in exact.stdout
    assert "FAULT INJECTED at some-point" in exact.stderr


def test_h_committed_defaults_cannot_enable_a_fault():
    import json as _json

    example = _json.loads(_read(os.path.join(
        _REPO_ROOT, "deploy", "etc", "config.example.json")))
    assert "EGA_DEPLOY_FAULT_POINT" not in _json.dumps(example)
    for rel in ("deploy/scripts/install.sh", "deploy/scripts/upgrade.sh"):
        text = _read(os.path.join(_REPO_ROOT, rel))
        assert "export EGA_DEPLOY_FAULT_POINT" not in text
        assert "EGA_DEPLOY_FAULT_POINT=" not in text, (
            "%s must never set the fault variable" % rel)
    unit_dir = os.path.join(_REPO_ROOT, "systemd")
    for dirpath, _dirnames, filenames in os.walk(unit_dir):
        for name in filenames:
            assert "EGA_DEPLOY_FAULT_POINT" not in _read(
                os.path.join(dirpath, name))


def test_h_pointer_switch_is_the_only_supported_switch():
    import re

    def legacy_lines(text):
        hits = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if re.search(r"(^|[;&|()\s])ln\s+-sfn", line):
                hits.append(stripped)
        return hits

    for rel in ("deploy/scripts/lib/deploy_common.sh",
                "deploy/scripts/install.sh", "deploy/scripts/upgrade.sh"):
        text = _read(os.path.join(_REPO_ROOT, rel))
        assert legacy_lines(text) == [], (rel, legacy_lines(text))


# =============================================================================
# I. Disposable-VM acceptance harness (manual; guarded)
# =============================================================================

_VM_HARNESS = os.path.join(_REPO_ROOT, "deploy", "tests",
                           "vm-acceptance-failure-matrix.sh")


def test_i_vm_harness_is_marked_manual_and_guarded():
    assert os.path.isfile(_VM_HARNESS)
    text = _read(_VM_HARNESS)
    for token in ("EGA_VM_ACCEPTANCE", "EGA_VM_DISPOSABLE", "NOT RUN",
                  "MANUAL", "REFUSING", "NEVER RUN AUTOMATICALLY"):
        assert token in text, token


def test_i_vm_harness_refuses_without_explicit_enable():
    proc = subprocess.run(["/bin/bash", _VM_HARNESS], capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "NOT RUN" in (proc.stdout + proc.stderr)


@pytest.mark.skipif(os.geteuid() == 0,
                    reason="never invoke with EGA_VM_ACCEPTANCE set as root")
def test_i_vm_harness_refuses_non_root():
    env = dict(os.environ)
    env["EGA_VM_ACCEPTANCE"] = "1"
    env["EGA_VM_DISPOSABLE"] = "1"
    proc = subprocess.run(["/bin/bash", _VM_HARNESS], capture_output=True,
                          text=True, timeout=60, env=env)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "root" in (proc.stdout + proc.stderr).lower()
