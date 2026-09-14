"""W9 durable-probe metadata retention tests (write-only artifacts).

Contract under test: retention removes OLD durable probe metadata
(probe_requests/probe_results/released probe execution_leases) ONLY when
eligibility is positively proven; everything unknown/unresolved is kept.

Protected forever (until positively resolved):
- queued / running probe request;
- any request bound to an unreleased probe lease (modern request_id
  binding or legacy canonical subject binding);
- any result whose observation has not been durably applied
  (probe_requests.result_id != probe_results.finished_at for observation
  ops) or whose status/classification is unknown;
- result evidence that names an unresolved exclusion lease;
- mutation leases (any state) - never in scope.

Eligible:
- terminal request (done with a classified/applied result, or expired
  without one) whose completion timestamp is older than
  metadata_retention_days and whose execution relationship is resolved;
- released probe leases older than the window whose bound request is
  being removed (or was already gone).

Deletion order follows the real FK: probe_results.request_id REFERENCES
probe_requests(id) (child first); execution_leases has NO FK and is
removed only after release proof. One transaction per request keeps an
interrupted run fail-safe; a rerun is idempotent.

Hermetic: tmp SQLite only; no systemd, no /opt, /etc, /var/lib access.
"""
from __future__ import annotations

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
from backend.app import leases as leases_lib
from backend.app import owner_probes as probes_lib
from backend.app import retention as retention_lib

OLD_DAYS = 200          # comfortably past the 90d default window
FRESH_DAYS = 1


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _iso_days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _settings(log_dir, backup_dir, meta_days=90):
    ns = types.SimpleNamespace()
    ns.log_dir = log_dir
    ns.backup_dir = backup_dir
    ns.total_log_cap_bytes = 500 * 1024 * 1024
    ns.completed_log_retention_days = 30
    ns.metadata_retention_days = meta_days
    return ns


def _dirs(tmp_path):
    log_dir = str(tmp_path / "logs")
    backup_dir = str(tmp_path / "backups")
    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(backup_dir, exist_ok=True)
    return log_dir, backup_dir


def _fresh_db(tmp_path, target=None):
    conn = db_lib.connect(str(tmp_path / "state.db"))
    if target is None:
        assert db_lib.migrate(conn) == db_lib.CODE_VERSION
    else:
        db_lib.migrate(conn, target=target)
    conn.commit()
    return conn


def _request(conn, rid, op="refresh", state="done", days_ago=OLD_DAYS,
             tool_id="hermes"):
    conn.execute(
        "INSERT INTO probe_requests(id,subject,tool_id,op,arg_json,"
        "created_at,claim_deadline,state,owner,result_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (rid, "api", tool_id, op, "{}", _iso_days_ago(days_ago),
         _iso_days_ago(days_ago), state, "", ""))
    conn.commit()
    return rid


def _result(conn, rid, status="ok", days_ago=OLD_DAYS, applied=True,
            payload=None, raw=None):
    finished = _iso_days_ago(days_ago)
    doc = raw if raw is not None else json.dumps(payload or {})
    conn.execute(
        "INSERT INTO probe_results(request_id,status,result_json,"
        "finished_at) VALUES(?,?,?,?)", (rid, status, doc, finished))
    if applied:
        conn.execute("UPDATE probe_requests SET result_id=? WHERE id=?",
                     (finished, rid))
    conn.commit()
    return finished


def _lease(conn, lease_id, request_id="", released=False, days_ago=OLD_DAYS,
           kind="probe", subject=""):
    released_at = _iso_days_ago(days_ago) if released else ""
    conn.execute(
        "INSERT INTO execution_leases(id,kind,subject,tool_id,job_id,"
        "request_id,holder,acquired_at,expires_at,released_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (lease_id, kind, subject, "hermes", "", request_id, "dispatcher",
         _iso_days_ago(days_ago + 1), _iso_days_ago(days_ago), released_at))
    conn.commit()
    return lease_id


def _run(conn, log_dir, backup_dir, meta_days=90):
    return retention_lib.run_retention(
        conn, _settings(log_dir, backup_dir, meta_days=meta_days))


def _count(conn, table, where="", params=()):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM %s %s" % (table, where), params).fetchone()
    return int(row["n"])


