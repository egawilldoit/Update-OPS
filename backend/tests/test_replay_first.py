"""D4 replay-first tests: identical retries must replay BEFORE any
new-admission prerequisite (expiry sweep, plan load, owner probe, worker
heartbeat, drain, recovery).

Hermetic: tmp dirs, faked auth and owner-probe seam, no server, no real
probes. Style matches backend/tests/test_api_flows.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import uuid

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from backend.app import admission as admission_lib
from backend.app import db as db_lib
from backend.app import jobs as jobs_lib
from backend.app.api import deps as deps_lib
from backend.app.api import routes as routes_lib
from backend.app.config import settings as settings_lib

import support as support_lib


# ---------------------------------------------------------------------------
# helpers (same patterns as test_api_flows.py)
# ---------------------------------------------------------------------------

class _FakeRequest(object):
    """Minimal stand-in for a FastAPI Request (auth is monkeypatched)."""

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
        lambda request: ({"email": "owner@example.invalid"}, None,
                         "test-rid"))
    monkeypatch.setattr(
        deps_lib, "require_mutation_guards", lambda request, claims: None)
    monkeypatch.setattr(deps_lib, "check_rate_limit",
                         lambda ident, kind="read": True)


def _isolate_settings(monkeypatch, tmp_path):
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
    monkeypatch.setattr(settings_lib, "body_limit_bytes", 256 * 1024)
    monkeypatch.setattr(settings_lib, "plan_ttl_s", 300)
    monkeypatch.setenv("EGA_RELEASE_ROOT", release_dir)
    try:
        from backend.app.schemas import utcnow_iso
        with open(os.path.join(state_dir, "dispatcher.heartbeat"), "w",
                  encoding="utf-8") as fh:
            json.dump({"ts": utcnow_iso(), "pid": 4242}, fh)
    except Exception:
        pass
    return state_dir, db_path, log_dir


def _make_db(db_path):
    conn = db_lib.connect(db_path)
    db_lib.migrate(conn)
    conn.commit()
    return conn


def _setup(tmp_path, monkeypatch):
    _auth_ok(monkeypatch)
    state_dir, db_path, _log_dir = _isolate_settings(monkeypatch, tmp_path)
    _make_db(db_path).close()
    return state_dir, db_path


def _resp_json(resp):
    try:
        raw = getattr(resp, "body", b"")
        if isinstance(raw, (bytes, bytearray)):
            return json.loads(bytes(raw).decode("utf-8") or "{}")
    except Exception:
        pass
    return {}


def _run(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def _install_probe(monkeypatch, fail=False, fingerprint="fp-replay-1"):
    """Replace routes._owner_probe with a counted, never-slow fake.

    fail=True models an unavailable inspection path; the returned dict
    exposes the call count so tests can prove replay never probed.
    """
    state = {"calls": 0}

    async def _fake_probe(tool_id, op, timeout_s=25.0):
        state["calls"] += 1
        if fail:
            return "error", {"reason": "owner probe unavailable"}
        return "ok", {"fingerprint": fingerprint, "version": "1.0.0"}

    monkeypatch.setattr(routes_lib, "_owner_probe", _fake_probe)
    return state


def _plan(db_path, plan_id, subject="owner@example.invalid",
          fingerprint="fp-replay-1"):
    conn = db_lib.connect(db_path)
    try:
        support_lib.v2_plan_row(conn, plan_id, tool_id="hermes",
                                subject=subject, fingerprint=fingerprint)
    finally:
        conn.close()


def _post(plan_id, ack=False, key="k-replay-1"):
    body = json.dumps(
        {"plan_id": plan_id, "activity_ack": ack}).encode("utf-8")
    req = _FakeRequest(
        headers={"content-type": "application/json", "origin": "x",
                 "x-csrf-token": "x", "idempotency-key": key},
        body=body)
    return _run(routes_lib.post_job(req))


def _jobs_by_key(db_path, key):
    conn = db_lib.connect(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM jobs WHERE idempotency_key=?",
            (key,)).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. retry while the original job is still active
# ---------------------------------------------------------------------------

def test_retry_while_original_job_active_replays_recorded_view(
        tmp_path, monkeypatch):
    _state_dir, db_path = _setup(tmp_path, monkeypatch)
    pid = str(uuid.uuid4())
    _plan(db_path, pid)
    _install_probe(monkeypatch)
    first = _post(pid, ack=False, key="k-active-1")
    assert first.status_code == 202, _resp_json(first)
    job_id = _resp_json(first)["id"]

    # Client retries after the claim window passed: replay must return
    # the recorded reservation, not a swept/blocked row (the expiry
    # sweep runs as a new-admission prerequisite on the broken path).
    conn = db_lib.connect(db_path)
    try:
        conn.execute(
            "UPDATE jobs SET claim_deadline='2000-01-01T00:00:00+00:00'"
            " WHERE id=?", (job_id,))
        conn.commit()
    finally:
        conn.close()

    probe = _install_probe(monkeypatch)
    retry = _post(pid, ack=False, key="k-active-1")
    body = _resp_json(retry)
    assert retry.status_code == 200, body
    assert body.get("id") == job_id, body
    assert body.get("replayed") is True, body
    assert body.get("state") == "accepted", body
    assert "backup_summary" in body, body
    assert probe["calls"] == 0, "replay must not run the owner probe"
    conn = db_lib.connect(db_path)
    try:
        state = conn.execute("SELECT state FROM jobs WHERE id=?",
                             (job_id,)).fetchone()["state"]
    finally:
        conn.close()
    assert state == "accepted", state


# ---------------------------------------------------------------------------
# 2. retry after drain becomes active
# ---------------------------------------------------------------------------

def test_retry_after_drain_replays_without_new_admission(
        tmp_path, monkeypatch):
    state_dir, db_path = _setup(tmp_path, monkeypatch)
    pid = str(uuid.uuid4())
    _plan(db_path, pid)
    _install_probe(monkeypatch)
    first = _post(pid, ack=False, key="k-drain-1")
    assert first.status_code == 202, _resp_json(first)
    job_id = _resp_json(first)["id"]

    with open(os.path.join(state_dir, "drain"), "w",
              encoding="utf-8") as fh:
        fh.write("maintenance\n")
    assert routes_lib._drained() is True

    probe = _install_probe(monkeypatch, fail=True)
    retry = _post(pid, ack=False, key="k-drain-1")
    body = _resp_json(retry)
    assert retry.status_code == 200, body
    assert body.get("id") == job_id, body
    assert body.get("replayed") is True, body
    assert probe["calls"] == 0, "replay must not run the owner probe"
    assert len(_jobs_by_key(db_path, "k-drain-1")) == 1


# ---------------------------------------------------------------------------
# 3. retry after recovery_required is introduced
# ---------------------------------------------------------------------------

def test_retry_after_recovery_required_replays_without_new_admission(
        tmp_path, monkeypatch):
    _state_dir, db_path = _setup(tmp_path, monkeypatch)
    pid = str(uuid.uuid4())
    _plan(db_path, pid)
    _install_probe(monkeypatch)
    first = _post(pid, ack=False, key="k-recovery-1")
    assert first.status_code == 202, _resp_json(first)
    job_id = _resp_json(first)["id"]

    conn = db_lib.connect(db_path)
    try:
        jobs_lib.set_recovery(conn, job_id, True)
        conn.commit()
    finally:
        conn.close()

    probe = _install_probe(monkeypatch, fail=True)
    retry = _post(pid, ack=False, key="k-recovery-1")
    body = _resp_json(retry)
    assert retry.status_code == 200, body
    assert body.get("id") == job_id, body
    assert body.get("replayed") is True, body
    assert probe["calls"] == 0, "replay must not run the owner probe"
    assert len(_jobs_by_key(db_path, "k-recovery-1")) == 1


# ---------------------------------------------------------------------------
# 4. retry while owner inspection is unavailable
# ---------------------------------------------------------------------------

def test_retry_while_owner_inspection_unavailable_replays(
        tmp_path, monkeypatch):
    _state_dir, db_path = _setup(tmp_path, monkeypatch)
    pid = str(uuid.uuid4())
    _plan(db_path, pid)
    _install_probe(monkeypatch)
    first = _post(pid, ack=False, key="k-probe-1")
    assert first.status_code == 202, _resp_json(first)
    job_id = _resp_json(first)["id"]

    probe = _install_probe(monkeypatch, fail=True)
    retry = _post(pid, ack=False, key="k-probe-1")
    body = _resp_json(retry)
    assert retry.status_code == 200, body
    assert body.get("id") == job_id, body
    assert body.get("replayed") is True, body
    assert probe["calls"] == 0, "replay must not run the owner probe"
    assert len(_jobs_by_key(db_path, "k-probe-1")) == 1


# ---------------------------------------------------------------------------
# 5. retry while the worker and the probe boundary are unavailable
# ---------------------------------------------------------------------------

def test_retry_while_worker_and_probe_boundary_unavailable(
        tmp_path, monkeypatch):
    state_dir, db_path = _setup(tmp_path, monkeypatch)
    pid = str(uuid.uuid4())
    _plan(db_path, pid)

    import backend.app.owner_probes as owner_probes_lib
    boundary = {"calls": 0}

    def _ok_boundary(*args, **kwargs):
        boundary["calls"] += 1
        return "ok", {"fingerprint": "fp-replay-1", "version": "1.0.0"}

    def _down_boundary(*args, **kwargs):
        boundary["calls"] += 1
        return "error", {"reason": "probe boundary down"}

    # Exercise the REAL routes._owner_probe wrapper against the patched
    # owner-probe boundary (no routes-level fake in this test).
    monkeypatch.setattr(owner_probes_lib, "request_owner_probe",
                        _ok_boundary)
    first = _post(pid, ack=False, key="k-worker-1")
    assert first.status_code == 202, _resp_json(first)
    job_id = _resp_json(first)["id"]

    try:
        os.remove(os.path.join(state_dir, "dispatcher.heartbeat"))
    except OSError:
        pass
    monkeypatch.setattr(owner_probes_lib, "request_owner_probe",
                        _down_boundary)
    boundary["calls"] = 0

    retry = _post(pid, ack=False, key="k-worker-1")
    body = _resp_json(retry)
    assert retry.status_code == 200, body
    assert body.get("id") == job_id, body
    assert body.get("replayed") is True, body
    assert boundary["calls"] == 0, "replay must not touch the probe boundary"
    assert len(_jobs_by_key(db_path, "k-worker-1")) == 1


# ---------------------------------------------------------------------------
# 6. concurrent identical submissions converge on ONE job
# ---------------------------------------------------------------------------

def test_concurrent_identical_submissions_converge(tmp_path, monkeypatch):
    _state_dir, db_path = _setup(tmp_path, monkeypatch)
    pid = str(uuid.uuid4())
    _plan(db_path, pid)
    _install_probe(monkeypatch)

    # Gate the FIRST find_replay call of each request thread: both
    # requests are provably inside the replay lookup before either can
    # reserve, so any later winner/loser split is a true race.
    barrier = threading.Barrier(2, timeout=30)
    local = threading.local()
    real_find = jobs_lib.find_replay

    def _gated_find(conn, subject, idem_key):
        if not getattr(local, "gated", False):
            local.gated = True
            barrier.wait(timeout=30)
        return real_find(conn, subject, idem_key)

    monkeypatch.setattr(jobs_lib, "find_replay", _gated_find)

    out = [None, None]

    def _worker(idx):
        try:
            out[idx] = _post(pid, ack=False, key="k-race-1")
        except Exception as exc:  # pragma: no cover - surfaced below
            out[idx] = exc

    t1 = threading.Thread(target=_worker, args=(0,))
    t2 = threading.Thread(target=_worker, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert all(r is not None and not isinstance(r, Exception)
               for r in out), out
    bodies = [_resp_json(r) for r in out]
    ids = [b.get("id", "") for b in bodies]
    assert all(ids), bodies
    assert ids[0] == ids[1], bodies
    assert sorted(r.status_code for r in out) == [200, 202], \
        [r.status_code for r in out]
    rows = _jobs_by_key(db_path, "k-race-1")
    assert len(rows) == 1, rows


def test_admit_concurrent_same_key_converges(tmp_path, monkeypatch):
    """Same-key race at the admission seam: the loser must return the
    winner's job (created_new False), never a second reservation."""
    _state_dir, db_path = _setup(tmp_path, monkeypatch)
    pid = str(uuid.uuid4())
    _plan(db_path, pid)

    barrier = threading.Barrier(2, timeout=30)
    local = threading.local()
    real_find = jobs_lib.find_replay

    def _gated_find(conn, subject, idem_key):
        if not getattr(local, "gated", False):
            local.gated = True
            barrier.wait(timeout=30)
        return real_find(conn, subject, idem_key)

    monkeypatch.setattr(jobs_lib, "find_replay", _gated_find)

    out = [None, None]

    def _worker(idx):
        conn = db_lib.connect(db_path)
        try:
            out[idx] = admission_lib.admit(
                conn, "owner@example.invalid", "k-race-unit", pid, False,
                "fp-replay-1", True, False)
        except Exception as exc:  # pragma: no cover - surfaced below
            out[idx] = exc
        finally:
            conn.close()

    t1 = threading.Thread(target=_worker, args=(0,))
    t2 = threading.Thread(target=_worker, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert all(isinstance(o, tuple) for o in out), out
    assert all(o[2] == "" for o in out), out
    assert out[0][0] == out[1][0] and out[0][0], out
    conn = db_lib.connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    finally:
        conn.close()
    assert n == 1, n


# ---------------------------------------------------------------------------
# 7. same key with mismatched request data is a safe conflict
# ---------------------------------------------------------------------------

def test_same_key_mismatched_request_is_conflict_no_new_job(
        tmp_path, monkeypatch):
    _state_dir, db_path = _setup(tmp_path, monkeypatch)
    pid_a = str(uuid.uuid4())
    pid_b = str(uuid.uuid4())
    _plan(db_path, pid_a)
    _plan(db_path, pid_b)
    _install_probe(monkeypatch)
    first = _post(pid_a, ack=False, key="k-conflict-1")
    assert first.status_code == 202, _resp_json(first)
    job_id = _resp_json(first)["id"]

    probe = _install_probe(monkeypatch, fail=True)

    # Same key + same plan + different ack -> conflict, no new job.
    ack_conflict = _post(pid_a, ack=True, key="k-conflict-1")
    assert ack_conflict.status_code == 409, _resp_json(ack_conflict)
    assert _resp_json(ack_conflict).get("code") == "conflict"
    # Same key + different plan -> conflict, no new job.
    plan_conflict = _post(pid_b, ack=False, key="k-conflict-1")
    assert plan_conflict.status_code == 409, _resp_json(plan_conflict)
    assert _resp_json(plan_conflict).get("code") == "conflict"
    assert probe["calls"] == 0, "conflict must not probe or admit"
    rows = _jobs_by_key(db_path, "k-conflict-1")
    assert len(rows) == 1 and rows[0]["id"] == job_id


# ---------------------------------------------------------------------------
# 8. replay never depends on current plan eligibility
# ---------------------------------------------------------------------------

def test_retry_replays_after_plan_deleted(tmp_path, monkeypatch):
    _state_dir, db_path = _setup(tmp_path, monkeypatch)
    pid = str(uuid.uuid4())
    _plan(db_path, pid)
    _install_probe(monkeypatch)
    first = _post(pid, ack=False, key="k-deleted-1")
    assert first.status_code == 202, _resp_json(first)
    job_id = _resp_json(first)["id"]

    # Plan no longer loads as eligible (retention/tamper): the recorded
    # job still exists and must replay. The jobs->plans FK forbids a
    # hard delete, so invalidate the immutable hash instead.
    conn = db_lib.connect(db_path)
    try:
        conn.execute("UPDATE plans SET plan_hash='tampered-hash'"
                     " WHERE id=?", (pid,))
        conn.commit()
    finally:
        conn.close()

    probe = _install_probe(monkeypatch)
    retry = _post(pid, ack=False, key="k-deleted-1")
    body = _resp_json(retry)
    assert retry.status_code == 200, body
    assert body.get("id") == job_id, body
    assert body.get("replayed") is True, body
    assert probe["calls"] == 0, "replay must not touch plan/probe gates"


# ---------------------------------------------------------------------------
# 9. an idempotency key never crosses subject (owner) boundaries
# ---------------------------------------------------------------------------

def test_idempotency_key_does_not_cross_subject(tmp_path, monkeypatch):
    _state_dir, db_path = _setup(tmp_path, monkeypatch)
    pid_a = str(uuid.uuid4())
    pid_b = str(uuid.uuid4())
    _plan(db_path, pid_a, subject="owner@example.invalid")
    _plan(db_path, pid_b, subject="other@example.invalid")
    _install_probe(monkeypatch)
    first = _post(pid_a, ack=False, key="k-shared-1")
    assert first.status_code == 202, _resp_json(first)
    job_a = _resp_json(first)["id"]

    # Free the single slot (subject B must not inherit A's key).
    from backend.app import tx as tx_lib
    conn = db_lib.connect(db_path)
    try:
        jobs_lib.transition(conn, job_a, "succeeded", step="succeeded",
                            exit_code=0, after_version="9.9.9")
        conn.commit()
        tx_lib.release_ownership(
            conn, job_a, expect_states=["succeeded"],
            event="ownership_released",
            event_detail="test quiescence proven")
    finally:
        conn.close()

    monkeypatch.setattr(
        deps_lib, "authenticate",
        lambda request: ({"email": "other@example.invalid"}, None,
                         "test-rid"))
    second = _post(pid_b, ack=False, key="k-shared-1")
    body = _resp_json(second)
    assert second.status_code == 202, body
    job_b = body.get("id", "")
    assert job_b and job_b != job_a, body
    rows = _jobs_by_key(db_path, "k-shared-1")
    assert len(rows) == 2, rows


# ---------------------------------------------------------------------------
# 10. lookup_replay seam contract + fail-closed behavior
# ---------------------------------------------------------------------------

def test_lookup_replay_seam_contract(tmp_path, monkeypatch):
    _state_dir, db_path = _setup(tmp_path, monkeypatch)
    pid = str(uuid.uuid4())
    _plan(db_path, pid)
    conn = db_lib.connect(db_path)
    try:
        job_id = support_lib.admit_new(
            conn, subject="owner@example.invalid", plan_id=pid,
            fingerprint="fp-replay-1", idem_key="k-seam-1")
        assert admission_lib.lookup_replay(
            conn, "owner@example.invalid", "k-seam-1", pid, False) == \
            (job_id, "")
        assert admission_lib.lookup_replay(
            conn, "owner@example.invalid", "k-seam-1", pid, True) == \
            ("", "conflict")
        assert admission_lib.lookup_replay(
            conn, "owner@example.invalid", "k-unknown", pid, False) == \
            ("", admission_lib.REPLAY_NONE)
        assert admission_lib.lookup_replay(
            conn, "", "k-seam-1", pid, False) == \
            ("", "invalid_request")
    finally:
        conn.close()
    conn2 = db_lib.connect(db_path)
    conn2.close()
    # A closed connection must fail closed, never masquerade as no_replay.
    assert admission_lib.lookup_replay(
        conn2, "owner@example.invalid", "k-seam-1", pid, False) == \
        ("", "unavailable")


def test_replay_lookup_failure_returns_503_not_500(tmp_path, monkeypatch):
    _state_dir, db_path = _setup(tmp_path, monkeypatch)
    pid = str(uuid.uuid4())
    _plan(db_path, pid)
    _install_probe(monkeypatch)
    first = _post(pid, ack=False, key="k-boom-1")
    assert first.status_code == 202, _resp_json(first)

    def _boom(*args, **kwargs):
        raise RuntimeError("replay lookup exploded")

    monkeypatch.setattr(admission_lib, "lookup_replay", _boom)
    retry = _post(pid, ack=False, key="k-boom-1")
    assert retry.status_code == 503, _resp_json(retry)
    assert _resp_json(retry).get("code") == "unavailable"
