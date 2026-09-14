"""W3 durability regression tests: HTTP request lifetime != probe lifetime.

Core invariants under test:
- A probe that completes successfully persists its result AND applies the
  tool observation even when the waiting HTTP request times out, the API
  process disappears, or the API restarts.
- The dispatcher owns result persistence and observation application; the
  waiting route is a reader.
- The dispatcher main loop keeps heartbeating, dispatching jobs and
  reconciling while a slow probe runs (no synchronous 120s execution in
  the main loop).
- Duplicate normal checks coalesce durably; ?force=1 bypasses observation
  freshness only, never active mutation/probe safety.
- Failure preserves the last good observation and records the attempt
  separately (attempt/error/freshness are independent).
- Restart reconciliation is proof-based and resumes/applies durable state.

Hermetic: tmp sqlite DB, monkeypatched supervisors and unit states. No
test in this module touches real systemd, /opt, /etc or /var/lib.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import types
import uuid
from datetime import datetime, timedelta, timezone

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
from backend.app import owner_probes as probes_lib
from backend.app import units as units_lib
from backend.app.admission import admit as admit_lib
from backend.app.api import deps as deps_lib
from backend.app.api import routes as routes_lib
from backend.app.config import settings as settings_lib
from backend.app.worker import dispatch as dispatch_lib
from backend.app.worker import phase_run as phase_run_lib

import support as support_lib

_GOOD_TS = "2026-09-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _isolate_settings(tmp_path, monkeypatch):
    """Point db/state/logs at tmp; clear the in-memory discovery cache.

    Returns (state_dir, db_path, log_dir).
    """
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    log_dir = str(tmp_path / "logs")
    os.makedirs(log_dir, exist_ok=True)
    db_path = str(tmp_path / "state.db")
    release_dir = str(tmp_path / "release")
    os.makedirs(release_dir, exist_ok=True)
    monkeypatch.setattr(settings_lib, "state_dir", state_dir)
    monkeypatch.setattr(settings_lib, "db_path", db_path)
    monkeypatch.setattr(settings_lib, "log_dir", log_dir)
    monkeypatch.setattr(settings_lib, "plan_ttl_s", 300)
    monkeypatch.setenv("EGA_RELEASE_ROOT", release_dir)
    try:
        routes_lib._DISCOVERY_CACHE.clear()
    except Exception:
        pass
    return state_dir, db_path, log_dir


def _fresh_db(db_path):
    conn = db_lib.connect(db_path)
    db_lib.migrate(conn)
    conn.commit()
    return conn


def _payload(version="1.18.30"):
    # type: (str) -> dict
    return {
        "inspection": {"install_identity": "hermes-display-identity",
                       "version": version, "fingerprint": "fp-w3",
                       "channel": "stable"},
        "discovery": {"target": "1.18.31", "channel": "stable",
                      "available": True, "unknown_reason": ""},
        "activity": {"state": "idle", "evidence": "idle"},
        "verification": {
            "passed": True, "version": version,
            "checks": [{"name": "smoke", "result": "pass",
                        "mandatory": True, "summary": "ok"}]},
    }


def _patch_supervised_ok(monkeypatch, payload=None, calls=None):
    data = payload if payload is not None else _payload()

    def _ok(*args, **kwargs):
        if calls is not None:
            calls.append("run")
        return True, dict(data), "", False

    monkeypatch.setattr(phase_run_lib, "run_supervised_probe", _ok)
    return data


def _patch_supervised_fail(monkeypatch, reason="probe exploded"):
    def _fail(*args, **kwargs):
        return False, {}, reason, False

    monkeypatch.setattr(phase_run_lib, "run_supervised_probe", _fail)
    return reason


def _patch_units(monkeypatch, mapping=None, default="confirmed_stopped"):
    fake = support_lib.FakeUnitStates(mapping or {}, default=default)
    monkeypatch.setattr(units_lib, "query_unit", fake.query_unit)
    return fake


def _seed_good_observation(conn, tool_id="hermes", version="1.18.30"):
    conn.execute("INSERT OR IGNORE INTO tools(id) VALUES(?)", (tool_id,))
    conn.execute(
        "UPDATE tools SET install_identity=?, observed_version=?,"
        " available_target=?, channel=?, fingerprint=?, observation_time=?,"
        " discovery_error='', health=?, health_detail=?, updated_at=?,"
        " last_success_at=?, last_attempt_at='', last_attempt_error=''"
        " WHERE id=?",
        ("hermes-display-identity", version, "1.18.31", "stable", "fp-good",
         _GOOD_TS, "healthy", "verify pass version=%s" % version, _GOOD_TS,
         _GOOD_TS, tool_id))
    conn.commit()


def _tool_row(conn, tool_id="hermes"):
    return dict(conn.execute("SELECT * FROM tools WHERE id=?",
                             (tool_id,)).fetchone())


def _unreleased_probe_leases(conn):
    return conn.execute(
        "SELECT * FROM execution_leases WHERE kind='probe'"
        " AND released_at=''").fetchall()


def _probe_unit(request_id):
    from backend.app.owner_env import transient_probe_name
    return transient_probe_name(request_id)


# -- minimal FastAPI-request/auth fakes -------------------------------------

class _FakeRequest(object):
    def __init__(self, headers=None, query=None, body=b""):
        lowered = {}
        for k, v in (headers or {}).items():
            lowered[str(k).lower()] = v
        self.headers = lowered
        self.query_params = dict(query or {})
        self.cookies = {}
        self._body = body if isinstance(body, (bytes, bytearray)) else b""

    async def body(self):
        return bytes(self._body)


def _auth_ok(monkeypatch):
    monkeypatch.setattr(
        deps_lib, "authenticate",
        lambda request: ({"email": "owner@example.invalid"}, None, "rid-w3"))
    monkeypatch.setattr(
        deps_lib, "require_mutation_guards", lambda request, claims: None)
    monkeypatch.setattr(deps_lib, "check_rate_limit",
                        lambda ident, kind="read": True)


def _check_req(force=None):
    query = {}
    if force is not None:
        query["force"] = force
    return _FakeRequest(
        headers={"content-type": "application/json", "origin": "x",
                 "x-csrf-token": "x"},
        query=query, body=b"{}")


def _run(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def _resp_json(resp):
    try:
        raw = getattr(resp, "body", b"")
        if isinstance(raw, (bytes, bytearray)):
            return json.loads(bytes(raw).decode("utf-8") or "{}")
    except Exception:
        pass
    return {}


def _observation_module():
    """The W3 coordinator module (missing on the pre-W3 base: RED)."""
    import importlib
    return importlib.import_module("backend.app.observation")


# ---------------------------------------------------------------------------
# 1-3, 13. result + observation survive waiter timeout / API loss / restart
# ---------------------------------------------------------------------------

def test_waiter_timeout_then_probe_succeeds_persists(tmp_path, monkeypatch):
    """The HTTP waiter times out; a later successful probe must still
    persist the result AND the observation, and the durable request must
    remain claimable (the waiter never mutates durable lifecycle)."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    status, _payload_out = probes_lib.await_probe(conn, rid, timeout_s=0.3)
    assert status == "timeout"
    row = dict(conn.execute("SELECT * FROM probe_requests WHERE id=?",
                            (rid,)).fetchone())
    assert row["state"] == "queued", row["state"]
    _patch_supervised_ok(monkeypatch)
    assert dispatch_lib.run_probe_queue(conn) == 1
    res = conn.execute("SELECT * FROM probe_results WHERE request_id=?",
                       (rid,)).fetchone()
    assert res is not None and res["status"] == "ok"
    assert conn.execute("SELECT state FROM probe_requests WHERE id=?",
                        (rid,)).fetchone()["state"] == "done"
    tool = _tool_row(conn)
    assert tool["observed_version"] == "1.18.30"
    assert tool["health"] == "healthy"
    assert tool["observation_time"] == res["finished_at"]
    conn.close()


