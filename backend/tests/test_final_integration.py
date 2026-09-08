"""Final static integration regression tests (F01-F16, write-only).

Behavioral regression artifacts for every F-finding. NOT EXECUTED in
this phase per instruction. Mocks stay at external boundaries
(systemd bus, process spawn); console-component integration is never
mocked away. Python 3.10 compatible, pytest style, stdlib + backend.
"""
from __future__ import annotations

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
from backend.app import plans as plans_lib

import support as support_lib


def _fresh_db(tmp_path, name="f.db"):
    conn = db_lib.connect(str(tmp_path / name))
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.commit()
    return conn


def _terminal_job_with_lease(conn, key="k-f02"):
    """Admit + terminalize WITHOUT ownership release (F02: runner-side
    finalization never disposes the lease). Returns job id."""
    from backend.app.admission import admit
    from backend.app import tx as tx_lib

    support_lib.test_release_root()
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid, created, err = admit(conn, "owner@example.invalid", key,
                              row["id"], False, "fp-test-1", True, False)
    assert err == "" and created, err
    tx_lib.transition_tx(conn, jid, "succeeded", step="verifying",
                         expect_states=["accepted"],
                         update={"after_version": "9.9.9"},
                         event="succeeded", event_detail="")
    return jid


def _lease_held(conn, job_id):
    row = conn.execute(
        "SELECT released_at FROM execution_leases WHERE kind='mutation'"
        " AND job_id=?", (job_id,)).fetchone()
    return row is not None and not str(row["released_at"] or "")


def _live_units(monkeypatch, live=(), unknown=()):
    from backend.app import units as units_lib

    def _fake(unit, timeout_s=10):
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
    monkeypatch.setattr(units_lib, "query_unit", _fake)
    monkeypatch.setattr(units_lib, "query_unit_system", _fake)


# -- F01 canonical plan hash ---------------------------------------------------

def test_f01_build_insert_load_roundtrip_unmodified(tmp_path):
    """build_plan_row -> insert_plan -> load_plan succeeds with the hash
    exactly as built (no fixture repair, no manual recompute)."""
    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    loaded = plans_lib.load_plan(conn, row["id"])
    assert loaded["plan_hash"] == row["plan_hash"]
    conn.close()


def test_f01_env_release_config_target_mutations_break_hash(tmp_path):
    """Mutating any hashed binding invalidates the plan at load."""
    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    originals = {}
    for column in ("env_fingerprint", "release_path", "config_hash",
                   "target", "fingerprint"):
        originals[column] = row[column]
    for column, value in (
            ("env_fingerprint", "mutated-env"),
            ("release_path", "/mutated-release"),
            ("config_hash", "mutated-config"),
            ("target", "0.0.0-mutated"),
            ("fingerprint", "mutated-fp")):
        conn.execute("UPDATE plans SET %s=? WHERE id=?" % column,
                     (value, row["id"]))
        conn.commit()
        with pytest.raises(plans_lib.PlanInvalid):
            plans_lib.load_plan(conn, row["id"])
        conn.execute("UPDATE plans SET %s=? WHERE id=?" % column,
                     (originals[column], row["id"]))
        conn.commit()
    # Restored row loads cleanly again.
    assert plans_lib.load_plan(conn, row["id"])["plan_hash"] == \
        row["plan_hash"]
    conn.close()


# -- F03 unified owner execution contract --------------------------------------

def _contract(**over):
    from backend.app.owner_env import build_owner_contract

    support_lib.test_release_root()
    base = {"LANG": "C.UTF-8", "TZ": "UTC"}
    base.update(over)
    return build_owner_contract(None, None, "", base)


def test_f03_probe_env_equals_apply_env():
    """Runner launch env and probe launch env derive from ONE contract:
    identical except the attempt nonce (probes never consume attempts)."""
    from backend.app.owner_env import contract_env, contract_fingerprint

    contract = _contract()
    probe_env = contract_env(contract)
    apply_env = contract_env(contract, nonce="n-1")
    for key, value in probe_env.items():
        assert apply_env.get(key) == value, key
    assert apply_env["EGA_ATTEMPT_NONCE"] == "n-1"
    assert "EGA_ATTEMPT_NONCE" not in probe_env
    assert contract_fingerprint(contract)
    # Same inputs rebuild the identical contract (deterministic).
    again = _contract()
    assert again == contract


