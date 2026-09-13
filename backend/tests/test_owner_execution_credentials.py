"""D1 — effective owner-execution credentials (write-only regression).

The long-running ``user@<uid>.service`` keeps the supplementary-group
vector it had at start. Transient ``systemd-run --user`` units inherit
that stale vector, so a tool owner added to ``ega-update`` AFTER the
manager started cannot traverse the group-owned shared paths. This file
pins the correction:

* the shared group is an explicit, configurable contract value;
* effective access (not account-database membership) is provisioned and
  probed with redacted, structured diagnostics;
* install.sh applies the explicit access contract and PROVES it from the
  real transient user-unit identity, fail-closed;
* the transient launch argv never emits ``SupplementaryGroups=`` (that
  is actively unsafe on the deployed systemd: the unprivileged user
  manager lacks CAP_SETGID and the unit fails with EXIT_GROUP).

Python 3.10 compatible. Hermetic: tmp_path only.
"""
from __future__ import annotations

import json
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


def _owner_env():
    from backend.app import owner_env
    return owner_env


def _install_text():
    with open(os.path.join(_REPO_ROOT, "deploy", "scripts", "install.sh"),
              "r", encoding="utf-8") as fh:
        return fh.read()


# -- shared-group resolution -------------------------------------------------

def test_shared_group_default_is_documented_ega_update():
    oe = _owner_env()
    resolver = getattr(oe, "resolve_shared_group", None)
    assert callable(resolver), "owner_env.resolve_shared_group missing"
    assert resolver(None) == "ega-update"
    assert resolver(None, None, "") == "ega-update"
    assert getattr(oe, "DEFAULT_OWNER_SHARED_GROUP", "") == "ega-update"


def test_shared_group_derives_from_config_not_hardcoded():
    import types

    oe = _owner_env()
    settings = types.SimpleNamespace(shared_group="other-shared")
    assert oe.resolve_shared_group(settings) == "other-shared"
    # Invalid / injection-shaped names fail closed to the documented default.
    bad = types.SimpleNamespace(shared_group="bad group; rm -rf /")
    assert oe.resolve_shared_group(bad) == "ega-update"


def test_owner_contract_declares_shared_group():
    from backend.app.owner_env import (build_owner_contract,
                                       contract_fingerprint)
    import support as support_lib

    support_lib.test_release_root()
    contract = build_owner_contract(None)
    assert contract.get("shared_group") == "ega-update"
    baseline = contract_fingerprint(contract)
    altered = dict(contract)
    altered["shared_group"] = "other-shared"
    assert contract_fingerprint(altered) != baseline


# -- ACL primitive (provisioning must not widen permissions) ------------------

def test_named_user_acl_is_effective_and_preserves_mode(tmp_path):
    oe = _owner_env()
    ensure = getattr(oe, "ensure_named_user_access", None)
    perms_for = getattr(oe, "named_user_acl_perms", None)
    assert callable(ensure) and callable(perms_for)

    target = tmp_path / "config.json"
    target.write_text("{\"k\":\"v\"}\n", encoding="utf-8")
    os.chmod(str(target), 0o640)
    before = os.stat(str(target)).st_mode & 0o7777

    assert ensure(str(target), 4242, 0o4) is True
    # A different principal's named-user entry is evaluated (kernel order:
    # owner -> named user -> group -> other).
    assert perms_for(str(target), 4242, groups=()) == 0o4
    # The visible mode (and therefore the group/mask class) is unchanged.
    assert os.stat(str(target)).st_mode & 0o7777 == before
    # Idempotent: re-applying changes nothing observable.
    assert ensure(str(target), 4242, 0o4) is True
    assert perms_for(str(target), 4242, groups=()) == 0o4


def test_named_user_acl_refuses_to_widen_mask(tmp_path):
    oe = _owner_env()
    target = tmp_path / "tight"
    target.write_text("x", encoding="utf-8")
    os.chmod(str(target), 0o600)  # group/mask class grants nothing
    # Granting r-- to another principal would require raising the mask and
    # widening group access; the primitive must refuse rather than weaken.
    assert oe.ensure_named_user_access(str(target), 4242, 0o4) is False
    assert oe.named_user_acl_perms(str(target), 4242, groups=()) == 0