def test_real_handle_helper_times_out_with_durable_id_and_coalesces(
        tmp_path, monkeypatch):
    """The real submit helper returns (timeout, {}, request_id) and a
    second call coalesces onto the same durable active request."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    conn.close()
    status, _payload, rid = probes_lib.request_owner_probe_handle(
        "hermes", "refresh", timeout_s=0.3)
    assert status == "timeout"
    uuid.UUID(rid)  # durable handle is a real request id
    status2, _payload2, rid2 = probes_lib.request_owner_probe_handle(
        "hermes", "refresh", timeout_s=0.3)
    assert status2 == "timeout"
    assert rid2 == rid
    conn = db_lib.connect(db_path)
    try:
        row = dict(conn.execute("SELECT * FROM probe_requests WHERE id=?",
                                (rid,)).fetchone())
        assert row["state"] == "queued"
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM probe_requests WHERE tool_id='hermes'"
            " AND op='refresh'").fetchone()["n"] == 1
    finally:
        conn.close()


def test_api_request_disappears_after_submission_dispatcher_persists(
        tmp_path, monkeypatch):
    """The API process vanishes after submitting (connection closed, no
    waiter). The dispatcher must still store result + observation."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn1 = _fresh_db(db_path)
    rid = probes_lib.enqueue_probe(conn1, "api", "hermes", "refresh")
    conn1.close()
    conn2 = db_lib.connect(db_path)
    try:
        _patch_supervised_ok(monkeypatch)
        assert dispatch_lib.run_probe_queue(conn2) == 1
        res = conn2.execute("SELECT * FROM probe_results WHERE request_id=?",
                            (rid,)).fetchone()
        assert res is not None and res["status"] == "ok"
        tool = _tool_row(conn2)
        assert tool["observed_version"] == "1.18.30"
        assert tool["health"] == "healthy"
    finally:
        conn2.close()


def test_api_restart_after_submission_observation_not_lost(
        tmp_path, monkeypatch):
    """Simulated API restart after submission: a fresh connection/db
    reader still sees the durable result and applied observation."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn1 = _fresh_db(db_path)
    rid = probes_lib.enqueue_probe(conn1, "api", "hermes", "refresh")
    conn1.close()

    conn2 = db_lib.connect(db_path)  # restart: new process connection
    _patch_supervised_ok(monkeypatch)
    assert dispatch_lib.run_probe_queue(conn2) == 1

    conn3 = db_lib.connect(db_path)  # another restart before reading
    try:
        res = conn3.execute("SELECT * FROM probe_results WHERE request_id=?",
                            (rid,)).fetchone()
        assert res is not None and res["status"] == "ok"
        assert _tool_row(conn3)["observed_version"] == "1.18.30"
    finally:
        conn3.close()
        conn2.close()


def test_probe_completes_with_no_http_waiter_persists(tmp_path, monkeypatch):
    """No waiter ever exists: the coordinator alone owns completion."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    _patch_supervised_ok(monkeypatch)
    assert dispatch_lib.run_probe_queue(conn) == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM probe_results WHERE request_id=?",
        (rid,)).fetchone()["n"] == 1
    assert _tool_row(conn)["observed_version"] == "1.18.30"
    conn.close()


