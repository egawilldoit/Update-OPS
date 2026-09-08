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
