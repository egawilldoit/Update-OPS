"""Focused integration regression tests (N01-N18, write-only).

Behavioral regression artifacts for every N-finding. NOT EXECUTED in
this phase per instruction. Mocks stay at external boundaries
(systemd bus, process spawn); console-component integration is never
mocked away. Python 3.10 compatible, pytest style, stdlib + backend.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import sys
import tarfile
import uuid

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from backend.app import db as db_lib
from backend.app import jobs as jobs_lib
from backend.app import leases as leases_lib
from backend.app import quiescence as quiescence_lib
from backend.app import receipts as receipts_lib
from backend.app import tx as tx_lib
from backend.app import units as units_lib
from backend.app.admission import admit as admit_lib
from backend.app import plans as plans_lib

import support as support_lib


def _fresh_db(tmp_path, name="n.db"):
    conn = db_lib.connect(str(tmp_path / name))
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.commit()
    return conn


def _ns_state(tmp_path):
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    return state_dir


class _Settings(object):
    def __init__(self, state_dir):
        self.state_dir = state_dir


# -- N01 quiescence ----------------------------------------------------------

def _patch_units(monkeypatch, live=(), unknown=(), stray=None):
    def _fake_query(unit, timeout_s=10):
        if unit in live:
            return {"state": "live", "unit": unit, "active_state": "active",
                    "sub_state": "running", "main_pid": 1, "cgroup": "",
                    "identity_ok": True, "detail": ""}
        if unit in unknown:
            return {"state": "unknown", "unit": unit, "active_state": "",
                    "sub_state": "", "main_pid": 0, "cgroup": "",
                    "identity_ok": False, "detail": "bus down"}
        return {"state": "confirmed_stopped", "unit": unit,
                "active_state": "inactive", "sub_state": "dead",
                "main_pid": 0, "cgroup": "", "identity_ok": True,
                "detail": "manager=inactive load=not-found"}
    monkeypatch.setattr(units_lib, "query_unit", _fake_query)
    monkeypatch.setattr(units_lib, "query_unit_system", _fake_query)
    if stray is None:
        stray = []
    monkeypatch.setattr(units_lib, "list_job_units",
                        lambda prefix="ega-update-job-", timeout_s=10:
                        (list(stray), ""))


def _write_heartbeat(state_dir, fresh=True):
    from datetime import datetime, timedelta, timezone
    ts = datetime.now(timezone.utc)
    if not fresh:
        ts = ts - timedelta(seconds=120)
    with open(os.path.join(state_dir, "dispatcher.heartbeat"), "w",
              encoding="utf-8") as fh:
        json.dump({"ts": ts.isoformat(), "pid": 1}, fh)


def test_n01_schema_shape(tmp_path):
    conn = _fresh_db(tmp_path)
    state_dir = _ns_state(tmp_path)
    out = quiescence_lib.assess_quiescence(conn, _Settings(state_dir))
    for key in ("worker_alive", "active_job", "unresolved_jobs",
                "recovery_jobs", "unresolved_units", "live_units",
                "delegated_operations", "drain", "quiescent", "reasons"):
        assert key in out, key
    conn.close()


def test_n01_active_job_not_quiescent(tmp_path, monkeypatch):
    conn = _fresh_db(tmp_path)
    state_dir = _ns_state(tmp_path)
    _write_heartbeat(state_dir, fresh=True)
    open(os.path.join(state_dir, "drain"), "w").close()
    _patch_units(monkeypatch)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid, created, err = admit_lib(
        conn, "owner@example.invalid", "k-n01a", row["id"], False, "fp-test-1",
        True, False)
    assert err == "" and created
    out = quiescence_lib.assess_quiescence(conn, _Settings(state_dir))
    assert out["quiescent"] is False
    assert out["active_job"] == jid
    conn.close()


def test_n01_unresolved_not_quiescent(tmp_path, monkeypatch):
    conn = _fresh_db(tmp_path)
    state_dir = _ns_state(tmp_path)
    _write_heartbeat(state_dir, fresh=True)
    open(os.path.join(state_dir, "drain"), "w").close()
    _patch_units(monkeypatch)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid, _, err = admit_lib(
        conn, "owner@example.invalid", "k-n01b", row["id"], False, "fp-test-1",
        True, False)
    assert err == ""
    conn.execute("UPDATE jobs SET unresolved=1 WHERE id=?", (jid,))
    conn.commit()
    out = quiescence_lib.assess_quiescence(conn, _Settings(state_dir))
    assert out["quiescent"] is False
    assert jid in out["unresolved_jobs"]
    conn.close()


def test_n01_live_and_unknown_units_block(tmp_path, monkeypatch):
    conn = _fresh_db(tmp_path)
    state_dir = _ns_state(tmp_path)
    _write_heartbeat(state_dir, fresh=True)
    open(os.path.join(state_dir, "drain"), "w").close()
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid, _, err = admit_lib(
        conn, "owner@example.invalid", "k-n01c", row["id"], False, "fp-test-1",
        True, False)
    assert err == ""
    from backend.app.reconcile_core import canonical_unit
    unit = canonical_unit(jid)
    _patch_units(monkeypatch, live=[unit])
    out = quiescence_lib.assess_quiescence(conn, _Settings(state_dir))
    assert out["quiescent"] is False
    assert any(u["unit"] == unit for u in out["live_units"])
    _patch_units(monkeypatch, unknown=[unit])
    out2 = quiescence_lib.assess_quiescence(conn, _Settings(state_dir))
    assert out2["quiescent"] is False
    conn.close()


def test_n01_proven_empty_is_quiescent(tmp_path, monkeypatch):
    conn = _fresh_db(tmp_path)
    state_dir = _ns_state(tmp_path)
    _write_heartbeat(state_dir, fresh=True)
    open(os.path.join(state_dir, "drain"), "w").close()
    _patch_units(monkeypatch)
    out = quiescence_lib.assess_quiescence(conn, _Settings(state_dir))
    assert out["quiescent"] is True, out["reasons"]
    assert out["reasons"] == []
    conn.close()


def test_n01_missing_drain_and_dead_worker_block(tmp_path, monkeypatch):
    conn = _fresh_db(tmp_path)
    state_dir = _ns_state(tmp_path)
    _patch_units(monkeypatch)
    out = quiescence_lib.assess_quiescence(conn, _Settings(state_dir))
    assert out["quiescent"] is False
    assert out["worker_alive"] is False
    assert out["drain"] is False
    conn.close()


# -- N02 strict integers -----------------------------------------------------

def test_n02_installer_exit_zero_survives():
    assert receipts_lib._strict_int(0, "installer_exit") == 0
    assert receipts_lib._strict_int("0", "installer_exit") == 0
    assert receipts_lib._strict_int(4, "installer_exit") == 4


def test_n02_malformed_fails_closed():
    for bad in (None, "", "   ", "abc", "1.5", True, False, [], {}):
        with pytest.raises(ValueError):
            receipts_lib._strict_int(bad, "installer_exit")


def test_n02_success_receipt_zero_exit_validates(tmp_path):
    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid, _, err = admit_lib(
        conn, "owner@example.invalid", "k-n02", row["id"], False, "fp-test-1",
        True, False)
    assert err == ""
    assert jobs_lib.claim_with_nonce(conn, jid, "n-n02") is True
    data = receipts_lib.build_receipt(
        jid, "hermes", "succeeded", "1.0", "9.9.9", 0, "", [
            {"name": "smoke", "result": "pass", "mandatory": True,
             "summary": "ok"}], "2026-09-08T00:00:00+00:00",
        plan_id=row["id"], plan_hash=row["plan_hash"],
        attempt_nonce="n-n02",
        release_path=row["release_path"], target="9.9.9",
        target_mode="exact", expected_checks=["smoke"], installer_exit=0,
        install_outcome="succeeded", actual_change=True,
        evidence_durable=True)
    ok, reason = receipts_lib.validate_receipt(data)
    assert ok, reason
    conn.close()


# -- N03 binding -------------------------------------------------------------

def _bound_receipt(conn, jid, nonce, **over):
    row = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
    plan = dict(conn.execute("SELECT * FROM plans WHERE id=?",
                             (row["plan_id"],)).fetchone())
    base = dict(plan_hash=plan["plan_hash"],
                release_path=plan["release_path"], target=plan["target"],
                target_mode=plan["target_mode"])
    base.update(over)
    return receipts_lib.build_receipt(
        jid, row["tool_id"], "succeeded", "1.0", plan["target"], 0, "", [
            {"name": "smoke", "result": "pass", "mandatory": True,
             "summary": "ok"}], "2026-09-08T00:00:00+00:00",
        plan_id=row["plan_id"], attempt_nonce=nonce,
        installer_exit=0, install_outcome="succeeded", actual_change=True,
        evidence_durable=True, expected_checks=["smoke"], **base)


def test_n03_binding_matrix(tmp_path):
    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    # NOTE: support plans carry required_checks ["smoke"].
    jid, _, err = admit_lib(
        conn, "owner@example.invalid", "k-n03", row["id"], False, "fp-test-1",
        True, False)
    assert err == ""
    assert jobs_lib.claim_with_nonce(conn, jid, "n-n03") is True
    good = _bound_receipt(conn, jid, "n-n03")
    ok, reason = receipts_lib.validate_receipt(good)
    assert ok, reason
    job = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
    plan = dict(conn.execute("SELECT * FROM plans WHERE id=?",
                             (job["plan_id"],)).fetchone())
    bound, why = receipts_lib.check_binding(good, job, plan, jid)
    assert bound, why
    for key, value in (("plan_hash", "other"), ("release_path", "/elsewhere"),
                       ("target", "0.0.0"), ("target_mode", "native_latest"),
                       ("attempt_nonce", "wrong")):
        mutated = dict(good)
        mutated[key] = value
        b2, _ = receipts_lib.check_binding(mutated, job, plan, jid)
        assert b2 is False, key
    weakened = dict(good)
    weakened["checks"] = [{"name": "smoke", "result": "pass",
                           "mandatory": False, "summary": ""}]
    b3, _ = receipts_lib.check_binding(weakened, job, plan, jid)
    assert b3 is False
    dropped = dict(good)
    dropped["checks"] = []
    dropped["expected_checks"] = []
    b4, _ = receipts_lib.check_binding(dropped, job, plan, jid)
    assert b4 is False
    failing = dict(good)
    failing["checks"] = [{"name": "smoke", "result": "fail",
                          "mandatory": True, "summary": ""}]
    assert receipts_lib.validate_receipt(failing)[0] is False
    conn.close()


# -- N04/N05/N06 migrations ----------------------------------------------------

def test_n04_runner_and_services_validate_only():
    import inspect
    from backend.app.worker import runner as runner_lib
    from backend.app.worker import dispatch as dispatch_lib
    from backend.app import main as main_lib
    assert "validate_schema(" in inspect.getsource(runner_lib.Runner._connect)
    assert "migrate(" not in inspect.getsource(runner_lib.Runner._connect)
    assert "validate_schema(" in inspect.getsource(dispatch_lib.main)
    assert "migrate(" not in inspect.getsource(dispatch_lib.main)
    assert "validate_schema(" in inspect.getsource(main_lib.lifespan)
    assert "migrate(" not in inspect.getsource(main_lib.lifespan)


def test_n06_validate_is_read_only(tmp_path):
    conn = _fresh_db(tmp_path)
    before_master = conn.execute(
        "SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    before_count = conn.execute(
        "SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]
    assert db_lib.validate_schema(conn) == db_lib.CODE_VERSION
    after_master = conn.execute(
        "SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    after_count = conn.execute(
        "SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]
    assert [tuple(r) for r in before_master] == [tuple(r) for r in after_master]
    assert before_count == after_count
    conn.close()
    # Ledger absent -> pending, and still absent afterwards (no repair).
    path = str(tmp_path / "legacy.db")
    raw = db_lib.connect(path)
    with open(os.path.join(_REPO_ROOT, "backend", "migrations",
                           "001_init.sql"), "r", encoding="utf-8") as fh:
        raw.executescript(fh.read())
    raw.commit()
    with pytest.raises(db_lib.SchemaError):
        db_lib.validate_schema(raw)
    assert raw.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND"
        " name='schema_migrations'").fetchall() == []
    # ...but migrate completes it, resumably.
    assert db_lib.migrate(raw) == db_lib.CODE_VERSION
    assert db_lib.validate_schema(raw) == db_lib.CODE_VERSION
    raw.close()


def test_n05_partial_003_resumes(tmp_path):
    path = str(tmp_path / "partial.db")
    conn = db_lib.connect(path)
    for filename in ("001_init.sql", "002_execution_hardening.sql"):
        with open(os.path.join(_REPO_ROOT, "backend", "migrations",
                               filename), "r", encoding="utf-8") as fh:
            conn.executescript(fh.read())
    conn.commit()
    # Simulate interruption mid-003: one column present, no ledger.
    conn.execute("ALTER TABLE jobs ADD COLUMN attempt_claimed INTEGER"
                 " NOT NULL DEFAULT 0")
    conn.commit()
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    cols = {r["name"] for r in
            conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "attempt_claimed" in cols and "final_log_seq" in cols
    assert "env_fingerprint" in {
        r["name"] for r in
        conn.execute("PRAGMA table_info(plans)").fetchall()}
    # Rerun is a no-op.
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.close()


def test_n05_ledger_delete_reapplies_idempotently(tmp_path):
    conn = _fresh_db(tmp_path)
    conn.execute("DELETE FROM schema_migrations WHERE version=4")
    conn.execute("UPDATE schema_meta SET value='3' WHERE key='version'")
    conn.commit()
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.close()


# -- N07 cgroup proof ----------------------------------------------------------

def test_n07_permission_denied_cgroup_is_unknown(monkeypatch):
    monkeypatch.setattr(
        units_lib, "_show",
        lambda unit, timeout_s=10: {"ok": True, "props": {
            "Id": "u.service", "ActiveState": "inactive",
            "SubState": "dead", "MainPID": "0", "ControlGroup": "/x",
            "LoadState": "loaded"}})
    monkeypatch.setattr(units_lib, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(units_lib, "_cgroup_empty", lambda cg: None)
    assert units_lib.query_unit("u.service")["state"] == "unknown"


def test_n07_removal_proof_is_stopped(monkeypatch):
    monkeypatch.setattr(
        units_lib, "_show",
        lambda unit, timeout_s=10: {"ok": True, "props": {
            "Id": "", "ActiveState": "inactive", "SubState": "dead",
            "MainPID": "0", "ControlGroup": "",
            "LoadState": "not-found"}})
    info = units_lib.query_unit("u.service")
    assert info["state"] == "confirmed_stopped"


def test_n07_zero_pid_unknown_cgroup_is_unknown(monkeypatch):
    monkeypatch.setattr(
        units_lib, "_show",
        lambda unit, timeout_s=10: {"ok": True, "props": {
            "Id": "u.service", "ActiveState": "inactive",
            "SubState": "dead", "MainPID": "0", "ControlGroup": "/gone",
            "LoadState": "loaded"}})
    monkeypatch.setattr(units_lib, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(units_lib, "_cgroup_empty", lambda cg: None)
    assert units_lib.query_unit("u.service")["state"] == "unknown"


# -- N08 leases ------------------------------------------------------------------

def test_n08_probe_blocks_reservation_and_vice_versa(tmp_path):
    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    lease = leases_lib.acquire_probe_lease(conn, "hermes", "tester")
    assert lease
    jid, created, err = admit_lib(
        conn, "owner@example.invalid", "k-n08a", row["id"], False,
        "fp-test-1", True, False)
    assert err == "busy" and not created
    assert leases_lib.release_lease(conn, lease) is True
    jid2, created2, err2 = admit_lib(
        conn, "owner@example.invalid", "k-n08b", row["id"], False,
        "fp-test-1", True, False)
    assert err2 == "" and created2, err2
    # Mutation lease blocks new probe leases.
    assert leases_lib.acquire_probe_lease(conn, "hermes", "tester") is None
    conn.close()


def test_n08_stale_probe_reclaim_never_touches_mutation(tmp_path):
    conn = _fresh_db(tmp_path)
    lease = leases_lib.acquire_probe_lease(conn, "hermes", "tester")
    assert lease
    conn.execute("UPDATE execution_leases SET expires_at=?"
                 " WHERE id=?", ("2000-01-01T00:00:00+00:00", lease))
    conn.commit()
    assert leases_lib.reclaim_expired_probes(conn) == 1
    # Mutation leases never time-expire, even backdated.
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid, created, err = admit_lib(
        conn, "owner@example.invalid", "k-n08c", row["id"], False,
        "fp-test-1", True, False)
    assert err == "" and created
    conn.execute("UPDATE execution_leases SET acquired_at=?"
                 " WHERE kind='mutation'",
                 ("2000-01-01T00:00:00+00:00",))
    conn.commit()
    assert leases_lib.reclaim_expired_probes(conn) == 0
    assert leases_lib.active_mutation_lease(conn) is not None
    assert leases_lib.acquire_probe_lease(conn, "codex", "t2") is None
    conn.close()


# -- N09 env parity --------------------------------------------------------------

def test_n09_fingerprint_stable_and_release_sensitive(tmp_path):
    from backend.app.owner_env import canonical_fingerprint
    support_lib.test_release_root()
    a = canonical_fingerprint(None, None, "/rel-a")
    b = canonical_fingerprint(None, None, "/rel-a")
    c = canonical_fingerprint(None, None, "/rel-b")
    assert a and a == b
    assert a != c


def test_n09_changed_env_invalidates_plan(tmp_path):
    conn = _fresh_db(tmp_path)
    support_lib.test_release_root()
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    conn.execute("UPDATE plans SET env_fingerprint=? WHERE id=?",
                 ("stale-env", row["id"]))
    conn.commit()
    jid, created, err = admit_lib(
        conn, "owner@example.invalid", "k-n09", row["id"], False,
        "fp-test-1", True, False)
    assert err == "config_changed" and not created
    conn.close()


# -- N10/N11 supervision -----------------------------------------------------------

def test_n10_scope_argv_validation():
    from backend.app.worker import phase_run
    with pytest.raises(ValueError):
        phase_run._scope_argv("bad scope", "/r", "/c", "/p", "j",
                              "execute", "/a", "/b", "/c", "", 10.0)
    with pytest.raises(ValueError):
        phase_run._scope_argv("", "/r", "/c", "/p", "j",
                              "execute", "/a", "/b", "/c", "", 10.0)


def test_n10_missing_systemd_run_fails_closed(monkeypatch, tmp_path):
    from backend.app.worker import phase_run
    monkeypatch.setenv("EGA_RELEASE_ROOT", str(tmp_path))
    monkeypatch.setattr("os.path.isfile", lambda p: False)
    settings = type("S", (), {"log_dir": str(tmp_path),
                              "node_path": "", "npm_path": "",
                              "npx_path": "", "state_dir": str(tmp_path),
                              "tool_owner": "ubuntu"})()
    ok, data, error, timed_out = phase_run.run_supervised_phase(
        "hermes", "job-x", "verify", {}, 5.0, settings, str(tmp_path),
        lambda s, line: None)
    assert ok is False and timed_out is False
    assert "systemd-run" in error


def test_n11_no_subprocess_fallback(monkeypatch):
    from backend.app.adapters import registry as registry_lib
    monkeypatch.setattr(
        registry_lib, "_executor_run_stream",
        lambda *a, **k: (_ for _ in ()).throw(ImportError("gone")))
    res = registry_lib.run_fixed(["/bin/echo", "hi"], timeout=5)
    assert res.exit_code == 127
    assert "unsupervised" in (res.stderr or "")
    assert res.timed_out is False


def test_n11_no_direct_subprocess_in_adapters():
    import inspect
    from backend.app.adapters import registry as registry_lib
    src = inspect.getsource(registry_lib.run_fixed)
    assert "subprocess.run(" not in src
    assert "subprocess.Popen(" not in src


# -- N12/N13 fail-closed evidence ------------------------------------------------------

def test_n12_probe_sanitize_failure_is_error(tmp_path, monkeypatch):
    from backend.app.worker import dispatch as dispatch_lib
    import backend.app.sanitize as sanitize_lib
    monkeypatch.setattr(
        sanitize_lib, "sanitize_json",
        lambda obj, secrets=(): (_ for _ in ()).throw(
            sanitize_lib.SanitizerError("boom")))
    with pytest.raises(Exception):
        dispatch_lib._sanitize_payload({"a": "b"})


def test_n12_receipt_build_fails_not_raw(monkeypatch):
    import backend.app.sanitize as sanitize_lib
    monkeypatch.setattr(
        sanitize_lib, "sanitize_text",
        lambda text, secrets=(): (_ for _ in ()).throw(
            sanitize_lib.SanitizerError("boom")))
    with pytest.raises(Exception):
        receipts_lib.build_receipt(
            "j", "hermes", "failed", "1.0", "1.0", 4, "install_failed",
            [], "2026-09-08T00:00:00+00:00")


def test_n12_cli_envelope_fixed_message(monkeypatch):
    from backend.app import cli as cli_lib
    import backend.app.sanitize as sanitize_lib
    monkeypatch.setattr(
        sanitize_lib, "sanitize_json",
        lambda obj, secrets=(): (_ for _ in ()).throw(
            sanitize_lib.SanitizerError("boom")))
    import io as _io
    import contextlib as _ctx
    buf = _io.StringIO()
    with _ctx.redirect_stdout(buf):
        cli_lib._emit_envelope("hermes", "inspect",
                               detail="secret-bearing-boom")
    payload = json.loads(buf.getvalue())
    assert "secret-bearing-boom" not in json.dumps(payload)
    assert payload["detail"] == "detail withheld: sanitization failed"


def test_n13_tx_evidence_fail_closed(tmp_path, monkeypatch):
    import backend.app.sanitize as sanitize_lib
    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid = admit_lib(
        conn, "owner@example.invalid", "k-n13", row["id"], False,
        "fp-test-1", True, False)[0]
    monkeypatch.setattr(
        sanitize_lib, "sanitize_text",
        lambda text, secrets=(): (_ for _ in ()).throw(
            sanitize_lib.SanitizerError("boom")))
    from backend.app import tx as tx_lib
    with pytest.raises(tx_lib.TxError):
        tx_lib.transition_tx(conn, jid, "preflight", step="preflight",
                             expect_states=["accepted"],
                             event="preflight", event_detail="x")
    assert conn.execute(
        "SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()["state"] \
        == "accepted"
    conn.close()


# -- N14/N15 shared admission ------------------------------------------------------------

def test_n14_api_and_cli_share_admission():
    import inspect
    from backend.app.api import routes as routes_lib
    from backend.app import cli as cli_lib
    assert "admit(" in inspect.getsource(routes_lib.post_job)
    assert "admit(" in inspect.getsource(cli_lib.cmd_apply)
    assert "reserve_job(" not in inspect.getsource(routes_lib.post_job)
    assert "reserve_job(" not in inspect.getsource(cli_lib.cmd_apply)


def test_n15_plan_consume_atomic_rollback(tmp_path):
    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)

    class _FailPlans(object):
        """Connection proxy: fail exactly the plan-consume UPDATE so the
        test proves the job row rolls back with it (N15)."""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, params=()):
            if isinstance(sql, str) and sql.strip().upper().startswith(
                    "UPDATE plans SET used_at"):
                raise sqlite3.OperationalError("injected plan failure")
            return self._real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

    proxied = _FailPlans(conn)
    jid, created, err = admit_lib(
        proxied, "owner@example.invalid", "k-n15", row["id"], False,
        "fp-test-1", True, False)
    assert err == "unavailable" and not created
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 0
    assert conn.execute(
        "SELECT used_at FROM plans WHERE id=?",
        (row["id"],)).fetchone()["used_at"] == ""
    conn.close()


def test_n14_replay_during_drain_and_recovery(tmp_path):
    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid, created, err = admit_lib(
        conn, "owner@example.invalid", "k-n14r", row["id"], False,
        "fp-test-1", True, False)
    assert err == "" and created
    # Replay during drain still returns the recorded job.
    jid2, created2, err2 = admit_lib(
        conn, "owner@example.invalid", "k-n14r", row["id"], False,
        "fp-test-1", True, True)
    assert err2 == "" and created2 is False and jid2 == jid
    # Fresh reservation during drain is denied.
    row2 = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    _j, _c, err3 = admit_lib(
        conn, "owner@example.invalid", "k-n14fresh", row2["id"], False,
        "fp-test-1", True, True)
    assert err3 == "maintenance"
    # Recovery blocks fresh reservations but not replays.
    conn.execute("UPDATE jobs SET recovery_required=1 WHERE id=?", (jid,))
    conn.commit()
    jid3, _, err4 = admit_lib(
        conn, "owner@example.invalid", "k-n14r", row["id"], False,
        "fp-test-1", True, False)
    assert err4 == "" and jid3 == jid
    conn.close()


def test_n14_active_probe_lease_denies_reservation(tmp_path):
    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    lease = leases_lib.acquire_probe_lease(conn, "hermes", "tester")
    assert lease
    jid, created, err = admit_lib(
        conn, "owner@example.invalid", "k-n14p", row["id"], False,
        "fp-test-1", True, False)
    assert err == "busy" and not created
    conn.close()


# -- N16/N17 deploy ------------------------------------------------------------------

def test_n16_bad_config_parse_blocks(tmp_path, monkeypatch):
    from backend.app import config_cli as config_cli_lib
    monkeypatch.delenv("EGA_CONFIG_FILE", raising=False)
    assert config_cli_lib.main(["get", "db_path"]) == 3
    assert config_cli_lib.main(["get", "--require", "db_path"]) == 3
    assert config_cli_lib.main(["json"]) == 3
    assert config_cli_lib.main(["get", "no_such_key"]) == 3


def test_n16_no_inline_config_parsers_in_scripts():
    for name in ("install.sh", "upgrade.sh"):
        path = os.path.join(_REPO_ROOT, "deploy", "scripts", name)
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        assert "python3 -c 'import json" not in text, name
        assert "python -c 'import json" not in text, name
        assert "config_cli get" in text, name


def _write_tar(path, members):
    # type: (...) -> None
    with tarfile.open(path, "w") as tf:
        for item in members:
            info = tarfile.TarInfo(item["name"])
            data = item.get("data", b"")
            if item.get("type") == tarfile.DIRTYPE:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
                continue
            if item.get("type") in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.type = item["type"]
                info.linkname = item.get("linkname", "")
                tf.addfile(info)
                continue
            if item.get("type") == tarfile.CHRTYPE:
                info.type = tarfile.CHRTYPE
                tf.addfile(info)
                continue
            info.type = tarfile.REGTYPE
            info.mode = item.get("mode", 0o644)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def _safe_members():
    return [
        {"name": "backend/app/x.py", "data": b"x = 1\n"},
        {"name": "systemd/a.service", "data": b"[Unit]\n"},
        {"name": "deploy/etc/v.py", "data": b"print(1)\n"},
    ]


def test_n17_rejects_absolute_member(tmp_path):
    import sys as _sys
    _sys.path.insert(0, os.path.join(_REPO_ROOT, "deploy", "etc"))
    import validate_archive
    path = str(tmp_path / "evil.tar")
    _write_tar(path, _safe_members() + [
        {"name": "/etc/evil", "data": b"x"}])
    assert validate_archive.validate(path, str(tmp_path / "dest")) == 3


def test_n17_rejects_traversal_member(tmp_path):
    import sys as _sys
    _sys.path.insert(0, os.path.join(_REPO_ROOT, "deploy", "etc"))
    import validate_archive
    path = str(tmp_path / "evil.tar")
    _write_tar(path, _safe_members() + [
        {"name": "../../evil", "data": b"x"}])
    assert validate_archive.validate(path, str(tmp_path / "dest")) == 3


def test_n17_rejects_escaping_symlink_and_hardlink(tmp_path):
    import sys as _sys
    _sys.path.insert(0, os.path.join(_REPO_ROOT, "deploy", "etc"))
    import validate_archive
    for link_type in (tarfile.SYMTYPE, tarfile.LNKTYPE):
        path = str(tmp_path / "evil.tar")
        _write_tar(path, _safe_members() + [
            {"name": "backend/evil", "type": link_type,
             "linkname": "../../etc/passwd"}])
        assert validate_archive.validate(path, str(tmp_path / "dest")) == 3


def test_n17_rejects_special_and_bad_modes(tmp_path):
    import sys as _sys
    _sys.path.insert(0, os.path.join(_REPO_ROOT, "deploy", "etc"))
    import validate_archive
    path = str(tmp_path / "evil.tar")
    _write_tar(path, _safe_members() + [
        {"name": "backend/dev", "type": tarfile.CHRTYPE}])
    assert validate_archive.validate(path, str(tmp_path / "dest")) == 3
    path2 = str(tmp_path / "evil2.tar")
    _write_tar(path2, _safe_members() + [
        {"name": "backend/suid", "data": b"x", "mode": 0o4755}])
    assert validate_archive.validate(path2, str(tmp_path / "dest")) == 3


def test_n17_accepts_safe_archive(tmp_path):
    import sys as _sys
    _sys.path.insert(0, os.path.join(_REPO_ROOT, "deploy", "etc"))
    import validate_archive
    path = str(tmp_path / "good.tar")
    _write_tar(path, _safe_members())
    assert validate_archive.validate(path, str(tmp_path / "dest")) == 0


# -- N18 evidence --------------------------------------------------------------------

def test_n18_evidence_references_sha_or_placeholder():
    import re as _re
    path = os.path.join(_REPO_ROOT, "docs", "EVIDENCE.md")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert "NOT EXECUTED" in text
    assert "PENDING:final-sha" in text or \
        bool(_re.search(r"\b[0-9a-f]{40}\b", text))
    assert "test_focused_corrective.py" in text