# ---------------------------------------------------------------------------
# 4-5. scheduler: slow probe must not starve heartbeat/dispatch
# ---------------------------------------------------------------------------

def test_slow_probe_keeps_worker_heartbeat_fresh(tmp_path, monkeypatch):
    """A 120s-class probe runs in the bounded probe worker; the main
    loop heartbeat keeps advancing while the probe is still running."""
    state_dir, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    started = threading.Event()
    release = threading.Event()

    def _slow(*args, **kwargs):
        started.set()
        release.wait(10)
        return True, _payload(), "", False

    monkeypatch.setattr(phase_run_lib, "run_supervised_probe", _slow)
    probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    worker = dispatch_lib.ProbeWorker(db_path, interval_s=0.05)
    worker.start()
    try:
        assert started.wait(5), "probe worker never started the probe"
        assert worker.is_alive()
        # Main-loop heartbeat write while the probe is blocked.
        dispatch_lib._write_dispatcher_heartbeat()
        hb = jobs_lib.read_dispatcher_heartbeat(state_dir, max_age_s=20)
        assert hb, "heartbeat went stale while a slow probe was running"
    finally:
        release.set()
        worker.stop(timeout_s=10)
    conn.close()


def test_slow_probe_does_not_block_job_dispatch(tmp_path, monkeypatch):
    """Job dispatch keeps making progress while a slow probe runs."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    started = threading.Event()
    release = threading.Event()

    def _slow(*args, **kwargs):
        started.set()
        release.wait(10)
        return True, _payload(), "", False

    monkeypatch.setattr(phase_run_lib, "run_supervised_probe", _slow)
    probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    monkeypatch.setattr(
        dispatch_lib, "_owner_env_for_job",
        lambda nonce: ({}, {"release_root": "/tmp",
                            "venv_python": sys.executable}, "/tmp"))
    monkeypatch.setattr(dispatch_lib, "_canonical_cmd",
                        lambda *a, **k: ["true"])
    monkeypatch.setattr(dispatch_lib, "_prove_launch",
                        lambda *a, **k: "live")
    monkeypatch.setattr(
        dispatch_lib.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0))
    worker = dispatch_lib.ProbeWorker(db_path, interval_s=0.05)
    worker.start()
    try:
        assert started.wait(5), "probe worker never started the probe"
        # A job already reserved before the probe was claimed (the only
        # production ordering: admission refuses while a probe lease is
        # held) must still be launched by the main loop.
        support_lib.test_release_root()
        plan = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO jobs(id,tool_id,plan_id,subject,idempotency_key,"
            "request_hash,state,step,created_at,claim_deadline,ack)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, plan["tool_id"], plan["id"], plan["subject"],
             "k-w3-dispatch", "hash-w3", "accepted", "",
             now.isoformat(),
             (now + timedelta(seconds=300)).isoformat(), 1))
        conn.commit()
        claimed = dispatch_lib.dispatch_once(conn)
        assert claimed == job_id
        row = dict(conn.execute("SELECT * FROM jobs WHERE id=?",
                                (job_id,)).fetchone())
        assert row["state"] == "preflight"
        # Reconciliation also keeps running (no-op with no claimed rows).
        assert dispatch_lib.reconcile_claimed_jobs(conn) == 0
    finally:
        release.set()
        worker.stop(timeout_s=10)
    conn.close()


# ---------------------------------------------------------------------------
# 6. exactly-once observation application (including restart/replay)
# ---------------------------------------------------------------------------

def test_success_updates_observation_exactly_once(tmp_path, monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    _patch_supervised_ok(monkeypatch)
    assert dispatch_lib.run_probe_queue(conn) == 1
    before = _tool_row(conn)
    observation = _observation_module()
    assert observation.apply_probe_result(conn, rid) is False
    assert _tool_row(conn) == before
    # Simulated restart/replay: the reconciliation scan must not re-apply.
    assert observation.reconcile_observations(conn) == 0
    assert _tool_row(conn) == before
    conn.close()


def test_unapplied_stale_result_cannot_regress_newer_observation(
        tmp_path, monkeypatch):
    """An older unapplied result (crash window) can never overwrite a
    newer applied observation, and it is still marked processed."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    observation = _observation_module()
    rid_new = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    probes_lib.finish_probe(conn, rid_new, "ok", _payload("2.0.0"))
    conn.execute("UPDATE probe_results SET finished_at=? WHERE request_id=?",
                 ("2026-09-10T00:00:00+00:00", rid_new))
    conn.commit()
    assert observation.apply_probe_result(conn, rid_new) is True
    assert _tool_row(conn)["observed_version"] == "2.0.0"
    rid_old = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    probes_lib.finish_probe(conn, rid_old, "ok", _payload("1.0.0"))
    conn.execute("UPDATE probe_results SET finished_at=? WHERE request_id=?",
                 ("2026-09-05T00:00:00+00:00", rid_old))
    conn.commit()
    assert observation.apply_probe_result(conn, rid_old) is False
    assert _tool_row(conn)["observed_version"] == "2.0.0"
    assert observation.apply_probe_result(conn, rid_old) is False
    conn.close()


