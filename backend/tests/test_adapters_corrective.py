"""Corrective R22-R30 behavioral tests (offline, write-only artifact).

Covers senior-review findings without live probes/servers/builds. All
subprocess/FS touchpoints are faked via monkeypatch; process-boundary mocks
use monkeypatched backend.app.executor.run_stream (created as a stub when the
main-agent executor is pending). Python 3.10 compatible.
"""
from __future__ import annotations

import json
import os
import sys
import types

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.app.adapters import registry as registry_lib
from backend.app.adapters.base import PlanResult


def _repo_path(*parts):
    return os.path.join(_REPO_ROOT, *parts)


def _plan(tool, target, mode):
    return PlanResult(
        tool=tool, target=target, target_mode=mode, channel="test",
        fingerprint="", services=[], backup_scope={}, required_space_bytes=0,
        steps=["preflight", "backup", "updating", "verifying"],
        timeouts={"preflight": 5, "backup": 5, "updating": 10, "verifying": 5},
        restart_impact="", already_current=False)


class _FakeProc(object):
    def __init__(self, exit_code=0, stdout="", stderr="", timed_out=False):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out

    def ok(self):
        return (not self.timed_out) and self.exit_code == 0


class _FakeStream(object):
    def __init__(self, exit_code=0, out=b"", err=b"", timed_out=False):
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.stdout_tail = out
        self.stderr_tail = err
        self.bytes_total = len(out) + len(err)
        self.truncated = False
        self.duration_s = 0.01


def _ensure_executor(monkeypatch):
    """Ensure backend.app.executor.run_stream exists and is monkeypatchable."""
    try:
        import backend.app.executor as exec_mod  # type: ignore
        return exec_mod
    except Exception:
        stub = types.ModuleType("backend.app.executor")

        def _missing(argv, **kwargs):
            raise ImportError("executor pending")

        stub.run_stream = _missing  # type: ignore[attr-defined]
        sys.modules["backend.app.executor"] = stub
        try:
            import backend.app as app_pkg  # type: ignore

            app_pkg.executor = stub  # type: ignore[attr-defined]
        except Exception:
            pass
        return stub


# ---------------------------------------------------------------------------
# semver (R30): numeric prerelease, malformed rejected, is_upgrade
# ---------------------------------------------------------------------------

def test_semver_numeric_prerelease_ordering():
    from backend.app import semver as semver_mod

    assert semver_mod.compare("1.0.0-beta.2", "1.0.0-beta.10") == -1
    assert semver_mod.compare("1.0.0-beta.10", "1.0.0-beta.2") == 1
    assert semver_mod.compare("1.0.0-alpha", "1.0.0-beta") == -1
    assert semver_mod.compare("1.0.0-beta", "1.0.0") == -1
    assert semver_mod.compare("1.0.0", "1.0.0") == 0
    left = semver_mod.parse("1.0.0-beta.10")
    right = semver_mod.parse("1.0.0-beta.2")
    assert right < left
    assert left != right
    assert not (left == right)


def test_semver_malformed_rejected():
    from backend.app import semver as semver_mod

    for bad in ("", "1.2", "1.2.3-", "1.2.3-01", "1.2.3-alpha..1",
                "01.2.3", "1.02.3", "1.2.3-", "1.2.3+", ".1.2.3",
                "1.2.3-.", "v"):
        assert semver_mod.try_parse(bad) is None, bad
        assert semver_mod.compare(bad, "1.0.0") is None
        assert semver_mod.compare("1.0.0", bad) is None
        assert semver_mod.is_upgrade("1.0.0", bad) is False
    with pytest.raises(ValueError):
        semver_mod.parse("")
    with pytest.raises(ValueError):
        semver_mod.parse("1.2.3-01")