def test_f03_every_contract_field_moves_fingerprint(monkeypatch):
    """Bus/locale/release/owner-identity/privilege changes each move the
    fingerprint (F03: no conceptual approximation).

    Note: raw environ HOME/USER/PATH are deliberately NOT contract
    inputs — the contract takes uid/gid/user/home from passwd and PATH
    from the configured node bin, so a stray login-shell override can
    neither sneak in nor invalidate. Owner identity itself (tool_owner)
    does move the fingerprint.
    """
    from backend.app.config import settings as settings_lib
    from backend.app.owner_env import contract_fingerprint

    baseline = contract_fingerprint(_contract())
    assert baseline
    for variant in (
            _contract(XDG_RUNTIME_DIR="/run/user/9999"),
            _contract(DBUS_SESSION_BUS_ADDRESS="unix:path=/other/bus"),
            _contract(LANG="fr_FR.UTF-8"),
            _contract(TZ="America/New_York")):
        assert contract_fingerprint(variant) not in ("", baseline)
    monkeypatch.setattr(settings_lib, "tool_owner", "other-owner")
    assert contract_fingerprint(_contract()) != baseline


def test_f03_release_node_privilege_move_fingerprint(monkeypatch):
    from backend.app import inventory as inventory_lib
    from backend.app import owner_env as owner_env_lib
    from backend.app.config import settings as settings_lib

    support_lib.test_release_root()
    baseline = owner_env_lib.contract_fingerprint(
        owner_env_lib.build_owner_contract())
    assert baseline
    assert owner_env_lib.contract_fingerprint(
        owner_env_lib.build_owner_contract(
            None, None, "/other-release")) != baseline
    monkeypatch.setattr(settings_lib, "node_path",
                        "/other/node/bin/node")
    assert owner_env_lib.contract_fingerprint(
        owner_env_lib.build_owner_contract()) != baseline


def test_f03_sudo_profile_change_moves_fingerprint(monkeypatch):
    from backend.app import inventory as inventory_lib
    from backend.app import owner_env as owner_env_lib

    support_lib.test_release_root()
    baseline = owner_env_lib.contract_fingerprint(
        owner_env_lib.build_owner_contract())
    monkeypatch.setattr(
        inventory_lib, "get_tool_inventory",
        lambda tool_id: {"service_units": [
            {"unit": "hermes-gateway@x.service",
             "restart_authority": "owner-sudo"}],
            "launch_method": "systemd --user"} if tool_id == "hermes"
        else {})
    changed = owner_env_lib.contract_fingerprint(
        owner_env_lib.build_owner_contract())
    assert changed and changed != baseline


def test_f03_stale_env_fingerprint_blocks_admission(tmp_path):
    """A plan bound to an older environment is invalidated at admission
    (config_changed), never silently applied under a new contract.

    The stored plan keeps a CONSISTENT hash for the stale environment
    (recomputed like production does), so the test exercises exactly
    the env gate rather than the tamper gate.
    """
    from backend.app import plans as plans_lib
    from backend.app.admission import admit

    conn = _fresh_db(tmp_path)
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    stored = dict(conn.execute("SELECT * FROM plans WHERE id=?",
                              (row["id"],)).fetchone())
    stored["env_fingerprint"] = "stale-environment-fingerprint"
    fresh_hash = plans_lib.canonical_plan_hash(
        plans_lib._hash_view(stored))
    conn.execute("UPDATE plans SET env_fingerprint=?, plan_hash=?"
                 " WHERE id=?",
                 ("stale-environment-fingerprint", fresh_hash,
                  row["id"]))
    conn.commit()
    jid, created, err = admit(
        conn, "owner@example.invalid", "k-f03", row["id"], False,
        "fp-test-1", True, False)
    assert err == "config_changed" and not created
    conn.close()


# -- F02 mutation ownership lifetime -------------------------------------------

def test_f02_terminal_live_unit_keeps_lease(tmp_path, monkeypatch):
    """Terminal DB state with a live runner unit: lease stays held and
    no new job is admitted (terminal != quiescence)."""
    from backend.app.admission import admit
    from backend.app.reconcile_core import canonical_unit
    from backend.app.worker import dispatch as dispatch_lib

    conn = _fresh_db(tmp_path)
    jid = _terminal_job_with_lease(conn, key="k-f02-live")
    assert _lease_held(conn, jid)
    _live_units(monkeypatch, live=[canonical_unit(jid)])
    row = conn.execute("SELECT * FROM jobs WHERE id=?",
                       (jid,)).fetchone()
    assert dispatch_lib._reconcile_row(conn, row) == "live"
    assert _lease_held(conn, jid)
    # New admission is refused while another ownership is held.
    row2 = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    _jid2, _created2, err2 = admit(
        conn, "owner@example.invalid", "k-f02-live-new", row2["id"],
        False, "fp-test-1", True, False)
    assert err2 == "busy" and not _created2
    conn.close()


