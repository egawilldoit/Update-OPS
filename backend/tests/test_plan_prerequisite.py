"""Wave 2.1 — T3 manual-prerequisite classification at the plan API.

Policy B (Wave 2 / D7) makes T3 automatic planning fail closed when the
unit is not provably inactive. That is NOT a stale observation: the
operator must stop T3 manually BEFORE a plan can be created. Surfacing
it as `stale_plan`/"run check again" is misleading, so the plan route
returns a deliberate `manual_prerequisite_required` code while
genuinely stale/incomplete observations keep returning `stale_plan`.

Hermetic: tmp dirs, faked auth and owner-probe seam, no server, no real
probes or systemd. Style matches backend/tests/test_replay_first.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from backend.app import db as db_lib
from backend.app.api import deps as deps_lib
from backend.app.api import routes as routes_lib
from backend.app.config import settings as settings_lib

import support as support_lib


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
    return state_dir, db_path


def _setup(tmp_path, monkeypatch):
    _auth_ok(monkeypatch)
    state_dir, db_path = _isolate_settings(monkeypatch, tmp_path)
    conn = db_lib.connect(db_path)
    db_lib.migrate(conn)
    conn.commit()
    conn.close()
    return state_dir, db_path


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


FOUR_STEPS = ["preflight", "backup", "updating", "verifying"]


def _planned_payload(tool="t3", manual=True, steps=None,
                     activity_state="idle", fingerprint="fp-t3",
                     target="1.3.0", restart_impact=None):
    if steps is None:
        steps = [] if manual else FOUR_STEPS
    if restart_impact is None:
        restart_impact = (
            "planning blocked: automatic T3 update requires a quiesced "
            "unit (activity=idle active=active); stop t3code.service "
            "manually and take a fresh plan")
    return {
        "planned": {
            "tool": tool, "target": target, "target_mode": "exact",
            "channel": "nightly", "fingerprint": fingerprint,
            "services": [], "backup_scope": {},
            "required_space_bytes": 0, "steps": list(steps),
            "restart_impact": restart_impact,
            "required_checks": ["smoke"],
            "budgets": {"unknown:t3": -1},
            "manual_restart_limitation": bool(manual),
            "backup_policy": {
                "mode": "consistent-backup-unavailable" if manual
                else "quiesced-copy",
                "consistency": restart_impact[:400],
            },
        },
        "activity": {"state": activity_state,
                     "evidence": "hermetic activity evidence",
                     "checked_at": "2026-09-13T00:00:00+00:00"},
        "inspection": {"fingerprint": fingerprint, "version": "1.2.3",
                       "install_kind": "managed-service"},
    }


def _install_probe(monkeypatch, payload):
    state = {"calls": 0, "op": ""}

    async def _fake_probe(tool_id, op, timeout_s=30.0):
        state["calls"] += 1
        state["op"] = op
        return "ok", dict(payload)

    monkeypatch.setattr(routes_lib, "_owner_probe", _fake_probe)
    return state


def _post_plan(tool_id="t3", body=None):
    req = _FakeRequest(
        headers={"content-type": "application/json", "origin": "x",
                 "x-csrf-token": "x"},
        body=json.dumps(body or {}).encode("utf-8"))
    return _run(routes_lib.post_tool_plan(tool_id, req))


def _plan_count(db_path):
    conn = db_lib.connect(db_path)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) AS n FROM plans").fetchone()["n"])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. active T3 -> classified manual prerequisite (not stale)
# ---------------------------------------------------------------------------

def test_active_t3_plan_returns_manual_prerequisite_not_stale(
        tmp_path, monkeypatch):
    _state, db_path = _setup(tmp_path, monkeypatch)
    _install_probe(monkeypatch, _planned_payload(manual=True))
    resp = _post_plan("t3")
    assert resp.status_code == 409, resp.status_code
    body = _resp_json(resp)
    assert body.get("code") == "manual_prerequisite_required", body
    message = str(body.get("message", ""))
    assert "T3" in message
    assert "stopped manually" in message
    assert "stale" not in message.lower()
    assert "run check again" not in message.lower()
    details = str(body.get("details", ""))
    assert "stale" not in details.lower()
    assert len(details) <= 300
    assert _plan_count(db_path) == 0


# ---------------------------------------------------------------------------
# 2. unknown/unprovable service state stays fail-closed
# ---------------------------------------------------------------------------

def test_unknown_t3_state_remains_fail_closed_prerequisite(
        tmp_path, monkeypatch):
    _state, db_path = _setup(tmp_path, monkeypatch)
    payload = _planned_payload(manual=True, activity_state="unknown")
    payload["inspection"]["version"] = ""
    _install_probe(monkeypatch, payload)
    resp = _post_plan("t3")
    assert resp.status_code == 409, resp.status_code
    assert _resp_json(resp).get("code") == "manual_prerequisite_required"
    assert _plan_count(db_path) == 0


# ---------------------------------------------------------------------------
# 3. genuinely stale/incomplete plans still return stale_plan
# ---------------------------------------------------------------------------

def test_stale_empty_plan_without_prerequisite_still_stale(
        tmp_path, monkeypatch):
    _state, db_path = _setup(tmp_path, monkeypatch)
    payload = _planned_payload(manual=False, steps=[])
    payload["planned"]["manual_restart_limitation"] = False
    _install_probe(monkeypatch, payload)
    resp = _post_plan("t3")
    assert resp.status_code == 409, resp.status_code
    body = _resp_json(resp)
    assert body.get("code") == "stale_plan", body
    assert "run check again" in str(body.get("message", ""))
    assert _plan_count(db_path) == 0


def test_missing_fingerprint_still_stale_plan(tmp_path, monkeypatch):
    _state, db_path = _setup(tmp_path, monkeypatch)
    _install_probe(monkeypatch, _planned_payload(
        manual=False, steps=FOUR_STEPS, fingerprint=""))
    resp = _post_plan("t3")
    assert resp.status_code == 409, resp.status_code
    body = _resp_json(resp)
    assert body.get("code") == "stale_plan", body
    assert "fingerprint" in str(body.get("message", ""))
    assert _plan_count(db_path) == 0


def test_missing_target_still_stale_plan(tmp_path, monkeypatch):
    _state, db_path = _setup(tmp_path, monkeypatch)
    _install_probe(monkeypatch, _planned_payload(
        manual=False, steps=FOUR_STEPS, target=""))
    resp = _post_plan("t3")
    assert resp.status_code == 409, resp.status_code
    body = _resp_json(resp)
    assert body.get("code") == "stale_plan", body
    assert _plan_count(db_path) == 0


# ---------------------------------------------------------------------------
# 4. other adapters retain existing blocked/stale behavior
# ---------------------------------------------------------------------------

def test_other_adapter_blocked_plan_unchanged(tmp_path, monkeypatch):
    _state, db_path = _setup(tmp_path, monkeypatch)
    payload = _planned_payload(tool="hermes", manual=False, steps=[])
    payload["planned"].pop("manual_restart_limitation", None)
    _install_probe(monkeypatch, payload)
    resp = _post_plan("hermes")
    assert resp.status_code == 409, resp.status_code
    body = _resp_json(resp)
    assert body.get("code") == "stale_plan", body
    assert _plan_count(db_path) == 0


def test_executable_plan_happy_path_unchanged(tmp_path, monkeypatch):
    _state, db_path = _setup(tmp_path, monkeypatch)
    _install_probe(monkeypatch, _planned_payload(
        manual=False, steps=FOUR_STEPS))
    resp = _post_plan("t3")
    assert resp.status_code == 201, (resp.status_code, _resp_json(resp))
    assert _plan_count(db_path) == 1


# ---------------------------------------------------------------------------
# 5. prerequisite detail is sanitized and bounded
# ---------------------------------------------------------------------------

def test_prerequisite_detail_is_sanitized_and_bounded(tmp_path, monkeypatch):
    _state, _db_path = _setup(tmp_path, monkeypatch)
    support_lib.use_test_secrets(monkeypatch, tmp_path)
    secret = "dummy-secret-value-12345"
    payload = _planned_payload(
        manual=True,
        restart_impact=("planning blocked: " + secret + " " + "x" * 5000))
    _install_probe(monkeypatch, payload)
    resp = _post_plan("t3")
    assert resp.status_code == 409, resp.status_code
    body = _resp_json(resp)
    assert body.get("code") == "manual_prerequisite_required", body
    details = str(body.get("details", ""))
    assert secret not in details, details
    assert len(details) <= 300