# ---------------------------------------------------------------------------
# 7-8. failure preserves last good observation; attempt recorded separately
# ---------------------------------------------------------------------------

def test_failed_probe_preserves_last_good_observation(tmp_path, monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    _seed_good_observation(conn)
    _patch_units(monkeypatch, default="confirmed_stopped")
    reason = _patch_supervised_fail(monkeypatch, "install probe exploded")
    rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    assert dispatch_lib.run_probe_queue(conn) == 1
    res = conn.execute("SELECT * FROM probe_results WHERE request_id=?",
                       (rid,)).fetchone()
    assert res is not None and res["status"] == "error"
    tool = _tool_row(conn)
    assert tool["observed_version"] == "1.18.30"
    assert tool["health"] == "healthy"
    assert tool["install_identity"] == "hermes-display-identity"
    assert tool["observation_time"] == _GOOD_TS
    assert tool["last_success_at"] == _GOOD_TS
    assert reason in (tool["discovery_error"] or "")
    assert reason in (tool["last_attempt_error"] or "")
    conn.close()


def test_failed_probe_records_attempt_error_freshness_separately(
        tmp_path, monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    _seed_good_observation(conn)
    _patch_units(monkeypatch, default="confirmed_stopped")
    reason = _patch_supervised_fail(monkeypatch, "probe timed out")
    rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    assert dispatch_lib.run_probe_queue(conn) == 1
    res = conn.execute("SELECT * FROM probe_results WHERE request_id=?",
                       (rid,)).fetchone()
    tool = _tool_row(conn)
    # freshness (observation_time) is untouched; the attempt is recorded.
    assert tool["observation_time"] == _GOOD_TS
    assert tool["last_attempt_at"] == res["finished_at"]
    assert tool["last_attempt_at"] != tool["observation_time"]
    assert "probe timed out" in (tool["last_attempt_error"] or "")
    card = routes_lib._read_tool_card(conn, "hermes")
    assert card["checked_at"] == _GOOD_TS
    assert card["attempted_at"] == res["finished_at"]
    assert "probe timed out" in (card["attempt_error"] or "")
    conn.close()


def test_empty_ok_observation_never_erases_last_good(tmp_path, monkeypatch):
    """A structurally 'ok' result with no usable observation fields is
    recorded as a failed attempt; the last good observation survives."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    _seed_good_observation(conn, version="1.18.30")
    _patch_units(monkeypatch, default="confirmed_stopped")
    _patch_supervised_ok(monkeypatch, payload={"verification": {}})
    rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    assert dispatch_lib.run_probe_queue(conn) == 1
    res = conn.execute("SELECT * FROM probe_results WHERE request_id=?",
                       (rid,)).fetchone()
    assert res is not None and res["status"] == "ok"
    tool = _tool_row(conn)
    assert tool["observed_version"] == "1.18.30"
    assert tool["health"] == "healthy"
    assert tool["observation_time"] == _GOOD_TS
    assert "unusable" in (tool["last_attempt_error"] or "")
    conn.close()


# ---------------------------------------------------------------------------
# 9-10. durable coalescing; force bypasses freshness only
# ---------------------------------------------------------------------------

def test_duplicate_normal_checks_coalesce_durably(tmp_path, monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    rid1 = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    rid2 = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    assert rid2 == rid1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM probe_requests WHERE tool_id='hermes'"
        " AND op='refresh'").fetchone()["n"] == 1
    # Once the request completes, a new check creates a new probe.
    _patch_supervised_ok(monkeypatch)
    assert dispatch_lib.run_probe_queue(conn) == 1
    rid3 = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    assert rid3 != rid1
    # Different ops never coalesce into each other.
    rid_i = probes_lib.enqueue_probe(conn, "api", "hermes", "inspect")
    assert rid_i != rid3
    conn.close()


def test_concurrent_checks_coalesce_one_probe(tmp_path, monkeypatch):
    """Two concurrent submitters (separate connections) converge on ONE
    durable probe request: the lookup+insert is one IMMEDIATE tx."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    _fresh_db(db_path).close()
    barrier = threading.Barrier(2)
    ids = []  # type: list
    errors = []  # type: list

    def _submit():
        try:
            c = db_lib.connect(db_path)
        except Exception as exc:
            errors.append(exc)
            return
        try:
            barrier.wait(5)
            ids.append(probes_lib.enqueue_probe(c, "api", "hermes",
                                                "refresh"))
        except Exception as exc:
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=_submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors, errors
    assert len(ids) == 2 and len(set(ids)) == 1, ids
    conn = db_lib.connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM probe_requests"
            " WHERE tool_id='hermes' AND op='refresh'").fetchone()["n"] == 1
    finally:
        conn.close()


def test_force_bypasses_freshness_only(tmp_path, monkeypatch):
    """?force=1 skips the observation freshness cache but still coalesces
    onto an active probe and still respects the active-mutation gate."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    _auth_ok(monkeypatch)
    _fresh_db(db_path).close()
    submissions = []

    def _submit(tool_id, op, timeout_s=25.0, subject="api", coalesce=True):
        submissions.append((tool_id, op))
        c = db_lib.connect(settings_lib.db_path)
        try:
            rid = probes_lib.enqueue_probe(c, subject, tool_id, op,
                                           coalesce=coalesce)
        finally:
            c.close()
        return "timeout", {}, rid

    monkeypatch.setattr(probes_lib, "request_owner_probe_handle", _submit)
    first = _run(routes_lib.post_tool_check("hermes", _check_req(force="1")))
    assert first.status_code == 200
    body1 = _resp_json(first)
    assert body1.get("probe_request_id")
    assert body1.get("probe_pending") is True
    # A forced duplicate coalesces onto the still-active request.
    second = _run(routes_lib.post_tool_check("hermes", _check_req(force="1")))
    assert second.status_code == 200
    assert _resp_json(second).get("probe_request_id") == \
        body1.get("probe_request_id")
    conn = db_lib.connect(settings_lib.db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM probe_requests"
            " WHERE tool_id='hermes' AND op='refresh'").fetchone()["n"]
        assert n == 1
        # Active mutation gate: no probe submission, cached card returned.
        support_lib.test_release_root()
        support_lib.admit_new(conn)
    finally:
        conn.close()
    before = len(submissions)
    gated = _run(routes_lib.post_tool_check("hermes", _check_req(force="1")))
    assert gated.status_code == 200
    assert len(submissions) == before, "force must not probe during a job"
    body3 = _resp_json(gated)
    assert "updating" in (body3.get("health_detail") or "") or \
        "stale" in (body3.get("health_detail") or "")


# ---------------------------------------------------------------------------
# 11. observation may persist even when stop proof is missing
# ---------------------------------------------------------------------------

def test_ok_result_unknown_stop_applies_observation_but_holds_exclusion(
        tmp_path, monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    _patch_units(monkeypatch, default="unknown")
    _patch_supervised_ok(monkeypatch)
    monkeypatch.setattr(dispatch_lib, "_probe_stop_proven",
                        lambda request_id, supervised: False)
    rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    assert dispatch_lib.run_probe_queue(conn) == 1
    # Observation is applied: observation success != execution stopped.
    assert _tool_row(conn)["observed_version"] == "1.18.30"
    res = conn.execute("SELECT * FROM probe_results WHERE request_id=?",
                       (rid,)).fetchone()
    assert res is not None and res["status"] == "ok"
    evidence = json.loads(res["result_json"])
    assert evidence.get("exclusion_held") is True
    held = _unreleased_probe_leases(conn)
    assert len(held) == 1 and held[0]["request_id"] == rid
    # Mutation admission stays refused until proof arrives.
    plan = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    _jid, created, err = admit_lib(
        conn, "owner@example.invalid", "k-w3-hold", plan["id"], False,
        "fp-test-1", True, False)
    assert err == "busy" and not created
    conn.close()


# ---------------------------------------------------------------------------
# 12. worker restart reconciliation
# ---------------------------------------------------------------------------

def test_worker_restart_resumes_unfinished_probe(tmp_path, monkeypatch):
    """A running request whose probe unit is confirmed stopped (and whose
    claim deadline has not passed) is safely resumed; exclusion is
    released only with proof."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    assert probes_lib.claim_probe(conn, "dispatcher-dead")
    lease = leases_lib.acquire_probe_lease(
        conn, "hermes", "dispatcher-dead", request_id=rid)
    assert lease
    unit = _probe_unit(rid)
    _patch_units(monkeypatch, {unit: "confirmed_stopped"})
    dispatch_lib.reconcile_boot(conn)
    row = dict(conn.execute("SELECT * FROM probe_requests WHERE id=?",
                            (rid,)).fetchone())
    assert row["state"] == "queued", row["state"]
    assert _unreleased_probe_leases(conn) == []
    _patch_supervised_ok(monkeypatch)
    assert dispatch_lib.run_probe_queue(conn) == 1
    assert _tool_row(conn)["observed_version"] == "1.18.30"
    conn.close()


def test_worker_restart_applies_stored_but_unapplied_result(
        tmp_path, monkeypatch):
    """A crash after result storage but before observation application:
    boot reconciliation applies the durable result exactly once."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    assert probes_lib.claim_probe(conn, "dispatcher-dead")
    lease = leases_lib.acquire_probe_lease(
        conn, "hermes", "dispatcher-dead", request_id=rid)
    assert lease
    probes_lib.finish_probe(conn, rid, "ok", _payload())
    unit = _probe_unit(rid)
    _patch_units(monkeypatch, {unit: "confirmed_stopped"})
    dispatch_lib.reconcile_boot(conn)
    assert _tool_row(conn)["observed_version"] == "1.18.30"
    assert _unreleased_probe_leases(conn) == []
    observation = _observation_module()
    assert observation.apply_probe_result(conn, rid) is False
    conn.close()


def test_orphaned_claim_past_deadline_is_reconciled_by_loop(
        tmp_path, monkeypatch):
    """A claim orphaned past its bounded execution window is resolved by
    the loop backstop with unit proof (terminal here), never by time."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    assert probes_lib.claim_probe(conn, "dispatcher-dead")
    conn.execute("UPDATE probe_requests SET claim_deadline=? WHERE id=?",
                 ("2000-01-01T00:00:00+00:00", rid))
    conn.commit()
    lease = leases_lib.acquire_probe_lease(
        conn, "hermes", "dispatcher-dead", request_id=rid)
    assert lease
    unit = _probe_unit(rid)
    _patch_units(monkeypatch, {unit: "confirmed_stopped"})
    report = dispatch_lib.reconcile_probe_requests(
        conn, only_past_deadline=True)
    assert report["expired"] == 1, report
    assert conn.execute("SELECT state FROM probe_requests WHERE id=?",
                        (rid,)).fetchone()["state"] == "expired"
    # A live in-flight probe inside its window is never touched.
    rid2 = probes_lib.enqueue_probe(conn, "api", "opencode", "refresh")
    assert probes_lib.claim_probe(conn, "dispatcher-live")
    _patch_units(monkeypatch, {_probe_unit(rid2): "confirmed_stopped"})
    report2 = dispatch_lib.reconcile_probe_requests(
        conn, only_past_deadline=True)
    assert report2["checked"] == 0, report2
    assert conn.execute("SELECT state FROM probe_requests WHERE id=?",
                        (rid2,)).fetchone()["state"] == "running"
    conn.close()


def test_worker_restart_unknown_unit_keeps_exclusion(tmp_path, monkeypatch):
    """Unknown unit state on restart: the exclusion is held and mutation
    admission stays refused (recovery-required equivalent)."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    assert probes_lib.claim_probe(conn, "dispatcher-dead")
    lease = leases_lib.acquire_probe_lease(
        conn, "hermes", "dispatcher-dead", request_id=rid)
    assert lease
    unit = _probe_unit(rid)
    _patch_units(monkeypatch, {unit: "unknown"})
    dispatch_lib.reconcile_boot(conn)
    assert _unreleased_probe_leases(conn)
    assert conn.execute("SELECT state FROM probe_requests WHERE id=?",
                        (rid,)).fetchone()["state"] == "running"
    plan = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    _jid, created, err = admit_lib(
        conn, "owner@example.invalid", "k-w3-unknown", plan["id"], False,
        "fp-test-1", True, False)
    assert err == "busy" and not created
    conn.close()


# ---------------------------------------------------------------------------
# HTTP polling surface: GET /probes/{request_id}
# ---------------------------------------------------------------------------

def test_probe_status_endpoint_reads_durable_state(tmp_path, monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    _auth_ok(monkeypatch)
    conn = _fresh_db(db_path)
    rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    resp = routes_lib.get_probe(rid, _FakeRequest())
    assert resp.status_code == 200
    body = _resp_json(resp)
    assert body.get("request_id") == rid
    assert body.get("state") == "queued"
    assert body.get("pending") is True
    # after completion, the result and the applied marker are readable
    _patch_supervised_ok(monkeypatch)
    assert dispatch_lib.run_probe_queue(conn) == 1
    body2 = _resp_json(routes_lib.get_probe(rid, _FakeRequest()))
    assert body2.get("state") == "done"
    assert body2.get("pending") is False
    assert body2.get("status") == "ok"
    assert body2.get("observation_applied") is True
    conn.close()


# ---------------------------------------------------------------------------
# W3.1: RESULT VISIBLE != OBSERVATION APPLIED
# ---------------------------------------------------------------------------

def _wait_for_queued_request(conn, tool_id="hermes", timeout_s=5.0):
    # type: (object, str, float) -> str
    deadline = time.monotonic() + float(timeout_s)
    while time.monotonic() < deadline:
        row = conn.execute(
            "SELECT id FROM probe_requests WHERE tool_id=? AND op='refresh'"
            " AND state='queued' ORDER BY created_at LIMIT 1",
            (tool_id,)).fetchone()
        if row is not None:
            return str(row["id"])
        time.sleep(0.02)
    raise AssertionError("helper never enqueued a probe request")


def _applied_marker(conn, request_id):
    # type: (object, str) -> bool
    """The durable applied marker: probe_requests.result_id == the
    finished_at token of the stored result."""
    row = conn.execute(
        "SELECT pr.result_id AS marker, res.finished_at AS finished_at"
        " FROM probe_requests pr JOIN probe_results res"
        " ON res.request_id=pr.id WHERE pr.id=?",
        (request_id,)).fetchone()
    if row is None:
        return False
    finished = str(row["finished_at"] or "")
    return bool(finished) and str(row["marker"] or "") == finished


def test_visible_unapplied_result_helper_returns_pending(
        tmp_path, monkeypatch):
    """A stored result whose coordinator apply has not committed must not
    be reported as ok: the bounded helper returns pending + durable id."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(probes_lib, "OBSERVATION_APPLY_WAIT_S", 0.3,
                        raising=False)
    conn = _fresh_db(db_path)
    _seed_good_observation(conn)
    out = {}

    def _waiter():
        out["r"] = probes_lib.request_owner_probe_handle(
            "hermes", "refresh", timeout_s=10.0)

    waiter = threading.Thread(target=_waiter)
    waiter.start()
    try:
        rid = _wait_for_queued_request(conn)
        assert probes_lib.claim_probe(conn, "test-coordinator")
        # Result visible; observation application never ran (crash window).
        assert probes_lib.finish_probe(conn, rid, "ok", _payload("2.0.0"))
        assert _applied_marker(conn, rid) is False
    finally:
        waiter.join(10)
    assert not waiter.is_alive()
    status, payload, handle = out["r"]
    assert status == "pending", (status, payload)
    assert handle == rid
    # The old card is untouched: application is still pending.
    assert _tool_row(conn)["observed_version"] == "1.18.30"
    conn.close()