def test_f02_terminal_unknown_unit_keeps_lease(tmp_path, monkeypatch):
    from backend.app.reconcile_core import canonical_unit
    from backend.app.worker import dispatch as dispatch_lib

    conn = _fresh_db(tmp_path)
    jid = _terminal_job_with_lease(conn, key="k-f02-unk")
    _live_units(monkeypatch, unknown=[canonical_unit(jid)])
    row = conn.execute("SELECT * FROM jobs WHERE id=?",
                       (jid,)).fetchone()
    assert dispatch_lib._reconcile_row(conn, row) == "unknown-held"
    assert _lease_held(conn, jid)
    conn.close()


def test_f02_terminal_stopped_releases_lease(tmp_path, monkeypatch):
    """Terminal job + confirmed stopped unit + no procs: the reconciler
    releases ownership; afterwards admission proceeds."""
    from backend.app.admission import admit
    from backend.app.worker import dispatch as dispatch_lib

    conn = _fresh_db(tmp_path)
    jid = _terminal_job_with_lease(conn, key="k-f02-rel")
    _live_units(monkeypatch)
    row = conn.execute("SELECT * FROM jobs WHERE id=?",
                       (jid,)).fetchone()
    assert dispatch_lib._reconcile_row(conn, row) == "ownership-released"
    assert not _lease_held(conn, jid)
    row2 = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    jid2, created2, err2 = admit(
        conn, "owner@example.invalid", "k-f02-rel-new", row2["id"],
        False, "fp-test-1", True, False)
    assert err2 == "" and created2 and jid2
    conn.close()


def test_f02_missing_job_lease_never_auto_released(tmp_path):
    """A mutation lease pointing at a missing job blocks admission and
    is never silently disposed by the acquire path."""
    from backend.app import leases as leases_lib
    from backend.app.admission import admit

    conn = _fresh_db(tmp_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO execution_leases(id,kind,subject,tool_id,job_id,"
        "request_id,holder,acquired_at,expires_at,released_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("mutation-ghost", "mutation", "", "", "ghost-job", "",
         "test", "2026-09-08T00:00:00+00:00", "", ""))
    conn.execute("COMMIT")
    row = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    _jid, created, err = admit(
        conn, "owner@example.invalid", "k-f02-ghost", row["id"], False,
        "fp-test-1", True, False)
    assert err == "busy" and not created
    ghost = conn.execute(
        "SELECT released_at FROM execution_leases WHERE id=?"
        " AND kind='mutation'", ("mutation-ghost",)).fetchone()
    assert ghost is not None and not str(ghost["released_at"] or "")
    conn.close()


def test_f02_release_failure_keeps_admission_blocked(tmp_path):
    """A failed ownership commit keeps the lease held: admission stays
    blocked instead of silently proceeding (no try/except-pass)."""
    import sqlite3
    from backend.app import tx as tx_lib
    from backend.app.admission import admit
    from backend.app.reconcile_core import canonical_unit

    conn = _fresh_db(tmp_path)
    jid = _terminal_job_with_lease(conn, key="k-f02-fail")

    class _FailRelease(object):
        def __init__(self, real):
            self._real = real

        def execute(self, sql, params=()):
            if isinstance(sql, str) and "execution_leases" in sql \
                    and "released_at" in sql:
                raise sqlite3.OperationalError("injected lease failure")
            return self._real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

    with pytest.raises(tx_lib.TxError):
        tx_lib.release_ownership(
            _FailRelease(conn), jid, expect_states=["succeeded"],
            event="ownership_released", event_detail="test")
    assert _lease_held(conn, jid)
    row2 = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    _jid2, created2, err2 = admit(
        conn, "owner@example.invalid", "k-f02-fail-new", row2["id"],
        False, "fp-test-1", True, False)
    assert err2 == "busy" and not created2
    assert canonical_unit(jid)
    conn.close()
