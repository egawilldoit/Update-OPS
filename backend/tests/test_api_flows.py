"""API flow tests for the corrective implementation pass (offline, no server).

Covers the audit findings without executing servers, builds, or live probes:
- plan-unknown-without-ack then job-requires-ack matrix (fakes, no server)
- fingerprint-column compare (tools.fingerprint, not install_identity)
- drain 503s for mutations, reads unaffected
- heartbeat fresh/stale mapping via jobs.read_dispatcher_heartbeat
- has_more vs truncated split in _read_log_page
- discovery coalesce (cache hit skips probes, ?force=1 bypasses,
  active-job skips probes)
- check-again persists verify health (pass/fail/unknown + exception preserve)

Style: plain pytest functions with tmp_path + monkeypatch. Stdlib plus
backend imports only. Function-level fakes for adapters and auth; no HTTP
server, no subprocess. Python 3.10 compatible. Write only; never runs
live probes.
"""
from __future__ import annotations

import asyncio
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
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from backend.app import db as db_lib
from backend.app import jobs as jobs_lib
from backend.app.admission import admit as admit_lib
from backend.app.api import deps as deps_lib
from backend.app.api import routes as routes_lib
from backend.app.config import settings as settings_lib

import support as support_lib


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class _FakeRequest(object):
    """Minimal stand-in for a FastAPI Request (auth is monkeypatched)."""

    def __init__(self, headers=None, query=None, body=b""):
        # Starlette headers are case-insensitive; routes use lower-case
        # lookups, so normalise to lower-case keys.
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
    """Bypass Cloudflare JWT + mutation guards for function-level tests."""
    monkeypatch.setattr(
        deps_lib, "authenticate",
        lambda request: ({"email": "owner@example.invalid"}, None,
                         "test-rid"))
    monkeypatch.setattr(
        deps_lib, "require_mutation_guards", lambda request, claims: None)
    monkeypatch.setattr(deps_lib, "check_rate_limit",
                         lambda ident, kind="read": True)


def _isolate_settings(monkeypatch, tmp_path):
    """Point state/db/logs at tmp; clear discovery cache; fresh heartbeat;
    disposable release root (EGA_RELEASE_ROOT) so resolve_release works."""
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
    monkeypatch.setattr(settings_lib, "secrets_file",
                        str(tmp_path / "missing-secrets.env"))
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
    try:
        routes_lib._DISCOVERY_CACHE.clear()
    except Exception:
        pass
    try:
        with routes_lib._DISCOVERY_LOCKS_GUARD:
            routes_lib._DISCOVERY_INFLIGHT.clear()
    except Exception:
        pass
    return state_dir, db_path, log_dir


def _ns_to_dict(value):
    """Recursive SimpleNamespace/list/dict -> plain JSON dicts."""
    if isinstance(value, types.SimpleNamespace):
        return {k: _ns_to_dict(v) for k, v in vars(value).items()}
    if isinstance(value, dict):
        return {k: _ns_to_dict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_ns_to_dict(v) for v in value]
    return value


def _probe_fake(monkeypatch, fake):
    """Patch the owner probe queue with a fake owner (R01 test seam).

    Counts the same probe calls as the legacy adapter fake; raising
    adapter methods surface as ("error", ...) like a failed probe.
    """
    async def _fake_owner_probe(tool_id, op, timeout_s=25.0):
        try:
            if op == "refresh":
                return "ok", {
                    "inspection": _ns_to_dict(fake.inspect()),
                    "discovery": _ns_to_dict(fake.discover()),
                    "activity": _ns_to_dict(fake.activity()),
                    "verification": _ns_to_dict(fake.verify()),
                }
            if op == "plan":
                return "ok", {
                    "planned": _ns_to_dict(fake.plan()),
                    "activity": _ns_to_dict(fake.activity()),
                    "inspection": _ns_to_dict(fake.inspect()),
                }
            if op == "inspect":
                return "ok", _ns_to_dict(fake.inspect())
            return "error", {"reason": "unsupported op in test"}
        except Exception as exc:
            return "error", {"reason": "probe failed: %s" % exc}
    monkeypatch.setattr(routes_lib, "_owner_probe", _fake_owner_probe)
    return fake