def test_route_apply_failure_never_caches_or_reports_fresh(
        tmp_path, monkeypatch):
    """Route-level: a result stored but not applied returns the cached card
    labeled pending with the durable handle, and never seeds the
    in-process freshness cache. A later reconcile applies it durably."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    _auth_ok(monkeypatch)
    monkeypatch.setattr(probes_lib, "OBSERVATION_APPLY_WAIT_S", 0.3,
                        raising=False)
    conn = _fresh_db(db_path)
    _seed_good_observation(conn)
    _patch_supervised_ok(monkeypatch, payload=_payload("2.0.0"))
    observation = _observation_module()
    real_apply = observation.apply_probe_result
    fail = {"on": True}

    def _flaky_apply(c, request_id):
        if fail["on"]:
            return False
        return real_apply(c, request_id)

    monkeypatch.setattr(observation, "apply_probe_result", _flaky_apply)
    coord = {}

    def _coordinator():
        c = db_lib.connect(db_path)
        try:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if dispatch_lib.run_probe_queue(c) == 1:
                    coord["processed"] = True
                    return
                time.sleep(0.01)
            coord["error"] = "probe never processed"
        finally:
            c.close()

    coordinator = threading.Thread(target=_coordinator)
    coordinator.start()
    try:
        resp = _run(routes_lib.post_tool_check("hermes", _check_req()))
    finally:
        coordinator.join(10)
    assert "error" not in coord, coord
    assert resp.status_code == 200
    body = _resp_json(resp)
    assert body.get("probe_pending") is True, body
    rid = body.get("probe_request_id")
    assert rid
    assert body.get("installed_version") == "1.18.30"
    assert body.get("health") == "stale"
    assert routes_lib._DISCOVERY_CACHE == {}, "unapplied result was cached"
    assert _applied_marker(conn, rid) is False
    # The coordinator recovers later: reconcile applies exactly once.
    fail["on"] = False
    assert observation.reconcile_observations(conn) == 1
    assert _applied_marker(conn, rid) is True
    poll = _resp_json(routes_lib.get_probe(rid, _FakeRequest()))
    assert poll.get("observation_applied") is True
    assert poll.get("status") == "ok"
    card = routes_lib._read_tool_card(conn, "hermes")
    assert card["installed_version"] == "2.0.0"
    assert card["health"] == "healthy"
    conn.close()


def test_apply_write_contention_returns_pending_not_false_completion(
        tmp_path, monkeypatch):
    """A visible result whose apply is delayed by an unrelated writer
    returns pending (not false completion); once contention clears the
    apply commits and the durable marker is readable."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    _auth_ok(monkeypatch)
    monkeypatch.setattr(probes_lib, "OBSERVATION_APPLY_WAIT_S", 0.3,
                        raising=False)
    conn = _fresh_db(db_path)
    _seed_good_observation(conn)
    observation = _observation_module()
    out = {}

    def _waiter():
        out["r"] = probes_lib.request_owner_probe_handle(
            "hermes", "refresh", timeout_s=10.0)

    waiter = threading.Thread(target=_waiter)
    waiter.start()
    blocker = None
    coord_conn = None
    try:
        rid = _wait_for_queued_request(conn)
        assert probes_lib.claim_probe(conn, "test-coordinator")
        assert probes_lib.finish_probe(conn, rid, "ok", _payload("2.0.0"))
        # An unrelated writer holds the single SQLite write lock.
        blocker = db_lib.connect(db_path)
        blocker.execute("BEGIN IMMEDIATE")
        coord_conn = db_lib.connect(db_path)
        coord_conn.execute("PRAGMA busy_timeout=200")
        # The coordinator apply is delayed by contention and fails
        # closed: no applied marker is written.
        assert observation.apply_probe_result(coord_conn, rid) is False
        waiter.join(10)
        assert not waiter.is_alive()
        status, payload, handle = out["r"]
        assert status == "pending", (status, payload)
        assert handle == rid
        assert _applied_marker(conn, rid) is False
        # Contention clears: the recorded result applies later.
        blocker.execute("ROLLBACK")
        blocker.close()
        blocker = None
        assert observation.apply_probe_result(coord_conn, rid) is True
        assert _applied_marker(conn, rid) is True
        body = _resp_json(routes_lib.get_probe(rid, _FakeRequest()))
        assert body.get("observation_applied") is True
        assert body.get("status") == "ok"
        assert _tool_row(conn)["observed_version"] == "2.0.0"
    finally:
        if blocker is not None:
            try:
                blocker.execute("ROLLBACK")
            except Exception:
                pass
            try:
                blocker.close()
            except Exception:
                pass
        if coord_conn is not None:
            coord_conn.close()
        if waiter.is_alive():
            waiter.join(10)
    conn.close()


