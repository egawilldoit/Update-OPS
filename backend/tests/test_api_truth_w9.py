"""W9 API truth regression tests (backend contract gaps).

Two read-only API contract gaps are pinned here:

A1. GET /api/v1/health reports the maintenance/drain fact DIRECTLY as the
    canonical boolean field ``drain``, read from the actual
    ``<state_dir>/drain`` marker. Drain is not a system failure: it never
    changes ``api``/``database``/``worker`` and it never clears
    ``recovery_required``. The frontend maintenance banner consumes this
    direct health truth (``maintenanceFromHealth`` reads ``health.drain``).

A2. GET /api/v1/probes/{request_id} is subject-bound: a durable probe
    handle is readable only by the authenticated subject recorded at
    enqueue, using the same normalized identity semantics
    (``deps.subject_of``: trimmed/lower-cased email) that plan/job/probe
    enqueue paths use. A mismatched subject fails closed with the EXACT
    unknown-probe 404 envelope (no cross-subject existence leak). Legacy
    rows with an empty or pre-W9 shared (``"api"``) subject fail closed:
    they are never transferable to an authenticated subject. Malformed
    UUIDs and unknown probes are unchanged; auth + read rate-limit
    semantics are unchanged.

Hermetic: tmp sqlite DB + tmp state dir, monkeypatched auth/heartbeat and
function-level fakes. No test touches real systemd, /opt, /etc, /var/lib,
the network, or production state.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import uuid

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from backend.app import db as db_lib
from backend.app import owner_probes as probes_lib
from backend.app.api import deps as deps_lib
from backend.app.api import routes as routes_lib
from backend.app.config import settings as settings_lib
from backend.app.schemas import utcnow_iso

import support as support_lib

OWNER = "owner@example.invalid"


# ---------------------------------------------------------------------------
# fakes + helpers
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


def _auth_ok(monkeypatch, email=OWNER):
    """Bypass Cloudflare JWT + mutation guards; keep subject_of real."""
    monkeypatch.setattr(
        deps_lib, "authenticate",
        lambda request: ({"email": email}, None, "test-rid"))
    monkeypatch.setattr(
        deps_lib, "require_mutation_guards", lambda request, claims: None)
    monkeypatch.setattr(deps_lib, "check_rate_limit",
                        lambda ident, kind="read": True)


def _isolate_settings(monkeypatch, tmp_path):
    """Point state/db/logs at tmp; fresh heartbeat; disposable release root."""
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
    monkeypatch.setenv("EGA_RELEASE_ROOT", release_dir)
    try:
        with open(os.path.join(state_dir, "dispatcher.heartbeat"), "w",
                  encoding="utf-8") as fh:
            json.dump({"ts": utcnow_iso(), "pid": 4242}, fh)
    except Exception:
        pass
    try:
        routes_lib._DISCOVERY_CACHE.clear()
    except Exception:
        pass
    return state_dir, db_path, log_dir


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


def _check_req():
    return _FakeRequest(
        headers={"content-type": "application/json", "origin": "x",
                 "x-csrf-token": "x"},
        body=b"{}")


def _enqueue(conn, subject, tool_id="hermes", op="refresh", coalesce=True):
    return probes_lib.enqueue_probe(conn, subject, tool_id, op,
                                    coalesce=coalesce)


def _drain_path(state_dir):
    return os.path.join(state_dir, "drain")


def _touch_drain(state_dir):
    with open(_drain_path(state_dir), "w", encoding="utf-8") as fh:
        fh.write("maintenance\n")


def _remove_drain(state_dir):
    try:
        os.remove(_drain_path(state_dir))
    except OSError:
        pass


def _repo_read(*parts):
    with open(os.path.join(_REPO_ROOT, *parts), "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# A1. GET /health reports drain directly
# ---------------------------------------------------------------------------

def test_health_drain_absent_reports_false(tmp_path, monkeypatch):
    """No drain marker -> the canonical boolean is present and false."""
    _auth_ok(monkeypatch)
    state_dir, db_path, _logs = _isolate_settings(monkeypatch, tmp_path)
    _make_db(db_path).close()
    assert routes_lib._drained() is False
    body = _resp_json(routes_lib.get_health(_FakeRequest()))
    assert body.get("drain") is False, body
    # One canonical field: no alias (the frontend prefers `drain`).
    assert "maintenance" not in body, body


def test_health_drain_present_reports_true(tmp_path, monkeypatch):
    """Drain marker present -> health says so directly, before any refusal."""
    _auth_ok(monkeypatch)
    state_dir, db_path, _logs = _isolate_settings(monkeypatch, tmp_path)
    _make_db(db_path).close()
    _touch_drain(state_dir)
    assert routes_lib._drained() is True
    body = _resp_json(routes_lib.get_health(_FakeRequest()))
    assert body.get("drain") is True, body


def test_health_recovery_required_and_drain_are_independent(
        tmp_path, monkeypatch):
    """Both facts are reported independently; drain=false never clears
    recovery_required and recovery_required never masks drain."""
    _auth_ok(monkeypatch)
    state_dir, db_path, _logs = _isolate_settings(monkeypatch, tmp_path)
    conn = _make_db(db_path)
    plan = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    conn.execute(
        "INSERT INTO jobs(id,tool_id,plan_id,subject,idempotency_key,"
        "request_hash,state,step,created_at,recovery_required)"
        " VALUES(?,?,?,?,?,?,?,?,?,1)",
        (uuid.uuid4().hex, "hermes", plan["id"], OWNER, "w9-key",
         "w9-hash", "failed", "", utcnow_iso()))
    conn.commit()
    conn.close()
    first = _resp_json(routes_lib.get_health(_FakeRequest()))
    assert first.get("recovery_required") is True, first
    assert first.get("drain") is False, first
    _touch_drain(state_dir)
    second = _resp_json(routes_lib.get_health(_FakeRequest()))
    assert second.get("recovery_required") is True, second
    assert second.get("drain") is True, second
    _remove_drain(state_dir)
    third = _resp_json(routes_lib.get_health(_FakeRequest()))
    assert third.get("recovery_required") is True, third
    assert third.get("drain") is False, third


def test_health_api_database_worker_unaffected_by_drain(tmp_path, monkeypatch):
    """Drain is an independent operational fact, not a failure: the
    api/database/worker fields keep their exact mapping while drained."""
    _auth_ok(monkeypatch)
    state_dir, db_path, _logs = _isolate_settings(monkeypatch, tmp_path)
    _make_db(db_path).close()
    before = _resp_json(routes_lib.get_health(_FakeRequest()))
    assert (before.get("api"), before.get("database"),
            before.get("worker")) == ("ok", "ok", "ok"), before
    _touch_drain(state_dir)
    after = _resp_json(routes_lib.get_health(_FakeRequest()))
    assert (after.get("api"), after.get("database"),
            after.get("worker")) == ("ok", "ok", "ok"), after
    assert after.get("drain") is True, after
    # Database failure is still reported honestly (with drain true) and is
    # never suppressed by maintenance mode.
    def _db_down():
        raise sqlite3.OperationalError("db unavailable")

    monkeypatch.setattr(routes_lib, "_db", _db_down)
    down = _resp_json(routes_lib.get_health(_FakeRequest()))
    assert (down.get("api"), down.get("database"),
            down.get("worker")) == ("degraded", "down", "down"), down
    assert down.get("drain") is True, down


def test_health_authenticated_and_no_store(tmp_path, monkeypatch):
    """Health stays authenticated and no-store (read-only truth only)."""
    _state_dir, db_path, _logs = _isolate_settings(monkeypatch, tmp_path)
    _make_db(db_path).close()
    unauthenticated = routes_lib.get_health(_FakeRequest())
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers.get("cache-control") == "no-store"
    _auth_ok(monkeypatch)
    authenticated = routes_lib.get_health(_FakeRequest())
    assert authenticated.status_code == 200
    assert authenticated.headers.get("cache-control") == "no-store"
    assert "drain" in _resp_json(authenticated)


def test_frontend_maintenance_banner_consumes_direct_health_truth():
    """W8 frontend already reads the direct health flag: no redesign."""
    client = _repo_read("frontend", "src", "api", "client.ts")
    assert "drain?: boolean" in client
    assert "/health" in client
    operational = _repo_read("frontend", "src", "operational.ts")
    assert "maintenanceFromHealth" in operational
    assert "health.drain" in operational


# ---------------------------------------------------------------------------
# A2. GET /probes/{request_id} is subject-bound
# ---------------------------------------------------------------------------

def test_owner_reads_own_probe_with_normalized_subject(tmp_path, monkeypatch):
    """Owner casing differences never deny the owner: both the stored row
    and the authenticated claims normalize through subject_of."""
    _auth_ok(monkeypatch, email="Owner@Example.INVALID")
    _state_dir, db_path, _logs = _isolate_settings(monkeypatch, tmp_path)
    conn = _make_db(db_path)
    rid = _enqueue(conn, OWNER)
    resp = routes_lib.get_probe(rid, _FakeRequest())
    assert resp.status_code == 200
    body = _resp_json(resp)
    assert body.get("request_id") == rid
    assert body.get("state") == "queued"
    assert body.get("pending") is True
    conn.close()


def test_other_subject_denied_without_exposing_probe(tmp_path, monkeypatch):
    """A different authenticated subject cannot read the handle and learns
    nothing: tool/state/status/result are all absent and the envelope is
    byte-identical (except request_id) to an unknown probe."""
    _auth_ok(monkeypatch)
    _state_dir, db_path, _logs = _isolate_settings(monkeypatch, tmp_path)
    conn = _make_db(db_path)
    rid = _enqueue(conn, "someone-else@example.invalid")
    assert probes_lib.finish_probe(
        conn, rid, "ok",
        {"note": "private", "inspection": {"secret": "s3cr3t-token"}})
    denied = routes_lib.get_probe(rid, _FakeRequest())
    unknown = routes_lib.get_probe(str(uuid.uuid4()), _FakeRequest())
    assert denied.status_code == 404
    body = _resp_json(denied)
    reference = _resp_json(unknown)
    assert body.get("code") == reference.get("code") == "not_found"
    assert body.get("message") == reference.get("message") == "unknown probe"
    assert body.get("details") == reference.get("details") == ""
    for key in ("tool_id", "op", "state", "pending", "status", "result",
                "created_at", "claim_deadline", "finished_at",
                "observation_applied"):
        assert key not in body, key
    assert "s3cr3t-token" not in json.dumps(body)
    conn.close()


def test_legacy_empty_or_shared_subject_fails_closed(tmp_path, monkeypatch):
    """Rows enqueued before subject binding (empty or the pre-W9 shared
    ``\"api\"`` subject) are NEVER transferable to an authenticated
    subject; they fail closed with the unknown-probe envelope."""
    _auth_ok(monkeypatch)
    _state_dir, db_path, _logs = _isolate_settings(monkeypatch, tmp_path)
    conn = _make_db(db_path)
    legacy_ids = (
        _enqueue(conn, "", tool_id="hermes", coalesce=False),
        _enqueue(conn, "api", tool_id="opencode", coalesce=False),
    )
    for rid in legacy_ids:
        resp = routes_lib.get_probe(rid, _FakeRequest())
        assert resp.status_code == 404, rid
        body = _resp_json(resp)
        assert body.get("code") == "not_found", body
        assert body.get("message") == "unknown probe", body
    conn.close()


def test_malformed_uuid_and_unknown_probe_unchanged(tmp_path, monkeypatch):
    """Input-validation and unknown-row behavior is untouched."""
    _auth_ok(monkeypatch)
    _state_dir, db_path, _logs = _isolate_settings(monkeypatch, tmp_path)
    _make_db(db_path).close()
    malformed = routes_lib.get_probe("not-a-uuid", _FakeRequest())
    assert malformed.status_code == 422
    assert _resp_json(malformed).get("code") == "invalid_request"
    unknown = routes_lib.get_probe(str(uuid.uuid4()), _FakeRequest())
    assert unknown.status_code == 404
    assert _resp_json(unknown).get("code") == "not_found"


def test_frontend_polling_contract_roundtrip(tmp_path, monkeypatch):
    """POST /tools/{id}/check hands the durable handle under the normalized
    authenticated subject, and polling GET /probes/{id} from the same
    session returns the existing response shape."""
    _auth_ok(monkeypatch, email="Owner@Example.INVALID")
    _state_dir, db_path, _logs = _isolate_settings(monkeypatch, tmp_path)
    _make_db(db_path).close()
    captured = {}

    async def _fake_owner_probe_handle(tool_id, op, timeout_s=25.0,
                                       subject="api"):
        captured["subject"] = subject
        conn = db_lib.connect(settings_lib.db_path)
        try:
            rid = probes_lib.enqueue_probe(conn, subject, tool_id, op)
        finally:
            conn.close()
        return "timeout", {}, rid

    monkeypatch.setattr(routes_lib, "_owner_probe_handle",
                        _fake_owner_probe_handle)
    check = _run(routes_lib.post_tool_check("hermes", _check_req()))
    assert check.status_code == 200, _resp_json(check)
    check_body = _resp_json(check)
    rid = check_body.get("probe_request_id")
    assert rid
    assert check_body.get("probe_pending") is True
    assert captured.get("subject") == OWNER
    poll = routes_lib.get_probe(rid, _FakeRequest())
    assert poll.status_code == 200, _resp_json(poll)
    poll_body = _resp_json(poll)
    assert poll_body.get("request_id") == rid
    assert poll_body.get("state") == "queued"
    assert poll_body.get("pending") is True
    conn = db_lib.connect(settings_lib.db_path)
    try:
        row = dict(conn.execute("SELECT * FROM probe_requests WHERE id=?",
                                (rid,)).fetchone())
        assert row["subject"] == OWNER
    finally:
        conn.close()


def test_owner_probe_handle_forwards_subject_to_enqueue(monkeypatch):
    """The submit helper forwards the caller's normalized subject into the
    durable enqueue (single subject semantics, no raw re-derivation)."""
    captured = {}

    def _fake_request_owner_probe_handle(tool_id, op, timeout_s=25.0,
                                         subject="api", coalesce=True,
                                         apply_wait_s=None):
        captured.update(tool_id=tool_id, op=op, subject=subject)
        return "timeout", {}, str(uuid.uuid4())

    monkeypatch.setattr(probes_lib, "request_owner_probe_handle",
                        _fake_request_owner_probe_handle)
    status, _payload, rid = _run(routes_lib._owner_probe_handle(
        "hermes", "refresh", 1.0, OWNER))
    assert status == "timeout"
    assert rid
    assert captured == {"tool_id": "hermes", "op": "refresh",
                        "subject": OWNER}


def test_probe_read_auth_and_rate_limit_unchanged(tmp_path, monkeypatch):
    """Auth and read rate limiting stay first: missing credentials -> 401,
    exhausted read bucket -> 429 with Retry-After, both no-store."""
    _state_dir, db_path, _logs = _isolate_settings(monkeypatch, tmp_path)
    _make_db(db_path).close()
    unauth = routes_lib.get_probe(str(uuid.uuid4()), _FakeRequest())
    assert unauth.status_code == 401
    assert unauth.headers.get("cache-control") == "no-store"
    monkeypatch.setattr(
        deps_lib, "authenticate",
        lambda request: ({"email": OWNER}, None, "test-rid"))
    monkeypatch.setattr(deps_lib, "check_rate_limit",
                        lambda ident, kind="read": False)
    monkeypatch.setattr(deps_lib, "retry_after_s",
                        lambda ident, kind="read": 7)
    limited = routes_lib.get_probe(str(uuid.uuid4()), _FakeRequest())
    assert limited.status_code == 429
    assert limited.headers.get("retry-after") == "7"
    assert _resp_json(limited).get("code") == "rate_limited"
    assert limited.headers.get("cache-control") == "no-store"