def _make_db(db_path):
    conn = db_lib.connect(db_path)
    db_lib.migrate(conn)
    conn.commit()
    return conn


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


class _FakeAdapterBase(object):
    """Read-only fake: counts probe calls; never touches subprocesses."""

    def __init__(self, fingerprint="fp-fake-1", activity_state="idle",
                 verify_passed=True, verify_checks=None):
        self.calls = {"inspect": 0, "discover": 0, "activity": 0,
                      "plan": 0, "verify": 0}
        self._fp = fingerprint
        self._activity_state = activity_state
        self._verify_passed = verify_passed
        self._verify_checks = verify_checks

    def inspect(self):
        self.calls["inspect"] += 1
        return types.SimpleNamespace(
            install_identity="display-human-readable-identity",
            version="1.0.0",
            channel="test-channel",
            fingerprint=self._fp)

    def discover(self):
        self.calls["discover"] += 1
        return types.SimpleNamespace(
            target="9.9.9", target_mode="exact", channel="test-channel",
            available=True, unknown_reason="")

    def activity(self):
        self.calls["activity"] += 1
        return types.SimpleNamespace(
            state=self._activity_state,
            evidence="evidence-%s" % self._activity_state)

    def plan(self):
        self.calls["plan"] += 1
        return types.SimpleNamespace(
            fingerprint=self._fp, target="9.9.9", target_mode="exact",
            channel="test-channel", services=[], backup_scope={},
            required_space_bytes=1024, steps=["preflight", "backup",
                                              "updating", "verifying"],
            timeouts={}, restart_impact="none",
            required_checks=["smoke"], budgets={"/tmp": 2048},
            deadlines={"updating": 60})

    def verify(self):
        self.calls["verify"] += 1
        if self._verify_checks is not None:
            checks = self._verify_checks
        elif self._verify_passed:
            checks = [types.SimpleNamespace(name="smoke", result="pass",
                                            mandatory=True, summary="ok")]
        else:
            checks = [types.SimpleNamespace(name="smoke", result="fail",
                                            mandatory=True, summary="bad")]
        return types.SimpleNamespace(tool="hermes", version="1.0.0",
                                     checks=checks,
                                     passed=bool(self._verify_passed),
                                     error_code="" if self._verify_passed
                                     else "health_failed",
                                     error_detail="")


# ---------------------------------------------------------------------------
# 1. plan-unknown-without-ack then job-requires-ack matrix
# ---------------------------------------------------------------------------