def test_fast_applied_path_returns_new_card_and_caches(
        tmp_path, monkeypatch):
    """Normal fast path: result stored AND applied within the bounded
    wait -> the route returns the new observation as fresh (and caches
    the freshness snapshot)."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    _auth_ok(monkeypatch)
    monkeypatch.setattr(probes_lib, "OBSERVATION_APPLY_WAIT_S", 5.0,
                        raising=False)
    conn = _fresh_db(db_path)
    _seed_good_observation(conn)
    _patch_supervised_ok(monkeypatch, payload=_payload("2.0.0"))
    coord = {}

    def _coordinator():
        c = db_lib.connect(db_path)
        try:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if dispatch_lib.run_probe_queue(c) == 1:
                    coord["processed"] = True
                    return
                time.sleep(0.01)
            coord["error"] = "probe never processed"
        finally:
            c.close()

    coordinator = threading.Thread(target=_coordinator)
    coordinator.start()
    try:
        resp = _run(routes_lib.post_tool_check("hermes", _check_req()))
    finally:
        coordinator.join(10)
    assert "error" not in coord, coord
    assert resp.status_code == 200
    body = _resp_json(resp)
    assert body.get("probe_pending") is False
    assert body.get("installed_version") == "2.0.0"
    assert body.get("health") == "healthy"
    assert routes_lib._DISCOVERY_CACHE, "applied result must be cached"
    conn.close()


# ---------------------------------------------------------------------------
# W3.1: probe worker liveness is part of worker health
# ---------------------------------------------------------------------------

def test_probe_worker_startup_db_failure_is_not_healthy(
        tmp_path, monkeypatch):
    """A probe executor whose DB connect fails at startup never reports
    ready, and the supervisor gate fails closed with SystemExit."""
    _state, _db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    bad = str(tmp_path / "not-a-db")
    os.makedirs(bad, exist_ok=True)  # a directory: sqlite cannot open it
    worker = dispatch_lib.ProbeWorker(bad, interval_s=0.05)
    worker.start(timeout_s=1.0)
    assert worker.is_ready() is False
    assert worker.startup_error()
    with pytest.raises(SystemExit):
        dispatch_lib._require_probe_worker(worker)


def test_probe_worker_death_after_start_raises_supervisor_gate(
        tmp_path, monkeypatch):
    """A required probe executor that dies after startup must terminate
    the dispatcher (SystemExit), never leave a green heartbeat forever."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    _fresh_db(db_path).close()
    worker = dispatch_lib.ProbeWorker(db_path, interval_s=0.05)
    worker.start()
    assert worker.is_ready()
    assert dispatch_lib._require_probe_worker(worker) is None
    worker.stop(timeout_s=2.0)
    assert not worker.is_alive()
    assert not worker.is_ready()
    with pytest.raises(SystemExit):
        dispatch_lib._require_probe_worker(worker)


