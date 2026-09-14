"""W4-D8 deploy script ordering + single readiness primitive (executable).

Runs the real install.sh/upgrade.sh inside a tmp_path sandbox with PATH
shims (see deploy_harness.py) and asserts the OBSERVED event order:
for an existing deployment, no host/runtime mutation (account/dirs,
linger, ACL provisioning, release staging, venv) happens before the
drain + proven-quiescence maintenance boundary. Also pins the single
shared readiness gate consumed by both scripts and the documented drain
success/failure policy.

These tests never touch the host: all rewritten paths are under tmp_path
and every mutating external command is shimmed.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import deploy_harness  # noqa: E402

LIB_REL = os.path.join("deploy", "scripts", "lib", "deploy_common.sh")


def _read(rel):
    with open(os.path.join(_REPO_ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def _events(events, prefix):
    return [e for e in events if e.startswith(prefix)]


def _first(events, needle):
    for index, event in enumerate(events):
        if needle in event:
            return index
    return -1


def _assert_existing_deploy_boundary(events):
    drain = next((i for i, e in enumerate(events)
                  if e.startswith("TOUCH") and e.endswith("drain")), -1)
    quiesce = _first(events, "QUIESCE")
    provision = _first(events, "PROVISION")
    useradd = _first(events, "USERADD")
    venv = _first(events, "VENV")
    assert drain != -1, "existing deploy never created the drain"
    assert quiesce != -1, "existing deploy never proved quiescence"
    assert provision != -1, "install never provisioned owner access"
    assert drain < quiesce, "drain must precede the quiescence proof"
    assert quiesce < provision, (
        "owner-execution ACL provisioning ran BEFORE drain+quiescence "
        "(defect D8-1): %r" % events[:40])
    assert quiesce < useradd, (
        "account mutation ran BEFORE drain+quiescence: %r" % events[:40])
    assert quiesce < venv, (
        "release preparation ran BEFORE drain+quiescence: %r" % events[:40])
    stop = _first(events, "SYSTEMCTL stop")
    assert stop != -1 and quiesce < stop, "services stopped after quiescence"


def test_existing_deploy_mutations_only_after_maintenance_boundary(tmp_path):
    proc, sandbox, events, env, paths = deploy_harness.run_deploy_script(
        tmp_path, _REPO_ROOT, "install.sh", existing_deploy=True,
        db_exists=True)
    assert proc.returncode == 0, (
        "install.sh failed in sandbox: rc=%s stderr=%s" %
        (proc.returncode, proc.stderr[-2000:]))
    _assert_existing_deploy_boundary(events)
    # Release prep (venv install) happens after the boundary.
    assert _first(events, "VENV") > _first(events, "QUIESCE")
    # Success path removes the deployment-created drain.
    assert not os.path.exists(os.path.join(paths["state"], "drain"))
    assert _first(events, "READINESS_OK") != -1


def test_fresh_install_needs_no_drain_or_quiescence(tmp_path):
    proc, sandbox, events, env, paths = deploy_harness.run_deploy_script(
        tmp_path, _REPO_ROOT, "install.sh", existing_deploy=False,
        db_exists=False)
    assert proc.returncode == 0, (
        "fresh install failed in sandbox: rc=%s stderr=%s" %
        (proc.returncode, proc.stderr[-2000:]))
    assert _first(events, "QUIESCE") == -1, (
        "fresh install must not require a pre-existing drain/quiescence "
        "proof")
    assert not os.path.exists(os.path.join(paths["state"], "drain"))
    # Runtime prerequisites are still proven before success.
    assert _first(events, "READINESS_OK") != -1


def test_pre_existing_drain_remains_after_success(tmp_path):
    proc, sandbox, events, env, paths = deploy_harness.run_deploy_script(
        tmp_path, _REPO_ROOT, "install.sh", existing_deploy=True,
        db_exists=True, preexisting_drain=True)
    assert proc.returncode == 0, proc.stderr[-2000:]
    drain = os.path.join(paths["state"], "drain")
    assert os.path.exists(drain), (
        "a pre-existing (operator) drain was removed by the deployment")
    assert not any(e.startswith("RM ") and e.endswith("drain")
                   for e in events), "deployment removed an operator drain"


def test_deployment_created_drain_removed_only_on_success(tmp_path):
    proc, sandbox, events, env, paths = deploy_harness.run_deploy_script(
        tmp_path, _REPO_ROOT, "install.sh", existing_deploy=True,
        db_exists=True)
    assert proc.returncode == 0, proc.stderr[-2000:]
    drain = os.path.join(paths["state"], "drain")
    assert not os.path.exists(drain)
    ready = _first(events, "READINESS_OK")
    removed = next((i for i, e in enumerate(events)
                    if e.startswith("RM ") and e.endswith("drain")), -1)
    assert ready != -1 and removed > ready, (
        "drain removed before operational acceptance")


def test_readiness_failure_keeps_drain(tmp_path):
    proc, sandbox, events, env, paths = deploy_harness.run_deploy_script(
        tmp_path, _REPO_ROOT, "install.sh", existing_deploy=True,
        db_exists=True, ready_fail=True)
    assert proc.returncode != 0
    assert os.path.exists(os.path.join(paths["state"], "drain")), (
        "failed deployment removed the drain (must fail closed)")
    assert _first(events, "READINESS_FAIL") != -1
    assert not any(e.startswith("RM ") and e.endswith("drain")
                   for e in events)


def test_upgrade_readiness_follows_drain_and_uses_shared_gate(tmp_path):
    proc, sandbox, events, env, paths = deploy_harness.run_deploy_script(
        tmp_path, _REPO_ROOT, "upgrade.sh", existing_deploy=True,
        db_exists=True, services_active=True)
    assert proc.returncode == 0, (
        "upgrade.sh failed in sandbox: rc=%s stderr=%s" %
        (proc.returncode, proc.stderr[-2000:]))
    drain = next((i for i, e in enumerate(events)
                  if e.startswith("TOUCH") and e.endswith("drain")), -1)
    stop = _first(events, "SYSTEMCTL stop")
    ready = _first(events, "READINESS_OK")
    assert drain != -1 and stop != -1 and ready != -1
    assert drain < stop < ready
    assert _first(events, "VENV") > drain
    assert not os.path.exists(os.path.join(paths["state"], "drain"))


def test_upgrade_readiness_failure_keeps_drain(tmp_path):
    proc, sandbox, events, env, paths = deploy_harness.run_deploy_script(
        tmp_path, _REPO_ROOT, "upgrade.sh", existing_deploy=True,
        db_exists=True, services_active=True, ready_fail=True)
    assert proc.returncode != 0
    assert _first(events, "READINESS_FAIL") != -1
    assert os.path.exists(os.path.join(paths["state"], "drain"))


# -- single readiness primitive -----------------------------------------------

def test_scripts_share_one_readiness_primitive():
    lib_path = os.path.join(_REPO_ROOT, LIB_REL)
    assert os.path.exists(lib_path), (
        "shared deploy helper %s is missing" % LIB_REL)
    lib = _read(LIB_REL)
    install = _read(os.path.join("deploy", "scripts", "install.sh"))
    upgrade = _read(os.path.join("deploy", "scripts", "upgrade.sh"))
    assert "ega_wait_for_readiness" in lib
    assert "status --require-ready" in lib
    # The canonical readiness CLI INVOCATION exists EXACTLY once across
    # the two scripts and the shared helper (no divergent implementations).
    def _invocations(text):
        return [ln for ln in text.splitlines()
                if "venv/bin/python" in ln
                and "status --require-ready" in ln]
    total = sum(len(_invocations(t)) for t in (install, upgrade, lib))
    assert total == 1, (
        "readiness gate must have one implementation (found %d)" % total)
    for name, text in (("install.sh", install), ("upgrade.sh", upgrade)):
        assert "deploy_common.sh" in text, name
        assert "ega_wait_for_readiness" in text, name
        assert not _invocations(text), name
        assert "status --require-quiescent" not in text, name


def test_owner_transient_primitive_is_python_single_definition():
    """The canonical transient acceptance lives in owner_env (probe +
    verify + termination proof); no shell reimplementation."""
    owner_env = _read(os.path.join("backend", "app", "owner_env.py"))
    readiness = _read(os.path.join("backend", "app",
                                   "deployment_readiness.py"))
    assert "def transient_acceptance" in owner_env
    assert "owner_env probe" in owner_env
    assert "owner_env verify" in owner_env
    assert "transient_acceptance" in readiness
    lib = _read(LIB_REL)
    assert "owner_env probe" not in lib
    assert "systemd-run" not in lib


def test_shared_readiness_helper_retries_then_fails(tmp_path):
    """Behavioral: the shared helper consumes the canonical gate exit
    code, retries a bounded number of times, and fails closed."""
    lib = os.path.join(_REPO_ROOT, LIB_REL)
    assert os.path.exists(lib), "shared helper missing"
    release = tmp_path / "release"
    (release / "venv" / "bin").mkdir(parents=True)
    marker = tmp_path / "attempts"
    fake = release / "venv" / "bin" / "python"
    with open(str(fake), "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent("""\
            #!/usr/bin/env bash
            n=0
            [ -f "%s" ] && n="$(cat "%s")"
            n=$((n + 1))
            printf '%%s' "$n" > "%s"
            if [ "${EGA_HELPER_FAIL:-1}" = "1" ]; then exit 3; fi
            exit 0
            """) % (str(marker), str(marker), str(marker)))
    os.chmod(str(fake), 0o755)
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    report = tmp_path / "report.json"
    script = textwrap.dedent("""\
        set -uo pipefail
        source "%s"
        ega_wait_for_readiness "%s" "%s" 1 "%s" harness 3 0
        """) % (lib, str(release), str(config), str(report))
    env = dict(os.environ)
    env["EGA_HELPER_FAIL"] = "1"
    proc = subprocess.run(["/bin/bash", "-c", script], env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1, proc.stderr
    assert int(marker.read_text()) == 3, "helper did not retry bounded"
    assert report.exists(), "helper must persist the gate output"
    env["EGA_HELPER_FAIL"] = "0"
    proc = subprocess.run(["/bin/bash", "-c", script], env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
