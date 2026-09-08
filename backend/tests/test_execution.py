"""Execution-hardening tests for Update-OPS V1 (offline, no execution).

Covers the corrective pass without running tests/builds/servers/probes:
- dispatch nonce claim + runner replay-refusal (H-01)
- receipt build/validate/apply/idempotency (P0-04)
- dispatcher heartbeat fresh/stale/missing/unparseable (H-03, jobs contract)
- exact-target mismatch guard (P0-06)
- adapter timeout funnels into the runner hard-timeout path + recovery flag
- multiline PEM redaction across StreamRedactor feed boundaries (P0-08)
- JobLog seq starts at 1 (H-06)
- evidence-failure stickiness -> interrupted/storage_failure (P0-08)
- dispatcher reconcile-decision matrix: live vs dead+receipt vs dead bare
- shared-contract spot checks: timed_out field, margin constant, LogPage,
  ReceiptModel, migration 002 ALTERs

Style: plain pytest functions with tmp_path + monkeypatch. Stdlib only
apart from the backend imports. Python 3.10 compatible. Nothing here
spawns subprocesses, servers, or probes: systemctl/process-tree touchpoints
are monkeypatched, /proc scans are stubbed where used.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.app import db as db_lib
from backend.app import jobs as jobs_lib
from backend.app import receipts as receipts_lib
from backend.app import redaction as redaction_lib
from backend.app import schemas as schemas_lib
from backend.app.adapters import base as base_lib
from backend.app.adapters import registry as registry_lib
from backend.app.worker import dispatch as dispatch_lib
from backend.app.worker import runner as runner_lib


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_db(path):
    # type: (str) -> sqlite3.Connection
    conn = db_lib.connect(path)
    db_lib.migrate(conn)
    conn.commit()
    # Migration 002 columns must exist on fresh DDL (fingerprint/nonce).
    cols_jobs = {r["name"] for r in
                 conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "dispatch_nonce" in cols_jobs
    cols_tools = {r["name"] for r in
                  conn.execute("PRAGMA table_info(tools)").fetchall()}
    assert "fingerprint" in cols_tools
    return conn


def _insert_plan(conn, plan_id, tool_id="hermes", fingerprint="fp-test-1",
                 target="9.9.9", target_mode="exact", expires_future=True):
    # type: (...) -> None
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=600) if expires_future else \
        now - timedelta(seconds=600)
    conn.execute("INSERT OR IGNORE INTO tools(id) VALUES(?)", (tool_id,))
    conn.execute(
        "INSERT INTO plans(id,tool_id,subject,created_at,expires_at,"
        "fingerprint,target,target_mode,channel,services,backup_scope,"
        "activity_state,activity_evidence,used_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (plan_id, tool_id, "owner@example.invalid", now.isoformat(),
         exp.isoformat(), fingerprint, target, target_mode, "test-channel",
         json.dumps([]), json.dumps({}), "idle", "idle evidence", ""))
    conn.commit()


def _reserve(conn, tool_id="hermes", plan_id=None, subject="owner@example.invalid",
             idem_key=None, ack=False):
    # type: (...) -> str
    if plan_id is None:
        plan_id = str(uuid.uuid4())
        _insert_plan(conn, plan_id, tool_id=tool_id)
    if idem_key is None:
        idem_key = "k-%s" % uuid.uuid4().hex[:8]
    conn.execute("BEGIN IMMEDIATE")
    job_id, created, err = jobs_lib.reserve_job(
        conn, tool_id, plan_id, subject, idem_key, ack)
    assert err == "" and created and job_id
    conn.commit()
    return job_id


def _job_row(conn, job_id):
    # type: (...) -> dict
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row is not None
    return dict(row)


# ---------------------------------------------------------------------------
# shared-contract spot checks (no execution)
# ---------------------------------------------------------------------------

def test_execute_result_has_timed_out_field():
    r = base_lib.ExecuteResult(tool="hermes")
    assert r.timed_out is False
    r2 = base_lib.ExecuteResult(tool="hermes", timed_out=True,
                                error_code="timeout")
    assert r2.timed_out is True


def test_mutation_timeout_margin_constant():
    assert registry_lib.MUTATION_TIMEOUT_MARGIN_S == 120


def test_log_page_has_more_defaults_false():
    page = schemas_lib.LogPage()
    assert page.has_more is False
    assert page.truncated is False
    page2 = schemas_lib.LogPage(records=[], next_after=4, truncated=False,
                                has_more=True)
    assert page2.has_more is True and page2.truncated is False


def test_receipt_model_exists_and_matches():
    assert hasattr(schemas_lib, "ReceiptModel")
    m = schemas_lib.ReceiptModel(job_id="j", tool_id="hermes",
                                 state="succeeded", after_version="1.2.3",
                                 ts="2026-09-08T00:00:00+00:00")
    assert m.job_id == "j" and m.schema_version == 1
    assert receipts_lib.RECEIPT_SCHEMA_VERSION == 1


def test_migration_002_alters_only():
    path = os.path.join(_REPO_ROOT, "backend", "migrations",
                        "002_execution_hardening.sql")
    with open(path, "r", encoding="utf-8") as fh:
        sql = fh.read()
    assert "ADD COLUMN fingerprint" in sql
    assert "ADD COLUMN dispatch_nonce" in sql
    upper = sql.upper()
    assert "CREATE TABLE" not in upper and "CREATE INDEX" not in upper


def test_canonical_launch_argv():
    job_id = "12345678-abcd-ef00-1234-56789abcdef0"
    unit = "ega-update-job-12345678.service"
    cmd = dispatch_lib._canonical_cmd(job_id, "nonceABC", unit)
    assert cmd[0] == "systemd-run" and "--user" in cmd
    assert "--collect" in cmd
    assert "--unit=%s" % unit in cmd
    assert "--working-directory=/opt/ega-update/current" in cmd
    assert "--setenv=EGA_CONFIG_FILE=/etc/ega-update/config.json" in cmd
    assert "--setenv=EGA_ATTEMPT_NONCE=nonceABC" in cmd
    assert "--property=KillMode=control-group" in cmd
    assert "--property=Restart=no" in cmd
    assert cmd[-4:] == [sys.executable, "-m",
                        "backend.app.worker.runner", job_id, "nonceABC"] \
        or cmd[-3:] == ["backend.app.worker.runner", job_id, "nonceABC"]
    assert not any("sudo" in part for part in cmd)
    # No system-manager fallback: every systemctl touch uses --user.
    import inspect
    src = inspect.getsource(dispatch_lib._unit_active)
    assert "--user" in src


# ---------------------------------------------------------------------------
# 1. nonce claim / replay-refusal
# ---------------------------------------------------------------------------

def test_claim_with_nonce_atomic_and_single_use(tmp_path):
    conn = _make_db(str(tmp_path / "state.db"))
    job_id = _reserve(conn)
    nonce = "test-nonce-aaa"
    assert jobs_lib.claim_with_nonce(conn, job_id, nonce) is True
    row = _job_row(conn, job_id)
    assert row["dispatch_nonce"] == nonce and row["state"] == "preflight"
    # Second claim with a different nonce fails (already preflight).
    assert jobs_lib.claim_with_nonce(conn, job_id, "other-nonce") is False
    # Same-nonce retry also fails once claimed (state moved on).
    assert jobs_lib.claim_with_nonce(conn, job_id, nonce) is False
    # Empty nonce never claims (terminate the first job to free the slot).
    jobs_lib.transition(conn, job_id, "succeeded", step="succeeded",
                        exit_code=0, after_version="1.0.0")
    conn.commit()
    job2 = _reserve(conn, idem_key="k-second")
    assert jobs_lib.claim_with_nonce(conn, job2, "") is False
    conn.close()


def test_runner_replay_refusal_wrong_and_empty_nonce(tmp_path, monkeypatch):
    from backend.app.config import settings as settings_lib
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(settings_lib, "db_path", db_path)
    conn = _make_db(db_path)
    job_id = _reserve(conn)
    good = "good-nonce-123"
    assert jobs_lib.claim_with_nonce(conn, job_id, good) is True
    conn.close()
    # Wrong nonce: exit 6, tool untouched, DB row unchanged.
    touched = {"adapter": False}

    class _NopeAdapter(object):
        enabled = True

        def plan(self):
            touched["adapter"] = True
            raise AssertionError("tool touched on replay")

    monkeypatch.setattr(registry_lib, "get_adapter",
                        lambda tool_id: _NopeAdapter())
    bad = runner_lib.Runner(job_id, "wrong-nonce")
    rc = bad.run()
    assert rc == runner_lib.EXIT_INTERRUPTED == 6
    assert touched["adapter"] is False
    conn2 = db_lib.connect(db_path)
    row = _job_row(conn2, job_id)
    assert row["dispatch_nonce"] == good and row["state"] == "preflight"
    conn2.close()
    # Empty nonce likewise refused without tool touch.
    empty = runner_lib.Runner(job_id, "")
    assert empty.run() == 6
    assert touched["adapter"] is False


def test_runner_verify_nonce_unit():
    r = runner_lib.Runner("job-12345678", "n1")
    r.job = {"dispatch_nonce": "n1", "state": "preflight"}
    ok, _reason = r._verify_nonce()
    assert ok is True
    r2 = runner_lib.Runner("job-12345678", "n2")
    r2.job = {"dispatch_nonce": "n1", "state": "preflight"}
    assert r2._verify_nonce()[0] is False
    r3 = runner_lib.Runner("job-12345678", "n1")
    r3.job = {"dispatch_nonce": "n1", "state": "updating"}
    assert r3._verify_nonce()[0] is False
    r4 = runner_lib.Runner("job-12345678", "")
    r4.job = {"dispatch_nonce": "n1", "state": "preflight"}
    assert r4._verify_nonce()[0] is False


# ---------------------------------------------------------------------------
# 2. receipts: validate / apply / idempotency
# ---------------------------------------------------------------------------

def _good_receipt(job_id="job-1", tool_id="hermes", state="succeeded",
                  after="2.0.0"):
    return receipts_lib.build_receipt(
        job_id, tool_id, state, "1.0.0", after, 0, "", [
            {"name": "smoke", "result": "pass", "mandatory": True,
             "summary": "ok"},
            {"name": "optional", "result": "unknown", "mandatory": False,
             "summary": ""},
        ], "2026-09-08T00:00:00+00:00")


def test_receipt_validate_matrix():
    ok, reason = receipts_lib.validate_receipt(_good_receipt())
    assert ok and reason == ""
    bad_schema = _good_receipt()
    bad_schema["schema_version"] = 999
    assert receipts_lib.validate_receipt(bad_schema)[0] is False
    no_job = _good_receipt()
    no_job["job_id"] = ""
    assert receipts_lib.validate_receipt(no_job)[0] is False
    no_tool = _good_receipt()
    no_tool["tool_id"] = ""
    no_tool["tool"] = ""
    assert receipts_lib.validate_receipt(no_tool)[0] is False
    bad_state = _good_receipt()
    bad_state["state"] = "updating"
    assert receipts_lib.validate_receipt(bad_state)[0] is False
    no_after = _good_receipt()
    no_after["after_version"] = ""
    assert receipts_lib.validate_receipt(no_after)[0] is False
    bad_check = _good_receipt()
    bad_check["checks"] = [{"name": "m", "result": "bogus",
                            "mandatory": True, "summary": ""}]
    assert receipts_lib.validate_receipt(bad_check)[0] is False
    bad_ts = _good_receipt()
    bad_ts["ts"] = "not-a-time"
    bad_ts["finished_at"] = "not-a-time"
    assert receipts_lib.validate_receipt(bad_ts)[0] is False
    assert receipts_lib.validate_receipt("nope")[0] is False


def test_receipt_apply_and_idempotency(tmp_path):
    conn = _make_db(str(tmp_path / "state.db"))
    job_id = _reserve(conn)
    assert jobs_lib.claim_with_nonce(conn, job_id, "n-1") is True
    data = _good_receipt(job_id=job_id)
    state = receipts_lib.apply_receipt(conn, data)
    assert state == "succeeded"
    row = _job_row(conn, job_id)
    assert row["state"] == "succeeded" and row["after_version"] == "2.0.0"
    checks = conn.execute("SELECT * FROM checks WHERE job_id=?",
                          (job_id,)).fetchall()
    assert len(checks) == 2
    events = conn.execute(
        "SELECT * FROM events WHERE job_id=? AND event_type='receipt_applied'",
        (job_id,)).fetchall()
    assert len(events) == 1
    # Idempotent re-apply writes nothing new.
    assert receipts_lib.apply_receipt(conn, data) == "succeeded"
    checks2 = conn.execute("SELECT * FROM checks WHERE job_id=?",
                           (job_id,)).fetchall()
    events2 = conn.execute(
        "SELECT * FROM events WHERE job_id=? AND event_type='receipt_applied'",
        (job_id,)).fetchall()
    assert len(checks2) == 2 and len(events2) == 1
    # Invalid receipts raise instead of writing.
    with pytest.raises(ValueError):
        receipts_lib.apply_receipt(conn, {"schema_version": 999})
    with pytest.raises(ValueError):
        receipts_lib.apply_receipt(conn, _good_receipt(job_id="missing"))
    conn.close()


def test_receipt_builder_redacts_strings(monkeypatch):
    secret = "builder-secret-xyz-123"
    monkeypatch.setattr(receipts_lib, "_known_secrets", lambda: (secret,))
    data = receipts_lib.build_receipt(
        "job-1", "hermes", "failed", "1.0.0", "1.0.1", 4, "install_failed",
        [{"name": "n", "result": "fail", "mandatory": True,
          "summary": "leak %s here" % secret}],
        "2026-09-08T00:00:00+00:00", error_detail="boom %s" % secret)
    assert secret not in json.dumps(data)
    assert data["checks"][0]["summary"].count("***REDACTED***") >= 1


# ---------------------------------------------------------------------------
# 3. dispatcher heartbeat fresh / stale / missing / unparseable
# ---------------------------------------------------------------------------

def test_heartbeat_fresh_stale_missing_unparseable(tmp_path):
    from backend.app.schemas import utcnow_iso
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    assert jobs_lib.read_dispatcher_heartbeat(state_dir) == {}
    hb = os.path.join(state_dir, "dispatcher.heartbeat")
    with open(hb, "w", encoding="utf-8") as fh:
        json.dump({"ts": utcnow_iso(), "pid": 4242}, fh)
    fresh = jobs_lib.read_dispatcher_heartbeat(state_dir)
    assert fresh and fresh.get("pid") == 4242
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    with open(hb, "w", encoding="utf-8") as fh:
        json.dump({"ts": old, "pid": 1}, fh)
    assert jobs_lib.read_dispatcher_heartbeat(state_dir, max_age_s=20) == {}
    with open(hb, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert jobs_lib.read_dispatcher_heartbeat(state_dir) == {}
    with open(hb, "w", encoding="utf-8") as fh:
        json.dump({"ts": "garbage", "pid": 1}, fh)
    assert jobs_lib.read_dispatcher_heartbeat(state_dir) == {}


def test_heartbeat_write_then_read_roundtrip(tmp_path, monkeypatch):
    from backend.app.config import settings as settings_lib
    state_dir = str(tmp_path / "state")
    monkeypatch.setattr(settings_lib, "state_dir", state_dir)
    dispatch_lib._write_dispatcher_heartbeat()
    data = jobs_lib.read_dispatcher_heartbeat(state_dir)
    assert data and data.get("pid") == os.getpid()


# ---------------------------------------------------------------------------
# 4. exact-target mismatch
# ---------------------------------------------------------------------------

def test_exact_target_mismatch_helper():
    assert runner_lib._exact_target_mismatch("exact", "2.0.0", "2.0.0") is False
    assert runner_lib._exact_target_mismatch("exact", "2.0.0", "2.0.1") is True
    assert runner_lib._exact_target_mismatch("exact", "2.0.0", "") is True
    assert runner_lib._exact_target_mismatch("native_latest", "x", "y") is False
    assert runner_lib._exact_target_mismatch("", "x", "y") is False


# ---------------------------------------------------------------------------
# 5. timeout -> recovery_required
# ---------------------------------------------------------------------------

def test_is_timeout_result_matrix():
    assert runner_lib._is_timeout_result(
        base_lib.ExecuteResult(tool="h", timed_out=True)) is True
    assert runner_lib._is_timeout_result(
        base_lib.ExecuteResult(tool="h", error_code="timeout")) is True
    assert runner_lib._is_timeout_result(
        base_lib.ExecuteResult(tool="h", error_code="")) is False
    assert runner_lib._is_timeout_result(
        {"error_code": "timeout"}) is True
    assert runner_lib._is_timeout_result(None) is False


def test_adapter_timeout_funnels_to_hard_timeout(monkeypatch):
    calls = {"term": 0, "systemd": 0}

    def _fake_term(grace_s=30):
        calls["term"] += 1

    def _fake_systemd(unit):
        calls["systemd"] += 1

    monkeypatch.setattr(runner_lib, "terminate_tree", _fake_term)
    monkeypatch.setattr(runner_lib, "_systemd_terminate_unit", _fake_systemd)
    monkeypatch.setattr(runner_lib, "_no_live_descendants", lambda: True)
    monkeypatch.setattr(runner_lib, "_job_processes_alive", lambda j: [])

    class _TimeoutAdapter(object):
        enabled = True

        def execute(self, plan, job_id, activity_ack=False):
            return base_lib.ExecuteResult(
                tool="hermes", exit_code=4, state="install_failed",
                error_code="timeout", error_detail="timed out")

        def verify(self):
            return base_lib.VerifyResult(tool="hermes", version="",
                                         checks=[], passed=False)

    r = runner_lib.Runner("job-timeout-1", "n")
    r.job = {"ack": ""}
    r.adapter = _TimeoutAdapter()
    r.log = None
    out = r._do_execute(types.SimpleNamespace(), 30.0)
    assert out is None and r._timed_out is True
    assert calls["term"] >= 1 and calls["systemd"] >= 1


def test_finish_timeout_sets_recovery(tmp_path, monkeypatch):
    from backend.app.config import settings as settings_lib
    log_dir = str(tmp_path / "logs")
    monkeypatch.setattr(settings_lib, "log_dir", log_dir)
    conn = _make_db(str(tmp_path / "state.db"))
    job_id = _reserve(conn)
    assert jobs_lib.claim_with_nonce(conn, job_id, "n-time") is True
    r = runner_lib.Runner(job_id, "n-time")
    r.conn = conn
    r.job = _job_row(conn, job_id)
    r.tool_id = "hermes"
    r.before_version = "1.0.0"
    r.after_version = ""
    r.checks = []
    r.log = None
    rc = r._finish("failed", runner_lib.EXIT_INSTALL_FAILED, "timeout",
                   "hard timeout during update", recovery_required=True)
    assert rc == runner_lib.EXIT_INSTALL_FAILED
    row = _job_row(conn, job_id)
    assert row["state"] == "failed" and int(row["recovery_required"]) == 1
    conn.close()


# ---------------------------------------------------------------------------
# 6. multiline PEM across feed boundaries
# ---------------------------------------------------------------------------

def test_pem_split_across_feeds_redacted():
    red = redaction_lib.StreamRedactor(())
    feed1 = b"start\n-----BEGIN RSA PRIVATE KEY-----\nAAAABBBBCCCC\n"
    feed2 = b"DDDDEEEEFFFF\n-----END RSA PRIVATE KEY-----\nend\n"
    out1 = red.feed(feed1)
    assert out1 == ["start"]
    assert not any("AAAABBBB" in line for line in out1)
    out2 = red.feed(feed2)
    blob = "\n".join(out2)
    assert "***REDACTED***" in blob
    assert "AAAABBBB" not in blob and "DDDDEEEE" not in blob
    assert "PRIVATE KEY" not in blob
    assert red.flush() == []


def test_pem_complete_via_flush_without_trailing_newline():
    red = redaction_lib.StreamRedactor(())
    out = red.feed(b"note\n-----BEGIN RSA PRIVATE KEY-----\nZZZZ\n"
                   b"-----END RSA PRIVATE KEY-----")
    tail = red.flush()
    blob = "\n".join(list(out) + list(tail))
    assert "***REDACTED***" in blob
    assert "ZZZZ" not in blob or "PRIVATE KEY" not in blob


# ---------------------------------------------------------------------------
# 7. seq starts at 1
# ---------------------------------------------------------------------------

def test_joblog_seq_starts_at_1(tmp_path):
    from backend.app.worker.runner import JobLog
    path = str(tmp_path / "job.jsonl")
    log = JobLog(path, 1024 * 1024, secrets=())
    log.open()
    log.emit("stdout", "hello\n")
    log.emit("stdout", "world\n")
    log.close()
    seqs = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                seqs.append(json.loads(line)["seq"])
    assert seqs and seqs[0] == 1
    assert seqs == sorted(seqs)
    # after=0 delivers everything (first seq is 1, never 0).
    assert all(s > 0 for s in seqs)
    # Reopen continues increasing (no seq reuse).
    log2 = JobLog(path, 1024 * 1024, secrets=())
    log2.open()
    log2.emit("stdout", "again\n")
    log2.close()
    seqs2 = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                seqs2.append(json.loads(line)["seq"])
    assert seqs2[-1] == seqs[-1] + 1


# ---------------------------------------------------------------------------
# 8. evidence-failure stickiness
# ---------------------------------------------------------------------------

def test_check_persistence_failure_sets_sticky_flag(tmp_path):
    conn = _make_db(str(tmp_path / "state.db"))
    job_id = _reserve(conn)
    conn.close()  # closed conn: any write raises.
    r = runner_lib.Runner(job_id, "n")
    r.conn = conn
    r.job = {"id": job_id}
    r.tool_id = "hermes"
    r.log = None
    fake = types.SimpleNamespace(
        checks=[types.SimpleNamespace(name="c", result="pass",
                                      mandatory=True, summary="s")])
    r._record_checks(fake)
    assert r._evidence_failed is True


def test_finish_maps_sticky_evidence_to_interrupted(tmp_path, monkeypatch):
    from backend.app.config import settings as settings_lib
    log_dir = str(tmp_path / "logs")
    monkeypatch.setattr(settings_lib, "log_dir", log_dir)
    conn = _make_db(str(tmp_path / "state.db"))
    job_id = _reserve(conn)
    assert jobs_lib.claim_with_nonce(conn, job_id, "n-ev") is True
    r = runner_lib.Runner(job_id, "n-ev")
    r.conn = conn
    r.job = _job_row(conn, job_id)
    r.tool_id = "hermes"
    r.before_version = "1.0.0"
    r.after_version = "2.0.0"
    r.checks = [{"name": "c", "result": "pass", "mandatory": True,
                 "summary": "ok"}]
    r.log = None
    r._evidence_failed = True
    rc = r._finish("succeeded", runner_lib.EXIT_OK, "", "")
    assert rc == runner_lib.EXIT_INTERRUPTED
    row = _job_row(conn, job_id)
    assert row["state"] == "interrupted"
    assert row["error_code"] == "storage_failure"
    conn.close()


# ---------------------------------------------------------------------------
# 9. reconcile-decision matrix
# ---------------------------------------------------------------------------

def _claimed_job(conn, state="updating", nonce="n-r"):
    job_id = _reserve(conn, idem_key="k-%s" % uuid.uuid4().hex[:8])
    assert jobs_lib.claim_with_nonce(conn, job_id, nonce) is True
    if state != "preflight":
        conn.execute("UPDATE jobs SET state=?, step=? WHERE id=?",
                     (state, state, job_id))
        conn.commit()
    return job_id


def test_reconcile_live_unit_touches_heartbeat(tmp_path, monkeypatch):
    from backend.app.config import settings as settings_lib
    monkeypatch.setattr(settings_lib, "log_dir", str(tmp_path / "logs"))
    conn = _make_db(str(tmp_path / "state.db"))
    job_id = _claimed_job(conn, state="updating")
    before = _job_row(conn, job_id)["heartbeat"]
    monkeypatch.setattr(dispatch_lib, "_unit_active", lambda unit: "active")
    acted = dispatch_lib.reconcile_claimed_jobs(conn)
    after = _job_row(conn, job_id)
    assert after["state"] == "updating"
    assert after["heartbeat"] and after["heartbeat"] >= before
    assert acted == 0
    conn.close()


def test_reconcile_dead_plus_valid_receipt_applies(tmp_path, monkeypatch):
    from backend.app.config import settings as settings_lib
    log_dir = str(tmp_path / "logs")
    os.makedirs(log_dir, exist_ok=True)
    monkeypatch.setattr(settings_lib, "log_dir", log_dir)
    conn = _make_db(str(tmp_path / "state.db"))
    job_id = _claimed_job(conn, state="updating")
    data = receipts_lib.build_receipt(
        job_id, "hermes", "succeeded", "1.0.0", "9.9.9", 0, "",
        [{"name": "smoke", "result": "pass", "mandatory": True,
          "summary": "ok"}], "2026-09-08T00:00:00+00:00")
    with open(os.path.join(log_dir, "%s.receipt.json" % job_id), "w",
              encoding="utf-8") as fh:
        json.dump(data, fh)
    monkeypatch.setattr(dispatch_lib, "_unit_active", lambda unit: "inactive")
    acted = dispatch_lib.reconcile_claimed_jobs(conn)
    row = _job_row(conn, job_id)
    assert row["state"] == "succeeded" and row["after_version"] == "9.9.9"
    assert acted == 1
    conn.close()


def test_reconcile_dead_bare_mutation_state_needs_recovery(tmp_path,
                                                           monkeypatch):
    from backend.app.config import settings as settings_lib
    monkeypatch.setattr(settings_lib, "log_dir",
                        str(tmp_path / "logs-empty"))
    conn = _make_db(str(tmp_path / "state.db"))
    job_id = _claimed_job(conn, state="updating")
    monkeypatch.setattr(dispatch_lib, "_unit_active", lambda unit: "inactive")
    dispatch_lib.reconcile_claimed_jobs(conn)
    row = _job_row(conn, job_id)
    assert row["state"] == "interrupted"
    assert int(row["recovery_required"]) == 1
    conn.close()


def test_reconcile_dead_bare_preflight_no_recovery(tmp_path, monkeypatch):
    from backend.app.config import settings as settings_lib
    monkeypatch.setattr(settings_lib, "log_dir",
                        str(tmp_path / "logs-empty2"))
    conn = _make_db(str(tmp_path / "state.db"))
    job_id = _claimed_job(conn, state="preflight")
    monkeypatch.setattr(dispatch_lib, "_unit_active", lambda unit: "inactive")
    dispatch_lib.reconcile_claimed_jobs(conn)
    row = _job_row(conn, job_id)
    assert row["state"] == "interrupted"
    assert int(row["recovery_required"]) == 0
    conn.close()


def test_reconcile_boot_shares_logic(tmp_path, monkeypatch):
    from backend.app.config import settings as settings_lib
    monkeypatch.setattr(settings_lib, "log_dir",
                        str(tmp_path / "logs-boot"))
    conn = _make_db(str(tmp_path / "state.db"))
    job_id = _claimed_job(conn, state="verifying")
    monkeypatch.setattr(dispatch_lib, "_unit_active", lambda unit: "inactive")
    dispatch_lib.reconcile_boot(conn)
    row = _job_row(conn, job_id)
    assert row["state"] == "interrupted"
    assert int(row["recovery_required"]) == 1
    conn.close()
