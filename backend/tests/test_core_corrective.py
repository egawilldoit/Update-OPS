"""Core corrective regression tests (R01-R36, main-agent owned).

Offline and behavior-oriented; mocks stay at external boundaries
(subprocess/system bus). NOT EXECUTED in this phase — write-only per
instruction. Covers what prior suites do not: executor, sanitizer v2,
unit-state model, transactional transitions, attempt lifecycle, probe
queue mechanics, plan immutability, migration runner, secrets parsing,
reconcile decisions, admission ordering.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import types
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
from backend.app import executor as executor_lib
from backend.app import inventory as inventory_lib
from backend.app import jobs as jobs_lib
from backend.app import owner_probes as probes_lib
from backend.app import plans as plans_lib
from backend.app import reconcile_core as rc_lib
from backend.app import sanitize as sanitize_lib
from backend.app import tx as tx_lib
from backend.app import units as units_lib
from backend.app.admission import admit as admit_lib

import support as support_lib


def _fresh_db(tmp_path, name="s.db"):
    conn = db_lib.connect(str(tmp_path / name))
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.commit()
    return conn


def _plan_row(tool_id="hermes", subject="o@x.invalid", fp="fp-1",
              target="9.9.9"):
    # Build-only v2 row with live identities (F01: the production hash
    # path; callers INSERT it themselves).
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    cfg, rel, envfp = support_lib.live_identities()
    return plans_lib.build_plan_row(
        tool_id=tool_id, subject=subject,
        install_identity="display-identity-%s" % tool_id,
        fingerprint=fp, target=target, target_mode="exact",
        channel="test-channel", services=[], launch={}, state_homes=[],
        backup_scope={}, backup_policy={}, required_probes=[],
        required_checks=["smoke"], budgets={}, space_fs={},
        steps=["preflight", "backup", "updating", "verifying"],
        deadlines={"preflight": 120, "backup": 600, "updating": 1800,
                   "verifying": 300},
        restart_impact="none", restart_detail="", activity_state="idle",
        activity_ts=now.isoformat(), activity_evidence="evidence-idle",
        required_space_bytes=1024, config_hash=cfg, release_path=rel,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=300)).isoformat(), artifact={},
        env_fingerprint=envfp)


# -- executor ------------------------------------------------------------

def test_executor_true_exit_and_streaming():
    res = executor_lib.run_stream(["/bin/echo", "hi"], timeout_s=10)
    assert res.exit_code == 0 and res.timed_out is False
    assert b"hi" in res.stdout_tail


def test_executor_timeout_kills_and_reports():
    res = executor_lib.run_stream(["/bin/sleep", "30"], timeout_s=2)
    assert res.timed_out is True
    assert res.exit_code != 0
    assert res.duration_s < 25


def test_executor_cancel_event():
    ev = threading.Event()
    ev.set()
    with pytest.raises(executor_lib.Cancelled):
        executor_lib.run_stream(["/bin/echo", "x"], timeout_s=10,
                                cancel_event=ev)


def test_executor_rejects_relative_and_empty():
    with pytest.raises(ValueError):
        executor_lib.run_stream(["echo", "x"], timeout_s=5)
    with pytest.raises(ValueError):
        executor_lib.run_stream([], timeout_s=5)


def test_executor_scope_missing_fails_closed():
    import unittest.mock as _mock
    with _mock.patch("os.path.isfile", return_value=False):
        res = executor_lib.run_stream(["/bin/echo", "x"], timeout_s=5,
                                      scope_unit="ega-test.scope")
    assert res.exit_code == 127
    assert res.timed_out is False


def test_executor_nonzero_exit_preserved():
    res = executor_lib.run_stream(["/bin/sh", "-c", "exit 4"],
                                  timeout_s=10)
    assert res.exit_code == 4 and res.timed_out is False


# -- sanitizer -----------------------------------------------------------

def test_sanitizer_pem_split_across_feeds():
    stream = sanitize_lib.SanitizingStream(())
    out1 = stream.feed(b"start\n-----BEGIN RSA PRIVATE KEY-----\nAAA\n")
    assert out1 == ["start"]
    out2 = stream.feed(b"BBB\n-----END RSA PRIVATE KEY-----\nend\n")
    blob = "\n".join(out2)
    assert sanitize_lib.SUPPRESSED_MARKER in blob
    assert "AAA" not in blob and "BBB" not in blob


def test_sanitizer_tick_never_finalizes_block():
    stream = sanitize_lib.SanitizingStream(())
    assert stream.feed(b"a\n-----BEGIN RSA PRIVATE KEY-----\nAAA\n") == ["a"]
    assert stream.flush_tick() == []
    tail = stream.flush_final()
    blob = "\n".join(tail)
    assert "AAA" not in blob
    assert "[sensitive block suppressed]" in blob


def test_sanitizer_oversized_block_suppressed():
    stream = sanitize_lib.SanitizingStream(())
    big = b"-----BEGIN RSA PRIVATE KEY-----\n" + b"A" * 70000 + b"\n"
    out = stream.feed(big)
    assert not any("AAAA" in line for line in out)


def test_sanitizer_secrets_before_truncation():
    secret = "s3cr3t-value-xyz"
    stream = sanitize_lib.SanitizingStream((secret,), max_line=16)
    out = stream.feed(("prefix-%s-suffix\n" % secret).encode("utf-8"))
    blob = "\n".join(out)
    # Redaction precedes truncation: the secret never appears, the
    # replacement visibly starts, then the line cap marker applies.
    assert secret not in blob
    assert sanitize_lib.REPLACEMENT[:10] in blob
    assert "[truncated-line]" in blob


def test_sanitizer_invalid_utf8_fails_closed():
    stream = sanitize_lib.SanitizingStream(())
    with pytest.raises(sanitize_lib.SanitizerError):
        stream.feed(b"\xff\xfe invalid \xff\n")


def test_parse_secrets_file_formats(tmp_path):
    path = str(tmp_path / "s.env")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# comment\nexport A=alpha-1234\nB='bravo 5678'\n"
                 "C=\"charlie-90\"\nbare-secret-value\nab\n\n")
    values = sanitize_lib.parse_secrets_file(path)
    assert "alpha-1234" in values
    assert "bravo 5678" in values
    assert "charlie-90" in values
    assert "bare-secret-value" in values
    assert "ab" not in values
    assert sanitize_lib.parse_secrets_file(str(tmp_path / "nope")) == ()


def test_sanitize_json_recursive():
    secret = "j-secret-999"
    obj = {"a": "x %s y" % secret, "n": [{"b": secret}], "c": 1}
    clean = sanitize_lib.sanitize_json(obj, (secret,))
    assert secret not in json.dumps(clean)
    assert clean["c"] == 1


# -- units ---------------------------------------------------------------

class _Completed(object):
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _show_output(active, sub, pid=0, cgroup="", unit_id="u.service"):
    return ("Id=%s\nActiveState=%s\nSubState=%s\nMainPID=%d\n"
            "ControlGroup=%s\nFragmentPath=/tmp/x\nResult=success\n"
            "ExecMainStatus=0\n" % (unit_id, active, sub, pid, cgroup))


def test_units_live_mapping(monkeypatch):
    import subprocess as _sp
    monkeypatch.setattr(
        _sp, "run",
        lambda *a, **k: _Completed(
            0, _show_output("active", "running", 1234, "/x")))
    info = units_lib.query_unit("u.service")
    assert info["state"] == "live"
    assert info["identity_ok"] is True


def test_units_starting_stopping_mapping(monkeypatch):
    import subprocess as _sp
    monkeypatch.setattr(
        _sp, "run",
        lambda *a, **k: _Completed(0, _show_output("activating", "start")))
    assert units_lib.query_unit("u.service")["state"] == "starting"
    monkeypatch.setattr(
        _sp, "run",
        lambda *a, **k: _Completed(0, _show_output("deactivating", "stop")))
    assert units_lib.query_unit("u.service")["state"] == "stopping"


def test_units_unknown_on_bus_failure(monkeypatch):
    import subprocess as _sp

    def _boom(*a, **k):
        raise OSError("bus down")
    monkeypatch.setattr(_sp, "run", _boom)
    assert units_lib.query_unit("u.service")["state"] == "unknown"


def test_units_confirmed_stopped_needs_dead_pid_and_empty_cgroup(
        monkeypatch):
    import subprocess as _sp
    monkeypatch.setattr(
        _sp, "run",
        lambda *a, **k: _Completed(
            0, _show_output("inactive", "dead", 0, "")))
    import backend.app.units as _units
    monkeypatch.setattr(_units, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(_units, "_cgroup_empty", lambda cg: True)
    assert units_lib.query_unit("u.service")["state"] == "confirmed_stopped"
    # Live MainPID contradicts the manager -> unknown, never stopped.
    monkeypatch.setattr(_units, "_pid_alive", lambda pid: True)
    assert units_lib.query_unit("u.service")["state"] == "unknown"


# -- transactions ----------------------------------------------------------

def _v2row(conn, fp="fp", subject="s"):
    # type: (...) -> dict
    return support_lib.v2_plan_row(
        conn, uuid.uuid4().hex, subject=subject, fingerprint=fp)


def _admit(conn, subject, key, plan_id, ack, fp):
    # type: (...) -> str
    support_lib.test_release_root()
    job_id, created, err = admit_lib(conn, subject, key, plan_id, ack,
                                     fp, True, False)
    assert err == "" and created and job_id, err
    return job_id


def test_tx_guard_and_atomic_terminal(tmp_path):
    conn = _fresh_db(tmp_path)
    row = _v2row(conn)
    jid = _admit(conn, "s", "k-tx-1", row["id"], False, "fp")
    # Guard mismatch writes nothing.
    with pytest.raises(tx_lib.TxGuardError):
        tx_lib.transition_tx(conn, jid, "updating", expect_states=["bogus"])
    assert conn.execute(
        "SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()["state"] \
        == "accepted"
    # Atomic terminal: state + recovery + versions + event together.
    out = tx_lib.transition_tx(
        conn, jid, "failed", step="updating",
        expect_states=["accepted"],
        update={"error_code": "install_failed",
                "installer_exit": 4, "install_outcome": "install_failed",
                "after_version": "1.0.1", "recovery_required": 1,
                "unresolved": 0},
        event="failed", event_detail="boom",
        checks=[{"name": "c", "result": "fail", "mandatory": True,
                 "summary": "bad"}],
        tool_id="hermes")
    assert out["state"] == "failed"
    assert out["recovery_required"] == 1
    assert out["installer_exit"] == 4
    assert out["finished_at"]
    ev = conn.execute(
        "SELECT * FROM events WHERE job_id=? AND event_type='failed'",
        (jid,)).fetchall()
    assert len(ev) == 1
    conn.close()


# -- attempts / claims -----------------------------------------------------

def test_claim_deadline_enforced(tmp_path):
    conn = _fresh_db(tmp_path)
    row = _v2row(conn)
    jid = _admit(conn, "s", "k-dl-1", row["id"], False, "fp")
    # Force the deadline into the past: claim must refuse.
    conn.execute("UPDATE jobs SET claim_deadline='2000-01-01T00:00:00+00:00'"
                 " WHERE id=?", (jid,))
    conn.commit()
    assert jobs_lib.claim_with_nonce(conn, jid, "n") is False
    # Safe expiry only touches never-claimed, past-deadline accepted rows.
    assert jobs_lib.expire_stale_accepted(conn) == 1
    assert conn.execute(
        "SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()["state"] \
        == "blocked"
    conn.close()


def test_consume_attempt_single_use(tmp_path):
    conn = _fresh_db(tmp_path)
    row = _v2row(conn)
    jid = _admit(conn, "s", "k-ca-1", row["id"], False, "fp")
    assert jobs_lib.claim_with_nonce(conn, jid, "nonce-1") is True
    assert jobs_lib.consume_attempt(conn, jid, "nonce-1") is True
    assert jobs_lib.consume_attempt(conn, jid, "nonce-1") is False
    assert jobs_lib.consume_attempt(conn, jid, "other") is False
    conn.close()


def test_find_replay_precedes_admission(tmp_path):
    conn = _fresh_db(tmp_path)
    row = _v2row(conn)
    jid = _admit(conn, "s", "k-replay-1", row["id"], True, "fp")
    digest = jobs_lib.request_hash(row["id"], True)
    found = jobs_lib.find_replay(conn, "s", "k-replay-1")
    assert found is not None and found["id"] == jid
    assert found["request_hash"] == digest
    assert jobs_lib.find_replay(conn, "s", "k-nope") is None
    conn.close()


# -- probe queue -----------------------------------------------------------

def test_probe_queue_roundtrip(tmp_path):
    conn = _fresh_db(tmp_path)
    rid = probes_lib.enqueue_probe(conn, "s", "hermes", "inspect")
    assert rid
    with pytest.raises(ValueError):
        probes_lib.enqueue_probe(conn, "s", "hermes", "rm -rf")
    claimed = probes_lib.claim_probe(conn, "tester-1")
    assert claimed is not None and claimed["id"] == rid
    assert probes_lib.claim_probe(conn, "tester-2") is None
    probes_lib.finish_probe(conn, rid, "ok", {"version": "1.0"})
    status, payload = probes_lib.await_probe(conn, rid, timeout_s=2)
    assert status == "ok" and payload.get("version") == "1.0"
    conn.close()


# -- plans -----------------------------------------------------------------

def test_plan_hash_tamper_rejected(tmp_path):
    conn = _fresh_db(tmp_path)
    row = _plan_row()
    conn.execute("BEGIN IMMEDIATE")
    plans_lib.insert_plan(conn, row)
    conn.commit()
    loaded = plans_lib.load_plan(conn, row["id"])
    assert loaded["target"] == "9.9.9"
    conn.execute("UPDATE plans SET target=? WHERE id=?",
                 ("9.9.10", row["id"]))
    conn.commit()
    with pytest.raises(plans_lib.PlanInvalid):
        plans_lib.load_plan(conn, row["id"])
    with pytest.raises(plans_lib.PlanNotFound):
        plans_lib.load_plan(conn, str(uuid.uuid4()))
    conn.close()


def test_config_identity_stable_and_sensitive(tmp_path):
    h1 = inventory_lib.config_identity()
    h2 = inventory_lib.config_identity()
    assert h1 and h1 == h2
    assert inventory_lib.get_tool_inventory("hermes") == {}
    assert inventory_lib.get_tool_inventory("nope") == {}


# -- migrations --------------------------------------------------------------

def test_migrations_dir_resolves_to_backend_migrations():
    """D2: every migration file in MIGRATIONS must exist on disk at the
    resolved path (derived from db.py location, independent of CWD).
    A wrong parent level broke all fresh migrations silently."""
    path = db_lib._migration_path("001_init.sql")
    assert path.endswith(
        os.path.join("backend", "migrations", "001_init.sql"))
    assert os.path.isfile(path)
    for _version, filename in db_lib.MIGRATIONS:
        resolved = db_lib._migration_path(filename)
        assert os.path.isfile(resolved), filename


def test_split_statements_ignores_comment_semicolons():
    """A1/A2: semicolons inside full-line -- comments (even several)
    never produce fake statements."""
    sql = ("-- harmless comment; still comment\n"
           "ALTER TABLE tools ADD COLUMN x TEXT DEFAULT '';\n"
           "-- another; comment; with; semicolons\n")
    statements = db_lib._split_statements(sql)
    assert len(statements) == 1
    assert statements[0].startswith("ALTER TABLE")
    assert "harmless comment" not in statements[0]


def test_split_statements_keeps_real_splitting():
    """A3: real statements still split; blank lines, multiline
    statements, and a missing trailing semicolon are preserved."""
    sql = ("-- lead comment\n"
           "\n"
           "ALTER TABLE a ADD COLUMN x TEXT\n"
           "  DEFAULT '';\n"
           "ALTER TABLE b ADD COLUMN y INTEGER DEFAULT 0")
    statements = db_lib._split_statements(sql)
    assert len(statements) == 2
    assert statements[0].startswith("ALTER TABLE a")
    assert "DEFAULT ''" in statements[0]
    assert statements[1].startswith("ALTER TABLE b")


def test_migration_002_parses_to_intended_alters():
    """A4: the actual 002 file parses to exactly its two ALTERs."""
    path = db_lib._migration_path("002_execution_hardening.sql")
    with open(path, "r", encoding="utf-8") as fh:
        statements = db_lib._split_statements(fh.read())
    assert len(statements) == 2
    assert all(s.startswith("ALTER TABLE") for s in statements)
    assert "fingerprint" in statements[0]
    assert "dispatch_nonce" in statements[1]


def test_migrate_fresh_rerun_and_newer_rejected(tmp_path):
    path = str(tmp_path / "m.db")
    conn = db_lib.connect(path)
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    assert db_lib.validate_schema(conn) == db_lib.CODE_VERSION
    # Rerun is idempotent.
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.close()
    # Newer-than-code schema is rejected, never served.
    conn2 = db_lib.connect(path)
    conn2.execute(
        "INSERT INTO schema_meta(key,value) VALUES('version','99')"
        " ON CONFLICT(key) DO UPDATE SET value='99'")
    conn2.commit()
    with pytest.raises(db_lib.SchemaError):
        db_lib.validate_schema(conn2)
    with pytest.raises(db_lib.SchemaError):
        db_lib.migrate(conn2)
    conn2.close()


def test_migrate_001_to_current_upgrade(tmp_path):
    src = os.path.join(_REPO_ROOT, "backend", "migrations",
                       "001_init.sql")
    path = str(tmp_path / "old.db")
    conn = db_lib.connect(path)
    with open(src, "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())
    conn.commit()
    # Legacy v1 base (no ledger): migrate infers + applies 002/003/004.
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    cols = {r["name"] for r in
            conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "attempt_claimed" in cols and "final_log_seq" in cols
    assert "plan_hash" in {r["name"] for r in
                           conn.execute("PRAGMA table_info(plans)").fetchall()}
    conn.close()


# -- reconcile decisions -----------------------------------------------------

def test_decide_matrix():
    live = {"state": "live"}
    assert rc_lib.decide({}, live, None, [])[0] == "live"
    assert rc_lib.decide(
        {}, {"state": "starting"}, None, [])[0] == "starting"
    assert rc_lib.decide(
        {}, {"state": "stopping"}, None, [])[0] == "stopping"
    assert rc_lib.decide(
        {}, {"state": "unknown"}, None, [])[0] == "keep-unknown"
    stopped = {"state": "confirmed_stopped"}
    quiet = {"quiescent": True, "evidence": [], "reason": ""}
    # G02: stopped unit alone never suffices — delegated proof missing
    # holds even with a perfect receipt.
    assert rc_lib.decide({}, stopped, None, [])[0] == "keep-unknown"
    assert rc_lib.decide(
        {}, stopped, {"_valid": True}, [])[0] == "keep-unknown"
    assert rc_lib.decide({}, stopped, None, [], quiet)[0] == \
        "mark-interrupted"
    assert rc_lib.decide(
        {}, stopped, {"_valid": True}, [], quiet)[0] == "apply-receipt"
    assert rc_lib.decide(
        {}, stopped, None, [{"pid": 1}])[0] == "keep-unknown"
    # Surviving processes override even receipt + delegated proof.
    assert rc_lib.decide(
        {}, stopped, {"_valid": True}, [{"pid": 1}], quiet)[0] == \
        "keep-unknown"
    assert rc_lib.canonical_unit("ab-cd").endswith(
        "ega-update-job-abcd.service")
    assert rc_lib.job_processes("zz-no-such-token-zz", "") == []
    assert rc_lib.recovery_for("updating", False, False) is True
    assert rc_lib.recovery_for("preflight", False, False) is False
    assert rc_lib.recovery_for("preflight", False, True) is True