class _InterruptingConn(object):
    """Connection proxy that fails on the first `fragment` match.

    Used to simulate a retention run interrupted midway (process death)
    inside the per-request transaction.
    """

    def __init__(self, conn, fragment):
        self._conn = conn
        self._fragment = fragment
        self._tripped = False

    def execute(self, sql, *params):
        if not self._tripped and self._fragment in sql:
            self._tripped = True
            raise sqlite3.OperationalError("simulated interruption")
        return self._conn.execute(sql, *params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


# ---------------------------------------------------------------------------
# 1-2. age window
# ---------------------------------------------------------------------------

def test_old_completed_applied_probe_deleted(tmp_path):
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)
    rid = _request(conn, str(uuid.uuid4()))
    _result(conn, rid, days_ago=OLD_DAYS, applied=True)
    _lease(conn, "probe-old-applied", request_id=rid, released=True)

    out = _run(conn, log_dir, bdir)

    assert out["deleted_probe_requests"] == 1
    assert out["deleted_probe_results"] == 1
    assert out["deleted_probe_leases"] == 1
    assert _count(conn, "probe_requests") == 0
    assert _count(conn, "probe_results") == 0
    assert _count(conn, "execution_leases") == 0
    assert out["errors"] == []
    conn.close()


def test_recent_completed_probe_retained(tmp_path):
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)
    rid = _request(conn, str(uuid.uuid4()))
    _result(conn, rid, days_ago=FRESH_DAYS, applied=True)
    _lease(conn, "probe-fresh", request_id=rid, released=True,
           days_ago=FRESH_DAYS)

    out = _run(conn, log_dir, bdir)

    assert out["deleted_probe_requests"] == 0
    assert out["deleted_probe_results"] == 0
    assert out["deleted_probe_leases"] == 0
    assert _count(conn, "probe_requests") == 1
    assert _count(conn, "probe_results") == 1
    assert _count(conn, "execution_leases") == 1
    conn.close()


# ---------------------------------------------------------------------------
# 3-7. protected regardless of age
# ---------------------------------------------------------------------------

def test_queued_probe_retained_regardless_of_age(tmp_path):
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)
    rid = _request(conn, str(uuid.uuid4()), state="queued", days_ago=500)
    out = _run(conn, log_dir, bdir)
    assert out["deleted_probe_requests"] == 0
    assert conn.execute("SELECT state FROM probe_requests WHERE id=?",
                        (rid,)).fetchone()["state"] == "queued"
    conn.close()


def test_running_probe_retained(tmp_path):
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)
    rid = _request(conn, str(uuid.uuid4()), state="running", days_ago=500)
    _lease(conn, "probe-running", request_id=rid, released=False,
           days_ago=500)
    out = _run(conn, log_dir, bdir)
    assert out["deleted_probe_requests"] == 0
    assert out["deleted_probe_leases"] == 0
    assert _count(conn, "probe_requests") == 1
    assert _count(conn, "execution_leases") == 1
    conn.close()


def test_completed_probe_with_unapplied_result_retained(tmp_path):
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)
    rid = _request(conn, str(uuid.uuid4()))
    _result(conn, rid, days_ago=OLD_DAYS, applied=False)
    # Released old lease: still evidence for the retained request/result.
    _lease(conn, "probe-unapplied", request_id=rid, released=True)

    out = _run(conn, log_dir, bdir)

    assert out["deleted_probe_requests"] == 0
    assert out["deleted_probe_results"] == 0
    assert out["deleted_probe_leases"] == 0
    assert _count(conn, "probe_requests") == 1
    assert _count(conn, "probe_results") == 1
    assert _count(conn, "execution_leases") == 1
    conn.close()


def test_completed_probe_with_unreleased_probe_lease_retained(tmp_path):
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)
    rid = _request(conn, str(uuid.uuid4()))
    _result(conn, rid, days_ago=OLD_DAYS, applied=True)
    _lease(conn, "probe-held", request_id=rid, released=False)

    out = _run(conn, log_dir, bdir)

    assert out["deleted_probe_requests"] == 0
    assert out["deleted_probe_results"] == 0
    assert out["deleted_probe_leases"] == 0
    assert _count(conn, "probe_requests") == 1
    assert _count(conn, "execution_leases") == 1
    assert leases_lib.active_probe_leases(conn)
    conn.close()


