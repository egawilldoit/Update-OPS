"""D5 probe ownership regression tests (write-only artifacts).

Core invariant under test: NO MUTATION MAY START WHILE (a) a probe
process is live, OR (b) probe stop state is unknown. Lease TTL expiry is
NOT proof of process death; exclusion may be released only through
reconciliation with positive stop proof from the explicit unit model.

Hermetic: a tmp sqlite DB plus a fake unit-state provider
(support.FakeUnitStates: explicit live / confirmed_stopped / unknown).
No test in this module queries or mutates real systemd state.
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
from backend.app import leases as leases_lib
from backend.app import owner_probes as probes_lib
from backend.app.admission import admit
from backend.app.worker import dispatch as dispatch_lib

import support as support_lib

_PAST = "2000-01-01T00:00:00+00:00"


def _fresh_db(tmp_path, name="d5.db"):
    # type: (object, str) -> object
    conn = db_lib.connect(str(tmp_path / name))
    assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    conn.commit()
    return conn


def _probe_unit(request_id):
    # type: (str) -> str
    from backend.app.owner_env import transient_probe_name
    return transient_probe_name(request_id)


def _expire(conn, lease_id):
    # type: (object, str) -> None
    conn.execute("UPDATE execution_leases SET expires_at=? WHERE id=?",
                 (_PAST, lease_id))
    conn.commit()


def _unreleased_probe_leases(conn):
    # type: (object) -> list
    return conn.execute(
        "SELECT * FROM execution_leases WHERE kind='probe'"
        " AND released_at=''").fetchall()


def _admit(conn, plan_id, key):
    # type: (object, str, str) -> tuple
    support_lib.test_release_root()
    return admit(conn, "owner@example.invalid", key, plan_id, False,
                 "fp-test-1", True, False)


# -- D5.1 dispatcher dies while the probe runs --------------------------------

def test_d5_dispatcher_death_probe_blocks_until_proof(tmp_path):
    """A probe lease whose owner (dispatcher) is gone must keep blocking
    mutation admission while the transient probe service is live, and
    even after its TTL passes. Only positive stop proof releases it."""
    conn = _fresh_db(tmp_path)
    plan = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    request_id = str(uuid.uuid4())
    unit = _probe_unit(request_id)
    lease = leases_lib.acquire_probe_lease(
        conn, "hermes", "dispatcher-dead", request_id=request_id)
    assert lease
    # Bound identity: request id + canonical probe unit, and expiry is
    # recorded as an advisory TTL, never as permission to overlap.
    row = dict(conn.execute(
        "SELECT * FROM execution_leases WHERE id=?",
        (lease,)).fetchone())
    assert row["request_id"] == request_id
    assert row["subject"] == unit
    assert row["expires_at"] > _PAST
    # No dispatcher is alive to release it: the lease itself must block.
    _jid, created, err = _admit(conn, plan["id"], "k-d5-death-1")
    assert err == "busy" and not created
    # TTL passes while the unit is still live: time is not proof.
    _expire(conn, lease)
    fake = support_lib.FakeUnitStates({unit: "live"})
    assert leases_lib.active_probe_leases(conn)
    _jid, created, err = _admit(conn, plan["id"], "k-d5-death-1")
    assert err == "busy" and not created
    report = leases_lib.reconcile_probe_leases(conn, units_mod=fake)
    assert report["released"] == 0
    assert report["held"] == 1
    assert report["evidence"][0]["state"] == "live"
    assert _unreleased_probe_leases(conn)
    # Positive proof arrives only when the service is confirmed stopped.
    fake.set(unit, "confirmed_stopped")
    assert leases_lib.reconcile_expired_probes(conn, units_mod=fake) == 1
    assert _unreleased_probe_leases(conn) == []
    _jid, created, err = _admit(conn, plan["id"], "k-d5-death-2")
    assert err == "" and created, err
    conn.close()


# -- D5.2 lease expires while the unit is still active ------------------------

def test_d5_expired_live_unit_still_blocks_and_reconcile_reports_live(
        tmp_path):
    """An expired lease over a LIVE unit still blocks admission; the
    reconciler reports live and refuses any release."""
    conn = _fresh_db(tmp_path)
    plan = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    request_id = str(uuid.uuid4())
    unit = _probe_unit(request_id)
    lease = leases_lib.acquire_probe_lease(
        conn, "hermes", "dispatcher-gone", request_id=request_id)
    assert lease
    _expire(conn, lease)
    fake = support_lib.FakeUnitStates({unit: "live"})
    report = leases_lib.reconcile_probe_leases(conn, units_mod=fake)
    assert report["released"] == 0
    entry = report["evidence"][0]
    assert entry["unit"] == unit
    assert entry["state"] == "live"
    assert entry["action"] == "held"
    assert leases_lib.active_probe_leases(conn)
    _jid, created, err = _admit(conn, plan["id"], "k-d5-live")
    assert err == "busy" and not created
    conn.close()


# -- D5.3 systemd query returns unknown ---------------------------------------

def test_d5_unknown_unit_state_stays_blocking(tmp_path):
    """Unknown unit state can never release exclusion: the lease stays
    held (recovery-required equivalent) and admission stays refused."""
    conn = _fresh_db(tmp_path)
    plan = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    request_id = str(uuid.uuid4())
    unit = _probe_unit(request_id)
    lease = leases_lib.acquire_probe_lease(
        conn, "hermes", "dispatcher-unknown", request_id=request_id)
    assert lease
    _expire(conn, lease)
    fake = support_lib.FakeUnitStates({unit: "unknown"})
    report = leases_lib.reconcile_probe_leases(conn, units_mod=fake)
    assert report["released"] == 0 and report["held"] == 1
    assert report["evidence"][0]["state"] == "unknown"
    assert leases_lib.active_probe_leases(conn)
    _jid, created, err = _admit(conn, plan["id"], "k-d5-unknown")
    assert err == "busy" and not created
    conn.close()


# -- D5.4 kill/stop cannot be proven ------------------------------------------

def test_d5_unproven_stop_keeps_lease_and_blocks_mutation(tmp_path,
                                                          monkeypatch):
    """run_probe_queue releases a probe lease ONLY on proven stop. A
    supervised probe that returns non-quiescent leaves the lease held so
    no mutation can start over the surviving process."""
    from backend.app import units as units_lib
    from backend.app.worker import phase_run as phase_run_lib

    support_lib.test_release_root()
    conn = _fresh_db(tmp_path)
    fake = support_lib.FakeUnitStates(default="live")
    monkeypatch.setattr(units_lib, "query_unit", fake.query_unit)
    request_id = probes_lib.enqueue_probe(conn, "test", "hermes",
                                          "inspect")
    unit = _probe_unit(request_id)

    def _not_quiescent(*args, **kwargs):
        return False, {}, "probe service not quiescent after kill", True

    monkeypatch.setattr(phase_run_lib, "run_supervised_probe",
                        _not_quiescent)
    assert dispatch_lib.run_probe_queue(conn) == 1
    held = _unreleased_probe_leases(conn)
    assert len(held) == 1
    assert held[0]["request_id"] == request_id
    assert held[0]["subject"] == unit
    result = conn.execute("SELECT * FROM probe_results WHERE request_id=?",
                          (request_id,)).fetchone()
    assert result is not None and result["status"] == "error"
    plan = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    _jid, created, err = _admit(conn, plan["id"], "k-d5-unproven")
    assert err == "busy" and not created
    conn.close()


# -- D5.5 unit confirmed gone -------------------------------------------------

def test_d5_confirmed_stopped_reconciliation_releases(tmp_path):
    """Positive unit-stop proof is the only releaser: reconciliation
    releases the lease and mutation admission opens again."""
    conn = _fresh_db(tmp_path)
    plan = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    request_id = str(uuid.uuid4())
    unit = _probe_unit(request_id)
    lease = leases_lib.acquire_probe_lease(
        conn, "hermes", "dispatcher-stopped", request_id=request_id)
    assert lease
    _expire(conn, lease)
    fake = support_lib.FakeUnitStates({unit: "confirmed_stopped"})
    report = leases_lib.reconcile_probe_leases(conn, units_mod=fake)
    assert report["released"] == 1 and report["held"] == 0
    entry = report["evidence"][0]
    assert entry["state"] == "confirmed_stopped"
    assert entry["action"] == "released"
    assert _unreleased_probe_leases(conn) == []
    _jid, created, err = _admit(conn, plan["id"], "k-d5-gone")
    assert err == "" and created, err
    conn.close()


# -- D5.6 normal successful probe releases promptly ---------------------------

def test_d5_normal_success_releases_promptly(tmp_path, monkeypatch):
    """The corrected path must not create permanent blocking: a probe
    that completes with proven stop releases immediately and the next
    reservation succeeds."""
    from backend.app import units as units_lib
    from backend.app.worker import phase_run as phase_run_lib

    support_lib.test_release_root()
    conn = _fresh_db(tmp_path)
    monkeypatch.setattr(
        units_lib, "query_unit",
        lambda unit, timeout_s=5: {"state": "live", "unit": unit,
                                   "detail": "must not be needed"})
    request_id = probes_lib.enqueue_probe(conn, "test", "hermes",
                                          "inspect")

    def _ok(*args, **kwargs):
        return True, {"activity": "idle"}, "", False

    monkeypatch.setattr(phase_run_lib, "run_supervised_probe", _ok)
    assert dispatch_lib.run_probe_queue(conn) == 1
    assert _unreleased_probe_leases(conn) == []
    result = conn.execute("SELECT * FROM probe_results WHERE request_id=?",
                          (request_id,)).fetchone()
    assert result is not None and result["status"] == "ok"
    plan = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    _jid, created, err = _admit(conn, plan["id"], "k-d5-success")
    assert err == "" and created, err
    conn.close()


# -- D5.7 reboot/reconciliation: releases only with proof ---------------------

def test_d5_boot_reconciliation_releases_only_with_proof(tmp_path,
                                                         monkeypatch):
    """After a reboot the transient units are gone, but release still
    requires per-unit confirmation; a live survivor keeps its lease."""
    from backend.app import units as units_lib

    conn = _fresh_db(tmp_path)
    rid_gone = str(uuid.uuid4())
    rid_live = str(uuid.uuid4())
    unit_gone = _probe_unit(rid_gone)
    unit_live = _probe_unit(rid_live)
    lease_gone = leases_lib.acquire_probe_lease(
        conn, "hermes", "boot-dead-a", request_id=rid_gone)
    lease_live = leases_lib.acquire_probe_lease(
        conn, "hermes", "boot-dead-b", request_id=rid_live)
    assert lease_gone and lease_live
    fake = support_lib.FakeUnitStates({
        unit_gone: "confirmed_stopped", unit_live: "live"})
    monkeypatch.setattr(units_lib, "query_unit", fake.query_unit)
    dispatch_lib.reconcile_boot(conn)
    rows = {str(r["id"]): str(r["released_at"] or "")
            for r in conn.execute(
                "SELECT * FROM execution_leases WHERE kind='probe'")}
    assert rows[lease_gone], "confirmed-gone probe lease must release"
    assert not rows[lease_live], "live probe lease must stay held"
    conn.close()


# -- D5.8 admission refused while an unresolved probe exists ------------------

def test_d5_admission_refused_while_unresolved_probe_exists(tmp_path):
    """Mutation admission reads the durable probe exclusion: an
    unresolved (expired, unproven) probe lease refuses reservation; only
    proven stop clears it."""
    conn = _fresh_db(tmp_path)
    plan = support_lib.v2_plan_row(conn, uuid.uuid4().hex)
    request_id = str(uuid.uuid4())
    unit = _probe_unit(request_id)
    lease = leases_lib.acquire_probe_lease(
        conn, "hermes", "dispatcher-stuck", request_id=request_id)
    assert lease
    _expire(conn, lease)
    fake = support_lib.FakeUnitStates({unit: "unknown"})
    _jid, created, err = _admit(conn, plan["id"], "k-d5-admit")
    assert err == "busy" and not created
    assert leases_lib.active_probe_leases(conn)
    assert leases_lib.reconcile_expired_probes(conn, units_mod=fake) == 0
    _jid, created, err = _admit(conn, plan["id"], "k-d5-admit")
    assert err == "busy" and not created
    fake.set(unit, "confirmed_stopped")
    assert leases_lib.reconcile_expired_probes(conn, units_mod=fake) == 1
    _jid, created, err = _admit(conn, plan["id"], "k-d5-admit")
    assert err == "" and created, err
    conn.close()
