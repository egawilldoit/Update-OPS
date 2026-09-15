"""W11 regressions for the real-deployment upgrade failures.

Real deployment attempt of 114d256 exposed three upgrade-path bugs:

B1  upgrade.sh never ran the canonical owner-execution access provisioning
    (backend.app.owner_env provision) that install.sh performs, so a host
    installed before the named-user ACL model failed readiness stage
    ``owner_transient_execution``.
B2  The post-switch pointer-restore path printed "restarting units" but
    executed ``systemctl start`` on units already started by the failed
    attempt, leaving pointer=old / processes=new until a manual restart.
B3  ``su -s /bin/bash ubuntu -c 'systemctl --user daemon-reload'`` ran
    without the resolved user-bus environment, so the copied user runner
    unit was never reloaded ("Failed to connect to bus").

These tests run the REAL deploy scripts through the sandbox harness.
"""
from __future__ import annotations

import os
import subprocess
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import deploy_harness  # noqa: E402

_PREV_NAME = "0" * 40
_UI_COMMIT = {"install.sh": "1" * 40, "upgrade.sh": "2" * 40}
_BUS_ENV = ("XDG_RUNTIME_DIR=/run/user/1001 "
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus")


def _run(tmp_path, script, **kw):
    return deploy_harness.run_deploy_script(
        tmp_path, _REPO_ROOT, script, **kw)


def _current(sandbox):
    return os.path.join(sandbox, "opt", "ega-update", "current")


def _prev(sandbox):
    return os.path.join(sandbox, "opt", "ega-update", "releases", _PREV_NAME)


def _new(sandbox, script):
    return os.path.join(sandbox, "opt", "ega-update", "releases",
                        _UI_COMMIT[script])


def _resolves(path):
    return os.path.realpath(path)


def _has(events, needle):
    return any(needle in event for event in events)


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


def _indices(events, needle):
    return [i for i, event in enumerate(events) if needle in event]


def _assert_failed_closed(proc, paths):
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert os.path.exists(os.path.join(paths["state"], "drain")), (
        "failure after the maintenance boundary must keep the drain")
    assert "drain removed" not in proc.stdout + proc.stderr


def _provision_options(event):
    """Option sequence + owner/group values from a PROVISION event."""
    words = event.split()
    begin = words.index("provision") + 1
    options = [w for w in words[begin:] if w.startswith("--")]
    values = {}
    for flag, value in zip(options, words[begin + 1::2]):
        values[flag] = value
    return options, values


# --------------------------------------------------------------------------
# B1 - upgrade.sh provisions owner-execution access (install parity)
# --------------------------------------------------------------------------

def test_upgrade_provisions_owner_access_after_maintenance_boundary(
        tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    provision = _first(events, "PROVISION")
    assert provision != -1, (
        "upgrade never provisioned owner-execution access "
        "(B1: install.sh-only provisioning)")
    quiesce = _first(events, "QUIESCE")
    stop = _first(events, "SYSTEMCTL stop")
    venv = _first(events, "VENV")
    readiness = _first(events, "READINESS_OK")
    assert -1 not in (quiesce, stop, venv, readiness)
    assert quiesce < stop < provision < venv < readiness, events
    # Provisioning uses the canonical owner_env mechanism, never a wider
    # chmod/chown path.
    assert _has(events, "PROVISION -m backend.app.owner_env provision")


def test_upgrade_provisioning_matches_install_contract(tmp_path):
    install_proc, _s1, install_events, _e1, _p1 = _run(
        tmp_path / "install", "install.sh", existing_deploy=False,
        db_exists=False)
    assert install_proc.returncode == 0, install_proc.stdout + install_proc.stderr
    upgrade_proc, _s2, upgrade_events, _e2, _p2 = _run(
        tmp_path / "upgrade", "upgrade.sh", existing_deploy=True,
        db_exists=True, services_active=True)
    assert upgrade_proc.returncode == 0, upgrade_proc.stdout + upgrade_proc.stderr

    def provision_event(events):
        for event in events:
            if event.startswith("PROVISION "):
                return event
        raise AssertionError("no PROVISION event: %r" % events)

    install_options, install_values = _provision_options(
        provision_event(install_events))
    upgrade_options, upgrade_values = _provision_options(
        provision_event(upgrade_events))
    assert upgrade_options == install_options, (
        "upgrade/install provisioning option parity: %r != %r"
        % (upgrade_options, install_options))
    assert upgrade_values["--owner"] == install_values["--owner"] == "ubuntu"
    assert upgrade_values["--group"] == install_values["--group"] == "ega-update"
    for required in ("--state-dir", "--log-dir", "--backup-dir",
                     "--config-dir", "--config-file", "--secrets-file",
                     "--inventory-file"):
        assert required in upgrade_values, required


def test_upgrade_provisioning_failure_fails_closed(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, provision_fail=True)
    _assert_failed_closed(proc, paths)
    out = proc.stdout + proc.stderr
    assert "owner-execution effective access could not be provisioned" in out, out
    assert _resolves(_current(sandbox)) == _resolves(_prev(sandbox)), (
        "provisioning failure must leave the previous pointer linked")
    # Restored services were stopped at the maintenance boundary, so the
    # pre-migration failure path starts them again (never restart-claim).
    assert _last(events, "SYSTEMCTL start ega-update-worker") > _first(
        events, "PROVISION_FAIL")


def test_install_still_provisions_owner_access_after_maintenance(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "install.sh", existing_deploy=False, db_exists=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    provision = _first(events, "PROVISION")
    assert provision != -1
    assert _first(events, "QUIESCE") < provision


# --------------------------------------------------------------------------
# B2 - post-switch restore restarts services on the RESTORED release
# --------------------------------------------------------------------------

def test_post_switch_restore_restarts_services_on_restored_release(
        tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, fault_point="post-switch")
    _assert_failed_closed(proc, paths)
    assert _resolves(_current(sandbox)) == _resolves(_prev(sandbox)), (
        "compat-proven post-switch failure must restore the previous pointer")

    restored = _indices(events, "DEPLOY_RELEASE switch")
    restore_at = max(
        i for i in restored
        if ("--previous %s" % _new(sandbox, "upgrade.sh")) in events[i])
    api_restart = _last(events, "SYSTEMCTL restart ega-update-api")
    worker_restart = _last(events, "SYSTEMCTL restart ega-update-worker")
    assert api_restart > restore_at, (
        "restoration must actually restart the API (B2: 'start' on a "
        "running unit is a no-op): %r" % events[restore_at:])
    assert worker_restart > restore_at, (
        "restoration must actually restart the worker")
    assert not [i for i in _indices(events, "SYSTEMCTL start ega-update-api")
                if i > restore_at]
    assert not [i for i in _indices(events, "SYSTEMCTL start ega-update-worker")
                if i > restore_at]

    resolved = [event for event in events
                if event.startswith("SYSTEMCTL_RESOLVED ega-update-")]
    assert len(resolved) >= 2, events
    # The LAST start/restart of each unit is the restoration restart and
    # must resolve the RESTORED release (pointer=old, processes=old).
    last_api = [e for e in resolved
                if e.startswith("SYSTEMCTL_RESOLVED ega-update-api")][-1]
    last_worker = [e for e in resolved
                   if e.startswith("SYSTEMCTL_RESOLVED ega-update-worker")][-1]
    assert last_api.endswith(_resolves(_prev(sandbox))), (
        "restoration restart resolved %r, expected the restored release"
        % last_api)
    assert last_worker.endswith(_resolves(_prev(sandbox))), (
        "restoration restart resolved %r, expected the restored release"
        % last_worker)
    assert "SERVICE RESTORATION: restarting units active before maintenance" \
        in proc.stderr


def test_pre_migration_failure_keeps_start_semantics(tmp_path):
    """Only the switched-restore path may restart; pre-migration failures
    reconcile previously-active (stopped) services with `start`."""
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True, fault_point="pre-switch")
    _assert_failed_closed(proc, paths)
    assert _resolves(_current(sandbox)) == _resolves(_prev(sandbox))
    assert _has(events, "SYSTEMCTL start ega-update-api"), events
    assert not _has(events, "SYSTEMCTL restart ega-update-api")


# --------------------------------------------------------------------------
# B3 - canonical user-bus environment for the user-manager daemon-reload
# --------------------------------------------------------------------------

def test_upgrade_user_daemon_reload_uses_canonical_bus_env(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "upgrade.sh", existing_deploy=True, db_exists=True,
        services_active=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    reload_events = [event for event in events
                     if event.startswith("SU ") and "daemon-reload" in event]
    assert reload_events, events
    event = reload_events[0]
    assert "systemctl --user daemon-reload" in event
    assert "XDG_RUNTIME_DIR=/run/user/1001" in event, (
        "B3: daemon-reload must carry the resolved user-bus XDG_RUNTIME_DIR")
    assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus" in event, (
        "B3: daemon-reload must carry the resolved user-bus address")
    assert "user manager reloaded" in proc.stdout + proc.stderr


def test_install_user_daemon_reload_uses_canonical_bus_env(tmp_path):
    proc, sandbox, events, env, paths = _run(
        tmp_path, "install.sh", existing_deploy=False, db_exists=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    reload_events = [event for event in events
                     if event.startswith("SU ") and "daemon-reload" in event]
    assert reload_events, events
    assert _BUS_ENV.split()[0] in reload_events[0]
    assert _BUS_ENV.split()[1] in reload_events[0]


def test_owner_env_bus_env_subcommand(monkeypatch, capsys):
    from backend.app import owner_env

    monkeypatch.setattr(owner_env, "systemd_user_bus", lambda user="ubuntu": {
        "XDG_RUNTIME_DIR": "/run/user/1001",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1001/bus",
    })
    assert owner_env.main(["bus-env", "--owner", "ubuntu"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == _BUS_ENV, out


def test_owner_env_bus_env_fails_when_bus_unresolvable(monkeypatch, capsys):
    from backend.app import owner_env

    monkeypatch.setattr(owner_env, "systemd_user_bus",
                        lambda user="ubuntu": {})
    assert owner_env.main(["bus-env", "--owner", "ubuntu"]) != 0
    assert capsys.readouterr().out.strip() == ""


# --------------------------------------------------------------------------
# B1/B3 root cause: `python3 -m` puts the caller's CWD first on sys.path,
# so a stale checkout in the operator's working directory silently shadows
# the trusted module. Observed on the real VM: /home/ubuntu/Update-OPS
# (an old CLI-less owner_env.py) made `-m backend.app.owner_env provision`
# exit 0 while doing nothing. All trusted-checkout module invocations must
# run in a subshell with CWD pinned (same rule as ega_deploy_release).
# --------------------------------------------------------------------------

def test_user_bus_env_helper_pins_trusted_checkout_cwd(tmp_path):
    decoy = tmp_path / "decoy-cwd"
    decoy.mkdir()
    trusted = tmp_path / "trusted-repo"
    trusted.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "pwd-record.txt"
    fake_py = bin_dir / "python3"
    fake_py.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$PWD\" >> '" + str(record) + "'\n"
        "printf 'XDG_RUNTIME_DIR=/run/user/1001 "
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus\\n'\n",
        encoding="utf-8")
    fake_py.chmod(0o755)
    common = os.path.join(_REPO_ROOT, "deploy", "scripts", "lib",
                          "deploy_common.sh")
    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    proc = subprocess.run(
        ["/bin/bash", "-c",
         'source "%s" && ega_user_bus_env ubuntu "%s"' % (common, trusted)],
        cwd=str(decoy), env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "XDG_RUNTIME_DIR=/run/user/1001" in proc.stdout
    assert record.read_text(encoding="utf-8").strip() == str(trusted), (
        "ega_user_bus_env must resolve its module from the trusted "
        "checkout CWD, never the operator's working directory")


def test_deploy_scripts_pin_cwd_for_trusted_module_invocations():
    for rel in ("deploy/scripts/install.sh", "deploy/scripts/upgrade.sh"):
        path = os.path.join(_REPO_ROOT, rel)
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        marker = "python3 -m backend.app.owner_env provision"
        assert marker in text, rel
        head = text[:text.index(marker)]
        assert 'cd "$REPO_ROOT" || exit 1' in head[-400:], (
            "%s: the owner_env provision invocation must run in a "
            "subshell with CWD pinned to the trusted checkout" % rel)
