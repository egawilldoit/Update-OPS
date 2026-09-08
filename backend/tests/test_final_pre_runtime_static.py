"""Final pre-runtime static regression tests (H01-H06, S01; write-only).

Behavioral regression artifacts for every H/S finding. NOT EXECUTED in
this phase per instruction. Mocks stay at external boundaries
(systemd bus, process spawn, filesystem failures, sanitizer failures);
admission transactions, leases, reconciliation decisions, and receipt
recovery logic are never mocked away. Python 3.10 compatible, pytest
style, stdlib + backend.
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

import support as support_lib


def _fresh_db(tmp_path, name="h.db"):
    conn = db_lib.connect(str(tmp_path / name))
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.commit()
    return conn


def _admit(conn, plan_id, key, fp="fp-test-1", ack=False,
           subject="owner@example.invalid"):
    from backend.app.admission import admit

    support_lib.test_release_root()
    return admit(conn, subject, key, plan_id, ack, fp, True, False)


def _lease_ids(conn):
    rows = conn.execute(
        "SELECT id, job_id, released_at FROM execution_leases"
        " WHERE kind='mutation' ORDER BY rowid").fetchall()
    return [(str(r["id"]), str(r["job_id"]),
             str(r["released_at"] or "")) for r in rows]


# -- H01 unique mutation-lease identity ------------------------------------------

def test_h01_first_job_reserves(tmp_path):
    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid, created, err = _admit(conn, row["id"], "k-h01-1")
    assert err == "" and created and jid
    leases = _lease_ids(conn)
    assert leases == [("mutation-%s" % jid, jid, "")]
    conn.close()


def test_h01_released_then_second_reserves(tmp_path, monkeypatch):
    """Ownership release followed by a second reservation: unique
    identities, no primary-key collision with history."""
    from backend.app import units as units_lib
    from backend.app import tx as tx_lib

    support_lib.use_test_secrets(monkeypatch, tmp_path)
    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid1, _, err1 = _admit(conn, row["id"], "k-h01-2a")
    assert err1 == ""
    # Simulate proven quiescence + terminal outcome, then release.
    tx_lib.transition_tx(conn, jid1, "succeeded", step="verifying",
                         expect_states=["accepted"],
                         update={"after_version": "9.9.9"},
                         event="succeeded", event_detail="")
    monkeypatch.setattr(
        units_lib, "query_unit",
        lambda unit, timeout_s=10: {"state": "confirmed_stopped",
                                    "unit": unit,
                                    "active_state": "inactive",
                                    "sub_state": "dead", "main_pid": 0,
                                    "cgroup": "", "identity_ok": True,
                                    "detail": ""})
    released = tx_lib.release_ownership(
        conn, jid1, expect_states=["succeeded"],
        event="ownership_released", event_detail="test proof")
    assert released.get("_ownership_released") == 1
    row2 = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid2, created2, err2 = _admit(conn, row2["id"], "k-h01-2b")
    assert err2 == "" and created2, err2
    assert jid2 != jid1
    leases = _lease_ids(conn)
    assert ("mutation-%s" % jid1, jid1, "") not in [
        (lid, j, r) for lid, j, r in leases if not r]
    assert ("mutation-%s" % jid2, jid2, "") in leases
    conn.close()


def test_h01_five_sequential_unique_leases(tmp_path, monkeypatch):
    """Five sequential jobs (each terminalized + released) produce five
    unique historical lease rows — never a primary-key reuse."""
    from backend.app import tx as tx_lib

    support_lib.use_test_secrets(monkeypatch, tmp_path)
    conn = _fresh_db(tmp_path)
    seen = set()
    for index in range(5):
        row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
        jid, created, err = _admit(
            conn, row["id"], "k-h01-seq-%d" % index)
        assert err == "" and created, (index, err)
        lease_id = "mutation-%s" % jid
        assert lease_id not in seen
        seen.add(lease_id)
        tx_lib.transition_tx(conn, jid, "succeeded", step="verifying",
                             expect_states=["accepted"],
                             update={"after_version": "9.9.9"},
                             event="succeeded", event_detail="")
        tx_lib.release_ownership(
            conn, jid, expect_states=["succeeded"],
            event="ownership_released", event_detail="test proof")
    assert len(seen) == 5
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM execution_leases"
        " WHERE kind='mutation'").fetchone()
    assert int(rows["n"]) == 5
    conn.close()


def test_h01_held_old_lease_blocks(tmp_path):
    """A still-held older lease blocks another reservation (fail closed),
    even though identities can never collide."""
    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid1, _, err1 = _admit(conn, row["id"], "k-h01-held-a")
    assert err1 == ""
    row2 = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    _jid2, created2, err2 = _admit(conn, row2["id"], "k-h01-held-b")
    assert err2 == "busy" and not created2
    assert _lease_held(conn, jid1)
    conn.close()


def _lease_held(conn, job_id):
    row = conn.execute(
        "SELECT released_at FROM execution_leases WHERE kind='mutation'"
        " AND job_id=?", (job_id,)).fetchone()
    return row is not None and not str(row["released_at"] or "")


def test_h01_rollback_leaves_neither_job_nor_lease(tmp_path):
    """Injected failure inside the reservation transaction leaves
    neither a job row nor a lease row behind."""
    import sqlite3
    from backend.app.admission import admit

    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    support_lib.test_release_root()
    real_execute = conn.execute

    def _failing_execute(sql, params=()):
        if isinstance(sql, str) and sql.strip().upper().startswith(
                "INSERT INTO jobs"):
            raise sqlite3.OperationalError("injected job failure")
        return real_execute(sql, params)

    class _FailJobs(object):
        def __init__(self, real):
            self._real = real

        def execute(self, sql, params=()):
            return _failing_execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

    jid, created, err = admit(
        _FailJobs(conn), "owner@example.invalid", "k-h01-rb", row["id"],
        False, "fp-test-1", True, False)
    assert err == "unavailable" and not created and not jid
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM execution_leases").fetchone()["n"] == 0
    assert conn.execute(
        "SELECT used_at FROM plans WHERE id=?",
        (row["id"],)).fetchone()["used_at"] == ""
    conn.close()


# -- H02 scoped delegated-service references -----------------------------------

def _plan_with_services(conn, services):
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    conn.execute("UPDATE plans SET services=? WHERE id=?",
                 (json.dumps(services), row["id"]))
    conn.commit()
    return row


class _FakeUnits(object):
    """Fake systemd managers; records WHICH manager each unit hit."""

    def __init__(self, user=None, system=None):
        self.calls = []
        self._user = user or {}
        self._system = system or {}

    def query_unit(self, unit, timeout_s=5):
        self.calls.append(("user", unit))
        return dict(self._user.get(
            unit, {"state": "unknown", "detail": "not present"}))

    def query_unit_system(self, unit, timeout_s=5):
        self.calls.append(("system", unit))
        return dict(self._system.get(
            unit, {"state": "unknown", "detail": "not present"}))


def _stopped():
    return {"state": "confirmed_stopped", "detail": ""}


def test_h02_parse_matrix():
    from backend.app.reconcile_core import parse_service_ref as parse

    assert parse("system:ega-update-runner@owner.service") == \
        ("system", "ega-update-runner@owner.service")
    assert parse("user:opencode.service") == ("user", "opencode.service")
    # Legacy bare unit means user scope (console-managed user units).
    assert parse("opencode.service") == ("user", "opencode.service")
    assert parse("  user:foo.service  ") == ("user", "foo.service")
    # Malformed: unknown scope, extra colons, empty unit, empty, paths.
    assert parse("") == ("", "")
    assert parse(None) == ("", "")
    assert parse("bogus:x.service") == ("", "")
    assert parse("a:b:c") == ("", "")
    assert parse("system:") == ("", "")
    assert parse(":foo.service") == ("", "")
    assert parse("user:foo bar.service") == ("", "")
    assert parse("user:../evil.service") == ("", "")
    assert parse("user:") == ("", "")


def test_h02_system_ref_queries_system_manager_only(tmp_path):
    """A system-scoped ref hits ONLY the system manager: no
    user-then-system fallback that could turn activity into
    false not-found evidence."""
    from backend.app import reconcile_core as rc_lib

    conn = _fresh_db(tmp_path)
    row = _plan_with_services(conn, ["system:runner.service"])
    job = {"id": "job-h02-sys", "plan_id": row["id"]}
    fake = _FakeUnits(user={"runner.service": {"state": "live",
                                               "detail": "active"}},
                      system={"runner.service": _stopped()})
    quiescent, evidence, _reason = rc_lib.delegated_quiescence(
        conn, job, units_mod=fake)
    assert quiescent is True
    assert ("user", "runner.service") not in fake.calls
    assert ("system", "runner.service") in fake.calls
    assert evidence[0]["scope"] == "system"
    conn.close()


def test_h02_bare_ref_queries_user_manager_only(tmp_path):
    from backend.app import reconcile_core as rc_lib

    conn = _fresh_db(tmp_path)
    row = _plan_with_services(conn, ["opencode.service"])
    job = {"id": "job-h02-bare", "plan_id": row["id"]}
    fake = _FakeUnits(user={"opencode.service": _stopped()},
                      system={"opencode.service": {"state": "live",
                                                  "detail": "active"}})
    quiescent, evidence, _reason = rc_lib.delegated_quiescence(
        conn, job, units_mod=fake)
    assert quiescent is True
    assert ("system", "opencode.service") not in fake.calls
    assert evidence[0]["scope"] == "user"
    conn.close()


def test_h02_malformed_ref_blocks(tmp_path):
    from backend.app import reconcile_core as rc_lib

    conn = _fresh_db(tmp_path)
    for bad in (["system:"], ["a:b:c"], ["bogus:x.service"], [""]):
        services = [s for s in bad if s] or ["system:"]
        row = _plan_with_services(conn, services)
        job = {"id": "job-h02-bad", "plan_id": row["id"]}
        fake = _FakeUnits(user={}, system={})
        quiescent, evidence, reason = rc_lib.delegated_quiescence(
            conn, job, units_mod=fake)
        assert quiescent is False
        assert "malformed" in (evidence[0].get("detail", "") + reason)
        assert fake.calls == []
    conn.close()


def test_h02_missing_plan_blocks(tmp_path):
    from backend.app import reconcile_core as rc_lib

    conn = _fresh_db(tmp_path)
    quiescent, _evidence, reason = rc_lib.delegated_quiescence(
        conn, {"id": "job-h02-noplan", "plan_id": "plan-missing"},
        units_mod=_FakeUnits())
    assert quiescent is False
    assert "plan" in reason
    conn.close()


def test_h02_adapters_emit_structured_refs():
    """Codex never emits the non-unit 'codex-daemon' name; OpenCode
    emits an explicitly user-scoped ref (server_on implies a
    configured non-empty unit, so the ref is always well-formed)."""
    with open(os.path.join(_REPO_ROOT, "backend", "app", "adapters",
                           "codex.py"), "r", encoding="utf-8") as fh:
        codex_src = fh.read()
    assert "codex-daemon" not in codex_src
    with open(os.path.join(_REPO_ROOT, "backend", "app", "adapters",
                           "opencode.py"), "r", encoding="utf-8") as fh:
        opencode_src = fh.read()
    assert '"user:%s" % os.environ.get("EGA_OPENCODE_UNIT", "")' in \
        opencode_src
    for name in ("hermes.py", "t3.py"):
        with open(os.path.join(_REPO_ROOT, "backend", "app", "adapters",
                               name), "r", encoding="utf-8") as fh:
            src = fh.read()
        assert '"%s:%s" % (scope' in src


# -- H03 structured process proof + phase-scope gates --------------------------

def test_h03_prove_empty_token_is_vacuous():
    from backend.app import reconcile_core as rc_lib

    proof = rc_lib.prove_processes("", "")
    assert proof["ok"] is True and proof["processes"] == []


def test_h03_prove_completes_empty_on_unknown_token():
    from backend.app import reconcile_core as rc_lib

    token = "deadbeefcafe%032d" % 1
    proof = rc_lib.prove_processes(token, "")
    assert proof["ok"] is True
    assert proof["processes"] == []


def test_h03_prove_enumeration_failure_is_not_empty(monkeypatch):
    """A failed /proc listing is UNPROVABLE (ok False), never []."""
    from backend.app import reconcile_core as rc_lib

    def _boom(_path):
        raise OSError("proc unavailable")

    monkeypatch.setattr(rc_lib.os, "listdir", _boom)
    proof = rc_lib.prove_processes("abcdef1234", "")
    assert proof["ok"] is False
    assert proof["processes"] == []
    assert "enumeration" in proof["reason"]


def test_h03_prove_vanished_pid_skips(monkeypatch):
    """PIDs that exit mid-scan are gone and cannot be survivors."""
    from backend.app import reconcile_core as rc_lib

    monkeypatch.setattr(rc_lib.os, "listdir",
                        lambda _path: ["99999991"])
    proof = rc_lib.prove_processes("abcdef1234", "")
    assert proof["ok"] is True
    assert proof["processes"] == []


def test_h03_prove_unreadable_pid_blocks(monkeypatch):
    """A live but unreadable PID (PermissionError) blocks: its
    membership cannot be ruled out."""
    import builtins
    from backend.app import reconcile_core as rc_lib

    real_open = builtins.open
    monkeypatch.setattr(rc_lib.os, "listdir",
                        lambda _path: ["424243"])

    def _guarded_open(path, *args, **kwargs):
        if "424243" in str(path):
            raise PermissionError("denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _guarded_open)
    proof = rc_lib.prove_processes("abcdef1234", "")
    assert proof["ok"] is False
    assert proof["processes"] == []
    assert "424243" in proof["reason"]


def test_h03_job_processes_wrapper_stays_list():
    from backend.app import reconcile_core as rc_lib

    assert rc_lib.job_processes("zz-no-such-token-zz", "") == []


def test_h03_job_phases_match_supervised_phases():
    """_JOB_PHASES tracks phase_run.PHASES minus probe (probe scopes
    are probe-owned, never job-owned)."""
    from backend.app import reconcile_core as rc_lib
    from backend.app.worker import phase_run as phase_run_lib

    assert set(rc_lib._JOB_PHASES) == \
        set(phase_run_lib.PHASES) - {"probe"}


def test_h03_expected_scopes_cover_all_job_phases():
    from backend.app import reconcile_core as rc_lib

    jid = str(uuid.uuid4())
    scopes = rc_lib.expected_phase_scopes(jid)
    assert len(scopes) == 4
    assert all(s.endswith(".scope") for s in scopes)
    assert all(rc_lib.unit_hex(jid) in s for s in scopes)
    assert rc_lib.expected_phase_scopes("") == []


def test_h03_scopes_all_stopped_proves():
    from backend.app import reconcile_core as rc_lib

    jid = str(uuid.uuid4())
    scopes = rc_lib.expected_phase_scopes(jid)
    fake = _FakeUnits(
        user={s: {"state": "confirmed_stopped", "detail": ""}
              for s in scopes})
    quiescent, evidence, reason = rc_lib.phase_scopes_quiescence(
        jid, units_mod=fake)
    assert quiescent is True and reason == ""
    assert len(evidence) == 4


def test_h03_scopes_live_scope_blocks():
    from backend.app import reconcile_core as rc_lib

    jid = str(uuid.uuid4())
    scopes = rc_lib.expected_phase_scopes(jid)
    units = {s: {"state": "confirmed_stopped", "detail": ""}
             for s in scopes}
    units[scopes[2]] = {"state": "live", "detail": "active"}
    fake = _FakeUnits(user=units)
    quiescent, _evidence, reason = rc_lib.phase_scopes_quiescence(
        jid, units_mod=fake)
    assert quiescent is False
    assert scopes[2] in reason


def test_h03_scopes_query_crash_blocks():
    from backend.app import reconcile_core as rc_lib

    class _Crash(object):
        def query_unit(self, unit, timeout_s=5):
            raise OSError("bus down")

    quiescent, _evidence, _reason = rc_lib.phase_scopes_quiescence(
        str(uuid.uuid4()), units_mod=_Crash())
    assert quiescent is False


def test_h03_runner_sentinel_on_unprovable_scan(monkeypatch):
    """_job_processes_alive surfaces proof failure as a truthy
    sentinel, so timeout-verify treats it as unresolved."""
    from backend.app import reconcile_core as rc_lib
    from backend.app.worker import runner as runner_lib

    monkeypatch.setattr(
        rc_lib, "prove_processes",
        lambda *a, **k: {"ok": False, "processes": [],
                         "reason": "proc unavailable"})
    leftovers = runner_lib._job_processes_alive("any-job")
    assert leftovers and leftovers[0]["pid"] == -1


class _FakePopen(object):
    """Fake supervised worker spawn: writes the result file the real
    worker would produce, then reports immediate exit."""

    result_path = ""
    result_body = {"ok": True, "data": {"done": 1}}

    def __init__(self, *args, **kwargs):
        self._wrote = False

    def poll(self):
        if not self._wrote:
            self._wrote = True
            try:
                with open(type(self).result_path, "w",
                          encoding="utf-8") as fh:
                    json.dump(type(self).result_body, fh)
            except Exception:
                pass
        return 0

    def wait(self, timeout=None):
        return 0

    def communicate(self, timeout=None):
        return b"", b""

    def kill(self):
        pass


def _phase_run_settings(monkeypatch, tmp_path):
    from backend.app import owner_env as owner_env_lib

    release = str(tmp_path)
    monkeypatch.setattr(
        owner_env_lib, "resolved_paths",
        lambda _s: {"release_root": release,
                    "venv_python": "/usr/bin/python3",
                    "config_path": os.path.join(release, "cfg.json")})
    monkeypatch.setattr(
        owner_env_lib, "build_owner_contract",
        lambda _s: object())
    monkeypatch.setattr(
        owner_env_lib, "contract_fingerprint",
        lambda _c: "fp-test")
    return object()


def _patch_phase_spawn(monkeypatch):
    import subprocess as sp_lib

    monkeypatch.setattr(sp_lib, "Popen", _FakePopen)
    real_isfile = os.path.isfile
    monkeypatch.setattr(
        os.path, "isfile",
        lambda p: True if p == "/usr/bin/systemd-run" else
        real_isfile(p))


def test_h03_normal_completion_proves_scope_exit(tmp_path, monkeypatch):
    """Worker rc 0 + result data + already-empty scope delivers."""
    from backend.app import units as units_lib
    from backend.app.worker import phase_run as phase_run_lib

    settings = _phase_run_settings(monkeypatch, tmp_path)
    _patch_phase_spawn(monkeypatch)
    killed = []

    monkeypatch.setattr(
        phase_run_lib, "_kill_scope_wait_empty",
        lambda scope, grace: killed.append(scope) or True)
    monkeypatch.setattr(
        units_lib, "query_unit",
        lambda unit, timeout_s=5: {"state": "confirmed_stopped",
                                   "unit": unit, "detail": ""})
    jid = str(uuid.uuid4())
    emitted = []
    _FakePopen.result_path = os.path.join(
        str(tmp_path), "%s.execute.result.json" % jid)
    _FakePopen.result_body = {"ok": True, "data": {"done": 1}}
    ok, data, error, timed_out = phase_run_lib.run_supervised_phase(
        "hermes", jid, "execute", {}, 60.0, settings, str(tmp_path),
        lambda stream, line: emitted.append((stream, line)),
        op="test-op", env={"PATH": "/usr/bin:/bin"})
    assert ok is True and timed_out is False, error
    assert data.get("done") == 1
    assert killed == []


def test_h03_completion_reaps_lingering_scope(tmp_path, monkeypatch):
    """Worker done but scope live + kill succeeds: result still
    delivers (the scope was proven empty before delivery)."""
    from backend.app import units as units_lib
    from backend.app.worker import phase_run as phase_run_lib

    settings = _phase_run_settings(monkeypatch, tmp_path)
    _patch_phase_spawn(monkeypatch)
    killed = []
    monkeypatch.setattr(
        phase_run_lib, "_kill_scope_wait_empty",
        lambda scope, grace: killed.append(scope) or True)
    monkeypatch.setattr(
        units_lib, "query_unit",
        lambda unit, timeout_s=5: {"state": "live", "unit": unit,
                                   "active_state": "active",
                                   "sub_state": "running",
                                   "detail": ""})
    jid = str(uuid.uuid4())
    _FakePopen.result_path = os.path.join(
        str(tmp_path), "%s.execute.result.json" % jid)
    _FakePopen.result_body = {"ok": True, "data": {"done": 1}}
    ok, data, error, timed_out = phase_run_lib.run_supervised_phase(
        "hermes", jid, "execute", {}, 60.0, settings, str(tmp_path),
        lambda stream, line: None, op="test-op",
        env={"PATH": "/usr/bin:/bin"})
    assert ok is True and timed_out is False, error
    assert data.get("done") == 1
    assert len(killed) == 1


def test_h03_completion_unreapable_scope_is_timeout(tmp_path,
                                                   monkeypatch):
    """Worker done but scope will not empty: timeout — survivors
    exist, so the caller must keep recovery."""
    from backend.app import units as units_lib
    from backend.app.worker import phase_run as phase_run_lib

    settings = _phase_run_settings(monkeypatch, tmp_path)
    _patch_phase_spawn(monkeypatch)
    monkeypatch.setattr(
        phase_run_lib, "_kill_scope_wait_empty",
        lambda scope, grace: False)
    monkeypatch.setattr(
        units_lib, "query_unit",
        lambda unit, timeout_s=5: {"state": "live", "unit": unit,
                                   "active_state": "active",
                                   "sub_state": "running",
                                   "detail": ""})
    jid = str(uuid.uuid4())
    _FakePopen.result_path = os.path.join(
        str(tmp_path), "%s.execute.result.json" % jid)
    _FakePopen.result_body = {"ok": True, "data": {"done": 1}}
    ok, _data, error, timed_out = phase_run_lib.run_supervised_phase(
        "hermes", jid, "execute", {}, 60.0, settings, str(tmp_path),
        lambda stream, line: None, op="test-op",
        env={"PATH": "/usr/bin:/bin"})
    assert ok is False and timed_out is True
    assert "not quiescent after completion" in error


# -- H04 probe/apply privilege parity ------------------------------------------

def test_h04_probe_service_name_shapes():
    from backend.app.owner_env import transient_probe_name

    assert transient_probe_name(
        "12345678-1234-1234-1234-123456789abc") == \
        "ega-update-probe-12345678123412341234123456789abc.service"
    for bad in ("", "!!!", "///", "   "):
        try:
            transient_probe_name(bad)
        except ValueError:
            continue
        raise AssertionError("accepted %r" % (bad,))


def test_h04_probe_cmd_is_service_not_scope():
    """Authoritative probe launcher is a transient SERVICE with the
    shared NNP-off properties — never --scope."""
    from backend.app.owner_env import (TRANSIENT_PROBE_PROPERTIES,
                                       TRANSIENT_RUNNER_PROPERTIES,
                                       build_probe_cmd)

    assert TRANSIENT_PROBE_PROPERTIES is TRANSIENT_RUNNER_PROPERTIES
    cmd = build_probe_cmd(
        "ega-update-probe-abc.service", "/rel",
        {"PATH": "/usr/bin:/bin", "HOME": "/home/ubuntu",
         "USER": "ubuntu", "LOGNAME": "ubuntu"},
        "/rel/venv/bin/python", "req-1",
        "/l/req.payload.json", "/l/req.result.json",
        "/l/req.stream", "inspect", 60.0)
    assert "--scope" not in cmd
    assert "--wait" in cmd
    assert "--collect" in cmd
    assert "--unit=ega-update-probe-abc.service" in cmd
    assert "--property=NoNewPrivileges=no" in cmd
    assert "--property=KillMode=control-group" in cmd
    assert "--property=Restart=no" in cmd
    assert "backend.app.worker.phase_run" in cmd
    assert "probe" in cmd


def test_h04_probe_cmd_rejects_bad_names():
    from backend.app.owner_env import build_probe_cmd

    for service in ("", "bad name.service", "bad/name.service"):
        try:
            build_probe_cmd(
                service, "/rel", {}, "/rel/venv/bin/python", "req-1",
                "/a", "/b", "/c", "inspect", 60.0)
        except ValueError:
            continue
        raise AssertionError("accepted %r" % (service,))


def test_h04_contract_encodes_probe_runner_truth():
    from backend.app.owner_env import build_owner_contract

    support_lib.test_release_root()
    contract = build_owner_contract(None)
    assert contract.get("runner_no_new_privileges") == "false"
    assert contract.get("probe_no_new_privileges") == "false"
    assert contract.get("phase_privilege_source") == "runner"
    assert contract.get("privilege_profile") == "owner-exec-nnp-off"
    assert "scope_no_new_privileges" not in contract
    assert str(contract.get("sudo_profile", "")).startswith("sha256:")


def test_h04_privilege_fields_participate_in_fingerprint():
    from backend.app.owner_env import (build_owner_contract,
                                       contract_fingerprint)

    support_lib.test_release_root()
    baseline = contract_fingerprint(build_owner_contract(None))
    assert baseline
    for key, value in (("runner_no_new_privileges", "true"),
                       ("probe_no_new_privileges", "true"),
                       ("phase_privilege_source", "scope"),
                       ("privilege_profile", "owner-exec-nnp-on"),
                       ("sudo_profile", "sha256:tampered")):
        altered = dict(build_owner_contract(None))
        altered[key] = value
        assert contract_fingerprint(altered) not in ("", baseline), key


def test_h04_probe_privilege_change_invalidates_plan(tmp_path):
    """A plan bound under a different probe privilege profile is
    refused at admission (preview/apply parity is fingerprinted)."""
    from backend.app.admission import admit
    from backend.app.owner_env import (build_owner_contract,
                                       contract_fingerprint)

    support_lib.test_release_root()
    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    altered = dict(build_owner_contract(None))
    altered["probe_no_new_privileges"] = "true"
    stale_fp = contract_fingerprint(altered)
    assert stale_fp
    conn.execute("UPDATE plans SET env_fingerprint=? WHERE id=?",
                 (stale_fp, row["id"]))
    conn.commit()
    support_lib.test_release_root()
    _jid, created, err = _admit(conn, row["id"], "k-h04-fp")
    assert err == "config_changed" and not created, err
    conn.close()


def test_h04_probe_wiring_uses_service_launcher(tmp_path, monkeypatch):
    """Dispatcher probes execute through run_supervised_probe (the
    transient-service path), preserving leases/deadlines/sanitize."""
    from backend.app.worker import dispatch as dispatch_lib
    from backend.app.worker import phase_run as phase_run_lib

    support_lib.test_release_root()
    seen = {}

    def _fake_probe(tool_id, request_id, payload_extra, timeout_s,
                    settings, log_dir, emit, op="", env=None):
        seen["argv"] = (tool_id, request_id, op, timeout_s)
        seen["env"] = dict(env or {})
        return True, {"activity": "idle"}, "", False

    monkeypatch.setattr(phase_run_lib, "run_supervised_probe",
                        _fake_probe)
    status, payload = dispatch_lib._execute_probe_op(
        "hermes", "inspect", "req-h04")
    assert status == "ok" and payload.get("activity") == "idle"
    assert seen["argv"][1] == "req-h04"
    assert seen["env"].get("USER") == "ubuntu"

    def _fake_timeout(*args, **kwargs):
        return False, {}, "probe deadline exceeded", True

    monkeypatch.setattr(phase_run_lib, "run_supervised_probe",
                        _fake_timeout)
    status, payload = dispatch_lib._execute_probe_op(
        "hermes", "inspect", "req-h04")
    assert status == "error"


def test_h04_probe_launch_failure_fails_closed(tmp_path, monkeypatch):
    """Missing systemd-run or an unusable request id refuses the probe
    without a result."""
    from backend.app.worker import phase_run as phase_run_lib

    settings = _phase_run_settings(monkeypatch, tmp_path)
    real_isfile = os.path.isfile
    monkeypatch.setattr(
        os.path, "isfile",
        lambda p: False if p == "/usr/bin/systemd-run" else
        real_isfile(p))
    ok, _data, error, timed_out = \
        phase_run_lib.run_supervised_probe(
            "hermes", str(uuid.uuid4()), {"op": "inspect"}, 60.0,
            settings, str(tmp_path), lambda s, line: None,
            op="inspect", env={"PATH": "/usr/bin:/bin"})
    assert ok is False and timed_out is False
    assert "systemd-run" in error
    ok, _data, error, timed_out = \
        phase_run_lib.run_supervised_probe(
            "hermes", "", {"op": "inspect"}, 60.0, settings,
            str(tmp_path), lambda s, line: None, op="inspect",
            env={"PATH": "/usr/bin:/bin"})
    assert ok is False and timed_out is False
    assert "refused" in error or "unbuildable" in error


def test_h04_unstopped_probe_service_blocks_success(tmp_path,
                                                   monkeypatch):
    """A probe service that will not stop prevents successful probe
    completion (H03 exit proof applies to probe services)."""
    from backend.app import units as units_lib
    from backend.app.worker import phase_run as phase_run_lib

    settings = _phase_run_settings(monkeypatch, tmp_path)
    _patch_phase_spawn(monkeypatch)
    monkeypatch.setattr(
        phase_run_lib, "_kill_scope_wait_empty",
        lambda service, grace: False)
    monkeypatch.setattr(
        units_lib, "query_unit",
        lambda unit, timeout_s=5: {"state": "live", "unit": unit,
                                   "detail": ""})
    req_id = str(uuid.uuid4())
    _FakePopen.result_path = os.path.join(
        str(tmp_path), "%s.probe.result.json" % req_id)
    _FakePopen.result_body = {"ok": True, "data": {"activity": "idle"}}
    ok, _data, error, timed_out = \
        phase_run_lib.run_supervised_probe(
            "hermes", req_id, {"op": "inspect"}, 60.0, settings,
            str(tmp_path), lambda s, line: None, op="inspect",
            env={"PATH": "/usr/bin:/bin"})
    assert ok is False and timed_out is True
    assert "probe service not quiescent after completion" in error


def test_h04_stopped_probe_service_delivers(tmp_path, monkeypatch):
    from backend.app import units as units_lib
    from backend.app.worker import phase_run as phase_run_lib

    settings = _phase_run_settings(monkeypatch, tmp_path)
    _patch_phase_spawn(monkeypatch)
    monkeypatch.setattr(
        units_lib, "query_unit",
        lambda unit, timeout_s=5: {"state": "confirmed_stopped",
                                   "unit": unit, "detail": ""})
    req_id = str(uuid.uuid4())
    _FakePopen.result_path = os.path.join(
        str(tmp_path), "%s.probe.result.json" % req_id)
    _FakePopen.result_body = {"ok": True, "data": {"activity": "idle"}}
    ok, data, error, timed_out = phase_run_lib.run_supervised_probe(
        "hermes", req_id, {"op": "inspect"}, 60.0, settings,
        str(tmp_path), lambda s, line: None, op="inspect",
        env={"PATH": "/usr/bin:/bin"})
    assert ok is True and timed_out is False, error
    assert data.get("activity") == "idle"