def test_provision_owner_access_grants_dir_and_file(tmp_path):
    oe = _owner_env()
    import pwd

    state = tmp_path / "state"
    logs = state / "logs"
    backups = state / "backups"
    etc = tmp_path / "etc"
    for d in (state, logs, backups, etc):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(str(d), 0o770 if d != etc else 0o750)
    cfg = etc / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    os.chmod(str(cfg), 0o640)
    secrets = etc / "secrets.env"
    secrets.write_text("", encoding="utf-8")
    os.chmod(str(secrets), 0o640)

    uid = os.getuid()
    report = oe.provision_owner_access(
        owner=pwd.getpwuid(uid).pw_name, group="ega-update",
        state_dir=str(state), log_dir=str(logs), backup_dir=str(backups),
        config_dir=str(etc), config_file=str(cfg),
        secrets_file=str(secrets), inventory_file="")
    assert report["ok"] is True, report
    assert oe.named_user_acl_perms(str(logs), uid, groups=()) & 0o7
    assert oe.named_user_acl_perms(str(cfg), uid, groups=()) & 0o4


# -- structured, redacted credential probe ------------------------------------

def _layout(tmp_path):
    state = tmp_path / "state"
    logs = state / "logs"
    backups = state / "backups"
    etc = tmp_path / "etc"
    for d in (state, logs, backups, etc):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(str(state), 0o770)
    os.chmod(str(logs), 0o770)
    os.chmod(str(backups), 0o770)
    os.chmod(str(etc), 0o750)
    cfg = etc / "config.json"
    cfg.write_text("{\"listen_port\": 8771}", encoding="utf-8")
    os.chmod(str(cfg), 0o640)
    secrets = etc / "secrets.env"
    secrets.write_text("KNOWN_SECRET=super-secret-value-xyz\n",
                       encoding="utf-8")
    os.chmod(str(secrets), 0o640)
    return {"state_dir": str(state), "log_dir": str(logs),
            "backup_dir": str(backups), "config_dir": str(etc),
            "config_file": str(cfg), "secrets_file": str(secrets),
            "inventory_file": ""}


def test_credentials_probe_is_structured_and_never_leaks_contents(tmp_path):
    oe = _owner_env()
    probe = getattr(oe, "execution_credentials_probe", None)
    assert callable(probe), "owner_env.execution_credentials_probe missing"
    layout = _layout(tmp_path)
    report = probe(**layout)
    for key in ("ok", "uid", "gid", "groups", "user", "shared_group",
                "checks", "reasons"):
        assert key in report, key
    assert report["uid"] == os.getuid()
    assert report["gid"] == os.getgid()
    assert isinstance(report["groups"], list)
    assert report["shared_group"] == "ega-update"
    assert isinstance(report["checks"], dict)
    assert report["checks"]["config_file_read"]["ok"] is True
    assert report["checks"]["log_dir_write"]["ok"] is True
    assert report["checks"]["config_file_read"]["path"] == layout[
        "config_file"]
    dumped = json.dumps(report)
    assert "super-secret-value-xyz" not in dumped
    assert report["ok"] is True, report


def test_credentials_probe_detects_inaccessible_paths(tmp_path):
    """Deny the owner even its own path (mode 0000) and prove the probe
    reports failure instead of returning a green boolean."""
    oe = _owner_env()
    probe = getattr(oe, "execution_credentials_probe", None)
    assert callable(probe), "owner_env.execution_credentials_probe missing"
    layout = _layout(tmp_path)
    locked = layout["state_dir"]
    try:
        os.chmod(locked, 0o000)
        report = probe(**layout)
        assert report["ok"] is False
        assert report["reasons"]
        assert report["checks"]["state_dir_write"]["ok"] is False
    finally:
        os.chmod(locked, 0o770)


# -- realistic stale-group harness (root-only; skips unprivileged) ------------

def _stale_group_harness_available():
    if os.geteuid() != 0:
        return False, ("requires root to drop supplementary groups and "
                       "chown a tmp layout to a foreign group; "
                       "unprivileged users lack CAP_SETGID on this host")
    return True, ""