def test_semver_is_upgrade():
    from backend.app import semver as semver_mod

    assert semver_mod.is_upgrade("1.0.0", "1.0.1") is True
    assert semver_mod.is_upgrade("1.0.0-beta.2", "1.0.0-beta.10") is True
    assert semver_mod.is_upgrade("1.0.1", "1.0.0") is False
    assert semver_mod.is_upgrade("1.0.0", "1.0.0") is False
    assert semver_mod.is_upgrade("bad", "1.0.0") is False
    assert semver_mod.is_upgrade("", "1.0.0") is True


def test_registry_compare_delegates_numeric():
    assert registry_lib.compare_semver("1.0.0-beta.2", "1.0.0-beta.10") == -1
    assert registry_lib.compare_semver("bad", "1.0.0") is None
    assert registry_lib.is_upgrade_semver("1.0.0", "2.0.0") is True
    assert registry_lib.is_upgrade_semver("2.0.0", "1.0.0") is False


# ---------------------------------------------------------------------------
# run_fixed delegates to executor.run_stream (behavioral, no source scan)
# ---------------------------------------------------------------------------

def test_run_fixed_delegates_to_executor(monkeypatch):
    exec_mod = _ensure_executor(monkeypatch)
    seen = {}

    def _fake(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = dict(kwargs)
        assert kwargs.get("timeout_s") == 42
        assert "scope_unit" in kwargs
        return _FakeStream(0, b"hello-tail", b"err-tail", False)

    monkeypatch.setattr(exec_mod, "run_stream", _fake)
    res = registry_lib.run_fixed(["/bin/echo", "hi"], timeout=42)
    assert res.exit_code == 0
    assert res.stdout == "hello-tail"
    assert res.stderr == "err-tail"
    assert res.timed_out is False
    assert seen["argv"][0] == "/bin/echo"


def test_run_fixed_maps_timed_out_and_tails(monkeypatch):
    exec_mod = _ensure_executor(monkeypatch)

    def _fake(argv, **kwargs):
        return _FakeStream(124, b"out", b"err", True)

    monkeypatch.setattr(exec_mod, "run_stream", _fake)
    res = registry_lib.run_fixed(["/bin/sleep", "10"], timeout=5)
    assert res.timed_out is True
    assert res.exit_code == 124


def test_adapter_surfaces_executor_failure(monkeypatch):
    exec_mod = _ensure_executor(monkeypatch)

    def _fake(argv, **kwargs):
        # Every probe reports missing executable via executor path.
        return _FakeStream(127, b"", b"missing", False)

    monkeypatch.setattr(exec_mod, "run_stream", _fake)
    from backend.app.adapters import opencode as opencode_mod

    adapter = opencode_mod.OpenCodeAdapter()
    version, _raw = adapter._version_probe()
    assert version == ""


def test_no_direct_subprocess_in_adapters_behavioral(monkeypatch):
    """Adapters must go through executor: block subprocess.run, still work."""
    import subprocess as _subprocess

    exec_mod = _ensure_executor(monkeypatch)

    def _fake(argv, **kwargs):
        return _FakeStream(0, b"9.9.9", b"", False)

    monkeypatch.setattr(exec_mod, "run_stream", _fake)

    def _boom(*args, **kwargs):
        raise AssertionError("direct subprocess.run must not be used by adapters")

    monkeypatch.setattr(_subprocess, "run", _boom)
    from backend.app.adapters import codex as codex_mod

    adapter = codex_mod.CodexAdapter()
    # _version_probe goes via run_fixed -> executor, never subprocess.run.
    version, _raw = adapter._version_probe()
    assert version == "9.9.9"


# ---------------------------------------------------------------------------
# sanitize (R11): every adapter evidence string via sanitize_text
# ---------------------------------------------------------------------------

def test_adapters_use_sanitize_text():
    for rel in ("backend/app/adapters/codex.py",
                "backend/app/adapters/opencode.py",
                "backend/app/adapters/hermes.py",
                "backend/app/adapters/t3.py",
                "backend/app/adapters/registry.py"):
        with open(_repo_path(*rel.split("/")), "r", encoding="utf-8") as fh:
            text = fh.read()
        assert "sanitize_evidence" in text or "sanitize_text" in text, rel


def test_sanitize_evidence_uses_sanitizer_when_present(monkeypatch):
    stub = types.ModuleType("backend.app.sanitize")

    def _fake_sanitize(s, secrets):
        return "SANITIZED:%s" % s

    stub.sanitize_text = _fake_sanitize  # type: ignore[attr-defined]
    sys.modules["backend.app.sanitize"] = stub
    try:
        out = registry_lib.sanitize_evidence("hello")
        assert out.startswith("SANITIZED:")
    finally:
        try:
            del sys.modules["backend.app.sanitize"]
        except KeyError:
            pass


# ---------------------------------------------------------------------------
# backup basename uniqueness (R27)
# ---------------------------------------------------------------------------

def test_backup_basename_uniqueness():
    first = registry_lib.backup_dest_for_db("/backups", "job123",
                                            "/a/data/opencode.db")
    second = registry_lib.backup_dest_for_db("/backups", "job123",
                                             "/b/data/opencode.db")
    assert first != second
    assert "job123" in first and "job123" in second
    assert first.endswith("opencode.db") and second.endswith("opencode.db")
    # Same path is stable.
    again = registry_lib.backup_dest_for_db("/backups", "job123",
                                            "/a/data/opencode.db")
    assert again == first


# ---------------------------------------------------------------------------
# unknown footprint blocks (R28)
# ---------------------------------------------------------------------------

def _plan_with_unknown_budget(tool, target="1.2.3", mode="exact"):
    plan = _plan(tool, target, mode)
    registry_lib.attach_plan_v2(plan, budgets={"x": -1}, space_fs={},
                                required_checks=[], scope_unit=None)
    return plan


def test_unknown_footprint_blocks_opencode(monkeypatch):
    import backend.app.adapters.opencode as opencode_mod

    adapter = opencode_mod.OpenCodeAdapter()
    plan = _plan_with_unknown_budget("opencode", "1.2.3", "exact")
    res = adapter.execute(plan, "job-unknown-1", activity_ack=True)
    assert res.state == "blocked"
    assert res.error_code == "disk_blocked"


def test_unknown_footprint_blocks_codex():
    from backend.app.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    plan = _plan_with_unknown_budget("codex", "native-latest", "native_latest")
    res = adapter.execute(plan, "job-unknown-2", activity_ack=True)
    assert res.state == "blocked"
    assert res.error_code == "disk_blocked"


def test_unknown_footprint_blocks_hermes():
    from backend.app.adapters.hermes import HermesAdapter

    adapter = HermesAdapter()
    plan = _plan_with_unknown_budget("hermes", "main-latest", "native_latest")
    res = adapter.execute(plan, "job-unknown-3", activity_ack=True)
    assert res.state == "blocked"
    assert res.error_code == "disk_blocked"


def test_unknown_footprint_blocks_t3(monkeypatch):
    import backend.app.adapters.t3 as t3_mod

    adapter = t3_mod.T3Adapter()
    plan = _plan_with_unknown_budget("t3", "1.2.3", "exact")
    res = adapter.execute(plan, "job-unknown-4", activity_ack=True)
    assert res.state == "blocked"
    assert res.error_code == "disk_blocked"


# ---------------------------------------------------------------------------
# hermes (R22,R23,R24)
# ---------------------------------------------------------------------------

def test_hermes_wrong_remote_blocks(monkeypatch):
    import backend.app.adapters.hermes as hermes_mod

    adapter = hermes_mod.HermesAdapter()
    # Clean git outputs but remote mismatch vs inventory expectation.
    monkeypatch.setattr(hermes_mod, "run_fixed",
                        lambda argv, timeout=60, cwd=None, scope_unit=None, **kw: _FakeProc(
                            0,
                            "abc123def456abc123def456abc123def456abcd" if "rev-parse" in argv and "HEAD" in argv and "--abbrev-ref" not in argv
                            else ("main" if "--abbrev-ref" in argv else ""),
                            ""))
    # status clean is default "" stdout above; branch main ok.
    monkeypatch.setattr(adapter, "_git_remote",
                        lambda: ("https://example.invalid/wrong.git", ""))
    monkeypatch.setattr(adapter, "_expected_remote",
                        lambda: "https://example.invalid/expected.git")
    cleanliness, _head, detail = adapter._git_state()
    assert cleanliness in ("dirty", "unknown")
    assert "remote" in detail.lower() or "mismatch" in detail.lower()


def test_hermes_failed_branch_never_defaults_main(monkeypatch):
    import backend.app.adapters.hermes as hermes_mod

    adapter = hermes_mod.HermesAdapter()

    def _fake_run(argv, timeout=60, cwd=None, scope_unit=None, **kw):
        txt = " ".join(argv)
        if "rev-parse" in txt and "HEAD" in txt and "--abbrev-ref" not in txt:
            return _FakeProc(0, "abc123def456abc123def456abc123def456abcd", "")
        if "status" in txt:
            return _FakeProc(0, "", "")
        if "--abbrev-ref" in txt:
            return _FakeProc(128, "", "fatal: no branch")
        if "remote" in txt:
            return _FakeProc(0, "https://example.invalid/expected.git", "")
        return _FakeProc(0, "", "")

    monkeypatch.setattr(hermes_mod, "run_fixed", _fake_run)
    monkeypatch.setattr(adapter, "_expected_remote",
                        lambda: "https://example.invalid/expected.git")
    cleanliness, _head, detail = adapter._git_state()
    assert cleanliness == "unknown"
    assert "main" not in detail or "never default" in detail.lower() or "branch" in detail.lower()


def test_hermes_dirty_blocks(monkeypatch):
    import backend.app.adapters.hermes as hermes_mod

    adapter = hermes_mod.HermesAdapter()

    def _fake_run(argv, timeout=60, cwd=None, scope_unit=None, **kw):
        txt = " ".join(argv)
        if "rev-parse" in txt and "--abbrev-ref" not in txt:
            return _FakeProc(0, "abc123def456abc123def456abc123def456abcd", "")
        if "status" in txt:
            return _FakeProc(0, " M file.py\n?? new.py\n", "")
        if "--abbrev-ref" in txt:
            return _FakeProc(0, "main", "")
        if "remote" in txt:
            return _FakeProc(0, "https://example.invalid/expected.git", "")
        return _FakeProc(0, "", "")

    monkeypatch.setattr(hermes_mod, "run_fixed", _fake_run)
    monkeypatch.setattr(adapter, "_expected_remote",
                        lambda: "https://example.invalid/expected.git")
    cleanliness, _head, _detail = adapter._git_state()
    assert cleanliness == "dirty"


def test_hermes_keyword_without_artifact_fails(monkeypatch, tmp_path):
    import backend.app.adapters.hermes as hermes_mod

    adapter = hermes_mod.HermesAdapter()
    plan = _plan("hermes", "main-latest", "native_latest")
    plan.backup_scope = {"mode": "full"}
    registry_lib.attach_plan_v2(plan, budgets={"/tmp": 100}, space_fs={"/tmp": 1000},
                                required_checks=[], scope_unit=None)

    class _FakeInspect(object):
        fingerprint = ""
        version = "1.0.0"
        commit = "abc123"
        source_clean = "clean"
        source_detail = "clean"

    monkeypatch.setattr(adapter, "inspect", lambda: _FakeInspect())

    class _FakeActivity(object):
        state = "idle"
        evidence = "idle"

    monkeypatch.setattr(adapter, "activity", lambda: _FakeActivity())
    monkeypatch.setattr(hermes_mod, "check_disk", lambda _p, _n: (True, "ok", []))
    monkeypatch.setattr(hermes_mod, "resolve_executable",
                        lambda _p: (True, hermes_mod.HERMES_BIN, "ok"))
    monkeypatch.setattr(adapter, "_capabilities",
                        lambda: {"help": True, "plan": True, "check": False,
                                 "yes": True, "backup": True, "detail": ""})
    monkeypatch.setattr(adapter, "_has_system_restart_authority",
                        lambda _u=None: (True, "ok"))
    monkeypatch.setattr(adapter, "_effective_backup_policy",
                        lambda: {"mode": "full"})
    # Mutation succeeds with the word "backup" in output but no artifact file.
    monkeypatch.setattr(hermes_mod, "run_fixed",
                        lambda argv, timeout=60, cwd=None, scope_unit=None, **kw: _FakeProc(
                            0, "update ok backup done", ""))
    monkeypatch.setattr(adapter, "_native_backup_artifact",
                        lambda _job, _head: (False, "", 0))
    monkeypatch.setattr(adapter, "_version_probe", lambda: ("1.0.1", "raw"))
    res = adapter.execute(plan, "job-no-artifact", activity_ack=True)
    assert res.state == "install_failed"
    assert res.error_code == "backup_failed"


def test_hermes_show_vs_restart_authority(monkeypatch):
    import backend.app.adapters.hermes as hermes_mod

    adapter = hermes_mod.HermesAdapter()
    # Inventory allow-list contains the unit; sudo -n -l denies restart.
    monkeypatch.setattr(adapter, "_inventory_allowed_restart_argv",
                        lambda: [[hermes_mod._SUDO, "-n", hermes_mod._SYSTEMCTL,
                                  "restart", "hermes-gateway.service"]])

    def _fake_run(argv, timeout=15, cwd=None, scope_unit=None, **kw):
        # sudo -n -l fails -> no precise restart authority even though a
        # show probe would succeed (show proves nothing).
        return _FakeProc(1, "", "Sorry, user may not run sudo")

    monkeypatch.setattr(hermes_mod, "run_fixed", _fake_run)
    ok, detail = adapter._has_system_restart_authority(
        [("system", "hermes-gateway.service")])
    assert ok is False
    assert "BLOCKED_RESTART_AUTHORITY" in detail


def test_hermes_old_commit_unit_fails(monkeypatch):
    import backend.app.adapters.hermes as hermes_mod

    adapter = hermes_mod.HermesAdapter()

    class _FakeInspect(object):
        version = "1.0.0"
        commit = "newhead1234567890newhead1234567890abcd"
        owner = "1000:1000"

    monkeypatch.setattr(adapter, "inspect", lambda: _FakeInspect())
    monkeypatch.setattr(adapter, "_service_commit",
                        lambda: ("oldhead9999999999oldhead9999999999abcd", "service commit"))
    monkeypatch.setattr(hermes_mod, "run_fixed",
                        lambda argv, timeout=60, cwd=None, scope_unit=None, **kw: _FakeProc(
                            0, "ok", ""))
    # Units running (LoadState/active/running, MainPID in cgroup mocked True).
    monkeypatch.setattr(adapter, "_unit_show",
                        lambda _scope, _unit: {"LoadState": "loaded", "ActiveState": "active",
                                               "SubState": "running", "MainPID": "1234",
                                               "User": "ubuntu", "ExecStart": hermes_mod.HERMES_BIN})
    monkeypatch.setattr(hermes_mod, "cgroup_belongs_to_unit", lambda _p, _u: True)
    res = adapter.verify()
    names = {c.name: c for c in res.checks}
    assert "gateway_running_commit" in names
    assert names["gateway_running_commit"].result in ("fail", "unknown")
    assert names["gateway_running_commit"].mandatory is True
    assert res.passed is False


# ---------------------------------------------------------------------------
# codex (R25)
# ---------------------------------------------------------------------------

def test_codex_expected_daemon_missing_fails(monkeypatch):
    from backend.app.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    monkeypatch.setattr(adapter, "_daemon_status",
                        lambda: ("absent", "", "structured absence"))
    monkeypatch.setattr(adapter, "_version_probe", lambda: ("1.0.0", "raw"))
    monkeypatch.setattr(adapter, "_releases", lambda: ["r1"])
    import backend.app.adapters.codex as codex_mod

    monkeypatch.setattr(codex_mod, "run_fixed",
                        lambda argv, timeout=60, cwd=None, scope_unit=None, **kw: _FakeProc(0, "ok", ""))
    plan = _plan("codex", "native-latest", "native_latest")
    registry_lib.attach_plan_v2(plan, daemon_expected=True,
                                daemon_status_at_plan="healthy",
                                required_checks=[], scope_unit=None)
    res = adapter.verify(plan=plan)
    by_name = {c.name: c for c in res.checks}
    assert by_name["daemon_readiness"].result == "fail"
    assert by_name["daemon_readiness"].mandatory is True
    assert res.passed is False


def test_codex_probe_failure_fails(monkeypatch):
    from backend.app.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    monkeypatch.setattr(adapter, "_daemon_status",
                        lambda: ("failed-probe", "", "probe error"))
    monkeypatch.setattr(adapter, "_version_probe", lambda: ("1.0.0", "raw"))
    monkeypatch.setattr(adapter, "_releases", lambda: ["r1"])
    import backend.app.adapters.codex as codex_mod

    monkeypatch.setattr(codex_mod, "run_fixed",
                        lambda argv, timeout=60, cwd=None, scope_unit=None, **kw: _FakeProc(0, "ok", ""))
    res = adapter.verify()
    by_name = {c.name: c for c in res.checks}
    assert by_name["daemon_readiness"].result == "fail"


def test_codex_broad_substring_alone_is_unknown(monkeypatch):
    exec_mod = _ensure_executor(monkeypatch)

    def _fake(argv, **kwargs):
        txt = " ".join(argv)
        if "daemon" in txt and "--help" in txt:
            return _FakeStream(0, b"usage: daemon version", b"", False)
        if "daemon" in txt and "version" in txt:
            # Plain-text "not found" with no JSON schema -> unknown (failed-probe).
            return _FakeStream(1, b"", b"not found", False)
        return _FakeStream(0, b"ok", b"", False)

    monkeypatch.setattr(exec_mod, "run_stream", _fake)
    from backend.app.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    state, _ver, _detail = adapter._daemon_status()
    assert state == "failed-probe"
    assert state != "absent"


def test_codex_already_current_no_restart(monkeypatch):
    import backend.app.adapters.codex as codex_mod

    adapter = codex_mod.CodexAdapter()
    plan = _plan("codex", "native-latest", "native_latest")
    registry_lib.attach_plan_v2(plan, budgets={"/tmp": 10}, space_fs={"/tmp": 100},
                                required_checks=[], scope_unit=None)

    class _FakeInspect(object):
        fingerprint = ""
        version = "1.0.0"

    monkeypatch.setattr(adapter, "inspect", lambda: _FakeInspect())

    class _FakeActivity(object):
        state = "idle"
        evidence = "idle"

    monkeypatch.setattr(adapter, "activity", lambda: _FakeActivity())
    monkeypatch.setattr(codex_mod, "check_disk", lambda _p, _n: (True, "ok", []))
    monkeypatch.setattr(codex_mod, "resolve_executable",
                        lambda _p: (True, codex_mod.STANDALONE_DIR + "/x", "ok"))
    monkeypatch.setattr(adapter, "_daemon_status",
                        lambda: ("healthy", "1.0.0", "healthy"))
    calls = {"restart": 0}

    def _fake_run(argv, timeout=60, cwd=None, scope_unit=None, **kw):
        if argv == [codex_mod.CODEX_BIN, "update"]:
            return _FakeProc(0, "already up to date, latest", "")
        if "restart" in argv:
            calls["restart"] += 1
            return _FakeProc(0, "restarted", "")
        if "--version" in argv:
            return _FakeProc(0, "codex 1.0.0", "")
        return _FakeProc(0, "ok", "")

    monkeypatch.setattr(codex_mod, "run_fixed", _fake_run)
    monkeypatch.setattr(adapter, "_version_probe", lambda: ("1.0.0", "raw"))
    res = adapter.execute(plan, "job-already", activity_ack=True)
    assert res.state == "already_current"
    assert calls["restart"] == 0


def test_codex_manual_gap_not_mandatory_fail(monkeypatch):
    from backend.app.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    monkeypatch.setenv("EGA_T3_ENDPOINT", "http://127.0.0.1:9/health")

    class _FakeResp(object):
        status = 200

        def read(self, _n):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    import urllib.request as _urllib

    monkeypatch.setattr(_urllib, "urlopen", lambda *a, **k: _FakeResp())
    check = adapter._t3_integration_check()
    assert check.name == "t3_integration"
    assert check.result == "unknown"
    assert check.mandatory is False
    assert "manual" in check.summary.lower()


# ---------------------------------------------------------------------------
# t3 (R26,R27,R30)
# ---------------------------------------------------------------------------

def _t3_base_info():
    import backend.app.adapters.t3 as t3_mod

    return {
        "unit": t3_mod.T3_UNIT, "state_path": "/tmp/s.json",
        "endpoint": "http://127.0.0.1:1/health", "port": "3000",
        "launch_mode": "managed-service",
        "unit_load": "loaded", "unit_active": "active", "unit_sub": "running",
        "state_exists": True, "state_version": "1.2.3",
        "exec_path": "/x/npx", "exec_resolved": "/x/npx",
        "process_hit": True, "process_checked": True,
    }


def test_t3_stale_state_stranger_fails(monkeypatch):
    import backend.app.adapters.t3 as t3_mod

    adapter = t3_mod.T3Adapter()
    info = _t3_base_info()
    # Stranger: ExecStart does not reference the inventoried npx, MainPID not
    # in cgroup. Provide bound keys so BOUND checks engage.
    info.update({
        "exec_start_raw": "/usr/bin/stranger --flag",
        "exec_argv": ["/usr/bin/stranger", "--flag"],
        "unit_pid": "999999",
        "official_units": [t3_mod.T3_UNIT],
        "unit_scope": "user",
        "inventory_home": "/tmp",
    })
    monkeypatch.setattr(t3_mod, "cgroup_belongs_to_unit", lambda _p, _u: False)
    ok, detail = adapter._inventory_gate(dict(info))
    assert ok is False


def test_t3_mainpid_mismatch_blocks(monkeypatch):
    import backend.app.adapters.t3 as t3_mod

    adapter = t3_mod.T3Adapter()
    info = _t3_base_info()
    info.update({
        "exec_start_raw": "/x/npx --yes t3@1.2.3 service update",
        "exec_argv": ["/x/npx", "--yes", "t3@1.2.3", "service", "update"],
        "unit_pid": "999999",
        "official_units": [t3_mod.T3_UNIT],
        "unit_scope": "user",
        "inventory_home": "/tmp",
    })
    monkeypatch.setattr(t3_mod, "cgroup_belongs_to_unit", lambda _p, _u: False)
    ok, _detail = adapter._inventory_gate(dict(info))
    assert ok is False


def test_t3_mutable_execstart_blocks():
    import backend.app.adapters.t3 as t3_mod

    adapter = t3_mod.T3Adapter()
    info = _t3_base_info()
    info.update({
        "exec_start_raw": "/x/npx --yes t3@latest service update",
        "exec_argv": ["/x/npx", "--yes", "t3@latest", "service", "update"],
        "official_units": [t3_mod.T3_UNIT],
        "unit_scope": "user",
        "inventory_home": "/tmp",
    })
    ok, detail = adapter._inventory_gate(dict(info))
    assert ok is False
    assert "mutable" in detail.lower() or "latest" in detail.lower()


def test_t3_version_in_unrelated_arg_blocks():
    import backend.app.adapters.t3 as t3_mod

    adapter = t3_mod.T3Adapter()
    info = _t3_base_info()
    # Mutable selector hidden in an unrelated arg still blocks (ANYWHERE).
    info.update({
        "exec_start_raw": "/x/npx --yes t3@1.2.3 service update --tag nightly",
        "exec_argv": ["/x/npx", "--yes", "t3@1.2.3", "service", "update",
                      "--tag", "nightly"],
        "official_units": [t3_mod.T3_UNIT],
        "unit_scope": "user",
        "inventory_home": "/tmp",
    })
    ok, _detail = adapter._inventory_gate(dict(info))
    assert ok is False


def test_t3_unlisted_provenance_blocks():
    import backend.app.adapters.t3 as t3_mod

    adapter = t3_mod.T3Adapter()
    info = _t3_base_info()
    info.update({
        "official_units": ["other.service"],
        "unit_scope": "user",
        "inventory_home": "/tmp",
        "exec_start_raw": "/x/npx --yes t3@1.2.3 service update",
        "exec_argv": ["/x/npx", "--yes", "t3@1.2.3", "service", "update"],
    })
    ok, detail = adapter._inventory_gate(dict(info))
    assert ok is False
    assert "allow-list" in detail.lower() or "provenance" in detail.lower()


def test_t3_endpoint_version_mismatch_fails(monkeypatch):
    import backend.app.adapters.t3 as t3_mod

    adapter = t3_mod.T3Adapter()
    info = dict(_t3_base_info())
    info["endpoint"] = "http://127.0.0.1:1/health"

    class _FakeResp(object):
        status = 200

        def read(self, _n):
            return b'{"version": "9.9.9"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    import urllib.request as _urllib

    monkeypatch.setattr(_urllib, "urlopen", lambda *a, **k: _FakeResp())
    result, _summary, ep_version = adapter._endpoint_probe(dict(info), "1.2.3")
    assert result == "fail"
    assert ep_version == "9.9.9"


def test_t3_launch_pinned_rejects_mutable_anywhere(monkeypatch):
    import backend.app.adapters.t3 as t3_mod

    adapter = t3_mod.T3Adapter()
    info = {"unit": t3_mod.T3_UNIT, "unit_scope": "user",
            "exec_resolved": "/x/npx"}

    def _fake_run(argv, timeout=30, cwd=None, scope_unit=None, **kw):
        return _FakeProc(0, "ExecStart=/x/npx --yes t3@1.2.3 service update --channel nightly", "")

    monkeypatch.setattr(t3_mod, "run_fixed", _fake_run)
    check = adapter._launch_pinned(dict(info), "1.2.3")
    assert check.name == "launch_pinned"
    assert check.result == "fail"


# ---------------------------------------------------------------------------
# verify covers required_checks (runner fails missing)
# ---------------------------------------------------------------------------

def test_verify_covers_required_checks_opencode(monkeypatch):
    import backend.app.adapters.opencode as opencode_mod

    adapter = opencode_mod.OpenCodeAdapter()
    monkeypatch.setattr(adapter, "_version_probe", lambda: ("1.2.3", "raw"))
    monkeypatch.setattr(opencode_mod, "run_fixed",
                        lambda argv, timeout=60, cwd=None, scope_unit=None, **kw: _FakeProc(
                            0, "active" if "is-active" in argv else "ok", ""))
    monkeypatch.setattr(adapter, "_server_configured", lambda: (False, "none"))
    monkeypatch.setattr(adapter, "_db_integrity", lambda: ("not_applicable", "none"))
    plan = _plan("opencode", "1.2.3", "exact")
    registry_lib.attach_plan_v2(plan, required_checks=["cli_version", "custom_required"],
                                scope_unit=None)
    res = adapter.verify(expected_target="1.2.3", plan=plan)
    names = {c.name for c in res.checks}
    assert "custom_required" in names