def test_legacy_subject_bound_unreleased_lease_protects_request(tmp_path):
    """A pre-W4 probe lease may bind the request only by canonical unit
    subject (request_id empty); the request is still protected."""
    from backend.app.owner_env import transient_probe_name

    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)
    rid = _request(conn, str(uuid.uuid4()))
    _result(conn, rid, days_ago=OLD_DAYS, applied=True)
    _lease(conn, "probe-legacy", request_id="", released=False,
           subject=transient_probe_name(rid))

    out = _run(conn, log_dir, bdir)

    assert out["deleted_probe_requests"] == 0
    assert out["deleted_probe_leases"] == 0
    assert _count(conn, "probe_requests") == 1
    conn.close()


def test_unknown_unresolved_stop_evidence_retained(tmp_path):
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)

    # (a) result names an unresolved exclusion lease that is not present
    # (cannot prove the exclusion was ever released).
    held = str(uuid.uuid4())
    _request(conn, held)
    _result(conn, held, days_ago=OLD_DAYS, applied=True, payload={
        "exclusion_held": True, "lease_id": "lease-that-is-gone",
        "reconciliation": "required"})

    # (b) result_json unparsable: parsing failure must never delete.
    broken = str(uuid.uuid4())
    _request(conn, broken)
    _result(conn, broken, days_ago=OLD_DAYS, applied=True,
            raw="not-json{{{")

    # (c) old unreleased, unbound probe lease: unknown stop -> keep lease.
    _lease(conn, "probe-unbound", request_id="", released=False,
           subject="ega-update-probe-unknown.service")

    out = _run(conn, log_dir, bdir)

    assert out["deleted_probe_requests"] == 0
    assert out["deleted_probe_results"] == 0
    assert out["deleted_probe_leases"] == 0
    assert _count(conn, "probe_requests") == 2
    assert _count(conn, "probe_results") == 2
    assert _count(conn, "execution_leases") == 1
    conn.close()


def test_completed_probe_with_unknown_state_retained(tmp_path):
    """Any state outside the terminal pair is unknown -> retain."""
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)
    rid = _request(conn, str(uuid.uuid4()), state="queued-unknown",
                   days_ago=500)
    out = _run(conn, log_dir, bdir)
    assert out["deleted_probe_requests"] == 0
    assert _count(conn, "probe_requests") == 1
    conn.close()


def test_done_probe_without_result_retained(tmp_path):
    """Terminal state without a result is unclassifiable -> retain."""
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)
    _request(conn, str(uuid.uuid4()), state="done", days_ago=500)
    out = _run(conn, log_dir, bdir)
    assert out["deleted_probe_requests"] == 0
    assert _count(conn, "probe_requests") == 1
    conn.close()


# ---------------------------------------------------------------------------
# 8. released probe leases: removed only with safely eligible request
# ---------------------------------------------------------------------------

def test_released_old_probe_lease_removed_only_with_eligible_request(
        tmp_path):
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)

    eligible = str(uuid.uuid4())
    _request(conn, eligible)
    _result(conn, eligible, days_ago=OLD_DAYS, applied=True)
    _lease(conn, "probe-rm", request_id=eligible, released=True)

    blocked = str(uuid.uuid4())
    _request(conn, blocked)
    _result(conn, blocked, days_ago=OLD_DAYS, applied=False)
    _lease(conn, "probe-keep", request_id=blocked, released=True)

    out = _run(conn, log_dir, bdir)

    assert out["deleted_probe_requests"] == 1
    assert out["deleted_probe_leases"] == 1
    assert conn.execute("SELECT 1 FROM probe_requests WHERE id=?",
                        (eligible,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM execution_leases WHERE id=?",
                        ("probe-rm",)).fetchone() is None
    assert conn.execute("SELECT 1 FROM probe_requests WHERE id=?",
                        (blocked,)).fetchone() is not None
    assert conn.execute("SELECT 1 FROM execution_leases WHERE id=?",
                        ("probe-keep",)).fetchone() is not None
    conn.close()


def test_old_released_orphan_probe_lease_removed(tmp_path):
    """A released old probe lease whose request row is already gone is
    resolved evidence; it is collected without a request row."""
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)
    _lease(conn, "probe-orphan", request_id="request-that-is-gone",
           released=True)
    out = _run(conn, log_dir, bdir)
    assert out["deleted_probe_leases"] == 1
    assert _count(conn, "execution_leases") == 0
    conn.close()


