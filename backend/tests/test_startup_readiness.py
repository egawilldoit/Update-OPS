"""D2 regression: startup readiness must fail closed (hermetic).

A fatal readiness violation (placeholder/missing identity, unreadable
secret source, unparsable inventory, missing executable, unresolvable
release) must deterministically prevent API and worker startup. Legit
warnings must still be returnable. No live service, DB, or /opt access.
"""
from __future__ import annotations

import asyncio
import inspect
import types

import pytest


def _settings(**over):
    base = dict(
        team_domain="", audience="", owner_emails=[],
        public_origin="", csrf_secret="", csrf_secret_file="",
        secrets_file="", inventory_file="", state_dir="",
        db_path="", log_dir="", backup_dir="", tool_owner="ubuntu",
        node_path="", npm_path="", npx_path="",
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def _fatal_api_settings():
    return _settings(
        team_domain="CHANGEME.team.cloudflareaccess.com",
        audience="",
        owner_emails=[],
        public_origin="TODO-origin",
        csrf_secret="CHANGEME-csrf",
    )


# -- readiness core ---------------------------------------------------------

def test_validate_startup_raises_on_fatal_api_violations():
    from backend.app.readiness import ReadinessError, validate_startup

    with pytest.raises(ReadinessError) as exc:
        validate_startup("api", _fatal_api_settings())
    violations = exc.value.violations
    assert violations
    joined = " ".join(violations)
    assert "team_domain" in joined
    assert "csrf_secret" in joined
    # Never leak secret/placeholder VALUES, only field names.
    assert "CHANGEME" not in joined
    assert "TODO" not in joined


def test_validate_startup_clean_api_returns_warnings_only():
    from backend.app.readiness import validate_startup

    warnings = validate_startup("api", _settings(
        team_domain="team.cloudflareaccess.com",
        audience="aud-123",
        owner_emails=["owner@example.invalid"],
        public_origin="https://console.example.invalid",
        csrf_secret="a-real-secret-value-xyz",
    ))
    assert isinstance(warnings, list)
    assert warnings == []


# -- API startup fails closed ----------------------------------------------

def test_api_lifespan_fails_closed_on_fatal_readiness(monkeypatch):
    import backend.app.main as main_mod
    from backend.app.readiness import ReadinessError

    class _FakeConn(object):
        def close(self):
            pass

    monkeypatch.setattr(main_mod, "connect", lambda _p: _FakeConn())
    monkeypatch.setattr(main_mod, "validate_schema", lambda _c: None)
    monkeypatch.setattr(main_mod, "settings", _fatal_api_settings())

    async def _run():
        async with main_mod.lifespan(None):  # type: ignore
            pass

    with pytest.raises(ReadinessError):
        asyncio.run(_run())


# -- worker startup fails closed -------------------------------------------

def test_worker_validate_or_exit_exits_on_fatal(monkeypatch):
    from backend.app.worker import dispatch as dispatch_lib

    monkeypatch.setattr(dispatch_lib, "settings",
                        _settings(secrets_file="CHANGEME-secrets.env"))
    with pytest.raises(SystemExit):
        dispatch_lib._validate_or_exit("worker")


def test_worker_validate_or_exit_clean_returns_none(tmp_path, monkeypatch):
    from backend.app.worker import dispatch as dispatch_lib

    release = tmp_path / "release"
    (release / "venv" / "bin").mkdir(parents=True)
    (release / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (release / "venv" / "bin" / "python").chmod(0o755)
    for name in ("node", "npm", "npx"):
        exe = tmp_path / name
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    inventory = tmp_path / "inventory.json"
    inventory.write_text('{"tools": {}}')
    secrets = tmp_path / "secrets.env"
    secrets.write_text("TEST_KEY=test-value\n")

    monkeypatch.setenv("EGA_RELEASE_ROOT", str(release))
    monkeypatch.setenv("EGA_INVENTORY_FILE", str(inventory))
    monkeypatch.setattr(dispatch_lib, "settings", _settings(
        secrets_file=str(secrets),
        inventory_file=str(inventory),
        node_path=str(tmp_path / "node"),
        npm_path=str(tmp_path / "npm"),
        npx_path=str(tmp_path / "npx"),
    ))
    assert dispatch_lib._validate_or_exit("worker") is None


def test_worker_main_uses_startup_gate():
    from backend.app.worker import dispatch as dispatch_lib

    # The gate is extracted so it is testable without the loop/DB; main
    # must actually call it (no inline duplicate that could drift).
    src = inspect.getsource(dispatch_lib.main)
    assert "_validate_or_exit(" in src
