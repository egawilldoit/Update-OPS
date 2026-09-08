"""Deploy bootstrap regression tests (F06-F10, write-only).

Covers the version-independent maintenance controller and script
integration without executing deployments, daemons, or probes.
quiescence-check.py is stdlib-only and importable directly; script
assertions are static marker checks. NOT EXECUTED in this phase per
instruction. Python 3.10 compatible, pytest style.
"""
from __future__ import annotations

import importlib.util
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


def _load_script_module(module_name, filename):
    """Load a dash-named deploy script by file path (B1).

    deploy/etc/quiescence-check.py is a valid executable script name
    but cannot be imported with a plain `import` statement; load it
    explicitly with importlib under a stable unique test module name.
    Does not mutate production imports.
    """
    path = os.path.join(_DEPLOY_ETC, filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qc_lib = _load_script_module(
    "ega_test_quiescence_check",
    "quiescence-check.py",
)

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


# -- G07/G08 fail-closed quiescence --------------------------------------------------

def _quiescent_env(tmp_path, monkeypatch, name="g78"):
    """Empty migrated DB + fresh heartbeat + drain + stopped units."""

    state_dir = str(tmp_path / ("%s-state" % name))
    os.makedirs(state_dir, exist_ok=True)
    config_path = str(tmp_path / ("%s-config.json" % name))
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
        qc_lib, "query_user_unit",
        lambda owner, uid, unit: ("confirmed_stopped",
                                  "manager=inactive"))
    monkeypatch.setattr(
        qc_lib, "query_system_unit",
        lambda unit: ("confirmed_stopped", "manager=inactive"))
    monkeypatch.setattr(
        qc_lib, "list_user_job_units",
        lambda owner, uid, prefix="ega-update-job-": ([], ""))
    return state_dir, config_path, db_path


def test_g07_jobs_unreadable_not_empty(tmp_path):

    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    config_path = str(tmp_path / "config.json")
    garbage = os.path.join(state_dir, "state.db")
    with open(garbage, "w", encoding="utf-8") as fh:
        fh.write("not a database at all")
    _write_config(config_path, state_dir, garbage)
    _heartbeat(state_dir, fresh=True)
    open(os.path.join(state_dir, "drain"), "w").close()
    report = qc_lib.assess(config_path, require_drain=True)
    assert report["quiescent"] is False
    assert any("unreadable" in reason or "unprovable" in reason
               for reason in report["reasons"])


def test_g07_missing_leases_table_blocks(tmp_path, monkeypatch):
    """A v4-schema DB without execution_leases is corruption, not zero
    leases (G07): explicit version-aware handling, no silent downgrade."""
    from backend.app import db as db_lib

    state_dir, config_path, db_path = _quiescent_env(
        tmp_path, monkeypatch, name="g07leases")
    conn = db_lib.connect(db_path)
    conn.execute("DROP TABLE execution_leases")
    conn.commit()
    conn.close()
    report = qc_lib.assess(config_path, require_drain=True)
    assert report["quiescent"] is False
    assert any("execution_leases" in reason
               for reason in report["reasons"])