# ---------------------------------------------------------------------------
# 9. mutation leases untouched
# ---------------------------------------------------------------------------

def test_mutation_leases_untouched(tmp_path):
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)
    _lease(conn, "mutation-released", released=True, kind="mutation")
    _lease(conn, "mutation-held", released=False, kind="mutation")
    out = _run(conn, log_dir, bdir)
    assert out["deleted_probe_leases"] == 0
    assert _count(conn, "execution_leases") == 2
    assert _count(conn, "execution_leases",
                  "WHERE kind='mutation'") == 2
    conn.close()


# ---------------------------------------------------------------------------
# 10. deletion order satisfies the real FK
# ---------------------------------------------------------------------------

def test_deletion_order_satisfies_fk(tmp_path):
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)
    fk = [dict(r) for r in conn.execute(
        "PRAGMA foreign_key_list(probe_results)").fetchall()]
    assert any(row.get("table") == "probe_requests" for row in fk)

    rid = str(uuid.uuid4())
    _request(conn, rid)
    _result(conn, rid, days_ago=OLD_DAYS, applied=True)

    # The child row really does guard the parent: request-first fails.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM probe_requests WHERE id=?", (rid,))
    try:
        conn.rollback()
    except Exception:
        pass

    out = _run(conn, log_dir, bdir)
    assert out["deleted_probe_requests"] == 1
    assert out["deleted_probe_results"] == 1
    assert _count(conn, "probe_requests") == 0
    assert _count(conn, "probe_results") == 0
    assert out["errors"] == []
    conn.close()


# ---------------------------------------------------------------------------
# 11-12. interruption fail-safe + idempotence
# ---------------------------------------------------------------------------

def test_retention_interruption_midway_fail_safe(tmp_path):
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)

    rid = str(uuid.uuid4())
    _request(conn, rid)
    _result(conn, rid, days_ago=OLD_DAYS, applied=True)
    _lease(conn, "probe-interrupt", request_id=rid, released=True)

    protected = str(uuid.uuid4())
    _request(conn, protected)
    _result(conn, protected, days_ago=OLD_DAYS, applied=False)

    proxy = _InterruptingConn(conn, "DELETE FROM probe_requests")
    out = _run(proxy, log_dir, bdir)
    assert isinstance(out["errors"], list) and out["errors"], \
        "interruption must be reported through the retention result"
    # The per-request transaction rolled back: evidence fully intact.
    assert _count(conn, "probe_requests") == 2
    assert _count(conn, "probe_results") == 2
    assert _count(conn, "execution_leases") == 1

    # A later clean run converges (idempotent recovery).
    out2 = _run(conn, log_dir, bdir)
    assert out2["deleted_probe_requests"] == 1
    assert out2["deleted_probe_results"] == 1
    assert out2["deleted_probe_leases"] == 1
    assert _count(conn, "probe_requests") == 1
    assert conn.execute("SELECT 1 FROM probe_requests WHERE id=?",
                        (protected,)).fetchone() is not None
    conn.close()


def test_repeated_retention_is_idempotent(tmp_path):
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)
    rid = str(uuid.uuid4())
    _request(conn, rid)
    _result(conn, rid, days_ago=OLD_DAYS, applied=True)
    _lease(conn, "probe-once", request_id=rid, released=True)

    first = _run(conn, log_dir, bdir)
    assert first["deleted_probe_requests"] == 1
    second = _run(conn, log_dir, bdir)
    assert second["deleted_probe_requests"] == 0
    assert second["deleted_probe_results"] == 0
    assert second["deleted_probe_leases"] == 0
    assert second["errors"] == []
    assert _count(conn, "probe_requests") == 0
    assert _count(conn, "probe_results") == 0
    assert _count(conn, "execution_leases") == 0
    conn.close()


# ---------------------------------------------------------------------------
# 13. missing older-schema tables tolerated
# ---------------------------------------------------------------------------