def test_stale_group_denied_without_effective_access(tmp_path):
    """Reproduce the defect: a child whose supplementary vector omits the
    layout's group cannot traverse/write it. After provisioning, the same
    child succeeds. Skips (never silently passes) when the scenario
    cannot be established without privilege."""
    ok, why = _stale_group_harness_available()
    if not ok:
        pytest.skip(why)

    import pwd
    import subprocess

    oe = _owner_env()
    foreign_gid = 4242  # not the tool owner's primary group
    owner_uid = pwd.getpwnam("ubuntu").pw_uid if _has_user("ubuntu") \
        else os.getuid()

    state = tmp_path / "state"
    logs = state / "logs"
    etc = tmp_path / "etc"
    for d in (state, logs, etc):
        d.mkdir(parents=True, exist_ok=True)
    cfg = etc / "config.json"
    cfg.write_text("{}", encoding="utf-8")

    for path in (state, logs):
        os.chown(str(path), 0, foreign_gid)
        os.chmod(str(path), 0o770)
    os.chown(str(etc), 0, foreign_gid)
    os.chmod(str(etc), 0o750)
    os.chown(str(cfg), 0, foreign_gid)
    os.chmod(str(cfg), 0o640)

    child = (
        "import os,sys\n"
        "os.setgroups([])\n"
        "os.setgid(%d)\n"
        "os.setuid(%d)\n"
        "ok=os.access(sys.argv[1], os.W_OK)\n"
        "print('1' if ok else '0')\n") % (owner_uid, owner_uid)

    def _child_can_write():
        out = subprocess.run(
            [sys.executable, "-c", child, str(logs)],
            capture_output=True, text=True, timeout=30)
        return out.stdout.strip() == "1"

    assert _child_can_write() is False, \
        "stale-group child unexpectedly had access (harness invalid)"

    report = oe.provision_owner_access(
        owner=pwd.getpwuid(owner_uid).pw_name, group="ega-update",
        state_dir=str(state), log_dir=str(logs),
        backup_dir=str(logs), config_dir=str(etc),
        config_file=str(cfg), secrets_file="", inventory_file="")
    assert report["ok"] is True, report
    assert _child_can_write() is True, \
        "provisioned effective access did not reach the stale-group child"


def _has_user(name):
    import pwd
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


# -- launch contract must not emit the unsafe property ------------------------

def test_transient_launch_never_emits_supplementary_groups():
    from backend.app.owner_env import (build_owner_contract, build_probe_cmd,
                                       build_transient_cmd, contract_env,
                                       transient_probe_name)
    import support as support_lib

    support_lib.test_release_root()
    env = contract_env(build_owner_contract(None))
    runner = build_transient_cmd("ega-update-job-abc.service", "/rel", env,
                                 "/rel/venv/bin/python", "job-id", "n1")
    probe = build_probe_cmd(
        transient_probe_name("12345678-1234-1234-1234-123456789abc"),
        "/rel", env, "/rel/venv/bin/python",
        "12345678-1234-1234-1234-123456789abc",
        "/l/p.json", "/l/r.json", "/l/s.jsonl", "inspect", 30.0)
    for argv in (runner, probe):
        joined = " ".join(argv)
        assert "SupplementaryGroups" not in joined, (
            "SupplementaryGroups= on a user unit is fatal on the deployed "
            "systemd: the unprivileged user manager lacks CAP_SETGID")
        assert "NoNewPrivileges=no" in joined


# -- install.sh provisions AND proves the invariant ---------------------------

def test_install_sh_provisions_and_proves_effective_access():
    text = _install_text()
    assert "backend.app.owner_env" in text
    assert "owner_env provision" in text
    assert "owner_env probe" in text
    assert "owner_env verify" in text
    assert "systemd-run --user" in text
    # Failure keeps the drain (fail closed) and is explicit.
    assert "fail_keep_drain" in text
    # Membership is still declared for isolation/documentation, but the
    # effective contract is proven separately.
    assert 'usermod -aG ega-update "$TOOL_OWNER"' in text