def test_g08_terminal_held_lease_blocks(tmp_path, monkeypatch):
    """Terminal job + unreleased mutation lease blocks maintenance
    even with everything else clean (G08)."""
    from backend.app import tx as tx_lib

    state_dir, config_path, db_path = _quiescent_env(
        tmp_path, monkeypatch, name="g08held")
    from backend.app import db as db_lib

    conn = db_lib.connect(db_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    from backend.app.admission import admit
    support_lib.test_release_root()
    jid, created, err = admit(
        conn, "owner@example.invalid", "k-g08h", row["id"], False,
        "fp-test-1", True, False)
    assert err == "" and created
    tx_lib.transition_tx(conn, jid, "succeeded", step="verifying",
                         expect_states=["accepted"],
                         update={"after_version": "9.9.9"},
                         event="succeeded", event_detail="")
    conn.close()
    report = qc_lib.assess(config_path, require_drain=True)
    assert report["quiescent"] is False
    assert jid in report["details"]["held_leases"]
    assert any("lease" in reason for reason in report["reasons"])


def test_g08_terminal_held_live_delegated_blocks(tmp_path, monkeypatch):
    from backend.app import tx as tx_lib

    state_dir, config_path, db_path = _quiescent_env(
        tmp_path, monkeypatch, name="g08del")
    from backend.app import db as db_lib
    import json as _json

    conn = db_lib.connect(db_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    conn.execute("UPDATE plans SET services=? WHERE id=?",
                 (_json.dumps(["svc-stuck.service"]), row["id"]))
    stored = dict(conn.execute("SELECT * FROM plans WHERE id=?",
                               (row["id"],)).fetchone())
    from backend.app import plans as plans_lib
    conn.execute("UPDATE plans SET plan_hash=? WHERE id=?",
                 (plans_lib.canonical_plan_hash(
                     plans_lib._hash_view(stored)), row["id"]))
    conn.commit()
    from backend.app.admission import admit
    support_lib.test_release_root()
    jid, created, err = admit(
        conn, "owner@example.invalid", "k-g08d", row["id"], False,
        "fp-test-1", True, False)
    assert err == "" and created
    tx_lib.transition_tx(conn, jid, "failed", step="updating",
                         expect_states=["accepted"],
                         update={"error_code": "install_failed"},
                         event="failed", event_detail="")
    conn.close()
    monkeypatch.setattr(
        qc_lib, "query_user_unit",
        lambda owner, uid, unit: ("live", "active/running")
        if unit == "svc-stuck.service"
        else ("confirmed_stopped", "manager=inactive"))
    report = qc_lib.assess(config_path, require_drain=True)
    assert report["quiescent"] is False
    assert any(e["service"] == "svc-stuck.service"
               for e in report["details"]["delegated_operations"])


def test_g08_terminal_held_unknown_delegated_blocks(tmp_path, monkeypatch):
    from backend.app import tx as tx_lib

    state_dir, config_path, db_path = _quiescent_env(
        tmp_path, monkeypatch, name="g08unk")
    from backend.app import db as db_lib
    import json as _json

    conn = db_lib.connect(db_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    conn.execute("UPDATE plans SET services=? WHERE id=?",
                 (_json.dumps(["svc-mystery.service"]), row["id"]))
    stored = dict(conn.execute("SELECT * FROM plans WHERE id=?",
                               (row["id"],)).fetchone())
    from backend.app import plans as plans_lib
    conn.execute("UPDATE plans SET plan_hash=? WHERE id=?",
                 (plans_lib.canonical_plan_hash(
                     plans_lib._hash_view(stored)), row["id"]))
    conn.commit()
    from backend.app.admission import admit
    support_lib.test_release_root()
    jid, created, err = admit(
        conn, "owner@example.invalid", "k-g08u", row["id"], False,
        "fp-test-1", True, False)
    assert err == "" and created
    tx_lib.transition_tx(conn, jid, "failed", step="updating",
                         expect_states=["accepted"],
                         update={"error_code": "install_failed"},
                         event="failed", event_detail="")
    conn.close()
    monkeypatch.setattr(
        qc_lib, "query_user_unit",
        lambda owner, uid, unit: ("unknown", "bus down")
        if unit == "svc-mystery.service"
        else ("confirmed_stopped", "manager=inactive"))
    monkeypatch.setattr(
        qc_lib, "query_system_unit",
        lambda unit: ("unknown", "bus down"))
    report = qc_lib.assess(config_path, require_drain=True)
    assert report["quiescent"] is False


def test_g08_clean_state_potentially_quiescent(tmp_path, monkeypatch):

    state_dir, config_path, db_path = _quiescent_env(
        tmp_path, monkeypatch, name="g08clean")
    report = qc_lib.assess(config_path, require_drain=True)
    assert report["quiescent"] is True, report["reasons"]
    assert report["reasons"] == []


def test_g08_unknown_runner_blocks(tmp_path, monkeypatch):

    state_dir, config_path, db_path = _quiescent_env(
        tmp_path, monkeypatch, name="g08ru")
    from backend.app import db as db_lib

    conn = db_lib.connect(db_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    from backend.app.admission import admit
    support_lib.test_release_root()
    jid, created, err = admit(
        conn, "owner@example.invalid", "k-g08r", row["id"], False,
        "fp-test-1", True, False)
    assert err == "" and created
    conn.close()
    from backend.app.reconcile_core import canonical_unit
    unit = canonical_unit(jid)
    monkeypatch.setattr(
        qc_lib, "query_user_unit",
        lambda owner, uid, u: ("unknown", "bus down")
        if u == unit else ("confirmed_stopped", "manager=inactive"))
    report = qc_lib.assess(config_path, require_drain=True)
    assert report["quiescent"] is False


def test_scripts_use_checkout_controller_not_release_cli():
    """Quiescence gates must not depend on any installed release CLI
    (F07/F10): no CURRENT_LINK venv status calls in the gate path, no
    ambient-interpreter fallback."""
    path = os.path.join(_REPO_ROOT, "deploy", "scripts", "upgrade.sh")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert 'STATUS_PY="python3"' not in text
    assert "quiescence-check.py" in text


def test_g08_drain_absent_blocks(tmp_path, monkeypatch):

    state_dir, config_path, db_path = _quiescent_env(
        tmp_path, monkeypatch, name="g08drain")
    os.remove(os.path.join(state_dir, "drain"))
    report = qc_lib.assess(config_path, require_drain=True)
    assert report["quiescent"] is False
    assert any("drain" in reason for reason in report["reasons"])
