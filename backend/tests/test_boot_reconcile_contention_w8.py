"""W8 boot-reconciliation reliability tests: no silent transient skip.

Deterministic SQLite contention (a REAL held write transaction on a second
connection plus busy_timeout=0 on the boot connection) proves the W8
contract:

- boot reconciliation distinguishes safe holds (live/unknown unit,
  unresolved job) from transient storage contention (SQLITE_BUSY/locked)
  from persistent storage failure;
- transient contention is retried with a short bounded backoff;
- persistent contention fails loudly (ReconcileError -> SystemExit) and
  NEVER releases a lease (unknown reconciliation -> lease held);
- confirmed-stopped probes still release exactly once, and observation
  application still happens before exclusion release;
- a concurrent ProbeWorker connection cannot make boot reconciliation
  nondeterministic.

Hermetic: tmp sqlite DB, fake unit states, no systemd, no /opt, /etc,
/var/lib access.
"""
from __future__ import annotations

import os
import sys
import threading
import time
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
from backend.app import observation as observation_lib
from backend.app import owner_probes as probes_lib
from backend.app import units as units_lib
from backend.app.config import settings as settings_lib
from backend.app.worker import dispatch as dispatch_lib

import support as support_lib


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _isolate_settings(tmp_path, monkeypatch):
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    log_dir = str(tmp_path / "logs")
    os.makedirs(log_dir, exist_ok=True)
    db_path = str(tmp_path / "state.db")
    monkeypatch.setattr(settings_lib, "state_dir", state_dir)
    monkeypatch.setattr(settings_lib, "db_path", db_path)
    monkeypatch.setattr(settings_lib, "log_dir", log_dir)
    return state_dir, db_path, log_dir


def _fresh_db(db_path):
    conn = db_lib.connect(db_path)
    db_lib.migrate(conn)
    conn.commit()
    return conn


def _probe_unit(request_id):
    from backend.app.owner_env import transient_probe_name
    return transient_probe_name(request_id)


def _patch_units(monkeypatch, mapping=None, default="unknown"):
    fake = support_lib.FakeUnitStates(mapping or {}, default=default)
    monkeypatch.setattr(units_lib, "query_unit", fake.query_unit)
    return fake


def _hold_write_lock(db_path):
    """A REAL held write transaction on a second connection.

    BEGIN IMMEDIATE + a write acquires and keeps the single SQLite write
    lock; the boot connection (busy_timeout=0) then fails deterministically
    with SQLITE_BUSY instead of waiting. No timing sleeper is involved.
    """
    blocker = db_lib.connect(db_path)
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute(
        "UPDATE tools SET updated_at='w8-held' WHERE id='hermes'")
    return blocker


def _release_write_lock(blocker):
    try:
        blocker.execute("ROLLBACK")
    except Exception:
        pass
    try:
        blocker.close()
    except Exception:
        pass


def _boot_conn(db_path):
    conn = db_lib.connect(db_path)
    db_lib.migrate(conn)
    conn.commit()
    conn.execute("PRAGMA busy_timeout=0")
    return conn


def _unreleased_probe_leases(conn):
    return conn.execute(
        "SELECT * FROM execution_leases WHERE kind='probe'"
        " AND released_at=''").fetchall()


def _lease_row(conn, lease_id):
    return dict(conn.execute(
        "SELECT * FROM execution_leases WHERE id=?",
        (lease_id,)).fetchone())


def _seed_lease(conn, tool_id="hermes"):
    """Unreleased probe lease bound to a canonical probe unit."""
    request_id = str(uuid.uuid4())
    lease = leases_lib.acquire_probe_lease(
        conn, tool_id, "dispatcher-dead", request_id=request_id)
    assert lease
    return request_id, lease


def _seed_running_probe(conn):
    """Claimed running request + held probe lease (restart scenario)."""
    rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
    assert probes_lib.claim_probe(conn, "dispatcher-dead")
    lease = leases_lib.acquire_probe_lease(
        conn, "hermes", "dispatcher-dead", request_id=rid)
    assert lease
    return rid, lease


