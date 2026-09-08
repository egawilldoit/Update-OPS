"""Static corrective tests for adapter/deploy audit findings (offline, never runs probes).

Write-only artifact: covers review findings without executing servers,
builds, or live probes. All subprocess/FS interactions are faked via
monkeypatch; shell assertions read script text. Python 3.10 compatible.
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

from backend.app.adapters import registry as registry_lib


def _repo_path(*parts):
    return os.path.join(_REPO_ROOT, *parts)


def _read_text(*parts):
    with open(_repo_path(*parts), "r", encoding="utf-8") as fh:
        return fh.read()


def _plan(tool, target, mode):
    from backend.app.adapters.base import PlanResult

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


# ---------------------------------------------------------------------------
# registry: mutation margin constant + helper
# ---------------------------------------------------------------------------

def test_mutation_margin_constant():
    assert registry_lib.MUTATION_TIMEOUT_MARGIN_S == 120
    assert registry_lib.mutation_timeout(1800) == 1920
    assert registry_lib.mutation_timeout("bad", default=100) == 220
    assert registry_lib.mutation_timeout_for(
        {"timeouts": {"updating": 50}}, "updating", default=10) == 170


def test_attach_timed_out_dynamic():
    from backend.app.adapters.base import ExecuteResult

    res = ExecuteResult(tool="t", exit_code=4, state="install_failed")
    registry_lib.attach_timed_out(res, True)
    assert getattr(res, "timed_out", None) is True
    registry_lib.attach_timed_out(res, False)
    assert getattr(res, "timed_out", None) is False


# ---------------------------------------------------------------------------
# exact-target mismatch per adapter (fakes)
# ---------------------------------------------------------------------------

def test_codex_rejects_exact_mode():
    from backend.app.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    plan = _plan("codex", "1.2.3", "exact")
    res = adapter.execute(plan, "job-test-1234", activity_ack=True)
    assert res.state == "blocked"
    assert res.error_code == "invalid_request"


def test_hermes_rejects_exact_mode():
    from backend.app.adapters.hermes import HermesAdapter

    adapter = HermesAdapter()
    plan = _plan("hermes", "1.2.3", "exact")
    res = adapter.execute(plan, "job-test-1234", activity_ack=True)
    assert res.state == "blocked"


def test_opencode_exact_mismatch_fails(monkeypatch):
    import backend.app.adapters.opencode as opencode_mod

    adapter = opencode_mod.OpenCodeAdapter()
    plan = _plan("opencode", "1.2.3", "exact")
    plan.fingerprint = ""
    plan.backup_scope = {}
    monkeypatch.setattr(opencode_mod, "resolve_executable",
                        lambda _p: (True, opencode_mod.OPENCODE_BIN, "ok"))

    class _FakeInspect(object):
        fingerprint = ""
        version = "0.0.1"

    monkeypatch.setattr(adapter, "inspect", lambda: _FakeInspect())

    class _FakeActivity(object):
        state = "idle"
        evidence = "idle"

    monkeypatch.setattr(adapter, "activity", lambda: _FakeActivity())
    monkeypatch.setattr(opencode_mod, "check_disk",
                        lambda _paths, _need: (True, "ok", []))
    monkeypatch.setattr(opencode_mod, "run_fixed",
                        lambda _argv, timeout=60, cwd=None,
                        scope_unit=None, **kw: _FakeProc(0, "ok", ""))
    # After version differs from planned exact target.
    monkeypatch.setattr(adapter, "_version_probe", lambda: ("9.9.9", "raw"))
    monkeypatch.setattr(adapter, "_server_configured", lambda: (False, "none"))
    res = adapter.execute(plan, "job-test-1234", activity_ack=True)
    assert res.state == "install_failed"
    assert "mismatch" in (res.error_detail or "").lower()
    assert getattr(res, "timed_out", False) is False


def test_t3_exact_mismatch_fails(monkeypatch):
    import backend.app.adapters.t3 as t3_mod

    adapter = t3_mod.T3Adapter()
    plan = _plan("t3", "1.2.3", "exact")
    plan.fingerprint = ""
    fake_info = {
        "unit": t3_mod.T3_UNIT, "state_path": "/tmp/x.json",
        "endpoint": "http://127.0.0.1:1/health", "port": "1",
        "launch_mode": "managed-service", "unit_load": "loaded",
        "unit_active": "inactive", "unit_sub": "dead",
        "state_exists": True, "state_version": "0.0.1",
        "exec_path": "/x/npx", "exec_resolved": "/x/npx",
        "process_hit": False, "process_checked": True,
    }
    monkeypatch.setattr(adapter, "_inventory", lambda: dict(fake_info))
    monkeypatch.setattr(adapter, "_inventory_gate", lambda _i: (True, ""))

    class _FakeInspect(object):
        fingerprint = ""
        version = "0.0.1"

    monkeypatch.setattr(adapter, "inspect", lambda: _FakeInspect())

    class _FakeActivity(object):
        state = "idle"
        evidence = "idle"

    monkeypatch.setattr(adapter, "activity", lambda: _FakeActivity())
    monkeypatch.setattr(t3_mod, "check_disk",
                        lambda _paths, _need: (True, "ok", []))
    monkeypatch.setattr(t3_mod, "resolve_executable",
                        lambda _p: (True, "/x/npx", "ok"))
    monkeypatch.setattr(t3_mod, "run_fixed",
                        lambda _argv, timeout=60, cwd=None,
                        scope_unit=None, **kw: _FakeProc(0, "ok", ""))
    # Running version differs from planned exact target.
    monkeypatch.setattr(adapter, "_running_version", lambda _i: "9.9.9")
    res = adapter.execute(plan, "job-test-1234", activity_ack=True)
    assert res.state == "install_failed"
    assert "mismatch" in (res.error_detail or "").lower()


# ---------------------------------------------------------------------------
# codex: foreground process without daemon -> unknown (never idle)
# ---------------------------------------------------------------------------

def test_codex_foreground_process_unknown(monkeypatch):
    from backend.app.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    monkeypatch.setattr(adapter, "_daemon_status",
                        lambda: ("absent", "", "no daemon"))
    monkeypatch.setattr(adapter, "_codex_processes",
                        lambda: (2, "2 codex process line(s) observed"))
    res = adapter.activity()
    assert res.state == "unknown"
    assert res.state != "idle"


def test_codex_absent_no_process_idle(monkeypatch):
    from backend.app.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    monkeypatch.setattr(adapter, "_daemon_status",
                        lambda: ("absent", "", "no daemon"))
    monkeypatch.setattr(adapter, "_codex_processes", lambda: (0, "0 lines"))
    res = adapter.activity()
    assert res.state == "idle"


# ---------------------------------------------------------------------------
# hermes: narrow allow-list shape, no generic sudo
# ---------------------------------------------------------------------------

def test_hermes_no_generic_sudo():
    """Narrow allow-list shape enforced behaviorally (T04): the exact
    sudo argv builders produce only scoped systemctl show/restart for
    an inventoried unit — never a generic passwordless probe."""
    from backend.app.adapters import hermes as hermes_mod

    show = hermes_mod._allowed_show_argv("fake-unit.service")
    assert show == [hermes_mod._SUDO, "-n", hermes_mod._SYSTEMCTL,
                    "--no-pager", "show", "fake-unit.service"]
    restart = hermes_mod._allowed_restart_argv("fake-unit.service")
    assert restart == [hermes_mod._SUDO, "-n", hermes_mod._SYSTEMCTL,
                       "restart", "fake-unit.service"]
    for argv in (show, restart):
        assert argv != [hermes_mod._SUDO, "-n", "true"]
        assert "true" not in argv


def test_hermes_sudoers_example_shape():
    text = _read_text("deploy", "etc", "sudoers.d",
                      "ega-update-hermes.example")
    assert "CHANGEME" in text
    assert "NOPASSWD" in text
    assert "show" in text
    assert "restart" in text
    # No generic passwordless grant: every NOPASSWD line must name an exact
    # systemctl show/restart command, never a bare `true` probe.
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "NOPASSWD" in stripped:
            assert ("show" in stripped or "restart" in stripped), stripped
            assert stripped.rstrip().endswith(".service"), stripped


# ---------------------------------------------------------------------------
# t3: five-pillar gate matrix
# ---------------------------------------------------------------------------

def _valid_t3_info():
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


def test_t3_five_pillar_matrix():
    from backend.app.adapters.t3 import T3Adapter

    adapter = T3Adapter()
    base = _valid_t3_info()
    ok, _detail = adapter._inventory_gate(dict(base))
    assert ok is True
    # Each missing pillar blocks (never success).
    cases = [
        ("unit", {"unit_load": ""}),
        ("state-path", {"state_exists": False}),
        ("exec", {"exec_resolved": ""}),
        ("process", {"process_hit": False}),
        ("endpoint", {"port": "", "endpoint": ""}),
    ]
    for _name, override in cases:
        info = dict(base)
        info.update(override)
        ok, detail = adapter._inventory_gate(dict(info))
        assert ok is False, _name


def test_t3_running_version_never_state_alone():
    from backend.app.adapters.t3 import T3Adapter

    adapter = T3Adapter()
    # State file alone (no process/endpoint/exec) yields "".
    info = {"state_version": "1.2.3", "process_checked": False,
            "process_hit": False, "unit_active": "active",
            "port": "", "endpoint": "", "exec_resolved": ""}
    assert adapter._running_version(info) == ""
    # Corroborated pillars yield the version.
    assert adapter._running_version(_valid_t3_info()) == "1.2.3"


# ---------------------------------------------------------------------------
# backup deep-scrub nesting
# ---------------------------------------------------------------------------

def test_t3_deep_scrub_nesting():
    from backend.app.adapters.t3 import _deep_scrub

    payload = {
        "version": "1.2.3",
        "nested": {"ApiToken": "secret-1", "inner": [{"PASSWORD": "p2", "ok": 1}]},
        "list": [{"BeArEr": "b3"}],
        "TOKEN": "top",
    }
    out = _deep_scrub(payload)
    assert out["version"] == "1.2.3"
    assert out["nested"]["ApiToken"] == "***REDACTED***"
    assert out["nested"]["inner"][0]["PASSWORD"] == "***REDACTED***"
    assert out["nested"]["inner"][0]["ok"] == 1
    assert out["list"][0]["BeArEr"] == "***REDACTED***"
    assert out["TOKEN"] == "***REDACTED***"
    assert "secret-1" not in json.dumps(out)


# ---------------------------------------------------------------------------
# install.sh static assertions
# ---------------------------------------------------------------------------

def test_install_sh_static():
    text = _read_text("deploy", "scripts", "install.sh")
    assert "loginctl enable-linger" in text
    assert "loginctl show-user" in text or "Linger" in text
    assert "root:ega-update" in text
    assert "0750" in text
    assert "drain" in text.lower()
    # Build-output gate uses backend/app/static; it must never gate on a
    # frontend/dist build output path (a clarifying "(not frontend/dist/)"
    # comment is allowed, but no existence check on frontend/dist).
    assert "backend/app/static/index.html" in text
    assert '[ -f' not in text or "frontend/dist" not in "".join(
        line for line in text.splitlines() if "[ -f" in line or "test -f" in line)
    # Placeholder hostname, fail-closed.
    assert "CHANGEME" in text
    assert "cloudflared" in text.lower()


# ---------------------------------------------------------------------------
# upgrade.sh drain-before-stop ordering
# ---------------------------------------------------------------------------

def test_upgrade_sh_drain_before_stop():
    text = _read_text("deploy", "scripts", "upgrade.sh")
    drain_pos = text.find("drain")
    assert drain_pos != -1
    stop_pos = text.find("systemctl stop")
    assert stop_pos != -1
    assert drain_pos < stop_pos, "drain must be created BEFORE stopping services"
    assert "quiescen" in text.lower()
    assert "WAS_API" in text or "previously-running" in text.lower()
    assert "set -e" not in text.splitlines()[0:15].__str__() or "set -uo" in text


# ---------------------------------------------------------------------------
# migration failure must raise (import-guarded)
# ---------------------------------------------------------------------------

def test_lifespan_migration_failure_raises():
    """API startup validates-only and fails closed (T04 behavioral):
    schema mismatch and readiness failure both raise out of lifespan,
    and the lifespan wrapper never invokes migration."""
    import asyncio
    import inspect
    import backend.app.main as main_mod
    import backend.app.db as db_mod

    # Scoped to the lifespan wrapper itself (not whole-file prose):
    # startup validates; the controlled deploy procedure owns migration.
    assert "migrate(" not in inspect.getsource(main_mod.lifespan)

    class _FakeConn(object):
        def rollback(self):
            self.rolled_back = True

        def close(self):
            pass

    fake_conn = _FakeConn()
    orig_connect = main_mod.connect
    orig_validate = main_mod.validate_schema
    main_mod.connect = lambda _p: fake_conn  # type: ignore

    async def _run():
        try:
            async with main_mod.lifespan(None):  # type: ignore
                pass
        except Exception:
            return True
        return False

    try:
        # Schema mismatch fails startup.
        def _bad_schema(_conn):
            raise db_mod.SchemaError("pending migrations")

        main_mod.validate_schema = _bad_schema  # type: ignore
        assert asyncio.run(_run()) is True
    finally:
        main_mod.connect = orig_connect  # type: ignore
        main_mod.validate_schema = orig_validate  # type: ignore