def test_healthy_probe_worker_keeps_dispatcher_gate_open(
        tmp_path, monkeypatch):
    """A live, connected executor passes the gate across loop cadence
    while the dispatcher heartbeat keeps advancing."""
    state_dir, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    _fresh_db(db_path).close()
    worker = dispatch_lib.ProbeWorker(db_path, interval_s=0.05)
    worker.start()
    try:
        for _ in range(3):
            assert worker.is_ready()
            assert dispatch_lib._require_probe_worker(worker) is None
            dispatch_lib._write_dispatcher_heartbeat()
            assert jobs_lib.read_dispatcher_heartbeat(
                state_dir, max_age_s=20)
            time.sleep(0.1)
    finally:
        worker.stop(timeout_s=2.0)


def test_probe_worker_failure_cannot_release_unresolved_probe_lease(
        tmp_path, monkeypatch):
    """A dead/never-ready executor must not resolve exclusion: an
    unresolved probe lease remains held and mutation admission stays
    refused."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    assert probes_lib.claim_probe(conn, "dispatcher-dead")
    lease = leases_lib.acquire_probe_lease(
        conn, "hermes", "dispatcher-dead", request_id=rid)
    assert lease
    bad = str(tmp_path / "not-a-db")
    os.makedirs(bad, exist_ok=True)
    worker = dispatch_lib.ProbeWorker(bad, interval_s=0.05)
    worker.start(timeout_s=1.0)
    with pytest.raises(SystemExit):
        dispatch_lib._require_probe_worker(worker)
    assert _unreleased_probe_leases(conn)
    plan = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    _jid, created, err = admit_lib(
        conn, "owner@example.invalid", "k-w3-liveness", plan["id"], False,
        "fp-test-1", True, False)
    assert err == "busy" and not created
    conn.close()


def test_restart_reconcile_safe_with_unfinished_probe(
        tmp_path, monkeypatch):
    """Supervised restart with an unfinished durable probe: boot
    reconciliation keeps the unresolved lease held until positive unit
    proof (existing W3 semantics), and a healthy new executor passes the
    gate."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    assert probes_lib.claim_probe(conn, "dispatcher-dead")
    lease = leases_lib.acquire_probe_lease(
        conn, "hermes", "dispatcher-dead", request_id=rid)
    assert lease
    unit = _probe_unit(rid)
    _patch_units(monkeypatch, {unit: "unknown"})
    dispatch_lib.reconcile_boot(conn)
    assert _unreleased_probe_leases(conn)
    worker = dispatch_lib.ProbeWorker(db_path, interval_s=0.05)
    worker.start()
    try:
        assert dispatch_lib._require_probe_worker(worker) is None
        assert _unreleased_probe_leases(conn)
    finally:
        # Quiesce the executor before proof-based reconciliation: a live
        # worker may legitimately claim the resumed request and hold its
        # OWN new lease while the probe runs, which would make a blanket
        # "no unreleased leases" assertion race that execution. Production
        # order is exactly reconcile_boot -> ProbeWorker.start().
        worker.stop(timeout_s=2.0)
    _patch_units(monkeypatch, {unit: "confirmed_stopped"})
    dispatch_lib.reconcile_boot(conn)
    assert _unreleased_probe_leases(conn) == []
    # A healthy new executor still passes the supervisor gate afterwards.
    worker2 = dispatch_lib.ProbeWorker(db_path, interval_s=0.05)
    worker2.start()
    try:
        assert dispatch_lib._require_probe_worker(worker2) is None
    finally:
        worker2.stop(timeout_s=2.0)
    conn.close()