def _release_lock_on_first_retry(monkeypatch, blocker):
    """Deterministic temporary contention: the held write transaction is
    rolled back exactly when the boot retry loop waits between attempts."""
    sleeps = []
    real_sleep = dispatch_lib._boot_sleep

    def _sleeper(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            try:
                blocker.execute("ROLLBACK")
            except Exception:
                pass
            return
        real_sleep(min(float(seconds), 0.02))

    monkeypatch.setattr(dispatch_lib, "_boot_sleep", _sleeper)
    return sleeps


def _instant_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(
        dispatch_lib, "_boot_sleep",
        lambda seconds: sleeps.append(seconds))
    return sleeps


def _refresh_payload(version="2.0.0"):
    return {
        "inspection": {"install_identity": "hermes-display-identity",
                       "version": version, "fingerprint": "fp-w8",
                       "channel": "stable"},
        "discovery": {"target": "2.0.1", "channel": "stable",
                      "available": True, "unknown_reason": ""},
        "activity": {"state": "idle", "evidence": "idle"},
        "verification": {
            "passed": True, "version": version,
            "checks": [{"name": "smoke", "result": "pass",
                        "mandatory": True, "summary": "ok"}]},
    }


# ---------------------------------------------------------------------------
# 1. deterministic SQLite lock during probe-request reconciliation
# ---------------------------------------------------------------------------

def test_lock_during_probe_request_reconcile_is_retried_not_swallowed(
        tmp_path, monkeypatch):
    """A held write transaction makes the request-resume UPDATE fail with
    SQLITE_BUSY; boot retries and resumes instead of silently returning."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _boot_conn(db_path)
    try:
        rid, lease = _seed_running_probe(conn)
        unit = _probe_unit(rid)
        _patch_units(monkeypatch, {unit: "confirmed_stopped"})
        blocker = _hold_write_lock(db_path)
        sleeps = _release_lock_on_first_retry(monkeypatch, blocker)
        try:
            report = dispatch_lib.reconcile_boot(conn)
        finally:
            _release_write_lock(blocker)
        assert report["probe_request_reconcile"]["resumed"] == 1, report
        assert conn.execute(
            "SELECT state FROM probe_requests WHERE id=?",
            (rid,)).fetchone()["state"] == "queued"
        assert sleeps, "retry backoff was never exercised"
        # Once the request step committed, the lease step (same proof)
        # also completed: the exclusion was released on proven stop.
        assert report["probe_lease_reconcile"]["released"] == 1, report
        assert _unreleased_probe_leases(conn) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. deterministic SQLite lock during probe-lease reconciliation
# ---------------------------------------------------------------------------

def test_lock_during_probe_lease_reconcile_is_retried_not_swallowed(
        tmp_path, monkeypatch):
    """The strict release hits SQLITE_BUSY; boot retries until the lock
    clears and the lease is released (never silently reported as held)."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _boot_conn(db_path)
    try:
        rid, lease = _seed_lease(conn)
        _patch_units(monkeypatch, {_probe_unit(rid): "confirmed_stopped"})
        blocker = _hold_write_lock(db_path)
        sleeps = _release_lock_on_first_retry(monkeypatch, blocker)
        try:
            report = dispatch_lib.reconcile_boot(conn)
        finally:
            _release_write_lock(blocker)
        assert report["probe_request_reconcile"].get("checked", 0) == 0
        assert report["probe_lease_reconcile"]["released"] == 1, report
        assert _lease_row(conn, lease)["released_at"] != ""
        assert sleeps, "retry backoff was never exercised"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. temporary lock clears within the retry window -> succeeds
# ---------------------------------------------------------------------------

def test_temporary_lock_clears_within_retry_window_releases(
        tmp_path, monkeypatch):
    """A real lock held only briefly: the real bounded backoff retries and
    the release succeeds on the second attempt."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _boot_conn(db_path)
    try:
        rid, lease = _seed_lease(conn)
        _patch_units(monkeypatch, {_probe_unit(rid): "confirmed_stopped"})
        attempts = []
        real_release = leases_lib.release_lease

        def _counting_release(c, lease_id, fail_on_storage_error=False):
            attempts.append(str(lease_id))
            return real_release(c, lease_id, fail_on_storage_error)

        monkeypatch.setattr(leases_lib, "release_lease", _counting_release)
        blocker = _hold_write_lock(db_path)

        def _clear_lock():
            time.sleep(0.05)
            try:
                blocker.execute("ROLLBACK")
            except Exception:
                pass

        clearer = threading.Thread(target=_clear_lock)
        clearer.start()
        try:
            report = dispatch_lib.reconcile_boot(conn)
        finally:
            clearer.join(5)
            _release_write_lock(blocker)
        assert attempts.count(str(lease)) == 2, attempts
        assert report["probe_lease_reconcile"]["released"] == 1, report
        assert _lease_row(conn, lease)["released_at"] != ""
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. lock persists past the bound -> loud failure, startup cannot proceed
# ---------------------------------------------------------------------------

def test_persistent_lock_fails_boot_loudly_and_holds_every_lease(
        tmp_path, monkeypatch):
    """If contention outlasts the bounded retry, boot raises ReconcileError
    and the startup gate exits; the lease stays held (never released on
    failure)."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _boot_conn(db_path)
    try:
        rid, lease = _seed_lease(conn)
        _patch_units(monkeypatch, {_probe_unit(rid): "confirmed_stopped"})
        sleeps = _instant_sleep(monkeypatch)
        blocker = _hold_write_lock(db_path)
        try:
            with pytest.raises(dispatch_lib.ReconcileError) as excinfo:
                dispatch_lib.reconcile_boot(conn)
            err = excinfo.value
            assert err.step == "probe_lease_reconcile", err
            assert err.attempts == dispatch_lib.BOOT_RECONCILE_ATTEMPTS
            assert len(sleeps) == dispatch_lib.BOOT_RECONCILE_ATTEMPTS - 1
            # SAFETY: unknown reconciliation -> lease held.
            assert _lease_row(conn, lease)["released_at"] == ""
            assert _unreleased_probe_leases(conn)
            # Worker startup cannot proceed: the gate exits, lease held.
            with pytest.raises(SystemExit) as exitinfo:
                dispatch_lib._reconcile_boot_or_exit(conn)
            assert "boot reconciliation failed" in str(exitinfo.value)
            assert _unreleased_probe_leases(conn)
        finally:
            _release_write_lock(blocker)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5. live/unknown probe -> safe hold, NOT a reconciliation failure
# ---------------------------------------------------------------------------

def test_live_and_unknown_probes_are_safe_holds_not_failures(
        tmp_path, monkeypatch):
    """Safe holds (live / unknown unit) are reported and never fatal: the
    exceptions stay held and the startup gate passes."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _boot_conn(db_path)
    try:
        rid_live, lease_live = _seed_lease(conn)
        rid_unknown, lease_unknown = _seed_lease(conn)
        _patch_units(monkeypatch, {
            _probe_unit(rid_live): "live",
            _probe_unit(rid_unknown): "unknown",
        })
        report = dispatch_lib.reconcile_boot(conn)  # must not raise
        lease_report = report["probe_lease_reconcile"]
        assert lease_report["released"] == 0, report
        assert lease_report["held"] == 2, report
        assert _lease_row(conn, lease_live)["released_at"] == ""
        assert _lease_row(conn, lease_unknown)["released_at"] == ""
        # The startup gate treats a safe hold as a completed reconcile.
        assert dispatch_lib._reconcile_boot_or_exit(conn)
        assert len(_unreleased_probe_leases(conn)) == 2
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. confirmed-stopped probe -> releases exactly once
# ---------------------------------------------------------------------------

def test_confirmed_stopped_releases_exactly_once(tmp_path, monkeypatch):
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _boot_conn(db_path)
    try:
        rid, lease = _seed_lease(conn)
        _patch_units(monkeypatch, {_probe_unit(rid): "confirmed_stopped"})
        first = dispatch_lib.reconcile_boot(conn)
        assert first["probe_lease_reconcile"]["released"] == 1, first
        released_at = _lease_row(conn, lease)["released_at"]
        assert released_at
        second = dispatch_lib.reconcile_boot(conn)
        assert second["probe_lease_reconcile"]["released"] == 0, second
        assert second["probe_lease_reconcile"]["checked"] == 0, second
        assert _lease_row(conn, lease)["released_at"] == released_at
        assert _unreleased_probe_leases(conn) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 7. observation application still occurs before exclusion release
# ---------------------------------------------------------------------------

def test_observation_applied_before_exclusion_release_on_boot(
        tmp_path, monkeypatch):
    """A stored-but-unapplied refresh result is applied BEFORE the bound
    lease is released; the order is observable and the observation lands."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _boot_conn(db_path)
    try:
        rid = probes_lib.enqueue_probe(conn, "api", "hermes", "refresh")
        assert probes_lib.claim_probe(conn, "dispatcher-dead")
        assert probes_lib.finish_probe(conn, rid, "ok",
                                       _refresh_payload("2.0.0"))
        lease = leases_lib.acquire_probe_lease(
            conn, "hermes", "dispatcher-dead", request_id=rid)
        assert lease
        _patch_units(monkeypatch, {_probe_unit(rid): "confirmed_stopped"})
        order = []
        real_observe = observation_lib.reconcile_observations

        def _observe(c):
            order.append("observation")
            return real_observe(c)

        real_release = leases_lib.release_lease

        def _release(c, lease_id, fail_on_storage_error=False):
            order.append("release")
            return real_release(c, lease_id, fail_on_storage_error)

        monkeypatch.setattr(
            observation_lib, "reconcile_observations", _observe)
        monkeypatch.setattr(leases_lib, "release_lease", _release)
        report = dispatch_lib.reconcile_boot(conn)
        assert order == ["observation", "release"], order
        tool = dict(conn.execute(
            "SELECT * FROM tools WHERE id='hermes'").fetchone())
        assert tool["observed_version"] == "2.0.0"
        assert _lease_row(conn, lease)["released_at"] != ""
        assert report["probe_lease_reconcile"]["released"] == 1, report
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 8. ProbeWorker concurrent connection cannot break boot determinism
# ---------------------------------------------------------------------------

def test_concurrent_probe_worker_does_not_break_boot_reconciliation(
        tmp_path, monkeypatch):
    """A live ProbeWorker on its own connection performs short writes
    throughout; every boot reconciliation still completes and releases the
    proven-stopped lease, including a deterministic held-lock round."""
    _state, db_path, _logs = _isolate_settings(tmp_path, monkeypatch)
    conn = _fresh_db(db_path)
    _patch_units(monkeypatch, default="confirmed_stopped")
    worker = dispatch_lib.ProbeWorker(db_path, interval_s=0.01)
    worker.start()
    try:
        assert worker.is_ready()
        for _ in range(15):
            _rid, lease = _seed_lease(conn)
            report = dispatch_lib._reconcile_boot_or_exit(conn)
            assert report["probe_lease_reconcile"]["released"] == 1, report
            assert _lease_row(conn, lease)["released_at"] != ""
            assert _unreleased_probe_leases(conn) == []
        # Deterministic held lock while the worker connection is live.
        _rid, lease = _seed_lease(conn)
        contended_conn = _boot_conn(db_path)
        blocker = _hold_write_lock(db_path)
        sleeps = _release_lock_on_first_retry(monkeypatch, blocker)
        try:
            report = dispatch_lib._reconcile_boot_or_exit(contended_conn)
        finally:
            _release_write_lock(blocker)
            contended_conn.close()
        assert report["probe_lease_reconcile"]["released"] == 1, report
        assert sleeps, "retry backoff was never exercised"
        assert _lease_row(conn, lease)["released_at"] != ""
        assert _unreleased_probe_leases(conn) == []
    finally:
        worker.stop(timeout_s=3.0)
        conn.close()