def test_plan_unknown_without_ack_then_job_requires_ack(tmp_path,
                                                        monkeypatch):
    _auth_ok(monkeypatch)
    _state_dir, db_path, _log_dir = _isolate_settings(monkeypatch, tmp_path)
    _make_db(db_path).close()
    fake = _FakeAdapterBase(fingerprint="fp-ack-1",
                            activity_state="unknown",
                            verify_passed=True)
    _probe_fake(monkeypatch, fake)

    # POST /tools/{id}/plans with unknown activity and NO ack body must
    # still return 201 and record activity_state=unknown on the plan.
    req = _FakeRequest(
        headers={"content-type": "application/json",
                 "origin": "x", "x-csrf-token": "x"},
        body=b"{}")
    resp = _run(routes_lib.post_tool_plan("hermes", req))
    assert resp.status_code == 201, _resp_json(resp)
    plan = _resp_json(resp)
    assert plan.get("activity_state") == "unknown"
    plan_id = plan.get("id")
    assert plan_id

    def _post_job(payload):
        body = json.dumps(payload).encode("utf-8")
        req = _FakeRequest(
            headers={"content-type": "application/json",
                     "origin": "x", "x-csrf-token": "x",
                     "idempotency-key": "k-%s" % uuid.uuid4().hex[:8]},
            body=body)
        return _run(routes_lib.post_job(req))

    # Unknown-activity plan + activity_ack=false -> 409 ack_required.
    r_noack = _post_job({"plan_id": plan_id, "activity_ack": False})
    assert r_noack.status_code == 409, _resp_json(r_noack)
    assert _resp_json(r_noack).get("code") == "ack_required"
    # Unknown-activity plan + activity_ack=true -> 202 new job.
    r_ack = _post_job({"plan_id": plan_id, "activity_ack": True})
    assert r_ack.status_code == 202, _resp_json(r_ack)

    # Idle-activity plan needs no ack at either layer.
    fake2 = _FakeAdapterBase(fingerprint="fp-ack-2",
                             activity_state="idle",
                             verify_passed=True)
    _probe_fake(monkeypatch, fake2)
    # Free the single slot: finish the acked job first. Terminal
    # outcome alone does NOT free it (T08) — the lease releases only
    # via release_ownership after quiescence proof, modeled here.
    from backend.app import tx as tx_lib
    conn = db_lib.connect(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
        if row is not None:
            jobs_lib.transition(conn, row["id"], "succeeded",
                                step="succeeded", exit_code=0,
                                after_version="9.9.9")
            conn.commit()
            tx_lib.release_ownership(
                conn, row["id"], expect_states=["succeeded"],
                event="ownership_released",
                event_detail="test quiescence proven")
    finally:
        conn.close()
    req2 = _FakeRequest(
        headers={"content-type": "application/json",
                 "origin": "x", "x-csrf-token": "x"},
        body=b"{}")
    resp2 = _run(routes_lib.post_tool_plan("hermes", req2))
    assert resp2.status_code == 201, _resp_json(resp2)
    plan2 = _resp_json(resp2)
    assert plan2.get("activity_state") == "idle"
    r_idle = _post_job({"plan_id": plan2["id"], "activity_ack": False})
    assert r_idle.status_code == 202, _resp_json(r_idle)


def test_plan_never_requires_ack_even_with_unknown():
    # Static guard: plans handler must not contain an ack_required 409 for
    # unknown activity (ack lives at POST /jobs only).
    import inspect
    src = inspect.getsource(routes_lib.post_tool_plan)
    assert "ack_required" not in src


# ---------------------------------------------------------------------------
# 2. fingerprint-column compare (not install_identity)
# ---------------------------------------------------------------------------

def test_fingerprint_column_compare(tmp_path, monkeypatch):
    from backend.app import plans as plans_lib
    from backend.app.inventory import config_identity
    from backend.app.owner_env import resolve_release
    _auth_ok(monkeypatch)
    _state_dir, db_path, _log_dir = _isolate_settings(monkeypatch, tmp_path)
    conn = _make_db(db_path)
    # tools.fingerprint is the adapter fingerprint; install_identity is a
    # human display string that must be ignored for the comparison.
    conn.execute("INSERT OR IGNORE INTO tools(id) VALUES('hermes')")
    conn.execute(
        "UPDATE tools SET fingerprint=?, install_identity=? WHERE id=?",
        ("fp-real-1", "display-human-readable-identity", "hermes"))
    conn.commit()
    # v2 immutable plans carrying the real config/release binding.
    now = datetime.now(timezone.utc)
    exp = (now + timedelta(seconds=600)).isoformat()
    cfg_hash = config_identity(settings_lib)
    release = resolve_release()
    assert cfg_hash and release
    from backend.app.owner_env import canonical_fingerprint
    envfp = canonical_fingerprint(settings_lib, None, release)
    assert envfp
    plan_match = plans_lib.build_plan_row(
        tool_id="hermes", subject="owner@example.invalid",
        install_identity="display-human-identity",
        fingerprint="fp-real-1", target="9.9.9", target_mode="exact",
        channel="c", services=[], launch={}, state_homes=[],
        backup_scope={}, backup_policy={}, required_probes=[],
        required_checks=["smoke"], budgets={}, space_fs={},
        steps=["preflight"], deadlines={"preflight": 120},
        restart_impact="none", restart_detail="", activity_state="idle",
        activity_ts=now.isoformat(), activity_evidence="",
        required_space_bytes=1024, config_hash=cfg_hash,
        release_path=release, created_at=now.isoformat(),
        expires_at=exp, artifact={}, env_fingerprint=envfp)
    plan_changed = plans_lib.build_plan_row(
        tool_id="hermes", subject="owner@example.invalid",
        install_identity="display-human-identity",
        fingerprint="fp-other-2", target="9.9.9", target_mode="exact",
        channel="c", services=[], launch={}, state_homes=[],
        backup_scope={}, backup_policy={}, required_probes=[],
        required_checks=["smoke"], budgets={}, space_fs={},
        steps=["preflight"], deadlines={"preflight": 120},
        restart_impact="none", restart_detail="", activity_state="idle",
        activity_ts=now.isoformat(), activity_evidence="",
        required_space_bytes=1024, config_hash=cfg_hash,
        release_path=release, created_at=now.isoformat(),
        expires_at=exp, artifact={}, env_fingerprint=envfp)
    conn.execute("BEGIN IMMEDIATE")
    plans_lib.insert_plan(conn, plan_match)
    plans_lib.insert_plan(conn, plan_changed)
    conn.commit()
    conn.close()
    # Owner revalidation returns the CURRENT installation fingerprint.
    fake = _FakeAdapterBase(fingerprint="fp-real-1",
                            activity_state="idle",
                            verify_passed=True)
    _probe_fake(monkeypatch, fake)

    def _post_job(plan_id):
        body = json.dumps(
            {"plan_id": plan_id, "activity_ack": False}).encode("utf-8")
        req = _FakeRequest(
            headers={"content-type": "application/json",
                     "origin": "x", "x-csrf-token": "x",
                     "idempotency-key": "k-%s" % uuid.uuid4().hex[:8]},
            body=body)
        return _run(routes_lib.post_job(req))

    # Matching fingerprint succeeds even though install_identity differs
    # from the plan fingerprint (display string is ignored); the owner
    # revalidation probe authoritatively confirms the installation.
    ok = _post_job(plan_match["id"])
    assert ok.status_code == 202, _resp_json(ok)
    # Changed adapter fingerprint -> 409 fingerprint_changed.
    conn2 = db_lib.connect(db_path)
    try:
        row = conn2.execute(
            "SELECT id FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
        jobs_lib.transition(conn2, row["id"], "succeeded",
                            step="succeeded", exit_code=0,
                            after_version="9.9.9")
        conn2.commit()
    finally:
        conn2.close()
    bad = _post_job(plan_changed["id"])
    assert bad.status_code == 409, _resp_json(bad)
    assert _resp_json(bad).get("code") == "fingerprint_changed"


# ---------------------------------------------------------------------------
# 3. drain 503s mutations, reads unaffected
# ---------------------------------------------------------------------------

def test_drain_blocks_plans_and_jobs_not_reads(tmp_path, monkeypatch):
    _auth_ok(monkeypatch)
    state_dir, db_path, _log_dir = _isolate_settings(monkeypatch, tmp_path)
    _make_db(db_path).close()
    fake = _FakeAdapterBase()
    _probe_fake(monkeypatch, fake)
    assert routes_lib._drained() is False

    drain_path = os.path.join(state_dir, "drain")
    with open(drain_path, "w", encoding="utf-8") as fh:
        fh.write("maintenance\n")
    assert routes_lib._drained() is True

    plan_req = _FakeRequest(
        headers={"content-type": "application/json",
                 "origin": "x", "x-csrf-token": "x"},
        body=b"{}")
    plan_resp = _run(routes_lib.post_tool_plan("hermes", plan_req))
    assert plan_resp.status_code == 503, _resp_json(plan_resp)
    assert _resp_json(plan_resp).get("code") == "maintenance"

    job_req = _FakeRequest(
        headers={"content-type": "application/json",
                 "origin": "x", "x-csrf-token": "x",
                 "idempotency-key": "drain-key-1"},
        body=json.dumps(
            {"plan_id": str(uuid.uuid4()),
             "activity_ack": False}).encode("utf-8"))
    job_resp = _run(routes_lib.post_job(job_req))
    # T09: unknown plan under drain is refused without mutation — the
    # contract pins refusal (4xx) with no job, lease, or plan side
    # effects, not one obsolete status string.
    assert job_resp.status_code in (404, 409, 503), \
        _resp_json(job_resp)
    check = db_lib.connect(db_path)
    try:
        assert check.execute(
            "SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 0
        assert check.execute(
            "SELECT COUNT(*) AS n FROM execution_leases WHERE"
            " released_at=''").fetchone()["n"] == 0
    finally:
        check.close()

    # Reads keep working while drained.
    read_req = _FakeRequest()
    tools_resp = routes_lib.get_tools(read_req)
    assert tools_resp.status_code == 200
    health_resp = routes_lib.get_health(read_req)
    assert health_resp.status_code == 200

    os.remove(drain_path)
    assert routes_lib._drained() is False


# ---------------------------------------------------------------------------
# 4. heartbeat fresh/stale mapping
# ---------------------------------------------------------------------------

def test_heartbeat_fresh_stale_and_health_mapping(tmp_path, monkeypatch):
    from backend.app.schemas import utcnow_iso
    _auth_ok(monkeypatch)
    state_dir, db_path, _log_dir = _isolate_settings(monkeypatch, tmp_path)
    _make_db(db_path).close()
    hb_path = os.path.join(state_dir, "dispatcher.heartbeat")
    # Start missing (helper pre-creates a fresh one for other tests).
    try:
        os.remove(hb_path)
    except OSError:
        pass
    # Missing -> {} -> worker down.
    assert jobs_lib.read_dispatcher_heartbeat(state_dir) == {}
    health_missing = routes_lib.get_health(_FakeRequest())
    assert _resp_json(health_missing).get("worker") == "down"
    assert "recovery_required" in _resp_json(health_missing)
    # Fresh -> ok.
    with open(hb_path, "w", encoding="utf-8") as fh:
        json.dump({"ts": utcnow_iso(), "pid": 4242}, fh)
    fresh = jobs_lib.read_dispatcher_heartbeat(state_dir, max_age_s=20)
    assert fresh and fresh.get("pid") == 4242
    health_fresh = routes_lib.get_health(_FakeRequest())
    assert _resp_json(health_fresh).get("worker") == "ok"
    # Stale -> {} -> down (never ok from absence of jobs).
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    with open(hb_path, "w", encoding="utf-8") as fh:
        json.dump({"ts": old, "pid": 1}, fh)
    assert jobs_lib.read_dispatcher_heartbeat(state_dir,
                                              max_age_s=20) == {}
    health_stale = routes_lib.get_health(_FakeRequest())
    assert _resp_json(health_stale).get("worker") == "down"
    # Unparseable -> down.
    with open(hb_path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert jobs_lib.read_dispatcher_heartbeat(state_dir) == {}
    assert _resp_json(
        routes_lib.get_health(_FakeRequest())).get("worker") == "down"


# ---------------------------------------------------------------------------
# 5. has_more vs truncated split
# ---------------------------------------------------------------------------

def test_log_has_more_vs_truncated_split(tmp_path, monkeypatch):
    _state_dir, _db_path, log_dir = _isolate_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(settings_lib, "per_job_log_cap_bytes",
                        20 * 1024 * 1024)
    # G05: explicit secrets file (a missing source would fail closed
    # instead of serving weakened re-redaction).
    _secrets_path = str(tmp_path / "test-secrets.env")
    with open(_secrets_path, "w", encoding="utf-8") as _sfh:
        _sfh.write("TEST_DUMMY_SECRET_KEY=dummy-secret-value-12345\n")
    monkeypatch.setattr(settings_lib, "secrets_file", _secrets_path)
    job_id = str(uuid.uuid4())
    path = os.path.join(log_dir, job_id + ".jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for seq in range(10):
            fh.write(json.dumps(
                {"seq": seq, "ts": "2026-09-08T00:00:%02d+00:00" % seq,
                 "stream": "stdout", "line": "line-%d" % seq}) + "\n")
    # Pagination continuation reports has_more, never truncated.
    p1 = routes_lib._read_log_page(job_id, 0, 4)
    assert [r["seq"] for r in p1["records"]] == [1, 2, 3, 4]
    assert p1["has_more"] is True
    assert p1["truncated"] is False
    p2 = routes_lib._read_log_page(job_id, p1["next_after"], 4)
    assert [r["seq"] for r in p2["records"]] == [5, 6, 7, 8]
    assert p2["has_more"] is True and p2["truncated"] is False
    p3 = routes_lib._read_log_page(job_id, p2["next_after"], 4)
    assert [r["seq"] for r in p3["records"]] == [9]
    assert p3["has_more"] is False and p3["truncated"] is False
    # User output containing "truncate" never flags storage-cap loss.
    job2 = str(uuid.uuid4())
    with open(os.path.join(log_dir, job2 + ".jsonl"), "w",
              encoding="utf-8") as fh:
        fh.write(json.dumps(
            {"seq": 1, "ts": "2026-09-08T00:00:00+00:00",
             "stream": "stdout",
             "line": "please truncate this table"}) + "\n")
    assert routes_lib._read_log_page(job2, 0, 10)["truncated"] is False
    # Only the runner marker (byte-identical) flags truncated.
    with open(os.path.join(log_dir, job2 + ".jsonl"), "a",
              encoding="utf-8") as fh:
        fh.write(json.dumps(
            {"seq": 2, "ts": "2026-09-08T00:00:01+00:00",
             "stream": "event",
             "line": routes_lib.LOG_TRUNCATION_MARKER}) + "\n")
    capped = routes_lib._read_log_page(job2, 0, 10)
    assert capped["truncated"] is True
    assert routes_lib.LOG_TRUNCATION_MARKER == (
        "[truncated: per-job log cap reached; "
        "draining without persisting]")


# ---------------------------------------------------------------------------
# 6. discovery coalesce (cache hit, force bypass, active-job skips probes)
# ---------------------------------------------------------------------------

def _check_req(force=None):
    query = {}
    if force is not None:
        query["force"] = force
    return _FakeRequest(
        headers={"content-type": "application/json",
                 "origin": "x", "x-csrf-token": "x"},
        query=query, body=b"{}")


def test_discovery_coalesce_cache_hit_force_and_active_gate(
        tmp_path, monkeypatch):
    _auth_ok(monkeypatch)
    _state_dir, db_path, _log_dir = _isolate_settings(monkeypatch, tmp_path)
    _make_db(db_path).close()
    fake = _FakeAdapterBase()
    _probe_fake(monkeypatch, fake)

    first = _run(routes_lib.post_tool_check("hermes", _check_req()))
    assert first.status_code == 200
    assert fake.calls["inspect"] == 1 and fake.calls["verify"] == 1

    # Fresh cache hit (<900s) skips adapter probes.
    second = _run(routes_lib.post_tool_check("hermes", _check_req()))
    assert second.status_code == 200
    assert fake.calls["inspect"] == 1 and fake.calls["verify"] == 1

    # ?force=1 bypasses the cache and probes again.
    forced = _run(routes_lib.post_tool_check("hermes", _check_req(force="1")))
    assert forced.status_code == 200
    assert fake.calls["inspect"] == 2 and fake.calls["verify"] == 2

    # Invalid force values are 422.
    bad = _run(routes_lib.post_tool_check("hermes", _check_req(force="yes")))
    assert bad.status_code == 422

    # Active job (or recovery) skips probes even with ?force=1.
    before = dict(fake.calls)
    conn = db_lib.connect(db_path)
    try:
        support_lib.test_release_root()
        _prow = support_lib.v2_plan_row(
            conn, str(uuid.uuid4()), subject="owner@example.invalid",
            fingerprint="fp-x", activity_state="idle")
        _jid, _created, _err = admit_lib(
            conn, "owner@example.invalid", "coalesce-key-1",
            _prow["id"], False, "fp-x", True, False)
        assert _err == "", _err
    finally:
        conn.close()
    gated = _run(routes_lib.post_tool_check("hermes", _check_req(force="1")))
    assert gated.status_code == 200
    assert fake.calls == before
    body = _resp_json(gated)
    assert "updating" in (body.get("health_detail") or "") or \
        "stale" in (body.get("health_detail") or "")


# ---------------------------------------------------------------------------
# 7. check-again persists verify health
# ---------------------------------------------------------------------------

def test_check_again_persists_verify_health(tmp_path, monkeypatch):
    _auth_ok(monkeypatch)
    _state_dir, db_path, _log_dir = _isolate_settings(monkeypatch, tmp_path)
    _make_db(db_path).close()

    passing = _FakeAdapterBase(verify_passed=True)
    _probe_fake(monkeypatch, passing)
    resp = _run(routes_lib.post_tool_check("hermes", _check_req(force="1")))
    assert resp.status_code == 200
    card = _resp_json(resp)
    assert card.get("health") == "healthy"
    assert card.get("health_detail")
    conn = db_lib.connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM tools WHERE id='hermes'").fetchone()
        assert dict(row)["health"] == "healthy"
        assert dict(row)["fingerprint"] == "fp-fake-1"
    finally:
        conn.close()

    failing = _FakeAdapterBase(verify_passed=False)
    _probe_fake(monkeypatch, failing)
    resp2 = _run(routes_lib.post_tool_check("hermes", _check_req(force="1")))
    assert _resp_json(resp2).get("health") == "unhealthy"

    unknown_checks = [types.SimpleNamespace(name="probe",
                                            result="unknown",
                                            mandatory=True,
                                            summary="inconclusive")]
    unknown = _FakeAdapterBase(verify_passed=False,
                               verify_checks=unknown_checks)
    _probe_fake(monkeypatch, unknown)
    resp3 = _run(routes_lib.post_tool_check("hermes", _check_req(force="1")))
    assert _resp_json(resp3).get("health") == "unknown"

    # Verify exception preserves the last observation with discovery_error.
    class _BoomVerify(_FakeAdapterBase):
        def verify(self):
            self.calls["verify"] += 1
            raise RuntimeError("verify probe exploded")

    boom = _BoomVerify()
    _probe_fake(monkeypatch, boom)
    conn = db_lib.connect(db_path)
    try:
        before = dict(conn.execute(
            "SELECT * FROM tools WHERE id='hermes'").fetchone())
    finally:
        conn.close()
    resp4 = _run(routes_lib.post_tool_check("hermes", _check_req(force="1")))
    assert resp4.status_code == 200
    conn = db_lib.connect(db_path)
    try:
        after = dict(conn.execute(
            "SELECT * FROM tools WHERE id='hermes'").fetchone())
    finally:
        conn.close()
    assert after["health"] == before["health"]
    assert after["observed_version"] == before["observed_version"]
    assert "exploded" in (after["discovery_error"] or "")