def test_missing_probe_tables_tolerated(tmp_path):
    """Pre-probe schema (v2): retention runs, deletes nothing, no crash."""
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path, target=2)
    assert not retention_lib._table_exists(conn, "probe_requests")
    out = _run(conn, log_dir, bdir)
    assert out["deleted_probe_requests"] == 0
    assert out["deleted_probe_results"] == 0
    assert out["deleted_probe_leases"] == 0
    assert isinstance(out["errors"], list)
    conn.close()


def test_missing_results_table_keeps_requests(tmp_path):
    """probe_results absent: eligibility unprovable -> retain everything."""
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)
    rid = str(uuid.uuid4())
    _request(conn, rid)
    _result(conn, rid, days_ago=OLD_DAYS, applied=True)
    conn.execute("DROP TABLE probe_results")
    conn.commit()
    out = _run(conn, log_dir, bdir)
    assert out["deleted_probe_requests"] == 0
    assert _count(conn, "probe_requests") == 1
    conn.close()


def test_missing_leases_table_keeps_probes(tmp_path):
    """execution_leases absent: stop resolution unprovable -> retain."""
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)
    rid = str(uuid.uuid4())
    _request(conn, rid)
    _result(conn, rid, days_ago=OLD_DAYS, applied=True)
    conn.execute("DROP TABLE execution_leases")
    conn.commit()
    out = _run(conn, log_dir, bdir)
    assert out["deleted_probe_requests"] == 0
    assert _count(conn, "probe_requests") == 1
    conn.close()


# ---------------------------------------------------------------------------
# 14. admission/lifecycle behavior unchanged
# ---------------------------------------------------------------------------

def test_admission_behavior_unchanged_after_retention(tmp_path):
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)

    held = str(uuid.uuid4())
    _request(conn, held)
    _result(conn, held, days_ago=OLD_DAYS, applied=True)
    _lease(conn, "probe-still-held", request_id=held, released=False)

    _run(conn, log_dir, bdir)

    # The unresolved probe lease survives retention and still blocks a
    # mutation lease acquisition inside a reservation transaction.
    assert len(leases_lib.active_probe_leases(conn)) == 1
    conn.execute("BEGIN IMMEDIATE")
    try:
        acquired = leases_lib.acquire_mutation_lease(conn, "job-x", "test")
    finally:
        conn.execute("ROLLBACK")
    assert acquired is False

    # Probe admission/lifecycle still works: enqueue -> claim -> finish.
    new_id = probes_lib.enqueue_probe(conn, "api", "hermes", "inspect")
    claimed = probes_lib.claim_probe(conn, "test-owner")
    assert claimed is not None
    assert str(claimed["id"]) == new_id
    assert probes_lib.finish_probe(conn, new_id, "ok", {"ok": True}) is True
    conn.close()


# ---------------------------------------------------------------------------
# extra: non-observation result classified -> eligible; tombstone bounded
# ---------------------------------------------------------------------------

def test_old_inspect_probe_classified_result_deleted(tmp_path):
    """Non-observation ops have no applied marker; a stored terminal
    status is the classification and makes the old row eligible."""
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)
    rid = str(uuid.uuid4())
    _request(conn, rid, op="inspect", state="done")
    _result(conn, rid, status="ok", days_ago=OLD_DAYS, applied=False)
    out = _run(conn, log_dir, bdir)
    assert out["deleted_probe_requests"] == 1
    conn.close()


def test_probe_tombstone_is_bounded_per_run(tmp_path):
    """Probe removals are tombstoned with ONE bounded summary row, not
    one row per deleted probe."""
    log_dir, bdir = _dirs(tmp_path)
    conn = _fresh_db(tmp_path)
    for _i in range(5):
        rid = str(uuid.uuid4())
        _request(conn, rid)
        _result(conn, rid, days_ago=OLD_DAYS, applied=True)
    out = _run(conn, log_dir, bdir)
    assert out["deleted_probe_requests"] == 5
    probe_tombstones = conn.execute(
        "SELECT COUNT(*) AS n FROM tombstones WHERE kind='probe'").fetchone()
    assert int(probe_tombstones["n"]) == 1
    conn.close()
