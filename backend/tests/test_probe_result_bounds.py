"""W3.2 probe-result size-bound regression tests.

Contract under test (owner_probes): every durable probe result document
is bounded by PROBE_RESULT_MAX_BYTES serialized UTF-8 bytes. A result
within the bound is stored verbatim (valid JSON, original status). A
result over the bound is replaced by a small typed error document
(``probe_result_too_large``, status ``error``); the payload is never
truncated into invalid JSON. A serialization failure stores a typed
``probe_result_serialization_failed`` error (never ``{}``).

The pre-fix defect: ``finish_probe`` truncated the serialized document
(``raw[:200000]``), so readers fell back to ``{}`` while the stored
status could still claim success.

Hermetic: tmp sqlite DB, monkeypatched supervisors and unit states. No
test in this module touches real systemd, /opt, /etc or /var/lib.
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
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from backend.app import db as db_lib
from backend.app import owner_probes as probes_lib
from backend.app.admission import admit as admit_lib
from backend.app.api import deps as deps_lib
from backend.app.api import routes as routes_lib
from backend.app.config import settings as settings_lib
from backend.app.worker import dispatch as dispatch_lib
from backend.app.worker import phase_run as phase_run_lib

import support as support_lib

# The serialization parameters the store contract uses: sort_keys and
# default=str are the historical parameters; ensure_ascii=False makes the
# declared unit (UTF-8 bytes of the document as stored) exact.
_SERIALIZE_KWARGS = dict(sort_keys=True, default=str, ensure_ascii=False)

_GOOD_TS = "2026-09-01T00:00:00+00:00"
_OVER_PAD = 300_000  # clearly over the declared bound; no contract literal


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _isolate_settings(tmp_path, monkeypatch):
    """Point db/state/logs at tmp; clear the in-memory discovery cache."""
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


def _max_bytes():
    """The declared contract bound (fails loudly if the contract vanishes)."""
    return int(probes_lib.PROBE_RESULT_MAX_BYTES)


def _serialized(payload):
    return json.dumps(payload or {}, **_SERIALIZE_KWARGS)


def _size(payload):
    return len(_serialized(payload).encode("utf-8"))


def _fitted_payload(target_bytes, fields=None):
    """A payload whose serialized UTF-8 size is exactly target_bytes.

    ASCII padding adds exactly one byte per character, so one correction
    step lands on the byte.
    """
    payload = {"inspection": {"install_identity": "i", "version": "1.0"},
               "discovery": {"available": True}}
    if fields:
        payload.update(fields)
    payload["pad"] = ""
    delta = int(target_bytes) - _size(payload)
    assert delta >= 0, delta
    payload["pad"] = "x" * delta
    assert _size(payload) == int(target_bytes)
    return payload


def _huge_payload():
    return {"inspection": {"install_identity": "i", "version": "1.0"},
            "pad": "z" * _OVER_PAD}


def _too_large_doc(actual_bytes):
    return {"reason": "probe_result_too_large",
            "max_bytes": _max_bytes(),
            "actual_bytes": int(actual_bytes)}


def _result_row(conn, request_id):
    return conn.execute(
        "SELECT * FROM probe_results WHERE request_id=?",
        (request_id,)).fetchone()


def _finish(conn, status, payload, subject="api"):
    rid = probes_lib.enqueue_probe(conn, subject, "hermes", "refresh")
    assert probes_lib.finish_probe(conn, rid, status, payload) is True
    assert conn.execute("SELECT state FROM probe_requests WHERE id=?",
                        (rid,)).fetchone()["state"] == "done"
    return rid


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


def _patch_supervised_ok(monkeypatch, payload=None):
    data = payload if payload is not None else {}
    monkeypatch.setattr(phase_run_lib, "run_supervised_probe",
                        lambda *a, **k: (True, dict(data), "", False))
    return data


def _patch_units(monkeypatch, mapping=None, default="confirmed_stopped"):
    fake = support_lib.FakeUnitStates(mapping or {}, default=default)
    from backend.app import units as units_lib
    monkeypatch.setattr(units_lib, "query_unit", fake.query_unit)
    return fake


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
        lambda request: ({"email": "owner@example.invalid"}, None, "rid-w32"))
    monkeypatch.setattr(
        deps_lib, "require_mutation_guards", lambda request, claims: None)
    monkeypatch.setattr(deps_lib, "check_rate_limit",
                        lambda ident, kind="read": True)


def _resp_json(resp):
    try:
        raw = getattr(resp, "body", b"")
        if isinstance(raw, (bytes, bytearray)):
            return json.loads(bytes(raw).decode("utf-8") or "{}")
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# 1-2. under-bound and exact-bound results are stored verbatim
# ---------------------------------------------------------------------------

def test_under_bound_result_preserved_exactly(tmp_path, monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    payload = {"inspection": {"install_identity": "hermes-display-identity",
                              "version": "1.18.30", "fingerprint": "fp-w32"},
               "discovery": {"target": "1.18.31", "available": True},
               "verification": {"passed": True, "checks": [
                   {"name": "smoke", "result": "pass", "mandatory": True}]}}
    rid = _finish(conn, "ok", payload)
    res = _result_row(conn, rid)
    assert res["status"] == "ok"
    assert json.loads(res["result_json"]) == payload
    assert res["result_json"] == _serialized(payload)
    status, read_back = probes_lib.await_probe(conn, rid, timeout_s=1.0)
    assert status == "ok" and read_back == payload
    conn.close()


def test_exact_bound_result_accepted(tmp_path, monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    payload = _fitted_payload(_max_bytes())
    rid = _finish(conn, "ok", payload)
    res = _result_row(conn, rid)
    assert res["status"] == "ok"
    assert _size(payload) == _max_bytes()
    assert json.loads(res["result_json"]) == payload
    status, read_back = probes_lib.await_probe(conn, rid, timeout_s=1.0)
    assert status == "ok" and read_back == payload
    conn.close()


# ---------------------------------------------------------------------------
# 3-6. over-bound results are replaced, never truncated
# ---------------------------------------------------------------------------

def test_one_byte_over_bound_stores_typed_error(tmp_path, monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    payload = _fitted_payload(_max_bytes() + 1)
    actual = _size(payload)
    rid = _finish(conn, "ok", payload)
    res = _result_row(conn, rid)
    assert res["status"] == "error"
    assert json.loads(res["result_json"]) == _too_large_doc(actual)
    # No prefix/fragment of the original payload is persisted.
    assert payload["pad"] not in res["result_json"]
    status, read_back = probes_lib.await_probe(conn, rid, timeout_s=1.0)
    assert status == "error"
    assert read_back == _too_large_doc(actual)
    conn.close()


def test_very_large_result_is_bounded_typed_error(tmp_path, monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    payload = {"inspection": {"install_identity": "i"},
               "pad": "z" * 1_000_000}
    actual = _size(payload)
    rid = _finish(conn, "ok", payload)
    res = _result_row(conn, rid)
    assert res["status"] == "error"
    assert len(res["result_json"]) < 1000
    assert json.loads(res["result_json"]) == _too_large_doc(actual)
    conn.close()


def test_multibyte_utf8_near_boundary_is_never_split(tmp_path, monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    fields = {"inspection": {"install_identity": "i",
                             "note": "\u00e9\u4e2d\u6587" * 4}}
    at_bound = _fitted_payload(_max_bytes(), fields=fields)
    rid = _finish(conn, "ok", at_bound)
    res = _result_row(conn, rid)
    assert res["status"] == "ok"
    assert _size(at_bound) == _max_bytes()
    decoded = json.loads(res["result_json"])
    assert decoded == at_bound
    assert decoded["inspection"]["note"] == "\u00e9\u4e2d\u6587" * 4
    # The stored bytes are valid UTF-8 carrying the real code points.
    assert "\u00e9\u4e2d\u6587" in res["result_json"]
    # One byte over: typed error, no split code point / invalid JSON.
    over = _fitted_payload(_max_bytes() + 1, fields=fields)
    rid2 = _finish(conn, "ok", over)
    res2 = _result_row(conn, rid2)
    assert res2["status"] == "error"
    assert json.loads(res2["result_json"]) == _too_large_doc(_size(over))
    assert "\u00e9" not in res2["result_json"]
    conn.close()


def test_nested_oversized_field_not_partially_persisted(tmp_path,
                                                        monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    payload = {"verification": {
        "passed": True,
        "checks": [{"name": "smoke", "result": "pass",
                    "summary": "y" * _OVER_PAD}]}}
    rid = _finish(conn, "ok", payload)
    res = _result_row(conn, rid)
    assert res["status"] == "error"
    stored = json.loads(res["result_json"])
    assert stored == _too_large_doc(_size(payload))
    assert "summary" not in res["result_json"]
    assert "yyy" not in res["result_json"]
    conn.close()


# ---------------------------------------------------------------------------
# serialization failure path: honest typed error, never {}
# ---------------------------------------------------------------------------

def test_serialization_failure_stores_typed_error(tmp_path, monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    payload = {"k": "v"}
    payload["self"] = payload  # circular: json.dumps raises
    rid = _finish(conn, "ok", payload)
    res = _result_row(conn, rid)
    assert res["status"] == "error"
    assert json.loads(res["result_json"]) == {
        "reason": "probe_result_serialization_failed"}
    conn.close()


# ---------------------------------------------------------------------------
# 7-9. lifecycle: refresh attempt, last-good observation, exclusion
# ---------------------------------------------------------------------------

def test_oversized_refresh_preserves_last_good_records_attempt(
        tmp_path, monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    _seed_good_observation(conn)
    payload = {"pad": "z" * _OVER_PAD}
    _patch_supervised_ok(monkeypatch, payload=payload)
    rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    assert dispatch_lib.run_probe_queue(conn) == 1
    res = _result_row(conn, rid)
    assert res["status"] == "error"
    assert json.loads(res["result_json"]) == _too_large_doc(_size(payload))
    tool = _tool_row(conn)
    # Last good observation untouched; freshness not replaced.
    assert tool["observed_version"] == "1.18.30"
    assert tool["health"] == "healthy"
    assert tool["install_identity"] == "hermes-display-identity"
    assert tool["observation_time"] == _GOOD_TS
    assert tool["last_success_at"] == _GOOD_TS
    # Latest attempt records the failure via the normal failed path.
    assert tool["last_attempt_at"] == res["finished_at"]
    assert "probe_result_too_large" in (tool["last_attempt_error"] or "")
    assert "probe_result_too_large" in (tool["discovery_error"] or "")
    conn.close()


def test_oversized_stop_proven_preserves_observation_and_releases(
        tmp_path, monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    _seed_good_observation(conn)
    _patch_units(monkeypatch, default="confirmed_stopped")
    payload = {"pad": "z" * _OVER_PAD}
    _patch_supervised_ok(monkeypatch, payload=payload)  # ok => stop proven
    rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    assert dispatch_lib.run_probe_queue(conn) == 1
    res = _result_row(conn, rid)
    assert res["status"] == "error"
    assert json.loads(res["result_json"]) == _too_large_doc(_size(payload))
    tool = _tool_row(conn)
    assert tool["observed_version"] == "1.18.30"
    assert tool["observation_time"] == _GOOD_TS
    assert "probe_result_too_large" in (tool["last_attempt_error"] or "")
    # Stop proven + durable error result stored: exclusion releases.
    assert _unreleased_probe_leases(conn) == []
    support_lib.admit_new(conn, idem_key="k-bounds-proven")
    conn.close()


def test_oversized_stop_unknown_keeps_exclusion_held(tmp_path, monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    _seed_good_observation(conn)
    payload = {"pad": "z" * _OVER_PAD}
    _patch_supervised_ok(monkeypatch, payload=payload)
    monkeypatch.setattr(dispatch_lib, "_probe_stop_proven",
                        lambda request_id, supervised: False)
    rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    assert dispatch_lib.run_probe_queue(conn) == 1
    res = _result_row(conn, rid)
    assert res["status"] == "error"
    stored = json.loads(res["result_json"])
    # The dispatcher annotates a held exclusion (extra metadata) before
    # storage, so actual_bytes covers the annotated payload; the typed
    # replacement still contains no payload content.
    assert stored.get("reason") == "probe_result_too_large"
    assert stored.get("max_bytes") == _max_bytes()
    assert int(stored.get("actual_bytes", 0)) >= _size(payload)
    assert "zzz" not in res["result_json"]
    tool = _tool_row(conn)
    assert tool["observed_version"] == "1.18.30"
    assert tool["observation_time"] == _GOOD_TS
    # Failed attempt applied, but exclusion stays held without stop proof.
    held = _unreleased_probe_leases(conn)
    assert len(held) == 1 and held[0]["request_id"] == rid
    plan = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    _jid, created, err = admit_lib(
        conn, "owner@example.invalid", "k-bounds-hold", plan["id"], False,
        "fp-test-1", True, False)
    assert err == "busy" and not created
    conn.close()


# ---------------------------------------------------------------------------
# 10-11. HTTP read surface and secret containment
# ---------------------------------------------------------------------------

def test_get_probe_oversized_returns_valid_typed_error(tmp_path, monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    _auth_ok(monkeypatch)
    conn = _fresh_db(db_path)
    payload = {"pad": "z" * _OVER_PAD}
    # W9: the read surface is subject-bound to the authenticated owner.
    rid = _finish(conn, "ok", payload, subject="owner@example.invalid")
    resp = routes_lib.get_probe(rid, _FakeRequest())
    assert resp.status_code == 200
    body = _resp_json(resp)
    assert body.get("status") == "error"
    assert body.get("result") == _too_large_doc(_size(payload))
    assert body.get("result") != {}
    # Historical/corrupt rows keep the defensive decode fallback.
    rid2 = _finish(conn, "ok", {"fine": True},
                   subject="owner@example.invalid")
    conn.execute("UPDATE probe_results SET result_json=? WHERE request_id=?",
                 ("{not-valid-json", rid2))
    conn.commit()
    resp2 = routes_lib.get_probe(rid2, _FakeRequest())
    assert resp2.status_code == 200
    body2 = _resp_json(resp2)
    assert body2.get("result") == {}
    conn.close()


def test_oversized_secret_content_not_copied_into_typed_error(
        tmp_path, monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    secret = "sk-live-DEADBEEFDEADBEEFDEADBEEF"
    payload = {"env": {"OPENAI_API_KEY": secret},
               "inspection": {"note": "token=%s" % secret},
               "pad": "z" * _OVER_PAD}
    rid = _finish(conn, "ok", payload)
    res = _result_row(conn, rid)
    assert res["status"] == "error"
    assert json.loads(res["result_json"]) == _too_large_doc(_size(payload))
    assert secret not in res["result_json"]
    assert "OPENAI_API_KEY" not in res["result_json"]
    assert "zzz" not in res["result_json"]
    conn.close()
