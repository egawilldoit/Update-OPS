"""Deploy bootstrap regression tests (F06-F10, write-only).

Covers the version-independent maintenance controller and script
integration without executing deployments, daemons, or probes.
quiescence-check.py is stdlib-only and importable directly; script
assertions are static marker checks. NOT EXECUTED in this phase per
instruction. Python 3.10 compatible, pytest style.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_DEPLOY_ETC = os.path.join(_REPO_ROOT, "deploy", "etc")
if _DEPLOY_ETC not in sys.path:
    sys.path.insert(0, _DEPLOY_ETC)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import quiescence_check as qc_lib

import support as support_lib


def _write_config(path, state_dir, db_path=None, tool_owner="ubuntu"):
    config = {"state_dir": state_dir,
              "db_path": db_path or os.path.join(state_dir, "state.db"),
              "tool_owner": tool_owner}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh)
    return config


def _heartbeat(state_dir, fresh=True):
    from datetime import datetime, timedelta, timezone

    ts = datetime.now(timezone.utc)
    if not fresh:
        ts = ts - timedelta(seconds=120)
    with open(os.path.join(state_dir, "dispatcher.heartbeat"), "w",
              encoding="utf-8") as fh:
        json.dump({"ts": ts.isoformat(), "pid": 1}, fh)


def _stopped(monkeypatch):
    monkeypatch.setattr(
        qc_lib, "query_user_unit",
        lambda owner, uid, unit: ("confirmed_stopped", "manager=inactive"))
    monkeypatch.setattr(
        qc_lib, "query_system_unit",
        lambda unit: ("confirmed_stopped", "manager=inactive"))
    monkeypatch.setattr(
        qc_lib, "list_user_job_units", lambda owner, uid, prefix=None:
        ([], ""))


def test_f06_trusted_bootstrap_needs_no_candidate(tmp_path):
    """Config parse + quiescence use only stdlib + checkout code: no
    candidate release, venv, or installed release required (F06)."""
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    config_path = str(tmp_path / "config.json")
    db_path = os.path.join(state_dir, "state.db")
    _write_config(config_path, state_dir, db_path)
    from backend.app import db as db_lib

    conn = db_lib.connect(db_path)
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.commit()
    conn.close()
    _heartbeat(state_dir, fresh=True)
    open(os.path.join(state_dir, "drain"), "w").close()
    report = qc_lib.assess(config_path, require_drain=True)
    assert report["quiescent"] is True, report["reasons"]


def test_f07_old_release_feature_independence(tmp_path, monkeypatch):
    """Quiescence proof never shells to any release CLI (F07): the
    controller only reads DB/config/systemd directly."""
    import inspect

    src = inspect.getsource(qc_lib.assess) + inspect.getsource(qc_lib.main)
    assert "backend.app.cli" not in src
    _stopped(monkeypatch)
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    config_path = str(tmp_path / "config.json")
    db_path = os.path.join(state_dir, "state.db")
    _write_config(config_path, state_dir, db_path)
    from backend.app import db as db_lib

    conn = db_lib.connect(db_path)
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.commit()
    conn.close()
    _heartbeat(state_dir, fresh=True)
    open(os.path.join(state_dir, "drain"), "w").close()
    report = qc_lib.assess(config_path, require_drain=True)
    assert report["quiescent"] is True, report["reasons"]


def test_f08_configured_nondefault_db_path(tmp_path, monkeypatch):
    """Existing-deployment detection follows the CONFIGURED db path,
    including non-default locations (F08)."""
    _stopped(monkeypatch)
    state_dir = str(tmp_path / "custom-state")
    os.makedirs(state_dir, exist_ok=True)
    custom_db = os.path.join(state_dir, "custom-name.db")
    config_path = str(tmp_path / "config.json")
    _write_config(config_path, state_dir, custom_db)
    from backend.app import db as db_lib

    conn = db_lib.connect(custom_db)
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.commit()
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    from backend.app.admission import admit
    support_lib.test_release_root()
    jid, created, err = admit(conn, "owner@example.invalid", "k-f08",
                              row["id"], False, "fp-test-1", True, False)
    assert err == "" and created
    conn.close()
    report = qc_lib.assess(config_path, require_drain=True)
    # Drain absent here, but the ACTIVE job must be reported from the
    # configured (non-default) database.
    assert report["details"]["active_job"] == jid


def test_f08_malformed_config_blocks(tmp_path):
    config_path = str(tmp_path / "config.json")
    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write("{not valid json!!!")
    report = qc_lib.assess(config_path, require_drain=True)
    assert report["quiescent"] is False
    assert any("config" in reason for reason in report["reasons"])


def test_f09_no_direct_stop_fallback_in_scripts():
    """Neither script may stop services on unproven quiescence (F09)."""
    for name in ("install.sh", "upgrade.sh"):
        path = os.path.join(_REPO_ROOT, "deploy", "scripts", name)
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        assert "stopping services directly" not in text, name
        assert "quiescence-check.py" in text, name


def test_quiescence_live_runner_blocks(tmp_path, monkeypatch):
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    config_path = str(tmp_path / "config.json")
    db_path = os.path.join(state_dir, "state.db")
    _write_config(config_path, state_dir, db_path)
    from backend.app import db as db_lib

    conn = db_lib.connect(db_path)
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.commit()
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    from backend.app.admission import admit
    support_lib.test_release_root()
    jid, _, err = admit(conn, "owner@example.invalid", "k-ql", row["id"],
                        False, "fp-test-1", True, False)
    assert err == ""
    conn.close()
    _heartbeat(state_dir, fresh=True)
    open(os.path.join(state_dir, "drain"), "w").close()

    from backend.app.reconcile_core import canonical_unit
    unit = canonical_unit(jid)
    monkeypatch.setattr(
        qc_lib, "query_user_unit",
        lambda owner, uid, u: ("live", "active/running")
        if u == unit else ("confirmed_stopped", "manager=inactive"))
    monkeypatch.setattr(
        qc_lib, "query_system_unit",
        lambda u: ("confirmed_stopped", "manager=inactive"))
    monkeypatch.setattr(
        qc_lib, "list_user_job_units",
        lambda owner, uid, prefix="ega-update-job-": ([], ""))
    report = qc_lib.assess(config_path, require_drain=True)
    assert report["quiescent"] is False
    assert any(unit in str(u) for u in report["details"]["live_units"])


def test_quiescence_unknown_runner_blocks(tmp_path, monkeypatch):
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    config_path = str(tmp_path / "config.json")
    db_path = os.path.join(state_dir, "state.db")
    _write_config(config_path, state_dir, db_path)
    from backend.app import db as db_lib

    conn = db_lib.connect(db_path)
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.commit()
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    from backend.app.admission import admit
    support_lib.test_release_root()
    jid, _, err = admit(conn, "owner@example.invalid", "k-qu", row["id"],
                        False, "fp-test-1", True, False)
    assert err == ""
    conn.close()
    _heartbeat(state_dir, fresh=True)
    open(os.path.join(state_dir, "drain"), "w").close()
    monkeypatch.setattr(
        qc_lib, "query_user_unit",
        lambda owner, uid, u: ("unknown", "bus down"))
    monkeypatch.setattr(
        qc_lib, "query_system_unit",
        lambda u: ("unknown", "bus down"))
    monkeypatch.setattr(
        qc_lib, "list_user_job_units",
        lambda owner, uid, prefix="ega-update-job-": ([], ""))
    report = qc_lib.assess(config_path, require_drain=True)
    assert report["quiescent"] is False


def test_quiescence_unresolved_blocks(tmp_path, monkeypatch):
    _stopped(monkeypatch)
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    config_path = str(tmp_path / "config.json")
    db_path = os.path.join(state_dir, "state.db")
    _write_config(config_path, state_dir, db_path)
    from backend.app import db as db_lib

    conn = db_lib.connect(db_path)
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.commit()
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    from backend.app.admission import admit
    support_lib.test_release_root()
    jid, _, err = admit(conn, "owner@example.invalid", "k-qur", row["id"],
                        False, "fp-test-1", True, False)
    assert err == ""
    conn.execute("UPDATE jobs SET unresolved=1 WHERE id=?", (jid,))
    conn.commit()
    conn.close()
    _heartbeat(state_dir, fresh=True)
    open(os.path.join(state_dir, "drain"), "w").close()
    monkeypatch.setattr(
        qc_lib, "list_user_job_units",
        lambda owner, uid, prefix="ega-update-job-": ([], ""))
    report = qc_lib.assess(config_path, require_drain=True)
    assert report["quiescent"] is False
    assert jid in report["details"]["unresolved_jobs"]


def test_quiescence_all_clear(tmp_path, monkeypatch):
    _stopped(monkeypatch)
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    config_path = str(tmp_path / "config.json")
    db_path = os.path.join(state_dir, "state.db")
    _write_config(config_path, state_dir, db_path)
    from backend.app import db as db_lib

    conn = db_lib.connect(db_path)
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.commit()
    conn.close()
    _heartbeat(state_dir, fresh=True)
    open(os.path.join(state_dir, "drain"), "w").close()
    monkeypatch.setattr(
        qc_lib, "list_user_job_units",
        lambda owner, uid, prefix="ega-update-job-": ([], ""))
    report = qc_lib.assess(config_path, require_drain=True)
    assert report["quiescent"] is True, report["reasons"]
    assert report["reasons"] == []


def test_scripts_use_checkout_controller_not_release_cli():
    """Quiescence gates must not depend on any installed release CLI
    (F07/F10): no CURRENT_LINK venv status calls in the gate path, no
    ambient-interpreter fallback."""
    path = os.path.join(_REPO_ROOT, "deploy", "scripts", "upgrade.sh")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert 'STATUS_PY="python3"' not in text
    assert "quiescence-check.py" in text
